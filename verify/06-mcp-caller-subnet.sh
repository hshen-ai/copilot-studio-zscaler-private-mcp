#!/usr/bin/env bash
# Check 6 — Copilot Studio's MCP calls arrive over the PRIVATE network. Run after verify/04-agent-e2e.py
# (or any web question to the agent). The MCP server logs every caller; Copilot Studio's requests must
# come from the pp-delegated subnet (PP_SUBNET_PREFIX). The X-Forwarded-For header also carries a
# Microsoft public address — the TCP connection itself can only arrive through the VNet, because the
# Container Apps environment has no public endpoint (check 1).
source "$(dirname "$0")/../azure/_env.sh"
NET="${PP_SUBNET_PREFIX%.*}."        # e.g. 10.60.1.
LINES=$(az containerapp logs show -g "$RG" -n "$APP" --tail 200 --format text 2>/dev/null | grep -E "accepted POST /mcp|CallToolRequest|aiguard|web_search" || true)
echo "$LINES" | tail -15
if echo "$LINES" | grep -q "accepted POST /mcp from .*${NET//./\\.}"; then
  echo "PASS  MCP calls from the Power Platform delegated subnet (${PP_SUBNET_PREFIX})"
else
  echo "FAIL  no MCP call from ${PP_SUBNET_PREFIX} in the last 200 log lines — ask the agent a web question first"; exit 1
fi
