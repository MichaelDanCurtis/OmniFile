#!/usr/bin/env bash
# M1 exit test on the dev VMs, from the Mac. One command:
#
#   tests/e2e/run.sh            # bring-up + install + M1 exit test
#   tests/e2e/run.sh --test     # M1 test only (everything already installed)
#   tests/e2e/run.sh --m2       # bring-up + install (+ Samba) + M2 exit test
#   tests/e2e/run.sh --m2-test  # M2 test only
#   tests/e2e/run.sh --m3       # bring-up + install (+ Samba) + M3 exit test
#   tests/e2e/run.sh --m3-test  # M3 test only
#   tests/e2e/run.sh --m5       # bring-up + install (+ Samba, + the M5 test's tools) + M5 exit test
#   tests/e2e/run.sh --m5-test  # M5 test only
#
# Steps (each idempotent):
#   1. dev/up.sh                         both VMs (DC first), edge joined
#   2. dev/scripts/build-artifacts.sh    Ganesha + FSAL_OMNI from the fsal/build image (Docker on the Mac)
#   3. install-ganesha.sh                in the edge VM: /opt/ganesha + nfs-ganesha-omni.service
#   4. install-edge.sh                   in the edge VM: builds and installs omnifile-edge + its unit
#   5. install-service.sh                in the edge VM: Postgres 17, sim, enrol, api, worker
#   6. tests/e2e/exit_test.sh            in the edge VM, as root
# Results: tests/e2e/results/latest.md (the VM writes it through the /work mount).
# Stop the VMs afterwards with dev/down.sh.
set -euo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
# shellcheck source=../../dev/scripts/lib.sh
. "$REPO/dev/scripts/lib.sh"

in_edge() { limactl shell --workdir / "$EDGE_VM" -- "$@"; }

MODE=${1:-}
if [ "$MODE" != "--test" ] && [ "$MODE" != "--m2-test" ] && [ "$MODE" != "--m3-test" ] &&
  [ "$MODE" != "--m5-test" ]; then
  log "1/6 VMs"
  "$REPO/dev/up.sh" >/dev/null
  log "2/6 Ganesha + FSAL_OMNI artefacts"
  "$REPO/dev/scripts/build-artifacts.sh"
  log "3/6 install Ganesha in $EDGE_VM"
  in_edge sudo /work/dev/scripts/install-ganesha.sh /work/dev/.artifacts
  log "4/6 build and install omnifile-edge in $EDGE_VM"
  in_edge sudo /work/dev/scripts/install-edge.sh
  log "5/6 service side in $EDGE_VM"
  in_edge sudo /work/dev/scripts/install-service.sh
  if [ "$MODE" = "--m2" ] || [ "$MODE" = "--m3" ] || [ "$MODE" = "--m5" ]; then
    log "5b/6 Samba + Kerberos NFS in $EDGE_VM (M2, M3, M5)"
    in_edge sudo /work/dev/scripts/install-samba.sh
  fi
  if [ "$MODE" = "--m5" ]; then
    log "5c/6 the M5 test's tools in $EDGE_VM (libnfs, nftables, conntrack, vfs_omnifile_prevver)"
    in_edge sudo /work/dev/scripts/install-e2e-tools.sh
  fi
fi
rev="$(git -C "$REPO" describe --always --dirty 2>/dev/null || echo unknown)"
head="$(git -C "$REPO" rev-parse HEAD 2>/dev/null || echo unknown)"
if [ "$MODE" = "--m5" ] || [ "$MODE" = "--m5-test" ]; then
  log "6/6 M5 exit test"
  in_edge sudo env E2E_REV="$rev" /work/tests/e2e/m5_exit_test.sh
elif [ "$MODE" = "--m3" ] || [ "$MODE" = "--m3-test" ]; then
  log "6/6 M3 exit test"
  in_edge sudo env E2E_REV="$rev" E2E_HEAD="$head" /work/tests/e2e/m3_exit_test.sh
elif [ "$MODE" = "--m2" ] || [ "$MODE" = "--m2-test" ]; then
  log "6/6 M2 exit test"
  in_edge sudo env E2E_REV="$rev" E2E_HEAD="$head" /work/tests/e2e/m2_exit_test.sh
else
  log "6/6 exit test"
  in_edge sudo env E2E_REV="$rev" E2E_HEAD="$head" /work/tests/e2e/exit_test.sh
fi
