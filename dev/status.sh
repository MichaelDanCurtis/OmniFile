#!/usr/bin/env bash
# Show the state of the dev VMs and, when running, the AD join.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=scripts/lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/scripts/lib.sh"

need_limactl

printf '%-16s %-10s %s\n' VM STATUS DETAIL
for vm in "$DC_VM" "$EDGE_VM"; do
  status="$(vm_status "$vm")"
  detail=""
  if [ "$status" = Running ]; then
    ip="$(limactl shell --workdir / "$vm" -- ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | head -n1 || true)"
    detail="ip=${ip:-?}"
    case "$vm" in
      "$DC_VM")
        svc="$(limactl shell --workdir / "$vm" -- systemctl is-active samba-ad-dc 2>/dev/null || true)"
        detail="$detail samba-ad-dc=$svc" ;;
      "$EDGE_VM")
        if limactl shell --workdir / "$vm" -- sudo wbinfo -t >/dev/null 2>&1; then
          detail="$detail domain-trust=ok"
        else
          detail="$detail domain-trust=FAILED"
        fi
        detail="$detail data=$(limactl shell --workdir / "$vm" -- findmnt -no FSTYPE /srv/omnifile/data 2>/dev/null || echo unmounted)" ;;
    esac
  fi
  printf '%-16s %-10s %s\n' "$vm" "${status:-absent}" "$detail"
done
if [ -f "$AD_ENV" ]; then echo "secrets: dev/.secrets/ad.env present"; else echo "secrets: dev/.secrets/ad.env missing (run dev/up.sh)"; fi
