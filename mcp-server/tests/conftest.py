"""Offline fixtures: an RSA signing key + JWKS, a token factory, a CONNECT-recording proxy, a stub search API."""
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

TENANT = "00000000-0000-0000-0000-0000000000aa"
AUDIENCE = "11111111-1111-1111-1111-111111111111"
ISSUER = f"https://login.microsoftonline.com/{TENANT}/v2.0"
OKTA_ISSUER = "https://example.okta.com/oauth2/aus000000000000000000"


@pytest.fixture(scope="session")
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    jwk.update(kid="k1", use="sig", alg="RS256")
    return key, {"keys": [jwk]}


@pytest.fixture
def make_token(keypair):
    key, _ = keypair

    def _make(kid="k1", signer=None, **overrides):
        now = int(time.time())
        claims = {"iss": ISSUER, "aud": AUDIENCE, "exp": now + 3600, "iat": now, "sub": "pairwise-sub",
                  "oid": "oid-1", "email": "alice@example.com", "scp": "mcp.access", "azp": "agent-app"}
        claims.update(overrides)
        claims = {k: v for k, v in claims.items() if v is not None}
        return jwt.encode(claims, signer or key, algorithm="RS256", headers={"kid": kid})
    return _make


@pytest.fixture
def fetch_jwks(keypair):
    calls = []

    async def _fetch(uri):
        calls.append(uri)
        return keypair[1]
    _fetch.calls = calls
    return _fetch


class _Proxy:
    """Accepts one CONNECT per connection, records the request head, answers with `reply`."""

    def __init__(self, reply=b"HTTP/1.1 407 Unauthorized\r\nProxy-Authenticate: Bearer\r\nContent-Length: 0\r\n\r\n"):
        self.reply, self.requests = reply, []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
            self.requests.append(data.decode("latin1"))
            conn.sendall(self.reply)
            conn.close()

    def headers(self, i=-1):
        lines = self.requests[i].split("\r\n")
        return lines[0], {k.strip().lower(): v.strip() for k, v in (l.split(":", 1) for l in lines[1:] if ":" in l)}


@pytest.fixture
def proxy():
    p = _Proxy()
    yield p
    p.sock.close()


@pytest.fixture
def search_stub():
    """A plain-HTTP stand-in for the Tavily API that records what it received."""
    seen = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            seen.append({"auth": self.headers.get("authorization"), "body": body})
            out = json.dumps({"results": [{"title": "Stub result", "url": "https://example.com/a",
                                           "content": "hello from the stub"}]}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv.seen = seen
    srv.url = f"http://127.0.0.1:{srv.server_address[1]}/search"
    yield srv
    srv.shutdown()
