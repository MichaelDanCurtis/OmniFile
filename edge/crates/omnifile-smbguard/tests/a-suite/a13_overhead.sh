#!/bin/bash
# A13: resident reads must not pay an event round trip each (4 KiB reads fell 3,600 -> 76 MB/s
# in the spike without ignore marks). Ignore marks go through a read-only fd from the fill
# mount, and the stub handshake removes them before narrowing (else a re-stubbed file's punched
# range would skip the guard and read zeros).
. "$(dirname "$0")/lib.sh"
begin "A13: ignore marks"
mkdir -p "$DATA/seis"
policy wait; delay 0
bench() { # bench <file>: median of 3 warm 4 KiB sequential reads, MB/s
  local i r=()
  dd if="$1" of=/dev/null bs=4k 2> /dev/null
  for i in 1 2 3; do
    r+=("$(dd if="$1" of=/dev/null bs=4k 2>&1 | sed -nE 's/.*, ([0-9.]+) ([GM])B\/s.*/\1 \2/p' | awk '{print ($2=="G")?$1*1000:$1}')")
  done
  printf '%s\n' "${r[@]}" | sort -n | sed -n 2p
}
"$T" gen "$DATA/seis/res.bin" 1G --seed 701 --resident-marker

section "with ignore marks (default)"
with=$(bench "$DATA/seis/res.bin"); evidence "4 KiB reads: $with MB/s"
ino=$(stat -c %i "$DATA/seis/res.bin")
if group_fdinfo | grep -qE "^fanotify ino:$(printf %x "$ino") .*ignored_mask:100000"; then pass "inode ignore mark present in the group's fdinfo"
else fail "no ignore mark on inode $ino"; fi

section "without ignore marks (--no-ignore)"
harness_args "--no-ignore"; restart_guard
# A mark added earlier lives in the group; a fresh file has none.
"$T" gen "$DATA/seis/res2.bin" 1G --seed 702 --resident-marker
without=$(bench "$DATA/seis/res2.bin"); evidence "4 KiB reads: $without MB/s"
harness_args ""; restart_guard
expect_ge "ignore marks speed up 4 KiB resident reads (x)" "$(awk "BEGIN{print $with / ($without + 0.001)}")" 4

section "re-stub through the handshake removes the mark first"
mkdir -p "$STATE/ctl"
echo "seis/res.bin 1048576" > "$STATE/ctl/restub.1"
wait_until 20 test -f "$STATE/ctl/restub.1.done" || fail "restub did not run"
evidence "handshake: $(cat "$STATE/ctl/restub.1.done" 2>/dev/null)"
grep -q Stubbed "$STATE/ctl/restub.1.done" && pass "stubbed" || fail "not stubbed"
expect "marker" "$(marker_state "$DATA/seis/res.bin")" Partial
group_fdinfo | grep -qE "^fanotify ino:$(printf %x "$ino") .*ignored_mask:100000" \
  && fail "ignore mark still present after the handshake" || pass "ignore mark removed"
dropcache
r0=$(recalls)
read_now 60 "$DATA/seis/res.bin" --off 500M --len 1M --seed 701
expect "read of the punched range" "$VERDICT" OK "ZEROS = the ignore mark survived the re-stub"
expect_ge "recall requested" "$(recalls)" $((r0 + 1))
rm -f "$DATA/seis/res.bin" "$DATA/seis/res2.bin"; rm -rf "$STATE/ctl"
end_check
