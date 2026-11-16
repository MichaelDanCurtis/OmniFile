#!/usr/bin/env bash
# The A/B update round (decision 38; docs/design/appliance-image.md, spike step 4):
#
#   boot 1      0.1.0 (slot A) downloads 0.2.0 from a local feed at once, then waits for
#               its maintenance window: the setup wizard's restart window
#               (systemd-sysupdate-reboot.timer), set to a few minutes after the download.
#               The timer reboots it then, and not before
#   boot 2      0.2.0 (slot B, UKI with 3 tries) reaches boot-complete.target, is blessed,
#               updates itself to 0.3.0 (into slot A, over 0.1.0), reboots
#   boots 3-5   0.3.0 fails its kernel check (made to fail for this test), is not blessed,
#               spends its 3 tries
#   boot 6      systemd-boot falls back to 0.2.0, the last good version
#
#   appliance/tests/update-round.sh --disk OLD.raw --feed DIR [--feed DIR...] \
#     (--signer fake:KEY.pem --key-created EPOCH | --sign-key KEYID)
#
# DISK is a 0.1.0 image built with --pubring; each --feed is a build's feed/ directory
# (0.2.0 and 0.3.0). They are merged into one feed, its SHA256SUMS signed with KEYID (gpg,
# from $GNUPGHOME), and served over HTTP to the guest (qemu user networking: the host is
# 10.0.2.2). The feed's URL reaches the guest as transfer drop-ins in /etc/sysupdate.d, as
# the setup wizard writes them.
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source-path=SCRIPTDIR source=vm.sh
. "$HERE/vm.sh"
DISK='' SIGN_KEY='' SIGNER='' CREATED='' FEEDS=() PORT=${PORT:-8080}
# How long after the download the maintenance window opens (seconds).
WINDOW_DELAY=${WINDOW_DELAY:-300}
WORK=${WORK:-$HERE/../build/update-round}
while [ $# -gt 0 ]; do
  case "$1" in
    --disk) DISK=$2; shift ;;
    --feed) FEEDS+=("$2"); shift ;;
    --sign-key) SIGN_KEY=$2; shift ;;
    --signer) SIGNER=$2; shift ;;
    --key-created) CREATED=$2; shift ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
  shift
done
[ -n "$DISK" ] && [ ${#FEEDS[@]} -gt 0 ] && [ -n "$SIGN_KEY$SIGNER" ] || { sed -n '2,20p' "$0"; exit 2; }

mkdir -p "$WORK"
FEED=$WORK/feed
rm -rf "$FEED" && mkdir -p "$FEED"
for f in "${FEEDS[@]}"; do
  find "$f" -maxdepth 1 -type f -name 'omnifile-edge_*' -exec ln -f {} "$FEED/" \;
done
(cd "$FEED" && sha256sum -- omnifile-edge_* >SHA256SUMS)
# The merged test feed (0.3.0 is bad on purpose, so no release gate signs it) is signed the
# way the image's keyring expects: with the release's OpenPGP signer (pgp_kv.py, the key the
# image's --pubring holds; the workflow's way) or, for an image built with a gpg keyring,
# with that gpg key.
if [ -n "$SIGNER" ]; then
  python3 - "$SIGNER" "$CREATED" "$FEED/SHA256SUMS" "$FEED/SHA256SUMS.gpg" "$HERE/.." <<'PY'
import sys
sys.path.insert(0, sys.argv[5])
import pgp_kv
data = open(sys.argv[3], "rb").read()
open(sys.argv[4], "wb").write(pgp_kv.sign(pgp_kv.signer_from(sys.argv[1]), int(sys.argv[2]), data))
PY
else
  gpg --batch --yes --local-user "$SIGN_KEY" --detach-sign --output "$FEED/SHA256SUMS.gpg" "$FEED/SHA256SUMS"
fi
echo "feed:"; ls -l "$FEED"
python3 -m http.server "$PORT" --bind 127.0.0.1 --directory "$FEED" >"$WORK/http.log" 2>&1 &
HTTP=$!
trap 'kill $HTTP 2>/dev/null || true; rm -f "$WORK/guest.sh" "$WORK/guest.unit"' EXIT

cat >"$WORK/guest.sh" <<SH
PORT=$PORT
WINDOW_DELAY=$WINDOW_DELAY
SH
cat >>"$WORK/guest.sh" <<'SH'
d=/var/lib/omnifile-round
mkdir -p $d
n=$(( $(cat $d/n 2>/dev/null || echo 0) + 1 )); echo $n >$d/n
. /usr/lib/os-release
entry=$(tail -c +5 /sys/firmware/efi/efivars/LoaderEntrySelected-4a67b082-0a4c-41cf-b6c7-440b29bb8c4f 2>/dev/null | tr -d '\0')
esp=$(bootctl --print-boot-path 2>/dev/null || echo /boot)
echo "ROUND boot=$n version=$IMAGE_VERSION entry=$entry kernel_check=$(systemctl is-active omnifile-kernel-check) boot_complete=$(systemctl is-active boot-complete.target)"
echo "ROUND   ukis: $(ls "$esp/EFI/Linux" | tr '\n' ' ')"
echo "ROUND   slots: $(lsblk -nro PARTLABEL /dev/vda | tr '\n' ' ')"
# What a new /usr must bring to an /etc it never touches (an A/B update runs no presets and no
# postinsts): the firewall, Ubuntu's nftables.service kept off, the statd user, own SSH keys.
echo "ROUND   state: version=$IMAGE_VERSION fw=$(nft list table inet omnifile >/dev/null 2>&1 && echo yes || echo no) fw_unit=$(systemctl is-active omnifile-firewall.service 2>/dev/null) nftables=$(systemctl is-active nftables.service 2>/dev/null) statd=$(getent passwd statd | cut -d: -f1) sshfp=$(ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub 2>/dev/null | awk '{print $2}') sshmark=$(test -e /etc/ssh/omnifile-own-host-keys && echo yes || echo no)"
# The previous boot waited for its window: when it downloaded, when the window opened,
# and the last time it was still up (it wrote that every 2 s until the timer rebooted it).
if [ -e $d/window ] && [ ! -e $d/window-reported ]; then
  W=$(cat $d/window); D=$(cat $d/downloaded); A=$(cat $d/alive)
  echo "ROUND   window: opened=$(( W - D ))s_after_download last_up=$(( A - W ))s_after_window"
  touch $d/window-reported
fi
next=
case "$IMAGE_VERSION" in
  0.1.0) [ -e $d/sent-0.2.0 ] || next=0.2.0 ;;
  0.2.0) [ -e $d/sent-0.3.0 ] || next=0.3.0 ;;
esac
if [ "$n" -gt 9 ]; then
  echo "ROUND-END (boot limit)"; systemctl --no-block poweroff
elif [ -n "$next" ]; then
  for t in 10-uki 20-usr 21-usr-verity; do
    mkdir -p /etc/sysupdate.d/$t.transfer.d
    printf '[Source]\nPath=http://10.0.2.2:%s/\n' "$PORT" >/etc/sysupdate.d/$t.transfer.d/feed.conf
  done
  touch $d/sent-$next
  /usr/lib/systemd/systemd-sysupdate --no-pager update "$next" >$d/sysupdate.log 2>&1
  echo "ROUND   sysupdate to $next: rc=$? $(tail -3 $d/sysupdate.log | tr '\n' ' ')"
  if [ "$next" = 0.2.0 ]; then
    # The maintenance window, as the setup wizard sets it (its updates step, rendered with
    # --root so it enables no download timer): one time, WINDOW_DELAY seconds from now.
    # The timer is armed first, then given the window, so a slow reload can't eat into it.
    systemctl enable --now systemd-sysupdate-reboot.timer
    # A window still WINDOW_DELAY ahead once the reload has landed (a slow TCG reload can
    # take minutes, and a window already past would fire at once: the timer is Persistent).
    while :; do
    W=$(( $(date +%s) + WINDOW_DELAY ))
    win=$(date -u -d "@$W" '+%Y-%m-%d %H:%M:%S')
    cat >/run/round-answers.toml <<EOF
version = 1
hostname = "edge-round"
[network]
mode = "dhcp"
[data]
device = "/dev/vdb"
[ad]
domain = "corp.example"
workgroup = "CORP"
join_user = "svc-join"
admin_group = "filer admins"
[updates]
feed = "https://updates.invalid/omnifile-edge/"
reboot_window = "$win"
EOF
    rm -rf /run/round-stage
    /usr/local/bin/omnifile-setup apply --only updates --answers /run/round-answers.toml \
      --root /run/round-stage >/dev/null
    dropin=etc/systemd/system/systemd-sysupdate-reboot.timer.d/10-omnifile.conf
    install -D -m 0644 /run/round-stage/$dropin /$dropin
    echo "ROUND   window drop-in: $(grep '^OnCalendar=.' /$dropin) $(grep '^RandomizedDelaySec' /$dropin)"
    systemd-analyze calendar "$win" >/dev/null || echo "ROUND   window $win is not a calendar time"
    until systemctl daemon-reload; do sleep 5; done
    [ "$(date +%s)" -lt $(( W - 60 )) ] && break
    echo "ROUND   the reload took the window ($win); again"
    done
    systemctl restart systemd-sysupdate-reboot.timer
    A0=$(date +%s)
    echo $W >$d/window; date +%s >$d/downloaded; echo $A0 >$d/alive
    next_us=$(systemctl show -p NextElapseUSecRealtime --value systemd-sysupdate-reboot.timer)
    echo "ROUND   waiting: timer=$(systemctl is-active systemd-sysupdate-reboot.timer) armed=$(( W - A0 ))s_before next=$next_us matches=$([ "$(date -u -d "$next_us" +%s 2>/dev/null)" = "$W" ] && echo yes || echo no) installed=$(/usr/lib/systemd/systemd-sysupdate --no-pager list 2>/dev/null | grep -c 0.2.0)"
    sync
    # Up until the timer's service (systemd-sysupdate reboot) restarts the box.
    while :; do date +%s >$d/alive; sleep 2; done
  fi
  sync; systemctl --no-block reboot
elif [ "$IMAGE_VERSION" = 0.3.0 ]; then
  systemctl --no-block reboot
else
  echo "ROUND-END"; systemctl --no-block poweroff
fi
SH
vm_unit "$WORK/guest.sh" >"$WORK/guest.unit"
# 0.3.0 is the bad version: its kernel check fails. Every other version's check runs as built.
# ($$ is a literal $ in a unit file; os-release quotes the version.)
BAD=$(printf '[Service]\nExecStartPre=/bin/sh -c %s\n' "'. /usr/lib/os-release; test \"\$\$IMAGE_VERSION\" != 0.3.0'")
mapfile -t EXTRA < <(vm_cred systemd.unit-dropin.omnifile-kernel-check.service "$BAD")
vm_prepare "$WORK" "$DISK" 16G
echo "booting $DISK (log: $WORK/serial.log); six boots"
# No -no-reboot: the guest reboots itself between rounds and powers off at the end.
vm_run "$WORK" "${ROUND_TIMEOUT:-3600}" omnifile-round.service "$WORK/guest.unit" "${EXTRA[@]}" || true
# A line can start behind a terminal escape from the console, so match ROUND anywhere.
R=$(grep -ao 'ROUND.*' "$WORK/serial.log" | tr -d '\r')
printf '%s\n' "$R"

fail=0
check() { # check NAME REGEX
  if grep -Eq "$2" <<<"$R"; then echo "ok   $1"; else echo "FAIL $1"; fail=1; fi
}
check "0.1.0 boots first" '^ROUND boot=1 version=0\.1\.0 '
check "0.1.0 fetched 0.2.0" 'sysupdate to 0\.2\.0: rc=0'
check "the restart window is the wizard's, with no random delay" 'window drop-in: OnCalendar=[0-9-]+ [0-9:]+ RandomizedDelaySec=0'
check "0.1.0 waits for its window, the timer armed for it" 'waiting: timer=active armed=[0-9]+s_before next=.* matches=yes'
check "the restart waited for the window, and came with it" 'window: opened=[0-9]{2,}s_after_download last_up=(-[0-2]|[0-9]|[0-9][0-9])s_after_window'
check "0.2.0 boots next, with the check passed" '^ROUND boot=2 version=0\.2\.0 .*kernel_check=active boot_complete=active'
check "0.2.0 fetched 0.3.0" 'sysupdate to 0\.3\.0: rc=0'
check "0.3.0 boots and fails its check" '^ROUND boot=3 version=0\.3\.0 .*kernel_check=failed'
check "0.3.0 spends its tries" '^ROUND boot=5 version=0\.3\.0 '
check "falls back to 0.2.0" '^ROUND boot=6 version=0\.2\.0 '
check "0.2.0 is blessed, 0.3.0 is out of tries" 'ukis: .*omnifile-edge_0\.2\.0\.efi omnifile-edge_0\.3\.0\+0-3\.efi'
check "ends cleanly" '^ROUND-END$'
# The image this branch builds, reached by an update from any earlier one (--disk can be an
# image from before the firewall, the statd user and the SSH key fix).
check "after the update, 0.2.0 has the firewall loaded, and nftables.service stays off" \
  '^ROUND   state: version=0\.2\.0 fw=yes fw_unit=active nftables=(inactive|unknown) '
check "and the statd user (sysusers.d), and SSH keys of its own" \
  '^ROUND   state: version=0\.2\.0 .* statd=statd sshfp=SHA256:[^ ]+ sshmark=yes$'
fp() { sed -n 's/.* sshfp=\([^ ]*\).*/\1/p' <<<"$1"; }
S1=$(grep -m1 '^ROUND   state: version=0\.1\.0 ' <<<"$R" || true)
S2=$(grep -m1 '^ROUND   state: version=0\.2\.0 ' <<<"$R" || true)
S6=$(grep '^ROUND   state: version=0\.2\.0 ' <<<"$R" | tail -1 || true)
fp1=$(fp "$S1") fp2=$(fp "$S2") fp6=$(fp "$S6")
case "$S1" in
  *sshmark=yes) # 0.1.0 made its own keys: the update keeps them
    if [ -n "$fp2" ] && [ "$fp1" = "$fp2" ]; then echo "ok   0.1.0's own SSH keys are kept by the update ($fp1)"
    else echo "FAIL 0.1.0's own SSH keys changed in the update ($fp1 -> $fp2)"; fail=1; fi ;;
  *) # 0.1.0's keys came with its image (every box of it had them): replaced, once
    if [ -n "$fp2" ] && [ "$fp1" != "$fp2" ]; then echo "ok   0.1.0's image-made SSH keys are replaced after the update ($fp1 -> $fp2)"
    else echo "FAIL 0.1.0's image-made SSH keys are still there after the update ($fp1 -> $fp2)"; fail=1; fi ;;
esac
if [ -n "$fp6" ] && [ "$fp6" = "$fp2" ]; then echo "ok   and only once: the fallback to 0.2.0 keeps them"
else echo "FAIL the SSH keys changed again later ($fp2 -> $fp6)"; fail=1; fi
exit $fail
