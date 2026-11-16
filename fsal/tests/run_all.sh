#!/bin/bash
# SPDX-License-Identifier: LGPL-3.0-or-later
#
# The FSAL_OMNI guard suite. Runs INSIDE the toolchain image as root, in a
# privileged container (fsal/build/run.sh test starts it the right way):
# unit tests of the wire format, then the ported filer-spike tests B1-B5
# and the fail-closed, parking, invalidation and capability tests, each
# against a real ganesha.nfsd with the fake daemon (fsal/tests/fake_daemon.py)
# over NFSv3, 4.0, 4.1 and 4.2 mounts of an xfs volume.
#
#   run_all.sh [test ...]     e.g. run_all.sh t_b4_fail_closed t_park
#
# Environment: OMNI_MODULE (a built libfsalomni.so to install first; else it
# is built here), OMNI_TEST_DIR (default /srv/omni-test, must be a real
# filesystem: a Docker volume).
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
: "${GANESHA_PREFIX:=/opt/ganesha}"
: "${GANESHA_LIB:=$GANESHA_PREFIX/lib}"
: "${OMNI_TEST_DIR:=/srv/omni-test}"
export GANESHA_PREFIX OMNI_TEST_DIR
export RESULTS=$OMNI_TEST_DIR/results.txt
ALL="t_b1_load t_b2_read t_b2x_paths t_b3_identity t_b4_fail_closed t_park t_resume_race t_invalidate t_marker_cache t_fill_fence t_trunc_guard t_chunk_map t_pending t_threads t_snapshots t_reload t_ns_hint t_open_state t_b5_throughput t_caps"
TESTS=${*:-$ALL}

[ "$(id -u)" = 0 ] || { echo "run as root in the toolchain container" >&2; exit 2; }
mkdir -p "$OMNI_TEST_DIR"
: >"$RESULTS"

# --- image prerequisites --------------------------------------------------------
# fsal/build/run.sh test does not rebuild the image. One built from an older
# fsal/build/Containerfile lacks what newer tests need, and they then fail
# check by check for no reason in the code under test: an image from before
# libnfs-utils was added fails every tests/nfsc.py check (t_snapshots 16,
# t_reload 8, t_resume_race 2). Refuse to start instead, and say why.
missing=""
python3 -c 'import ctypes, ctypes.util; ctypes.CDLL(ctypes.util.find_library("nfs") or "libnfs.so.14")' \
	2>/dev/null || missing="$missing libnfs(libnfs-utils)"
for c in cc rpcbind mount.nfs showmount mkfs.xfs mkfs.ext4 xfs_io setfacl chattr fallocate setpriv; do
	command -v "$c" >/dev/null || missing="$missing $c"
done
if [ -n "$missing" ]; then
	echo "FAIL setup: the toolchain image lacks:$missing; it is older than fsal/build/Containerfile. Rebuild it: fsal/build/run.sh image (with the same IMAGE)" |
		tee -a "$RESULTS" >&2
	exit 2
fi

# --- module ---------------------------------------------------------------
if [ -n "${OMNI_MODULE:-}" ]; then
	install -D -m 0755 "$OMNI_MODULE" "$GANESHA_LIB/ganesha/libfsalomni.so"
else
	make -s -C "$HERE/../FSAL_OMNI" O=/tmp/omni-build install
fi

# --- unit tests (no Ganesha) ---------------------------------------------------
cc -Wall -Wextra -Werror -I "$HERE/../FSAL_OMNI" "$HERE/unit/test_wire.c" \
	"$HERE/../FSAL_OMNI/omni_wire.c" -o /tmp/test_wire
if /tmp/test_wire; then
	echo "PASS unit: marker codec (Rust golden vector), validity rules, message layouts" >>"$RESULTS"
else
	echo "FAIL unit: test_wire" >>"$RESULTS"
fi
python3 -c 'import sys; sys.path.insert(0, "'"$HERE"'"); import omni_proto' &&
	echo "PASS unit: Python codec matches the golden vector" >>"$RESULTS" ||
	echo "FAIL unit: Python codec" >>"$RESULTS"

rc=0
for t in $TESTS; do
	echo "===== $t"
	start=$(date +%s)
	if ! TNAME=$t bash "$HERE/$t.sh"; then
		rc=1
		grep -q "^FAIL $t" "$RESULTS" || echo "FAIL $t: exited non-zero" >>"$RESULTS"
	fi
	echo "===== $t done in $(($(date +%s) - start)) s"
done

umount "$OMNI_TEST_DIR/x" 2>/dev/null
echo
echo "================ FSAL_OMNI guard suite ================"
cat "$RESULTS"
np=$(grep -c '^PASS' "$RESULTS")
nf=$(grep -c '^FAIL' "$RESULTS")
echo "======== $np passed, $nf failed ========"
[ "$nf" = 0 ] && [ "$rc" = 0 ]
