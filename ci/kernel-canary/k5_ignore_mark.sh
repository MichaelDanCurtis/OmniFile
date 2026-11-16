#!/bin/bash
# K5: inode ignore marks for resident files (filer spike A13).
#   K5a a plain FAN_MARK_IGNORE added through the O_RDWR event fd is a silent no-op: the inode
#       counts as open for write, so do_fanotify_mark() returns 0 without adding the mark;
#   K5b the same mark added through a separate read-only fd (opened by handle on the guard's
#       private fill mount after the event fd is closed) is added, and later reads of the file
#       raise no events.
# Both are asserted: if K5a starts working, or K5b stops, the guard's ignore-mark code needs review.
. "$(dirname "$0")/lib.sh"
begin "K5 ignore marks: no-op through the event fd, effective through a read-only fd"
F=$DATA/res/k5.bin
CHANGED="kernel behaviour CHANGED: review the guard's ignore-mark handling (A13)"

mkfile() {
  rm -rf "$DATA/res"; mkdir -p "$DATA/res"
  "$T" gen "$F" 64M --seed 9
  setfattr -n user.omnifile.state -v resident "$F"
  setfattr -n user.omnifile.resident_bytes -v "$(stat -c %s "$F")" "$F"
  setfattr -n user.omnifile.key -v k5.bin "$F"
}
events() { grep -c "event fd=" "$GLOG"; }
# run_mode <label> <guard flag>: first read (marks the inode), then a second read; sets
# MARK_LINES, EV1 (events in the first read), EV2 (events in the second read).
run_mode() {
  mkfile
  start_plain "$2" -v || end_check
  read_now 30 "$F" --seed 9
  expect "$1: first read of the resident file" "$VERDICT" OK
  EV1=$(events)
  read_now 30 "$F" --seed 9
  expect "$1: second read" "$VERDICT" OK
  EV2=$(($(events) - EV1))
  MARK_LINES=$(grep "ignore mark on ino" "$GLOG")
  grep "ignore mark on ino" "$GLOG" | tail -n 1 | sed -E 's/^\[[0-9.]+\] //' | while read -r l; do evidence "$l"; done
  evidence "events: first read $EV1, second read $EV2"
  stop_plain
}

section "K5a plain FAN_MARK_IGNORE through the O_RDWR event fd"
run_mode K5a --ignore-evfd
if grep -q "present in fdinfo: true" <<< "$MARK_LINES"; then fail "the mark was added: $CHANGED"
elif grep -q "rc=0, present in fdinfo: false" <<< "$MARK_LINES"; then pass "fanotify_mark returned 0 and added nothing (silent no-op)"
else fail "unexpected result: $(head -n 1 <<< "$MARK_LINES")"; fi
if [ "$EV2" -gt 0 ]; then pass "second read still raised $EV2 events"; else fail "second read raised no events: $CHANGED"; fi

section "K5b FAN_MARK_IGNORE through a read-only fd on the fill mount"
run_mode K5b --ignore-filled
if grep -q "IGNORE via read-only fill-mount fd.*present in fdinfo: true" <<< "$MARK_LINES"; then pass "mark present in the group's fdinfo"
else fail "mark not added: $CHANGED"; fi
expect "events raised by the second read" "$EV2" 0 "$CHANGED"

rm -rf "$DATA/res"
end_check
