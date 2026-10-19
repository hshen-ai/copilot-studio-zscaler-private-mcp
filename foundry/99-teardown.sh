#!/usr/bin/env bash
# Foundry teardown — deletes the Foundry account, project and FOUNDRY_RESOURCE_GROUP, then the Foundry
# subnets and address space. The MCP server, network and Copilot Studio parts are not touched.
#
# Order matters: the capability host holds a service association link (legionservicelink, not deletable
# by you) on foundry-agent. Delete the capability hosts, delete AND PURGE the account (purge is what
# releases it), wait for the link to disappear (up to ~20 min), and only then delete the subnet.
#
#     CONFIRM=delete-foundry foundry/99-teardown.sh
source "$(dirname "$0")/../azure/_env.sh"
[[ "${CONFIRM:-}" == "delete-foundry" ]] || { echo "refusing: set CONFIRM=delete-foundry"; exit 2; }
# shellcheck disable=SC1091
source "$STATE/foundry.env"
API=2025-04-01-preview
ARM="https://management.azure.com/subscriptions/$AZ_SUBSCRIPTION_ID/resourceGroups/$FOUNDRY_RG/providers/Microsoft.CognitiveServices/accounts/$ACCOUNT"

az rest --method delete --url "$ARM/projects/$PROJECT/capabilityHosts/caphostproj?api-version=$API" || true
for ch in $(az rest --method get --url "$ARM/capabilityHosts?api-version=$API" --query 'value[].name' -o tsv 2>/dev/null); do
  az rest --method delete --url "$ARM/capabilityHosts/${ch##*/}?api-version=$API" || true
done
az cognitiveservices account delete -g "$FOUNDRY_RG" -n "$ACCOUNT"
az cognitiveservices account purge -g "$FOUNDRY_RG" -n "$ACCOUNT" -l "$AZ_LOCATION"
az group delete -n "$FOUNDRY_RG" --yes

for i in $(seq 1 40); do
  n=$(az network vnet subnet show -g "$RG" --vnet-name "$VNET_NAME" -n foundry-agent --query 'length(serviceAssociationLinks)' -o tsv)
  [[ "$n" == 0 ]] && break
  echo "waiting for the service association link to be released ($i/40)"; sleep 30
done
az network vnet subnet delete -g "$RG" --vnet-name "$VNET_NAME" -n foundry-agent
az network vnet subnet delete -g "$RG" --vnet-name "$VNET_NAME" -n foundry-pe
REST=$(az network vnet show -g "$RG" -n "$VNET_NAME" --query 'addressSpace.addressPrefixes' -o tsv | grep -vx "$FOUNDRY_ADDRESS_SPACE")
# shellcheck disable=SC2086
az network vnet update -g "$RG" -n "$VNET_NAME" --address-prefixes $REST -o none
rm -f "$STATE/foundry.env"
echo "Foundry part removed"
