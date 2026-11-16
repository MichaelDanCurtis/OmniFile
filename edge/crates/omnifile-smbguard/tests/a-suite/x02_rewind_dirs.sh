#!/bin/bash
# M5: unfilled rewind directories (fsal-namespace.md section 7). smbd opens a previous
# version's folder only to list it, so the daemon marks each unfilled one (DIR_UNPOPULATED)
# with an open-permission mark: the open is held while the folder fills, and is never let
# through to an empty listing. Only file servers are held (smbd, Ganesha; here `ls` stands in
# for one): a crawler is let through at once and fills nothing. A fill that fails refuses
# the open (EIO); the daemon's own opens are never held; a held open survives a guard kill -9.
. "$(dirname "$0")/lib.sh"
begin "X2: unfilled rewind directories are filled before they are listed"
mkdir -p "$DATA/rw" "$DATA/smb" "$STATE/ctl"
policy wait; delay 0
echo fill > "$STATE/dirpolicy"
rm -f "$STATE/populates.log"
harness_args "--server-comm ls"; restart_guard || fail "guard did not restart"
# Fresh names each run: earlier runs' folders stay filled on the volume.
N=$$
# watch REL: the harness puts the guard's mark on $DATA/REL (as the daemon does).
watch() {
  local n=$RANDOM$RANDOM
  echo "$1" > "$STATE/ctl/watchdir.$n"
  wait_until 10 test -f "$STATE/ctl/watchdir.$n.done" || { echo "no answer"; return; }
  cat "$STATE/ctl/watchdir.$n.done"
}
unfilled() { "$T" dirmarker "$1" | grep -q "unpopulated=true"; }
populates() { grep -c "path=$DATA/$1\$" "$STATE/populates.log" 2> /dev/null || true; }

section "a listing of an unfilled folder waits for the fill, then shows it filled"
"$T" mkudir "$DATA/rw/a.$N" --seed 901 > /dev/null
expect "mark added" "$(watch rw/a.$N)" ok
delay 1500
T0=$(date +%s.%N)
out=$(timeout 30 ls "$DATA/rw/a.$N" 2>&1); rc=$?
el=$(awk "BEGIN{print $(date +%s.%N) - $T0}")
expect "ls exit" "$rc" 0
expect "listing" "$out" "filled.txt"
expect_ge "waited for the fill (s)" "$el" 1.2
expect "one fill asked for" "$(populates rw/a.$N)" 1
unfilled "$DATA/rw/a.$N" && fail "still marked unfilled" || pass "the folder is filled"

section "a filled folder is listed at once"
T0=$(date +%s.%N)
out=$(timeout 30 ls "$DATA/rw/a.$N" 2>&1)
el=$(awk "BEGIN{print $(date +%s.%N) - $T0}")
expect "listing" "$out" "filled.txt"
expect_le "not held (s)" "$el" 0.5
expect "no second fill" "$(populates rw/a.$N)" 1

section "a crawler (not a file server) is not held, and fills nothing"
"$T" mkudir "$DATA/rw/d.$N" --seed 906 > /dev/null
expect "mark added" "$(watch rw/d.$N)" ok
delay 1500
T0=$(date +%s.%N)
out=$(timeout 30 find "$DATA/rw/d.$N" 2>&1); rc=$?
el=$(awk "BEGIN{print $(date +%s.%N) - $T0}")
expect "find exit" "$rc" 0
expect "it sees the folder unfilled" "$out" "$DATA/rw/d.$N"
expect_le "not held (s)" "$el" 0.5
expect "no fill asked for" "$(populates rw/d.$N)" 0
unfilled "$DATA/rw/d.$N" && pass "still unfilled" || fail "filled by a crawler"

section "over SMB: smbd's listing is held, filled, and attributed to smbd"
systemctl start "$SMBU"
wait_until 15 bash -c "[ \"\$(systemctl is-active $SMBU)\" = active ]" || fail "test smbd did not start"
wait_until 60 smb_listening || fail "test smbd never listened on $SMBPORT"
SMB() { timeout -s KILL 120 smbclient "//127.0.0.1/sg" -p "$SMBPORT" -N -m SMB3 -t 100 -c "$1" > "$LOGS/x02-smb.out" 2>&1; }
"$T" mkudir "$DATA/smb/rw2.$N" --seed 902 > /dev/null
expect "mark added" "$(watch smb/rw2.$N)" ok
delay 1500
SMB "ls rw2.$N/*"; rc=$?
evidence "$(grep -E 'filled|NT_STATUS' "$LOGS/x02-smb.out" | head -n 3 | tr -s ' ')"
expect "smbclient ls" "$rc" 0
grep -q "filled.txt" "$LOGS/x02-smb.out" && pass "the SMB listing shows the filled folder" \
  || fail "the SMB listing does not show filled.txt"
l=$(grep "path=$DATA/smb/rw2.$N\$" "$STATE/populates.log" | head -n 1)
evidence "$l"
grep -q "comm=smbd" <<< "$l" && pass "the fill was asked for by smbd's open" || fail "fill not attributed to smbd"

section "a fill that fails refuses the open, never an empty listing"
echo fail > "$STATE/dirpolicy"
"$T" mkudir "$DATA/rw/b.$N" --seed 903 > /dev/null
expect "mark added" "$(watch rw/b.$N)" ok
out=$(timeout 30 ls "$DATA/rw/b.$N" 2>&1); rc=$?
evidence "$out"
expect "ls fails" "$(( rc != 0 ))" 1
grep -q "Input/output error" <<< "$out" && pass "EIO" || fail "not EIO: $out"
"$T" mkudir "$DATA/smb/rw3.$N" --seed 904 > /dev/null
expect "mark added" "$(watch smb/rw3.$N)" ok
SMB "ls rw3.$N/*"; rc=$?
evidence "$(grep -m1 NT_STATUS "$LOGS/x02-smb.out")"
expect "smbclient ls fails" "$(( rc != 0 ))" 1
grep -q NT_STATUS "$LOGS/x02-smb.out" && pass "the SMB client gets an error" || fail "no NT_STATUS error"
unfilled "$DATA/rw/b.$N" && pass "the folder stays unfilled" || fail "the folder lost its flag"
echo fill > "$STATE/dirpolicy"

section "an open held across a guard kill -9 completes after the restart"
echo hang > "$STATE/dirpolicy"
"$T" mkudir "$DATA/rw/c.$N" --seed 905 > /dev/null
expect "mark added" "$(watch rw/c.$N)" ok
timeout 90 ls "$DATA/rw/c.$N" > "$LOGS/x02-c.out" 2>&1 &
R=$!
sleep 2
L=$(pgrep -P "$R" | head -n 1)
st=$(awk '{print $3}' "/proc/$L/stat" 2> /dev/null)
expect "listing held (ls in D)" "$st" D
delay 0
echo fill > "$STATE/dirpolicy"
kill_guard
wait "$R"; rc=$?
expect "ls exit after the restart" "$rc" 0
expect "listing" "$(cat "$LOGS/x02-c.out")" "filled.txt"
expect "the mark survived the restart (group kept in the fd store)" "$(watch rw/c.$N)" ok
systemctl stop "$SMBU"
harness_args ""; restart_guard
end_check
