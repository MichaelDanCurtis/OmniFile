#!/bin/bash
# A6h: crash loop until the start limit trips (unit "failed"): what happens to the fd store,
# smbd and a waiting reader; then recovery with reset-failed + start.
. "$(dirname "$0")/lib.sh"
U=omni-guard-a
cp /work/spikes/filer/guard/systemd/omni-guard-a.service /etc/systemd/system/
mkdir -p /etc/systemd/system/$U.service.d
printf '[Unit]\nStartLimitIntervalSec=60\nStartLimitBurst=2\n' > /etc/systemd/system/$U.service.d/startlimit.conf
systemctl daemon-reload; release_unit; systemctl reset-failed $U 2>/dev/null
mkstubs >/dev/null; chmod 666 $DATA/seis/*; dropcache; policy wait; delay 0
systemctl start smbd
gpid() { systemctl show -p MainPID --value $U; }
for i in 1 2 3 4; do
  for w in $(seq 1 25); do [ "$(gpid)" != 0 ] && systemctl is-active --quiet $U && break; sleep 0.2; done
  systemctl is-active --quiet $U || { echo "guard not coming back: $(systemctl is-active $U)"; break; }
  kill -9 $(gpid); echo "kill $i"; sleep 2.5
done
sleep 2
echo "guard=$(systemctl is-active $U) smbd=$(systemctl is-active smbd) fdstore=$(systemctl show -p NFileDescriptorStore --value $U)"
$T check $DATA/seis/line_2g.segy --off 100M --len 1M --seed 2 > $LOGS/a6h.out 2>&1 & R=$!
sleep 5; echo "local reader after 5 s: state=$(awk '{print $3}' /proc/$R/stat 2>/dev/null || echo exited)"
systemctl reset-failed $U; systemctl start smbd
wait $R; cat $LOGS/a6h.out
echo "guard=$(systemctl is-active $U) smbd=$(systemctl is-active smbd) fdstore=$(systemctl show -p NFileDescriptorStore --value $U)"
journalctl -u $U --since "-40s" -o cat | grep -E "REUSING|created group|start-limit|Start request repeated" | head
rm -f /etc/systemd/system/$U.service.d/startlimit.conf; systemctl daemon-reload
