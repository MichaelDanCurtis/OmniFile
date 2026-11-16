#!/bin/bash
# SPDX-License-Identifier: LGPL-3.0-or-later
#
# Build and test FSAL_OMNI in the pinned Ganesha container. Used by CI
# (.github/workflows/fsal.yml) and locally; works on x86_64 and arm64 hosts.
#
#   fsal/build/run.sh image    # build the Ganesha toolchain image
#   fsal/build/run.sh module   # compile libfsalomni.so into fsal/.build/
#   fsal/build/run.sh smoke    # load it in ganesha.nfsd and test an export
#   fsal/build/run.sh test [t_b2_read ...]  # the guard suite (fsal/tests/run_all.sh)
#   fsal/build/run.sh all      # image, smoke and test, in order
#   fsal/build/run.sh shell    # interactive shell in the toolchain image
#
# Environment: IMAGE (default omnifile-ganesha:local), DOCKER (default docker),
# SMOKE_MOUNT (yes|no|auto, passed to fsal/tests/smoke.sh; default auto),
# OMNI_TEST_IO_FLUSHER_FAIL_OK (non-empty: start Ganesha even where the
# container runtime has no CAP_SYS_RESOURCE for PR_SET_IO_FLUSHER, as in
# some sandboxes; never set in CI).
set -euo pipefail

FSAL_DIR=$(cd "$(dirname "$0")/.." && pwd)
IMAGE=${IMAGE:-omnifile-ganesha:local}
DOCKER=${DOCKER:-docker}
OUT=$FSAL_DIR/.build

image() {
	"$DOCKER" build -f "$FSAL_DIR/build/Containerfile" -t "$IMAGE" "$FSAL_DIR"
}

module() {
	mkdir -p "$OUT"
	# Run as the calling user so the build products are not root-owned.
	"$DOCKER" run --rm --user "$(id -u):$(id -g)" \
		-v "$FSAL_DIR:/fsal:ro" -v "$OUT:/out" "$IMAGE" \
		make -C /fsal/FSAL_OMNI O=/out
	echo "built $OUT/libfsalomni.so"
}

smoke() {
	module # incremental; never smoke-test a stale .so
	# --privileged: FSAL_VFS needs CAP_DAC_READ_SEARCH (open_by_handle_at)
	# and the NFS client round trip needs CAP_SYS_ADMIN (mount).
	# The anonymous volume gives the export a real filesystem (overlayfs,
	# the container root, cannot be exported by FSAL_VFS); --rm removes it.
	"$DOCKER" run --rm --privileged \
		--mount type=volume,dst=/srv/omni-smoke \
		-v "$FSAL_DIR:/fsal:ro" -v "$OUT:/out:ro" \
		-e OMNI_MODULE=/out/libfsalomni.so -e SMOKE_DIR=/srv/omni-smoke \
		-e SMOKE_MOUNT="${SMOKE_MOUNT:-auto}" \
		"$IMAGE" /fsal/tests/smoke.sh
}

test_suite() {
	module
	# The guard suite: ganesha.nfsd + FSAL_OMNI + the fake daemon on an xfs
	# loop volume, NFSv3/4.0/4.1/4.2 mounts. --privileged for the loop mount,
	# the NFS client and chattr; t_caps.sh then runs ganesha.nfsd itself with
	# only the production unit's capabilities (setpriv), so --privileged
	# cannot hide a missing CAP_SYS_ADMIN. The anonymous volume holds the xfs
	# image and is removed with the container.
	"$DOCKER" run --rm --privileged \
		--mount type=volume,dst=/srv/omni-test \
		-v "$FSAL_DIR:/fsal:ro" -v "$OUT:/out:ro" \
		-e OMNI_MODULE=/out/libfsalomni.so -e PYTHONDONTWRITEBYTECODE=1 \
		-e OMNI_TEST_IO_FLUSHER_FAIL_OK="${OMNI_TEST_IO_FLUSHER_FAIL_OK:-}" \
		"$IMAGE" /fsal/tests/run_all.sh "$@"
}

shell() {
	"$DOCKER" run --rm -it --privileged --mount type=volume,dst=/srv/omni-smoke \
		-v "$FSAL_DIR:/fsal" "$IMAGE" bash
}

case "${1:-all}" in
image) image ;;
module) module ;;
smoke) smoke ;;
test) shift; test_suite "$@" ;;
shell) shell ;;
all) image && smoke && test_suite ;;
*) echo "usage: $0 image|module|smoke|test [tests...]|all|shell" >&2; exit 2 ;;
esac
