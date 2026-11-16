#!/usr/bin/env bash
# 01-create.sh -- create the two storage accounts + container used by the
# rest of this kit. Idempotent: safe to re-run; existing resources are left
# alone (a resource that already exists is reported and skipped, not
# reconfigured -- if you need different settings, run 99-cleanup.sh first).
#
# Creates:
#   - a Standard GPv2 ZRS "live" account, default access tier Smart
#   - a Standard GPv2 LRS "archive" account, default access tier left at
#     its normal default (Hot) -- we only ever set Archive explicitly on
#     individual blobs in this account, per docs/AGENTS.md's two-account
#     design (Smart requires ZRS; Archive is forbidden on ZRS).
#   - one blob container in each account
#
# Both accounts are created with:
#   - HTTPS only, TLS 1.2 minimum
#   - shared key access DISABLED (Entra/RBAC only, matching the product's
#     own "no Azure secrets on the edge" design)
#
# Public network access is left at its default (open) for both accounts.
# Per decision 22 in docs/DECISIONS.md, OmniFile does not create private
# endpoints -- the customer does, following the setup guide -- so this
# script only PRINTS the private-endpoint commands it would take to lock
# these accounts down; it does not run them. Run those yourself (or have
# the client run them) once you're done experimenting and want to confirm
# access still works over a private endpoint.
#
# Required env vars: AZ_SUBSCRIPTION, AZ_RESOURCE_GROUP, AZ_REGION, AZ_NAME_PREFIX
# Optional: AZ_CONTAINER_NAME (default: segy-pilot)

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
source lib/common.sh

MIN_CLI_FOR_SMART="2.87.0"
ACCESS_TIER_SMART_SUPPORTED=false
if version_ge "$AZ_CLI_VERSION" "$MIN_CLI_FOR_SMART"; then
  ACCESS_TIER_SMART_SUPPORTED=true
else
  log "az CLI $AZ_CLI_VERSION is older than $MIN_CLI_FOR_SMART: the native"
  log "--access-tier Smart flag documented at"
  log "https://learn.microsoft.com/azure/storage/blobs/access-tiers-smart#client-tooling"
  log "is not available. Falling back to 'az rest' against ARM to set it after create."
  log "Recommended: 'az upgrade' (or reinstall) to get >= $MIN_CLI_FOR_SMART and avoid this path."
fi

ensure_resource_group() {
  if az group show -n "$AZ_RESOURCE_GROUP" >/dev/null 2>&1; then
    log "resource group '$AZ_RESOURCE_GROUP' already exists"
  else
    log "creating resource group '$AZ_RESOURCE_GROUP' in $AZ_REGION"
    az group create -n "$AZ_RESOURCE_GROUP" -l "$AZ_REGION" >/dev/null
  fi
}

# set_access_tier_smart <account-name>
# VERIFY: the ARM api-version below (2025-08-01) is the minimum REST API
# version Microsoft documents for smart tier as of 2026-09-26
# (https://learn.microsoft.com/azure/storage/blobs/access-tiers-smart#client-tooling).
# If this specific api-version is retired or renamed by the time you run
# this, `az rest` will return a clear 4xx -- bump the api-version string
# below to whatever `az provider show -n Microsoft.Storage --query
# "resourceTypes[?resourceType=='storageAccounts'].apiVersions[0]" -o tsv`
# reports as current, and re-run.
set_access_tier_smart_via_rest() {
  local account="$1"
  log "setting accessTier=Smart on '$account' via az rest (ARM PATCH)"
  az rest --method patch \
    --url "https://management.azure.com/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${AZ_RESOURCE_GROUP}/providers/Microsoft.Storage/storageAccounts/${account}?api-version=2025-08-01" \
    --body '{"properties":{"accessTier":"Smart"}}' \
    --headers "Content-Type=application/json" >/dev/null
}

ensure_live_account() {
  if az storage account show -g "$AZ_RESOURCE_GROUP" -n "$LIVE_ACCOUNT" >/dev/null 2>&1; then
    log "live account '$LIVE_ACCOUNT' already exists, skipping create"
    return
  fi

  log "creating live account '$LIVE_ACCOUNT' (StorageV2, Standard_ZRS)"
  if $ACCESS_TIER_SMART_SUPPORTED; then
    az storage account create \
      -g "$AZ_RESOURCE_GROUP" -n "$LIVE_ACCOUNT" -l "$AZ_REGION" \
      --kind StorageV2 \
      --sku Standard_ZRS \
      --access-tier Smart \
      --https-only true \
      --min-tls-version TLS1_2 \
      --allow-shared-key-access false \
      -o none
  else
    az storage account create \
      -g "$AZ_RESOURCE_GROUP" -n "$LIVE_ACCOUNT" -l "$AZ_REGION" \
      --kind StorageV2 \
      --sku Standard_ZRS \
      --https-only true \
      --min-tls-version TLS1_2 \
      --allow-shared-key-access false \
      -o none
    set_access_tier_smart_via_rest "$LIVE_ACCOUNT"
  fi

  local actual_tier
  actual_tier="$(az storage account show -g "$AZ_RESOURCE_GROUP" -n "$LIVE_ACCOUNT" --query accessTier -o tsv)"
  log "live account default access tier reports: $actual_tier"
  [[ "$actual_tier" == "Smart" ]] || log "WARNING: expected accessTier=Smart, got '$actual_tier' -- check manually"
}

ensure_archive_account() {
  if az storage account show -g "$AZ_RESOURCE_GROUP" -n "$ARCHIVE_ACCOUNT" >/dev/null 2>&1; then
    log "archive account '$ARCHIVE_ACCOUNT' already exists, skipping create"
    return
  fi

  log "creating archive account '$ARCHIVE_ACCOUNT' (StorageV2, Standard_LRS)"
  az storage account create \
    -g "$AZ_RESOURCE_GROUP" -n "$ARCHIVE_ACCOUNT" -l "$AZ_REGION" \
    --kind StorageV2 \
    --sku Standard_LRS \
    --https-only true \
    --min-tls-version TLS1_2 \
    --allow-shared-key-access false \
    -o none
}

ensure_container() {
  local account="$1"
  log "ensuring container '$CONTAINER_NAME' exists in '$account' (auth-mode login)"
  # az storage container create is naturally idempotent: it reports
  # created=false and exits 0 if the container already exists.
  az storage container create \
    --auth-mode login \
    --account-name "$account" \
    --name "$CONTAINER_NAME" \
    -o none
}

print_private_endpoint_commands() {
  cat >&2 <<EOF

-----------------------------------------------------------------------
Private endpoints (decision 22, docs/DECISIONS.md): the CUSTOMER creates
these, in their own VNet, following our setup guide. This script does
NOT run the commands below -- they're printed so you can hand them to
whoever owns the client's networking, or run them yourself in a test
subscription to confirm the setup guide's steps actually work end to end.

For EACH account (substitute \$ACCOUNT, \$VNET, \$SUBNET, \$DNS_RG as needed):

  az network private-endpoint create \\
    --name "pe-\${ACCOUNT}-blob" \\
    --resource-group "$AZ_RESOURCE_GROUP" \\
    --vnet-name "\$VNET" \\
    --subnet "\$SUBNET" \\
    --private-connection-resource-id "/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${AZ_RESOURCE_GROUP}/providers/Microsoft.Storage/storageAccounts/\${ACCOUNT}" \\
    --group-id blob \\
    --connection-name "\${ACCOUNT}-blob-connection"

  az network private-endpoint dns-zone-group create \\
    --resource-group "$AZ_RESOURCE_GROUP" \\
    --endpoint-name "pe-\${ACCOUNT}-blob" \\
    --name default \\
    --private-dns-zone "/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/\${DNS_RG}/providers/Microsoft.Network/privateDnsZones/privatelink.blob.core.windows.net" \\
    --zone-name blob

Once private endpoints are in place and DNS resolves the account names to
their private IPs, --default-action can be set to Deny to close the public
path entirely:

  az storage account update -g "$AZ_RESOURCE_GROUP" -n \${ACCOUNT} --default-action Deny

# VERIFY: confirm the private DNS zone name (privatelink.blob.core.windows.net)
# and the "blob" group-id/zone-name are still current -- these have been
# stable for years but re-check
# https://learn.microsoft.com/azure/private-link/private-endpoint-dns if in doubt.
-----------------------------------------------------------------------
EOF
}

ensure_resource_group
ensure_live_account
ensure_archive_account
ensure_container "$LIVE_ACCOUNT"
ensure_container "$ARCHIVE_ACCOUNT"
print_private_endpoint_commands

log "done. Live account: $LIVE_ACCOUNT   Archive account: $ARCHIVE_ACCOUNT   Container: $CONTAINER_NAME"
