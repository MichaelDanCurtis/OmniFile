#!/usr/bin/env bash
# NFS grace-period reclaim across a server restart (docs/BUILD-PLAN.md M6,
# "NFS clients ride the grace period"). Runs as root on a Linux NFS CLIENT,
# against any OmniFile edge: the dev VM (the server on this machine) or an
# appliance (another machine). Only --restart-cmd changes between a Ganesha
# restart, an appliance reboot and an A/B-update reboot:
#
#   tests/e2e/nfs_grace_test.sh --server 127.0.0.1 --restart-cmd 'systemctl restart nfs-ganesha-omni'
#   tests/e2e/nfs_grace_test.sh --server 10.0.0.5 --restart-cmd 'ssh admin@10.0.0.5 sudo systemctl reboot' \
#     --client-net macvlan:eth0:10.0.0.61/24,10.0.0.62/24,10.0.0.63/24
#
# Three kernel NFS clients, each in its own network, UTS and mount namespace,
# so the server sees three machines (three NFSv4 client ids, three NLM hosts),
# each with its own rpcbind and rpc.statd:
#   A  holds, across the restart: N NFSv4.1 opens, each with an fcntl write
#      lock, M read-only opens (where a server grants delegations, M read
#      delegations), and K NFSv3 fcntl write locks (NLM). A is cut off from
#      the server for --partition-a seconds after the restart, so B asks for
#      the same locks before A has reclaimed anything;
#   B  holds opens of the same files and asks for the same locks: before the
#      restart, during grace and after it. Every answer must be "no" until A
#      lets go; then every one must be "yes";
#   C  rides the restart on an NFSv3 and an NFSv4.1 hard mount, writing,
#      fsyncing and reading back throughout: no application error, no wrong
#      byte. Three riders: NFSv3 writes through new opens; NFSv3 rewrites with
#      O_TRUNC (a truncate, which Ganesha holds off with JUKEBOX until grace
#      ends); NFSv4.1 writes through one fd held across the restart.
# Counts come from the kernel's own tracepoints (nfs4:*reclaim*, nfs4_set_lock,
# nfs4_set_delegation, lockd:nlmclnt_lock), attributed to each client by its
# mount's device number (NFSv4) or its locking process (NLM).
#
# What it checks (each line of the results file is one check):
#   1. every client mounts; A's locks are granted; B is refused each of them;
#   2. the restart: the server goes away and comes back (--restart-cmd);
#   3. during grace, B is never granted a lock A holds (NFS4ERR_GRACE and
#      NLM4_DENIED_GRACE_PERIOD are retried by the kernel; no attempt returns
#      success) while A cannot reach the server;
#   4. A reclaims every open and every lock (NFSv4: CLAIM_PREVIOUS opens and
#      reclaim LOCKs, error=0; NLM: the server's SM_NOTIFY reaches A's statd,
#      and A's lockd reclaims); A's fds still read and write inside the locked
#      ranges (a lost lock makes them fail with EIO);
#   5. after grace, B is still refused every lock; once A unlocks, B gets each;
#   6. delegations: how many the server granted and A reclaimed. FSAL_VFS (and
#      FSAL_OMNI over it) grants none, so the count is 0 on OmniFile today;
#   7. C's riders saw no error and no wrong byte; the longest cycle is reported.
#
# Options:
#   --server HOST          the NFS server (an address of this machine means the
#                          clients reach it over a local bridge, ogr0)
#   --restart-cmd CMD      run with bash -c on this machine
#   --v4-path PATH         NFSv4 pseudo path of the share (default /e2e/wait)
#   --v3-path PATH         NFSv3 export path (default /srv/omnifile/data/wait)
#   --uid U --gid G        the user the clients act as (default 20002:10000; the
#                          shares squash root)
#   --files N              A's NFSv4.1 opens with locks (default 8)
#   --deleg-files M        A's read-only opens (default 4)
#   --v3-files K           A's NFSv3 locks (default 4; 0, or --no-nlm, skips NLM)
#   --partition-a S        seconds A is cut off after the server is back (20)
#   --grace-timeout S      for the server to come back, and for grace to end (600)
#   --client-net MODE      bridge (default; the server is on this machine) or
#                          macvlan:IFACE:IPA/NN,IPB/NN,IPC/NN[:GATEWAY]
#   --server-log-cmd CMD   optional; prints the server's log since {since} (epoch
#                          seconds), e.g. journalctl -u nfs-ganesha-omni --since @{since}
#   --results FILE         default tests/e2e/results/grace-latest.md
#   --label TEXT           a line for the results (which config, which restart)
#   --keep                 leave the namespaces and mounts up
set -uo pipefail
if [ -z "${E2E_COPY:-}" ]; then
  copy=$(mktemp /tmp/omnifile-grace-XXXXXX)
  cp "$0" "$copy"
  E2E_COPY=1 E2E_SELF_SHA=$(sha256sum "$0" | cut -d' ' -f1) \
    E2E_HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd) exec bash "$copy" "$@"
fi
# The private copy removes itself at once: bash goes on reading it through its open fd,
# and nothing is left in /tmp for anyone to run again.
case "$0" in /tmp/omnifile-grace-??????) rm -f -- "$0" ;; esac
HERE=${E2E_HERE:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}

# --- checks with no side effects (also run by --self-test) ---------------------------
# A rider's last report: it ran, saw no error, and finished. A rider that hung never
# writes done, and must fail.
rider_ok() { python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d["errors"] == 0 and d["ops"] > 0 and d.get("done") else 1)' "$1"; }
# How many of B's attempts tagged TAG have been granted so far: each attempt writes
# att.TAG.N.json the moment it returns.
granted_so_far() { python3 -c '
import glob, json, os, sys
print(sum(1 for p in glob.glob(os.path.join(sys.argv[1], f"att.{sys.argv[2]}.*.json"))
          if json.load(open(p))["result"] == "GOT"))' "$1" "$2"; }
# Is PID in this shell's own network namespace? Unreadable counts as yes, so a reused pid
# or a missing /proc can never make ns_down kill the host's processes.
same_netns() {
  local a b
  a=$(readlink "/proc/$1/ns/net" 2>/dev/null) b=$(readlink /proc/self/ns/net 2>/dev/null)
  [ -z "$a" ] || [ -z "$b" ] || [ "$a" = "$b" ]
}
# The clients' processes (stdlib Python, run as the test user), written to FILE.
write_grace_py() {
  cat >"$1" <<'PYEOF'
"""Holder, contender and riders for nfs_grace_test.sh. Commands arrive as files
cmd.<name> in the control directory; each answer is done.<name>.json."""
import errno, fcntl, json, os, random, sys, threading, time

LOCKLEN = 4096


def wjson(path, obj):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.rename(tmp, path)


def commands(ctl):
    seen = set()
    while True:
        for n in sorted(os.listdir(ctl)):
            if n.startswith("cmd.") and n not in seen:
                seen.add(n)
                yield n[4:]
        time.sleep(0.1)


def ename(e):
    return errno.errorcode.get(e.errno, str(e.errno))


def lock(fd):
    fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, LOCKLEN, 0, 0)


def unlock(fd):
    fcntl.lockf(fd, fcntl.LOCK_UN, LOCKLEN, 0, 0)


def holder(ctl, v4, deleg, v3):
    st = {"pid": os.getpid(), "locks": {}, "opens": {}, "errors": []}
    fds = {}
    for p in v4 + v3:
        try:
            fd = os.open(p, os.O_RDWR)
            lock(fd)
            os.pwrite(fd, b"A holds %s\n" % os.path.basename(p).encode(), 0)
            os.fsync(fd)
            fds[p] = fd
            st["locks"][p] = "granted"
        except OSError as e:
            st["errors"].append([p, ename(e)])
    for p in deleg:
        try:
            fd = os.open(p, os.O_RDONLY)
            os.pread(fd, 16, 0)
            fds[p] = fd
            st["opens"][p] = "open"
        except OSError as e:
            st["errors"].append([p, ename(e)])
    wjson(os.path.join(ctl, "holder.json"), st)
    for c in commands(ctl):
        if c == "verify":
            res = {}
            for p, fd in fds.items():
                t = time.time()
                try:
                    if p in deleg:
                        os.pread(fd, 16, 0)
                        res[p] = ["ok", round(time.time() - t, 2)]
                        continue
                    data = b"A after %d\n" % int(t)
                    os.pwrite(fd, data, 0)
                    os.fsync(fd)
                    back = os.pread(fd, len(data), 0)
                    res[p] = ["ok" if back == data else "MISMATCH", round(time.time() - t, 2)]
                except OSError as e:
                    res[p] = [ename(e), round(time.time() - t, 2)]
            wjson(os.path.join(ctl, "done.verify.json"), res)
        elif c in ("release4", "release3"):
            res = {}
            for p in v4 if c == "release4" else v3:
                try:
                    unlock(fds[p])
                    res[p] = "unlocked"
                except (OSError, KeyError) as e:
                    res[p] = getattr(e, "errno", None) and ename(e) or "no fd"
            wjson(os.path.join(ctl, f"done.{c}.json"), res)
        elif c == "exit":
            return


def contender(ctl, files):
    fds = {}
    st = {"pid": os.getpid(), "errors": []}
    for p in files:
        try:
            fds[p] = os.open(p, os.O_RDWR)
        except OSError as e:
            st["errors"].append([p, ename(e)])
    wjson(os.path.join(ctl, "contender.json"), st)

    def attempt(p, out, tag, i):
        t = time.time()
        try:
            lock(fds[p])
            r = "GOT"
            unlock(fds[p])
        except OSError as e:
            r = ename(e)
        out[p] = {"result": r, "t0": round(t, 2), "t1": round(time.time(), 2)}
        # Each answer on its own, the moment it comes: a lock granted early in grace
        # shows at once, not only when every attempt has ended.
        wjson(os.path.join(ctl, f"att.{tag}.{i}.json"), {"file": p, **out[p]})

    def run(tag):
        out = {}
        ts = [threading.Thread(target=attempt, args=(p, out, tag, i), daemon=True)
              for i, p in enumerate(fds)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        wjson(os.path.join(ctl, f"done.{tag}.json"), out)

    for c in commands(ctl):
        if c == "exit":
            return
        threading.Thread(target=run, args=(c,), daemon=True).start()


def rider(kind, path, ctl, name):
    """trunc: open(O_TRUNC)/write/fsync/close/reread small files in rotation (an
    NFSv3 truncate is a SETATTR of the size, which Ganesha answers JUKEBOX while in
    grace); write: pwrite+fsync+pread at a rolling offset through a new open of one
    file each time; held: one open fd kept, pwrite+fsync+pread. Records every
    error and the longest single cycle."""
    ops = errs = 0
    worst = 0.0
    errors = []
    if kind in ("held", "write"):
        with open(path, "wb") as f:
            f.truncate(64 * 65536)
    fd = os.open(path, os.O_RDWR) if kind == "held" else None
    stop = os.path.join(ctl, f"cmd.stop-{name}")
    i = 0
    last = time.time()
    while not os.path.exists(stop):
        data = random.Random(i).randbytes(65536)
        t = time.time()
        try:
            if kind == "trunc":
                p = os.path.join(path, f"r{i % 8}.bin")
                with open(p, "wb") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                with open(p, "rb") as f:
                    back = f.read()
            elif kind == "write":
                off = (i % 64) * 65536
                wfd = os.open(path, os.O_RDWR)
                try:
                    os.pwrite(wfd, data, off)
                    os.fsync(wfd)
                    back = os.pread(wfd, 65536, off)
                finally:
                    os.close(wfd)
            else:
                off = (i % 64) * 65536
                os.pwrite(fd, data, off)
                os.fsync(fd)
                back = os.pread(fd, 65536, off)
            if back != data:
                errs += 1
                errors.append([round(t, 2), "WRONG BYTES"])
            ops += 1
        except OSError as e:
            errs += 1
            errors.append([round(t, 2), ename(e)])
        worst = max(worst, time.time() - t)
        i += 1
        if time.time() - last > 2:
            wjson(os.path.join(ctl, f"rider-{name}.json"),
                  {"ops": ops, "errors": errs, "worst_s": round(worst, 2), "list": errors[:20]})
            last = time.time()
        time.sleep(0.2)
    wjson(os.path.join(ctl, f"rider-{name}.json"),
          {"ops": ops, "errors": errs, "worst_s": round(worst, 2), "list": errors[:20], "done": True})


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[0] == "holder":
        holder(a[1], [x for x in a[2].split(",") if x], [x for x in a[3].split(",") if x],
               [x for x in a[4].split(",") if x])
    elif a[0] == "contender":
        contender(a[1], [x for x in a[2].split(",") if x])
    elif a[0] == "rider":
        rider(a[1], a[2], a[3], a[4])
PYEOF
}

ns_down() { # client
  local c=$1 pid=${NSPID[$1]:-}
  [ -n "$pid" ] || return 0
  # A pid reused after the namespace's holder died would be in the host's namespace:
  # killing "everything in its namespace" would then kill the host's processes.
  if same_netns "$pid"; then
    say "client $c: pid $pid is in this host's network namespace; not touching it"
    NSPID[$c]=""
    return 0
  fi
  for m in $(in_ns "$c" findmnt -rn -t nfs,nfs4 -o TARGET 2>/dev/null | grep omnifile-grace); do
    in_ns "$c" timeout 20 umount "$m" 2>/dev/null || in_ns "$c" umount -f -l "$m" 2>/dev/null
  done
  # Everything that runs in the namespace: the holder sleep's children and nsenter'd processes.
  for p in $(ps -eo pid=); do
    [ "$p" = "$pid" ] && continue
    [ "$(readlink "/proc/$p/ns/net" 2>/dev/null)" = "$(readlink "/proc/$pid/ns/net" 2>/dev/null)" ] && kill -9 "$p" 2>/dev/null
  done
  kill -9 "$pid" 2>/dev/null
  ip link del "og-$c" 2>/dev/null
  NSPID[$c]=""
}

if [ "${1:-}" = --self-test ]; then
  st=$(mktemp -d)
  bad=0
  t() { if "${@:2}"; then echo "ok: $1"; else echo "FAIL: $1"; bad=1; fi; }
  echo '{"ops": 5, "errors": 0}' >"$st/hung.json"
  echo '{"ops": 5, "errors": 0, "done": true}' >"$st/done.json"
  echo '{"ops": 5, "errors": 1, "done": true}' >"$st/err.json"
  not() { ! "$@"; }
  t "a rider that never finished fails" not rider_ok "$st/hung.json"
  t "a finished rider with no error passes" rider_ok "$st/done.json"
  t "a rider with an error fails" not rider_ok "$st/err.json"
  t "nothing answered yet: none granted" test "$(granted_so_far "$st" ingrace)" -eq 0
  echo '{"file": "x", "result": "EAGAIN"}' >"$st/att.ingrace.0.json"
  echo '{"file": "y", "result": "GOT"}' >"$st/att.ingrace.1.json"
  t "a lock granted early is seen before the other attempts end" test "$(granted_so_far "$st" ingrace)" -eq 1
  t "this shell's own pid is in the host namespace" same_netns $$

  # B's contender, for real on local files: each attempt writes its own answer.
  write_grace_py "$st/grace.py"
  mkdir "$st/ctl"
  : >"$st/f0" && : >"$st/f1" && : >"$st/f2"
  python3 "$st/grace.py" contender "$st/ctl" "$st/f0,$st/f1,$st/f2" </dev/null >"$st/contender.log" 2>&1 &
  cpid=$!
  answered() { find "$st/ctl" -maxdepth 1 -name 'att.ingrace.*.json' | wc -l | tr -d ' '; }
  for _ in $(seq 100); do [ -s "$st/ctl/contender.json" ] && break; sleep 0.1; done
  touch "$st/ctl/cmd.ingrace"
  for _ in $(seq 100); do [ "$(answered)" -eq 3 ] && break; sleep 0.1; done
  t "the contender writes each attempt's answer as att.ingrace.N.json" test "$(answered)" -eq 3
  t "granted_so_far counts the contender's own answers" test "$(granted_so_far "$st/ctl" ingrace)" -eq 3
  touch "$st/ctl/cmd.exit"
  # A contender that never exits fails the self-test instead of hanging it (bounded, 10 s).
  gone() { ! kill -0 "$cpid" 2>/dev/null; }
  for _ in $(seq 100); do gone && break; sleep 0.1; done
  t "the contender exits when told" gone
  kill -9 "$cpid" 2>/dev/null
  wait "$cpid" 2>/dev/null

  # ns_down given a pid in this shell's own namespace: it must touch nothing. Everything it
  # could run is a stub here that only records the call (ps names pid 1 and this shell).
  : >"$st/calls"
  rec() { echo "$*" >>"$st/calls"; }
  say() { :; }
  kill() { rec kill "$@"; }
  ip() { rec ip "$@"; }
  in_ns() { rec in_ns "$@"; }
  ps() { rec ps "$@"; printf '%s\n' 1 $$; }
  NSPID=([0]=$$) # client "0": works whether NSPID is an indexed or associative array
  ns_down 0
  t "ns_down leaves a pid in this shell's own namespace alone" test ! -s "$st/calls"
  t "ns_down forgets that pid" test -z "${NSPID[0]}"
  unset -f kill ip in_ns ps
  rm -rf "$st"
  exit $bad
fi

SERVER=""
RESTART_CMD=""
V4_PATH=/e2e/wait
V3_PATH=/srv/omnifile/data/wait
TUID=20002
TGID=10000
NFILES=8
NDELEG=4
NV3=4
NLM=1
PARTITION_A=20
GRACE_TIMEOUT=600
CLIENT_NET=bridge
SERVER_LOG_CMD=""
RESULTS=$HERE/results/grace-latest.md
LABEL=""
KEEP=0
while [ $# -gt 0 ]; do
  case "$1" in
    --server) SERVER=$2; shift ;;
    --restart-cmd) RESTART_CMD=$2; shift ;;
    --v4-path) V4_PATH=$2; shift ;;
    --v3-path) V3_PATH=$2; shift ;;
    --uid) TUID=$2; shift ;;
    --gid) TGID=$2; shift ;;
    --files) NFILES=$2; shift ;;
    --deleg-files) NDELEG=$2; shift ;;
    --v3-files) NV3=$2; shift ;;
    --no-nlm) NLM=0 ;;
    --partition-a) PARTITION_A=$2; shift ;;
    --grace-timeout) GRACE_TIMEOUT=$2; shift ;;
    --client-net) CLIENT_NET=$2; shift ;;
    --server-log-cmd) SERVER_LOG_CMD=$2; shift ;;
    --results) RESULTS=$2; shift ;;
    --label) LABEL=$2; shift ;;
    --keep) KEEP=1 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
  shift
done
[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 2; }
[ -n "$SERVER" ] && [ -n "$RESTART_CMD" ] || { echo "--server and --restart-cmd are required" >&2; exit 2; }
[ "$NV3" -gt 0 ] || NLM=0
for t in unshare nsenter ip mount.nfs rpcbind rpc.statd rpcinfo python3 setpriv; do
  command -v "$t" >/dev/null || { echo "needs $t (apt install nfs-common rpcbind iproute2 util-linux python3)" >&2; exit 2; }
done

WORK=/var/tmp/omnifile-grace
TR=/sys/kernel/tracing/instances/omnifile-grace
BR=ogr0
NET=10.231.0
NPASS=0
NFAIL=0
NINFO=0
T0=$(date +%s)
rm -rf "$WORK"
install -d -m 0755 "$WORK"
: >"$WORK/checks.txt"
: >"$WORK/counts.txt"
mkdir -p "$(dirname "$RESULTS")"

say() { printf '[grace %4ss] %s\n' "$(($(date +%s) - T0))" "$*"; }
pass() { say "PASS: $*"; echo "| PASS | $* |" >>"$WORK/checks.txt"; NPASS=$((NPASS + 1)); }
fail() { say "FAIL: $*"; echo "| **FAIL** | $* |" >>"$WORK/checks.txt"; NFAIL=$((NFAIL + 1)); }
info() { say "INFO: $*"; echo "| info | $* |" >>"$WORK/checks.txt"; NINFO=$((NINFO + 1)); }
check() { local d=$1; shift; if "$@"; then pass "$d"; else fail "$d"; fi; }
count() { say "count: $1 = $2"; echo "| $1 | $2 |" >>"$WORK/counts.txt"; }
wait_for() {
  local t=$1; shift
  local end=$(($(date +%s) + t))
  while [ "$(date +%s)" -lt "$end" ]; do "$@" && return 0; sleep 0.5; done
  "$@"
}
j() { python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); v=eval(sys.argv[2]); print(v if not isinstance(v,(dict,list)) else json.dumps(v))' "$1" "$2"; }
jt() { python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if eval(sys.argv[2]) else 1)' "$1" "$2"; }

# --- the clients' processes (write_grace_py, above) ----------------------------------
write_grace_py "$WORK/grace.py"

# --- tracepoints count; parse them per client ---------------------------------------
cat >"$WORK/tracecount.py" <<'PYEOF'
"""Count the tracepoints nfs_grace_test.sh enables, per client: NFSv4 events by
the mount's device (fileid=MM:mm:...), NLM events by the locking pid (svid)."""
import json, re, sys
trace, devs, pids = sys.argv[1], json.loads(sys.argv[2]), json.loads(sys.argv[3])
since = float(sys.argv[4]) if len(sys.argv) > 4 else 0.0
out = {}
first = {}
def bump(k, ts):
    out[k] = out.get(k, 0) + 1
    first.setdefault(k, ts)
for line in open(trace, errors="replace"):
    m = re.search(r"\s(\d+\.\d+):\s+(\w+):\s(.*)$", line)
    if not m:
        continue
    ts, ev, rest = float(m.group(1)), m.group(2), m.group(3)
    if ts < since:
        continue
    who = None
    f = re.search(r"fileid=([0-9a-f]{2}:[0-9a-f]{2}):", rest)
    if f:
        who = devs.get(f.group(1))
    # NLM events carry no device, and a lock owner id (svid) lockd reassigns; the task
    # tells them apart: A's holder is one thread (its pid), B's attempts run in the
    # contender's threads, which Python names Thread-N.
    if ev.startswith("nlmclnt"):
        t = re.match(r"\s*(.+?)-(\d+)\s+\[", line)
        if t:
            who = pids.get(t.group(2)) or ("b" if t.group(1).startswith("Thread-") else None)
    err = re.search(r"error=(-?\d+) \((\w+)\)", rest)
    st = re.search(r"status=(\w+)", rest)
    tag = err.group(2) if err else (st.group(1) if st else "")
    bump(f"{who or '?'}:{ev}:{tag}", ts)
if len(sys.argv) > 5:
    print(json.dumps(first, sort_keys=True))
else:
    print(json.dumps(out, sort_keys=True))
PYEOF

# --- the client namespaces ------------------------------------------------------------
is_local() {
  case "$1" in localhost|127.*) return 0 ;; esac
  ip -o addr show | awk '{print $4}' | cut -d/ -f1 | grep -qx "$1"
}
declare -A NSPID IPOF MNTDEV
CLIENTS=(a b c)
SRV=$SERVER
MODE=${CLIENT_NET%%:*}
if [ "$MODE" = bridge ]; then
  is_local "$SERVER" || { echo "bridge mode needs the server on this machine; use --client-net macvlan:..." >&2; exit 2; }
  SRV=$NET.1
  i=11
  for c in "${CLIENTS[@]}"; do IPOF[$c]=$NET.$i/24; i=$((i + 1)); done
elif [ "$MODE" = macvlan ]; then
  IFS=: read -r _ MV_IF MV_IPS MV_GW <<<"$CLIENT_NET"
  IFS=, read -r -a mvips <<<"$MV_IPS"
  [ "${#mvips[@]}" -eq 3 ] || { echo "macvlan needs three client addresses" >&2; exit 2; }
  i=0
  for c in "${CLIENTS[@]}"; do IPOF[$c]=${mvips[$i]}; i=$((i + 1)); done
else
  echo "unknown --client-net $CLIENT_NET" >&2; exit 2
fi
SRV_NAME=$(hostname)
SRV_FQDN=$(hostname -f 2>/dev/null || hostname)

in_ns() { local c=$1; shift; nsenter -t "${NSPID[$c]}" -n -m -u "$@"; }
as_user() { local c=$1; shift; in_ns "$c" setpriv --reuid="$TUID" --regid="$TGID" --clear-groups "$@"; }
mnt() { echo "/mnt/omnifile-grace-$1$2"; } # client version

ns_up() { # client
  local c=$1 pid
  rm -f "$WORK/ns-$c.pid"
  unshare --net --uts --mount --propagation private --fork -- \
    bash -c "echo \$\$ >$WORK/ns-$c.pid; exec sleep infinity" &
  disown
  wait_for 10 test -s "$WORK/ns-$c.pid" || return 1
  pid=$(cat "$WORK/ns-$c.pid")
  NSPID[$c]=$pid
  nsenter -t "$pid" -u hostname "grace-$c-$$"
  if [ "$MODE" = bridge ]; then
    ip link add "og-$c" type veth peer name eth0 netns "$pid" || return 1
    ip link set "og-$c" master "$BR" up
    nsenter -t "$pid" -n ip addr add "${IPOF[$c]}" dev eth0
    nsenter -t "$pid" -n ip link set eth0 up
    nsenter -t "$pid" -n ip route add default via "$NET.1"
  else
    ip link add "og-$c" link "$MV_IF" type macvlan mode bridge || return 1
    ip link set "og-$c" netns "$pid"
    nsenter -t "$pid" -n ip addr add "${IPOF[$c]}" dev "og-$c"
    nsenter -t "$pid" -n ip link set "og-$c" up
    [ -n "${MV_GW:-}" ] && nsenter -t "$pid" -n ip route add default via "$MV_GW"
  fi
  nsenter -t "$pid" -n ip link set lo up
  # The client's own /run (rpcbind's socket, statd's pid), NSM state and /etc/hosts,
  # where the server's name resolves to the address this client mounts it by: the
  # server's SM_NOTIFY names it, and statd matches that name to the mount.
  { grep -vwE "$SRV_NAME|$SRV_FQDN" /etc/hosts; echo "$SRV $SRV_NAME $SRV_FQDN"; } >"$WORK/hosts-$c"
  nsenter -t "$pid" -m bash -c "
    mount -t tmpfs -o mode=0755 tmpfs /run &&
    mount -t tmpfs -o mode=0755 tmpfs /var/lib/nfs &&
    { install -d -o statd -m 0755 /var/lib/nfs/sm /var/lib/nfs/sm.bak 2>/dev/null ||
      install -d -m 0755 /var/lib/nfs/sm /var/lib/nfs/sm.bak; } &&
    mount --bind $WORK/hosts-$c /etc/hosts" || return 1
  # Started detached from this shell (setsid), so the test's own exit never waits on them.
  in_ns "$c" setsid rpcbind -f >"$WORK/rpcbind-$c.log" 2>&1 </dev/null &
  disown
  wait_for 10 in_ns "$c" rpcinfo -p 127.0.0.1 >/dev/null 2>&1 || return 1
  in_ns "$c" setsid rpc.statd --no-notify -F -d >"$WORK/statd-$c.log" 2>&1 </dev/null &
  disown
  wait_for 10 bash -c "nsenter -t $pid -n -m -u rpcinfo -p 127.0.0.1 | grep -q status"
}
cut_off() { # client on|off: A loses its link to the server (packets dropped, nothing refused)
  if [ "$MODE" = bridge ]; then
    if [ "$2" = on ]; then ip link set "og-$1" down; else ip link set "og-$1" up; fi
  else
    if [ "$2" = on ]; then in_ns "$1" ip link set "og-$1" down; else in_ns "$1" ip link set "og-$1" up; fi
  fi
}
trace_on() {
  [ -d /sys/kernel/tracing ] || mount -t tracefs nodev /sys/kernel/tracing 2>/dev/null
  rmdir "$TR" 2>/dev/null
  mkdir "$TR" || return 1
  echo 16384 >"$TR/buffer_size_kb"
  # CLOCK_BOOTTIME, the clock of /proc/uptime: events are filtered by when the restart began.
  echo boot >"$TR/trace_clock" 2>/dev/null
  local e
  for e in nfs4/nfs4_open_reclaim nfs4/nfs4_state_lock_reclaim nfs4/nfs4_reclaim_delegation \
    nfs4/nfs4_set_delegation nfs4/nfs4_set_lock nfs4/nfs4_reclaim_complete nfs4/nfs4_cb_recall \
    nfs4/nfs4_delegreturn lockd/nlmclnt_lock lockd/nlmclnt_unlock; do
    [ -e "$TR/events/$e/enable" ] && echo 1 >"$TR/events/$e/enable"
  done
  echo 1 >"$TR/tracing_on"
}
cleanup() {
  [ "$KEEP" = 1 ] && return
  for c in "${CLIENTS[@]}"; do touch "$WORK/ctl-$c/cmd.exit" "$WORK/ctl-$c/cmd.stop-c3" "$WORK/ctl-$c/cmd.stop-c4" 2>/dev/null; done
  sleep 1
  for c in "${CLIENTS[@]}"; do cut_off "$c" off 2>/dev/null; ns_down "$c"; done
  [ "$MODE" = bridge ] && ip link del "$BR" 2>/dev/null
  [ -d "$TR" ] && { echo 0 >"$TR/tracing_on"; rmdir "$TR" 2>/dev/null; }
}
trap cleanup EXIT

finish() {
  {
    echo "# NFS grace reclaim: $(date -u +%FT%TZ)"
    echo
    echo "server $SERVER (clients reach it as $SRV, $MODE), restart: \`$RESTART_CMD\`"
    [ -n "$LABEL" ] && { echo; echo "$LABEL"; }
    echo
    echo "client kernel $(uname -r), script ${E2E_SELF_SHA:-?}; v4 $V4_PATH, v3 $V3_PATH; A holds $NFILES v4.1 locks, $NDELEG read opens, $NV3 NLM locks (NLM $([ "$NLM" = 1 ] && echo on || echo off)); A cut off ${PARTITION_A}s after the restart"
    echo
    echo "| result | check |"
    echo "|---|---|"
    cat "$WORK/checks.txt"
    echo
    echo "| count | value |"
    echo "|---|---|"
    cat "$WORK/counts.txt"
    echo
    echo "$NPASS passed, $NFAIL failed, $NINFO notes"
  } >"$RESULTS"
  say "$NPASS passed, $NFAIL failed ($RESULTS)"
  [ "$NFAIL" -eq 0 ]
  exit
}

# ---------------------------------------------------------------------------------
say "1. three client machines (namespaces), each with rpcbind and statd"
if [ "$MODE" = bridge ]; then
  ip link del "$BR" 2>/dev/null
  ip link add "$BR" type bridge && ip addr add "$NET.1/24" dev "$BR" && ip link set "$BR" up ||
    { fail "bridge $BR"; finish; }
  # No name service on the test bridge. rpc.statd looks up the name of every client it
  # is asked to monitor (SM_MON); systemd-resolved would ask by LLMNR and mDNS on this
  # link too, and leave statd's PTR query unanswered past Ganesha's SM_MON timeout:
  # every NFSv3 lock would then fail with ENOLCK (measured: 10 s per lookup).
  if command -v resolvectl >/dev/null; then
    resolvectl llmnr "$BR" no 2>/dev/null
    resolvectl mdns "$BR" no 2>/dev/null
  fi
fi
for c in "${CLIENTS[@]}"; do
  ns_up "$c" || { fail "client namespace $c"; finish; }
done
pass "clients a, b, c up (${IPOF[a]}, ${IPOF[b]}, ${IPOF[c]}), each with its own rpcbind and rpc.statd"
check "the server answers NFS from a client (rpcinfo -T tcp $SRV nfs 4)" wait_for 30 in_ns b rpcinfo -T tcp "$SRV" 100003 4
if [ "$NLM" = 1 ]; then
  if in_ns b rpcinfo -p "$SRV" 2>/dev/null | grep -q nlockmgr; then
    pass "the server registers NLM (nlockmgr) with its rpcbind"
  else
    fail "the server has no NLM (Enable_NLM = false?): NFSv3 locks cannot work; rerun with --no-nlm to test the rest"
    finish
  fi
fi

say "2. mounts: NFSv4.1 and NFSv3, hard"
V3OPT="vers=3,proto=tcp,hard,timeo=50"
[ "$NLM" = 1 ] || V3OPT="$V3OPT,nolock"
for c in "${CLIENTS[@]}"; do
  in_ns "$c" install -d "$(mnt "$c" 4)" "$(mnt "$c" 3)"
  in_ns "$c" mount -t nfs -o vers=4.1,proto=tcp,hard,timeo=50 "$SRV:$V4_PATH" "$(mnt "$c" 4)" ||
    { fail "client $c: mount v4.1 $SRV:$V4_PATH"; finish; }
  in_ns "$c" mount -t nfs -o "$V3OPT" "$SRV:$V3_PATH" "$(mnt "$c" 3)" ||
    { fail "client $c: mount v3 $SRV:$V3_PATH ($V3OPT)"; finish; }
  d=$(in_ns "$c" stat -c '%Hd %Ld' "$(mnt "$c" 4)")
  MNTDEV[$c]=$(printf '%02x:%02x' ${d% *} ${d#* })
done
pass "each client mounts the share over NFSv4.1 and NFSv3 ($V3OPT)"

DIR=grace-$T0-$$
F4=() FD=() F3=()
for i in $(seq 1 "$NFILES"); do F4+=("$DIR/lock-$i"); done
for i in $(seq 1 "$NDELEG"); do FD+=("$DIR/deleg-$i"); done
[ "$NLM" = 1 ] && for i in $(seq 1 "$NV3"); do F3+=("$DIR/v3lock-$i"); done
as_user a mkdir "$(mnt a 4)/$DIR" || { fail "mkdir $DIR as $TUID"; finish; }
for f in "${F4[@]}" "${FD[@]}" "${F3[@]}"; do
  as_user a bash -c "head -c 65536 /dev/urandom >'$(mnt a 4)/$f'" || { fail "create $f"; finish; }
done
as_user a mkdir "$(mnt a 4)/$DIR/rider-trunc"
pathlist() { local m=$1 out="" f; shift; for f in "$@"; do out="$out,$m/$f"; done; echo "${out#,}"; }
A4=$(pathlist "$(mnt a 4)" "${F4[@]}")
AD=$(pathlist "$(mnt a 4)" "${FD[@]}")
A3=$(pathlist "$(mnt a 3)" "${F3[@]}")
B_ALL=$(pathlist "$(mnt b 4)" "${F4[@]}")
[ "$NLM" = 1 ] && B_ALL="$B_ALL,$(pathlist "$(mnt b 3)" "${F3[@]}")"
NLOCKS=$((NFILES + ${#F3[@]}))
for c in "${CLIENTS[@]}"; do install -d -o "$TUID" -g "$TGID" -m 0755 "$WORK/ctl-$c"; done

say "3. A takes its opens and locks; B is refused each lock"
trace_on || { fail "tracefs instance $TR"; finish; }

as_user a setsid python3 "$WORK/grace.py" holder "$WORK/ctl-a" "$A4" "$AD" "$A3" </dev/null >"$WORK/holder.log" 2>&1 &
disown
wait_for 60 test -s "$WORK/ctl-a/holder.json" || { fail "A's holder never reported: $(cat "$WORK/holder.log")"; finish; }
HPID=$(j "$WORK/ctl-a/holder.json" 'd["pid"]')
check "A holds $NFILES NFSv4.1 opens with fcntl write locks and ${#F3[@]} NFSv3 (NLM) locks, and $NDELEG read-only opens" \
  jt "$WORK/ctl-a/holder.json" "len(d['locks']) == $NLOCKS and len(d['opens']) == $NDELEG and not d['errors']"
[ -s "$WORK/ctl-a/holder.json" ] && jt "$WORK/ctl-a/holder.json" "d['errors']" && say "holder errors: $(j "$WORK/ctl-a/holder.json" 'd["errors"]')"
as_user b setsid python3 "$WORK/grace.py" contender "$WORK/ctl-b" "$B_ALL" </dev/null >"$WORK/contender.log" 2>&1 &
disown
wait_for 60 test -s "$WORK/ctl-b/contender.json" || { fail "B's contender never reported"; finish; }
CPID=$(j "$WORK/ctl-b/contender.json" 'd["pid"]')
touch "$WORK/ctl-b/cmd.before"
wait_for 60 test -s "$WORK/ctl-b/done.before.json"
refused() { jt "$1" "len(d) == $NLOCKS and all(v['result'] in ('EAGAIN', 'EACCES') for v in d.values())"; }
check "before the restart, B is refused every one of A's $NLOCKS locks (EAGAIN)" refused "$WORK/ctl-b/done.before.json"

say "4. C starts riding on NFSv3 and NFSv4.1"
# name kind mount path
RIDERS=("v3-write write 3 $DIR/rider-w3.bin" "v3-trunc trunc 3 $DIR/rider-trunc" "v41-held held 4 $DIR/rider-h4.bin")
for r in "${RIDERS[@]}"; do
  read -r rn rk rv rp <<<"$r"
  as_user c setsid python3 "$WORK/grace.py" rider "$rk" "$(mnt c "$rv")/$rp" "$WORK/ctl-c" "$rn" </dev/null >"$WORK/rider-$rn.log" 2>&1 &
  disown
done
for r in "${RIDERS[@]}"; do
  read -r rn _ <<<"$r"
  wait_for 30 test -s "$WORK/ctl-c/rider-$rn.json" || fail "rider $rn never reported: $(cat "$WORK/rider-$rn.log")"
done

say "5. A loses its link; the server restarts: $RESTART_CMD"
cut_off a on
TR0=$(date +%s)
TRACE_R=$(awk '{print $1}' /proc/uptime)
out=$(bash -c "$RESTART_CMD" 2>&1); rc=$?
say "restart command exited $rc after $(($(date +%s) - TR0)) s${out:+: $(tail -n 2 <<<"$out" | tr '\n' ' ')}"
check "the restart command succeeded (exit $rc)" test "$rc" -eq 0
# For a reboot, the server may still answer for a moment: wait until it has been away.
sleep 2
back() { in_ns b rpcinfo -T tcp "$SRV" 100003 4 >/dev/null 2>&1; }
if wait_for "$GRACE_TIMEOUT" back; then
  TB=$(date +%s)
  pass "the server is back $((TB - TR0)) s after the restart began"
else
  fail "the server never came back within $GRACE_TIMEOUT s"; finish
fi
if [ "$NLM" = 1 ]; then
  check "and registers NLM again" wait_for 60 bash -c "nsenter -t ${NSPID[b]} -n -m -u rpcinfo -p $SRV 2>/dev/null | grep -q nlockmgr"
fi

say "6. during grace, with A cut off, B asks for every one of A's locks"
touch "$WORK/ctl-b/cmd.ingrace"
sleep "$PARTITION_A"
granted_early=$(granted_so_far "$WORK/ctl-b" ingrace)
answered_early=$(find "$WORK/ctl-b" -maxdepth 1 -name 'att.ingrace.*.json' | wc -l)
check "while A could not reclaim (${PARTITION_A} s), B was granted none of A's locks ($answered_early of $NLOCKS attempts answered by then, $granted_early granted)" \
  test "$granted_early" -eq 0
cut_off a off
TA=$(date +%s)
TA_UP=$(awk '{print $1}' /proc/uptime)

say "7. A is back: it reclaims; its fds still write inside the locks"
touch "$WORK/ctl-a/cmd.verify"
if wait_for "$GRACE_TIMEOUT" test -s "$WORK/ctl-a/done.verify.json"; then
  check "A's $((NLOCKS + NDELEG)) fds all work after the restart (pwrite+fsync+pread inside each lock; a lost lock gives EIO)" \
    jt "$WORK/ctl-a/done.verify.json" "len(d) == $((NLOCKS + NDELEG)) and all(v[0] == 'ok' for v in d.values())"
  say "A's verify: $(j "$WORK/ctl-a/done.verify.json" '{k.rsplit("/",1)[1]: v for k, v in d.items()}')"
else
  fail "A's fds never answered after the restart"
fi
say "8. B's in-grace attempts end once grace is over: still refused"
if wait_for "$GRACE_TIMEOUT" test -s "$WORK/ctl-b/done.ingrace.json"; then
  TG=$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(int(max(v["t1"] for v in d.values())))' "$WORK/ctl-b/done.ingrace.json")
  check "every in-grace attempt by B ended refused, none granted (answered $((TG - TB)) s after the server was back)" \
    refused "$WORK/ctl-b/done.ingrace.json"
  say "in grace: $(j "$WORK/ctl-b/done.ingrace.json" '{k.rsplit("/",1)[1]: [v["result"], round(v["t1"]-v["t0"],1)] for k, v in d.items()}')"
else
  fail "B's in-grace attempts never ended (grace never lifted within $GRACE_TIMEOUT s)"
fi
touch "$WORK/ctl-b/cmd.after"
wait_for 60 test -s "$WORK/ctl-b/done.after.json"
check "after grace, B is still refused every lock A reclaimed" refused "$WORK/ctl-b/done.after.json"

say "9. A lets go: B gets every lock"
touch "$WORK/ctl-a/cmd.release4"
wait_for 60 test -s "$WORK/ctl-a/done.release4.json"
[ "$NLM" = 1 ] && { touch "$WORK/ctl-a/cmd.release3"; wait_for 60 test -s "$WORK/ctl-a/done.release3.json"; }
touch "$WORK/ctl-b/cmd.released"
wait_for 120 test -s "$WORK/ctl-b/done.released.json"
check "once A unlocks, B is granted each of the $NLOCKS locks (the server's lock state is A's, not stuck)" \
  jt "$WORK/ctl-b/done.released.json" "len(d) == $NLOCKS and all(v['result'] == 'GOT' for v in d.values())"

say "10. the riders"
for r in "${RIDERS[@]}"; do read -r rn _ <<<"$r"; touch "$WORK/ctl-c/cmd.stop-$rn"; done
for r in "${RIDERS[@]}"; do
  read -r rn rk rv _ <<<"$r"
  wait_for 60 grep -q '"done": true' "$WORK/ctl-c/rider-$rn.json"
  case $rk in
    write) what="NFSv3, a new open, pwrite, fsync and read back each cycle" ;;
    trunc) what="NFSv3, open(O_TRUNC), write, fsync, close and read back each cycle (a truncate: Ganesha answers JUKEBOX until grace ends)" ;;
    held) what="NFSv4.1, one open fd held across the restart, pwrite, fsync and read back each cycle" ;;
  esac
  f=$WORK/ctl-c/rider-$rn.json
  check "rider $rn ($what) rode the restart and finished: $(j "$f" 'd["ops"]') cycles, no error, no wrong byte; longest cycle $(j "$f" 'd["worst_s"]') s" \
    rider_ok "$f"
  jt "$f" "d['errors']" && say "rider $rn errors: $(j "$f" 'd["list"]')"
  count "rider $rn: cycles / errors / longest cycle (s)" "$(j "$f" '"%d / %d / %s" % (d["ops"], d["errors"], d["worst_s"])')"
done

say "11. counts from the kernel's tracepoints"
echo 0 >"$TR/tracing_on"
cp "$TR/trace" "$WORK/trace.txt"
devs=$(python3 -c 'import json,sys; print(json.dumps({sys.argv[1]: "a", sys.argv[2]: "b", sys.argv[3]: "c"}))' "${MNTDEV[a]}" "${MNTDEV[b]}" "${MNTDEV[c]}")
pids=$(python3 -c 'import json,sys; print(json.dumps({sys.argv[1]: "a", sys.argv[2]: "b"}))' "$HPID" "$CPID")
tc=$(python3 "$WORK/tracecount.py" "$WORK/trace.txt" "$devs" "$pids" "$TRACE_R")
echo "$tc" >"$WORK/tracecounts.json"
say "tracepoints after the restart: $tc"
tcv() { python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(sum(v for k, v in d.items() if all(p in k.split(":") for p in sys.argv[2:])))' "$WORK/tracecounts.json" "$@"; }
OPEN_RECL=$(tcv a nfs4_open_reclaim OK)
LOCK_STATES=$(tcv a nfs4_state_lock_reclaim)
# After the restart A asks for no new lock: each LOCK of A's answered OK is a reclaim.
LOCK_RECL=$(tcv a nfs4_set_lock OK)
B_GRACE=$(tcv b nfs4_set_lock GRACE)
B_DENIED=$(tcv b nfs4_set_lock DENIED)
DELEG_SET_BEFORE=$(python3 "$WORK/tracecount.py" "$WORK/trace.txt" "$devs" "$pids" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(sum(v for k, v in d.items() if k.startswith("a:nfs4_set_delegation")))')
DELEG_RECL=$(tcv a nfs4_reclaim_delegation)
count "NFSv4.1 opens A held / reclaimed (nfs4_open_reclaim, error=0)" "$((NFILES + NDELEG)) / $OPEN_RECL"
count "NFSv4.1 locks A held / lock states it reclaimed (nfs4_state_lock_reclaim) / reclaim LOCKs answered OK (nfs4_set_lock)" "$NFILES / $LOCK_STATES / $LOCK_RECL"
count "B's LOCK requests answered NFS4ERR_GRACE / NFS4ERR_DENIED after the restart" "$B_GRACE / $B_DENIED"
count "delegations the server granted A / A reclaimed" "$DELEG_SET_BEFORE / $DELEG_RECL"
check "A reclaimed every NFSv4.1 open it held ($OPEN_RECL of $((NFILES + NDELEG)))" test "$OPEN_RECL" -ge $((NFILES + NDELEG))
check "A reclaimed every NFSv4.1 lock it held ($LOCK_RECL of $NFILES)" test "$LOCK_RECL" -ge "$NFILES"
check "B's in-grace LOCKs were answered GRACE by the server ($B_GRACE times), never granted" test "$B_GRACE" -gt 0
# A was cut off: its first reclaim must come after it rejoined, so B's GRACE answers came
# while A's state was not yet back. (Times are CLOCK_BOOTTIME, as /proc/uptime.)
A_FIRST=$(python3 "$WORK/tracecount.py" "$WORK/trace.txt" "$devs" "$pids" "$TRACE_R" first |
  python3 -c 'import json,sys; d=json.load(sys.stdin); v=[t for k, t in d.items() if k.startswith("a:nfs4_open_reclaim")]; print(min(v) if v else 0)')
B_FIRST_GRACE=$(python3 "$WORK/tracecount.py" "$WORK/trace.txt" "$devs" "$pids" "$TRACE_R" first |
  python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("b:nfs4_set_lock:GRACE", 0))')
check "A's first reclaim came after it rejoined (+$(awk -v a="$A_FIRST" -v r="$TRACE_R" 'BEGIN{printf "%.1f", a-r}') s), after B's first GRACE answer (+$(awk -v a="$B_FIRST_GRACE" -v r="$TRACE_R" 'BEGIN{printf "%.1f", a-r}') s)" \
  awk -v a="$A_FIRST" -v t="$TA_UP" -v b="$B_FIRST_GRACE" 'BEGIN{exit !(a >= t && b > 0 && b < a)}' 
if [ "$DELEG_SET_BEFORE" -eq 0 ]; then
  info "the server granted no delegation for A's $NDELEG read-only opens, so there was none to reclaim (Ganesha grants them only where the FSAL supports leases: FSAL_VFS, and FSAL_OMNI over it, does not; and NFSV4 Delegations defaults to false)"
else
  check "A reclaimed every delegation it held ($DELEG_RECL of $DELEG_SET_BEFORE)" test "$DELEG_RECL" -ge "$DELEG_SET_BEFORE"
fi
if [ "$NLM" = 1 ]; then
  # lockd traces each lock request once, with its final answer: an in-grace request is
  # retried inside the kernel (DENIED_GRACE_PERIOD) and traced when grace has ended.
  B_NLM_DENIED=$(tcv b nlmclnt_lock LCK_DENIED)
  B_NLM_GRANTED=$(tcv b nlmclnt_lock LCK_GRANTED)
  NOTIFY=$(grep -c 'Received SM_NOTIFY' "$WORK/statd-a.log" 2>/dev/null)
  count "SM_NOTIFY from the server received by A's statd" "$NOTIFY"
  count "B's NLM LOCK requests after the restart, final answer DENIED / GRANTED (nlmclnt_lock; the GRANTED ones after A let go)" "$B_NLM_DENIED / $B_NLM_GRANTED"
  check "the restarted server told A it had restarted (SM_NOTIFY reached A's statd $NOTIFY time(s)), so A's lockd reclaimed" test "${NOTIFY:-0}" -ge 1
  failed=$(dmesg --since "@$T0" 2>/dev/null | grep -c 'lockd: failed to reclaim' || true)
  check "no 'lockd: failed to reclaim' in the kernel log ($failed)" test "${failed:-0}" -eq 0
fi
if [ -n "$SERVER_LOG_CMD" ]; then
  slog=$(bash -c "${SERVER_LOG_CMD//\{since\}/$TR0}" 2>&1)
  echo "$slog" >"$WORK/server.log"
  # Ganesha logs "NFS Server Now IN GRACE, duration N" and "NFS Server Now NOT IN GRACE";
  # with an epoch first field (journalctl -o short-unix) the times are measured too.
  g_in=$(grep 'Now IN GRACE' <<<"$slog" | head -n 1)
  g_out=$(grep 'NOT IN GRACE' <<<"$slog" | head -n 1)
  t_in=$(awk '{print $1}' <<<"$g_in"); t_out=$(awk '{print $1}' <<<"$g_out")
  if [[ "$t_in" =~ ^[0-9.]+$ ]] && [[ "$t_out" =~ ^[0-9.]+$ ]]; then
    GRACE_S=$(awk -v a="$t_in" -v b="$t_out" 'BEGIN{printf "%.0f", b-a}')
    count "server grace (its log): began / ended, seconds after the restart began; length" \
      "$(awk -v a="$t_in" -v r="$TR0" 'BEGIN{printf "%.0f", a-r}') / $(awk -v a="$t_out" -v r="$TR0" 'BEGIN{printf "%.0f", a-r}'); $GRACE_S s"
  fi
  info "server log: '$(grep -o 'NFS Server Now IN GRACE[^:]*' <<<"$g_in" | head -n 1)' then '$(grep -o 'NFS Server Now NOT IN GRACE' <<<"$g_out" | head -n 1)'"
fi
count "seconds after the restart began: server back / A rejoined / B's in-grace attempts answered" \
  "$((TB - TR0)) / $((TA - TR0)) / $(( ${TG:-$TB} - TR0 ))"
finish
