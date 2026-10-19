#!/usr/bin/env bash
# Foundry step 1 — register the resource providers a Foundry standard agent setup needs. Idempotent.
# Polls instead of `--wait` so a long registration (MachineLearningServices) shows progress.
source "$(dirname "$0")/../azure/_env.sh"

PROVIDERS="Microsoft.KeyVault Microsoft.CognitiveServices Microsoft.Storage Microsoft.MachineLearningServices
           Microsoft.Search Microsoft.Network Microsoft.App Microsoft.ContainerService Microsoft.DocumentDB"
for p in $PROVIDERS; do az provider register -n "$p" >/dev/null 2>&1; done
for i in $(seq 1 60); do
  pending=""
  for p in $PROVIDERS; do
    s=$(az provider show -n "$p" --query registrationState -o tsv)
    [[ "$s" == Registered ]] || pending="$pending $p=$s"
  done
  [[ -z "$pending" ]] && { echo "all providers Registered"; exit 0; }
  echo "$(date -u +%H:%M:%SZ) pending:$pending"; sleep 30
done
echo "still pending after 30 min:$pending"; exit 1
