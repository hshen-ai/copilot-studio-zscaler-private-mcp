# Sourced by the azure/ and verify/ shell scripts. Loads config/demo.env (and config/secrets.env when
# the caller sets NEED_SECRETS=1), selects the subscription, and defines the state directory.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE="$ROOT/state"
mkdir -p "$STATE"; chmod 700 "$STATE"
[[ -f "$ROOT/config/demo.env" ]] || { echo "missing config/demo.env — copy config/demo.env.example (README, Setup)"; exit 2; }
set -a
# shellcheck disable=SC1091
source "$ROOT/config/demo.env"
if [[ "${NEED_SECRETS:-0}" == 1 ]]; then
  [[ -f "$ROOT/config/secrets.env" ]] || { echo "missing config/secrets.env"; exit 2; }
  # shellcheck disable=SC1091
  source "$ROOT/config/secrets.env"
fi
set +a
[[ -n "${CA_BUNDLE:-}" ]] && export REQUESTS_CA_BUNDLE="$CA_BUNDLE"
case "$AZ_SUBSCRIPTION_ID$TENANT_ID" in *xxxx*) echo "config/demo.env still contains masked example values"; exit 2;; esac
az account set --subscription "$AZ_SUBSCRIPTION_ID"
RG="$AZ_RESOURCE_GROUP"
APP=websearch-mcp
ACA_ENV=websearch-mcp-env
