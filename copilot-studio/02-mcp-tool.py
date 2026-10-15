#!/usr/bin/env python3
"""Step 6b — give the agent the private MCP server as its ONLY Internet path.

  1. custom connector "Private WebSearch MCP": host = the internal Container Apps FQDN, POST /mcp with
     x-ms-agentic-protocol: mcp-streamable-1.0, API key in header x-api-key
  2. API-key connection (key from state/mcp_api_key) and a Dataverse connection reference
  3. MCP tool component (TaskDialog / InvokeExternalAgentTaskAction / ModelContextProtocolMetadata)
  4. gpt.default with instructions and webBrowsing OFF
  5. agent configuration: generative orchestration ON (MCP requires it), model knowledge OFF
  6. publish

Run AFTER power-platform/02-managed-env-vnet.py: a connector created before the VNet link keeps the
public path until it is edited and re-saved. Idempotent.

    python3 copilot-studio/02-mcp-tool.py
"""
import json, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "lib"))
from pp import CFG, STATE, bot, dv, link_connection_reference, publish, save_state, template, upsert_component
from connector import ensure_connection, ensure_connection_reference, upsert_connector

SCHEMA = CFG["AGENT_SCHEMA"]
FQDN = (STATE / "mcp_fqdn").read_text().strip()
KEY = (STATE / "mcp_api_key").read_text().strip()
NAME = "Private WebSearch MCP"
b = bot() or sys.exit("agent not found — run 01-create-agent.py")
botid = b["botid"]

swagger = {
    "swagger": "2.0",
    "info": {"title": NAME, "description": "Private web search MCP server (Tavily) on internal Azure Container Apps", "version": "1.0.0"},
    "host": FQDN, "basePath": "/", "schemes": ["https"],
    "paths": {"/mcp": {"post": {"summary": "Private web search MCP server", "operationId": "InvokeServer",
                                "x-ms-agentic-protocol": "mcp-streamable-1.0", "parameters": [],
                                "responses": {"200": {"description": "Success"}}}}},
    "securityDefinitions": {"apiKeyHeader": {"type": "apiKey", "name": "x-api-key", "in": "header"}},
    "security": [{"apiKeyHeader": []}],
}
api, how = upsert_connector(NAME, swagger["info"]["description"], swagger, f"https://{FQDN}/")
print("connector:", how, api)
conn, how = ensure_connection(api, NAME, KEY)
print("connection:", how)
cr_id, cr_logical = ensure_connection_reference(api, conn, NAME)
print("connection reference:", cr_logical)

tool_id, how = upsert_component(botid, f"{SCHEMA}.topic.PrivateWebSearchMCP", "Private web search", 9,
                                template("mcp-tool.taskdialog.yaml", CONNECTION_REFERENCE=cr_logical),
                                "Private web search MCP server")
print("MCP tool:", how, "| link:", link_connection_reference(tool_id, cr_id))
_, how = upsert_component(botid, f"{SCHEMA}.gpt.default", CFG["AGENT_NAME"], 15, template("gpt-default.yaml"))
print("gpt.default:", how)

config = {
    "$kind": "BotConfiguration",
    "settings": {"GenerativeActionsEnabled": True},
    "gPTSettings": {"$kind": "GPTSettings", "defaultSchemaName": f"{SCHEMA}.gpt.default"},
    "aISettings": {"$kind": "AISettings", "useModelKnowledge": False, "isFileAnalysisEnabled": False,
                   "isSemanticSearchEnabled": False, "contentModeration": "Low", "optInUseLatestModels": False},
    "recognizer": {"$kind": "GenerativeAIRecognizer"},
}
s, _, r = dv("PATCH", f"bots({botid})", {"configuration": json.dumps(config, indent=2)}, headers={"If-Match": "*"})
print("configuration:", s, r or "")
status, diag = publish(botid)
print("publish:", status, json.dumps(diag)[:500] if diag else "")
save_state("mcp_tool.json", {"connector": api, "connection_reference": cr_logical})
