"""Offline unit tests for mcp-jumphost."""
import gzip
import json
import os
import sys

import httpx
import pytest
from starlette.testclient import TestClient

# Setup default test environment variables before importing jumphost
os.environ.setdefault("ROUTES", json.dumps({"dlptest": "https://mcp.dlptest.com/api/mcp/"}))
os.environ.setdefault("MCP_AUTH", "entra")
os.environ.setdefault("ENTRA_TENANT_ID", "00000000-0000-0000-0000-0000000000aa")
os.environ.setdefault("MCP_AUDIENCE", "11111111-1111-1111-1111-111111111111")
os.environ.setdefault("ZIA_EGRESS_MODE", "direct")
os.environ.setdefault("MCP_PUBLIC_BASE", "https://jumphost.internal")

# Add mcp-jumphost to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import auth
import egress
import jumphost
from jumphost import app, create_app, parse_routes


def mock_response(status_code=200, headers=None, json_data=None, content=None):
    hdrs = dict(headers or {})
    if json_data is not None:
        content = json.dumps(json_data).encode()
        hdrs.setdefault("Content-Type", "application/json")
    content = content or b""
    return httpx.Response(status_code, headers=hdrs, stream=httpx.ByteStream(content))


class DummyCaller:
    def __init__(self, token="valid-token", identity="alice@example.com", scopes=None):
        self.token = token
        self.identity = identity
        self.scopes = scopes or ["mcp.access"]


class DummyVerifier:
    def __init__(self, valid_token="valid-token", scopes=None):
        self.valid_token = valid_token
        self.scopes = scopes or ["mcp.access"]

    async def verify_token(self, token: str):
        if token == self.valid_token:
            return DummyCaller(token=token, scopes=self.scopes)
        return None


def test_no_token_returns_401():
    client = TestClient(app)
    resp = client.post("/dlptest/mcp", json={"jsonrpc": "2.0", "method": "initialize", "id": 1})
    assert resp.status_code == 401
    auth_header = resp.headers.get("www-authenticate", "")
    assert 'Bearer resource_metadata="https://jumphost.internal/.well-known/oauth-protected-resource/dlptest/mcp"' in auth_header
    body = resp.json()
    assert body["jsonrpc"] == "2.0"
    assert body["error"]["message"] == "unauthorized"
    assert body["id"] == 1


def test_bad_token_returns_401(monkeypatch):
    monkeypatch.setattr(jumphost, "verifier", DummyVerifier("good-token"))
    client = TestClient(app)
    resp = client.post(
        "/dlptest/mcp",
        headers={"Authorization": "Bearer bad-token"},
        json={"jsonrpc": "2.0", "method": "ping", "id": 2},
    )
    assert resp.status_code == 401
    auth_header = resp.headers.get("www-authenticate", "")
    assert 'Bearer resource_metadata="https://jumphost.internal/.well-known/oauth-protected-resource/dlptest/mcp"' in auth_header
    body = resp.json()
    assert body["error"]["message"] == "unauthorized"
    assert body["id"] == 2


def test_metadata_endpoint_content():
    client = TestClient(app)
    resp = client.get("/.well-known/oauth-protected-resource/dlptest/mcp")
    assert resp.status_code == 200
    data = resp.json()
    assert data["resource"] == "https://jumphost.internal/dlptest/mcp"
    assert data["authorization_servers"] == [jumphost.IDP.issuer]
    assert isinstance(data["scopes_supported"], list)


def test_unknown_route_returns_404():
    client = TestClient(app)
    resp = client.post("/nonexistent/mcp", headers={"Authorization": "Bearer valid-token"})
    assert resp.status_code == 404

    resp_meta = client.get("/.well-known/oauth-protected-resource/nonexistent/mcp")
    assert resp_meta.status_code == 404


def test_headers_forwarding_allowlist(monkeypatch):
    upstream_requests = []

    def mock_handler(request: httpx.Request):
        upstream_requests.append(request)
        return mock_response(200, json_data={"jsonrpc": "2.0", "result": "ok", "id": 1})

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))

    class DummyEgress:
        async def client(self, caller=None):
            return mock_client

    monkeypatch.setattr(jumphost, "verifier", DummyVerifier("valid-token"))
    monkeypatch.setattr(jumphost, "EGRESS", DummyEgress())

    client = TestClient(app)
    headers = {
        "Authorization": "Bearer valid-token",
        "Cookie": "session=secret123",
        "X-Forwarded-For": "198.51.100.1",
        "Mcp-Session-Id": "sess-xyz",
        "Mcp-Protocol-Version": "2024-11-05",
        "Last-Event-ID": "evt-42",
        "User-Agent": "test-client/1.0",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    resp = client.post("/dlptest/mcp", headers=headers, json={"jsonrpc": "2.0", "method": "test", "id": 1})
    assert resp.status_code == 200

    assert len(upstream_requests) == 1
    req = upstream_requests[0]
    # Prohibited headers MUST NOT be forwarded
    assert "authorization" not in req.headers
    assert "cookie" not in req.headers
    assert "x-forwarded-for" not in req.headers

    # Allowlisted headers MUST be forwarded
    assert req.headers.get("mcp-session-id") == "sess-xyz"
    assert req.headers.get("mcp-protocol-version") == "2024-11-05"
    assert req.headers.get("last-event-id") == "evt-42"
    assert req.headers.get("user-agent") == "test-client/1.0"
    assert req.headers.get("accept") == "application/json, text/event-stream"
    assert "application/json" in req.headers.get("content-type", "")


def test_upstream_mcp_session_id_returned(monkeypatch):
    def mock_handler(request: httpx.Request):
        return mock_response(
            200,
            headers={"Mcp-Session-Id": "upstream-sess-999"},
            json_data={"jsonrpc": "2.0", "result": "ok", "id": 1},
        )

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))

    class DummyEgress:
        async def client(self, caller=None):
            return mock_client

    monkeypatch.setattr(jumphost, "verifier", DummyVerifier("valid-token"))
    monkeypatch.setattr(jumphost, "EGRESS", DummyEgress())

    client = TestClient(app)
    resp = client.post(
        "/dlptest/mcp",
        headers={"Authorization": "Bearer valid-token"},
        json={"jsonrpc": "2.0", "method": "test", "id": 1},
    )
    assert resp.status_code == 200
    assert resp.headers.get("mcp-session-id") == "upstream-sess-999"


def test_sse_body_streamed_intact(monkeypatch):
    sse_payload = b"event: message\ndata: {\"jsonrpc\":\"2.0\",\"method\":\"notifications/message\",\"params\":{}}\n\n"

    def mock_handler(request: httpx.Request):
        return mock_response(
            200,
            headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"},
            content=sse_payload,
        )

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))

    class DummyEgress:
        async def client(self, caller=None):
            return mock_client

    monkeypatch.setattr(jumphost, "verifier", DummyVerifier("valid-token"))
    monkeypatch.setattr(jumphost, "EGRESS", DummyEgress())

    client = TestClient(app)
    resp = client.get(
        "/dlptest/mcp",
        headers={"Authorization": "Bearer valid-token", "Accept": "text/event-stream"},
    )
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers.get("content-type", "")
    assert resp.headers.get("cache-control") == "no-cache"
    assert resp.content == sse_payload


def test_gzip_upstream_response_decompressed(monkeypatch):
    raw_json = {"jsonrpc": "2.0", "result": {"message": "decompressed ok"}, "id": 42}
    compressed = gzip.compress(json.dumps(raw_json).encode())

    def mock_handler(request: httpx.Request):
        return mock_response(
            200,
            headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
            content=compressed,
        )

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))

    class DummyEgress:
        async def client(self, caller=None):
            return mock_client

    monkeypatch.setattr(jumphost, "verifier", DummyVerifier("valid-token"))
    monkeypatch.setattr(jumphost, "EGRESS", DummyEgress())

    client = TestClient(app)
    resp = client.post(
        "/dlptest/mcp",
        headers={"Authorization": "Bearer valid-token"},
        json={"jsonrpc": "2.0", "method": "test", "id": 42},
    )
    assert resp.status_code == 200
    assert resp.json() == raw_json
    assert "content-encoding" not in resp.headers


def test_egress_denied_returns_502_and_zero_requests(monkeypatch):
    upstream_requests = []

    def mock_handler(request: httpx.Request):
        upstream_requests.append(request)
        return mock_response(200)

    class DeniedEgress:
        async def client(self, caller=None):
            raise egress.EgressDenied("caller has no ZIA permission")

    monkeypatch.setattr(jumphost, "verifier", DummyVerifier("valid-token"))
    monkeypatch.setattr(jumphost, "EGRESS", DeniedEgress())

    client = TestClient(app)
    resp = client.post(
        "/dlptest/mcp",
        headers={"Authorization": "Bearer valid-token"},
        json={"jsonrpc": "2.0", "method": "initialize", "id": 10},
    )
    assert resp.status_code == 502
    body = resp.json()
    assert body["jsonrpc"] == "2.0"
    assert "egress denied: caller has no ZIA permission" in body["error"]["message"]
    assert body["id"] == 10
    # Crucial: Mock transport must see zero requests
    assert len(upstream_requests) == 0


def test_proxy_error_returns_502(monkeypatch):
    def mock_handler(request: httpx.Request):
        raise httpx.ProxyError("407 Proxy Authentication Required")

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))

    class DummyEgress:
        async def client(self, caller=None):
            return mock_client

    monkeypatch.setattr(jumphost, "verifier", DummyVerifier("valid-token"))
    monkeypatch.setattr(jumphost, "EGRESS", DummyEgress())

    client = TestClient(app)
    resp = client.post(
        "/dlptest/mcp",
        headers={"Authorization": "Bearer valid-token"},
        json={"jsonrpc": "2.0", "method": "initialize", "id": 11},
    )
    assert resp.status_code == 502
    body = resp.json()
    assert body["jsonrpc"] == "2.0"
    assert body["error"]["message"] == "ZIA proxy refused"
    assert body["id"] == 11


def test_query_string_preserved(monkeypatch):
    captured_urls = []

    def mock_handler(request: httpx.Request):
        captured_urls.append(str(request.url))
        return mock_response(200, json_data={"jsonrpc": "2.0", "result": "ok"})

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))

    class DummyEgress:
        async def client(self, caller=None):
            return mock_client

    monkeypatch.setattr(jumphost, "verifier", DummyVerifier("valid-token"))
    monkeypatch.setattr(jumphost, "EGRESS", DummyEgress())

    client = TestClient(app)
    resp = client.post(
        "/dlptest/mcp?session_id=123&env=prod",
        headers={"Authorization": "Bearer valid-token"},
        json={"jsonrpc": "2.0", "method": "ping"},
    )
    assert resp.status_code == 200
    assert len(captured_urls) == 1
    assert captured_urls[0] == "https://mcp.dlptest.com/api/mcp/?session_id=123&env=prod"


def test_upstream_trailing_slash_preserved(monkeypatch):
    captured_urls = []

    def mock_handler(request: httpx.Request):
        captured_urls.append(str(request.url))
        return mock_response(200, json_data={"jsonrpc": "2.0", "result": "ok"})

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))

    class DummyEgress:
        async def client(self, caller=None):
            return mock_client

    monkeypatch.setattr(jumphost, "verifier", DummyVerifier("valid-token"))
    monkeypatch.setattr(jumphost, "EGRESS", DummyEgress())

    client = TestClient(app)
    resp = client.post(
        "/dlptest/mcp",
        headers={"Authorization": "Bearer valid-token"},
        json={"jsonrpc": "2.0", "method": "ping"},
    )
    assert resp.status_code == 200
    assert len(captured_urls) == 1
    assert captured_urls[0] == "https://mcp.dlptest.com/api/mcp/"
    assert captured_urls[0].endswith("/")


def test_insufficient_scope_returns_403(monkeypatch):
    class ScopedIdp:
        issuer = "https://login.microsoftonline.com/00000000-0000-0000-0000-0000000000aa/v2.0"
        required_scopes = ("mcp.admin",)

    monkeypatch.setattr(jumphost, "IDP", ScopedIdp())
    monkeypatch.setattr(jumphost, "verifier", DummyVerifier("valid-token", scopes=["mcp.access"]))

    client = TestClient(app)
    resp = client.post(
        "/dlptest/mcp",
        headers={"Authorization": "Bearer valid-token"},
        json={"jsonrpc": "2.0", "method": "test", "id": 5},
    )
    assert resp.status_code == 403
    body = resp.json()
    assert body["error"]["message"] == "insufficient_scope"
    assert 'Bearer error="insufficient_scope"' in resp.headers.get("www-authenticate", "")


def test_healthz_endpoint():
    client = TestClient(app)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.text == "ok"


def test_bad_routes_raises_system_exit():
    bad_routes = [
        "",
        "   ",
        "not-json",
        "[]",
        "{}",
        json.dumps({"test": "http://insecure.com"}),
        json.dumps({"": "https://example.com"}),
        json.dumps({"test": 123}),
    ]
    for bad in bad_routes:
        with pytest.raises(SystemExit):
            parse_routes(bad)
