"""Tests for mcp-server/server.py."""
import importlib
import json
import logging
import sys

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

from tests.conftest import AUDIENCE, ISSUER, TENANT

API_KEY = "test-api-key-12345678901234567890"

BASE_ENV = {
    "MCP_API_KEY": API_KEY,
    "TAVILY_API_KEY": "tvly-test",
    "AIGUARD_API_KEY": "aig-test",
    "AIGUARD_POLICY_ID": "123",
}

MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


def rpc(method, params=None, id_=1):
    return {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}


def init_payload():
    return rpc(
        "initialize",
        {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "1.0"},
        },
        id_=1,
    )


def call_payload(query="test query"):
    return rpc("tools/call", {"name": "web_search", "arguments": {"query": query}}, id_=2)


def tool_result(resp):
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "error" not in body, body
    res = body["result"]
    if res.get("structuredContent"):
        sc = res["structuredContent"]
        return sc.get("result", sc)
    return json.loads(res["content"][0]["text"])


def load_server(monkeypatch, env, keypair=None):
    """Load or reload server.py with specific environment variables."""
    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)

    if "server" in sys.modules:
        del sys.modules["server"]

    import server

    if keypair and server.verifier is not None:
        async def mock_fetch_jwks(url):
            return keypair[1]

        server.fetch_jwks = mock_fetch_jwks
        server.verifier._fetch = mock_fetch_jwks
        server.verifier._keys = {}
        server.verifier._fetched_at = 0.0

    return server


# Case 1: /healthz 200 with no auth
def test_case_1_healthz_no_auth(monkeypatch, proxy):
    env = {**BASE_ENV, "ZIA_PROXY": f"127.0.0.1:{proxy.port}"}
    srv = load_server(monkeypatch, env)
    with TestClient(srv.app) as client:
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json() == {"ok": True}


# Case 2: no credential -> 401; x-api-key -> 200
def test_case_2_auth_modes(monkeypatch, proxy):
    env = {**BASE_ENV, "ZIA_PROXY": f"127.0.0.1:{proxy.port}"}
    srv = load_server(monkeypatch, env)
    with TestClient(srv.app) as client:
        # No credential -> 401
        r_no_auth = client.post("/mcp", headers=MCP_HEADERS, json=init_payload())
        assert r_no_auth.status_code == 401
        assert r_no_auth.json() == {"error": "unauthorized"}

        # Bad x-api-key -> 401
        r_bad_key = client.post("/mcp", headers={**MCP_HEADERS, "x-api-key": "wrong"}, json=init_payload())
        assert r_bad_key.status_code == 401

        # Valid x-api-key -> 200
        r_good_key = client.post("/mcp", headers={**MCP_HEADERS, "x-api-key": API_KEY}, json=init_payload())
        assert r_good_key.status_code == 200


# Case 3: valid bearer -> 200 and log contains "as <email>"; bad signature -> 401; token without mcp.access -> 403
def test_case_3_bearer_auth(monkeypatch, proxy, keypair, make_token, caplog):
    env = {
        **BASE_ENV,
        "ZIA_PROXY": f"127.0.0.1:{proxy.port}",
        "MCP_AUTH": "entra",
        "ENTRA_TENANT_ID": TENANT,
        "MCP_AUDIENCE": AUDIENCE,
        "MCP_REQUIRED_SCOPES": "mcp.access",
    }
    srv = load_server(monkeypatch, env, keypair=keypair)
    with TestClient(srv.app) as client:
        # Valid bearer -> 200 and log contains "as <email>"
        token = make_token(email="alice@example.com")
        with caplog.at_level(logging.INFO):
            r_valid = client.post("/mcp", headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"}, json=init_payload())
        assert r_valid.status_code == 200
        assert "as alice@example.com" in caplog.text

        # Bad signature -> 401
        other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        bad_token = make_token(signer=other_key)
        r_bad_sig = client.post("/mcp", headers={**MCP_HEADERS, "Authorization": f"Bearer {bad_token}"}, json=init_payload())
        assert r_bad_sig.status_code == 401
        assert r_bad_sig.json() == {"error": "unauthorized"}

        # Token without mcp.access -> 403
        no_scope_token = make_token(scp="other.scope")
        r_no_scope = client.post("/mcp", headers={**MCP_HEADERS, "Authorization": f"Bearer {no_scope_token}"}, json=init_payload())
        assert r_no_scope.status_code == 403
        assert r_no_scope.json() == {"error": "insufficient_scope"}


# Case 4: CONTEXTVAR PROOF: ZIA_EGRESS_MODE=proxy-caller, EGRESS.exchange patched to return "egress-token-123",
# fake proxy answers 407 -> tool result is blocked AND the fake proxy recorded "Proxy-Authorization: Bearer egress-token-123" on the CONNECT
def test_case_4_contextvar_proof(monkeypatch, proxy, keypair, make_token):
    env = {
        **BASE_ENV,
        "ZIA_PROXY": f"127.0.0.1:{proxy.port}",
        "ZIA_EGRESS_MODE": "proxy-caller",
        "MCP_AUTH": "entra",
        "ENTRA_TENANT_ID": TENANT,
        "MCP_AUDIENCE": AUDIENCE,
        "MCP_REQUIRED_SCOPES": "mcp.access",
        "MCP_CLIENT_ID": "client-id",
        "MCP_CLIENT_SECRET": "client-secret",
        "ZIA_EGRESS_SCOPE": "api://zia/zia:egress",
    }
    srv = load_server(monkeypatch, env, keypair=keypair)

    # Patch EGRESS.exchange to return "egress-token-123"
    async def mock_exchange(token):
        return "egress-token-123"

    monkeypatch.setattr(srv.EGRESS, "exchange", mock_exchange)

    with TestClient(srv.app) as client:
        token = make_token()
        headers = {**MCP_HEADERS, "Authorization": f"Bearer {token}"}

        # initialize then tools/call
        r_init = client.post("/mcp", headers=headers, json=init_payload())
        assert r_init.status_code == 200

        r_call = client.post("/mcp", headers=headers, json=call_payload("test query"))
        out = tool_result(r_call)
        assert out["blocked"] is True
        # Verify fake proxy recorded the header on CONNECT
        assert len(proxy.requests) > 0
        connect_line, connect_headers = proxy.headers()
        assert connect_line.startswith("CONNECT ")
        assert connect_headers.get("proxy-authorization") == "Bearer egress-token-123"


# Case 5: proxy-caller + x-api-key -> blocked with "no caller token"
def test_case_5_proxy_caller_with_api_key(monkeypatch, proxy, keypair):
    env = {
        **BASE_ENV,
        "ZIA_PROXY": f"127.0.0.1:{proxy.port}",
        "ZIA_EGRESS_MODE": "proxy-caller",
        "MCP_AUTH": "entra",
        "ENTRA_TENANT_ID": TENANT,
        "MCP_AUDIENCE": AUDIENCE,
        "MCP_REQUIRED_SCOPES": "mcp.access",
        "MCP_CLIENT_ID": "client-id",
        "MCP_CLIENT_SECRET": "client-secret",
        "ZIA_EGRESS_SCOPE": "api://zia/zia:egress",
    }
    srv = load_server(monkeypatch, env, keypair=keypair)
    with TestClient(srv.app) as client:
        headers = {**MCP_HEADERS, "x-api-key": API_KEY}

        r_init = client.post("/mcp", headers=headers, json=init_payload())
        assert r_init.status_code == 200

        r_call = client.post("/mcp", headers=headers, json=call_payload("test query"))
        out = tool_result(r_call)
        assert out["blocked"] is True
        assert "no caller token" in out["reason"]


# Case 6: mode proxy + x-api-key -> CONNECT carries NO Proxy-Authorization
def test_case_6_mode_proxy_with_api_key(monkeypatch, proxy):
    env = {
        **BASE_ENV,
        "ZIA_PROXY": f"127.0.0.1:{proxy.port}",
        "ZIA_EGRESS_MODE": "proxy",
    }
    srv = load_server(monkeypatch, env)
    with TestClient(srv.app) as client:
        headers = {**MCP_HEADERS, "x-api-key": API_KEY}

        r_init = client.post("/mcp", headers=headers, json=init_payload())
        assert r_init.status_code == 200

        r_call = client.post("/mcp", headers=headers, json=call_payload("test query"))
        out = tool_result(r_call)
        assert out["blocked"] is True  # blocked because fake proxy returns 407 to aiguard
        assert len(proxy.requests) > 0
        connect_line, connect_headers = proxy.headers()
        assert connect_line.startswith("CONNECT ")
        assert "proxy-authorization" not in connect_headers


# Case 7: AI Guard body carries "user" = token email (the ZIA username) when bearer, none with x-api-key
def test_case_7_aiguard_user_field(monkeypatch, proxy, keypair, make_token):
    env = {
        **BASE_ENV,
        "ZIA_PROXY": f"127.0.0.1:{proxy.port}",
        "ZIA_EGRESS_MODE": "direct",
        "MCP_AUTH": "entra",
        "ENTRA_TENANT_ID": TENANT,
        "MCP_AUDIENCE": AUDIENCE,
        "MCP_REQUIRED_SCOPES": "mcp.access",
    }
    srv = load_server(monkeypatch, env, keypair=keypair)

    captured_calls = []

    async def mock_aiguard(client, direction, content, user=None):
        captured_calls.append({"direction": direction, "content": content, "user": user})
        # Block immediately to return fast without calling Tavily
        return False, "tx-mock", ["mock_block"]

    monkeypatch.setattr(srv, "aiguard", mock_aiguard)
    with TestClient(srv.app) as client:
        # 1. Bearer token with UPN
        token = make_token(upn="user_upn@corp.com", email="other@corp.com")
        headers_bearer = {**MCP_HEADERS, "Authorization": f"Bearer {token}"}
        client.post("/mcp", headers=headers_bearer, json=init_payload())
        client.post("/mcp", headers=headers_bearer, json=call_payload("query 1"))
        assert len(captured_calls) == 1
        assert captured_calls[0]["user"] == "other@corp.com"

        # 2. API key
        captured_calls.clear()
        headers_key = {**MCP_HEADERS, "x-api-key": API_KEY}
        client.post("/mcp", headers=headers_key, json=init_payload())
        client.post("/mcp", headers=headers_key, json=call_payload("query 2"))
        assert len(captured_calls) == 1
        assert captured_calls[0]["user"] is None
