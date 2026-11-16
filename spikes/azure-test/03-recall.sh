#!/usr/bin/env bash
# 03-recall.sh -- pilot 4's recall-timing question: rehydrate an
# Archive-tier blob by SERVER-SIDE COPY (never Set Blob Tier -- that would
# risk the 180-day early-deletion fee on the archived copy, per
# docs/AGENTS.md) into a Hot blob in the live account, at a given
# rehydrate priority, and record how long it actually takes.
#
# docs/PILOTS.md asks us to recall 8 GB, 184 GB and 1.2 TB files. Generating
# synthetic files of that size just to throw them away is expensive disk
# and archive-storage churn for no signal (the recall path doesn't care
# about content, only size and tier), so this script does NOT generate
# data. Instead, it takes an EXISTING Archive-tier blob name as a
# parameter and times its recall. Use the client's own SEG-Y files at the
# three sizes docs/PILOTS.md asks for: upload each one, run
# 02-smart-to-archive.sh's copy-to-Archive step against it (or archive it
# by whatever real path you're testing), then point this script at it.
#
# This script NEVER prints or implies a promised completion time. It only
# reports what was measured. Per docs/AGENTS.md: Microsoft's "up to 15 h"
# figure applies only under 10 GB, and priority (High) retrieval is capped
# at 10 GiB/hour PER STORAGE ACCOUNT -- shared across every recall running
# against that account at the same time, including this test's own runs if
# you fire off more than one concurrently. Don't promise a time above 10 GB,
# and don't expect High priority to be faster than Standard once that cap
# is the bottleneck rather than Microsoft's internal scheduling.
#
# Costs to expect (see docs/AGENTS.md and Azure's published pricing --
# confirm current numbers before reporting them to the client, they change):
#   - Data retrieval from Archive is billed per GB read, regardless of
#     priority (High priority is a scheduling premium, not a bandwidth
#     product).
#   - While the recall is in flight, and until you decide to delete the
#     original, you are billed for BOTH the Archive-tier original and the
#     new Hot-tier copy -- this script does not delete the Archive
#     original automatically (unlike 02-smart-to-archive.sh's live-account
#     cleanup), specifically so you keep it for a possible second timed
#     run instead of having to re-upload and re-archive at the top of
#     every test.
#   - No early-deletion fee is triggered by this recall: a server-side
#     copy leaves the Archive blob in place, so its 180-day minimum
#     duration clock is undisturbed. The fee only applies if you delete or
#     move (Set Blob Tier) the Archive blob itself before that clock runs
#     out -- see 99-cleanup.sh's comments before tearing accounts down
#     early in a real (non-pilot) engagement.
#
# Usage:
#   03-recall.sh <archive-blob-name> <Standard|High> [destination-blob-name]
#
# <archive-blob-name>     must already exist, tier Archive, in the archive
#                          account's container (see AZ_CONTAINER_NAME).
# destination-blob-name   defaults to "<archive-blob-name>.recalled"
#
# Required env vars: AZ_SUBSCRIPTION, AZ_RESOURCE_GROUP, AZ_REGION, AZ_NAME_PREFIX
# Optional: AZ_CONTAINER_NAME, POLL_SECONDS (default 600 = 10 min, per
#           docs/PILOTS.md's "poll every 10 min")

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
source lib/common.sh

usage() {
  echo "usage: $0 <archive-blob-name> <Standard|High> [destination-blob-name]" >&2
  exit 1
}

[[ $# -ge 2 ]] || usage
SOURCE_BLOB="$1"
PRIORITY="$2"
DEST_BLOB="${3:-${SOURCE_BLOB}.recalled}"
POLL_SECONDS="${POLL_SECONDS:-600}"

case "$PRIORITY" in
  Standard|High) ;;
  *) echo "priority must be 'Standard' or 'High' (got: $PRIORITY)" >&2; exit 1 ;;
esac

CSV_PATH="$STATE_DIR/03-recall-timings.csv"
if [[ ! -f "$CSV_PATH" ]]; then
  echo "run_id,timestamp_utc,source_blob,size_bytes,priority,elapsed_seconds,status,progress" > "$CSV_PATH"
fi
RUN_ID="$(date -u '+%Y%m%dT%H%M%SZ')-${PRIORITY}"

log "recalling '$SOURCE_BLOB' (priority=$PRIORITY) from archive account '$ARCHIVE_ACCOUNT' into live account '$LIVE_ACCOUNT' as '$DEST_BLOB'"

SIZE_BYTES="$(az storage blob show \
  --auth-mode login \
  --account-name "$ARCHIVE_ACCOUNT" \
  --container-name "$CONTAINER_NAME" \
  --name "$SOURCE_BLOB" \
  --query "properties.contentLength" -o tsv)" || die "source blob '$SOURCE_BLOB' not found in $ARCHIVE_ACCOUNT/$CONTAINER_NAME"

SOURCE_TIER="$(az storage blob show \
  --auth-mode login \
  --account-name "$ARCHIVE_ACCOUNT" \
  --container-name "$CONTAINER_NAME" \
  --name "$SOURCE_BLOB" \
  --query "properties.blobTier" -o tsv)"
[[ "$SOURCE_TIER" == "Archive" ]] || log "WARNING: source blob tier is '$SOURCE_TIER', not 'Archive' -- this won't exercise a real rehydrate"

log "source size: $SIZE_BYTES bytes, tier: $SOURCE_TIER"
log "reminder: at $PRIORITY priority this storage account is capped at 10 GiB/hour of priority retrieval, shared with any other recall in flight against it right now"

# --- Start the rehydrate-by-copy --------------------------------------------
# Same SAS-vs-Entra question as 02-smart-to-archive.sh: try Entra-only
# first, fall back to a user-delegation SAS on the source (archive) blob.
# See that script's comments for why the fallback is expected to be needed.
log "recall started at $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
t0="$(now_s)"
COPY_AUTH_PATH="entra-only"
if az storage blob copy start \
  --auth-mode login \
  --account-name "$LIVE_ACCOUNT" \
  --destination-container "$CONTAINER_NAME" \
  --destination-blob "$DEST_BLOB" \
  --source-account-name "$ARCHIVE_ACCOUNT" \
  --source-container "$CONTAINER_NAME" \
  --source-blob "$SOURCE_BLOB" \
  --tier Hot \
  --rehydrate-priority "$PRIORITY" \
  -o none 2>"$STATE_DIR/03-copy-start-entra-only.err"; then
  log "Entra-only cross-account copy start succeeded (no SAS needed)"
else
  log "Entra-only cross-account copy start failed as expected; falling back to a user-delegation SAS on the source (archive) blob"
  COPY_AUTH_PATH="user-delegation-sas"
  expiry="$(date -u -d "+60 minutes" '+%Y-%m-%dT%H:%MZ' 2>/dev/null || date -u -v+60M '+%Y-%m-%dT%H:%MZ')"
  SOURCE_SAS="$(az storage blob generate-sas \
    --auth-mode login --as-user \
    --account-name "$ARCHIVE_ACCOUNT" \
    --container-name "$CONTAINER_NAME" \
    --name "$SOURCE_BLOB" \
    --permissions r \
    --expiry "$expiry" \
    --https-only \
    -o tsv)"
  az storage blob copy start \
    --auth-mode login \
    --account-name "$LIVE_ACCOUNT" \
    --destination-container "$CONTAINER_NAME" \
    --destination-blob "$DEST_BLOB" \
    --source-account-name "$ARCHIVE_ACCOUNT" \
    --source-container "$CONTAINER_NAME" \
    --source-blob "$SOURCE_BLOB" \
    --source-sas "$SOURCE_SAS" \
    --tier Hot \
    --rehydrate-priority "$PRIORITY" \
    -o none
fi
log "copy auth path used: $COPY_AUTH_PATH"

# --- Poll every POLL_SECONDS (default 10 min) until done --------------------
# No upper bound on iterations here on purpose: docs/AGENTS.md is explicit
# that we never promise a completion time, so this script waits as long as
# it takes and just keeps recording. Ctrl-C is safe -- the copy keeps
# running server-side; re-run with the same source/dest names to resume
# polling (az storage blob show will reflect the copy's real state).
STATUS="pending"
while true; do
  now_ts="$(now_s)"
  elapsed="$(elapsed_s "$t0" "$now_ts")"
  STATUS="$(az storage blob show \
    --auth-mode login \
    --account-name "$LIVE_ACCOUNT" \
    --container-name "$CONTAINER_NAME" \
    --name "$DEST_BLOB" \
    --query "properties.copy.status" -o tsv 2>/dev/null || echo "unknown")"
  PROGRESS="$(az storage blob show \
    --auth-mode login \
    --account-name "$LIVE_ACCOUNT" \
    --container-name "$CONTAINER_NAME" \
    --name "$DEST_BLOB" \
    --query "properties.copy.progress" -o tsv 2>/dev/null || echo "?")"
  row_ts="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  log "[$row_ts] status=$STATUS progress=$PROGRESS elapsed=${elapsed}s"
  echo "$RUN_ID,$row_ts,$SOURCE_BLOB,$SIZE_BYTES,$PRIORITY,$elapsed,$STATUS,$PROGRESS" >> "$CSV_PATH"

  [[ "$STATUS" == "success" || "$STATUS" == "failed" || "$STATUS" == "aborted" ]] && break
  sleep "$POLL_SECONDS"
done

t1="$(now_s)"
TOTAL_SECONDS="$(elapsed_s "$t0" "$t1")"

if [[ "$STATUS" == "success" ]]; then
  log "recall finished. Measured wall-clock time: ${TOTAL_SECONDS}s for $SIZE_BYTES bytes at $PRIORITY priority."
  log "That is a MEASUREMENT for this run, on this account, at this time -- not a number to promise a client for a different file, account, or moment."
else
  log "recall did NOT finish successfully (final status: $STATUS). Check properties.copyStatusDescription on '$DEST_BLOB' in $LIVE_ACCOUNT for the reason."
fi

log "full timing history for every poll of this run: $CSV_PATH (grep for run_id=$RUN_ID)"
log "the archived source blob '$SOURCE_BLOB' was left in place in $ARCHIVE_ACCOUNT -- delete it yourself once you're done re-testing against it, or via 99-cleanup.sh"
