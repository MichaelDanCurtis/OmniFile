#!/bin/bash
# A11/A12: through Samba. smbd reads the volume directly, so the guard is the only thing between
# an SMB client and a stub's holes. wait gives the right bytes, failfast an SMB error, a guard
# kill -9 during SMB reads leaves smbd running (RestartMode=direct) and the reads complete; the
# event pid is the smbd process serving the session (identity is the Identity track's).
. "$(dirname "$0")/lib.sh"
begin "A11/A12: Samba"
command -v smbclient > /dev/null || { fail "smbclient not installed"; end_check; }
mkdir -p "$DATA/smb"
policy wait; delay 0
systemctl start "$SMBU"
wait_until 15 bash -c "[ \"\$(systemctl is-active $SMBU)\" = active ]" || { fail "test smbd did not start"; end_check; }
wait_until 60 smb_listening || { fail "test smbd never listened on $SMBPORT"; end_check; }
SMB() { timeout -s KILL "${SMBTO:-150}" smbclient "//127.0.0.1/sg" -p "$SMBPORT" -N -m SMB3 -t 120 -c "$1" > "$LOGS/a11-smb.out" 2>&1; }

section "A11a wait: SMB read of a stub returns the backend bytes"
delay 3000
mkstub smb/w.segy 64M 1M 601; dropcache
rm -f "$LOGS/expect-601"; "$T" gen "$LOGS/expect-601" 64M --seed 601
T0=$(date +%s.%N)
SMB "get w.segy $LOGS/w.out"; rc=$?
el=$(awk "BEGIN{print $(date +%s.%N) - $T0}")
expect "smbclient get" "$rc" 0
cmp -s "$LOGS/w.out" "$LOGS/expect-601"; expect "bytes identical to the backend object" "$?" 0
expect_ge "waited for the recall (s)" "$el" 2.5

section "A12 the event pid is the smbd process serving the session"
l=$(grep "file_id=601 " "$STATE/recalls.log" | head -n 1)
evidence "$l"
grep -q "comm=smbd" <<< "$l" && pass "recall attributed to smbd ($(grep -o 'pid=[0-9]*' <<< "$l"))" \
  || fail "recall not attributed to smbd"

section "A11b failfast: SMB error, never zeros"
policy failfast; delay 2000
mkstub smb/f.segy 64M 1M 602; dropcache
SMB "get f.segy $LOGS/f.out"; rc=$?
expect "smbclient get fails" "$(( rc != 0 ))" 1
evidence "$(grep -m1 NT_STATUS "$LOGS/a11-smb.out")"
grep -q NT_STATUS "$LOGS/a11-smb.out" && pass "client sees an NT_STATUS error" || fail "no NT_STATUS error"

section "A11c guard kill -9 during two waiting SMB reads"
policy wait; delay 8000
mkstub smb/k1.segy 64M 1M 603; mkstub smb/k2.segy 64M 1M 604; dropcache
rm -f "$LOGS/expect-603" "$LOGS/expect-604"
"$T" gen "$LOGS/expect-603" 64M --seed 603; "$T" gen "$LOGS/expect-604" 64M --seed 604
SP=$(systemctl show -p MainPID --value "$SMBU")
( timeout -s KILL 150 smbclient //127.0.0.1/sg -p "$SMBPORT" -N -m SMB3 -t 120 -c "get k1.segy $LOGS/k1.out" > "$LOGS/k1.log" 2>&1; echo $? > "$LOGS/k1.rc" ) &
J1=$!
( timeout -s KILL 150 smbclient //127.0.0.1/sg -p "$SMBPORT" -N -m SMB3 -t 120 -c "get k2.segy $LOGS/k2.out" > "$LOGS/k2.log" 2>&1; echo $? > "$LOGS/k2.rc" ) &
J2=$!
sleep 3
kill_guard
wait $J1 $J2
expect "smbd kept its PID across the guard crash" "$(systemctl show -p MainPID --value "$SMBU")" "$SP"
expect "read 1 status" "$(cat "$LOGS/k1.rc")" 0
expect "read 2 status" "$(cat "$LOGS/k2.rc")" 0
cmp -s "$LOGS/k1.out" "$LOGS/expect-603"; expect "read 1 bytes" "$?" 0
cmp -s "$LOGS/k2.out" "$LOGS/expect-604"; expect "read 2 bytes" "$?" 0
delay 0
systemctl stop "$SMBU"
rm -f "$LOGS"/*.out "$LOGS"/expect-*
end_check
