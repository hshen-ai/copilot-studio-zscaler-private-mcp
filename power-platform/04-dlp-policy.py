#!/usr/bin/env python3
"""Step 8 — data policy for THIS environment only: the private MCP server is the only Internet path.

  Blocked  HTTP; HTTP with Microsoft Entra ID (both connectors); "Knowledge source with public websites
           and data in Copilot Studio"
  General  everything else, including the MCP and AI Guard custom connectors

Keep the custom connectors in the SAME group as the Copilot Studio channel connectors (General, the
default). Classifying the MCP connector as Business makes every publish fail with
DlpViolationError BusinessAndNonBusinessConnector — visible only in the publish diagnostics, while
the previously published version keeps serving.

    python3 power-platform/04-dlp-policy.py            # show what would be applied, and what it affects
    python3 power-platform/04-dlp-policy.py --apply
"""
import json, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "lib"))
from pp import BAP, ENV_ID, GOV, PA, bap, dv

NAME = "Copilot Studio - Internet only via private MCP"
s, _, virt = bap("GET", f"{GOV}/v1/connectors/metadata/virtual")
virt = virt if isinstance(virt, list) else (virt or {}).get("value", [])
s, _, apis = bap("GET", f"{PA}/apis?api-version=2016-11-01&$filter=environment eq '{ENV_ID}'&showApisWithToS=true")
cands = [{"id": v.get("id"), "name": v.get("name") or v.get("id", "").split("/")[-1],
          "display": (v.get("metadata") or {}).get("displayName") or v.get("displayName") or v.get("name")} for v in virt]
cands += [{"id": a["id"], "name": a["name"], "display": a["properties"].get("displayName")} for a in (apis or {}).get("value", [])]

blocked = []
for label, pred in [("HTTP", lambda d: d == "http"),
                    ("HTTP with Microsoft Entra ID", lambda d: "http with" in d and ("entra" in d or "azure ad" in d)),
                    ("Knowledge source with public websites", lambda d: "public website" in d)]:
    hit = [c for c in cands if c["display"] and pred(c["display"].lower())]
    if not hit:
        sys.exit(f"connector '{label}' not found in this tenant's connector list — nothing changed")
    blocked += [h for h in hit if h["id"] not in {b["id"] for b in blocked}]
print("Blocked:", [(b["display"], b["name"]) for b in blocked])

# impact: what already lives in the environment
s, _, apps = bap("GET", f"{PA}/scopes/admin/environments/{ENV_ID}/apps?api-version=2016-11-01")
_, _, bots = dv("GET", "bots?$select=name,schemaname")
print("apps in environment:", len((apps or {}).get("value", [])), "| agents:", [b["name"] for b in bots.get("value", [])])
print("Agents using the blocked connectors (e.g. built-in web search) lose that capability.")
if "--apply" not in sys.argv:
    sys.exit("dry run — re-run with --apply")

conn = lambda c: {"id": c["id"], "name": c["name"], "type": "Microsoft.PowerApps/apis"}
body = {"displayName": NAME, "defaultConnectorsClassification": "General", "environmentType": "OnlyEnvironments",
        "environments": [{"id": f"/providers/Microsoft.BusinessAppPlatform/scopes/admin/environments/{ENV_ID}",
                          "name": ENV_ID, "type": "Microsoft.BusinessAppPlatform/scopes/environments"}],
        "connectorGroups": [{"classification": "Confidential", "connectors": []},
                            {"classification": "General", "connectors": []},
                            {"classification": "Blocked", "connectors": [conn(c) for c in blocked]}]}
s, _, pols = bap("GET", f"{GOV}/v1/policies")
existing = next((p for p in (pols or {}).get("value", []) if p.get("displayName") == NAME), None)
# The v1 route with a flat body works; v2 rejects both a flat and a properties-wrapped body.
if existing:
    body.update({"name": existing["name"], "etag": existing.get("etag")})
    s, _, b = bap("PATCH", f"{GOV}/v1/policies/{existing['name']}", body)
else:
    s, _, b = bap("POST", f"{GOV}/v1/policies", body)
print("policy write:", s, b.get("name") if isinstance(b, dict) else b)
s, _, pols = bap("GET", f"{GOV}/v1/policies")
for p in (pols or {}).get("value", []):
    if p.get("displayName") == NAME:
        print("readback:", p["name"], p["environmentType"], [e["name"] for e in p["environments"]],
              {g["classification"]: [c["name"] for c in g["connectors"]] for g in p["connectorGroups"]})
