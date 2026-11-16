#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Previous Versions over SMB (fsal-namespace.md section 7): the share stanza
# `ShareConfig::render_smb` emits for a share with a rewind area, run by a
# real smbd. Checks that vfs_shadow_copy2 takes the daemon's layout (an
# absolute snapdir outside the share, <data>/.omni/snap/<share>/<label>/<path>,
# labels named %Y-%m-%dT%H%MZ in UTC):
#   - a file's labels show as previous versions, newest first;
#   - each label reads that label's bytes, in subdirectories too;
#   - the live file is untouched, and the rewind area is not in the share;
#   - nothing can be written into a previous version.
#
#   sudo shadow_copy2.sh --omniacl <path to the omniacl example binary>
#   sudo shadow_copy2.sh --stanza <file with the rendered [share] stanza>
#
# Needs root, smbd (with samba-vfs-modules) and smbclient. It runs its own
# smbd on port $PORT (default 4455) with private state, and a temporary local
# user standing in for the share's AD groups, both removed at the end.
set -uo pipefail

PORT=${PORT:-4455}
OMNIACL="" STANZA=""
while [ $# -gt 0 ]; do
	case $1 in
	--omniacl) OMNIACL=$2; shift 2 ;;
	--stanza) STANZA=$2; shift 2 ;;
	*) echo "usage: $0 --omniacl BIN | --stanza FILE" >&2; exit 2 ;;
	esac
done
[ "$(id -u)" = 0 ] || { echo "needs root" >&2; exit 2; }
for c in smbd smbclient smbpasswd testparm; do
	command -v "$c" >/dev/null || { echo "$c not installed" >&2; exit 2; }
done

W=$(mktemp -d /tmp/omni-shadow.XXXXXX)
USER_NAME=omnishadow$$
NFAIL=0
pass() { echo "PASS: $*"; }
fail() { echo "FAIL: $*" >&2; NFAIL=$((NFAIL + 1)); }
cleanup() {
	[ -f "$W/run/smbd.pid" ] && kill "$(cat "$W/run/smbd.pid")" 2>/dev/null
	sleep 0.5
	userdel "$USER_NAME" 2>/dev/null
	rm -rf "$W"
}
trap cleanup EXIT

DATA=$W/data
SNAP=$DATA/.omni/snap/proj
# The stanza for a share at $DATA/proj with its rewind area at $SNAP.
if [ -n "$OMNIACL" ]; then
	"$OMNIACL" render proj "$DATA/proj" 10 nfs4,smb krb5 - S-1-5-21-1-2-3-512 - - wait "$SNAP" |
		awk '/^#--- smb/{f=1;next} /^#--- /{f=0} f' >"$W/share.conf"
elif [ -n "$STANZA" ]; then
	sed "s|/srv/data|$DATA|g" "$STANZA" >"$W/share.conf"
else
	echo "usage: $0 --omniacl BIN | --stanza FILE" >&2
	exit 2
fi
grep -q "shadow:snapdir = $SNAP" "$W/share.conf" || { echo "no shadow:snapdir in the stanza" >&2; cat "$W/share.conf" >&2; exit 1; }
# The AD groups the share names become one local user here.
sed -i -E "s/^(\s*(valid users|write list|read list) = ).*/\1$USER_NAME/" "$W/share.conf"

# The live share, and two labels as the daemon lays them out.
mkdir -p "$DATA/proj/sub" "$SNAP/2026-09-01T0000Z/sub" "$SNAP/2026-10-01T0000Z" "$W"/{run,lock,state,cache,private}
echo "now" >"$DATA/proj/a.txt"
echo "sub now" >"$DATA/proj/sub/b.txt"
echo "as of september" >"$SNAP/2026-09-01T0000Z/a.txt"
echo "sub as of september" >"$SNAP/2026-09-01T0000Z/sub/b.txt"
echo "as of october" >"$SNAP/2026-10-01T0000Z/a.txt"
chmod -R a+rX "$W"
chmod 0555 "$SNAP"/*

useradd -M -s /usr/sbin/nologin "$USER_NAME"
cat >"$W/smb.conf" <<EOF
[global]
    security = user
    server role = standalone server
    map to guest = never
    smb ports = $PORT
    disable netbios = yes
    server min protocol = SMB2_10
    pid directory = $W/run
    lock directory = $W/lock
    state directory = $W/state
    cache directory = $W/cache
    private dir = $W/private
    ncalrpc dir = $W/run/ncalrpc
    passdb backend = tdbsam:$W/private/passdb.tdb
    log file = $W/smbd.log
    log level = 1
EOF
cat "$W/share.conf" >>"$W/smb.conf"
testparm -s "$W/smb.conf" >"$W/testparm.out" 2>&1 || { cat "$W/testparm.out" >&2; exit 1; }
(echo pw; echo pw) | smbpasswd -c "$W/smb.conf" -s -a "$USER_NAME" >/dev/null
smbd -D -s "$W/smb.conf" || { echo "smbd did not start" >&2; exit 1; }
for _ in $(seq 1 50); do
	smbclient -s "$W/smb.conf" -U "$USER_NAME%pw" "//127.0.0.1/proj" -p "$PORT" -m SMB3 -c 'ls' >/dev/null 2>&1 && break
	sleep 0.2
done

C() { smbclient -s "$W/smb.conf" -U "$USER_NAME%pw" "//127.0.0.1/proj" -p "$PORT" -m SMB3 -c "$1" 2>&1; }
get() { C "get \"$1\" $W/out" >/dev/null && cat "$W/out" && rm -f "$W/out"; }

versions=$(C 'allinfo a.txt' | grep -o '@GMT-[0-9.-]*' | tr '\n' ' ')
if [ "$versions" = "@GMT-2026.10.01-00.00.00 @GMT-2026.09.01-00.00.00 " ]; then
	pass "a file's labels are its previous versions, newest first"
else
	fail "previous versions of a.txt: '$versions'"
fi
[ "$(get '@GMT-2026.09.01-00.00.00/a.txt')" = "as of september" ] && pass "a label reads that label's bytes" ||
	fail "@GMT-2026.09.01 a.txt: '$(get '@GMT-2026.09.01-00.00.00/a.txt')'"
[ "$(get '@GMT-2026.10.01-00.00.00/a.txt')" = "as of october" ] && pass "and another label its own" ||
	fail "@GMT-2026.10.01 a.txt"
[ "$(get '@GMT-2026.09.01-00.00.00/sub/b.txt')" = "sub as of september" ] && pass "in a subdirectory too" ||
	fail "@GMT-2026.09.01 sub/b.txt"
[ "$(get a.txt)" = "now" ] && pass "the live file is untouched" || fail "live a.txt"
C 'ls' | grep -q '\.omni' && fail "the rewind area shows in the share" || pass "the rewind area is not in the share"
echo x >"$W/x"
out=$(C "put $W/x \"@GMT-2026.09.01-00.00.00/new.txt\"")
if grep -q NT_STATUS <<<"$out" && [ ! -e "$SNAP/2026-09-01T0000Z/new.txt" ]; then
	pass "a previous version is read-only ($(grep -o 'NT_STATUS_[A-Z_]*' <<<"$out" | head -1))"
else
	fail "write into a previous version: $out"
fi

echo "smbd $(smbd --version | awk '{print $2}'): $NFAIL failure(s)"
[ "$NFAIL" = 0 ]
