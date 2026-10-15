#!/usr/bin/env python3
"""Step 7 — validate every prompt and every response with Zscaler AI Guard, called DIRECTLY by
Copilot Studio through a custom connector to the AI Guard Detection-as-a-Service API.

  connector "Zscaler AI Guard DAS" (host from AIGUARD_BASE), two operations:
    ExecutePolicy            POST /v1/detection/execute-policy             {policyId, direction, content}
    ResolveAndExecutePolicy  POST /v1/detection/resolve-and-execute-policy {direction, content, user,
                             transactionId} — AI Guard's match rules pick the policy; `user` is recorded
                             on the detection event and shown in AI Guard's logs
  connection: the API key as "Bearer <key>" (the Authorization header value)
  topics: "AI Guard - prompt" (OnActivity, IN) and "AI Guard - response" (OnGeneratedResponse, OUT)

Because the environment is VNet-linked, these connector calls leave from the delegated subnet too.
Requires the agent to use Microsoft authentication (System.User.PrincipalName). Idempotent.

    python3 copilot-studio/03-aiguard.py
"""
import json, sys, pathlib
from urllib.parse import urlparse
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "lib"))
from pp import CFG, bot, link_connection_reference, publish, save_state, secret, template, upsert_component
from connector import ensure_connection, ensure_connection_reference, upsert_connector

SCHEMA = CFG["AGENT_SCHEMA"]
NAME = "Zscaler AI Guard DAS"
HOST = urlparse(CFG["AIGUARD_BASE"]).netloc
b = bot() or sys.exit("agent not found — run 01-create-agent.py")
botid = b["botid"]

verdict = {"type": "object", "properties": {
    "action": {"type": "string", "title": "Action"}, "transactionId": {"type": "string", "title": "Transaction ID"},
    "policyId": {"type": "integer", "title": "Policy ID"}, "policyName": {"type": "string", "title": "Policy name"},
    "matchRuleName": {"type": "string", "title": "Match rule"}, "statusCode": {"type": "integer", "title": "Status code"}}}


def op(op_id, summary, props, required):
    return {"post": {"summary": summary, "operationId": op_id,
                     "parameters": [{"name": "body", "in": "body", "required": True,
                                     "schema": {"type": "object", "required": required, "properties": props}}],
                     "responses": {"200": {"description": "Verdict", "schema": verdict}}}}


direction = {"type": "string", "title": "Direction", "enum": ["IN", "OUT"]}
content = {"type": "string", "title": "Content"}
swagger = {
    "swagger": "2.0",
    "info": {"title": NAME, "description": "Zscaler AI Guard Detection-as-a-Service: validate a prompt (IN) or a response (OUT)", "version": "1.0.0"},
    "host": HOST, "basePath": "/", "schemes": ["https"], "consumes": ["application/json"], "produces": ["application/json"],
    "paths": {
        "/v1/detection/execute-policy": op("ExecutePolicy", "Execute AI Guard policy",
                                           {"policyId": {"type": "integer", "title": "Policy ID"}, "direction": direction, "content": content},
                                           ["policyId", "direction", "content"]),
        "/v1/detection/resolve-and-execute-policy": op("ResolveAndExecutePolicy", "Resolve and execute AI Guard policy",
                                                       {"direction": direction, "content": content,
                                                        "user": {"type": "string", "title": "User"},
                                                        # must be a 36-character UUID, or AI Guard answers 500
                                                        "transactionId": {"type": "string", "title": "Transaction ID (UUID)"}},
                                                       ["direction", "content"]),
    },
    "securityDefinitions": {"apiKey": {"type": "apiKey", "name": "Authorization", "in": "header"}},
    "security": [{"apiKey": []}],
}
api, how = upsert_connector(NAME, swagger["info"]["description"], swagger, f"https://{HOST}/")
print("connector:", how, api)
conn, how = ensure_connection(api, NAME, "Bearer " + secret("AIGUARD_API_KEY"))
print("connection:", how)
cr_id, cr_logical = ensure_connection_reference(api, conn, NAME)
print("connection reference:", cr_logical)

for schema, name, file in ((f"{SCHEMA}.topic.AIGuardPrompt", "AI Guard - prompt", "aiguard-prompt.topic.yaml"),
                           (f"{SCHEMA}.topic.AIGuardResponse", "AI Guard - response", "aiguard-response.topic.yaml")):
    cid, how = upsert_component(botid, schema, name, 9, template(file, CONNECTION_REFERENCE=cr_logical), "Zscaler AI Guard check")
    print(f"topic {name}:", how, "| link:", link_connection_reference(cid, cr_id))

status, diag = publish(botid)
print("publish:", status, json.dumps(diag)[:600] if diag else "")
save_state("aiguard.json", {"connector": api, "connection_reference": cr_logical})
