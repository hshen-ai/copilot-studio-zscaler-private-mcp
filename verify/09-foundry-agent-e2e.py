#!/usr/bin/env python3
"""Check 9 — the Foundry agent end to end, and the network path its tool call took.

A new conversation (agents v2's thread) through the Foundry project endpoint, as you (Azure CLI sign-in):

  "hi"                        -> "Hello World"                          (the agent itself answers)
  a current-events question   -> an mcp_call to web_search, no error,   (the MCP tool was used)
                                 and an answer citing URLs
  the MCP server's log        -> the call arrived from foundry-agent;   (private path; Tavily only
                                 AI Guard ALLOW; web_search results       reachable through ZIA)

The MCP server logs X-Forwarded-For, e.g. "100.100.x.x,10.245.x.x,192.168.60.19": the leading hops are
inside Foundry's Data Proxy, the LAST one is the address the Container Apps ingress saw — it must be in
FOUNDRY_AGENT_SUBNET_PREFIX. Writes state/foundry-e2e-<timestamp>.json (the conversation, no token).

    .venv/bin/python verify/09-foundry-agent-e2e.py
"""
import datetime as dt
import ipaddress
import json
import pathlib
import re
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "lib"))
from foundry import CFG, STATE, foundry_state, project_client

FS = foundry_state()
NET = ipaddress.ip_network(CFG["FOUNDRY_AGENT_SUBNET_PREFIX"])
QUESTION = "Search the web: what did Zscaler announce this week? Give me the source URLs."
REF = {"agent_reference": {"name": CFG["FOUNDRY_AGENT_NAME"], "type": "agent_reference"}}
ok_all = True


def check(ok, label, detail=""):
    global ok_all
    ok_all &= bool(ok)
    print(("PASS  " if ok else "FAIL  ") + label + (f"\n      -> {detail}" if detail else ""))


def ask(oc, conv, text, tries=3):
    """Microsoft documents intermittent TaskCanceledException on tool calls (Data Proxy routing) — retry."""
    for i in range(1, tries + 1):
        try:
            return oc.responses.create(conversation=conv, input=text, extra_body=REF)
        except Exception as e:  # surfaced as a generic API error
            print(f"INFO  attempt {i}: {type(e).__name__}: {str(e)[:200]}")
            if i == tries:
                raise
            time.sleep(5)


def mcp_log(since):
    out = subprocess.run(["az", "containerapp", "logs", "show", "-g", CFG["AZ_RESOURCE_GROUP"], "-n", "websearch-mcp",
                          "--tail", "300", "--format", "text"], capture_output=True, text=True).stdout
    keep = []
    for line in out.splitlines():
        m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)", line)
        if m and dt.datetime.fromisoformat(m.group(1)).replace(tzinfo=dt.timezone.utc) >= since:
            keep.append(line)
    return keep


start = dt.datetime.now(dt.timezone.utc).replace(microsecond=0) - dt.timedelta(seconds=5)
with project_client(FS) as pc, pc.get_openai_client() as oc:
    conv = oc.conversations.create().id
    print("conversation:", conv)
    r1 = ask(oc, conv, "hi")
    check("hello world" in r1.output_text.lower(), "hi", r1.output_text)
    r2 = ask(oc, conv, QUESTION)
    calls = [o for o in r2.output if getattr(o, "type", None) == "mcp_call"]
    good = [c for c in calls if c.name == "web_search" and not getattr(c, "error", None)]
    check(good, "mcp_call web_search", "; ".join(f"{c.server_label}.{c.name} {c.arguments}" for c in calls) or
          f"no mcp_call (output types {[getattr(o, 'type', '?') for o in r2.output]})")
    check("http" in r2.output_text, "answer cites URLs", r2.output_text[:400].replace("\n", " "))

for _ in range(12):  # Container Apps log streaming lags by a few seconds
    lines = mcp_log(start)
    if any("web_search query=" in l for l in lines):
        break
    time.sleep(10)
for l in lines:  # the server's own log lines only (the revision name also contains "aiguard")
    m = re.search(r" (?:INFO|WARNING) ((?:accepted POST|rejected|aiguard|web_search).*)", l)
    if m:
        print("INFO  " + m.group(1))
peers = {m.group(1).split(",")[-1] for l in lines for m in [re.search(r"accepted POST /mcp from ([0-9.,]+)", l)] if m}
check(any(ipaddress.ip_address(p) in NET for p in peers), f"MCP calls from the Foundry agent subnet ({NET})", str(sorted(peers)))
check(not any("rejected" in l for l in lines), "MCP API key accepted (project connection)")
check(any("aiguard IN ALLOW" in l for l in lines), "AI Guard checked the tool input and results")
check(any(re.search(r"web_search query=.* results=[1-9]", l) for l in lines), "web_search returned results (Tavily through ZIA)")

STATE.mkdir(exist_ok=True)
(STATE / f"foundry-e2e-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json").write_text(json.dumps(
    {"conversation": conv, "turns": [{"user": "hi", "agent": r1.output_text}, {"user": QUESTION, "agent": r2.output_text}],
     "mcp_calls": [{"name": c.name, "arguments": c.arguments} for c in calls], "ingress_peers": sorted(peers)}, indent=1))
sys.exit(0 if ok_all else 1)
