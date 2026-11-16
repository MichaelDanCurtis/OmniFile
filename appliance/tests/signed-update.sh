#!/usr/bin/env bash
# Signed releases, end to end (decisions 47 and 48): Secure Boot and the Key Vault-signed feed.
#
#   appliance/tests/signed-update.sh --disk SIGNED-0.1.0.raw --unsigned-disk 0.1.0.raw \
#     --feed GOOD-0.2.0-assets --bad-feed FORGED-0.2.0-assets --db-cert CERT.pem
#
# SIGNED-0.1.0.raw is a release.sh output: its ESP signed with the Secure Boot key whose
# certificate is CERT, and the image built with the update keyring of the key that signed
# GOOD (pgp_kv.py) and --feed-url http://10.0.2.2:$PORT/omnifile/omnifile-edge/releases/latest/download/.
# FORGED is the same release with SHA256SUMS.gpg made by another key. Both are served the
# way GitHub Releases serves them (tests/gh-releases-stub.py: two redirects per file), from
# two repositories, omnifile/omnifile-edge and forged/omnifile-edge.
#
# With Secure Boot on and only CERT enrolled (vm_secure_boot):
#   1. the unsigned 0.1.0 disk does not start: the firmware refuses its loader;
#   2. the signed 0.1.0 boots, with Secure Boot on;
#   3. pointed at the forged release, systemd-sysupdate refuses it and installs nothing;
#   4. from the image's own feed it installs 0.2.0, and reboots into it, Secure Boot still on.
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source-path=SCRIPTDIR source=vm.sh
. "$HERE/vm.sh"
DISK='' UNSIGNED='' GOOD='' BAD='' CERT='' PORT=${PORT:-8080}
WORK=${WORK:-$HERE/../build/signed-update}
while [ $# -gt 0 ]; do
  case "$1" in
    --disk) DISK=$2; shift ;;
    --unsigned-disk) UNSIGNED=$2; shift ;;
    --feed) GOOD=$2; shift ;;
    --bad-feed) BAD=$2; shift ;;
    --db-cert) CERT=$2; shift ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
  shift
done
[ -n "$DISK" ] && [ -n "$UNSIGNED" ] && [ -n "$GOOD" ] && [ -n "$BAD" ] && [ -n "$CERT" ] ||
  { sed -n '2,20p' "$0"; exit 2; }
mkdir -p "$WORK"
T=$WORK/releases
rm -rf "$T"
mkdir -p "$T/omnifile/omnifile-edge/v0.2.0" "$T/forged/omnifile-edge/v0.2.0"
cp "$GOOD"/* "$T/omnifile/omnifile-edge/v0.2.0/"
cp "$BAD"/* "$T/forged/omnifile-edge/v0.2.0/"
echo v0.2.0 >"$T/omnifile/omnifile-edge/LATEST"
echo v0.2.0 >"$T/forged/omnifile-edge/LATEST"
rm -f "$WORK/gh.log"
python3 "$HERE/gh-releases-stub.py" "$PORT" "$T" "$WORK/gh.log" &
STUB=$!
trap 'kill $STUB 2>/dev/null || true; rm -f "$WORK/guest.sh" "$WORK/guest.unit"' EXIT
vm_init "$DISK"
vm_secure_boot "$WORK" "$CERT"
fail=0
check() { # check NAME REGEX [TEXT]
  if grep -Eq "$2" <<<"${3:-$R}"; then echo "ok   $1"; else echo "FAIL $1"; fail=1; fi
}

# 1. The unsigned disk, under Secure Boot.
echo 'echo UNSIGNED-BOOTED; systemctl --no-block poweroff' >"$WORK/guest.sh"
vm_unit "$WORK/guest.sh" >"$WORK/guest.unit"
vm_prepare "$WORK/unsigned" "$UNSIGNED" 16G
cp "$OVMF_VARS" "$WORK/unsigned/vars.fd"
vm_run "$WORK/unsigned" "${UNSIGNED_TIMEOUT:-600}" omnifile-sb.service "$WORK/guest.unit" || true
U=$(tr -d '\r' <"$WORK/unsigned/serial.log")
check "Secure Boot refuses the unsigned loader" 'Access Denied|Security Violation' "$U"
if grep -q UNSIGNED-BOOTED <<<"$U"; then echo "FAIL the unsigned disk booted"; fail=1; else echo "ok   the unsigned disk does not boot"; fi

# 2-4. The signed disk.
cat >"$WORK/guest.sh" <<SH
PORT=$PORT
SH
cat >>"$WORK/guest.sh" <<'SH'
d=/var/lib/omnifile-sb; mkdir -p $d
n=$(( $(cat $d/n 2>/dev/null || echo 0) + 1 )); echo $n >$d/n
. /usr/lib/os-release
sb=$(od -An -tu1 /sys/firmware/efi/efivars/SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c 2>/dev/null | awk '{print $NF}')
echo "SB boot=$n version=$IMAGE_VERSION secure_boot=$sb feed=$(cat /usr/lib/omnifile/update-feed)"
if [ "$IMAGE_VERSION" = 0.1.0 ] && [ "$n" = 1 ]; then
  for t in 10-uki 20-usr 21-usr-verity; do
    mkdir -p /etc/sysupdate.d/$t.transfer.d
    printf '[Source]\nPath=http://10.0.2.2:%s/forged/omnifile-edge/releases/latest/download/\n' "$PORT" \
      >/etc/sysupdate.d/$t.transfer.d/50-forged.conf
  done
  /usr/lib/systemd/systemd-sysupdate --no-pager update >$d/forged.log 2>&1; rc=$?
  echo "SB forged: rc=$rc installed=$(/usr/lib/systemd/systemd-sysupdate --no-pager list 2>/dev/null | grep -c 0.2.0) $(grep -iE 'signature|gpg|verif' $d/forged.log | tail -2 | tr '\n' ' ')"
  rm -f /etc/sysupdate.d/*.transfer.d/50-forged.conf
  /usr/lib/systemd/systemd-sysupdate --no-pager update >$d/good.log 2>&1; rc=$?
  echo "SB good: rc=$rc $(tail -2 $d/good.log | tr '\n' ' ')"
  sync; systemctl --no-block reboot
else
  echo "SB-END"; systemctl --no-block poweroff
fi
SH
vm_unit "$WORK/guest.sh" >"$WORK/guest.unit"
vm_prepare "$WORK/signed" "$DISK" 16G
cp "$OVMF_VARS" "$WORK/signed/vars.fd"
vm_run "$WORK/signed" "${SIGNED_TIMEOUT:-7200}" omnifile-sb.service "$WORK/guest.unit" || true
R=$(grep -ao 'SB.*' "$WORK/signed/serial.log" | tr -d '\r')
printf '%s\n' "$R"
check "the signed 0.1.0 boots, Secure Boot on, with the GitHub-style feed" '^SB boot=1 version=0\.1\.0 secure_boot=1 feed=http://10\.0\.2\.2:[0-9]+/omnifile/omnifile-edge/releases/latest/download/'
check "the forged release is refused, nothing installed" '^SB forged: rc=[1-9][0-9]* installed=0'
check "the signed release installs" '^SB good: rc=0 '
check "and boots, Secure Boot still on" '^SB boot=2 version=0\.2\.0 secure_boot=1 '
check "ends cleanly" '^SB-END'
check "downloads followed GitHub's redirects" 'GET /assets/omnifile/omnifile-edge/v0\.2\.0/SHA256SUMS\.gpg' "$(cat "$WORK/gh.log")"
exit $fail
