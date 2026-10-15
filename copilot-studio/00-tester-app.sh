#!/usr/bin/env bash
# Optional — the app registration used by verify/04-agent-e2e.py to talk to the agent AS A SIGNED-IN
# USER through the Copilot Studio conversation API (device-code sign-in, no secret).
#
# Public client, delegated permission CopilotStudio.Copilots.Invoke on the Power Platform API, admin
# consent. Prints the appId — put it into TESTER_CLIENT_ID in config/demo.env. Idempotent.
source "$(dirname "$0")/../azure/_env.sh"
PPAPI=8578e004-a5c6-46e7-913e-12f58912df43   # "Power Platform API" (Microsoft first-party appId)
NAME="Copilot Studio agent tester"

az ad sp show --id "$PPAPI" -o none 2>/dev/null || az ad sp create --id "$PPAPI" -o none
SCOPE=$(az ad sp show --id "$PPAPI" --query "oauth2PermissionScopes[?value=='CopilotStudio.Copilots.Invoke'].id | [0]" -o tsv)
APPID=$(az ad app list --display-name "$NAME" --query '[0].appId' -o tsv)
if [[ -z "$APPID" ]]; then
  APPID=$(az ad app create --display-name "$NAME" --sign-in-audience AzureADMyOrg --is-fallback-public-client true --query appId -o tsv)
  az ad app permission add --id "$APPID" --api "$PPAPI" --api-permissions "$SCOPE=Scope" -o none
fi
az ad sp show --id "$APPID" -o none 2>/dev/null || az ad sp create --id "$APPID" -o none
az ad app permission admin-consent --id "$APPID"
az ad app permission list-grants --id "$APPID" --query '[].{scope:scope,consent:consentType}' -o table
echo "TESTER_CLIENT_ID=$APPID"
