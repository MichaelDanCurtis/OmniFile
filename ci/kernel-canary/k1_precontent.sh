#!/bin/bash
# K1: pre-content permission events on xfs behave as the stub guard needs.
#   FAN_CLASS_PRE_CONTENT group + FAN_MARK_FILESYSTEM + FAN_PRE_ACCESS on the xfs loop volume:
#   - a read of a stub's hole blocks (state D) until the event is answered;
#   - ALLOW after a fill returns the correct pattern bytes;
#   - FAN_DENY_ERRNO(EIO) reaches the reader as EIO;
#   - the resident range reads correctly with no fill.
. "$(dirname "$0")/lib.sh"
begin "K1 pre-content events: block, allow after fill, deny EIO, resident range"
F=$DATA/seis/line.segy   # 2 GiB stub, first 4 MiB resident, seed 2

mkstubs; policy wait; delay 0

section "K1.0 baseline: with no group on the filesystem a stub hole reads as zeros"
read_now 30 "$F" --off 100M --len 1M --seed 2
expect "unguarded hole read" "$VERDICT" ZEROS "the stub is not sparse, so K1 would prove nothing"

section "K1.1 a hole read blocks until the event is answered"
touch "$STATE/pause"
start_plain --pause-file "$STATE/pause" || end_check
start_reader block 60 check "$F" --off 100M --len 1M --seed 2
R=$RPID
sleep 3
st=$(reader_state $R)
evidence "reader after 3 s with the event unanswered: state=${st%% *} wchan=${st#* }"
expect "reader blocked in the kernel (TASK_KILLABLE shows as D)" "${st%% *}" D
rm -f "$STATE/pause"
finish_reader block $R
expect "read after ALLOW (fill, then answer)" "$VERDICT" OK
evidence "$OUT"

section "K1.2 resident range: correct bytes, no fill"
before=$(grep -c "filled" "$GLOG")
read_now 30 "$F" --off 0 --len 1M --seed 2
expect "resident [0,1 MiB)" "$VERDICT" OK
read_now 30 "$F" --off 4096 --len 3600 --seed 2
expect "resident [4096,+3600)" "$VERDICT" OK
after=$(grep -c "filled" "$GLOG")
expect "fills caused by resident reads" "$((after - before))" 0

section "K1.3 a fill with backend latency: the reader waits, then gets correct bytes"
delay 3000
read_now 60 "$F" --off 900M --len 1M --seed 2
expect "read after a 3 s fill" "$VERDICT" OK
s=$(secs_of "$OUT")
if awk -v s="${s:-0}" 'BEGIN { exit !(s >= 2.9) }'; then pass "reader waited for the fill (${s} s >= 3 s)"
else fail "reader returned after ${s:-?} s, before the 3 s fill completed"; fi
delay 0

section "K1.4 FAN_DENY_ERRNO(EIO) reaches the reader"
policy failfast
read_now 30 "$F" --off 500M --len 1M --seed 2
expect "failfast read" "$VERDICT" ERROR
if grep -q "errno=5 " <<< "$OUT"; then pass "errno is EIO (5)"; else fail "errno is not EIO: $OUT"; fi
grep -m 1 "RECALL-QUEUED" "$GLOG" | sed 's/^/      /'
policy wait

stop_plain
end_check
