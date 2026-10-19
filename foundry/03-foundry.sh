#!/usr/bin/env bash
# Foundry step 3 — Foundry account + project + standard agent setup with VNet injection (20-40 min).
#
# Uses Microsoft's own template 19-private-network-agent-tools from github.com/azure-ai-foundry/
# foundry-samples, pinned to FOUNDRY_SAMPLES_COMMIT and fetched into state/ (it is not vendored here).
# It sets networkInjections (scenario "agent") on the account, which puts the agent runtime into the
# foundry-agent subnet, and creates the standard-setup resources (Cosmos DB, AI Search, Storage) with
# private endpoints. Parameters reference the existing VNet and subnets as-is (never re-PUT).
#
# Two patches to the template: publicNetworkAccess Enabled / defaultAction Allow on the Foundry account,
# so these scripts can run from your machine. Local (key) auth stays disabled — Entra ID only. The
# agent's tool calls go through the VNet either way. Keep the template default (Disabled) and run the
# scripts from inside the VNet if your policy requires it.
#
# Idempotent: when the project capability host already exists and Succeeded, the deployment is skipped
# (capability hosts are not updated in place) and only state/foundry.env is refreshed.
source "$(dirname "$0")/../azure/_env.sh"
FRG="$FOUNDRY_RESOURCE_GROUP"
ARMRG="https://management.azure.com/subscriptions/$AZ_SUBSCRIPTION_ID/resourceGroups/$FRG/providers/Microsoft.CognitiveServices/accounts"
API=2025-04-01-preview

account()  { az cognitiveservices account list -g "$FRG" --query '[0].name' -o tsv 2>/dev/null || true; }
project()  { local p; p=$(az rest --method get --url "$ARMRG/$1/projects?api-version=$API" --query 'value[0].name' -o tsv 2>/dev/null || true); echo "${p##*/}"; }
caphost()  { az rest --method get --url "$ARMRG/$1/projects/$2/capabilityHosts/caphostproj?api-version=$API" --query properties.provisioningState -o tsv 2>/dev/null || true; }

ACC=$(account); PROJ=""; [[ -n "$ACC" ]] && PROJ=$(project "$ACC")
if [[ -n "$PROJ" && "$(caphost "$ACC" "$PROJ")" == Succeeded ]]; then
  echo "already deployed: account $ACC, project $PROJ, capability host Succeeded — skipping the deployment"
else
  # --- the pinned template, patched
  SRC="$STATE/foundry-samples"; T=infrastructure/infrastructure-setup-bicep/19-private-network-agent-tools
  if [[ ! -d "$SRC/.git" ]]; then
    git clone -q --filter=blob:none --no-checkout https://github.com/azure-ai-foundry/foundry-samples.git "$SRC"
    git -C "$SRC" sparse-checkout set "$T"
  fi
  git -C "$SRC" checkout -q "$FOUNDRY_SAMPLES_COMMIT"
  BIC="$STATE/foundry-bicep"; rm -rf "$BIC"; cp -R "$SRC/$T" "$BIC"
  python3 - "$BIC/modules-network-secured/ai-account-identity.bicep" <<'PY'
import sys
p = sys.argv[1]; s = open(p).read()
for old, new in [("publicNetworkAccess: 'Disabled'", "publicNetworkAccess: 'Enabled'"),
                 ("defaultAction: 'Deny'", "defaultAction: 'Allow'")]:
    assert s.count(old) == 1, f"patch anchor not found exactly once: {old} (template changed?)"
    s = s.replace(old, new)
open(p, "w").write(s)
PY
  az bicep version >/dev/null 2>&1 || az bicep install

  az group create -n "$FRG" -l "$AZ_LOCATION" -o none
  sid() { az network vnet subnet show -g "$RG" --vnet-name "$VNET_NAME" -n "$1" --query id -o tsv; }
  # Only the private DNS zones the private endpoints need (the template default also links azurecr.io);
  # Key Vault (private CA trust) and a container registry are not needed: the MCP server's certificate
  # is the Container Apps environment's publicly trusted one.
  cat > "$BIC/lab.parameters.json" <<EOF
{ "\$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
  "contentVersion": "1.0.0.0",
  "parameters": {
    "location": {"value": "$AZ_LOCATION"},
    "aiServices": {"value": "$FOUNDRY_ACCOUNT_PREFIX"},
    "firstProjectName": {"value": "$FOUNDRY_PROJECT_PREFIX"},
    "modelName": {"value": "$FOUNDRY_MODEL"},
    "modelVersion": {"value": "$FOUNDRY_MODEL_VERSION"},
    "modelSkuName": {"value": "$FOUNDRY_MODEL_SKU"},
    "modelCapacity": {"value": $FOUNDRY_MODEL_CAPACITY},
    "existingVnetResourceId": {"value": "$(az network vnet show -g "$RG" -n "$VNET_NAME" --query id -o tsv)"},
    "existingAgentSubnetResourceId": {"value": "$(sid foundry-agent)"},
    "existingPeSubnetResourceId": {"value": "$(sid foundry-pe)"},
    "existingMcpSubnetResourceId": {"value": "$(sid aca-infra)"},
    "enableKeyVault": {"value": false},
    "enableContainerRegistry": {"value": false},
    "existingDnsZones": {"value": {
      "privatelink.services.ai.azure.com": {"subscriptionId": "", "resourceGroup": ""},
      "privatelink.openai.azure.com": {"subscriptionId": "", "resourceGroup": ""},
      "privatelink.cognitiveservices.azure.com": {"subscriptionId": "", "resourceGroup": ""},
      "privatelink.search.windows.net": {"subscriptionId": "", "resourceGroup": ""},
      "privatelink.blob.core.windows.net": {"subscriptionId": "", "resourceGroup": ""},
      "privatelink.documents.azure.com": {"subscriptionId": "", "resourceGroup": ""}
    }}
  }
}
EOF
  echo "--- validate"
  az deployment group validate -g "$FRG" -n foundry-agent-setup --template-file "$BIC/main.bicep" \
    --parameters @"$BIC/lab.parameters.json" --query properties.provisioningState -o tsv
  echo "--- deploy $(date -u +%H:%M:%SZ) (20-40 min; progress every minute)"
  az deployment group create -g "$FRG" -n foundry-agent-setup --template-file "$BIC/main.bicep" \
    --parameters @"$BIC/lab.parameters.json" --no-wait -o none
  for i in $(seq 1 90); do
    sleep 60
    S=$(az deployment group show -g "$FRG" -n foundry-agent-setup --query properties.provisioningState -o tsv)
    R=$(az deployment operation group list -g "$FRG" -n foundry-agent-setup \
      --query "[?properties.provisioningState=='Running'].properties.targetResource.resourceName" -o tsv | tr '\n' ' ')
    echo "$(date -u +%H:%M:%SZ) $S  running: $R"
    [[ "$S" == Running || "$S" == Accepted ]] || break
  done
  if [[ "$S" != Succeeded ]]; then
    az deployment operation group list -g "$FRG" -n foundry-agent-setup \
      --query "[?properties.provisioningState=='Failed'].{resource:properties.targetResource.resourceName,error:properties.statusMessage.error}" -o json
    exit 1
  fi
  ACC=$(account); PROJ=$(project "$ACC")
fi

cat > "$STATE/foundry.env" <<EOF
FOUNDRY_RG=$FRG
ACCOUNT=$ACC
PROJECT=$PROJ
PROJECT_ENDPOINT=https://$ACC.services.ai.azure.com/api/projects/$PROJ
EOF
cat "$STATE/foundry.env"
