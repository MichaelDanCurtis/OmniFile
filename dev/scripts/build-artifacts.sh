#!/usr/bin/env bash
# Build the Ganesha + FSAL_OMNI artefacts the edge VM installs (runs on the
# Mac, or on any Docker host of the VM's architecture).
#
#   dev/scripts/build-artifacts.sh            # into dev/.artifacts/
#
# Produces (git-ignored):
#   dev/.artifacts/ganesha.tar          /opt/ganesha from the fsal/build image
#                                       (the pinned V15.6 build, jemalloc)
#   dev/.artifacts/fsal/libfsalomni.so  FSAL_OMNI built against that same tree
#   dev/.artifacts/org.ganesha.nfsd.conf  Ganesha's D-Bus policy, from the same source
#
# The image is built first if it is missing (fsal/build/run.sh image). The
# module is built into dev/.artifacts/fsal, not fsal/.build, so a concurrent
# FSAL test run is never disturbed. Install inside the VM with
# dev/scripts/install-ganesha.sh /work/dev/.artifacts.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

IMAGE=${IMAGE:-omnifile-ganesha:local}
DOCKER=${DOCKER:-docker}
OUT=${ARTIFACTS_DIR:-$DEV_DIR/.artifacts}
FSAL_DIR=$REPO_DIR/fsal

command -v "$DOCKER" >/dev/null || die "$DOCKER not found"
mkdir -p "$OUT/fsal"

if ! "$DOCKER" image inspect "$IMAGE" >/dev/null 2>&1; then
  log "building the Ganesha toolchain image $IMAGE"
  "$FSAL_DIR/build/run.sh" image
fi

log "exporting /opt/ganesha from $IMAGE"
tmp="$OUT/ganesha.tar.tmp"
"$DOCKER" run --rm "$IMAGE" tar -C /opt -cf - ganesha >"$tmp"
mv "$tmp" "$OUT/ganesha.tar"
# Ganesha's D-Bus policy (lets root own org.ganesha.nfsd; without it Ganesha
# logs DBUS CRIT and has no admin interface for export reloads).
"$DOCKER" run --rm "$IMAGE" cat /usr/src/nfs-ganesha/src/scripts/ganeshactl/org.ganesha.nfsd.conf \
  >"$OUT/org.ganesha.nfsd.conf"

log "building libfsalomni.so"
"$DOCKER" run --rm --user "$(id -u):$(id -g)" \
  -v "$FSAL_DIR:/fsal:ro" -v "$OUT/fsal:/out" "$IMAGE" \
  make -s -C /fsal/FSAL_OMNI O=/out
# Record what was built, for the e2e results file.
{
  echo "image=$IMAGE $("$DOCKER" image inspect -f '{{.Id}}' "$IMAGE")"
  echo "fsal_tree=$(cd "$REPO_DIR" && git rev-parse --short HEAD 2>/dev/null || echo unknown)$(cd "$REPO_DIR" && git diff --quiet -- fsal 2>/dev/null || echo +dirty)"
  echo "module_sha256=$(shasum -a 256 "$OUT/fsal/libfsalomni.so" 2>/dev/null | cut -d' ' -f1 || sha256sum "$OUT/fsal/libfsalomni.so" | cut -d' ' -f1)"
} >"$OUT/BUILD-INFO"
log "artefacts in ${OUT#"$REPO_DIR"/}: ganesha.tar, fsal/libfsalomni.so"
