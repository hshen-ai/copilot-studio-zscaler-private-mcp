#!/usr/bin/env bash
# Check 1 — the MCP server is reachable ONLY from the private network, and only with its API key.
#
#   from this machine (Internet)          public DNS has no record; TCP 443 fails       -> PASS
#   from inside the VNet (subnet `test`)  /healthz 200; /mcp without key 401 (control);
#                                         initialize 200; tools/list contains web_search -> PASS
source "$(dirname "$0")/../azure/_env.sh"
source "$(dirname "$0")/_aci.sh"
FQDN=$(cat "$STATE/mcp_fqdn")
echo "== from the Internet: $FQDN"
python3 - "$FQDN" <<'PY'
import socket, sys
host = sys.argv[1]
try:
    ip = socket.gethostbyname(host)
    print(f"FAIL  public DNS resolves to {ip}")
except socket.gaierror:
    print("PASS  no public DNS record")
try:
    socket.create_connection((host, 443), timeout=8).close()
    print("FAIL  TCP 443 reachable from the Internet")
except OSError as e:
    print(f"PASS  TCP 443 unreachable ({type(e).__name__})")
PY

echo "== from inside the VNet"
PROBE=$(mktemp); trap 'rm -f "$PROBE"' EXIT
cat > "$PROBE" <<'PY'
import json, os, httpx
u = "https://" + os.environ["MCP_FQDN"]
H = {"Accept": "application/json, text/event-stream"}
K = {**H, "x-api-key": os.environ["MCP_API_KEY"]}
def check(label, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + label, detail)
r = httpx.get(u + "/healthz", timeout=30, trust_env=False); check("/healthz 200", r.status_code == 200, r.status_code)
r = httpx.post(u + "/mcp", headers=H, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, timeout=30, trust_env=False)
check("control: /mcp without x-api-key is refused (401)", r.status_code == 401, r.status_code)
r = httpx.post(u + "/mcp", headers=K, timeout=30, trust_env=False, json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "verify", "version": "1"}}})
check("initialize 200", r.status_code == 200, r.json().get("result", {}).get("serverInfo") if r.status_code == 200 else r.text[:200])
r = httpx.post(u + "/mcp", headers=K, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, timeout=30, trust_env=False)
names = [t["name"] for t in r.json().get("result", {}).get("tools", [])] if r.status_code == 200 else []
check("tools/list contains web_search", "web_search" in names, names)
PY
run_in_vnet verify-private-mcp "$PROBE"
