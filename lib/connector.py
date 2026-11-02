"""Custom connector + API-key connection + Dataverse connection reference, idempotent.

Power Apps connector API facts this relies on:
  - create:  POST /apis?$filter=environment eq '<env>'        (returns the connector id in `name`)
  - update:  PATCH /apis/<name> — PUT returns 405, and the body must NOT carry displayName
             (CannotUpdateApiDisplayName)
  - the current swagger is a SAS blob URL (apiDefinitions.originalSwaggerUrl) — fetch it WITHOUT the
    bearer header, or it answers 403
  - the API key goes into the connection's connectionParameters.api_key and is never echoed back
"""
import json
import urllib.request
import uuid

from pp import CFG, CTX, DV_API, ENV_ID, PA, bap, dv

ENV_REF = {"id": f"/providers/Microsoft.PowerApps/environments/{ENV_ID}", "name": ENV_ID}
API_KEY_PARAM = {"api_key": {"type": "securestring", "uiDefinition": {
    "displayName": "API Key", "description": "API key", "tooltip": "API key",
    "constraints": {"tabIndex": 2, "clearText": False, "required": "true"}}}}


def find_connector(display_name):
    s, _, b = bap("GET", f"{PA}/apis?api-version=2016-11-01&$filter=environment eq '{ENV_ID}' and IsCustomApi eq 'True'")
    return next((a for a in (b or {}).get("value", []) if a["properties"].get("displayName") == display_name), None)


def aad_oauth_param(client_id, client_secret, resource_uri, tenant_id):
    return {
        "token": {
            "type": "oauthSetting",
            "oAuthSettings": {
                "identityProvider": "aad",
                "clientId": client_id,
                "clientSecret": client_secret,
                "scopes": [],
                "redirectMode": "GlobalPerConnector",
                "properties": {
                    "IsFirstParty": "False",
                    "AzureActiveDirectoryResourceId": resource_uri,
                },
                "customParameters": {
                    "loginUri": {"value": "https://login.microsoftonline.com"},
                    "tenantId": {"value": tenant_id},
                    "resourceUri": {"value": resource_uri},
                    "enableOnbehalfOfLogin": {"value": "true"},
                },
            },
            "uiDefinition": {
                "displayName": "Azure Active Directory",
                "description": "Sign in using Azure Active Directory",
                "tooltip": "Provide your Azure AD credentials",
                "constraints": {"required": "true"},
            },
        }
    }


def get_connector(api_name):
    s, _, b = bap("GET", f"{PA}/apis/{api_name}?api-version=2016-11-01&$filter=environment eq '{ENV_ID}'")
    if s >= 300:
        raise SystemExit(f"get connector '{api_name}' failed: {s} {b}")
    return b


def upsert_connector(display_name, description, swagger, service_url, connection_parameters=None):
    api = find_connector(display_name)
    conn_params = connection_parameters if connection_parameters is not None else API_KEY_PARAM
    props = {"description": description, "iconBrandColor": "#2160e0", "capabilities": [], "isCustomApi": True,
             "openApiDefinition": swagger, "backendService": {"serviceUrl": service_url},
             "connectionParameters": conn_params, "environment": ENV_REF}
    if api is None:
        s, _, b = bap("POST", f"{PA}/apis?api-version=2016-11-01&$filter=environment eq '{ENV_ID}'",
                      {"properties": {"displayName": display_name, **props}})
        if s >= 300:
            raise SystemExit(f"create connector '{display_name}' failed: {s} {b}")
        return b["name"], "created"
    s, _, b = bap("PATCH", f"{PA}/apis/{api['name']}?api-version=2016-11-01&$filter=environment eq '{ENV_ID}'",
                  {"properties": props})
    if s >= 300:
        raise SystemExit(f"update connector '{display_name}' failed: {s} {b}")
    return api["name"], "updated"


def current_swagger(api_name):
    s, _, b = bap("GET", f"{PA}/apis/{api_name}?api-version=2016-11-01&$filter=environment eq '{ENV_ID}'")
    url = b["properties"]["apiDefinitions"]["originalSwaggerUrl"]
    return json.loads(urllib.request.urlopen(url, context=CTX, timeout=60).read())  # no bearer on SAS URLs


def ensure_connection(api_name, display_name, api_key_value):
    s, _, b = bap("GET", f"{PA}/apis/{api_name}/connections?api-version=2016-11-01&$filter=environment eq '{ENV_ID}'")
    rows = (b or {}).get("value", []) if isinstance(b, dict) else []
    if rows:
        return rows[0]["name"], "exists"
    s, _, b = bap("PUT", f"{PA}/apis/{api_name}/connections/{uuid.uuid4().hex}?api-version=2016-11-01&$filter=environment eq '{ENV_ID}'",
                  {"properties": {"displayName": display_name, "environment": ENV_REF,
                                  "connectionParameters": {"api_key": api_key_value}}})
    if s >= 300:
        raise SystemExit(f"create connection failed: {s} {b}")
    status = (b["properties"].get("statuses") or [{}])[0].get("status")
    return b["name"], f"created ({status})"


def ensure_connection_reference(api_name, connection_id, display_name):
    logical = f"{CFG['AGENT_SCHEMA']}.{api_name}.{uuid.uuid5(uuid.NAMESPACE_URL, api_name).hex}"
    s, _, b = dv("GET", f"connectionreferences?$select=connectionreferenceid&$filter=connectionreferencelogicalname eq '{logical}'")
    if b.get("value"):
        return b["value"][0]["connectionreferenceid"], logical
    s, _, b = dv("POST", "connectionreferences", {
        "connectionreferencelogicalname": logical, "connectionreferencedisplayname": display_name,
        "connectorid": f"/providers/Microsoft.PowerApps/apis/{api_name}", "connectionid": connection_id},
        headers={"Prefer": "return=representation"})
    if s >= 300:
        raise SystemExit(f"create connection reference failed: {s} {b}")
    return b["connectionreferenceid"], logical
