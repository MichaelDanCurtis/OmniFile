#!/bin/bash
# A11/A12: stubs through Samba (smbclient and a local CIFS mount), identity via smbstatus,
# and the systemd ordering smbd -> omni-guard-a.
# Test-only local accounts for this throwaway VM (not real credentials):
PW1='spike-geo1-pw'; PW2='spike-geo2-pw'
. "$(dirname "$0")/lib.sh"
U=omni-guard-a
MNT=/mnt/omni-a
stop_plain
id geo1 >/dev/null 2>&1 || useradd -M -s /usr/sbin/nologin geo1
id geo2 >/dev/null 2>&1 || useradd -M -s /usr/sbin/nologin geo2
printf '%s\n%s\n' "$PW1" "$PW1" | smbpasswd -s -a geo1 >/dev/null
printf '%s\n%s\n' "$PW2" "$PW2" | smbpasswd -s -a geo2 >/dev/null
[ -f /etc/samba/smb.conf.orig ] || cp /etc/samba/smb.conf /etc/samba/smb.conf.orig
cp /work/spikes/filer/guard/samba/smb.conf /etc/samba/smb.conf
mkdir -p /etc/systemd/system/smbd.service.d
cp /work/spikes/filer/guard/systemd/smbd-omni-guard.conf /etc/systemd/system/smbd.service.d/omni-guard.conf
systemctl disable --now nmbd samba-ad-dc 2>/dev/null
systemctl daemon-reload
chmod 777 $DATA $DATA/seis
policy wait; delay 0
umount $MNT 2>/dev/null; mkdir -p $MNT
smbpid() { systemctl show -p MainPID --value smbd; }
gpid() { systemctl show -p MainPID --value $U; }

say "ordering: start smbd only; the guard must be pulled in and be READY first"
systemctl stop smbd; release_unit
mkstubs; chmod 666 $DATA/seis/*; dropcache
systemctl start smbd
systemctl list-dependencies --no-pager smbd | grep -i omni
journalctl --since "-20s" -o short-precise | grep -E "Started (omni-guard|smbd)|READY=1 sent" | sed 's/lima-omnifile-spike //'

mount -t cifs //127.0.0.1/omni $MNT -o username=geo1,password=$PW1,vers=3.1.1,cache=none,uid=0 || exit 1
echo "cifs mounted: $(findmnt -no OPTIONS $MNT | tr , '\n' | grep -E '^(vers|cache|echo_interval|actimeo|hard|soft)' | tr '\n' ' ')"

say "A11a: resident header via CIFS"
$T check $MNT/seis/small.segy --off 0 --len 1M --seed 1

say "A11b: policy wait (delay 3 s) via CIFS mount, pread of 1 MiB at 20 MiB"
delay 3000
$T check $MNT/seis/small.segy --off 20M --len 1M --seed 1
EVP=$(grep -h "filled" <(journalctl -u $U --since "-10s" -o cat) | tail -1 | sed 's/.*pid=\([0-9]*\).*/\1/')
say "A12: map the event pid to the SMB session user"
journalctl -u $U --since "-10s" -o cat | grep filled | tail -1
echo "event pid=$EVP comm=$(cat /proc/$EVP/comm 2>/dev/null)"
smbstatus -p 2>/dev/null | sed -n '1,20p'
echo "smbstatus lookup of event pid:"; smbstatus -p 2>/dev/null | awk -v p=$EVP '$1==p'

say "A11c: policy wait via smbclient (geo2), whole 64 MiB file"
mkstubs; chmod 666 $DATA/seis/*; dropcache; delay 3000
smbclient //127.0.0.1/omni -U geo2%$PW2 -m SMB3 -c 'get seis/small.segy /tmp/small.smbclient' 2>&1 | tail -2
cmp /tmp/small.smbclient $BACKEND/small.segy && echo "smbclient copy == backend object (correct bytes)"
EVP=$(journalctl -u $U --since "-10s" -o cat | grep filled | tail -1 | sed 's/.*pid=\([0-9]*\).*/\1/')
echo "event pid=$EVP -> $(smbstatus -p 2>/dev/null | awk -v p=$EVP '$1==p {print "user="$2" group="$3" machine="$4}')"

say "A11d: policy failfast via CIFS and smbclient"
mkstubs; chmod 666 $DATA/seis/*; dropcache; policy failfast; delay 5000
$T check $MNT/seis/small.segy --off 30M --len 1M --seed 1
dd if=$MNT/seis/small.segy of=/dev/null bs=1M skip=40 count=1 2>&1 | head -2
smbclient //127.0.0.1/omni -U geo2%$PW2 -m SMB3 -c 'get seis/small.segy /tmp/small.ff' 2>&1 | tail -2
tail -3 $STATE/recalls.log
grep -h "NT_STATUS\|IO_DEVICE\|error" /var/log/samba/log.127.0.0.1 2>/dev/null | tail -3

say "A11e: long wait (policy wait, backend delay 90 s) via CIFS and via smbclient"
mkstubs; chmod 666 $DATA/seis/*; dropcache; policy wait; delay 90000; echo file > $STATE/delay_scope
( tcpdump -i lo -w /tmp/a11e.pcap -s 256 port 445 >/dev/null 2>&1 ) & TD=$!; sleep 1
( $T check $MNT/seis/small.segy --off 20M --len 1M --seed 1 ) > $LOGS/a11e-cifs.out 2>&1 &
C=$!
( time smbclient //127.0.0.1/omni -U geo2%$PW2 -m SMB3 -c 'get seis/line_2g.segy /tmp/l2g.smbclient' ) > $LOGS/a11e-smbc.out 2>&1 &
S=$!
wait $C; cat $LOGS/a11e-cifs.out
wait $S; tail -5 $LOGS/a11e-smbc.out
pkill -x tcpdump; sleep 1
echo "SMB2 STATUS_PENDING interim responses on the wire:"
tshark -r /tmp/a11e.pcap -Y 'smb2.nt_status == 0x00000103' 2>/dev/null | head -4
echo "count: $(tshark -r /tmp/a11e.pcap -Y 'smb2.nt_status == 0x00000103' 2>/dev/null | wc -l); READ responses with STATUS_SUCCESS: $(tshark -r /tmp/a11e.pcap -Y 'smb2.cmd == 8 && smb2.flags.response == 1 && smb2.nt_status == 0' 2>/dev/null | wc -l); SMB2 ECHO/keepalive: $(tshark -r /tmp/a11e.pcap -Y 'smb2.cmd == 13' 2>/dev/null | wc -l)"
cmp /tmp/l2g.smbclient $BACKEND/line_2g.segy && echo "smbclient 2G copy correct"
echo fill > $STATE/delay_scope

say "A11f: kill -9 the guard while an SMB read waits (fd store): client must get correct bytes"
mkstubs; chmod 666 $DATA/seis/*; dropcache; policy wait; delay 8000
S0=$(smbpid); G0=$(gpid)
$T check $MNT/seis/line_2g.segy --off 100M --len 1M --seed 2 > $LOGS/a11f.out 2>&1 &
C=$!; sleep 2
kill -9 $G0; echo "killed guard $G0"
sleep 1; $T check $MNT/seis/line_2g.segy --off 300M --len 1M --seed 2 > $LOGS/a11f2.out 2>&1 &
C2=$!
wait $C; cat $LOGS/a11f.out; wait $C2; cat $LOGS/a11f2.out
echo "smbd MainPID before=$S0 after=$(smbpid) (same => smbd was NOT restarted by the guard's auto-restart); guard $G0 -> $(gpid)"

say "A11g: systemctl stop of the guard: smbd must stop first (BindsTo), group stays (enabled unit)"
delay 0
journalctl --since "-1s" >/dev/null
T1=$(date '+%Y-%m-%d %H:%M:%S')
systemctl stop $U
echo "guard=$(systemctl is-active $U) smbd=$(systemctl is-active smbd) fdstore=$(systemctl show -p NFileDescriptorStore --value $U)"
journalctl --since "$T1" -o short-precise | grep -E "Stopp|Stopped|Deactivated" | sed 's/lima-omnifile-spike //'
systemctl start smbd; echo "after start smbd: guard=$(systemctl is-active $U) smbd=$(systemctl is-active smbd)"
umount -l $MNT 2>/dev/null
policy wait; delay 0
