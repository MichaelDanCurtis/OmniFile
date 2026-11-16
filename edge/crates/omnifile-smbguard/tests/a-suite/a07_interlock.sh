#!/bin/bash
# A7: releasing the group answers every pending read ALLOW (kernel fact, canary K2a/K2b), so
# nothing may release it while anything reads. systemd refuses `clean` on an active unit, the
# vendor .wants link keeps a disabled unit referenced (its store survives stop), and
# omnifile-guardctl refuses every releasing operation while a server runs.
. "$(dirname "$0")/lib.sh"
begin "A7: ops interlock"
G() { "$CTL" --unit "$UNIT.service" --servers "$SMBU.service" --volume "$DATA" "$@"; }
mkdir -p "$DATA/smb"
policy wait; delay 0

section "systemd refuses clean --what=fdstore on an active unit"
if systemctl clean --what=fdstore "$UNIT" 2> "$LOGS/a7-clean.err"; then fail "clean succeeded on an active unit"
else pass "refused: $(head -n 1 "$LOGS/a7-clean.err")"; fi
expect "store intact" "$(nstore)" 2

section "omnifile-guardctl while the (test) smbd runs"
systemctl start "$SMBU"
wait_until 10 bash -c "[ \"\$(systemctl is-active $SMBU)\" = active ]" || fail "test smbd did not start"
for op in clean disable mask uninstall; do
  G --dry-run "$op" > "$LOGS/a7-$op.out" 2>&1; rc=$?
  expect "guardctl $op with smbd running" "$rc" 3
done
G --dry-run --force uninstall > /dev/null 2>&1; expect "guardctl --force uninstall with smbd running" "$?" 3
G --dry-run stop > "$LOGS/a7-stop.out" 2>&1; expect "guardctl stop (allowed, smbd stops first)" "$?" 0
evidence "$(grep -m1 warning "$LOGS/a7-stop.out")"
systemctl stop "$SMBU"
G --dry-run clean > /dev/null 2>&1; expect "guardctl clean, servers stopped, no --force" "$?" 3

section "vendor .wants link: disable cannot unreference the unit"
# The vendor directory is /usr/lib/systemd/system. On the appliance /usr is read-only, so the
# test unit's link goes under /run/systemd/system, which `systemctl disable` leaves alone too;
# and the image itself must ship the guard unit's vendor link (edge/systemd/install.sh),
# without which a `systemctl disable --now` of the guard drops its fd store.
VENDOR=/usr/lib/systemd/system
if ! touch "$VENDOR/.a7-writable" 2> /dev/null; then
  VENDOR=/run/systemd/system
  evidence "/usr is read-only (the appliance): the test unit's vendor-style link goes in $VENDOR"
  l=/usr/lib/systemd/system/multi-user.target.wants/omnifile-edge.service
  # The link must lead to the guard unit itself: a dangling link, or one to another unit, makes
  # nothing want omnifile-edge.service.
  t=$(readlink -f "$l" 2> /dev/null)
  if [ -L "$l" ] && [ "$t" = /usr/lib/systemd/system/omnifile-edge.service ] && [ -f "$t" ]; then
    pass "the image ships the guard unit's vendor link ($l -> $(readlink "$l"))"
  else
    fail "the image's vendor link $l is missing or leads elsewhere (${t:-none}): disable --now of omnifile-edge.service would drop its fd store"
  fi
else
  rm -f "$VENDOR/.a7-writable"
fi
mkdir -p "$VENDOR/multi-user.target.wants"
ln -sfn "/etc/systemd/system/$UNIT.service" "$VENDOR/multi-user.target.wants/$UNIT.service"
systemctl daemon-reload
systemctl disable "$UNIT" > /dev/null 2>&1
wb=$(systemctl show -p WantedBy --value "$UNIT")
grep -qw multi-user.target <<< "$wb" && pass "still WantedBy multi-user.target after disable" \
  || fail "WantedBy after disable: '$wb'"
mkstub smb/a7.segy 256M 1M 301; dropcache
systemctl stop "$UNIT"
expect "store kept across stop of a disabled, vendor-wanted unit" "$(nstore)" 2
start_reader a 60 check "$DATA/smb/a7.segy" --off 100M --len 1M --seed 301; R=$RPID
sleep 2; st=$(reader_state $R); expect "reader blocked while stopped" "${st%% *}" D
start_guard
finish_reader a $R; expect "reader after start" "$VERDICT" OK

section "guardctl refuses stop when the unit would be garbage-collected"
rm -f "$VENDOR/multi-user.target.wants/$UNIT.service"; systemctl daemon-reload
G --dry-run stop > /dev/null 2>&1; expect "guardctl stop, unit unreferenced" "$?" 3
systemctl enable "$UNIT" > /dev/null 2>&1
G --dry-run stop > /dev/null 2>&1; expect "guardctl stop, unit enabled again" "$?" 0
end_check
