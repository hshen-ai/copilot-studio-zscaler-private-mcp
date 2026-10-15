"""Private web-search MCP server for a Microsoft Copilot Studio agent.

Streamable HTTP at /mcp with one tool, `web_search`, backed by the Tavily search API. Every request
must carry the header x-api-key. Runs in an internal Azure Container Apps environment, so it has no
public endpoint; Copilot Studio reaches it through Power Platform virtual-network support.

Egress: outbound calls honour HTTPS_PROXY (the ZIA explicit proxy) and SSL_CERT_FILE (public roots plus
the Zscaler root CA, for SSL inspection).

Zscaler AI Guard: the tool's INPUT (the search query the model chose) and OUTPUT (every result, which
becomes model input — the indirect prompt-injection path) are checked with AI Guard Detection-as-a-
Service. Fail-closed: if AI Guard cannot be reached, the query is refused and results are dropped.
"""
import asyncio
import logging
import os

import httpx
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("websearch-mcp")

API_KEY = os.environ["MCP_API_KEY"]
TAVILY_KEY = os.environ["TAVILY_API_KEY"]
AIGUARD_KEY = os.environ["AIGUARD_API_KEY"]
AIGUARD_POLICY = int(os.environ["AIGUARD_POLICY_ID"])
AIGUARD_URL = os.environ.get("AIGUARD_URL", "https://api.zseclipse.net/v1/detection/execute-policy")

mcp = FastMCP(
    "websearch",
    stateless_http=True,
    json_response=True,
    # The Host header is the Container Apps FQDN, not localhost; API-key auth below protects the endpoint.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


async def aiguard(client, direction, content):
    """Return (allowed, transactionId, triggered detectors). Any error counts as BLOCK (fail-closed)."""
    try:
        r = await client.post(AIGUARD_URL, headers={"Authorization": f"Bearer {AIGUARD_KEY}"},
                              json={"policyId": AIGUARD_POLICY, "direction": direction, "content": content[:20000]})
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
    max_results = max(1, min(int(max_results), 10))
    async with httpx.AsyncClient(timeout=30) as client:  # trust_env: HTTPS_PROXY, SSL_CERT_FILE
        ok, tx, hits = await aiguard(client, "IN", query)  # tool input
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
        verdicts = await asyncio.gather(*(aiguard(client, "IN", f"{x['title']}\n{x['snippet']}") for x in results))
    kept = [x for x, (ok, _, _) in zip(results, verdicts) if ok]
    dropped = [{"url": x["url"], "detectors": v[2], "transactionId": v[1]} for x, v in zip(results, verdicts) if not v[0]]
    log.info("web_search query=%r results=%d kept=%d dropped=%d", query, len(results), len(kept), len(dropped))
    return {"query": query, "results": kept, "removed_by_aiguard": dropped}


class ApiKeyAndAccessLog(BaseHTTPMiddleware):
    """Require x-api-key and log the caller — the log line is what verify/ uses to show that Copilot
    Studio's calls arrive from the Power Platform delegated subnet."""

    async def dispatch(self, request, call_next):
        peer = request.headers.get("x-forwarded-for") or (request.client.host if request.client else "-")
        if request.url.path == "/healthz":
            return JSONResponse({"ok": True})
        if request.headers.get("x-api-key") != API_KEY:
            log.warning("rejected %s %s from %s (bad/missing x-api-key)", request.method, request.url.path, peer)
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        log.info("accepted %s %s from %s", request.method, request.url.path, peer)
        return await call_next(request)


app = mcp.streamable_http_app()
app.add_middleware(ApiKeyAndAccessLog)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080, proxy_headers=True, forwarded_allow_ips="*")
