#!/usr/bin/env bash
# Shared helpers for dev/up.sh, down.sh, status.sh. Sourced, not executed.
# shellcheck shell=bash

DEV_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_DIR="$(cd "$DEV_DIR/.." && pwd)"
SECRETS_DIR="$DEV_DIR/.secrets"
AD_ENV="$SECRETS_DIR/ad.env"

# The only instances these scripts ever touch. (Never omnifile-spike.)
# shellcheck disable=SC2034  # used by the scripts that source this file
DC_VM=omnifile-dc
# shellcheck disable=SC2034
EDGE_VM=omnifile-edge

log()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

need_limactl() {
  command -v limactl >/dev/null 2>&1 || die "limactl not found (brew install lima)"
}

# Prints Running, Stopped, or nothing when the instance does not exist.
vm_status() {
  limactl list --format '{{.Status}}' "$1" 2>/dev/null | head -n1 || true
}

# Starts (creating if needed) a dev VM, with the repo mounted at /work.
#   ensure_vm <name> <yaml> <writable: true|false>
ensure_vm() {
  local name="$1" yaml="$2" writable="$3" status
  status="$(vm_status "$name")"
  case "$status" in
    Running)
      log "$name already running" ;;
    Stopped)
      log "starting $name"
      limactl start --tty=false "$name" ;;
    "")
      log "creating and starting $name (first boot downloads the image and installs packages)"
      local repo_json="${REPO_DIR//\\/\\\\}"
      repo_json="${repo_json//\"/\\\"}"
      limactl start --tty=false --name="$name" \
        --set ".mounts = [{\"location\": \"$repo_json\", \"mountPoint\": \"/work\", \"writable\": $writable}]" \
        "$yaml" ;;
    *)
      die "$name is in unexpected state '$status' (check: limactl list)" ;;
  esac
}

# Runs a command inside a VM as root, from /, without Lima's cwd mapping.
vm_root() {
  local name="$1"; shift
  limactl shell --workdir / "$name" -- sudo "$@"
}

# Copies the DC's generated AD settings and passwords to dev/.secrets/ad.env.
fetch_ad_env() {
  mkdir -p "$SECRETS_DIR"
  chmod 700 "$SECRETS_DIR"
  local tmp
  tmp="$(umask 077; mktemp "$SECRETS_DIR/ad.env.XXXXXX")"
  if ! vm_root "$DC_VM" cat /var/lib/omnifile-dev/ad.env >"$tmp"; then
    rm -f "$tmp"; die "could not read AD settings from $DC_VM"
  fi
  if ! grep -q '^AD_DC_IP=' "$tmp" || ! grep -q '^AD_ADMIN_PASSWORD=' "$tmp"; then
    rm -f "$tmp"; die "AD settings from $DC_VM are incomplete"
  fi
  chmod 600 "$tmp"
  mv "$tmp" "$AD_ENV"
}
