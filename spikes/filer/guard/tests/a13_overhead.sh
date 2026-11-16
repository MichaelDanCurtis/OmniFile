#!/bin/bash
# A13: overhead of the pre-content mark on resident files, with and without an ignore mark.
. "$(dirname "$0")/lib.sh"
release_unit; stop_plain; : > $LOGS/guard-plain.log
policy wait; delay 0
mkdir -p $DATA/res
F=$DATA/res/big.bin
if [ ! -f $F ]; then $T gen $F 4G --seed 9; fi
setfattr -n user.omnifile.state -v resident $F
setfattr -n user.omnifile.resident_bytes -v $(stat -c %s $F) $F
setfattr -n user.omnifile.key -v big.bin $F
cp -n $F $BACKEND/big.bin 2>/dev/null
rate() { dd if=$F of=/dev/null bs=$1 2>&1 | tail -1 | awk '{print $(NF-1), $NF}'; }
bench() {
  dropcache; echo "  cold, bs=1M : $(rate 1M)"
  rate 1M >/dev/null
  echo "  warm, bs=1M : $(rate 1M)   warm, bs=1M : $(rate 1M)"
  echo "  warm, bs=64K: $(rate 64K)   warm, bs=4K : $(rate 4K)"
}
evs() { grep "stats:" $LOGS/guard-plain.log | tail -1; }
say "A13-0: no guard (no pre-content group on the filesystem)"
bench
say "A13-1: guard armed, NO ignore marks (every read() is a userspace round trip)"
start_plain
bench
stop_plain
for mode in --ignore-filled --ignore-surv; do
  say "A13-2 ($mode): guard armed, inode ignore mark added after the first event on a resident file"
  $T gen $F 4G --seed 9 >/dev/null 2>&1 || true
  setfattr -n user.omnifile.state -v resident $F
  setfattr -n user.omnifile.resident_bytes -v $(stat -c %s $F) $F
  setfattr -n user.omnifile.key -v big.bin $F
  : > $LOGS/guard-plain.log
  start_plain $mode
  bench
  grep "ignore mark on" $LOGS/guard-plain.log | head -2
  sleep 31; grep "stats:" $LOGS/guard-plain.log | tail -1
  say "A13-3 ($mode): hazard: re-stub the file (xattrs, then punch through a normal fd) while the ignore mark exists"
  $T restub $F 4M
  $T check $F --off 1G --len 1M --seed 9
  grep -E "filled" $LOGS/guard-plain.log | tail -1
  stop_plain
done
rm -f $F
