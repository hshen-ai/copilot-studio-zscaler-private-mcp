# Universal MCP Jumphost

A lightweight, secure MCP Streamable-HTTP relay that bridges modern enterprise AI agents (e.g., Copilot Studio) to real upstream MCP tool servers through the **Zscaler Internet Access (ZIA)** explicit proxy with per-user attribution. It hosts no MCP tools directly.

## Architecture: Method 2 (Explicit-Proxy User Attribution)

```text
+------------------------+
| Copilot Studio Agent   |
+-----------+------------+
            | Inbound HTTP with Entra User Bearer Token (Authorization: Bearer <user token>)
            v
+------------------------+
|  mcp-jumphost          |  1. Validates Entra/Okta JWT signature & aud against IdP JWKS
|  (/<route>/mcp)        |  2. Exchanges user token via Entra On-Behalf-Of (OBO) -> ZIA Egress Token
+-----------+------------+
            | Outbound HTTPS through ZIA Explicit Proxy CONNECT:
            | Proxy-Authorization: Bearer <zia egress token>
            v
+------------------------+
|  ZIA Explicit Proxy    |  Attributes egress traffic to caller's identity (e.g. email/UPN)
+-----------+------------+
            | AI Gateway Forward Rule (injects X-Authenticated-User)
            v
+------------------------+
| Zscaler AI Guard /     |  Enforces AI security policies, prompt injection detection,
| AI Gateway             |  DLP, and audit logging under the user's real corporate identity
+-----------+------------+
            | Forward to upstream target
            v
+------------------------+
| Real MCP Server        |  (e.g., Jira, ServiceNow, Tavily, internal tools)
+------------------------+
```

### Key Security Properties
- **No AI Gateway Virtual Key**: The jumphost does not store or use a static AI Gateway virtual API key. All upstream attribution and policy enforcement is keyed off the user's authentic identity via ZIA explicit proxy authentication.
- **Header Sanitization**: Inbound `Authorization`, `Cookie`, `Host`, and `X-Forwarded-*` headers are stripped before relaying upstream. Only standard MCP and content negotiation headers (`Content-Type`, `Accept`, `Mcp-Session-Id`, `Mcp-Protocol-Version`, `Last-Event-ID`, `User-Agent`) are forwarded.
- **Fail-Closed Egress**: If the user lacks a valid token or the Entra OBO exchange fails, `EgressDenied` is raised and the request is aborted with HTTP 502 without sending any traffic upstream.
- **Protected Resource Metadata**: Serves RFC 9728 resource metadata at `GET /.well-known/oauth-protected-resource/<route>/mcp` for agent token acquisition.

## Environment Variables

| Variable | Required | Default | Description |
| :--- | :--- | :--- | :--- |
| `ROUTES` | Yes | - | JSON object mapping route prefixes to upstream HTTPS URLs, e.g. `{"dlptest": "https://mcp.dlptest.com/api/mcp/"}` |
| `MCP_PUBLIC_BASE` | No | Request base URL | Public base URL used in RFC 9728 resource metadata and `WWW-Authenticate` headers, e.g. `https://jumphost.internal` |
| `MCP_AUTH` | Yes | - | Inbound authentication IdP kind (`entra` or `okta`) |
| `MCP_AUDIENCE` | Yes | - | Expected audience(s) in caller token (comma-separated) |
| `MCP_REQUIRED_SCOPES` | No | - | Scopes required for inbound callers (space- or comma-separated). Tokens missing these receive HTTP 403. |
| `ENTRA_TENANT_ID` | If Entra | - | Microsoft Entra Directory (tenant) ID |
| `OKTA_ISSUER` | If Okta | - | Okta authorization server issuer URL |
| `ZIA_EGRESS_MODE` | Yes | `proxy` | Egress mode: `direct`, `proxy`, `proxy-token-file`, or `proxy-caller` (production standard) |
| `ZIA_PROXY` | If proxy | - | Host and port of the ZIA explicit proxy (e.g. `gateway.zscaler.net:80`) |
| `MCP_CLIENT_ID` | If OBO | - | Application (client) ID of this jumphost in Entra ID |
| `MCP_CLIENT_SECRET` | If OBO | - | Client secret for Entra OBO exchange (or path via `MCP_CLIENT_SECRET_FILE`) |
| `ZIA_EGRESS_SCOPE` | If OBO | - | Scope requested for ZIA egress token (e.g. `https://<zia-app-id>/.default`) |
| `ZIA_EGRESS_AUDIENCE` | If Okta | - | Target audience for ZIA egress token under Okta token exchange |
| `ZSCALER_CA_FILE` | No | System roots | Path to the Zscaler root CA certificate bundle for SSL inspection |
| `SSL_CERT_FILE` | No | System roots | Fallback path to CA bundle |
| `IDP_PROXY` | No | `ZIA_PROXY` | Explicit proxy for IdP JWKS and token exchange endpoints |
| `PORT` | No | `8080` | Port for the HTTP server |

## Validated 2026-11-03 (Method 2 End-to-End)

The Method 2 explicit-proxy architecture was tested and verified end-to-end on 2026-11-03:

### 1. ZIA & AI Gateway Prerequisites
Configured via **AI Security Console > Tenant Settings > Internal Integrations > ZIA**:
- **MCP Servers to forward**: Select your destination MCP server (e.g. `DLP Test MCP`).
- **Users**: Select the user or group (e.g. `<user@your-domain>`). **Never leave Users empty** — empty scopes break forwarding evaluation.
- Click **Save destinations and rules**, then **Activate** in ZIA.
- This automatically creates the following in ZIA:
  1. `AIGateway Forward Rule` (Forwarding Control): routes matching sessions to the AI Gateway.
  2. `AIGateway TLS Inspection` (SSL Inspection rule): decrypts traffic for policy inspection.
  3. `AIGateway MCP FQDNs` (Destination Group): matches the upstream MCP hostnames.

### 2. Architecture Observations
- **Real Upstream URLs (No Virtual Key)**: The jumphost sends requests directly to the real upstream URL (`https://mcp.dlptest.com/api/mcp/`), NOT `gateway.zsagentic.ai`. No static AI Gateway virtual API key is stored or needed on the jumphost.
- **Signed XAU Attribution**: In ZIA / AI Gateway Observability logs, requests appear with `auth_type: XAU`, `user: <user@your-domain>`, `location: <your-location>`, `user_agent: python-httpx`, and a cryptographically signed header (`hmac_present: true`, `key_id: aig1-zscaler`). This HMAC-signed user attribution is added by ZIA during proxy chaining and is not documented in standard design-partner guides.
- **A/B Policy Proof**: Verified live with a test policy (e.g. `jumphost-AB-test-block-ssn-ccn`). Within the same session, `tools/call generate_pii_ssn_ccn` was blocked by AI Gateway policy (`isError: true`, *"⛔ Request blocked by Zscaler"*), while `generate_pii_email` was allowed through.

### 3. Adding Another MCP Server
1. Add an entry to the `ROUTES` JSON object in `azure/04-mcp-jumphost.sh` (or `JUMPHOST_ROUTES` in `config/demo.env`):
   ```json
   {"dlptest": "https://mcp.dlptest.com/api/mcp/", "newapp": "https://mcp.mycorp.internal/mcp/"}
   ```
2. In the AI Console (**Tenant Settings > Internal Integrations > ZIA**), select the new MCP server under forwarded servers and re-save/activate.

### 4. Deployment & Verification
- **Deploy**: `bash azure/04-mcp-jumphost.sh [TAG]`
- **Run E2E Probe**: `python3 verify/08-jumphost-e2e.py [--run A|B]` (runs inside the VNet via ephemeral ACI).
