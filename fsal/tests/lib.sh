# SPDX-License-Identifier: LGPL-3.0-or-later
# Shared helpers for the FSAL_OMNI guard tests. Sourced by fsal/tests/t_*.sh,
# which run as root inside the toolchain image (fsal/build/run.sh test).
#
# Layout (on the Docker volume at $OMNI_TEST_DIR):
#   x.img          an xfs image, loop-mounted at $DATA (the product's filesystem)
#   $DATA/{wait,failfast,quick,park}   one OMNI export per policy
#   $DATA/null     upstream FSAL_NULL over VFS (control for B5)
#   $PLAIN         plain FSAL_VFS export (control for the threading test)
#   $BACKEND       "cloud" objects, named by file_id
set -uo pipefail

T=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
: "${GANESHA_PREFIX:=/opt/ganesha}"
: "${OMNI_TEST_DIR:=/srv/omni-test}"
B=$OMNI_TEST_DIR
DATA=$B/x
PLAIN=$B/plain
BACKEND=$B/backend
MNT=$B/mnt
RUN=/run/omnifile
SOCK=$RUN/fsal.sock
CTL=$RUN/fake-ctl.sock
LOG=$B/ganesha.log
DLOG=$B/daemon.log
GPID_FILE=$B/ganesha.pid
RESULTS=${RESULTS:-$B/results.txt}
MiB=$((1024 * 1024))
SIZE=$((64 * MiB)) # stub geometry, as in the spike: 64 MiB, 4 MiB resident
RES=$((4 * MiB))
TNAME=${TNAME:-$(basename "$0" .sh)}
NFAIL=0

say() { printf '[%s] %s\n' "$TNAME" "$*"; }
pass() {
	printf '[%s] PASS: %s\n' "$TNAME" "$*"
	echo "PASS $TNAME: $*" >>"$RESULTS"
}
fail() {
	printf '[%s] FAIL: %s\n' "$TNAME" "$*" >&2
	echo "FAIL $TNAME: $*" >>"$RESULTS"
	NFAIL=$((NFAIL + 1))
}
finish() {
	umount_all
	daemon_stop
	ganesha_stop
	if [ "$NFAIL" -ne 0 ]; then
		say "$NFAIL check(s) failed; last Ganesha log lines:"
		tail -n 40 "$LOG" 2>/dev/null | sed 's/^/    /' >&2
		exit 1
	fi
	exit 0
}

# jq-free JSON field test: jtest '<json>' '<python expr over d>'
jtest() { python3 -c 'import json,sys; d=json.loads(sys.argv[1]); sys.exit(0 if eval(sys.argv[2]) else 1)' "$1" "$2"; }
now() { python3 -c 'import time; print(time.time())'; }

# ---- volumes -----------------------------------------------------------------
setup_volumes() {
	mkdir -p "$B" "$MNT" "$RUN" "$BACKEND" "$PLAIN"
	if ! mountpoint -q "$DATA"; then
		mkdir -p "$DATA"
		local fs=xfs
		[ -f "$B/fstype" ] && fs=$(cat "$B/fstype")
		if [ ! -f "$B/x.img" ]; then
			truncate -s 16G "$B/x.img" # t_chunk_map fetches about 1 GiB per run
			mkfs_data "$fs"
		fi
		mount -o loop "$B/x.img" "$DATA" || { echo "cannot loop-mount the test image" >&2; exit 2; }
		# Ganesha is built with ENABLE_VFS_POSIX_ACL, and then every
		# GETATTR on a filesystem without POSIX ACL support fails with
		# NOTSUPP. Docker Desktop's linuxkit kernel has no
		# CONFIG_XFS_POSIX_ACL (Ubuntu's kernel, CI's and the appliance's,
		# has it). There the suite runs on ext4 instead, and says so.
		if [ "$fs" = xfs ] && ! has_posix_acl "$DATA"; then
			umount "$DATA"
			echo ext4 >"$B/fstype"
			mkfs_data ext4
			mount -o loop "$B/x.img" "$DATA" || { echo "cannot loop-mount ext4" >&2; exit 2; }
			echo "NOTE setup: this kernel's xfs has no POSIX ACL support (CONFIG_XFS_POSIX_ACL); the suite runs on ext4" >>"$RESULTS"
			echo "[setup] NOTE: xfs here has no POSIX ACLs; using an ext4 loop volume instead" >&2
		fi
	fi
	mkdir -p "$DATA"/{wait,failfast,quick,park,null}
}
mkfs_data() {
	case $1 in
	xfs) mkfs.xfs -q -f "$B/x.img" ;;
	ext4) mkfs.ext4 -q -F "$B/x.img" ;;
	esac
}
has_posix_acl() {
	python3 -c 'import errno, os, sys
try:
    os.getxattr(sys.argv[1], "system.posix_acl_access")
except OSError as e:
    sys.exit(1 if e.errno == errno.EOPNOTSUPP else 0)' "$1"
}

# ---- Ganesha -------------------------------------------------------------------
# ganesha_conf [park_ms] [extra OMNI block lines]
ganesha_conf() {
	local park_ms=${1:-5000}
	cat <<EOF
NFS_CORE_PARAM {
	Plugins_Dir = "${PLUGINS_DIR:-$GANESHA_PREFIX/lib/ganesha}";
	Protocols = 3, 4;
	NFS_Port = 2049;
	Enable_NLM = false;
	Enable_RQUOTA = false;
	${OMNI_TEST_IO_FLUSHER_FAIL_OK:+allow_set_io_flusher_fail = true;}
}
NFSV4 {
	Minor_Versions = 0, 1, 2;
	Graceless = true;
	RecoveryBackend = fs;
}
LOG {
	Default_Log_Level = ${DEFAULT_LOG:-EVENT};
	Components { FSAL = ${FSAL_LOG:-INFO}; }
}
OMNI {
	Socket_Path = "$SOCK";
	Park_Max_Per_Client = ${PARK_PER_CLIENT:-16};
}
EXPORT_DEFAULTS {
	Access_Type = RW;
	Squash = No_Root_Squash;
	SecType = sys;
	Protocols = 3, 4;
	Transports = TCP;
}
EXPORT {
	Export_Id = 101; Path = $DATA/wait; Pseudo = /omni/wait;
	FSAL { Name = OMNI; Policy = "wait"; Park_Ms = $park_ms;
	       Snap_Dir = "$DATA/.omni/snap/wait"; FSAL { Name = VFS; } }
}
EXPORT {
	Export_Id = 102; Path = $DATA/failfast; Pseudo = /omni/failfast;
	FSAL { Name = OMNI; Policy = "failfast"; Park_Ms = $park_ms;
	       Snap_Dir = "$DATA/.omni/snap/failfast"; FSAL { Name = VFS; } }
}
EXPORT {
	Export_Id = 103; Path = $DATA/quick; Pseudo = /omni/quick;
	FSAL { Name = OMNI; Policy = "waitquick:5000"; Park_Ms = $park_ms; FSAL { Name = VFS; } }
}
EXPORT {
	Export_Id = 104; Path = $DATA/park; Pseudo = /omni/park;
	FSAL { Name = OMNI; Policy = "failfast"; Park_Ms = $park_ms; FSAL { Name = VFS; } }
}
EXPORT {
	Export_Id = 202; Path = $DATA/null; Pseudo = /null;
	FSAL { Name = NULL; FSAL { Name = VFS; } }
}
EXPORT {
	Export_Id = 201; Path = $PLAIN; Pseudo = /plain;
	FSAL { Name = VFS; }
}
EOF
}

# ganesha_start [prefix command...]  e.g. ganesha_start setpriv --bounding-set ...
ganesha_start() {
	ganesha_stop
	ganesha_conf "${PARK_MS:-5000}" >"$B/ganesha.conf"
	# GANESHA_INCLUDE: a file of further EXPORT blocks, as the appliance
	# includes the ones the edge daemon renders (t_reload).
	if [ -n "${GANESHA_INCLUDE:-}" ]; then
		printf '%%include "%s"\n' "$GANESHA_INCLUDE" >>"$B/ganesha.conf"
	fi
	: >"$LOG"
	pgrep -x rpcbind >/dev/null || rpcbind -w
	"$@" "$GANESHA_PREFIX/bin/ganesha.nfsd" -F -L "$LOG" -f "$B/ganesha.conf" \
		-p "$GPID_FILE" -N NIV_EVENT >"$B/ganesha.out" 2>&1 &
	echo $! >"$B/ganesha.bgpid"
	disown
	local i
	for i in $(seq 1 60); do
		grep -q 'NFS SERVER INITIALIZED' "$LOG" 2>/dev/null && return 0
		kill -0 "$(cat "$B/ganesha.bgpid")" 2>/dev/null || break
		sleep 0.5
	done
	echo "ganesha.nfsd did not start" >&2
	tail -n 30 "$LOG" "$B/ganesha.out" >&2
	return 1
}
ganesha_pid() { cat "$B/ganesha.bgpid" 2>/dev/null; }
ganesha_stop() {
	local p
	p=$(ganesha_pid)
	if [ -n "$p" ] && kill -0 "$p" 2>/dev/null; then
		kill "$p" 2>/dev/null
		for _ in $(seq 1 40); do kill -0 "$p" 2>/dev/null || break; sleep 0.25; done
		kill -9 "$p" 2>/dev/null
	fi
	rm -f "$B/ganesha.bgpid"
	pkill -x ganesha.nfsd 2>/dev/null
	true
}
ganesha_alive() { kill -0 "$(ganesha_pid)" 2>/dev/null; }

# ---- fake daemon -----------------------------------------------------------------
daemon_start() { # args passed to fake_daemon.py
	local before i
	daemon_stop
	before=$(nconnected)
	setsid python3 "$T/fake_daemon.py" --root "$DATA" --backend "$BACKEND" \
		--socket "$SOCK" --ctl "$CTL" --log "$DLOG" "$@" >>"$B/daemon.out" 2>&1 </dev/null &
	echo $! >"$B/daemon.pid"
	disown
	for i in $(seq 1 50); do [ -S "$SOCK" ] && break; sleep 0.1; done
	[ -S "$SOCK" ] || { echo "fake daemon did not start" >&2; cat "$B/daemon.out" >&2; return 1; }
	# wait for the FSAL to (re)connect; its backoff is at most 2 s
	case "$*" in *hang-hello* | *bad-version*) sleep 2.5; return 0 ;; esac
	ganesha_alive || return 0
	for i in $(seq 1 50); do
		[ "$(nconnected)" -gt "$before" ] && return 0
		sleep 0.1
	done
	echo "FSAL did not connect to the fake daemon" >&2
	return 1
}
nconnected() { grep -c 'FSAL_OMNI: connected to the OmniFile daemon' "$LOG" 2>/dev/null || true; }
ganesha_connected() { grep -q 'FSAL_OMNI: connected to the OmniFile daemon' "$LOG"; }
daemon_stop() {
	if [ -f "$B/daemon.pid" ]; then
		kill -9 "$(cat "$B/daemon.pid")" 2>/dev/null
		rm -f "$B/daemon.pid"
	fi
	pkill -9 -f fake_daemon.py 2>/dev/null
	rm -f "$SOCK" "$CTL"
	sleep 0.2
	true
}
# daemon_ctl '<json command>' -> prints the JSON answer
daemon_ctl() {
	python3 - "$CTL" "$1" <<'PY'
import socket, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect(sys.argv[1])
f = s.makefile("rw")
f.write(sys.argv[2] + "\n"); f.flush()
print(f.readline().strip())
PY
}
# daemon_count <since-epoch> <python filter over m> -> number of logged messages
daemon_count() {
	python3 - "$DLOG" "$1" "$2" <<'PY'
import json, sys
since, expr = float(sys.argv[2]), sys.argv[3]
n = 0
for line in open(sys.argv[1]):
    m = json.loads(line)
    if m.get("t", 0) >= since and eval(expr):
        n += 1
print(n)
PY
}
# daemon_last <python filter> -> last matching message as JSON
daemon_last() {
	python3 - "$DLOG" "$1" <<'PY'
import json, sys
last = "{}"
for line in open(sys.argv[1]):
    m = json.loads(line)
    if eval(sys.argv[2]):
        last = line.strip()
print(last)
PY
}

# ---- NFS mounts --------------------------------------------------------------------
# mount_v <vers> <share|plain|null> <dir>   vers: 3 | 4.0 | 4.1 | 4.2
mount_v() {
	local vers=$1 share=$2 dir=$3 src
	mkdir -p "$dir"
	umount -f -l "$dir" 2>/dev/null
	if [ "$vers" = 3 ]; then
		case $share in
		plain) src=$PLAIN ;;
		null) src=$DATA/null ;;
		*) src=$DATA/$share ;;
		esac
		mount -t nfs -o vers=3,proto=tcp,nolock,mountproto=tcp,port=2049,actimeo=1 \
			"127.0.0.1:$src" "$dir"
	else
		case $share in
		plain) src=/plain ;;
		null) src=/null ;;
		*) src=/omni/$share ;;
		esac
		mount -t nfs -o "vers=$vers,proto=tcp,actimeo=1" "127.0.0.1:$src" "$dir"
	fi
}
umount_all() {
	local d
	for d in "$MNT"/*; do
		[ -d "$d" ] && umount -f -l "$d" 2>/dev/null
	done
	true
}

mkstub() { python3 "$T/mkstub.py" --backend "$BACKEND" "$@"; }
reader() { python3 "$T/reader.py" "$@"; }

# blocked_read <secs> <file> <backend> <start>: succeeds only if a read of
# the file at <start> is still blocked (the client retrying DELAY) when
# timeout fires (exit 124, nothing printed), and the mount still answers a
# stat afterwards. Otherwise prints why and fails. An empty output alone is
# not "blocked": the reader may have crashed, or the mount died.
blocked_read() {
	local secs=$1 file=$2 be=$3 start=$4 out rc sz
	out=$(timeout -k 2 "$secs" python3 "$T/reader.py" one "$file" "$be" --start "$start" 2>&1)
	rc=$?
	sz=$(timeout 5 stat -c %s "$file" 2>&1) || sz="mount not answering ($sz)"
	if [ $rc = 124 ] && [ -z "$out" ] && [[ $sz =~ ^[0-9]+$ ]] && ganesha_alive; then
		return 0
	fi
	echo "timeout rc $rc (want 124), reader said '${out}', stat after: $sz"
	return 1
}

# A copy of the module that can hold marker reads: the SAME object files as
# the module under test, relinked with the slowxattr.c shim first in its
# DT_NEEDED list. Ganesha dlopens FSALs with RTLD_DEEPBIND, so LD_PRELOAD
# cannot interpose; a dependency of the module can. Installed in its own
# plugins directory (with links to the other FSALs); start Ganesha with
# PLUGINS_DIR=$SLOW_PLUGINS to use it.
SLOW_PLUGINS=/tmp/omni-plugins-slow
build_slow_module() {
	local objdir=${OMNI_MODULE:-/tmp/omni-build/libfsalomni.so} f
	objdir=$(dirname "$objdir")
	ls "$objdir"/omni_guard.o >/dev/null || return 1
	mkdir -p "$SLOW_PLUGINS/lib"
	cc -Wall -Wextra -Werror -shared -fPIC -o "$SLOW_PLUGINS/lib/libomnislowx.so" "$T/slowxattr.c" -ldl || return 1
	cc -shared -Wl,--no-undefined -o "$SLOW_PLUGINS/libfsalomni.so" "$objdir"/*.o \
		-L"$SLOW_PLUGINS/lib" -Wl,-rpath,"$SLOW_PLUGINS/lib" -Wl,--no-as-needed -lomnislowx -Wl,--as-needed \
		-L"$GANESHA_PREFIX/lib" -lganesha_nfsd -lntirpc -ljemalloc || return 1
	for f in "$GANESHA_PREFIX"/lib/ganesha/*.so; do
		[ "$(basename "$f")" = libfsalomni.so ] || ln -sf "$f" "$SLOW_PLUGINS/"
	done
}
# slow_marker <ms>|off: hold every marker read in ganesha.nfsd for <ms> ms
slow_marker() {
	if [ "$1" = off ]; then rm -f "$RUN/test-slow-marker"; else echo "$1" >"$RUN/test-slow-marker"; fi
}
# slow_intent <ms>|off: hold every second READ_INTENT ganesha.nfsd sends for <ms> ms
slow_intent() {
	if [ "$1" = off ]; then rm -f "$RUN/test-slow-intent"; else echo "$1" >"$RUN/test-slow-intent"; fi
}
# slow_park_return <ms>|off: every parking read2 sleeps <ms> ms just before it returns
slow_park_return() {
	if [ "$1" = off ]; then rm -f "$RUN/test-slow-park-return"; else echo "$1" >"$RUN/test-slow-park-return"; fi
}
