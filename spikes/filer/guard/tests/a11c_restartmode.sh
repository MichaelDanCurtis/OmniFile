#!/bin/bash
# A11f-3: guard kill -9 with RestartMode=direct, for both smbd drop-in variants.
PW1='spike-geo1-pw'   # test-only local account (see a11_a12_samba.sh)
. "$(dirname "$0")/lib.sh"
U=omni-guard-a; MNT=/mnt/omni-a
smbpid() { systemctl show -p MainPID --value smbd; }
gpid() { systemctl show -p MainPID --value $U; }
mkdir -p /etc/systemd/system/$U.service.d
cp /work/spikes/filer/guard/systemd/omni-guard-a-restartmode.conf /etc/systemd/system/$U.service.d/restartmode.conf
for v in smbd-omni-guard.conf smbd-omni-guard-requires.conf; do
  say "RestartMode=direct + $v: kill -9 the guard during two waiting SMB reads"
  umount -l $MNT 2>/dev/null
  cp /work/spikes/filer/guard/systemd/$v /etc/systemd/system/smbd.service.d/omni-guard.conf
  systemctl daemon-reload; release_unit
  mkstubs >/dev/null; chmod 666 $DATA/seis/*; dropcache; policy wait; delay 0
  systemctl start smbd
  mount -t cifs //127.0.0.1/omni $MNT -o username=geo1,password=$PW1,vers=3.1.1,cache=none,uid=0
  delay 8000
  S0=$(smbpid); G0=$(gpid)
  $T check $MNT/seis/line_2g.segy --off 100M --len 1M --seed 2 > $LOGS/c1.out 2>&1 & C=$!
  sleep 2; kill -9 $G0
  sleep 1; $T check $MNT/seis/line_2g.segy --off 300M --len 1M --seed 2 > $LOGS/c2.out 2>&1 & C2=$!
  wait $C; cat $LOGS/c1.out; wait $C2; cat $LOGS/c2.out
  sleep 3
  echo "smbd MainPID before=$S0 after=$(smbpid) ($(systemctl is-active smbd)); guard $G0 -> $(gpid) ($(systemctl is-active $U))"
  journalctl -u $U -u smbd --since "-20s" -o short-precise | grep -E "systemd\[1\]" | sed 's/lima-omnifile-spike //' | head -8
  delay 0
  say "same variant: explicit 'systemctl stop $U' still stops smbd first?"
  systemctl stop $U; echo "guard=$(systemctl is-active $U) smbd=$(systemctl is-active smbd) fdstore=$(systemctl show -p NFileDescriptorStore --value $U)"
  systemctl start smbd
done
say "keep: RestartMode=direct + Requires/BindsTo/After"
cp /work/spikes/filer/guard/systemd/smbd-omni-guard.conf /etc/systemd/system/smbd.service.d/omni-guard.conf
systemctl daemon-reload; systemctl restart smbd
umount -l $MNT 2>/dev/null
