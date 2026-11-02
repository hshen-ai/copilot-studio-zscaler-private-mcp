#!/usr/bin/env bash
# Check 2 — ZIA is the ONLY Internet path from the MCP subnet. A 200 alone proves nothing; the control
# row (direct connection fails) is what proves the 200 went through ZIA.
#
#   direct HTTPS to api.tavily.com, no proxy        fails (NSG deny-internet)       control
#   same through the ZIA explicit proxy              200 Connection Established (or 407 in proxy-caller mode)
#   same with bogus bearer                           407 Proxy-Authenticate: Bearer (in proxy-caller mode)
#   MCP tools/call web_search                        results (proxy mode) or blocked with "no caller token" (proxy-caller mode)
source "$(dirname "$0")/../azure/_env.sh"
source "$(dirname "$0")/_aci.sh"
PROBE=$(mktemp); trap 'rm -f "$PROBE"' EXIT
cat > "$PROBE" <<'PY'
import json, os, socket, httpx

def check(label, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + label, detail)

try:
    httpx.get("https://api.tavily.com/", timeout=15, trust_env=False)
    check("control: direct Internet access is blocked", False, "direct connection succeeded")
except Exception as e:
    check("control: direct Internet access is blocked", True, type(e).__name__)

mode = (os.environ.get("ZIA_EGRESS_MODE") or "proxy").strip().lower()
host, port = os.environ["ZIA_PROXY"].split(":")

def raw_connect(extra=""):
    s = socket.create_connection((host, int(port)), timeout=20)
    msg = f"CONNECT api.tavily.com:443 HTTP/1.1\r\nHost: api.tavily.com:443\r\n{extra}\r\n"
    s.sendall(msg.encode())
    res = s.recv(4096).decode("latin-1")
    s.close()
    lines = res.split("\r\n\r\n")[0].split("\r\n")
    status = lines[0] if lines else ""
    hdr = " | ".join(h for h in lines[1:] if h.lower().startswith("proxy-authenticate"))
    return status, hdr

st_no, hdr_no = raw_connect()
st_bogus, hdr_bogus = raw_connect("Proxy-Authorization: Bearer badtoken\r\n")

if mode == "proxy-caller":
    ok_no = "407" in st_no and "bearer" in hdr_no.lower()
    check("ZIA explicit proxy without token returns 407", ok_no, f"{st_no} | {hdr_no}")
    ok_bogus = "407" in st_bogus and "bearer" in hdr_bogus.lower()
    check("ZIA explicit proxy with bogus bearer returns 407", ok_bogus, f"{st_bogus} | {hdr_bogus}")
else:
    ok_no = "200" in st_no
    check("HTTPS through the ZIA explicit proxy", ok_no, st_no)
    check("ZIA CONNECT with bogus bearer", bool(st_bogus), f"{st_bogus} | {hdr_bogus}" if hdr_bogus else st_bogus)

r = httpx.post("https://" + os.environ["MCP_FQDN"] + "/mcp", timeout=90, trust_env=False,
               headers={"x-api-key": os.environ["MCP_API_KEY"], "Accept": "application/json, text/event-stream"},
               json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                     "params": {"name": "web_search", "arguments": {"query": "Zurich weather today", "max_results": 2}}})
try:
    out = json.loads(r.json()["result"]["content"][0]["text"])
    if mode == "proxy-caller":
        blocked = out.get("blocked") is True and "no caller token" in str(out.get("reason", ""))
        check("MCP tools/call blocked with 'no caller token' (fail-closed)", blocked, out.get("reason", ""))
    else:
        check("MCP web_search returns results", bool(out.get("results")), [x["url"] for x in out.get("results", [])])
except Exception as e:
    if mode == "proxy-caller":
        check("MCP tools/call blocked with 'no caller token' (fail-closed)", False, str(e))
    else:
        check("MCP web_search returns results", False, r.text[:300])
PY
run_in_vnet verify-egress "$PROBE"
echo "== MCP server log (AI Guard tool scans and Tavily call)"
az containerapp logs show -g "$RG" -n "$APP" --tail 30 --format text 2>/dev/null | grep -E 'aiguard|web_search|tavily|egress denied' | tail -8
