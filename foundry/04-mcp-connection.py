#!/usr/bin/env python3
"""Foundry step 4 — your data-plane role, and the project connection that holds the MCP API key. Idempotent.

Owner/Contributor are control-plane roles without the agents/* data actions, so the signed-in user also
gets "Foundry User" (formerly "Azure AI User") on the account. The connection stores the MCP server's
x-api-key (state/mcp_api_key, from azure/02-mcp-server.sh) server-side; the agent references it by name, so
the key never appears in the agent definition. The key is never printed.

    .venv/bin/python foundry/04-mcp-connection.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "lib"))
from foundry import API, STATE, arm_account_url, az, foundry_state, mcp_url, request

FS = foundry_state()
ARM = arm_account_url(FS)

# --- Foundry User for the signed-in user
me = az("ad", "signed-in-user", "show", "--query", "id")
scope = az("cognitiveservices", "account", "show", "-g", FS["FOUNDRY_RG"], "-n", FS["ACCOUNT"], "--query", "id")
roles = az("role", "definition", "list", "--query",
           "[?roleName=='Foundry User' || roleName=='Azure AI User'].{n:roleName,id:name}")
if not roles:
    sys.exit("neither the 'Foundry User' nor the 'Azure AI User' role definition exists")
have = az("role", "assignment", "list", "--assignee", me, "--scope", scope, "--query", "[].roleDefinitionName")
if roles[0]["n"] not in have:
    az("role", "assignment", "create", "--assignee-object-id", me, "--assignee-principal-type", "User",
       "--role", roles[0]["id"], "--scope", scope)
print("your roles on the account:", az("role", "assignment", "list", "--assignee", me, "--scope", scope,
                                       "--query", "[].roleDefinitionName"))

# --- project connection (PUT is idempotent; re-run after rotating the MCP API key)
key = (STATE / "mcp_api_key").read_text().strip()
url = f"{ARM}/projects/{FS['PROJECT']}/connections/websearch-mcp?api-version={API}"
s, _, b = request("PUT", url, {"properties": {
    "category": "RemoteTool",
    "target": mcp_url(),
    "authType": "CustomKeys",
    "credentials": {"keys": {"x-api-key": key}},
    "metadata": {"type": "custom_MCP"},
}}, resource="https://management.azure.com")
print(f"PUT connection websearch-mcp: {s} (key {len(key)} characters)")
if s >= 300:
    sys.exit(str(b)[:1500])
s, _, b = request("GET", url, resource="https://management.azure.com")
print("connection:", {k: b["properties"].get(k) for k in ("category", "target", "authType")})
