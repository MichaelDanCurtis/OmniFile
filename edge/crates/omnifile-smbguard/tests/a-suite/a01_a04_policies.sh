#!/bin/bash
# A1-A4: resident header reads, wait, failfast, waitquick (spike tests A1-A4).
. "$(dirname "$0")/lib.sh"
begin "A1-A4: resident headers, wait, failfast, waitquick"
mkdir -p "$DATA/seis"

section "A1 resident header read: no recall"
policy wait; delay 0
mkstub seis/a1.segy 64M 1M 101; dropcache
r0=$(recalls)
read_now 20 "$DATA/seis/a1.segy" --off 0 --len 1M --seed 101
expect "read of [0,1M)" "$VERDICT" OK
read_now 20 "$DATA/seis/a1.segy" --off 4096 --len 3600 --seed 101
expect "read of [4096,+3600)" "$VERDICT" OK
expect "recalls started by header reads" "$(recalls)" "$r0"
expect "marker still PARTIAL" "$(marker_state "$DATA/seis/a1.segy")" Partial

section "A2 past the headers under wait"
delay 2000
mkstub seis/a2.segy 64M 1M 102; dropcache
read_now 30 "$DATA/seis/a2.segy" --off 20M --len 2M --seed 102
expect "2 MiB at 20 MiB" "$VERDICT" OK
expect_ge "waited for the 2 s backend latency (s)" "$(secs_of "$OUT")" 1.9
delay 0
mkstub seis/a2w.segy 512M 4M 103; dropcache
read_now 120 "$DATA/seis/a2w.segy" --seed 103
expect "whole-file sequential read of a 512 MiB stub" "$VERDICT" OK
expect "marker after the full fill" "$(marker_state "$DATA/seis/a2w.segy")" Resident

section "A3 failfast"
policy failfast; delay 1500
mkstub seis/a3.segy 64M 1M 104; dropcache
r0=$(recalls)
read_now 20 "$DATA/seis/a3.segy" --off 20M --len 1M --seed 104
expect "read past the headers" "$VERDICT" ERROR
grep -q "errno=5" <<< "$OUT" && pass "errno is EIO" || fail "errno is not EIO: $OUT"
expect_le "EIO returned within (s)" "$(secs_of "$OUT")" 0.5
expect_ge "recall requested" "$(recalls)" $((r0 + 1))
if cat "$DATA/seis/a3.segy" > /dev/null 2> "$LOGS/a3-cat.err"; then fail "cat succeeded under failfast"
else evidence "cat: $(cat "$LOGS/a3-cat.err")"; fi
wait_until 30 bash -c "[ \"\$($T marker $DATA/seis/a3.segy | grep -c Resident)\" = 1 ]" \
  && pass "background recall completed" || fail "background recall did not complete"
read_now 20 "$DATA/seis/a3.segy" --off 20M --len 1M --seed 104
expect "retry after the background recall" "$VERDICT" OK

section "A4 waitquick:1500"
policy waitquick:1500; delay 500
mkstub seis/a4a.segy 64M 1M 105; dropcache
read_now 20 "$DATA/seis/a4a.segy" --off 20M --len 1M --seed 105
expect "ETA 500 ms: waits" "$VERDICT" OK
expect_ge "waited (s)" "$(secs_of "$OUT")" 0.45
delay 3000
mkstub seis/a4b.segy 64M 1M 106; dropcache
read_now 20 "$DATA/seis/a4b.segy" --off 20M --len 1M --seed 106
expect "ETA 3000 ms: fails fast" "$VERDICT" ERROR
expect_le "EIO returned within (s)" "$(secs_of "$OUT")" 0.5
policy wait; delay 0
end_check
