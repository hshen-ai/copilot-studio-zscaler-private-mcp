# Sourced by verify/*.sh. run_in_vnet <name> <python-file> — runs a Python probe inside the VNet
# (subnet `test`, same NSG and NAT as the MCP server) as a throwaway Azure Container Instance, prints its
# output, deletes it. The image is the MCP server's own image from your ACR (Python + httpx), pulled with
# a user-assigned identity, because the NSG blocks Docker Hub. MCP_FQDN and MCP_API_KEY are passed in;
# the key as a secure environment variable.
run_in_vnet() {
  local name="$1" code="$2"
  local acr mi principal image
  acr=$(az acr list -g "$RG" --query '[0].name' -o tsv)
  image=$(az containerapp show -g "$RG" -n "$APP" --query 'properties.template.containers[0].image' -o tsv)
  az identity show -g "$RG" -n verify-probe-id -o none 2>/dev/null || {
    az identity create -g "$RG" -n verify-probe-id -o none
    principal=$(az identity show -g "$RG" -n verify-probe-id --query principalId -o tsv)
    az role assignment create --assignee-object-id "$principal" --assignee-principal-type ServicePrincipal \
      --role AcrPull --scope "$(az acr show -n "$acr" --query id -o tsv)" -o none
    sleep 45   # role assignment propagation
  }
  mi=$(az identity show -g "$RG" -n verify-probe-id --query id -o tsv)
  az container delete -g "$RG" -n "$name" --yes -o none 2>/dev/null || true
  az container create -g "$RG" -n "$name" --image "$image" --os-type Linux --cpu 1 --memory 1 \
    --vnet "$VNET_NAME" --subnet test --restart-policy Never --assign-identity "$mi" --acr-identity "$mi" \
    --environment-variables "MCP_FQDN=$(cat "$STATE/mcp_fqdn")" "ZIA_PROXY=$ZIA_PROXY" \
    --secure-environment-variables "MCP_API_KEY=$(cat "$STATE/mcp_api_key")" \
    --command-line "python -c \"$(sed 's/"/\\"/g' "$code")\"" -o none
  for _ in $(seq 1 36); do
    [[ "$(az container show -g "$RG" -n "$name" --query 'containers[0].instanceView.currentState.state' -o tsv)" == Terminated ]] && break
    sleep 10
  done
  sleep 5
  az container logs -g "$RG" -n "$name"
  az container delete -g "$RG" -n "$name" --yes -o none
}
