#!/usr/bin/env bash
# The Arc spike, offline (docs/design/appliance-image.md, "Arc agent"): the Azure Connected
# Machine agent as a systemd-sysext on the appliance's read-only /usr.
#
#   appliance/tests/arc-sysext.sh --disk DISK.raw --sysext azcmagent-sysext.raw
#
# The sysext comes from appliance/arc/make-sysext.sh (Microsoft's package, its maintainer
# scripts replaced by declared units, users and directories). It reaches the guest as a second
# disk. The guest merges it (/var/lib/extensions, systemd-sysext), and the checks are:
#   - the merge leaves /usr the verity-backed, read-only erofs, and puts the agent's files in
#     /opt/azcmagent and its units in /usr/lib/systemd/system;
#   - its users and directories are made (systemd-sysusers, systemd-tmpfiles);
#   - himdsd (the agent's metadata service) starts and listens on 40342;
#   - azcmagent answers `show` offline: installed, not connected. Connecting needs an Azure
#     subscription (Arc doesn't onboard from here): real-azure-checks.md.
#   - systemd-sysext unmerge takes it all away again, and /usr is unchanged.
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source-path=SCRIPTDIR source=vm.sh
. "$HERE/vm.sh"
DISK='' SX=''
WORK=${WORK:-$HERE/../build/arc-sysext}
while [ $# -gt 0 ]; do
  case "$1" in
    --disk) DISK=$2; shift ;;
    --sysext) SX=$2; shift ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
  shift
done
[ -n "$DISK" ] && [ -n "$SX" ] || { sed -n '2,17p' "$0"; exit 2; }
mkdir -p "$WORK"
cat >"$WORK/guest.sh" <<'SH'
echo ARC-BEGIN
u0=$(sha256sum /usr/lib/os-release /usr/local/bin/omnifile-edge | sha256sum | cut -c1-16)
mkdir -p /var/lib/extensions
cp /dev/vdb /var/lib/extensions/azcmagent.raw
systemd-sysext refresh >/run/sysext.out 2>&1; echo "merge=$? $(tail -1 /run/sysext.out)"
echo "status=$(systemd-sysext status --no-pager 2>&1 | grep -E '/usr|/opt' | tr -s ' ' | tr '\n' ';')"
echo "merged: usr=$(findmnt -n -o FSTYPE /usr | tail -1) ro=$(touch /usr/.w 2>/dev/null && echo no || echo yes) verity=$(for d in /sys/block/dm-*/dm; do [ "$(cat $d/name)" = usr ] && cut -d- -f1-2 $d/uuid; done) agent=$(ls /opt/azcmagent/bin/himds) unit=$(ls /usr/lib/systemd/system/himdsd.service)"
systemd-sysusers; systemd-tmpfiles --create /usr/lib/tmpfiles.d/azcmagent.conf
echo "users=$(id -u himds) $(id -u arcproxy) vardir=$(stat -c '%U %a' /var/opt/azcmagent)"
systemctl daemon-reload
systemctl start himdsd.socket himdsd.service
for _ in 1 2 3 4 5 6 7 8 9 10; do sleep 1; done
echo "himdsd=$(systemctl is-active himdsd.service) listen=$(ss -ltn | grep -c ':40342 ')"
echo "show: $(timeout 60 azcmagent show 2>&1 | grep -iE 'Agent Status|Agent Version|Resource Name|Cloud' | tr -s ' ' | tr '\n' ';')"
systemctl stop himdsd.service himdsd.socket
systemd-sysext unmerge >/dev/null 2>&1; echo "unmerge=$?"
u1=$(sha256sum /usr/lib/os-release /usr/local/bin/omnifile-edge | sha256sum | cut -c1-16)
echo "after: agent=$(ls /opt/azcmagent 2>/dev/null | wc -l) unit=$(ls /usr/lib/systemd/system/himdsd.service 2>/dev/null | wc -l) usr_same=$([ "$u0" = "$u1" ] && echo yes)"
echo ARC-END
systemctl --no-block poweroff
SH
vm_unit "$WORK/guest.sh" >"$WORK/guest.unit"
vm_prepare "$WORK" "$DISK" 16G
echo "booting $DISK with the sysext $(basename "$SX") (log: $WORK/serial.log)"
vm_run "$WORK" "${ARC_TIMEOUT:-1200}" omnifile-arc.service "$WORK/guest.unit" \
  -drive "if=virtio,format=raw,readonly=on,file=$SX" || true
R=$(sed -n '/ARC-BEGIN/,/ARC-END/p' "$WORK/serial.log" | tr -d '\r')
printf '%s\n' "$R"
fail=0
check() { if grep -Eq "$2" <<<"$R"; then echo "ok   $1"; else echo "FAIL $1"; fail=1; fi; }
check "systemd-sysext merges the agent" '^merge=0'
check "/usr is a read-only overlay on the verified erofs, the agent and its units in it" \
  '^merged: usr=overlay ro=yes verity=CRYPT-VERITY agent=/opt/azcmagent/bin/himds unit=/usr/lib/systemd/system/himdsd.service'
check "its users and state directory are made" '^users=[0-9]+ [0-9]+ vardir=himds 755'
check "himdsd starts and listens on 40342" '^himdsd=active listen=[1-9]'
check "azcmagent answers offline: installed, not connected" '^show: .*Agent Status *: *Disconnected'
check "unmerge removes it, and /usr is unchanged" '^after: agent=0 unit=0 usr_same=yes'
exit $fail
