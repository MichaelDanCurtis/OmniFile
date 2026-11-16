#!/bin/bash
# A5: the fail-open configurations are refused. The spike showed a guard without an fd holder
# fails open on kill -9 (A5), and an unmounted mark path marks / . The production guard refuses
# to arm in any such configuration; each refusal happens before a group exists.
. "$(dirname "$0")/lib.sh"
begin "A5: the guard refuses fail-open configurations"
refuse() { # refuse <what> <expected message> <harness args...>
  local what=$1 msg=$2; shift 2
  out=$(env -u NOTIFY_SOCKET -u FDSTORE timeout 20 "$H" --state "$STATE/refuse" "$@" 2>&1); rc=$?
  if [ "$rc" -ne 0 ] && grep -q "$msg" <<< "$out"; then pass "$what: refused ($(grep -m1 -o "refus[^:]*:[^\"]*" <<< "$out" | cut -c1-150))"
  else fail "$what: rc=$rc, output: $(tail -n 2 <<< "$out")"; fi
}
mkdir -p "$STATE/refuse"
section "no systemd fd store (the A5 baseline would fail open on kill -9)"
refuse "no NOTIFY_SOCKET/FDSTORE" "would die with this process" \
  --data "$DATA" --uuid "$(UUID)" --fill-mount "$A/pfill" --private-bind
section "wrong filesystem UUID"
refuse "UUID mismatch" "expected" --data "$DATA" --uuid 00000000-0000-0000-0000-000000000001 \
  --fill-mount "$A/pfill" --private-bind --no-fdstore
section "data root not a mount point"
mkdir -p "$A/notmounted"
refuse "plain directory on /" "refusing to arm" --data "$A/notmounted" --uuid "$(UUID)" \
  --fill-mount "$A/pfill" --private-bind --no-fdstore
section "fill mount is the data root itself"
refuse "fill mount = data root" "fill mount" --data "$DATA" --uuid "$(UUID)" --fill-mount "$DATA" --no-fdstore
section "fill mount visible to every process (host mount namespace)"
mkdir -p "$A/hostfill"; mount --bind "$DATA" "$A/hostfill"
refuse "fill mount in the host namespace" "host mount namespace" --data "$DATA" --uuid "$(UUID)" \
  --fill-mount "$A/hostfill" --no-fdstore
umount "$A/hostfill"
end_check
