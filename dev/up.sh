#!/usr/bin/env bash
# One command to bring up the OmniFile dev environment, in order:
#   1. omnifile-dc   Samba AD domain controller (OMNI.TEST) with seed users/groups
#   2. omnifile-edge Ubuntu 26.04 edge VM, joined to that domain
# Idempotent: re-running starts anything stopped and re-asserts provisioning.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=scripts/lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/scripts/lib.sh"

need_limactl

ensure_vm "$DC_VM" "$DEV_DIR/lima/omnifile-dc.yaml" false
log "provisioning $DC_VM (idempotent)"
vm_root "$DC_VM" /work/dev/scripts/dc-provision.sh
log "fetching AD settings to dev/.secrets/ad.env"
fetch_ad_env

ensure_vm "$EDGE_VM" "$DEV_DIR/lima/omnifile-edge.yaml" true
log "provisioning $EDGE_VM (idempotent; joins the domain)"
vm_root "$EDGE_VM" /work/dev/scripts/edge-provision.sh

echo
"$DEV_DIR/status.sh"
echo
echo "Shells:  limactl shell $DC_VM   |   limactl shell $EDGE_VM"
echo "Secrets: dev/.secrets/ad.env (git-ignored)"
