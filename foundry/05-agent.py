#!/usr/bin/env python3
"""Foundry step 5 — the prompt agent and its MCP tool (agents v2). Idempotent.

A new agent version is created only when the model, the instructions or the tool differ from the latest
version. The tool points at the MCP server's private URL; authentication comes from the project
connection of step 4 (project_connection_id), so the definition holds no secret.

    .venv/bin/python foundry/05-agent.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "lib"))
from foundry import CFG, foundry_state, mcp_url, project_client

from azure.ai.projects.models import MCPTool, PromptAgentDefinition
from azure.core.exceptions import ResourceNotFoundError

FS = foundry_state()
AGENT = CFG["FOUNDRY_AGENT_NAME"]
INSTRUCTIONS = """You are Hello World Foundry, a friendly demo agent.
- When the user greets you, reply with a short greeting that contains the words "Hello World".
- For any question about current events, news, or anything that may have changed recently, you MUST call the
  web_search tool first and answer only from its results, citing the source URLs.
- If web_search returns no results or reports that it was blocked, say so. Never answer current events from memory."""

tool = MCPTool(
    server_label="websearch",
    server_url=mcp_url(),
    server_description="Private web search (Tavily) on an internal-only Container Apps environment, egress via Zscaler",
    allowed_tools=["web_search"],
    require_approval="never",
    project_connection_id="websearch-mcp",
)
want = PromptAgentDefinition(model=CFG["FOUNDRY_MODEL"], instructions=INSTRUCTIONS, tools=[tool]).as_dict()


def same(have):
    if not have:
        return False
    t_have, t_want = (have.get("tools") or [{}])[0], want["tools"][0]
    keys = ("server_label", "server_url", "project_connection_id", "require_approval")  # echoed back by the service
    return (have.get("model") == want["model"] and have.get("instructions") == want["instructions"]
            and all(t_have.get(k) == t_want.get(k) for k in keys))


with project_client(FS) as pc:
    try:
        latest = pc.agents.get(agent_name=AGENT).as_dict().get("versions", {}).get("latest", {})
    except ResourceNotFoundError:
        latest = {}
    if same(latest.get("definition")):
        print("unchanged:", AGENT, "version", latest.get("version"))
    else:
        v = pc.agents.create_version(agent_name=AGENT, definition=PromptAgentDefinition(want))
        print("created:", v.name, "version", v.version)
    d = pc.agents.get(agent_name=AGENT).as_dict()["versions"]["latest"]["definition"]
    print("model:", d.get("model"), "| tool:", {k: d["tools"][0].get(k) for k in
                                               ("type", "server_label", "server_url", "project_connection_id", "require_approval")})
