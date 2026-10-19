"""Shared helpers for the Microsoft Foundry scripts (foundry/, verify/08-09).

Needs the Foundry SDK in a virtual environment (foundry/requirements.txt). Configuration comes from
config/demo.env and state/foundry.env (written by foundry/03-foundry.sh); tokens come from the
signed-in Azure CLI. Nothing here prints or writes a token or a secret.
"""
import _ssl
import json
import os
import ssl
import subprocess

from pp import CFG, ROOT, STATE, request  # noqa: F401  (re-exported for the scripts)

if CFG.get("CA_BUNDLE"):
    # Behind an SSL-inspecting proxy: trust the bundle, and drop Python 3.13+'s VERIFY_X509_STRICT, which
    # rejects some inspection-proxy roots. Both bindings, before urllib3/httpx are imported: urllib3 copies
    # ssl.VERIFY_X509_STRICT at import time, ssl.create_default_context() reads _ssl.VERIFY_X509_STRICT.
    os.environ.setdefault("REQUESTS_CA_BUNDLE", CFG["CA_BUNDLE"])
    os.environ.setdefault("SSL_CERT_FILE", CFG["CA_BUNDLE"])
    ssl.VERIFY_X509_STRICT = 0
    _ssl.VERIFY_X509_STRICT = 0

API = "2025-04-01-preview"  # networkInjections and capability hosts are only visible at this version


def az(*args):
    """Run the Azure CLI and return its parsed JSON output (None when empty)."""
    out = subprocess.check_output(["az", *args, "-o", "json"], text=True)
    return json.loads(out) if out.strip() else None


def foundry_state():
    """KEY=VALUE pairs written by foundry/03-foundry.sh."""
    p = STATE / "foundry.env"
    if not p.exists():
        raise SystemExit("missing state/foundry.env — run foundry/03-foundry.sh first")
    return dict(l.split("=", 1) for l in p.read_text().split() if "=" in l)


def arm_account_url(fs):
    return (f"https://management.azure.com/subscriptions/{CFG['AZ_SUBSCRIPTION_ID']}/resourceGroups/"
            f"{fs['FOUNDRY_RG']}/providers/Microsoft.CognitiveServices/accounts/{fs['ACCOUNT']}")


def mcp_url():
    return "https://" + (STATE / "mcp_fqdn").read_text().strip() + "/mcp"


def project_client(fs):
    from azure.ai.projects import AIProjectClient
    from azure.identity import AzureCliCredential
    return AIProjectClient(endpoint=fs["PROJECT_ENDPOINT"], credential=AzureCliCredential())
