#!/usr/bin/env bash
# Check 2 — ZIA is the ONLY Internet path from the MCP subnet. A 200 alone proves nothing; the control
# row (direct connection fails) is what proves the 200 went through ZIA.
#
#   direct HTTPS to api.tavily.com, no proxy        fails (NSG deny-internet)       control
#   same through the ZIA explicit proxy              200 Connection Established
#   MCP tools/call web_search                        results (server egresses via ZIA, and its
#                                                    AI Guard tool scans return verdicts)
source "$(dirname "$0")/../azure/_env.sh"
source "$(dirname "$0")/_aci.sh"
PROBE=$(mktemp); trap 'rm -f "$PROBE"' EXIT
cat > "$PROBE" <<'PY'
import json, os, httpx
def check(label, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + label, detail)
try:
    httpx.get("https://api.tavily.com/", timeout=15, trust_env=False)
    check("control: direct Internet access is blocked", False, "direct connection succeeded")
except Exception as e:
    check("control: direct Internet access is blocked", True, type(e).__name__)
try:
    r = httpx.get("https://api.tavily.com/", timeout=20, proxy="http://" + os.environ["ZIA_PROXY"])
    check("HTTPS through the ZIA explicit proxy", True, f"HTTP {r.status_code} from api.tavily.com")
except Exception as e:
    check("HTTPS through the ZIA explicit proxy", False, f"{type(e).__name__}: {str(e)[:160]}  (403 = Static IP/Location not active in ZIA)")
r = httpx.post("https://" + os.environ["MCP_FQDN"] + "/mcp", timeout=90, trust_env=False,
               headers={"x-api-key": os.environ["MCP_API_KEY"], "Accept": "application/json, text/event-stream"},
               json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                     "params": {"name": "web_search", "arguments": {"query": "Zurich weather today", "max_results": 2}}})
try:
    out = json.loads(r.json()["result"]["content"][0]["text"])
    check("MCP web_search returns results", bool(out.get("results")), [x["url"] for x in out.get("results", [])])
except Exception as e:
    check("MCP web_search returns results", False, r.text[:300])
PY
run_in_vnet verify-egress "$PROBE"
echo "== MCP server log (AI Guard tool scans and Tavily call)"
az containerapp logs show -g "$RG" -n "$APP" --tail 30 --format text 2>/dev/null | grep -E 'aiguard|web_search|tavily' | tail -8
