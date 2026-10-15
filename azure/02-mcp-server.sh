#!/usr/bin/env bash
# Step 3 — build the web-search MCP server in ACR and run it in an INTERNAL Container Apps
# environment (no public IP, no public DNS), with a private DNS zone linked to the VNet(s).
# Idempotent; re-run to roll out a code change.
#
# Secrets (Tavily key, AI Guard key, the generated MCP API key) go straight into Container Apps
# secrets; nothing is printed. The MCP API key is kept in state/mcp_api_key (mode 600) because the
# Copilot Studio connection needs it in step 6.
#
# Egress: HTTPS_PROXY points at ZIA (<ZIA_PROXY>), so Tavily and AI Guard calls go through ZIA.
NEED_SECRETS=1 source "$(dirname "$0")/_env.sh"
TAG="${1:-$(date -u +%Y%m%d%H%M)}"

[[ -f "$ROOT/$ZSCALER_ROOT_CA" ]] || { echo "missing $ZSCALER_ROOT_CA (Zscaler root CA, PEM) — README, Prerequisites"; exit 2; }
KEYFILE="$STATE/mcp_api_key"
[[ -s "$KEYFILE" ]] || (umask 077; openssl rand -hex 32 > "$KEYFILE")
MCP_API_KEY=$(cat "$KEYFILE")

# --- registry + cloud build (no local Docker needed)
ACR=$(az acr list -g "$RG" --query '[0].name' -o tsv)
if [[ -z "$ACR" ]]; then
  ACR="mcp$(openssl rand -hex 4)"
  az acr create -g "$RG" -n "$ACR" -l "$AZ_LOCATION" --sku Basic --admin-enabled false -o none
fi
BUILD="$(mktemp -d)"; trap 'rm -rf "$BUILD"' EXIT
cp "$ROOT"/mcp-server/{server.py,Dockerfile,requirements.txt} "$BUILD/"
cp "$ROOT/$ZSCALER_ROOT_CA" "$BUILD/zscaler-root-ca.crt"
az acr build -r "$ACR" -t "websearch-mcp:$TAG" "$BUILD" --no-logs -o none
IMAGE="$ACR.azurecr.io/websearch-mcp:$TAG"
echo "image: $IMAGE"

# --- internal environment
SUBNET=$(az network vnet subnet show -g "$RG" --vnet-name "$VNET_NAME" -n aca-infra --query id -o tsv)
az containerapp env show -g "$RG" -n "$ACA_ENV" -o none 2>/dev/null || \
  az containerapp env create -g "$RG" -n "$ACA_ENV" -l "$AZ_LOCATION" --infrastructure-subnet-resource-id "$SUBNET" \
    --internal-only true --enable-workload-profiles --logs-destination none -o none
DOMAIN=$(az containerapp env show -g "$RG" -n "$ACA_ENV" --query properties.defaultDomain -o tsv)
SIP=$(az containerapp env show -g "$RG" -n "$ACA_ENV" --query properties.staticIp -o tsv)

# --- the app. Ingress "external" = reachable from the VNet; the environment itself is internal-only.
SECRETS=("mcp-api-key=$MCP_API_KEY" "tavily-api-key=$TAVILY_API_KEY" "aiguard-api-key=$AIGUARD_API_KEY")
ENVV=(MCP_API_KEY=secretref:mcp-api-key TAVILY_API_KEY=secretref:tavily-api-key AIGUARD_API_KEY=secretref:aiguard-api-key
      "AIGUARD_POLICY_ID=$AIGUARD_POLICY_ID" "AIGUARD_URL=$AIGUARD_BASE/v1/detection/execute-policy"
      "HTTPS_PROXY=http://$ZIA_PROXY" "HTTP_PROXY=http://$ZIA_PROXY" "NO_PROXY=localhost,127.0.0.1")
if ! az containerapp show -g "$RG" -n "$APP" -o none 2>/dev/null; then
  az containerapp create -g "$RG" -n "$APP" --environment "$ACA_ENV" --image "$IMAGE" \
    --registry-server "$ACR.azurecr.io" --registry-identity system \
    --ingress external --target-port 8080 --transport http --min-replicas 1 --max-replicas 1 \
    --secrets "${SECRETS[@]}" --env-vars "${ENVV[@]}" -o none
else
  az containerapp secret set -g "$RG" -n "$APP" --secrets "${SECRETS[@]}" -o none
  az containerapp update -g "$RG" -n "$APP" --image "$IMAGE" --revision-suffix "r$TAG" --set-env-vars "${ENVV[@]}" -o none
fi
FQDN=$(az containerapp show -g "$RG" -n "$APP" --query properties.configuration.ingress.fqdn -o tsv)

# --- private DNS: the environment's default domain -> its internal load balancer, on every VNet
az network private-dns zone show -g "$RG" -n "$DOMAIN" -o none 2>/dev/null || az network private-dns zone create -g "$RG" -n "$DOMAIN" -o none
for name in '*' '@'; do
  [[ -n "$(az network private-dns record-set a show -g "$RG" -z "$DOMAIN" -n "$name" --query 'aRecords[0].ipv4Address' -o tsv 2>/dev/null)" ]] || \
    az network private-dns record-set a add-record -g "$RG" -z "$DOMAIN" -n "$name" -a "$SIP" -o none
done
for v in $(az network vnet list -g "$RG" --query '[].name' -o tsv); do
  az network private-dns link vnet show -g "$RG" -z "$DOMAIN" -n "link-$v" -o none 2>/dev/null || \
    az network private-dns link vnet create -g "$RG" -z "$DOMAIN" -n "link-$v" -v "$v" -e false -o none
done

echo "$FQDN" > "$STATE/mcp_fqdn"
sleep 30
az containerapp revision list -g "$RG" -n "$APP" --query '[?properties.active].{revision:name,health:properties.healthState,running:properties.runningState}' -o table
echo "internal environment: $(az containerapp env show -g "$RG" -n "$ACA_ENV" --query properties.vnetConfiguration.internal -o tsv), load balancer $SIP"
echo "MCP_URL=https://$FQDN/mcp"
