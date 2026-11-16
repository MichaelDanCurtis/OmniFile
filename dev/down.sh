#!/usr/bin/env bash
# Stop the dev VMs (edge first, then the DC). State is kept.
#   dev/down.sh            stop both
#   dev/down.sh --destroy  stop and DELETE both VMs and dev/.secrets/ad.env
# Never touches the filer-spike VM (omnifile-spike).
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=scripts/lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/scripts/lib.sh"

need_limactl

destroy=false
case "${1:-}" in
  "") ;;
  --destroy) destroy=true ;;
  *) die "usage: dev/down.sh [--destroy]" ;;
esac

for vm in "$EDGE_VM" "$DC_VM"; do
  status="$(vm_status "$vm")"
  if [ "$status" = Running ]; then
    log "stopping $vm"
    limactl stop "$vm"
  elif [ -n "$status" ]; then
    log "$vm already stopped"
  fi
  if $destroy && [ -n "$status" ]; then
    log "deleting $vm"
    limactl delete --force "$vm"
  fi
done

if $destroy; then
  rm -f "$AD_ENV"
  log "removed dev/.secrets/ad.env"
fi
