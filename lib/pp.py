"""Shared helpers for the Power Platform / Dataverse / Copilot Studio scripts.

Configuration comes from config/demo.env (non-secret) and config/secrets.env (secrets, mode 600).
Access tokens come from the signed-in Azure CLI (`az login`) and stay in memory; nothing here prints
or writes a token or a secret.
"""
import json
import os
import pathlib
import ssl
import subprocess
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATE = ROOT / "state"


def load_env(name):
    """Parse config/<name> (KEY=VALUE lines, # comments) into a dict; environment variables win."""
    path = ROOT / "config" / name
    if not path.exists():
        raise SystemExit(f"missing {path} — copy {path.name}.example and fill it in (README, Setup)")
    out = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"')
    out.update({k: v for k, v in os.environ.items() if k in out})
    return out


CFG = load_env("demo.env")


def secret(key):
    return load_env("secrets.env")[key]


def ssl_context():
    ctx = ssl.create_default_context(cafile=CFG.get("CA_BUNDLE") or None)
    # Python 3.13+ rejects some inspection-proxy roots under VERIFY_X509_STRICT
    ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
    return ctx


CTX = ssl_context()
_tokens = {}


def token(resource):
    if resource not in _tokens:
        env = dict(os.environ)
        if CFG.get("CA_BUNDLE"):
            env["REQUESTS_CA_BUNDLE"] = CFG["CA_BUNDLE"]
        _tokens[resource] = subprocess.check_output(
            ["az", "account", "get-access-token", "--resource", resource, "--query", "accessToken", "-o", "tsv"],
            env=env, text=True).strip()
    return _tokens[resource]


def request(method, url, body=None, resource=None, headers=None, timeout=180, raw=False):
    """HTTP helper returning (status, headers, parsed JSON or text). raw=True returns the full text."""
    h = {"Accept": "application/json"}
    if resource:
        h["Authorization"] = "Bearer " + token(resource)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    req = urllib.request.Request(url.replace(" ", "%20"), data=data, headers=h, method=method)
    try:
        r = urllib.request.urlopen(req, context=CTX, timeout=timeout)
        status, hdrs, text = r.status, dict(r.headers), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        status, hdrs, text = e.code, dict(e.headers), e.read().decode("utf-8", "replace")
    if raw:
        return status, hdrs, text
    try:
        return status, hdrs, (json.loads(text) if text.strip() else None)
    except ValueError:
        return status, hdrs, text[:2000]


# --- Dataverse --------------------------------------------------------------------------------------
DV = CFG["DATAVERSE_URL"].rstrip("/")
DV_API = DV.replace(".crm", ".api.crm") + "/api/data/v9.2/"


def dv(method, path, body=None, headers=None):
    return request(method, path if path.startswith("http") else DV_API + path, body, resource=DV,
                   headers={"OData-Version": "4.0", "OData-MaxVersion": "4.0", **(headers or {})})


# --- Power Platform admin (BAP), Power Apps connectors, licensing ------------------------------------
BAP = "https://api.bap.microsoft.com/providers/Microsoft.BusinessAppPlatform"
GOV = "https://api.bap.microsoft.com/providers/PowerPlatform.Governance"
PA = "https://api.powerapps.com/providers/Microsoft.PowerApps"
PP = "https://api.powerplatform.com"
ENV_ID = CFG["PP_ENVIRONMENT_ID"]


def bap(method, url, body=None):
    return request(method, url, body, resource="https://service.powerapps.com/")


def ppapi(method, url, body=None):
    return request(method, url, body, resource="https://api.powerplatform.com")


# --- agent helpers ------------------------------------------------------------------------------------
def bot():
    s, _, b = dv("GET", f"bots?$select=botid,name,schemaname,authenticationmode,publishedon&$filter=schemaname eq '{CFG['AGENT_SCHEMA']}'")
    rows = (b or {}).get("value", []) if isinstance(b, dict) else []
    return rows[0] if rows else None


def publish(botid, want_auth=None, timeout_s=600):
    """Publish and wait for the publish that started after this call. Returns (status, diagnostics)."""
    t0 = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    dv("POST", f"bots({botid})/Microsoft.Dynamics.CRM.PvaPublish", {})
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(15)
        _, _, b = dv("GET", f"bots({botid})?$select=synchronizationstatus")
        sync = json.loads(b.get("synchronizationstatus") or "{}")
        op = sync.get("lastFinishedPublishOperation") or {}
        if op.get("operationEnd", "") > t0:
            auth = (sync.get("lastPublishedDetails") or {}).get("authenticationMode")
            if want_auth and op.get("status") == "Succeeded" and auth != want_auth:
                continue  # lastPublishedDetails lags one cycle
            return op.get("status"), op.get("diagnosticDetails")
    return "Timeout", None


def upsert_component(botid, schema, name, componenttype, data, description=""):
    s, _, b = dv("GET", f"botcomponents?$select=botcomponentid&$filter=schemaname eq '{schema}'")
    if b.get("value"):
        cid = b["value"][0]["botcomponentid"]
        dv("PATCH", f"botcomponents({cid})", {"data": data}, headers={"If-Match": "*"})
        return cid, "updated"
    s, _, b = dv("POST", "botcomponents", {"name": name, "schemaname": schema, "componenttype": componenttype,
                                           "data": data, "description": description,
                                           "parentbotid@odata.bind": f"/bots({botid})"},
                 headers={"Prefer": "return=representation"})
    if s >= 300:
        raise SystemExit(f"create component {schema} failed: {s} {b}")
    return b["botcomponentid"], "created"


def link_connection_reference(component_id, cr_id):
    return dv("POST", f"botcomponents({component_id})/botcomponent_connectionreference/$ref",
              {"@odata.id": f"{DV_API}connectionreferences({cr_id})"})[0]


def save_state(name, data):
    STATE.mkdir(exist_ok=True)
    (STATE / name).write_text(json.dumps(data, indent=1))
    os.chmod(STATE / name, 0o600)


def load_state(name):
    p = STATE / name
    return json.loads(p.read_text()) if p.exists() else {}


def template(name, **values):
    """Load a YAML template from copilot-studio/topics/ and fill {{KEY}} markers."""
    text = (ROOT / "copilot-studio" / "topics" / name).read_text()
    text = "\n".join(l for l in text.splitlines() if not l.startswith("# "))  # drop header comments
    for k, v in values.items():
        text = text.replace("{{" + k + "}}", str(v))
    if "{{" in text:
        raise SystemExit(f"unfilled marker in {name}")
    return text.strip()
