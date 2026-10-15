# NOTICE

All code and documentation in this repository were written by the author; no third-party source code is
included. The author's material is personal work by Henry Shen, not an official Zscaler solution, and is
covered by `LICENSE`.

## Third-party software and services used

| Component | Used for | Terms |
|---|---|---|
| [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) (`mcp<2`), `httpx`, `uvicorn`, `certifi` | the MCP server, installed from PyPI at image build time | their own licenses (MIT / BSD / MPL-2.0) |
| [Tavily Search API](https://tavily.com) | the web search backend of the MCP server | Tavily's terms of service |
| Microsoft Copilot Studio, Power Platform, Dataverse, Azure | the agent and its hosting | your Microsoft agreements |
| Zscaler Internet Access, Zscaler AI Guard | egress control and AI security checks | your Zscaler agreement |

## Formats this repository follows

The agent components in `copilot-studio/topics/` are written in the Copilot Studio YAML format. Their
shape follows Microsoft's public samples — `microsoft/CopilotStudioSamples` (the MCP sample export and
`authoring/snippets/topics/citation-swap`) and `microsoft/skills-for-copilot-studio` (topic and
connector-action templates) — which were read, not copied. The AI Guard request fields (`user`,
`transactionId` on `resolve-and-execute-policy`) follow the public `zscaler/zscaler-sdk-python`
(`zscaler/aiguard/policy_detection.py`).

## How the values were obtained

Every masked example value comes from the reference lab, built in October 2026. Each setup step was
run end to end there with the lab scripts these were generalised from; the setup scripts in this
repository are those scripts made configurable and were syntax-checked, not re-run against the lab.
The `verify/` checks were run in exactly this form against the lab. Microsoft and Zscaler APIs used
here — several of them preview APIs — can change; where a step depends on undocumented behaviour, its
script says so in a comment.
