#!/usr/bin/env bash
# 02-smart-to-archive.sh -- pilot 4's first storage question:
#   1. Upload with NO explicit tier into the Smart-default live account,
#      and confirm the blob is Smart-managed rather than pinned to a
#      classic tier.
#   2. Server-side copy it into the archive account with tier Archive
#      (this is the "smart -> archive on the client's schedule" step the
#      real service will do), and record whether the cross-account copy
#      source needed a SAS or worked with plain Entra/RBAC auth.
#   3. Verify the copy, then delete the original from the live account.
#
# Every step is timed; timings and the SAS-vs-Entra outcome are appended
# to a CSV so multiple runs (e.g. different file sizes) accumulate in one
# place. Idempotent in the sense that re-running overwrites the same test
# blob name and produces a fresh copy -- it does not try to detect and
# skip a prior run's *result*, since the point is to re-measure.
#
# Required env vars: AZ_SUBSCRIPTION, AZ_RESOURCE_GROUP, AZ_REGION, AZ_NAME_PREFIX
# Optional: AZ_CONTAINER_NAME, AZ_BLOB_NAME (default: smart-archive-test.bin),
#           TEST_FILE_PATH (default: a generated 1 MiB random file -- pass
#           your own SEG-Y sample here for a more realistic run),
#           SAS_EXPIRY_MINUTES (default: 30)

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
source lib/common.sh

BLOB_NAME="${AZ_BLOB_NAME:-smart-archive-test.bin}"
TEST_FILE_PATH="${TEST_FILE_PATH:-}"
SAS_EXPIRY_MINUTES="${SAS_EXPIRY_MINUTES:-30}"
CSV_PATH="$STATE_DIR/02-smart-to-archive-timings.csv"

if [[ -z "$TEST_FILE_PATH" ]]; then
  TEST_FILE_PATH="$STATE_DIR/generated-test-file.bin"
  if [[ ! -f "$TEST_FILE_PATH" ]]; then
    log "no TEST_FILE_PATH given: generating a 1 MiB random test file at $TEST_FILE_PATH"
    log "(pass TEST_FILE_PATH=/path/to/real.segy for a representative run)"
    dd if=/dev/urandom of="$TEST_FILE_PATH" bs=1M count=1 status=none
  else
    log "reusing previously generated test file at $TEST_FILE_PATH"
  fi
fi

FILE_SIZE_BYTES="$(wc -c < "$TEST_FILE_PATH" | tr -d ' ')"
log "test file: $TEST_FILE_PATH ($FILE_SIZE_BYTES bytes)"

if [[ ! -f "$CSV_PATH" ]]; then
  echo "timestamp_utc,file_size_bytes,upload_seconds,tier_after_upload,copy_auth_path,copy_start_seconds,copy_wait_seconds,copy_status,archive_tier_confirmed,delete_seconds" > "$CSV_PATH"
fi

# --- 1. Upload with no explicit tier ---------------------------------------
log "uploading '$BLOB_NAME' to live account '$LIVE_ACCOUNT' with NO --tier flag"
t0="$(now_s)"
az storage blob upload \
  --auth-mode login \
  --account-name "$LIVE_ACCOUNT" \
  --container-name "$CONTAINER_NAME" \
  --name "$BLOB_NAME" \
  --file "$TEST_FILE_PATH" \
  --overwrite \
  -o none
t1="$(now_s)"
UPLOAD_SECONDS="$(elapsed_s "$t0" "$t1")"
log "upload took ${UPLOAD_SECONDS}s"

# --- 2. Confirm the blob is Smart-managed -----------------------------------
# VERIFY: `properties.blobTier` on an individual blob is expected to report
# the underlying CAPACITY tier (e.g. "Hot") even when the account's default
# access tier is Smart -- Microsoft's docs describe Smart-tier categorisation
# (SmartHot/SmartCool/SmartCold/SmartHot-small) as a METRICS-namespace
# concept (Storage account > Monitoring > Metrics, split by "Blob tier"),
# not a per-blob REST/CLI property. If the query below shows a plain tier
# name with no separate "is this blob under Smart management" flag, that is
# expected -- cross-check in the Portal's Metrics blade (Blob Count/Blob
# Capacity, split by Blob tier) and note what you see in what you send back.
TIER_JSON="$(az storage blob show \
  --auth-mode login \
  --account-name "$LIVE_ACCOUNT" \
  --container-name "$CONTAINER_NAME" \
  --name "$BLOB_NAME" \
  --query "{tier:properties.blobTier, tierInferred:properties.accessTierInferred, lastModified:properties.lastModified}" \
  -o json)"
log "blob properties after upload: $TIER_JSON"
TIER_AFTER_UPLOAD="$(az storage blob show \
  --auth-mode login \
  --account-name "$LIVE_ACCOUNT" \
  --container-name "$CONTAINER_NAME" \
  --name "$BLOB_NAME" \
  --query "properties.blobTier" -o tsv)"

STORAGE_ACCOUNT_DEFAULT_TIER="$(az storage account show -g "$AZ_RESOURCE_GROUP" -n "$LIVE_ACCOUNT" --query accessTier -o tsv)"
log "live account's default access tier is: $STORAGE_ACCOUNT_DEFAULT_TIER (blob has no explicit tier, so it is Smart-managed if this says Smart)"

# --- 3. Server-side copy into the archive account with tier Archive --------
# First attempt: pure Entra/RBAC, no SAS. Our research pass
# (docs/research/2026-09-26-verification.md is the place to record this)
# found that `az storage blob copy start` documents the cross-account
# --source-uri restriction as "must be public or authenticated via SAS",
# and community reports (Microsoft Q&A, AzCopy docs) say the same applies
# to the --source-account-name/--source-container/--source-blob form: it
# does not set the newer x-ms-copy-source-authorization bearer header, so
# plain Entra RBAC on the source account is NOT expected to be enough for
# az storage blob copy start specifically (AzCopy is the tool that does
# set that header). We still try Entra-only first here so a "no, it needs
# a SAS" is a positive, evidenced result rather than an assumption.
log "attempting server-side copy to archive account with auth-mode login only (no SAS)"
COPY_AUTH_PATH="entra-only"
t2="$(now_s)"
if az storage blob copy start \
  --auth-mode login \
  --account-name "$ARCHIVE_ACCOUNT" \
  --destination-container "$CONTAINER_NAME" \
  --destination-blob "$BLOB_NAME" \
  --source-account-name "$LIVE_ACCOUNT" \
  --source-container "$CONTAINER_NAME" \
  --source-blob "$BLOB_NAME" \
  --tier Archive \
  -o none 2>"$STATE_DIR/02-copy-start-entra-only.err"; then
  log "Entra-only cross-account copy start succeeded (no SAS needed)"
else
  log "Entra-only cross-account copy start failed as expected (see $STATE_DIR/02-copy-start-entra-only.err); falling back to a short-lived user-delegation SAS on the source blob"
  COPY_AUTH_PATH="user-delegation-sas"

  expiry="$(date -u -d "+${SAS_EXPIRY_MINUTES} minutes" '+%Y-%m-%dT%H:%MZ' 2>/dev/null || date -u -v"+${SAS_EXPIRY_MINUTES}M" '+%Y-%m-%dT%H:%MZ')"
  # --as-user requires --auth-mode login and Storage Blob Data Owner (or
  # equivalent delegation-key read rights) on the caller for the SOURCE
  # (live) account. Grant that role ahead of time -- see README.md.
  SOURCE_SAS="$(az storage blob generate-sas \
    --auth-mode login --as-user \
    --account-name "$LIVE_ACCOUNT" \
    --container-name "$CONTAINER_NAME" \
    --name "$BLOB_NAME" \
    --permissions r \
    --expiry "$expiry" \
    --https-only \
    -o tsv)"

  az storage blob copy start \
    --auth-mode login \
    --account-name "$ARCHIVE_ACCOUNT" \
    --destination-container "$CONTAINER_NAME" \
    --destination-blob "$BLOB_NAME" \
    --source-account-name "$LIVE_ACCOUNT" \
    --source-container "$CONTAINER_NAME" \
    --source-blob "$BLOB_NAME" \
    --source-sas "$SOURCE_SAS" \
    --tier Archive \
    -o none
  log "user-delegation-SAS cross-account copy start succeeded"
fi
t3="$(now_s)"
COPY_START_SECONDS="$(elapsed_s "$t2" "$t3")"

# --- Poll the copy to completion --------------------------------------------
log "polling copy status..."
t4="$(now_s)"
COPY_STATUS="pending"
for _ in $(seq 1 120); do  # up to ~10 minutes at 5s intervals; this is a
                            # small test file, so a real run should finish
                            # in seconds, not the full 10 minutes.
  COPY_STATUS="$(az storage blob show \
    --auth-mode login \
    --account-name "$ARCHIVE_ACCOUNT" \
    --container-name "$CONTAINER_NAME" \
    --name "$BLOB_NAME" \
    --query "properties.copy.status" -o tsv 2>/dev/null || echo "unknown")"
  COPY_PROGRESS="$(az storage blob show \
    --auth-mode login \
    --account-name "$ARCHIVE_ACCOUNT" \
    --container-name "$CONTAINER_NAME" \
    --name "$BLOB_NAME" \
    --query "properties.copy.progress" -o tsv 2>/dev/null || echo "?")"
  log "copy status: $COPY_STATUS ($COPY_PROGRESS)"
  [[ "$COPY_STATUS" == "success" || "$COPY_STATUS" == "failed" || "$COPY_STATUS" == "aborted" ]] && break
  sleep 5
done
t5="$(now_s)"
COPY_WAIT_SECONDS="$(elapsed_s "$t4" "$t5")"

if [[ "$COPY_STATUS" != "success" ]]; then
  die "copy did not reach 'success' (last status: $COPY_STATUS) -- see $STATE_DIR for logs, and check the destination blob's properties.copyStatusDescription for the reason"
fi

# --- Verify the destination tier is Archive ---------------------------------
DEST_TIER="$(az storage blob show \
  --auth-mode login \
  --account-name "$ARCHIVE_ACCOUNT" \
  --container-name "$CONTAINER_NAME" \
  --name "$BLOB_NAME" \
  --query "properties.blobTier" -o tsv)"
log "destination blob tier: $DEST_TIER"
ARCHIVE_TIER_CONFIRMED="false"
[[ "$DEST_TIER" == "Archive" ]] && ARCHIVE_TIER_CONFIRMED="true"
[[ "$ARCHIVE_TIER_CONFIRMED" == "true" ]] || log "WARNING: destination tier is '$DEST_TIER', not 'Archive' -- investigate before trusting the recall test in 03-recall.sh"

# --- Delete the original from the live account ------------------------------
log "deleting original blob from live account (this avoids the 180-day early-deletion trap -- we're deleting the LIVE copy, not the archived one)"
t6="$(now_s)"
az storage blob delete \
  --auth-mode login \
  --account-name "$LIVE_ACCOUNT" \
  --container-name "$CONTAINER_NAME" \
  --name "$BLOB_NAME" \
  -o none
t7="$(now_s)"
DELETE_SECONDS="$(elapsed_s "$t6" "$t7")"

echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ'),$FILE_SIZE_BYTES,$UPLOAD_SECONDS,$TIER_AFTER_UPLOAD,$COPY_AUTH_PATH,$COPY_START_SECONDS,$COPY_WAIT_SECONDS,$COPY_STATUS,$ARCHIVE_TIER_CONFIRMED,$DELETE_SECONDS" >> "$CSV_PATH"

log "done. Summary row appended to $CSV_PATH:"
tail -n1 "$CSV_PATH" >&2
log "IMPORTANT: send us $CSV_PATH plus the note on whether $STORAGE_ACCOUNT_DEFAULT_TIER matched 'Smart' and what the Portal Metrics blade showed for this blob's tier."
