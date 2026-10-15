#!/usr/bin/env bash
# Step 1 — Azure network for the private MCP server. Idempotent.
#
#   VNet <VNET_NAME>
#     pp-delegated  delegated to Microsoft.PowerPlatform/enterprisePolicies — Copilot Studio's
#                   connector traffic (MCP calls, AI Guard calls) is injected here
#     aca-infra     delegated to Microsoft.App/environments — the internal Container Apps environment
#     test          delegated to Microsoft.ContainerInstance — throwaway probes used by verify/
#   NAT gateway with a static public IP on aca-infra and test: the ONE egress address, registered in
#   ZIA as a Static IP Location.
#   Optional paired-region VNet + delegated subnet + peering when AZ_PAIRED_LOCATION is set.
source "$(dirname "$0")/_env.sh"

for p in Microsoft.App Microsoft.ContainerRegistry Microsoft.ContainerInstance Microsoft.Network \
         Microsoft.OperationalInsights Microsoft.PowerPlatform; do
  az provider register -n "$p" --wait >/dev/null
  echo "provider $p: $(az provider show -n "$p" --query registrationState -o tsv)"
done

az group create -n "$RG" -l "$AZ_LOCATION" -o none
az network vnet create -g "$RG" -n "$VNET_NAME" -l "$AZ_LOCATION" --address-prefixes "$VNET_PREFIX" -o none

subnet() {  # name prefix delegation
  if ! az network vnet subnet show -g "$RG" --vnet-name "$VNET_NAME" -n "$1" -o none 2>/dev/null; then
    az network vnet subnet create -g "$RG" --vnet-name "$VNET_NAME" -n "$1" --address-prefixes "$2" --delegations "$3" -o none
  fi
}
subnet pp-delegated "$PP_SUBNET_PREFIX"   Microsoft.PowerPlatform/enterprisePolicies
subnet aca-infra    "$ACA_SUBNET_PREFIX"  Microsoft.App/environments
subnet test         "$TEST_SUBNET_PREFIX" Microsoft.ContainerInstance/containerGroups

if [[ -n "${AZ_PAIRED_LOCATION:-}" ]]; then
  PV="${VNET_NAME}-paired"
  az network vnet create -g "$RG" -n "$PV" -l "$AZ_PAIRED_LOCATION" --address-prefixes "$PAIRED_VNET_PREFIX" -o none
  az network vnet subnet show -g "$RG" --vnet-name "$PV" -n pp-delegated -o none 2>/dev/null || \
    az network vnet subnet create -g "$RG" --vnet-name "$PV" -n pp-delegated --address-prefixes "$PAIRED_PP_SUBNET_PREFIX" \
      --delegations Microsoft.PowerPlatform/enterprisePolicies -o none
  N=$(az network vnet show -g "$RG" -n "$VNET_NAME" --query id -o tsv)
  W=$(az network vnet show -g "$RG" -n "$PV" --query id -o tsv)
  az network vnet peering create -g "$RG" -n to-paired --vnet-name "$VNET_NAME" --remote-vnet "$W" --allow-vnet-access -o none 2>/dev/null || true
  az network vnet peering create -g "$RG" -n to-primary --vnet-name "$PV" --remote-vnet "$N" --allow-vnet-access -o none 2>/dev/null || true
fi

az network public-ip create -g "$RG" -n mcp-egress-pip -l "$AZ_LOCATION" --sku Standard --allocation-method Static -o none
az network nat gateway create -g "$RG" -n mcp-egress-natgw -l "$AZ_LOCATION" --public-ip-addresses mcp-egress-pip --idle-timeout 10 -o none
for sn in aca-infra test; do
  az network vnet subnet update -g "$RG" --vnet-name "$VNET_NAME" -n "$sn" --nat-gateway mcp-egress-natgw -o none
done

az network vnet list -g "$RG" --query '[].{vnet:name,loc:location,subnets:subnets[].{name:name,prefix:addressPrefix,delegation:delegations[0].serviceName,nat:natGateway.id!=null}}' -o json
IP=$(az network public-ip show -g "$RG" -n mcp-egress-pip --query ipAddress -o tsv)
echo "$IP" > "$STATE/nat_public_ip"
echo
echo "NAT public IP: $IP"
echo "NEXT (Zscaler): add $IP as a Static IP and a Location in ZIA, then Activate (README, Setup step 2)."
