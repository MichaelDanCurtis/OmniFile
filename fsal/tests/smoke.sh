#!/bin/bash
# SPDX-License-Identifier: LGPL-3.0-or-later
#
# FSAL_OMNI load smoke test. Runs INSIDE the image from fsal/build/Containerfile,
# as root; fsal/build/run.sh smoke starts it the right way.
#
# It starts ganesha.nfsd with one export, FSAL OMNI stacked over FSAL VFS on
# a scratch directory, and checks that:
#   1. Ganesha loads libfsalomni.so and creates the export through it,
#      with no FSAL or CONFIG errors in its log;
#   2. the export is listed by the MOUNT protocol (showmount -e);
#   3. (when the container may mount NFS) NFSv3, v4.1 and v4.2 clients can
#      read and write through it, byte for byte, in both directions;
#   4. over NFSv4.2, CLONE (cp --reflink=always, FICLONE) is refused with
#      EOPNOTSUPP and leaves no destination data, while a plain cp goes out as
#      a server-side COPY and round-trips. Fails if Ganesha starts accepting
#      CLONE, which would let a reflink bypass the M1 stub guard.
#
# Environment:
#   OMNI_MODULE  path of a built libfsalomni.so to install first; if unset the
#                module is built from ../FSAL_OMNI with warnings as errors.
#   SMOKE_DIR    directory to export (default /srv/omni-smoke). It must be on a
#                filesystem that supports file handles (ext4, xfs); overlayfs
#                and tmpfs are refused by FSAL_VFS, so mount a volume there.
#   SMOKE_MOUNT  yes | no | auto (default auto). "yes" fails if the NFS mount
#                round trip cannot run; "auto" skips it, loudly, when the
#                container lacks CAP_SYS_ADMIN or the kernel has no NFS client.
#
# This is the load test. The stub-guard suite (ported spike tests B1-B5,
# fail-closed, parking, capabilities) is fsal/tests/run_all.sh
# (fsal/build/run.sh test). No daemon runs here, so the guard is
# disconnected and only unmanaged files are used: they pass through.
set -euo pipefail

: "${GANESHA_PREFIX:=/opt/ganesha}"
: "${GANESHA_LIB:=$GANESHA_PREFIX/lib}"
SMOKE_DIR=${SMOKE_DIR:-/srv/omni-smoke}
SMOKE_MOUNT=${SMOKE_MOUNT:-auto}
HERE=$(cd "$(dirname "$0")" && pwd)
WORK=$(mktemp -d /tmp/omni-smoke.XXXXXX)
CONF=$WORK/ganesha.conf
LOG=$WORK/ganesha.log
MNT=$WORK/mnt
GPID=

say() { printf '[smoke] %s\n' "$*"; }
fail() {
	printf '[smoke] FAIL: %s\n' "$*" >&2
	if [ -f "$LOG" ]; then
		echo "----- ganesha.log -----" >&2
		cat "$LOG" >&2
	fi
	exit 1
}

cleanup() {
	mountpoint -q "$MNT" 2>/dev/null && umount -f "$MNT" 2>/dev/null || true
	if [ -n "$GPID" ] && kill -0 "$GPID" 2>/dev/null; then
		kill "$GPID" 2>/dev/null || true
		wait "$GPID" 2>/dev/null || true
	fi
}
trap cleanup EXIT

[ "$(id -u)" = 0 ] || fail "must run as root (inside the toolchain container)"
[ -x "$GANESHA_PREFIX/bin/ganesha.nfsd" ] || fail "no Ganesha at $GANESHA_PREFIX"

# --- module -------------------------------------------------------------
if [ -n "${OMNI_MODULE:-}" ]; then
	[ -f "$OMNI_MODULE" ] || fail "OMNI_MODULE=$OMNI_MODULE not found"
	install -D -m 0755 "$OMNI_MODULE" "$GANESHA_LIB/ganesha/libfsalomni.so"
else
	make -s -C "$HERE/../FSAL_OMNI" O="$WORK/build" install
fi
say "module: $(sha256sum "$GANESHA_LIB/ganesha/libfsalomni.so" | cut -c1-16) $GANESHA_LIB/ganesha/libfsalomni.so"
readelf -d "$GANESHA_LIB/ganesha/libfsalomni.so" | grep -q libjemalloc ||
	fail "libfsalomni.so is not linked with jemalloc (Ganesha would crash at load)"

# --- export directory ---------------------------------------------------
mkdir -p "$SMOKE_DIR" "$MNT"
fstype=$(stat -f -c %T "$SMOKE_DIR")
case "$fstype" in
overlayfs | tmpfs | fuseblk | fuse | virtiofs | 9p)
	fail "$SMOKE_DIR is on $fstype, which FSAL_VFS cannot export; mount a volume there (fsal/build/run.sh smoke does)" ;;
esac
say "exporting $SMOKE_DIR ($fstype)"
head -c 4194304 /dev/urandom >"$SMOKE_DIR/seed.bin"

cat >"$CONF" <<EOF
NFS_CORE_PARAM {
	Protocols = 3, 4;
	Enable_NLM = false;
	Enable_RQUOTA = false;
}
NFSV4 {
	Minor_Versions = 0, 1, 2;
	Graceless = true;
	RecoveryBackend = fs;
}
LOG {
	Default_Log_Level = EVENT;
	Components { FSAL = INFO; }
}
EXPORT {
	Export_Id = 77;
	Path = $SMOKE_DIR;
	Pseudo = /omni;
	Access_Type = RW;
	Squash = No_Root_Squash;
	SecType = sys;
	Protocols = 3, 4;
	Transports = TCP;
	FSAL {
		Name = OMNI;
		FSAL { Name = VFS; }
	}
}
EOF

# --- start Ganesha ------------------------------------------------------
# rpcbind is needed for NFSv3 and showmount. There is no system D-Bus in the
# container, so Ganesha logs DBUS CRIT lines and carries on without its admin
# interface; that is expected here.
pgrep -x rpcbind >/dev/null || rpcbind -w
"$GANESHA_PREFIX/bin/ganesha.nfsd" -F -L "$LOG" -f "$CONF" -N NIV_EVENT >"$WORK/ganesha.out" 2>&1 &
GPID=$!

for _ in $(seq 1 60); do
	grep -q 'NFS SERVER INITIALIZED' "$LOG" 2>/dev/null && break
	kill -0 "$GPID" 2>/dev/null || fail "ganesha.nfsd exited during startup"
	sleep 0.5
done
grep -q 'NFS SERVER INITIALIZED' "$LOG" || fail "ganesha.nfsd did not initialise in 30 s"
kill -0 "$GPID" 2>/dev/null || fail "ganesha.nfsd died after initialising"
say "ganesha.nfsd up (pid $GPID)"

# --- 1. the export goes through FSAL_OMNI ------------------------------
grep -q 'omni_create_export :FSAL :INFO :FSAL_OMNI export stacked over FSAL VFS' "$LOG" ||
	fail "FSAL_OMNI did not create the export"
if grep -E ':(FSAL|CONFIG|EXPORT) :(CRIT|MAJ|FATAL) ' "$LOG" | grep -q .; then
	fail "FSAL/CONFIG/EXPORT errors in the Ganesha log"
fi
grep -q 'No export entries found' "$LOG" && fail "Ganesha found no exports"
say "PASS: libfsalomni.so loaded; export 77 created by FSAL_OMNI over VFS"

# --- 2. the export is listed -------------------------------------------
listing=$(showmount -e 127.0.0.1) || fail "showmount -e failed"
printf '%s\n' "$listing" | sed 's/^/[smoke]   /'
printf '%s\n' "$listing" | grep -q "^$SMOKE_DIR " || fail "$SMOKE_DIR not in the export list"
say "PASS: export listed by showmount"

# --- 3. read/write round trip over NFS ---------------------------------
can_mount() {
	grep -q '^CapEff:' /proc/self/status || return 1
	# CAP_SYS_ADMIN is bit 21.
	local eff
	eff=$(awk '/^CapEff:/ {print $2}' /proc/self/status)
	(( (16#$eff >> 21) & 1 ))
}

roundtrip() { # <label> <mount source> <mount options>
	local label=$1 src=$2 opts=$3 tag
	tag=$(echo "$label" | tr 'A-Z.' 'a-z_')
	mount -t nfs -o "$opts" "$src" "$MNT" || return 2
	# read through FSAL_OMNI what was written locally
	cmp -s "$SMOKE_DIR/seed.bin" "$MNT/seed.bin" || { umount "$MNT"; fail "$label: read mismatch"; }
	# write through FSAL_OMNI, read back locally and over NFS
	head -c 1048577 /dev/urandom >"$WORK/out.bin"
	cp "$WORK/out.bin" "$MNT/written-$tag.bin"
	sync "$MNT/written-$tag.bin"
	cmp -s "$WORK/out.bin" "$SMOKE_DIR/written-$tag.bin" || { umount "$MNT"; fail "$label: write did not reach the backing directory intact"; }
	mkdir "$MNT/dir-$tag" && mv "$MNT/written-$tag.bin" "$MNT/dir-$tag/" &&
		[ -f "$SMOKE_DIR/dir-$tag/written-$tag.bin" ] || { umount "$MNT"; fail "$label: mkdir/rename failed"; }
	rm -r "$MNT/dir-$tag"
	[ ! -e "$SMOKE_DIR/dir-$tag" ] || { umount "$MNT"; fail "$label: remove failed"; }
	umount "$MNT"
	say "PASS: $label read/write/rename/remove round trip"
}

# NFSv4 per-op call count for the mount at $MNT, from /proc/self/mountstats.
nfs_op_count() { # <OP>
	awk -v mnt="$MNT" -v op="$1:" '
		$1 == "device" { inmnt = ($5 == mnt) }
		inmnt && $1 == op { print $2; found = 1; exit }
		END { if (!found) print 0 }' /proc/self/mountstats
}

# CLONE / COPY regression (NFSv4.2). Ganesha V15.6 wires OP_CLONE to
# nfs4_op_notsupp (src/Protocols/NFS/nfs4_Compound.c, NFS4_OP_CLONE), so a
# reflink of a stub can never share its holes. If a Ganesha bump starts
# accepting CLONE, this fails: FSAL_OMNI must then guard clone like read2
# before the bump lands. Server-side COPY must keep working for normal files.
clone_copy_check() {
	local out before after
	mount -t nfs -o vers=4.2,proto=tcp 127.0.0.1:/omni "$MNT" || return 2
	head -c 3145731 /dev/urandom >"$SMOKE_DIR/cc-src.bin"

	# (1a) cp --reflink=always: EOPNOTSUPP, and no destination left behind.
	if out=$(cp --reflink=always "$MNT/cc-src.bin" "$MNT/cc-reflink.bin" 2>&1); then
		umount "$MNT"
		fail "NFSv4.2: cp --reflink=always SUCCEEDED; Ganesha now accepts CLONE. FSAL_OMNI must guard clone of stubs before this Ganesha ships"
	fi
	printf '%s\n' "$out" | grep -q 'Operation not supported' ||
		{ umount "$MNT"; fail "NFSv4.2: cp --reflink=always failed, but not with EOPNOTSUPP: $out"; }
	[ ! -e "$MNT/cc-reflink.bin" ] && [ ! -e "$SMOKE_DIR/cc-reflink.bin" ] ||
		{ umount "$MNT"; fail "NFSv4.2: failed cp --reflink=always left a destination file"; }

	# (1b) raw FICLONE ioctl (xfs_io reflink, no range): EOPNOTSUPP, and the
	# pre-opened destination stays empty.
	: >"$MNT/cc-ficlone.bin"
	if out=$(xfs_io -F -c "reflink $MNT/cc-src.bin" "$MNT/cc-ficlone.bin" 2>&1) &&
		! printf '%s\n' "$out" | grep -qi 'not supported'; then
		umount "$MNT"
		fail "NFSv4.2: FICLONE SUCCEEDED ($out); Ganesha now accepts CLONE. FSAL_OMNI must guard clone of stubs before this Ganesha ships"
	fi
	printf '%s\n' "$out" | grep -q 'Operation not supported' ||
		{ umount "$MNT"; fail "NFSv4.2: FICLONE failed, but not with EOPNOTSUPP: $out"; }
	[ "$(stat -c %s "$SMOKE_DIR/cc-ficlone.bin")" = 0 ] ||
		{ umount "$MNT"; fail "NFSv4.2: failed FICLONE wrote data into the destination"; }
	rm -f "$MNT/cc-ficlone.bin"

	# (2) plain cp of a normal file goes out as a server-side COPY
	# (copy_file_range) and round-trips byte for byte.
	before=$(nfs_op_count COPY)
	cp "$MNT/cc-src.bin" "$MNT/cc-copy.bin" ||
		{ umount "$MNT"; fail "NFSv4.2: plain cp failed"; }
	after=$(nfs_op_count COPY)
	[ "$after" -gt "$before" ] ||
		{ umount "$MNT"; fail "NFSv4.2: plain cp sent no COPY op (COPY count $before -> $after); the server-side copy path was not exercised"; }
	if ! cmp -s "$MNT/cc-src.bin" "$MNT/cc-copy.bin" ||
		! cmp -s "$SMOKE_DIR/cc-src.bin" "$SMOKE_DIR/cc-copy.bin"; then
		umount "$MNT"
		fail "NFSv4.2: server-side COPY produced different bytes"
	fi
	rm -f "$MNT/cc-src.bin" "$MNT/cc-copy.bin"
	umount "$MNT"
	say "PASS: NFSv4.2 CLONE refused with EOPNOTSUPP (cp --reflink=always, FICLONE), no destination data"
	say "PASS: NFSv4.2 server-side COPY ($((after - before)) COPY ops) round trip"
}

case "$SMOKE_MOUNT" in
no) say "SKIP: NFS mount round trip (SMOKE_MOUNT=no)" ;;
yes | auto)
	if ! can_mount; then
		[ "$SMOKE_MOUNT" = yes ] && fail "SMOKE_MOUNT=yes but the container lacks CAP_SYS_ADMIN (run it --privileged)"
		say "SKIP: NFS mount round trip (no CAP_SYS_ADMIN; run --privileged to include it)"
	else
		for v in "NFSv3|127.0.0.1:$SMOKE_DIR|vers=3,proto=tcp,nolock" \
			"NFSv4.1|127.0.0.1:/omni|vers=4.1,proto=tcp" \
			"NFSv4.2|127.0.0.1:/omni|vers=4.2,proto=tcp"; do
			IFS='|' read -r label src opts <<<"$v"
			rc=0
			roundtrip "$label" "$src" "$opts" || rc=$?
			if [ "$rc" = 2 ]; then
				[ "$SMOKE_MOUNT" = yes ] && fail "$label: mount failed (does the host kernel have an NFS client?)"
				say "SKIP: $label mount failed; the host kernel may lack an NFS client"
				break
			fi
		done
		if [ "$rc" != 2 ]; then
			rc=0
			clone_copy_check || rc=$?
			if [ "$rc" = 2 ]; then
				[ "$SMOKE_MOUNT" = yes ] && fail "NFSv4.2: mount failed for the CLONE/COPY check"
				say "SKIP: CLONE/COPY check (NFSv4.2 mount failed)"
			fi
		fi
	fi
	;;
*) fail "SMOKE_MOUNT must be yes, no or auto (got $SMOKE_MOUNT)" ;;
esac

kill -0 "$GPID" 2>/dev/null || fail "ganesha.nfsd died during the test"
say "OK"
