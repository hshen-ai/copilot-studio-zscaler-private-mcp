"""Universal MCP Jumphost: Streamable-HTTP proxy with per-user ZIA egress.

Accepts inbound MCP Streamable-HTTP requests with Authorization: Bearer <Entra user access token>,
validates them against Entra ID / Okta JWKS, performs On-Behalf-Of (OBO) exchange to a ZIA egress
token, and relays requests unchanged to upstream MCP servers through the ZIA explicit proxy with
Proxy-Authorization: Bearer <zia token> on the CONNECT tunnel.
"""
import json
import logging
import os
import time

import httpx
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route
import uvicorn

import auth
import egress

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mcp-jumphost")

FORWARD_REQUEST_HEADERS = {
    "content-type", "accept", "mcp-session-id", "mcp-protocol-version", "last-event-id", "user-agent",
}
FORWARD_RESPONSE_HEADERS = {
    "content-type", "mcp-session-id", "cache-control", "www-authenticate",
}


def parse_routes(raw: str | None) -> dict[str, str]:
    if not raw or not raw.strip():
        raise SystemExit("ROUTES environment variable must be set and non-empty JSON")
    try:
        data = json.loads(raw)
    except Exception as e:
        raise SystemExit(f"ROUTES is not valid JSON: {e}")
    if not isinstance(data, dict) or not data:
        raise SystemExit("ROUTES must be a non-empty JSON object mapping names to https URLs")
    for name, url in data.items():
        if not isinstance(name, str) or not name.strip():
            raise SystemExit(f"Invalid route name: {name!r}")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise SystemExit(f"Route {name!r} upstream URL must start with https://, got {url!r}")
    return data


def jsonrpc_error(code: int, message: str, req_id: object = None) -> dict:
    return {"jsonrpc": "2.0", "error": {"code": code, "message": message}, "id": req_id}


# Runtime module globals (accessible for monkeypatching in tests)
ROUTES: dict[str, str] = {}
IDP: auth.IdpConfig | None = None
verifier: auth.JwtVerifier | None = None
EGRESS: egress.Egress | None = None
IDP_HTTP: httpx.AsyncClient | None = None


def setup_runtime(env: dict | None = None):
    if env is None:
        env = os.environ
    routes = parse_routes(env.get("ROUTES"))

    zia_proxy = env.get("ZIA_PROXY")
    if not zia_proxy:
        hp = env.get("HTTPS_PROXY", "")
        if hp.startswith("http://"):
            zia_proxy = hp[7:]
        elif hp.startswith("https://"):
            zia_proxy = hp[8:]
        else:
            zia_proxy = hp
    zia_proxy = (zia_proxy or "").rstrip("/")

    ca_file = env.get("ZSCALER_CA_FILE") or env.get("SSL_CERT_FILE")
    ctx = egress.ssl_context(ca_file)
    idp_proxy = env.get("IDP_PROXY") or (f"http://{zia_proxy}" if zia_proxy else None)
    idp_http = httpx.AsyncClient(proxy=idp_proxy, verify=ctx, trust_env=False, timeout=30)

    async def fetch_jwks(url: str) -> dict:
        r = await idp_http.get(url)
        r.raise_for_status()
        return r.json()

    idp = auth.idp_from_env(env)
    v = auth.JwtVerifier(idp, fetch_jwks)

    mode = env.get("ZIA_EGRESS_MODE") or "proxy"
    cfg = egress.egress_from_env({**env, "ZIA_EGRESS_MODE": mode, "ZIA_PROXY": zia_proxy}, idp)
    eg = egress.Egress(cfg, ctx, idp_http)

    return routes, idp, v, eg, idp_http


async def healthz(request: Request) -> Response:
    return PlainTextResponse("ok")


async def protected_resource_metadata(request: Request) -> Response:
    route_name = request.path_params.get("route_name", "")
    if route_name not in ROUTES:
        return JSONResponse({"error": "not found"}, status_code=404)
    base = (os.environ.get("MCP_PUBLIC_BASE") or str(request.base_url)).rstrip("/")
    return JSONResponse({
        "resource": f"{base}/{route_name}/mcp",
        "authorization_servers": [IDP.issuer] if IDP else [],
        "scopes_supported": list(IDP.required_scopes) if IDP else [],
    })


async def relay_mcp(request: Request) -> Response:
    start_time = time.perf_counter()
    route_name = request.path_params.get("route_name", "")
    caller = None
    body = await request.body()
    req_id = None
    if body:
        try:
            j = json.loads(body)
            if isinstance(j, dict):
                req_id = j.get("id")
        except Exception:
            pass

    def fail(status: int, msg: str, headers: dict | None = None, is_rpc: bool = True):
        duration_ms = (time.perf_counter() - start_time) * 1000
        who = caller.identity if caller else "-"
        up_st = str(status) if status in (401, 403, 404) else "-"
        log.info("route=%s method=%s caller=%s upstream_status=%s duration_ms=%.1f",
                 route_name, request.method, who, up_st, duration_ms)
        content = jsonrpc_error(-32000, msg, req_id) if is_rpc else {"error": msg}
        return JSONResponse(content, status_code=status, headers=headers or {})

    if route_name not in ROUTES:
        return fail(404, "not found", is_rpc=False)

    base = (os.environ.get("MCP_PUBLIC_BASE") or str(request.base_url)).rstrip("/")
    meta_url = f"{base}/.well-known/oauth-protected-resource/{route_name}/mcp"

    auth_h = request.headers.get("authorization", "")
    if not auth_h.lower().startswith("bearer "):
        return fail(401, "unauthorized", {"WWW-Authenticate": f'Bearer resource_metadata="{meta_url}"'})

    caller = await verifier.verify_token(auth_h[7:].strip()) if verifier else None
    if caller is None:
        return fail(401, "unauthorized", {"WWW-Authenticate": f'Bearer resource_metadata="{meta_url}"'})

    req_scopes = set(IDP.required_scopes) if IDP else set()
    if req_scopes and not req_scopes.issubset(set(caller.scopes)):
        return fail(403, "insufficient_scope", {"WWW-Authenticate": 'Bearer error="insufficient_scope"'})

    try:
        client = await EGRESS.client(caller)
    except egress.EgressDenied as e:
        return fail(502, f"egress denied: {e}")

    upstream_base = ROUTES[route_name]
    query = request.url.query
    upstream_url = f"{upstream_base}?{query}" if query else upstream_base
    upstream_headers = {k: v for k, v in request.headers.items() if k.lower() in FORWARD_REQUEST_HEADERS}
    content = body if (body or request.method == "POST") else None
    upstream_req = client.build_request(request.method, upstream_url, headers=upstream_headers, content=content)

    try:
        upstream_resp = await client.send(upstream_req, stream=True)
    except httpx.ProxyError:
        await client.aclose()
        return fail(502, "ZIA proxy refused")
    except httpx.RequestError as e:
        await client.aclose()
        return fail(502, f"upstream connect error: {e}")

    resp_headers = {k: v for k, v in upstream_resp.headers.items() if k.lower() in FORWARD_RESPONSE_HEADERS}

    async def cleanup():
        duration_ms = (time.perf_counter() - start_time) * 1000
        log.info("route=%s method=%s caller=%s upstream_status=%d duration_ms=%.1f",
                 route_name, request.method, caller.identity, upstream_resp.status_code, duration_ms)
        await upstream_resp.aclose()
        await client.aclose()

    return StreamingResponse(
        upstream_resp.aiter_bytes(),
        status_code=upstream_resp.status_code,
        headers=resp_headers,
        background=BackgroundTask(cleanup),
    )


routes = [
    Route("/healthz", endpoint=healthz, methods=["GET"]),
    Route("/.well-known/oauth-protected-resource/{route_name}/mcp", endpoint=protected_resource_metadata, methods=["GET"]),
    Route("/.well-known/oauth-protected-resource/{route_name}/mcp/", endpoint=protected_resource_metadata, methods=["GET"]),
    Route("/{route_name}/mcp", endpoint=relay_mcp, methods=["GET", "POST", "DELETE"]),
    Route("/{route_name}/mcp/", endpoint=relay_mcp, methods=["GET", "POST", "DELETE"]),
]


def create_app(env: dict | None = None) -> Starlette:
    global ROUTES, IDP, verifier, EGRESS, IDP_HTTP
    r, idp, v, eg, idp_h = setup_runtime(env)
    ROUTES = r
    IDP = idp
    verifier = v
    EGRESS = eg
    IDP_HTTP = idp_h
    return Starlette(routes=routes)


app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT") or 8080)
    uvicorn.run(app, host="0.0.0.0", port=port)
