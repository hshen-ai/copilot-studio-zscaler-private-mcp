#!/usr/bin/env bash
# Check 10 — ZIA JWT Authentication Exemptions.
# From inside the VNet: unauthenticated CONNECT to login.microsoftonline.com must succeed (200),
# so the On-Behalf-Of token exchange can reach Entra ID. Unauthenticated CONNECT to api.tavily.com
# must be blocked (407 Proxy Authentication Required).
source "$(dirname "$0")/../azure/_env.sh"
source "$(dirname "$0")/_aci.sh"
PROBE=$(mktemp); trap 'rm -f "$PROBE"' EXIT
cat > "$PROBE" <<'PY'
import os, socket

def check(label, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + label, detail)

host, port = os.environ["ZIA_PROXY"].split(":")

def probe_connect(target):
    s = socket.create_connection((host, int(port)), timeout=20)
    s.sendall(f"CONNECT {target}:443 HTTP/1.1\r\nHost: {target}:443\r\n\r\n".encode())
    head = s.recv(4096).decode("latin-1").split("\r\n\r\n")[0].split("\r\n")
    s.close()
    status = head[0] if head else ""
    hdr = " | ".join(h for h in head[1:] if h.lower().startswith("proxy-authenticate"))
    return status, hdr

# login.microsoftonline.com must be exempt (200)
st_login, _ = probe_connect("login.microsoftonline.com")
check("exempt: unauthenticated CONNECT to login.microsoftonline.com allowed", "200" in st_login, st_login)

# api.tavily.com must require authentication (407)
st_tavily, hdr_tavily = probe_connect("api.tavily.com")
check("control: unauthenticated CONNECT to api.tavily.com challenged (407)", "407" in st_tavily and "bearer" in hdr_tavily.lower(), f"{st_tavily} | {hdr_tavily}" if hdr_tavily else st_tavily)
PY
run_in_vnet verify-ziaexempt "$PROBE"
