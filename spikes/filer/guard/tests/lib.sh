# Shared helpers for the part-A guard tests. Source from a root shell inside the VM.
set -u
A=/srv/omni-a
DATA=$A/data
BACKEND=$A/backend
STATE=$A/state
LOGS=$A/logs
BIN=$A/target/release
GUARD=$BIN/omnifile-guard
T=$BIN/omnitool
mkdir -p $LOGS $STATE $BACKEND

say() { echo; echo "=== $* ==="; }
policy() { echo "$1" > $STATE/policy; echo "policy=$1"; }
delay() { echo "$1" > $STATE/delay_ms; echo "backend delay_ms=$1"; }
dropcache() { sync; echo 3 > /proc/sys/vm/drop_caches; }

# plain (no systemd, no fd holder) guard in the background
start_plain() {
  pkill -9 -x omnifile-guard 2>/dev/null; sleep 0.3
  nohup $GUARD --fill-mount $A/.fill --private-ns "$@" >> $LOGS/guard-plain.log 2>&1 &
  GPID=$!
  for i in $(seq 1 50); do grep -q "guard armed" $LOGS/guard-plain.log 2>/dev/null && break; sleep 0.1; done
  echo "plain guard pid=$GPID"
}
stop_plain() { pkill -9 -x omnifile-guard 2>/dev/null; sleep 0.3; }

# fresh stubs used by most tests
mkstubs() {
  mkdir -p $DATA/seis
  rm -f $DATA/seis/small.segy $DATA/seis/line_2g.segy
  $T mkstub $DATA/seis/small.segy 64M 1M small.segy --seed 1 >/dev/null
  $T mkstub $DATA/seis/line_2g.segy 2G 4M line_2g.segy --seed 2 >/dev/null
  echo "stubs: small.segy (64M, 1M resident), line_2g.segy (2G, 4M resident)"
}
xa() { getfattr -d -m user.omnifile "$1" 2>/dev/null | grep -v '^#'; du -h --apparent-size "$1" | cut -f1; du -h "$1" | cut -f1; }

# Stop the systemd guard AND release its preserved group; otherwise every access to the volume
# blocks (fail-closed) because the group stays alive with nobody reading it.
release_unit() {
  systemctl stop smbd 2>/dev/null
  systemctl stop omni-guard-a 2>/dev/null
  systemctl clean --what=fdstore omni-guard-a 2>/dev/null
}
