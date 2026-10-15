#!/usr/bin/env bash
# Step 4 — make ZIA the ONLY Internet path of the MCP subnet. An NSG on aca-infra and test allows
# outbound TCP 80/443 to the ZIA proxy's addresses and the Azure platform service tags Container Apps
# needs, and denies all other Internet. Re-run if the ZIA proxy hostname resolves to new addresses.
source "$(dirname "$0")/_env.sh"
NSG=mcp-egress-nsg
HOST="${ZIA_PROXY%%:*}"
IPS=$(dig +short "$HOST" @1.1.1.1 | grep -E '^[0-9.]+$' | tr '\n' ' ')
[[ -n "$IPS" ]] || { echo "cannot resolve $HOST"; exit 2; }
echo "ZIA proxy $HOST -> $IPS"

az network nsg create -g "$RG" -n "$NSG" -l "$AZ_LOCATION" -o none
rule() { az network nsg rule create -g "$RG" --nsg-name "$NSG" --direction Outbound -o none "$@"; }
# shellcheck disable=SC2086
rule -n allow-zia --priority 100 --access Allow --protocol Tcp --destination-address-prefixes $IPS --destination-port-ranges 80 443
p=110
for tag in AzureContainerRegistry MicrosoftContainerRegistry AzureFrontDoor.FirstParty AzureActiveDirectory AzureMonitor "Storage.$AZ_LOCATION"; do
  rule -n "allow-${tag//./-}" --priority $p --access Allow --protocol '*' --destination-address-prefixes "$tag" --destination-port-ranges '*'
  p=$((p+10))
done
rule -n allow-vnet --priority 200 --access Allow --protocol '*' --destination-address-prefixes VirtualNetwork --destination-port-ranges '*'
rule -n deny-internet --priority 4000 --access Deny --protocol '*' --destination-address-prefixes Internet --destination-port-ranges '*'
for sn in aca-infra test; do
  az network vnet subnet update -g "$RG" --vnet-name "$VNET_NAME" -n "$sn" --network-security-group "$NSG" -o none
done
az network nsg rule list -g "$RG" --nsg-name "$NSG" --query '[].{priority:priority,name:name,access:access,to:join(`,`,[destinationAddressPrefix || ``, join(`,`, destinationAddressPrefixes)])}' -o table
echo "Verify with verify/02-egress-only-via-zia.sh"
