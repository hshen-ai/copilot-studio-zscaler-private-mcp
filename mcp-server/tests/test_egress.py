import asyncio
from types import SimpleNamespace

import httpx
import pytest

from auth import idp_from_env
from egress import Egress, EgressDenied, egress_from_env, secret, ssl_context
from tests.conftest import AUDIENCE, OKTA_ISSUER, TENANT

ENTRA = idp_from_env({"MCP_AUTH": "entra", "ENTRA_TENANT_ID": TENANT, "MCP_AUDIENCE": AUDIENCE})
OKTA = idp_from_env({"MCP_AUTH": "okta", "OKTA_ISSUER": OKTA_ISSUER, "MCP_AUDIENCE": "api://zia-mcp"})


def run(coro):
    return asyncio.run(coro)


def idp_mock(responses, seen):
    def handler(request):
        seen.append(request)
        status, body = responses.pop(0) if len(responses) > 1 else responses[0]
        return httpx.Response(status, json=body)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def hit_through(eg, caller, url="https://upstream.test/x"):
    async with await eg.client(caller) as c:
        return await c.get(url)


def test_secret_prefers_file(tmp_path):
    f = tmp_path / "s"
    f.write_text("from-file\n")
    assert secret({"X": "env", "X_FILE": str(f)}, "X") == "from-file"
    assert secret({"X": " env "}, "X") == "env"
    assert secret({}, "X") == ""


def test_ssl_context_accepts_extra_ca(tmp_path):
    import certifi
    assert ssl_context(None)
    ca = tmp_path / "ca.pem"
    ca.write_text(open(certifi.where()).read().split("-----END CERTIFICATE-----")[0] + "-----END CERTIFICATE-----\n")
    assert ssl_context(str(ca))


@pytest.mark.parametrize("env, msg", [
    ({"ZIA_EGRESS_MODE": "socks"}, "ZIA_EGRESS_MODE must be one of"),
    ({"ZIA_EGRESS_MODE": "proxy"}, "ZIA_PROXY=host:port"),
    ({"ZIA_EGRESS_MODE": "proxy", "ZIA_PROXY": "gateway.example.net"}, "ZIA_PROXY=host:port"),
    ({"ZIA_EGRESS_MODE": "proxy-token-file", "ZIA_PROXY": "p:80"}, "ZIA_PROXY_TOKEN_FILE"),
    ({"ZIA_EGRESS_MODE": "proxy-caller", "ZIA_PROXY": "p:80"}, "MCP_CLIENT_ID"),
])
def test_bad_egress_config_fails_fast(env, msg):
    with pytest.raises(SystemExit, match=msg):
        egress_from_env(env, ENTRA)


def test_okta_caller_mode_needs_audience():
    with pytest.raises(SystemExit, match="ZIA_EGRESS_AUDIENCE"):
        egress_from_env({"ZIA_EGRESS_MODE": "proxy-caller", "ZIA_PROXY": "p:80", "MCP_CLIENT_ID": "c",
                         "MCP_CLIENT_SECRET": "s", "ZIA_EGRESS_SCOPE": "zia:egress"}, OKTA)


def test_direct_ignores_proxy_env(monkeypatch, search_stub):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    eg = Egress(egress_from_env({"ZIA_EGRESS_MODE": "direct"}, ENTRA), ssl_context(None), None)

    async def go():
        async with await eg.client() as c:
            return await c.post(search_stub.url, json={})
    assert run(go()).status_code == 200


def test_proxy_mode_sends_connect_without_credential(proxy):
    eg = Egress(egress_from_env({"ZIA_EGRESS_MODE": "proxy", "ZIA_PROXY": f"127.0.0.1:{proxy.port}"}, ENTRA),
                ssl_context(None), None)
    with pytest.raises(httpx.ProxyError, match="407"):
        run(hit_through(eg, None))
    line, headers = proxy.headers()
    assert line == "CONNECT upstream.test:443 HTTP/1.1"
    assert "proxy-authorization" not in headers


def test_token_file_mode_reads_the_file_every_call(proxy, tmp_path):
    f = tmp_path / "zia.jwt"
    f.write_text("tok-one\n")
    eg = Egress(egress_from_env({"ZIA_EGRESS_MODE": "proxy-token-file", "ZIA_PROXY": f"127.0.0.1:{proxy.port}",
                                 "ZIA_PROXY_TOKEN_FILE": str(f)}, ENTRA), ssl_context(None), None)
    for expected in ("tok-one", "tok-two"):
        f.write_text(expected)
        with pytest.raises(httpx.ProxyError):
            run(hit_through(eg, None))
        assert proxy.headers()[1]["proxy-authorization"] == f"Bearer {expected}"


def test_token_file_missing_or_empty_is_denied(tmp_path):
    f = tmp_path / "zia.jwt"
    eg = Egress(egress_from_env({"ZIA_EGRESS_MODE": "proxy-token-file", "ZIA_PROXY": "127.0.0.1:9",
                                 "ZIA_PROXY_TOKEN_FILE": str(f)}, ENTRA), ssl_context(None), None)
    with pytest.raises(EgressDenied, match="unreadable"):
        run(eg.client())
    f.write_text("  ")
    with pytest.raises(EgressDenied, match="empty"):
        run(eg.client())


def _caller_egress(idp, port, idp_http, **extra):
    env = {"ZIA_EGRESS_MODE": "proxy-caller", "ZIA_PROXY": f"127.0.0.1:{port}", "MCP_CLIENT_ID": "mcp-app",
           "MCP_CLIENT_SECRET": "s3cret", "ZIA_EGRESS_SCOPE": "api://zia/zia:egress", **extra}
    return Egress(egress_from_env(env, idp), ssl_context(None), idp_http)


def test_entra_on_behalf_of_puts_the_callers_token_on_the_connect(proxy):
    seen = []
    eg = _caller_egress(ENTRA, proxy.port, idp_mock([(200, {"access_token": "zia-for-alice", "expires_in": 3600})], seen))
    with pytest.raises(httpx.ProxyError):
        run(hit_through(eg, SimpleNamespace(token="alice-inbound")))
    assert proxy.headers()[1]["proxy-authorization"] == "Bearer zia-for-alice"
    form = dict(httpx.QueryParams(seen[0].content.decode()))
    assert seen[0].url == ENTRA.token_url
    assert form["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
    assert form["requested_token_use"] == "on_behalf_of"
    assert form["assertion"] == "alice-inbound" and form["scope"] == "api://zia/zia:egress"
    assert form["client_id"] == "mcp-app" and form["client_secret"] == "s3cret"


def test_okta_token_exchange_form_and_basic_auth(proxy):
    seen = []
    eg = _caller_egress(OKTA, proxy.port, idp_mock([(200, {"access_token": "zia-okta", "expires_in": 3600})], seen),
                        ZIA_EGRESS_AUDIENCE="api://zia-egress", ZIA_EGRESS_TOKEN_URL="https://example.okta.com/oauth2/aus9/v1/token")
    with pytest.raises(httpx.ProxyError):
        run(hit_through(eg, SimpleNamespace(token="bob-inbound")))
    assert proxy.headers()[1]["proxy-authorization"] == "Bearer zia-okta"
    form = dict(httpx.QueryParams(seen[0].content.decode()))
    assert str(seen[0].url) == "https://example.okta.com/oauth2/aus9/v1/token"
    assert form["grant_type"] == "urn:ietf:params:oauth:grant-type:token-exchange"
    assert form["subject_token"] == "bob-inbound" and form["audience"] == "api://zia-egress"
    assert form["subject_token_type"] == "urn:ietf:params:oauth:token-type:access_token"
    assert seen[0].headers["authorization"].startswith("Basic ")
    assert "client_secret" not in form


def test_exchange_is_cached_per_caller_token_and_refreshed_near_expiry(proxy):
    seen = []
    eg = _caller_egress(ENTRA, proxy.port, idp_mock([(200, {"access_token": "t", "expires_in": 3600})], seen))
    run(eg.exchange("alice"))
    run(eg.exchange("alice"))
    assert len(seen) == 1
    run(eg.exchange("bob"))
    assert len(seen) == 2
    eg2 = _caller_egress(ENTRA, proxy.port, idp_mock([(200, {"access_token": "t", "expires_in": 30})], seen2 := []))
    run(eg2.exchange("alice"))
    run(eg2.exchange("alice"))           # expires within the 60 s margin -> exchanged again
    assert len(seen2) == 2


@pytest.mark.parametrize("response, msg", [
    ((400, {"error": "invalid_grant", "error_description": "AADSTS50013: Assertion failed\nTrace"}), "invalid_grant AADSTS50013"),
    ((200, {"no": "token"}), "HTTP 200"),
])
def test_exchange_failure_is_denied_and_nothing_connects(proxy, response, msg):
    eg = _caller_egress(ENTRA, proxy.port, idp_mock([response], []))
    with pytest.raises(EgressDenied, match=msg):
        run(eg.client(SimpleNamespace(token="alice")))
    assert proxy.requests == []


def test_caller_mode_without_caller_token_is_denied(proxy):
    eg = _caller_egress(ENTRA, proxy.port, idp_mock([(200, {"access_token": "t"})], []))
    with pytest.raises(EgressDenied, match="no caller token"):
        run(eg.client(None))


def test_unreachable_token_endpoint_is_denied(proxy):
    def boom(request):
        raise httpx.ConnectError("down")
    eg = _caller_egress(ENTRA, proxy.port, httpx.AsyncClient(transport=httpx.MockTransport(boom)))
    with pytest.raises(EgressDenied, match="unreachable"):
        run(eg.exchange("alice"))
