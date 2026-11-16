#!/bin/bash
# A6c: does FileDescriptorStorePreserve=yes keep the group across "systemctl stop"?
. "$(dirname "$0")/lib.sh"
U=omni-guard-a
nstore() { systemctl show -p NFileDescriptorStore --value $U; }
try_stop() {
  systemctl start $U; sleep 0.5
  echo "running: NFileDescriptorStore=$(nstore)"
  systemctl stop $U
  echo "stopped: active=$(systemctl is-active $U) NFileDescriptorStore=$(nstore)"
  $T check $DATA/seis/line_2g.segy --off $1 --len 1M --seed 2 > $LOGS/a6c.out &
  R=$!; sleep 10
  echo "after 10 s: reader state=$(awk '{print $3}' /proc/$R/stat 2>/dev/null || echo exited) wchan=$(cat /proc/$R/wchan 2>/dev/null)"
  systemctl start $U; wait $R; cat $LOGS/a6c.out
  echo "restarted: NFileDescriptorStore=$(nstore)"
}
: > $STATE/guard.env; policy wait; delay 0
mkstubs; dropcache
say "A6c-1: unit NOT enabled, nothing references it"
systemctl disable $U 2>/dev/null
try_stop 500M
say "A6c-2: unit enabled (WantedBy=multi-user.target references it)"
systemctl stop $U; systemctl enable $U 2>&1 | tail -1
try_stop 600M
say "A6c-3: enabled, stop + 'systemctl clean --what=fdstore' (explicit release, A7 via systemd)"
systemctl stop $U
echo "stopped: NFileDescriptorStore=$(nstore)"
$T check $DATA/seis/line_2g.segy --off 700M --len 1M --seed 2 > $LOGS/a6c3.out &
R=$!; sleep 2
echo "reader blocked: state=$(awk '{print $3}' /proc/$R/stat)"
systemctl clean --what=fdstore $U; echo "clean rc=$? NFileDescriptorStore=$(nstore)"
wait $R; cat $LOGS/a6c3.out
systemctl start $U
