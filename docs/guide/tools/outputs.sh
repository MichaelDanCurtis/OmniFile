#!/usr/bin/env bash
# Record the service deployment's outputs for the customer guide's later steps (docs/guide/README.md).
#
#   docs/guide/tools/outputs.sh --resource-group RG [--name omnifile]
#
# Reads the outputs of the deployment (`az deployment group show`, read-only) and records each one the
# guide uses in omnifile.state.env (tools/state.sh), plus pe-targets.json and dns-zones.json for the
# private endpoints (section 6). Run it after every deployment: outputs can change between phases.
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
RG='' NAME=omnifile
while [ $# -gt 0 ]; do
  case "$1" in
    --resource-group) RG=$2; shift ;;
    --name) NAME=$2; shift ;;
    *) echo "usage: outputs.sh --resource-group RG [--name omnifile]" >&2; exit 2 ;;
  esac
  shift
done
[ -n "$RG" ] || { echo "outputs.sh: --resource-group is required" >&2; exit 2; }

out=$(mktemp)
trap 'rm -f "$out"' EXIT
az deployment group show --resource-group "$RG" --name "$NAME" --query properties.outputs --output json >"$out"

# Output name -> the name the guide reads it as.
record() {
  local output=$1 name=$2 value
  value=$(jq -r --arg o "$output" '.[$o].value // empty | if type == "string" then . else tojson end' "$out")
  if [ -z "$value" ]; then
    echo "outputs.sh: the deployment has no output $output (or it is empty)" >&2
    return 1
  fi
  "$HERE/state.sh" set "$name" "$value" >/dev/null
  echo "  $name = $value"
}

record apiUrl OMNI_API_URL
record enrolUrl OMNI_ENROL_URL
record webUrl OMNI_WEB_URL
record relayPublicUrl OMNI_RELAY_URL
record internalEnvironmentDefaultDomain OMNI_INTERNAL_DOMAIN
record internalEnvironmentStaticIp OMNI_INTERNAL_IP
record postgresHost OMNI_PG_HOST
record postgresDatabase OMNI_PG_DATABASE
record keyVaultName OMNI_KV_NAME
record vnetId OMNI_VNET_ID
record privateEndpointSubnetId OMNI_PE_SUBNET_ID
record teamsBotAppId OMNI_BOT_APP_ID

ids=$(jq -c '.identityPrincipalIds.value' "$out")
"$HERE/state.sh" set OMNI_WEB_PRINCIPAL_ID "$(jq -r .web <<<"$ids")" >/dev/null
"$HERE/state.sh" set OMNI_WORKER_PRINCIPAL_ID "$(jq -r .worker <<<"$ids")" >/dev/null
"$HERE/state.sh" set OMNI_API_PRINCIPAL_ID "$(jq -r .api <<<"$ids")" >/dev/null
echo "  OMNI_WEB_PRINCIPAL_ID, OMNI_WORKER_PRINCIPAL_ID, OMNI_API_PRINCIPAL_ID recorded"

accounts=$(jq -c '.storageAccounts.value' "$out")
"$HERE/state.sh" set OMNI_LIVE_ACCOUNT "$(jq -r .live <<<"$accounts")" >/dev/null
"$HERE/state.sh" set OMNI_ARCHIVE_ACCOUNT "$(jq -r .archive <<<"$accounts")" >/dev/null
echo "  OMNI_LIVE_ACCOUNT = $(jq -r .live <<<"$accounts"), OMNI_ARCHIVE_ACCOUNT = $(jq -r .archive <<<"$accounts")"

jq '.privateEndpointTargets.value' "$out" >pe-targets.json
jq '.requiredPrivateDnsZones.value' "$out" >dns-zones.json
echo "  pe-targets.json: $(jq length pe-targets.json) private endpoints to create"
echo "  dns-zones.json: $(jq -r 'join(", ")' dns-zones.json)"
