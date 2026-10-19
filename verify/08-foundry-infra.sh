#!/usr/bin/env bash
# Check 8 — the Foundry deployment is what the agent's private path depends on: the account injects its
# agent runtime into foundry-agent (networkInjections, scenario "agent", and the service association link
# that proves it), the capability host and the model are ready, the private endpoints are approved, and
# the MCP server's private DNS zone is linked to the same VNet.
source "$(dirname "$0")/../azure/_env.sh"
# shellcheck disable=SC1091
source "$STATE/foundry.env"
ARM="https://management.azure.com/subscriptions/$AZ_SUBSCRIPTION_ID/resourceGroups/$FOUNDRY_RG/providers/Microsoft.CognitiveServices/accounts/$ACCOUNT"
API=2025-04-01-preview
FAIL=0
check() { if [[ "$2" == "$3" ]]; then echo "PASS  $1 $2"; else echo "FAIL  $1 '$2' (expected '$3')"; FAIL=1; fi; }
acct() { az rest --method get --url "$ARM?api-version=$API" --query "$1" -o tsv 2>/dev/null; }  # az's own api-version hides networkInjections

check "network injection scenario"     "$(acct 'properties.networkInjections[0].scenario')" agent
check "injected into subnet"           "$(acct 'properties.networkInjections[0].subnetArmId' | awk -F/ '{print $NF}')" foundry-agent
check "Microsoft-managed network off"  "$(acct 'properties.networkInjections[0].useMicrosoftManagedNetwork')" false
check "local (key) auth disabled"      "$(acct properties.disableLocalAuth)" true
check "service association link on foundry-agent" \
  "$(az network vnet subnet show -g "$RG" --vnet-name "$VNET_NAME" -n foundry-agent --query 'serviceAssociationLinks[0].name' -o tsv)" legionservicelink
check "project capability host"        "$(az rest --method get --url "$ARM/projects/$PROJECT/capabilityHosts/caphostproj?api-version=$API" --query properties.provisioningState -o tsv 2>/dev/null)" Succeeded
check "model deployment $FOUNDRY_MODEL" "$(az cognitiveservices account deployment show -g "$FOUNDRY_RG" -n "$ACCOUNT" --deployment-name "$FOUNDRY_MODEL" --query properties.provisioningState -o tsv 2>/dev/null)" Succeeded
check "approved private endpoints (Foundry, Cosmos DB, AI Search, Storage, Azure Monitor)" \
  "$(az network private-endpoint list -g "$FOUNDRY_RG" --query "length([?privateLinkServiceConnections[0].privateLinkServiceConnectionState.status=='Approved'])" -o tsv)" 5
ZONE=$(cut -d. -f2- "$STATE/mcp_fqdn")
check "MCP private DNS zone linked to $VNET_NAME" \
  "$(az network private-dns link vnet list -g "$RG" -z "$ZONE" --query "[?ends_with(virtualNetwork.id, '/$VNET_NAME')].virtualNetworkLinkState | [0]" -o tsv)" Completed
info=$(acct properties.publicNetworkAccess || true)
echo "INFO  Foundry publicNetworkAccess: $info (the agent's tool calls use the VNet either way)"
exit $FAIL
