#!/bin/bash
# A12 lookup while the request is still waiting, smbclient default timeout, and whether the fd
# store can be released while smbd runs (A7 reachability).
PW2='spike-geo2-pw'   # test-only local account (see a11_a12_samba.sh)
. "$(dirname "$0")/lib.sh"
U=omni-guard-a
systemctl is-active --quiet smbd || systemctl start smbd
mkstubs >/dev/null; chmod 666 $DATA/seis/*; dropcache; policy wait

say "A12: identity lookup at event time (pid from the guard's journal record, smbstatus)"
delay 20000
smbclient //127.0.0.1/omni -U geo2%$PW2 -m SMB3 -t 60 -c 'get seis/small.segy /tmp/s.smbclient' > $LOGS/a12.out 2>&1 & S=$!
sleep 3
for p in $(awk '$1=="B" {print $6}' $STATE/journal | sort -u); do
  echo "event pid $p ($(cat /proc/$p/comm 2>/dev/null)) -> $(smbstatus -p 2>/dev/null | awk -v p=$p '$1==p {print "user="$2, "group="$3, "machine="$4, "proto="$6}')"
done
wait $S; grep -E "getting|NT_STATUS" $LOGS/a12.out

say "smbclient with its DEFAULT timeout vs a 45 s recall"
mkstubs >/dev/null; chmod 666 $DATA/seis/*; dropcache; delay 45000
( time smbclient //127.0.0.1/omni -U geo2%$PW2 -m SMB3 -c 'get seis/small.segy /tmp/s2.smbclient' ) 2>&1 | grep -E "getting|NT_STATUS|real"
delay 0

say "A7 reachability: can the fd store be released while smbd is running?"
systemctl clean --what=fdstore $U; echo "clean while active: rc=$?"
echo "guard=$(systemctl is-active $U) smbd=$(systemctl is-active smbd) fdstore=$(systemctl show -p NFileDescriptorStore --value $U)"
