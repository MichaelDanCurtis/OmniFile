#!/bin/bash
# A9: a file opened while no group exists stays unguarded for its lifetime (kernel fact,
# canary K2d). Measures: servers start only after the guard's READY (unit ordering), and a
# guard that had to create a NEW group while servers hold files open says so and restarts them.
. "$(dirname "$0")/lib.sh"
begin "A9: ordering, and a new group under open files"
mkdir -p "$DATA/smb"
policy wait; delay 0

section "starting smbd starts the guard first, and smbd starts after READY"
systemctl stop "$SMBU" "$UNIT"
expect "store kept across the stop" "$(nstore)" 2
systemctl start "$SMBU"
wait_until 15 bash -c "[ \"\$(systemctl is-active $SMBU)\" = active ]" || fail "test smbd did not start"
expect "guard active" "$(systemctl is-active "$UNIT")" active
g=$(systemctl show -p ActiveEnterTimestampMonotonic --value "$UNIT")
s=$(systemctl show -p ExecMainStartTimestampMonotonic --value "$SMBU")
evidence "guard READY at ${g} us, smbd exec at ${s} us (monotonic)"
expect "smbd started after the guard was READY" "$(( s >= g ))" 1
section "stopping the guard stops smbd first (BindsTo)"
systemctl stop "$UNIT"
expect "smbd stopped with the guard" "$(systemctl is-active "$SMBU")" inactive
start_guard

section "a NEW group while a 'server' holds a file on the volume is flagged"
# A process named smbd (a script's comm is its file name) holding a file on the volume.
printf '#!/bin/bash\nexec 3< "$1"\nsleep 600\n' > "$A/smbd"; chmod +x "$A/smbd"
echo x > "$DATA/smb/held"
"$A/smbd" "$DATA/smb/held" &
SP=$!
release_guard
T0=$(since_mark)
start_guard
jl=$(glog "$T0")
grep -q "created a new fanotify pre-content group" <<< "$jl" && pass "new group created after the store was released" \
  || fail "no new group"
if grep -q "NEW fanotify group was created while file servers hold files" <<< "$jl"; then
  pass "flagged: $(grep -m1 -o 'servers=.*' <<< "$jl" | cut -c1-80)"
else fail "new group under an open server file was not flagged"; fi
pkill -P "$SP" sleep 2> /dev/null; kill "$SP" 2> /dev/null; wait "$SP" 2> /dev/null; rm -f "$A/smbd"
end_check
