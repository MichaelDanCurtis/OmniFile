#!/usr/bin/env bash
# 99-cleanup.sh -- tear down everything 01-create.sh made. Idempotent:
# each step no-ops if the resource is already gone.
#
# COST WARNING (read before running): if any blob currently sitting in the
# archive account is younger than 180 days (Archive's minimum duration,
# per docs/AGENTS.md), deleting its account/container/blob now bills an
# early-deletion fee prorated for the remaining days, same as it would in
# production. For a short pilot this is normally a few files at most, so
# the fee is small, but it is not zero -- this script does not skip or
# warn per-blob, it just tells you here, once, up front.
#
# Deletes, in order: both containers' contents is implied by deleting the
# storage accounts themselves (deleting the account is simpler and just as
# complete as emptying containers first). The resource group is only
# deleted if AZ_DELETE_RESOURCE_GROUP=true is set, since it may contain
# other things you don't want gone (e.g. if you reused an existing group).
#
# Required env vars: AZ_SUBSCRIPTION, AZ_RESOURCE_GROUP, AZ_REGION, AZ_NAME_PREFIX
# Optional: AZ_DELETE_RESOURCE_GROUP=true to also delete the whole resource
#           group (default: false -- only the two storage accounts are removed)
# Optional: AZ_YES=true to skip the interactive confirmation prompt (for
#           unattended/CI use; still prints the cost warning first)

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
source lib/common.sh

AZ_DELETE_RESOURCE_GROUP="${AZ_DELETE_RESOURCE_GROUP:-false}"
AZ_YES="${AZ_YES:-false}"

log "About to delete:"
log "  - storage account: $LIVE_ACCOUNT (and everything in it)"
log "  - storage account: $ARCHIVE_ACCOUNT (and everything in it -- see the"
log "    early-deletion cost warning at the top of this script)"
if [[ "$AZ_DELETE_RESOURCE_GROUP" == "true" ]]; then
  log "  - the ENTIRE resource group: $AZ_RESOURCE_GROUP"
fi

if [[ "$AZ_YES" != "true" ]]; then
  read -r -p "Type 'yes' to proceed: " confirm
  [[ "$confirm" == "yes" ]] || { log "aborted, nothing deleted"; exit 1; }
fi

delete_account_if_exists() {
  local account="$1"
  if az storage account show -g "$AZ_RESOURCE_GROUP" -n "$account" >/dev/null 2>&1; then
    log "deleting storage account '$account'"
    az storage account delete -g "$AZ_RESOURCE_GROUP" -n "$account" --yes -o none
  else
    log "storage account '$account' does not exist, skipping"
  fi
}

if [[ "$AZ_DELETE_RESOURCE_GROUP" == "true" ]]; then
  if az group show -n "$AZ_RESOURCE_GROUP" >/dev/null 2>&1; then
    log "deleting entire resource group '$AZ_RESOURCE_GROUP' (this also removes both storage accounts)"
    az group delete -n "$AZ_RESOURCE_GROUP" --yes -o none
  else
    log "resource group '$AZ_RESOURCE_GROUP' does not exist, skipping"
  fi
else
  delete_account_if_exists "$LIVE_ACCOUNT"
  delete_account_if_exists "$ARCHIVE_ACCOUNT"
fi

log "cleaning up local state directory: $STATE_DIR"
log "(this only removes locally generated test files and CSV logs -- not"
log "anything in Azure -- delete it by hand if you want to keep the CSVs"
log "you're about to send back)"
if [[ "${AZ_CLEAN_LOCAL_STATE:-false}" == "true" ]]; then
  rm -rf "$STATE_DIR"
  log "removed $STATE_DIR"
else
  log "leaving $STATE_DIR in place (set AZ_CLEAN_LOCAL_STATE=true to remove it too)"
fi

log "done."
