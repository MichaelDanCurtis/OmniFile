#!/bin/bash
# A10: every read path is guarded (pread, O_DIRECT, mmap, sendfile, splice, copy_file_range,
# reflink clone), (0,0) is whole-file, huge ranges hit the size cap, truncates and extending
# writes of a PARTIAL file wait for a full fill. mremap() growth is a known kernel gap.
. "$(dirname "$0")/lib.sh"
begin "A10: read paths, clones, truncates, huge ranges"
mkdir -p "$DATA/seis"
policy wait; delay 200
export SMBGUARD_TMPDIR=$DATA/seis

section "each read path on its own stub"
i=0
for m in pread odirect mmap sendfile splice cfr; do
  i=$((i + 1)); mkstub "seis/p-$m.segy" 128M 1M $((500 + i)); dropcache
  read_now 60 "$DATA/seis/p-$m.segy" --off 100M --len 4M --method "$m" --seed $((500 + i))
  expect "$m past the headers" "$VERDICT" OK
done

section "reflink clone of a stub: (0,0) is a whole-file access"
mkstub seis/c.segy 64M 1M 511; dropcache; rm -f "$DATA/seis/clone.segy"
cp --reflink=always "$DATA/seis/c.segy" "$DATA/seis/clone.segy"; expect "cp --reflink under wait" "$?" 0
read_now 30 "$DATA/seis/clone.segy" --seed 511; expect "the clone's bytes" "$VERDICT" OK
policy failfast
mkstub seis/c2.segy 64M 1M 512; dropcache; rm -f "$DATA/seis/clone2.segy"
cp --reflink=always "$DATA/seis/c2.segy" "$DATA/seis/clone2.segy" 2> "$LOGS/a10-clone.err"
expect "cp --reflink under failfast fails" "$(( $? != 0 ))" 1
if [ -s "$DATA/seis/clone2.segy" ]; then
  read_now 10 "$DATA/seis/clone2.segy" --seed 512
  [ "$VERDICT" = ZEROS ] && fail "a failed clone left zeros readable" || pass "no readable zeros in the failed clone ($VERDICT)"
fi
rm -f "$DATA/seis/clone.segy" "$DATA/seis/clone2.segy"

section "truncate of a PARTIAL file is fenced (waits for a full fill)"
mkstub seis/t.segy 64M 1M 513; dropcache
truncate -s 10M "$DATA/seis/t.segy" 2> "$LOGS/a10-trunc.err"; rc=$?
expect "truncate under failfast fails" "$(( rc != 0 ))" 1
expect "marker still PARTIAL" "$(marker_state "$DATA/seis/t.segy")" Partial
policy wait
truncate -s 10M "$DATA/seis/t.segy"; expect "truncate under wait" "$?" 0
read_now 30 "$DATA/seis/t.segy" --seed 513; expect "the truncated file's bytes" "$VERDICT" OK

section "a write at EOF of a PARTIAL file fills first"
mkstub seis/w.segy 64M 1M 514; dropcache
dd if=/dev/zero of="$DATA/seis/w.segy" bs=4k count=1 seek=16384 conv=notrunc status=none; expect "pwrite at EOF" "$?" 0
read_now 30 "$DATA/seis/w.segy" --len 64M --seed 514; expect "original bytes after the extending write" "$VERDICT" OK
grep -q "OK 67108864 bytes" <<< "$OUT" && pass "all 64 MiB verified" || fail "not all bytes verified: $OUT"

section "O_APPEND (event at f_pos, not EOF): lands before a fill, then fails closed"
mkstub seis/w2.segy 64M 1M 515; dropcache
printf 'tail' >> "$DATA/seis/w2.segy"; expect "append" "$?" 0
expect "the appended bytes are kept" "$(stat -c %s "$DATA/seis/w2.segy")" 67108868
read_now 30 "$DATA/seis/w2.segy" --off 20M --len 1M --seed 515
expect "original bytes past the prefix: EIO, never zeros" "$VERDICT" ERROR
expect "marker kept (not cleared as an O_TRUNC)" "$(marker_state "$DATA/seis/w2.segy")" Partial

section "huge ranges: whole-file mmap of a 200 GB stub hits the size cap"
touch "$STATE/fail"   # the background recall of 200 GB must not run on the test volume
mkstub seis/huge.segy 200G 1M 517; dropcache
read_now 30 "$DATA/seis/huge.segy" --method mmap --seed 517
expect "mmap of 200 GB" "$VERDICT" ERROR
expect_le "answered within (s)" "$(secs_of "$OUT")" 2
grep -q "file_id=517 .*huge=true" "$STATE/recalls.log" && pass "recall requested for the huge range" \
  || fail "no recall for the huge range"
rm -f "$STATE/fail" "$DATA/seis/huge.segy"

section "mremap() growth: known kernel gap (no event), reported, not failed"
mkstub seis/r.segy 64M 1M 518; dropcache
out=$(timeout -s KILL 20 "$T" mremap "$DATA/seis/r.segy" 1M 32M --seed 518 2>&1)
evidence "$out"
grep -q ZEROS <<< "$out" && warn "mremap-grown mapping read zeros: kernel gap (mm/mremap.c has no fsnotify hook); no user workloads on the filer" \
  || pass "mremap growth no longer reads zeros (kernel changed: review the design)"
delay 0
end_check
