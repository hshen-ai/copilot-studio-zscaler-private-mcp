#!/usr/bin/env python3
"""Check 5 — configuration audit of everything that makes the MCP server the ONLY Internet path.
Read-only.

  agent       Microsoft authentication; last publish Succeeded (prints publish diagnostics if not);
              generative orchestration ON; model knowledge OFF; web browsing OFF; the Hello World,
              MCP tool and two AI Guard components present
  environment Managed (protectionLevel Standard); VNet enterprise policy Linked
  billing     environment linked to a policy with MCSMessages pay-as-you-go ON
  data policy HTTP, HTTP with Entra ID and public-website knowledge Blocked for this environment;
              no Business-classified connector (would fail every publish)

    python3 verify/05-agent-config.py
"""
import json, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "lib"))
from pp import BAP, CFG, ENV_ID, GOV, PP, bap, bot, dv, ppapi

results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(("PASS  " if ok else "FAIL  ") + label, detail)


b = bot()
if not b:
    sys.exit("FAIL  agent not found")
_, _, r = dv("GET", f"bots({b['botid']})?$select=authenticationmode,configuration,synchronizationstatus,publishedon")
cfg = json.loads(r.get("configuration") or "{}")
sync = json.loads(r.get("synchronizationstatus") or "{}")
op = sync.get("lastFinishedPublishOperation") or {}
check("agent requires Microsoft sign-in (authenticationmode 2)", r.get("authenticationmode") == 2, r.get("authenticationmode"))
check("last publish Succeeded", op.get("status") == "Succeeded",
      f"{op.get('status')} at {op.get('operationEnd')}" + ("" if op.get("status") == "Succeeded" else f" diagnostics={json.dumps(op.get('diagnosticDetails'))[:400]}"))
check("generative orchestration ON", (cfg.get("settings") or {}).get("GenerativeActionsEnabled") is True)
check("model knowledge OFF", (cfg.get("aISettings") or {}).get("useModelKnowledge") is False)
_, _, comps = dv("GET", f"botcomponents?$select=schemaname,componenttype,data&$filter=_parentbotid_value eq {b['botid']}")
by = {c["schemaname"].split(".", 1)[1]: c for c in comps.get("value", [])}
gpt = by.get("gpt.default", {}).get("data") or ""
check("web browsing OFF (gpt.default)", "webBrowsing: false" in gpt)
for name in ("topic.HelloWorld", "topic.PrivateWebSearchMCP", "topic.AIGuardPrompt", "topic.AIGuardResponse"):
    check(f"component {name} present", name in by)

_, _, e = bap("GET", f"{BAP}/scopes/admin/environments/{ENV_ID}?api-version=2021-04-01")
p = e.get("properties", {})
check("managed environment", (p.get("governanceConfiguration") or {}).get("protectionLevel") == "Standard")
vn = (p.get("enterprisePolicies") or {}).get("vNets") or {}
check("VNet enterprise policy linked", vn.get("linkStatus") == "Linked", vn.get("policyId"))

V = "api-version=2022-03-01-preview"
_, _, pols = ppapi("GET", f"{PP}/licensing/billingPolicies?{V}")
mine = [x for x in (pols or {}).get("value", []) if ENV_ID.lower() in json.dumps(x.get("environmentIds", [])).lower()]
mcs = [x["name"] for x in mine for en in x.get("payGoEntitlements", []) if en["entitlementId"] == "MCSMessages" and en["payAsYouGoState"]]
check("pay-as-you-go Copilot Studio messages ON", bool(mcs), mcs or [x["name"] for x in mine])

_, _, dl = bap("GET", f"{GOV}/v1/policies")
scoped = [x for x in (dl or {}).get("value", []) if ENV_ID in json.dumps(x.get("environments", []))]
blocked = {c["name"] for x in scoped for g in x["connectorGroups"] if g["classification"] == "Blocked" for c in g["connectors"]}
business = [c["name"] for x in scoped for g in x["connectorGroups"] if g["classification"] == "Confidential" for c in g["connectors"]]
for c in ("Http", "shared_webcontents", "shared_webcontentsv2", "CSKnowledgePublicSites"):
    check(f"data policy blocks {c}", c in blocked)
check("no Business-classified connector (would fail every publish)", not business, business)
sys.exit(0 if all(results) else 1)
