#!/bin/bash
# A8: FAN_UNLIMITED_QUEUE (the default limit of 16,384 queued events overflows to ALLOW: 616 of
# 17,000 readers got zeros in the spike), and never exiting on EMFILE (an exit released the group
# in the spike's first version: 16,994 zeros).
. "$(dirname "$0")/lib.sh"
begin "A8: 17,000-reader flood and descriptor exhaustion"
N=${SG_FLOOD:-17000}
mkdir -p "$DATA/seis"
policy wait; delay 0

section "A8a $N readers queue behind a stopped guard"
gf=$(group_fdinfo | sed -n 's/^fanotify flags:\([0-9a-f]*\).*/\1/p')
expect "pre-content group has FAN_UNLIMITED_QUEUE (flags 0x$gf)" "$(( (0x${gf:-0} & 0x10) != 0 ))" 1
mkstub seis/a8.segy 128M 1M 401; dropcache
GP=$(guard_pid) || { fail "no guard to stop (MainPID $(mainpid))"; end_check; }
kill -STOP "$GP"
FL=$LOGS/$CHECK-flood.out
systemd-run --scope -q -p TasksMax=infinity -- \
  timeout -s KILL "$(scaled 1500)" "$T" flood "$DATA/seis/a8.segy" "$N" 4096 4M --seed 401 --timeout "$(scaled 1400)" > "$FL" 2>&1 &
F=$!
wait_until "$(scaled 120)" bash -c "grep -qE 'started=$N ' '$FL'" || warn "not all readers started: $(tail -n 1 "$FL")"
sleep 4
l=$(tail -n 1 "$FL"); evidence "while stopped: $l"
expect "readers that got zeros while the guard was stopped" "$(field completed_ZEROS "$l")" 0
expect "readers blocked" "$(field still_blocked "$l")" "$N"
kill -CONT "$GP"
T0=$(date +%s)
wait $F; rc=$?
l=$(tail -n 1 "$FL"); evidence "drained in $(( $(date +%s) - T0 )) s: $l"
expect "zero reads after the drain" "$(field completed_ZEROS "$l")" 0
expect "all readers completed OK" "$(field completed_ok "$l")" "$N"
expect "flood exit status" "$rc" 0

section "A8b descriptor exhaustion: LimitNOFILE=1024, 3,000 readers"
mkdir -p "/etc/systemd/system/$UNIT.service.d"
printf '[Service]\nLimitNOFILE=1024\n' > "/etc/systemd/system/$UNIT.service.d/99-nofile.conf"
systemctl daemon-reload; restart_guard
P0=$(mainpid)
delay 3000
mkstub seis/a8b.segy 128M 1M 402; dropcache
systemd-run --scope -q -p TasksMax=infinity -- \
  timeout -s KILL "$(scaled 600)" "$T" flood "$DATA/seis/a8b.segy" 3000 4096 4M --seed 402 --timeout "$(scaled 500)" > "$FL" 2>&1
l=$(tail -n 1 "$FL"); evidence "$l"
expect "zero reads" "$(field completed_ZEROS "$l")" 0
expect "guard never exited (same MainPID)" "$(mainpid)" "$P0"
e=$(field errors "$l"); [ "${e:-0}" -gt 0 ] && warn "$e reader(s) got an error (fail-closed)"
rm -f "/etc/systemd/system/$UNIT.service.d/99-nofile.conf"; systemctl daemon-reload; restart_guard
delay 0
end_check
