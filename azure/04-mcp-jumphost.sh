#!/usr/bin/env bash
# Deploy the universal MCP jumphost to Azure Container Apps in the internal environment.
# Accepts inbound MCP Streamable-HTTP requests with Entra Bearer tokens, performs OBO exchange
# to a ZIA egress token, and relays requests through ZIA explicit proxy to upstream MCP servers.
#
# Modelled on azure/02-mcp-server.sh. Idempotent; re-run to roll out code updates.
NEED_SECRETS=1 source "$(dirname "$0")/_env.sh"
TAG="${1:-$(date -u +%Y%m%d%H%M)}"

[[ -f "$ROOT/$ZSCALER_ROOT_CA" ]] || { echo "missing $ZSCALER_ROOT_CA (Zscaler root CA, PEM) — README, Prerequisites"; exit 2; }

# --- reuse the same ACR
ACR=$(az acr list -g "$RG" --query '[0].name' -o tsv)
if [[ -z "$ACR" ]]; then
  echo "Error: no ACR found in resource group $RG" >&2
  exit 2
fi

# --- build context: mcp-jumphost files + Zscaler root CA
BUILD="$(mktemp -d)"; trap 'rm -rf "$BUILD"' EXIT
cp "$ROOT"/mcp-jumphost/{jumphost.py,auth.py,egress.py,Dockerfile,requirements.txt} "$BUILD/"
cp "$ROOT/$ZSCALER_ROOT_CA" "$BUILD/zscaler-root-ca.crt"
az acr build -r "$ACR" -t "mcp-jumphost:$TAG" "$BUILD" --no-logs -o none
IMAGE="$ACR.azurecr.io/mcp-jumphost:$TAG"
echo "image: $IMAGE"

# --- internal environment (same env as websearch-mcp)
ACTUAL_ENV=$(az containerapp show -g "$RG" -n "$APP" --query 'properties.environmentId' -o tsv 2>/dev/null | awk -F/ '{print $NF}')
if [[ -n "$ACTUAL_ENV" ]]; then
  ACA_ENV="$ACTUAL_ENV"
fi
DOMAIN=$(az containerapp env show -g "$RG" -n "$ACA_ENV" --query properties.defaultDomain -o tsv)
SIP=$(az containerapp env show -g "$RG" -n "$ACA_ENV" --query properties.staticIp -o tsv)

# --- container app configuration
JUMPHOST_APP="${JUMPHOST_APP:-mcp-jumphost}"
DEFAULT_ROUTES='{"dlptest":"https://mcp.dlptest.com/api/mcp/"}'
ROUTES="${JUMPHOST_ROUTES:-$DEFAULT_ROUTES}"
FQDN="$JUMPHOST_APP.$DOMAIN"

SECRETS=("mcp-client-secret=$MCP_CLIENT_SECRET")
ENVV=(
  "ROUTES=$ROUTES"
  "MCP_AUTH=entra"
  "ENTRA_TENANT_ID=$TENANT_ID"
  "MCP_AUDIENCE=$MCP_CLIENT_ID,api://$MCP_CLIENT_ID"
  "MCP_REQUIRED_SCOPES=mcp.access"
  "MCP_CLIENT_ID=$MCP_CLIENT_ID"
  "MCP_CLIENT_SECRET=secretref:mcp-client-secret"
  "ZIA_EGRESS_MODE=proxy-caller"
  "ZIA_PROXY=$ZIA_PROXY"
  "ZIA_EGRESS_SCOPE=$ZIA_EGRESS_SCOPE"
  "MCP_PUBLIC_BASE=https://$FQDN"
)

ACR_ID=$(az acr show -n "$ACR" --query id -o tsv)

if ! az containerapp show -g "$RG" -n "$JUMPHOST_APP" -o none 2>/dev/null; then
  echo "creating containerapp $JUMPHOST_APP in $ACA_ENV..."
  az containerapp create -g "$RG" -n "$JUMPHOST_APP" --environment "$ACA_ENV" --image "$IMAGE" \
    --registry-server "$ACR.azurecr.io" --registry-identity system \
    --ingress external --target-port 8080 --transport http --min-replicas 1 --max-replicas 1 \
    --secrets "${SECRETS[@]}" --env-vars "${ENVV[@]}" -o none || {
      echo "az containerapp create failed; ensuring system identity and AcrPull role..."
      az containerapp identity assign -g "$RG" -n "$JUMPHOST_APP" --system-assigned -o none 2>/dev/null || true
      PID=$(az containerapp show -g "$RG" -n "$JUMPHOST_APP" --query 'identity.principalId' -o tsv)
      if [[ -n "$PID" ]]; then
        az role assignment create --assignee-object-id "$PID" --assignee-principal-type ServicePrincipal --role AcrPull --scope "$ACR_ID" -o none 2>/dev/null || true
        sleep 20
        az containerapp registry set -g "$RG" -n "$JUMPHOST_APP" --server "$ACR.azurecr.io" --identity system -o none
        az containerapp update -g "$RG" -n "$JUMPHOST_APP" --image "$IMAGE" -o none
      fi
  }
else
  echo "updating containerapp $JUMPHOST_APP..."
  az containerapp secret set -g "$RG" -n "$JUMPHOST_APP" --secrets "${SECRETS[@]}" -o none
  az containerapp update -g "$RG" -n "$JUMPHOST_APP" --image "$IMAGE" --revision-suffix "r$TAG" --set-env-vars "${ENVV[@]}" -o none
fi

# Ensure AcrPull role assignment exists for system identity
PID=$(az containerapp show -g "$RG" -n "$JUMPHOST_APP" --query 'identity.principalId' -o tsv 2>/dev/null || true)
if [[ -n "$PID" ]]; then
  if ! az role assignment list --assignee "$PID" --scope "$ACR_ID" --query '[0].id' -o tsv 2>/dev/null | grep -q .; then
    az role assignment create --assignee-object-id "$PID" --assignee-principal-type ServicePrincipal --role AcrPull --scope "$ACR_ID" -o none 2>/dev/null || true
  fi
fi

REAL_FQDN=$(az containerapp show -g "$RG" -n "$JUMPHOST_APP" --query properties.configuration.ingress.fqdn -o tsv)
if [[ -n "$REAL_FQDN" && "$REAL_FQDN" != "$FQDN" ]]; then
  FQDN="$REAL_FQDN"
  az containerapp update -g "$RG" -n "$JUMPHOST_APP" --set-env-vars "MCP_PUBLIC_BASE=https://$FQDN" -o none
fi

# --- verify private DNS wildcard
if az network private-dns record-set a show -g "$RG" -z "$DOMAIN" -n '*' --query 'aRecords[0].ipv4Address' -o tsv 2>/dev/null | grep -q .; then
  echo "private DNS wildcard verified: *.$DOMAIN -> $SIP"
else
  echo "WARNING: private DNS wildcard record for *.$DOMAIN not found"
fi

echo "$FQDN" > "$STATE/jumphost_fqdn"

# --- poll for revision health (up to 5 min)
LATEST_REV=$(az containerapp show -g "$RG" -n "$JUMPHOST_APP" --query 'properties.latestRevisionName' -o tsv)
echo "waiting for $JUMPHOST_APP revision $LATEST_REV to be Healthy and Running..."
for i in $(seq 1 30); do
  HEALTH=$(az containerapp revision show -g "$RG" -n "$JUMPHOST_APP" --revision "$LATEST_REV" --query 'properties.healthState' -o tsv 2>/dev/null || true)
  RUNNING=$(az containerapp revision show -g "$RG" -n "$JUMPHOST_APP" --revision "$LATEST_REV" --query 'properties.runningState' -o tsv 2>/dev/null || true)
  if [[ "$HEALTH" == "Healthy" && "$RUNNING" == Running* ]]; then
    echo "Revision $LATEST_REV is Healthy and $RUNNING."
    break
  fi
  sleep 10
done

az containerapp revision list -g "$RG" -n "$JUMPHOST_APP" \
  --query '[?properties.active].{revision:name,health:properties.healthState,running:properties.runningState}' -o table

echo "internal environment: $(az containerapp env show -g "$RG" -n "$ACA_ENV" --query properties.vnetConfiguration.internal -o tsv), load balancer $SIP"
echo "JUMPHOST_URL=https://$FQDN/dlptest/mcp"
