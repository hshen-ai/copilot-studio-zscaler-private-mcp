#!/usr/bin/env python3
"""Step 6a — create the agent as a CLASSIC Copilot Studio agent through the Dataverse Web API.

Why not the Copilot Studio "create" UI: agents created there in 2026 use the new agent type
(template cliagent-1.0.0, CLICopilotRecognizer). The Copilot Studio conversation API answers every
message to such an agent with "This action doesn't support agents built with the GitHub Copilot
harness", so it cannot be tested programmatically. A classic agent (template default-2.1.0) can.

Creates <AGENT_SCHEMA> with one topic (hi/hello -> "Hello World"), provisions and publishes it.
Authentication: "Authenticate with Microsoft" (Integrated), access limited (accesscontrolpolicy 2).
Idempotent.

    python3 copilot-studio/01-create-agent.py
"""
import json, sys, time, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "lib"))
from pp import CFG, bot, dv, publish, save_state, template, upsert_component

SCHEMA = CFG["AGENT_SCHEMA"]
CONFIG = {  # generative orchestration is switched on in 02-mcp-tool.py, together with the tool
    "$kind": "BotConfiguration",
    "settings": {"GenerativeActionsEnabled": False},
    "aISettings": {"$kind": "AISettings", "useModelKnowledge": False, "isFileAnalysisEnabled": False,
                   "isSemanticSearchEnabled": False, "contentModeration": "Low", "optInUseLatestModels": False},
}

b = bot()
if b:
    botid = b["botid"]
    print("agent exists:", b["name"], botid)
else:
    s, _, r = dv("POST", "bots", {
        "name": CFG["AGENT_NAME"], "schemaname": SCHEMA, "template": "default-2.1.0", "language": 1033,
        "runtimeprovider": 0, "authenticationmode": 2, "authenticationtrigger": 1, "accesscontrolpolicy": 2,
        "configuration": json.dumps(CONFIG, indent=2)}, headers={"Prefer": "return=representation"})
    if s >= 300:
        sys.exit(f"create agent failed: {s} {r}")
    botid = r["botid"]
    print("agent created:", botid)

cid, how = upsert_component(botid, f"{SCHEMA}.topic.HelloWorld", "Hello World", 9,
                            template("hello-world.topic.yaml"), "Replies Hello World to a greeting.")
print("topic Hello World:", how)

s, _, _ = dv("POST", f"bots({botid})/Microsoft.Dynamics.CRM.PvaProvision", {})
for _ in range(30):
    _, _, r = dv("GET", f"bots({botid})?$select=synchronizationstatus")
    st = json.loads(r.get("synchronizationstatus") or "{}").get("currentSynchronizationState", {})
    if st.get("provisioningStatus") == "Provisioned":
        break
    time.sleep(20)
print("provisioning:", st.get("provisioningStatus"))
status, diag = publish(botid)
print("publish:", status, json.dumps(diag)[:500] if diag else "")
save_state("agent.json", {"botid": botid, "schema": SCHEMA})
