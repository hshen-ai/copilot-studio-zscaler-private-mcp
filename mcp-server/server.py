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

# =====================================================================================================
#  PER-USER IDENTITY: ENTRA BEARER TOKEN -> ON-BEHALF-OF (OBO) -> ZIA  (MCP_AUTH=entra,
#  ZIA_EGRESS_MODE=proxy-caller). Every line that takes part is tagged [OBO-1] .. [OBO-5] below.
#
#  [OBO-1] Copilot Studio calls /mcp with "Authorization: Bearer <user token>". The custom connector
#          (copilot-studio/02b, 02c: end-user credentials) obtains it for the SIGNED-IN USER, audience
#          api://<MCP_CLIENT_ID>, scope mcp.access. ApiKeyAndAccessLog validates it with auth.JwtVerifier
#          (signature against Entra's JWKS, iss, aud, exp) and checks MCP_REQUIRED_SCOPES.
#  [OBO-2] The validated caller (auth.Caller: the raw token + its claims) is stored in the CALLER context
#          variable for the duration of that one HTTP request, so the tool knows who is calling.
#  [OBO-3] web_search -> EGRESS.client(caller) -> egress.Egress.exchange(): an Entra On-Behalf-Of request
#          (grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer, requested_token_use=on_behalf_of,
#          assertion=<user token>, client MCP_CLIENT_ID + MCP_CLIENT_SECRET, scope ZIA_EGRESS_SCOPE) to
#          login.microsoftonline.com. The result is a NEW token, issued to the same user, for the ZIA
#          egress app — the user's own token is never forwarded. Cached until shortly before it expires.
#  [OBO-4] That ZIA token rides the explicit-proxy CONNECT as "Proxy-Authorization: Bearer <zia token>".
#          ZIA validates it (ZIdentity Token Validator, Subject Claim = email) and attributes the traffic
#          to the ZIA user of that name. No token -> ZIA answers 407; this server never falls back to
#          another identity (EgressDenied -> the search is refused).
#  [OBO-5] The same identity (email claim) goes into every AI Guard scan as "user", so AI Guard, ZIA and
#          the Copilot Studio AI Guard topics all log the same name.
#
#  Bootstrap: the JWKS download ([OBO-1]) and the OBO request itself ([OBO-3]) go through ZIA WITHOUT a
#  ZIA token — login.microsoftonline.com must be in ZIA's JWT Authentication Exemptions, otherwise the
#  exchange fails with "token exchange unreachable: ProxyError".
#  x-api-key callers have no user token: they may initialize and list tools, but web_search is refused.
# =====================================================================================================

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
# [OBO-1][OBO-3] Client for Entra itself (JWKS + the OBO exchange). It goes through ZIA with NO
# Proxy-Authorization — it is how the ZIA token is obtained in the first place (see Bootstrap above).
idp_http = httpx.AsyncClient(proxy=idp_proxy, verify=ctx, trust_env=False, timeout=30)


async def fetch_jwks(url: str) -> dict:  # [OBO-1] Entra's public signing keys
    r = await idp_http.get(url)
    r.raise_for_status()
    return r.json()


MCP_AUTH = (os.environ.get("MCP_AUTH") or "").strip().lower()
if MCP_AUTH == "entra":  # [OBO-1] accept Entra Bearer tokens (issuer, audience, scopes from env)
    idp = auth.idp_from_env(os.environ)
    verifier = auth.JwtVerifier(idp, fetch_jwks)
else:
    idp = None
    verifier = None

mode = os.environ.get("ZIA_EGRESS_MODE") or "proxy"
cfg = egress.egress_from_env({**os.environ, "ZIA_EGRESS_MODE": mode, "ZIA_PROXY": ZIA_PROXY}, idp)
EGRESS = egress.Egress(cfg, ctx, idp_http)  # [OBO-3][OBO-4] proxy-caller: OBO exchange + token on CONNECT

CALLER = contextvars.ContextVar("caller", default=None)  # [OBO-2] the validated caller of THIS request

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
        if user:  # [OBO-5] the caller's identity, so AI Guard logs the real user
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
    caller = CALLER.get()  # [OBO-2] set by ApiKeyAndAccessLog; None for x-api-key callers
    user = caller.identity if caller else None  # [OBO-5] email claim first: the same name ZIA and the topics use
    try:
        # [OBO-3][OBO-4] proxy-caller: exchange the caller's token On-Behalf-Of for a ZIA token and return an
        # httpx client whose every CONNECT carries "Proxy-Authorization: Bearer <zia token>". Tavily AND the
        # AI Guard calls below use this client, so all of this tool's egress is attributed to the user.
        client = await EGRESS.client(caller)
    except egress.EgressDenied as e:  # [OBO-4] no user token / exchange failed: refuse, never fall back
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

        # [OBO-1] Entra Bearer token of the signed-in user (Copilot Studio end-user connector)
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

        CALLER.set(caller)  # [OBO-2] hand the validated caller to the tool
        who = caller.identity if caller else "apikey"
        log.info("accepted %s %s from %s as %s", request.method, request.url.path, peer, who)
        return await call_next(request)


app = mcp.streamable_http_app()
app.add_middleware(ApiKeyAndAccessLog)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080, proxy_headers=True, forwarded_allow_ips="*")
