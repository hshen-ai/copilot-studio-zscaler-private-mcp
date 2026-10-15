#!/usr/bin/env python3
"""Step 5b — Power Platform virtual-network support for the environment.

  1. create the enterprise policy (Azure resource Microsoft.PowerPlatform/enterprisePolicies,
     kind NetworkInjection) with the pp-delegated subnet(s) of azure/01-network.sh
  2. make the environment a MANAGED environment (required before linking)
  3. link the policy to the environment and wait for linkStatus = Linked

After this, every connector call of the environment — the MCP tool and the AI Guard connector — leaves
from the delegated subnet. Connectors created BEFORE the link do not move until edited and re-saved,
so run this before copilot-studio/.

    python3 power-platform/02-managed-env-vnet.py
"""
import json, os, subprocess, sys, time, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "lib"))
from pp import BAP, CFG, ENV_ID, bap

RG, SUB = CFG["AZ_RESOURCE_GROUP"], CFG["AZ_SUBSCRIPTION_ID"]
env = dict(os.environ, **({"REQUESTS_CA_BUNDLE": CFG["CA_BUNDLE"]} if CFG.get("CA_BUNDLE") else {}))


def az(*args):
    return subprocess.check_output(["az", *args, "--subscription", SUB], env=env, text=True)


vnets = json.loads(az("network", "vnet", "list", "-g", RG, "--query", "[?subnets[?name=='pp-delegated']].id", "-o", "json"))
body = {"location": CFG["PP_GEO"], "kind": "NetworkInjection",
        "properties": {"networkInjection": {"virtualNetworks": [{"id": v, "subnet": {"name": "pp-delegated"}} for v in vnets]}}}
policy = json.loads(az("resource", "create", "-g", RG, "-n", "mcp-pp-netpolicy",
                       "--resource-type", "Microsoft.PowerPlatform/enterprisePolicies",
                       "--api-version", "2020-10-30-preview", "--is-full-object", "--properties", json.dumps(body), "-o", "json"))
system_id = policy["properties"]["systemId"]
print("enterprise policy:", policy["id"].split("/")[-1], "| systemId:", system_id, "| VNets:", len(vnets))

URL = f"{BAP}/scopes/admin/environments/{ENV_ID}?api-version=2021-04-01"


def props():
    return bap("GET", URL)[2].get("properties", {})


if (props().get("governanceConfiguration") or {}).get("protectionLevel") != "Standard":
    # NB: the /scopes/admin/... form of this route returns 404
    s, _, b = bap("POST", f"{BAP}/environments/{ENV_ID}/governanceConfiguration?api-version=2021-04-01",
                  {"protectionLevel": "Standard", "settings": {"extendedSettings": {}}})
    print("managed environment:", s, b if s >= 300 else "")
    for _ in range(20):
        if (props().get("governanceConfiguration") or {}).get("protectionLevel") == "Standard":
            break
        time.sleep(15)
print("protectionLevel:", (props().get("governanceConfiguration") or {}).get("protectionLevel"))

s, _, b = bap("POST", f"{BAP}/environments/{ENV_ID}/enterprisePolicies/NetworkInjection/link?api-version=2023-06-01",
              {"SystemId": system_id})
print("link:", s, b if s >= 300 else "")
for _ in range(40):
    pol = (props().get("enterprisePolicies") or {}).get("vNets") or {}
    if pol.get("linkStatus") == "Linked":
        break
    time.sleep(30)
print("vNets policy:", pol.get("policyId"), "| linkStatus:", pol.get("linkStatus"))
