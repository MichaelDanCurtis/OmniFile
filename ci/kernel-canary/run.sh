#!/bin/bash
# Kernel canary orchestrator. Run as root on a disposable Linux machine (the CI runner):
#
#   sudo bash ci/kernel-canary/run.sh --bin-dir spikes/filer/guard/target/release [--only k1,k3]
#   sudo bash ci/kernel-canary/run.sh --cleanup-only
#
# Creates an xfs loop volume at /srv/omni-a/data, runs every K-check with a hard time budget,
# cleans up after each one (no blocked reader, no fanotify group, no unit left behind), writes
# /srv/omni-a/results/summary.md and exits non-zero if any check failed.
set -u
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
CHECK=run
# shellcheck source=lib.sh
. "$HERE/lib.sh"

BIN_DIR=
ONLY=
CLEANUP_ONLY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --bin-dir) BIN_DIR=$2; shift ;;
    --only) ONLY=$2; shift ;;
    --cleanup-only) CLEANUP_ONLY=1 ;;
    *) echo "usage: $0 [--bin-dir DIR] [--only k1,k2,...] [--cleanup-only]" >&2; exit 2 ;;
  esac
  shift
done
[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 2; }

IMG=$A/xfs.img
teardown() {
  canary_cleanup
  if mountpoint -q "$DATA"; then umount "$DATA" || umount -l "$DATA"; fi
  local loop
  for loop in $(losetup -j "$IMG" 2> /dev/null | cut -d: -f1); do losetup -d "$loop"; done
  rm -f "$IMG"
}
if [ "$CLEANUP_ONLY" = 1 ]; then teardown; exit 0; fi

# check name, title shown in the summary, time budget in seconds
CHECKS=(
  "k1_precontent|240"
  "k2_failopen|360"
  "k3_fdstore|300"
  "k4_flood|900"
  "k5_ignore_mark|180"
)

SUM=$RESULTS/summary.md
rm -f "$RESULTS"/*.md "$RESULTS"/*.title
mkdir -p "$RESULTS"

# ---- preflight: the kernel facts the lane depends on -------------------------------------------
KREL=$(uname -r)
CFG=/boot/config-$KREL
# The appliance image (a UKI, /usr read-only) keeps no /boot/config: look beside the modules,
# then in the running kernel's own copy.
for c in "/usr/lib/modules/$KREL/config" "/lib/modules/$KREL/build/.config"; do
  [ -f "$CFG" ] || CFG=$c
done
if [ -f "$CFG" ]; then
  CFGLINES=$(grep -E '^CONFIG_(FANOTIFY|FANOTIFY_ACCESS_PERMISSIONS|XFS_FS)=' "$CFG" 2> /dev/null)
elif [ -r /proc/config.gz ]; then
  CFG=/proc/config.gz
  CFGLINES=$(zcat /proc/config.gz | grep -E '^CONFIG_(FANOTIFY|FANOTIFY_ACCESS_PERMISSIONS|XFS_FS)=')
fi
[ -n "${CFGLINES:-}" ] || CFGLINES="no kernel config at /boot/config-$KREL"
{
  echo "## Kernel canary (fanotify pre-content, risk R3)"
  echo
  echo "| | |"
  echo "|---|---|"
  echo "| kernel | \`$KREL\` |"
  echo "| os | $(. /etc/os-release && echo "$PRETTY_NAME") |"
  echo "| systemd | $(systemctl --version | head -n 1) |"
  echo "| fs.fanotify.max_queued_events | $(sysctl -n fs.fanotify.max_queued_events 2> /dev/null) |"
  echo "| cpus / memory | $(nproc) / $(free -g | awk '/^Mem:/ {print $2}') GiB |"
  echo
  echo '```'
  echo "$CFGLINES"
  echo '```'
  echo
} > "$SUM"
cat "$SUM"

preflight_fail=
grep -q '^CONFIG_FANOTIFY_ACCESS_PERMISSIONS=y' <<< "$CFGLINES" || preflight_fail="CONFIG_FANOTIFY_ACCESS_PERMISSIONS is not y"
kv=$(sed -E 's/^([0-9]+)\.([0-9]+).*/\1 \2/' <<< "$KREL")
read -r kmaj kmin <<< "$kv"
if [ "$kmaj" -lt 6 ] || { [ "$kmaj" -eq 6 ] && [ "$kmin" -lt 14 ]; }; then preflight_fail="kernel $KREL < 6.14 has no pre-content events"; fi
if [ -n "$preflight_fail" ]; then
  echo "**PREFLIGHT FAILED: $preflight_fail**" >> "$SUM"; echo "preflight: $preflight_fail"; exit 1
fi

# ---- binaries and the data volume ------------------------------------------------------------
if [ -n "$BIN_DIR" ]; then
  mkdir -p "$BIN"
  install -m 0755 "$BIN_DIR/omnifile-guard" "$BIN_DIR/omnitool" "$BIN/"
fi
[ -x "$GUARD" ] && [ -x "$T" ] || { echo "missing $GUARD / $T (pass --bin-dir)" >&2; exit 2; }

trap 'teardown' EXIT
teardown
log "creating the xfs loop volume"
truncate -s 8G "$IMG"
cat /proc/sys/kernel/random/uuid > "$A/fs-uuid"
mkfs.xfs -q -f -m uuid="$(cat "$A/fs-uuid")" "$IMG"
mkdir -p "$DATA"
LOOP=$(losetup -f --show "$IMG")
mount -t xfs "$LOOP" "$DATA"
if ! bash "$HERE/check-mount.sh" "$DATA" "$A/fs-uuid"; then echo "data volume check failed" >&2; exit 1; fi
{
  echo "Data volume: xfs on \`$LOOP\` at \`$DATA\`, UUID \`$(cat "$A/fs-uuid")\` (checked before every mark; the root filesystem is never marked)."
  echo
  echo "| Check | Result | Time |"
  echo "|---|---|---|"
} >> "$SUM"

# ---- run ---------------------------------------------------------------------------------------
failed=0
for entry in "${CHECKS[@]}"; do
  # CANARY_BUDGET_SCALE multiplies the budget (an emulated guest: appliance/release.sh --budget-scale).
  name=${entry%%|*}; budget=$(awk -v b="${entry##*|}" -v s="${CANARY_BUDGET_SCALE:-1}" 'BEGIN { printf "%d", b * s }')
  key=${name%%_*}
  if [ -n "$ONLY" ] && ! grep -qw "$key" <<< "${ONLY//,/ }"; then continue; fi
  echo; echo "################ $name (budget ${budget} s) ################"
  t0=$(date +%s)
  CHECK=$name timeout -k 15 "$budget" bash "$HERE/$name.sh"
  rc=$?
  dt=$(($(date +%s) - t0))
  canary_cleanup
  case $rc in
    0) res=PASS ;;
    124 | 137) res="**FAIL (timed out after ${budget} s)**"; failed=1 ;;
    *) res="**FAIL**"; failed=1 ;;
  esac
  if [ $rc = 0 ] && grep -q '^- WARN' "$RESULTS/$name.md" 2> /dev/null; then res="PASS (with warnings)"; fi
  title=$(cat "$RESULTS/$name.title" 2> /dev/null || echo "$name")
  echo "| $title | $res | ${dt} s |" >> "$SUM"
  echo "$name: $res in ${dt} s"
done

{
  echo
  echo "### Evidence"
  for entry in "${CHECKS[@]}"; do
    name=${entry%%|*}
    [ -f "$RESULTS/$name.md" ] || continue
    echo
    echo "#### $(cat "$RESULTS/$name.title" 2> /dev/null || echo "$name")"
    cat "$RESULTS/$name.md"
  done
} >> "$SUM"

echo; echo "################ summary ################"; cat "$SUM"
exit $failed
