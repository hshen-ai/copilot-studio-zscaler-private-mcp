#!/usr/bin/env python3
"""Step 6c (optional) — switch the MCP tool to the Entra ID connector with END-USER credentials, so the
signed-in user's token reaches the MCP server (needed for ZIA_EGRESS_MODE=proxy-caller).

  1. run 02b-mcp-tool-entra.py (connector "Private Web Search MCP (Entra)", on-behalf-of login) with the
     connector client app <CONNECTOR_CLIENT_ID>
  2. add the connector's per-connector redirect URL to that app's web redirect URIs
  3. a connection reference WITHOUT a connection (each user gets their own connection at run time)
  4. the MCP tool: that reference, connectionProperties.mode Invoker; publish

The previous tool definition is saved to state/mcp_tool_maker_backup.json; restore it by re-running
02-mcp-tool.py. Run from the repo root:

    CONNECTOR_CLIENT_ID=<appId> CONNECTOR_CLIENT_SECRET_FILE=state/connector-client-secret \
        python3 <path>/02c-mcp-tool-end-user.py
"""
import json, os, pathlib, re, subprocess, sys
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
from pp import CFG, bot, dv, link_connection_reference, publish, save_state, template, upsert_component
from connector import ensure_connection_reference

SCHEMA = CFG["AGENT_SCHEMA"]
CLIENT = os.environ.get("CONNECTOR_CLIENT_ID") or sys.exit("set CONNECTOR_CLIENT_ID")
env = dict(os.environ, **({"REQUESTS_CA_BUNDLE": CFG["CA_BUNDLE"]} if CFG.get("CA_BUNDLE") else {}))

out = subprocess.run([sys.executable, str(ROOT / "copilot-studio" / "02b-mcp-tool-entra.py")], env=env,
                     capture_output=True, text=True, check=True).stdout
api = re.search(r"connector id/name: (\S+)", out).group(1)
redirect = re.search(r"redirectUrl: (\S+)", out).group(1)
print("Entra connector:", api, "| redirect:", redirect)

app = json.loads(subprocess.check_output(["az", "ad", "app", "show", "--id", CLIENT, "--query", "{id:id,uris:web.redirectUris}",
                                          "-o", "json"], env=env, text=True))
if redirect not in app["uris"]:
    body = json.dumps({"web": {"redirectUris": app["uris"] + [redirect]}})
    subprocess.run(["az", "rest", "--method", "PATCH", "--url", f"https://graph.microsoft.com/v1.0/applications/{app['id']}",
                    "--headers", "Content-Type=application/json", "--body", body], env=env, check=True)
    print("redirect URI added to", CLIENT)

cr_id, cr_logical = ensure_connection_reference(api, None, "Private Web Search MCP (Entra)")
print("connection reference:", cr_logical)

b = bot() or sys.exit("agent not found")
_, _, r = dv("GET", f"botcomponents?$select=botcomponentid,data&$filter=schemaname eq '{SCHEMA}.topic.PrivateWebSearchMCP'")
save_state("mcp_tool_maker_backup.json", r["value"][0] if r.get("value") else {})
data = template("mcp-tool.taskdialog.yaml", CONNECTION_REFERENCE=cr_logical).replace("mode: Maker", "mode: Invoker")
tool_id, how = upsert_component(b["botid"], f"{SCHEMA}.topic.PrivateWebSearchMCP", "Private web search", 9, data,
                                "Private web search MCP server")
print("MCP tool:", how, "(Invoker) | link:", link_connection_reference(tool_id, cr_id))
status, diag = publish(b["botid"])
print("publish:", status, json.dumps(diag)[:500] if diag else "")
save_state("mcp_tool.json", {"connector": api, "connection_reference": cr_logical, "mode": "Invoker"})
