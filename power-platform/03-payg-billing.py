#!/usr/bin/env python3
"""Step 5c — Copilot Studio credits through a pay-as-you-go billing policy (Azure subscription).

Generative orchestration (needed for MCP tools) consumes Copilot Studio messages. Without capacity the
agent answers "This agent is currently unavailable. It has reached its usage limit." This script:
  1. creates (or reuses) a billing policy <BILLING_POLICY_NAME> on <AZ_SUBSCRIPTION_ID>/<AZ_RESOURCE_GROUP>
  2. links the environment to it
  3. turns pay-as-you-go ON for Copilot Studio messages ONLY (entitlement MCSMessages) — linking alone
     leaves every entitlement off

This produces real Azure charges. Skip it if the environment already has Copilot Studio capacity.

    python3 power-platform/03-payg-billing.py
"""
import json, sys, time, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "lib"))
from pp import CFG, ENV_ID, PP, ppapi

V = "api-version=2022-03-01-preview"
NAME = CFG["BILLING_POLICY_NAME"]
s, b = ppapi("GET", f"{PP}/licensing/billingPolicies?{V}")[::2]
pol = next((p for p in (b or {}).get("value", []) if p.get("name") == NAME), None) if isinstance(b, dict) else None
if pol is None:
    s, _, pol = ppapi("POST", f"{PP}/licensing/billingPolicies?{V}", {
        "name": NAME, "location": CFG["PP_GEO"], "status": "Enabled",
        "billingInstrument": {"subscriptionId": CFG["AZ_SUBSCRIPTION_ID"], "resourceGroup": CFG["AZ_RESOURCE_GROUP"]}})
    print("create billing policy:", s, pol if s >= 300 else pol.get("id"))
    if s >= 300:
        sys.exit(1)  # e.g. BillingDocumentNameInvalid: 10-64 chars, A-Z and 0-9 only
PID = pol["id"]

s, _, envs = ppapi("GET", f"{PP}/licensing/billingPolicies/{PID}/environments?{V}")
if ENV_ID.lower() not in json.dumps(envs).lower():
    s, _, b = ppapi("POST", f"{PP}/licensing/billingPolicies/{PID}/environments/add?{V}", {"environmentIds": [ENV_ID]})
    print("link environment:", s, b if s >= 300 else "")
    time.sleep(10)

s, _, pol = ppapi("GET", f"{PP}/licensing/billingPolicies/{PID}?{V}")
for e in pol["payGoEntitlements"]:
    if e["entitlementId"] == "MCSMessages":
        e["payAsYouGoState"] = True
s, _, b = ppapi("PUT", f"{PP}/licensing/billingPolicies/{PID}?{V}",
                {"name": pol["name"], "status": pol["status"], "payGoEntitlements": pol["payGoEntitlements"]})
print("enable MCSMessages:", s, "" if s < 300 else b)
s, _, pol = ppapi("GET", f"{PP}/licensing/billingPolicies/{PID}?{V}")
s, _, envs = ppapi("GET", f"{PP}/licensing/billingPolicies/{PID}/environments?{V}")
print("policy:", pol["name"], pol["status"], "| linked:", [e["environmentId"] for e in envs.get("value", [])],
      "| pay-as-you-go on:", [e["entitlementId"] for e in pol["payGoEntitlements"] if e["payAsYouGoState"]])
