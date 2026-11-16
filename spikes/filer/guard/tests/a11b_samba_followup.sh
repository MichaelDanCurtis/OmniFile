#!/bin/bash
# A11 follow-up: unit dependency variants under a guard crash, stop ordering, STATUS_PENDING on
# the wire, and A12 identity lookup while the SMB request is still waiting.
PW1='spike-geo1-pw'; PW2='spike-geo2-pw'   # test-only local accounts (see a11_a12_samba.sh)
. "$(dirname "$0")/lib.sh"
U=omni-guard-a; MNT=/mnt/omni-a
smbpid() { systemctl show -p MainPID --value smbd; }
gpid() { systemctl show -p MainPID --value $U; }
setup() {  # $1 = drop-in file
  umount -l $MNT 2>/dev/null
  cp /work/spikes/filer/guard/systemd/$1 /etc/systemd/system/smbd.service.d/omni-guard.conf
  systemctl daemon-reload; release_unit
  mkstubs >/dev/null; chmod 666 $DATA/seis/*; dropcache; policy wait; delay 0
  systemctl start smbd
  mount -t cifs //127.0.0.1/omni $MNT -o username=geo1,password=$PW1,vers=3.1.1,cache=none,uid=0
}
crash() {
  delay 8000
  S0=$(smbpid); G0=$(gpid)
  $T check $MNT/seis/line_2g.segy --off 100M --len 1M --seed 2 > $LOGS/c1.out 2>&1 & C=$!
  sleep 2; kill -9 $G0; echo "killed guard $G0 at $(date +%T)"
  sleep 1; $T check $MNT/seis/line_2g.segy --off 300M --len 1M --seed 2 > $LOGS/c2.out 2>&1 & C2=$!
  wait $C; cat $LOGS/c1.out; wait $C2; cat $LOGS/c2.out
  sleep 3
  echo "smbd MainPID before=$S0 after=$(smbpid) state=$(systemctl is-active smbd); guard $G0 -> $(gpid) state=$(systemctl is-active $U)"
  delay 0
}
say "A11f-1: drop-in with Requires+BindsTo+After, guard kill -9 during an SMB read"
setup smbd-omni-guard.conf; crash
say "A11f-2: drop-in with Requires+After only (no BindsTo), guard kill -9 during an SMB read"
setup smbd-omni-guard-requires.conf; crash

say "A11g: 'systemctl stop' of the guard while smbd runs: stop order and fd store"
T1=$(date '+%Y-%m-%d %H:%M:%S'); sleep 1
systemctl stop $U
echo "guard=$(systemctl is-active $U) smbd=$(systemctl is-active smbd) fdstore=$(systemctl show -p NFileDescriptorStore --value $U)"
journalctl --since "$T1" -o short-precise | grep -E "Stopping|Stopped" | sed 's/lima-omnifile-spike //'
T1=$(date '+%Y-%m-%d %H:%M:%S'); sleep 1
systemctl start smbd
journalctl --since "$T1" -o short-precise | grep -E "READY=1|Started" | sed 's/lima-omnifile-spike //'
umount -l $MNT 2>/dev/null
mount -t cifs //127.0.0.1/omni $MNT -o username=geo1,password=$PW1,vers=3.1.1,cache=none,uid=0

say "STATUS_PENDING: 30 s recall seen by the CIFS client and by smbclient (-t 120), on the wire"
mkstubs >/dev/null; chmod 666 $DATA/seis/*; dropcache; delay 30000; echo file > $STATE/delay_scope
rm -f /tmp/p.pcap; tcpdump -U -i lo -w /tmp/p.pcap -s 512 tcp port 445 >/dev/null 2>&1 & sleep 1
$T check $MNT/seis/small.segy --off 20M --len 1M --seed 1 > $LOGS/p1.out 2>&1 & C=$!
( time smbclient //127.0.0.1/omni -U geo2%$PW2 -m SMB3 -t 120 -c 'get seis/line_2g.segy /tmp/l2g.smbclient' ) > $LOGS/p2.out 2>&1 & S=$!
sleep 5
say "A12 while the smbclient request waits: event pid -> smbstatus"
for p in $(journalctl -u $U --since "-6s" -o cat | grep -o 'event fd=[0-9]* mask=[^ ]* pid=[0-9]*' | sed 's/.*pid=//' | sort -u); do echo "pid $p: $(smbstatus -p 2>/dev/null | awk -v p=$p '$1==p {print "user="$2, "machine="$4}')"; done
smbstatus -p 2>/dev/null | tail -n +5
wait $C; cat $LOGS/p1.out
wait $S; grep -E "getting|NT_STATUS|real" $LOGS/p2.out
cmp /tmp/l2g.smbclient $BACKEND/line_2g.segy && echo "smbclient 2G copy correct"
sleep 1; pkill -x tcpdump; sleep 1
echo "interim STATUS_PENDING responses: $(tshark -r /tmp/p.pcap -Y 'smb2.nt_status == 0x00000103' 2>/dev/null | wc -l)"
tshark -r /tmp/p.pcap -Y 'smb2.nt_status == 0x00000103' 2>/dev/null | head -3
echo "READ requests: $(tshark -r /tmp/p.pcap -Y 'smb2.cmd == 8 && smb2.flags.response == 0' 2>/dev/null | wc -l), total SMB2 frames: $(tshark -r /tmp/p.pcap -Y smb2 2>/dev/null | wc -l)"
echo fill > $STATE/delay_scope; delay 0
