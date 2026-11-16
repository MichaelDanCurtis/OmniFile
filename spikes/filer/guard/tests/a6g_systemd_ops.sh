#!/bin/bash
# A6g: fd store across systemd operations: restart, daemon-reload, daemon-reexec, while a reader waits.
. "$(dirname "$0")/lib.sh"
U=omni-guard-a
nstore() { systemctl show -p NFileDescriptorStore --value $U; }
policy wait; mkstubs; dropcache
systemctl start $U
n=0
for op in "systemctl restart $U" "systemctl daemon-reload" "systemctl daemon-reexec"; do
  n=$((n+1)); delay 6000
  $T check $DATA/seis/line_2g.segy --off $((n*100+900))M --len 1M --seed 2 > $LOGS/a6g.out &
  R=$!; sleep 1.5
  say "$op while a reader waits in a 6 s fill"
  $op; echo "rc=$? active=$(systemctl is-active $U) NFileDescriptorStore=$(nstore)"
  wait $R; cat $LOGS/a6g.out
done
delay 0
journalctl -u $U --since "-2min" -o cat | grep -E "REUSING|ORPHAN|created group|sweep:" 
