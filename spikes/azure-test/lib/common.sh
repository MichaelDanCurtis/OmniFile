#!/usr/bin/env bash
# Shared setup for the azure-test (pilot 4, storage part) scripts.
# Source this from every script: `source "$(dirname "$0")/lib/common.sh"`
#
# All scripts in this kit are Entra-auth-only (--auth-mode login everywhere).
# No account keys, connection strings, or secrets are ever written to disk
# or printed. Nothing in this kit reads or writes AWS credentials or talks
# to AWS; it only covers the Azure storage half of pilot 4 (see README.md
# for the manual Teams/identity checks and what's out of scope here).

set -euo pipefail

# ---------------------------------------------------------------------------
# Required parameters (env vars). No defaults for these -- fail loudly.
# ---------------------------------------------------------------------------
: "${AZ_SUBSCRIPTION:?Set AZ_SUBSCRIPTION to a subscription id or name}"
: "${AZ_RESOURCE_GROUP:?Set AZ_RESOURCE_GROUP, e.g. rg-omnifile-pilot}"
: "${AZ_REGION:?Set AZ_REGION, e.g. eastus2 (must support Smart tier + ZRS)}"
: "${AZ_NAME_PREFIX:?Set AZ_NAME_PREFIX: lowercase letters/digits only, <=18 chars}"

# ---------------------------------------------------------------------------
# Optional parameters (env vars with defaults).
# ---------------------------------------------------------------------------
CONTAINER_NAME="${AZ_CONTAINER_NAME:-segy-pilot}"
STATE_DIR="${AZ_TEST_STATE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.state}"

# ---------------------------------------------------------------------------
# Validate AZ_NAME_PREFIX up front: storage account names must be 3-24 chars,
# lowercase letters and digits only, globally unique. We append up to 5
# characters ("-live"/"-arch" without the dash, since dashes aren't allowed:
# "live"/"arch"), so cap the prefix at 18 to stay under 24 with margin for
# uniqueness suffixes if the caller adds one.
# ---------------------------------------------------------------------------
if ! [[ "$AZ_NAME_PREFIX" =~ ^[a-z0-9]{1,18}$ ]]; then
  echo "ERROR: AZ_NAME_PREFIX must be 1-18 lowercase letters/digits only (got: $AZ_NAME_PREFIX)" >&2
  exit 1
fi

LIVE_ACCOUNT="${AZ_NAME_PREFIX}live"
ARCHIVE_ACCOUNT="${AZ_NAME_PREFIX}arch"

mkdir -p "$STATE_DIR"

log() {
  printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >&2
}

die() {
  log "ERROR: $*"
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"
}

# Portable epoch-seconds-with-fraction timer (GNU/BSD date both support %s;
# %N is GNU-only, so fall back to whole seconds on macOS/BSD date).
now_s() {
  if date +%s.%N >/dev/null 2>&1 && [[ "$(date +%N)" != "N" ]]; then
    date +%s.%N
  else
    date +%s
  fi
}

elapsed_s() {
  # elapsed_s <start> <end> -- prints a fixed-point seconds value using awk
  # (avoids bc, which isn't guaranteed to be installed).
  awk -v s="$1" -v e="$2" 'BEGIN { printf "%.3f", (e - s) }'
}

# Compare two dotted version strings: returns 0 (true) if $1 >= $2.
version_ge() {
  [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -n1)" = "$2" ]
}

require_cmd az
require_cmd awk
require_cmd date

log "Setting active subscription to: $AZ_SUBSCRIPTION"
az account set --subscription "$AZ_SUBSCRIPTION"

AZ_CLI_VERSION="$(az version --query '"azure-cli"' -o tsv)"
log "az CLI version: $AZ_CLI_VERSION"

# shellcheck disable=SC2034  # used by the scripts that source this file
SUBSCRIPTION_ID="$(az account show --query id -o tsv)"

log "Config: resource group=$AZ_RESOURCE_GROUP region=$AZ_REGION live-account=$LIVE_ACCOUNT archive-account=$ARCHIVE_ACCOUNT container=$CONTAINER_NAME"
