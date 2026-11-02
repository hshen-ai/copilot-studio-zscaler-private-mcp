#!/usr/bin/env python3
"""Create the Entra ID (OAuth 2.0) custom connector for the private MCP server.

Creates (idempotently, by displayName) the custom connector "Private Web Search MCP (Entra)"
pointing at the private Container Apps FQDN (from state/mcp_fqdn), configured with:
  - Swagger 2.0 with x-ms-agentic-protocol: mcp-streamable-1.0 on POST /mcp
  - securityDefinitions AAD (oauth2 accessCode)
  - connectionParameters with oauthSetting (identityProvider: aad, redirectMode: GlobalPerConnector,
    resourceUri: api://<MCP_CLIENT_ID>, enableOnbehalfOfLogin: true)

Does NOT create connections, connection references, topics, or modify the agent.

    CONNECTOR_CLIENT_ID=<appId> CONNECTOR_CLIENT_SECRET_FILE=state/connector-client-secret \
        python3 copilot-studio/02b-mcp-tool-entra.py
"""
import json, os, pathlib, sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "lib"))
from connector import aad_oauth_param, get_connector, upsert_connector
from pp import CFG, STATE, secret

NAME = "Private Web Search MCP (Entra)"
FQDN = (STATE / "mcp_fqdn").read_text().strip()


# The service app (MCP_CLIENT_ID, config/demo.env) is the token audience; the connector itself signs in
# as a SEPARATE client app (two-app on-behalf-of topology) when CONNECTOR_CLIENT_ID is set.
SERVICE_CLIENT_ID = CFG["MCP_CLIENT_ID"]
CLIENT_ID = os.environ.get("CONNECTOR_CLIENT_ID") or SERVICE_CLIENT_ID

secret_file = os.environ.get("CONNECTOR_CLIENT_SECRET_FILE")
CLIENT_SECRET = pathlib.Path(secret_file).read_text().strip() if secret_file else secret("MCP_CLIENT_SECRET")

TENANT_ID = CFG["TENANT_ID"]
RESOURCE_URI = f"api://{SERVICE_CLIENT_ID}"

swagger = {
    "swagger": "2.0",
    "info": {
        "title": NAME,
        "description": "Private web search MCP server (Tavily) on internal Azure Container Apps (Entra ID delegated)",
        "version": "1.0.0",
    },
    "host": FQDN,
    "basePath": "/",
    "schemes": ["https"],
    "paths": {
        "/mcp": {
            "post": {
                "summary": "Private web search MCP server",
                "operationId": "InvokeServer",
                "x-ms-agentic-protocol": "mcp-streamable-1.0",
                "parameters": [],
                "responses": {"200": {"description": "Success"}},
            }
        }
    },
    "securityDefinitions": {
        "AAD": {
            "type": "oauth2",
            "flow": "accessCode",
            "authorizationUrl": "https://login.microsoftonline.com/common/oauth2/authorize",
            "tokenUrl": "https://login.microsoftonline.com/common/oauth2/token",
            "scopes": {},
        }
    },
    "security": [{"AAD": []}],
}

conn_params = aad_oauth_param(
    client_id=CLIENT_ID,
    client_secret=CLIENT_SECRET,
    resource_uri=RESOURCE_URI,
    tenant_id=TENANT_ID,
)

api, how = upsert_connector(
    NAME,
    swagger["info"]["description"],
    swagger,
    f"https://{FQDN}/",
    connection_parameters=conn_params,
)

details = get_connector(api)
oauth_settings = (
    details.get("properties", {})
    .get("connectionParameters", {})
    .get("token", {})
    .get("oAuthSettings", {})
)
redirect_url = oauth_settings.get("redirectUrl")

# Sanitize any secret fields just in case
safe_oauth = {k: v for k, v in oauth_settings.items() if "secret" not in k.lower()}

print("connector id/name:", api)
print("displayName:", NAME)
print("operation:", how)
print("redirectUrl:", redirect_url)
print("oAuthSettings:", json.dumps(safe_oauth, indent=2))
