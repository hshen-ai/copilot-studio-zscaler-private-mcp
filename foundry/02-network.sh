#!/usr/bin/env bash
# Foundry step 2 — the Foundry subnets, in the SAME VNet as the MCP server. Idempotent.
#
#   VNet <VNET_NAME>  + address space <FOUNDRY_ADDRESS_SPACE>
#     foundry-agent  delegated to Microsoft.App/environments — Foundry injects its agent runtime (the
#                    "Data Proxy" that makes tool calls) here. One Foundry account per subnet.
#     foundry-pe     private endpoints of Foundry, Cosmos DB, AI Search, Storage and Azure Monitor
#
# Same VNet as aca-infra, so no peering and no private endpoint to the MCP server are needed: VNet
# routing reaches the Container Apps load balancer, and the private DNS zone from azure/02-mcp-server.sh
# is already linked to this VNet. Existing subnets are not modified.
source "$(dirname "$0")/../azure/_env.sh"

SPACE=$(az network vnet show -g "$RG" -n "$VNET_NAME" --query 'addressSpace.addressPrefixes' -o tsv)
if ! grep -qx "$FOUNDRY_ADDRESS_SPACE" <<<"$SPACE"; then
  # shellcheck disable=SC2086
  az network vnet update -g "$RG" -n "$VNET_NAME" --address-prefixes $SPACE "$FOUNDRY_ADDRESS_SPACE" -o none
fi
az network vnet subnet show -g "$RG" --vnet-name "$VNET_NAME" -n foundry-agent -o none 2>/dev/null || \
  az network vnet subnet create -g "$RG" --vnet-name "$VNET_NAME" -n foundry-agent \
    --address-prefixes "$FOUNDRY_AGENT_SUBNET_PREFIX" --delegations Microsoft.App/environments -o none
az network vnet subnet show -g "$RG" --vnet-name "$VNET_NAME" -n foundry-pe -o none 2>/dev/null || \
  az network vnet subnet create -g "$RG" --vnet-name "$VNET_NAME" -n foundry-pe \
    --address-prefixes "$FOUNDRY_PE_SUBNET_PREFIX" --private-endpoint-network-policies Disabled -o none

az network vnet show -g "$RG" -n "$VNET_NAME" \
  --query '{space:addressSpace.addressPrefixes,subnets:subnets[].{name:name,prefix:addressPrefix,delegation:delegations[0].serviceName}}' -o json
