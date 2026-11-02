"""Private web-search MCP server for a Microsoft Copilot Studio agent.

Streamable HTTP at /mcp with one tool, `web_search`, backed by the Tavily search API. Inbound callers
authenticate via the x-api-key header or optionally OAuth 2.0 Bearer (MCP_AUTH=entra, validated against
Entra ID JWKS with required scopes). Runs in an internal Azure Container Apps environment, so it has no
public endpoint; Copilot Studio reaches it through Power Platform virtual-network support.

Egress: outbound calls take the path specified by ZIA_EGRESS_MODE (direct, proxy, proxy-token-file,
or proxy-caller). In proxy-caller mode, the caller's Entra token is exchanged On-Behalf-Of for a ZIA
egress token attached as Proxy-Authorization: Bearer on the CONNECT request. Outbound TLS honours
ZSCALER_CA_FILE or SSL_CERT_FILE.

Zscaler AI Guard: the tool's INPUT (the search query the model chose) and OUTPUT (every result, which
becomes model input — the indirect prompt-injection path) are checked with AI Guard Detection-as-a-
Service. Fail-closed: if AI Guard cannot be reached, the query is refused and results are dropped.
"""
import asyncio
import contextvars
import logging
import os

import httpx
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

import auth
import egress

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("websearch-mcp")

API_KEY = os.environ["MCP_API_KEY"]
TAVILY_KEY = os.environ["TAVILY_API_KEY"]
AIGUARD_KEY = os.environ["AIGUARD_API_KEY"]
AIGUARD_POLICY = int(os.environ["AIGUARD_POLICY_ID"])
AIGUARD_URL = os.environ.get("AIGUARD_URL", "https://api.zseclipse.net/v1/detection/execute-policy")

# Proxy and TLS configuration
zia_proxy = os.environ.get("ZIA_PROXY")
if not zia_proxy:
    hp = os.environ.get("HTTPS_PROXY", "")
    if hp.startswith("http://"):
        zia_proxy = hp[7:]
    elif hp.startswith("https://"):
        zia_proxy = hp[8:]
    else:
        zia_proxy = hp
ZIA_PROXY = (zia_proxy or "").rstrip("/")

ctx = egress.ssl_context(os.environ.get("ZSCALER_CA_FILE") or os.environ.get("SSL_CERT_FILE"))
idp_proxy = f"http://{ZIA_PROXY}" if ZIA_PROXY else None
idp_http = httpx.AsyncClient(proxy=idp_proxy, verify=ctx, trust_env=False, timeout=30)


async def fetch_jwks(url: str) -> dict:
    r = await idp_http.get(url)
    r.raise_for_status()
    return r.json()


MCP_AUTH = (os.environ.get("MCP_AUTH") or "").strip().lower()
if MCP_AUTH == "entra":
    idp = auth.idp_from_env(os.environ)
    verifier = auth.JwtVerifier(idp, fetch_jwks)
else:
    idp = None
    verifier = None

mode = os.environ.get("ZIA_EGRESS_MODE") or "proxy"
cfg = egress.egress_from_env({**os.environ, "ZIA_EGRESS_MODE": mode, "ZIA_PROXY": ZIA_PROXY}, idp)
EGRESS = egress.Egress(cfg, ctx, idp_http)

CALLER = contextvars.ContextVar("caller", default=None)

mcp = FastMCP(
    "websearch",
    stateless_http=True,
    json_response=True,
    # The Host header is the Container Apps FQDN, not localhost; API-key auth below protects the endpoint.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


async def aiguard(client, direction, content, user=None):
    """Return (allowed, transactionId, triggered detectors). Any error counts as BLOCK (fail-closed)."""
    try:
        body = {"policyId": AIGUARD_POLICY, "direction": direction, "content": content[:20000]}
        if user:
            body["user"] = user
        r = await client.post(AIGUARD_URL, headers={"Authorization": f"Bearer {AIGUARD_KEY}"}, json=body)
        r.raise_for_status()
        j = r.json()
        hits = [k for k, v in (j.get("detectorResponses") or {}).items() if (v or {}).get("triggered")]
        # A policy without detectors for a direction returns action = null: that is not a block.
        allowed = j.get("action") != "BLOCK"
        log.info("aiguard %s %s tx=%s detectors=%s", direction, j.get("action"), j.get("transactionId"), hits)
        return allowed, j.get("transactionId"), hits
    except Exception as e:  # fail-closed
        log.warning("aiguard %s ERROR %s: %s -> treated as BLOCK", direction, type(e).__name__, e)
        return False, None, ["aiguard_unavailable"]


@mcp.tool()
async def web_search(query: str, max_results: int = 5) -> dict:
    """Search the public Internet and return the top results (title, url, snippet).
    Use this for any question about current events or anything not already known."""
    caller = CALLER.get()
    user = caller.identity if caller else None  # email claim first: the same name ZIA and the topics use
    try:
        client = await EGRESS.client(caller)
    except egress.EgressDenied as e:
        who = caller.identity if caller else "apikey"
        log.warning("egress denied for %s: %s", who, e)
        return {"query": query, "blocked": True, "reason": f"no permitted ZIA egress identity: {e}", "results": []}

    max_results = max(1, min(int(max_results), 10))
    async with client:
        ok, tx, hits = await aiguard(client, "IN", query, user=user)  # tool input
        if not ok:
            return {"query": query, "blocked": True, "reason": "Zscaler AI Guard blocked the search query",
                    "detectors": hits, "transactionId": tx, "results": []}
        r = await client.post(
            "https://api.tavily.com/search",
            headers={"Authorization": f"Bearer {TAVILY_KEY}"},
            json={"query": query, "max_results": max_results, "search_depth": "basic"},
        )
        r.raise_for_status()
        results = [
            {"title": x.get("title"), "url": x.get("url"), "snippet": (x.get("content") or "")[:500]}
            for x in r.json().get("results", [])
        ]
        # tool output: every result becomes model input, so scan each one as IN
        verdicts = await asyncio.gather(*(aiguard(client, "IN", f"{x['title']}\n{x['snippet']}", user=user) for x in results))
    kept = [x for x, (ok, _, _) in zip(results, verdicts) if ok]
    dropped = [{"url": x["url"], "detectors": v[2], "transactionId": v[1]} for x, v in zip(results, verdicts) if not v[0]]
    who = caller.identity if caller else "apikey"
    log.info("web_search query=%r results=%d kept=%d dropped=%d caller=%s egress=%s", query, len(results), len(kept), len(dropped), who, cfg.mode)
    return {"query": query, "results": kept, "removed_by_aiguard": dropped}


class ApiKeyAndAccessLog(BaseHTTPMiddleware):
    """Require x-api-key or valid Entra Bearer token, and log the caller — the log line is what
    verify/ uses to show that Copilot Studio's calls arrive from the Power Platform delegated subnet."""

    async def dispatch(self, request, call_next):
        peer = request.headers.get("x-forwarded-for") or (request.client.host if request.client else "-")
        if request.url.path == "/healthz":
            return JSONResponse({"ok": True})

        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer ") and verifier is not None:
            tok = auth_header[7:].strip()
            caller = await verifier.verify_token(tok)
            if caller is None:
                log.warning("rejected %s %s from %s (bad bearer)", request.method, request.url.path, peer)
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            req_scopes = set((os.environ.get("MCP_REQUIRED_SCOPES") or "").replace(",", " ").split())
            if req_scopes and not req_scopes.issubset(set(caller.scopes)):
                log.warning("rejected %s %s from %s (insufficient scope)", request.method, request.url.path, peer)
                return JSONResponse({"error": "insufficient_scope"}, status_code=403)
        elif request.headers.get("x-api-key") == API_KEY:
            caller = None
        else:
            log.warning("rejected %s %s from %s (bad/missing x-api-key)", request.method, request.url.path, peer)
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        CALLER.set(caller)
        who = caller.identity if caller else "apikey"
        log.info("accepted %s %s from %s as %s", request.method, request.url.path, peer, who)
        return await call_next(request)


app = mcp.streamable_http_app()
app.add_middleware(ApiKeyAndAccessLog)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080, proxy_headers=True, forwarded_allow_ips="*")
