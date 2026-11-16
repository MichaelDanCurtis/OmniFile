#!/usr/bin/env bash
# Secure Boot key rotation, in qemu with the edk2 firmware (OVMF, or AAVMF for the arm64 test
# build) enforcing Secure Boot (docs/design/secure-boot.md, "Rotating a key"):
#
#   appliance/tests/sb-rotation.sh --disk 0.1.0.raw --feed 0.2.0/feed --keys DIR
#
# KEYS holds throwaway keys (fake:, never a release key): sb-old.{key,crt} (the key boxes
# enrolled), sb-new.{key,crt} (the key it rotates to), pk.{key,crt} (the platform key and KEK,
# the client's), update.pem and update-ring.pgp (the feed key the image embeds). The disk's
# ESP (systemd-boot, the fallback loader, the UKI) is signed here with the old key, as
# release.sh signs it. The 0.2.0 feed is served three ways (signed feeds; the UKI differs):
#   new/   its UKI signed by the new key only
#   dual/  its UKI signed by the old key and the new key (two Authenticode signatures)
#
# Runs, each in its own VM with fresh firmware variables (PK and KEK = pk.crt):
#   1. db holds the old certificate. 0.1.0 boots; it updates to new/ 0.2.0 and reboots. The
#      firmware refuses that UKI: the fallback path, recorded as it happens. Back on 0.1.0, the
#      box appends the new certificate to db (an authenticated write, signed by the KEK, as a
#      client's own tooling would do it), gives 0.2.0 its tries back, and reboots: 0.2.0, signed
#      by the new key, boots with Secure Boot on and is blessed.
#   2. db holds the old certificate only. 0.1.0 updates to dual/ 0.2.0, which boots and is
#      blessed: no client action needed before the rotation.
#   3. db holds the new certificate only (the old one removed). The dual-signed 0.2.0 would
#      boot, but the boot loader on the ESP is the one 0.1.0 was released with, signed by the
#      old key only (systemd-boot-update is off, decision 48): the firmware refuses it.
#
# Needs sbsign, sbverify (sbsigntool), pe-addsigs (virt-firmware), cert-to-efi-sig-list and
# sign-efi-sig-list (efitools), mtools, virt-fw-vars, zstd, python3, qemu.
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source-path=SCRIPTDIR source=vm.sh
. "$HERE/vm.sh"
DISK='' FEED='' KEYS='' PORT=${PORT:-8089}
WORK=${WORK:-$HERE/../build/sb-rotation}
while [ $# -gt 0 ]; do
  case "$1" in
    --disk) DISK=$2; shift ;;
    --feed) FEED=$2; shift ;;
    --keys) KEYS=$2; shift ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
  shift
done
[ -n "$DISK" ] && [ -n "$FEED" ] && [ -n "$KEYS" ] || { sed -n '2,32p' "$0"; exit 2; }
KEYS=$(cd "$KEYS" && pwd)
for t in sbsign sbverify pe-addsigs cert-to-efi-sig-list sign-efi-sig-list mcopy virt-fw-vars zstd; do
  command -v $t >/dev/null || { echo "$t is needed" >&2; exit 2; }
done
rm -rf "$WORK" && mkdir -p "$WORK"
WORK=$(cd "$WORK" && pwd)
OWNER=d4f2c3a1-8f2e-4c7b-9a51-0f1e2d3c4b5a
vm_init "$DISK"
case $VM_ARCH in arm64) SFX=aa64 FALLBACK=BOOTAA64.EFI ;; *) SFX=x64 FALLBACK=BOOTX64.EFI ;; esac

# ---- the old-signed disk ----------------------------------------------------------------------
echo "signing the disk's ESP with the old key"
qemu-img convert -f raw -O raw "$DISK" "$WORK/disk.raw"
ESP=$(python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import feed_binding as f
for n, t, u, off, size in f.gpt_partitions(sys.argv[2]):
    if str(t) == "c12a7328-f81f-11d2-ba4b-00a0c93ec93b": print(off)' "$HERE/.." "$WORK/disk.raw")
UKI0=$(mdir -b -i "$WORK/disk.raw@@$ESP" ::EFI/Linux | sed -n 's|.*/\(omnifile-edge_[^/]*\.efi\)$|\1|p' | head -1)
for f in "EFI/systemd/systemd-boot$SFX.efi" "EFI/BOOT/$FALLBACK" "EFI/Linux/$UKI0"; do
  mcopy -n -i "$WORK/disk.raw@@$ESP" "::$f" "$WORK/esp.in"
  sbsign --key "$KEYS/sb-old.key" --cert "$KEYS/sb-old.crt" --output "$WORK/esp.out" "$WORK/esp.in" >/dev/null 2>&1
  mcopy -o -i "$WORK/disk.raw@@$ESP" "$WORK/esp.out" "::$f"
done
rm -f "$WORK/esp.in" "$WORK/esp.out"
mcopy -n -i "$WORK/disk.raw@@$ESP" ::loader/loader.conf "$WORK/loader.conf" 2>/dev/null || : >"$WORK/loader.conf"
echo "ESP: $UKI0 and the loaders signed (old key); loader.conf: $(tr '\n' ';' <"$WORK/loader.conf")"

# ---- the feeds ---------------------------------------------------------------------------------
V2=$(find "$FEED" -maxdepth 1 -name 'omnifile-edge_*.efi' | sed -n 's|.*/omnifile-edge_\(.*\)\.efi$|\1|p' | head -1)
uki=omnifile-edge_$V2.efi
mkfeed() { # mkfeed NAME: copy the feed, sign its UKI as NAME says, sign SHA256SUMS
  local d=$WORK/feeds/$1
  mkdir -p "$d"
  find "$FEED" -maxdepth 1 -type f -name "omnifile-edge_$V2.*" -exec cp {} "$d/" \;
  case $1 in
    new) sbsign --key "$KEYS/sb-new.key" --cert "$KEYS/sb-new.crt" --output "$d/$uki.s" "$d/$uki" >/dev/null 2>&1 ;;
    dual)
      sbsign --key "$KEYS/sb-old.key" --cert "$KEYS/sb-old.crt" --output "$d/$uki.o" "$d/$uki" >/dev/null 2>&1
      sbsign --key "$KEYS/sb-new.key" --cert "$KEYS/sb-new.crt" --detached --output "$d/new.p7" "$d/$uki" >/dev/null 2>&1
      # pe-addsigs appends a WIN_CERTIFICATE as given: the PKCS#7 needs its header (length,
      # revision 2.0, type PKCS_SIGNED_DATA) to be a second entry the firmware reads.
      python3 -c 'import struct, sys; d = open(sys.argv[1], "rb").read()
open(sys.argv[2], "wb").write(struct.pack("<IHH", 8 + len(d), 0x0200, 0x0002) + d)' "$d/new.p7" "$d/new.wincert"
      pe-addsigs --input "$d/$uki.o" --addsig "$d/new.wincert" --output "$d/$uki.s" >/dev/null
      rm -f "$d/$uki.o" "$d/new.p7" "$d/new.wincert" ;;
  esac
  mv "$d/$uki.s" "$d/$uki"
  (cd "$d" && sha256sum -- omnifile-edge_* >SHA256SUMS)
  python3 "$HERE/../pgp_kv.py" sign --signer "fake:$KEYS/update.pem" --created 1792540800 \
    --ring "$KEYS/update-ring.pgp" --in "$d/SHA256SUMS" --out "$d/SHA256SUMS.gpg" >/dev/null
  local o n
  sbverify --cert "$KEYS/sb-old.crt" "$d/$uki" >/dev/null 2>&1 && o=yes || o=no
  sbverify --cert "$KEYS/sb-new.crt" "$d/$uki" >/dev/null 2>&1 && n=yes || n=no
  echo "feed $1: $V2's UKI verifies with the old key: $o, the new key: $n; signatures: $(pe-listsigs "$d/$uki" 2>/dev/null | grep -c 'signature: len' || true)"
}
mkfeed new
mkfeed dual
python3 -m http.server "$PORT" --bind 127.0.0.1 --directory "$WORK/feeds" >"$WORK/http.log" 2>&1 &
HTTP=$!
trap 'kill $HTTP 2>/dev/null || true' EXIT

# The db append the client's tooling would write: an authenticated variable (time-based,
# APPEND_WRITE), signed by the KEK.
cert-to-efi-sig-list -g "$OWNER" "$KEYS/sb-new.crt" "$WORK/new.esl" >/dev/null
sign-efi-sig-list -a -g "$OWNER" -k "$KEYS/pk.key" -c "$KEYS/pk.crt" db "$WORK/new.esl" "$WORK/new-db.auth" >/dev/null
echo "db append: $(wc -c <"$WORK/new-db.auth" | tr -d ' ') bytes, signed by the KEK"

# ---- the guest ---------------------------------------------------------------------------------
cat >"$WORK/guest.sh" <<SH
PORT=$PORT
V2=$V2
SH
cat >>"$WORK/guest.sh" <<'SH'
d=/var/lib/omnifile-sbr; mkdir -p $d
n=$(( $(cat $d/n 2>/dev/null || echo 0) + 1 )); echo $n >$d/n
mode=$(cat /run/credentials/@system/sbr.mode 2>/dev/null || echo none)
. /usr/lib/os-release
G=8be4df61-93ca-11d2-aa0d-00e098032b8c
sb=$(od -An -tu1 /sys/firmware/efi/efivars/SecureBoot-$G 2>/dev/null | awk '{print $NF}')
dbn=$(python3 - <<'PY' 2>/dev/null || echo "?"
import struct
d = open("/sys/firmware/efi/efivars/db-d719b2cb-3d3a-4596-a3bc-dad00e67656f", "rb").read()[4:]
n, off = 0, 0
while off + 28 <= len(d):
    lsize, hsize, ssize = struct.unpack_from("<III", d, off + 16)
    n += (lsize - 28 - hsize) // ssize
    off += lsize
print(n)
PY
)
entry=$(tail -c +5 /sys/firmware/efi/efivars/LoaderEntrySelected-4a67b082-0a4c-41cf-b6c7-440b29bb8c4f 2>/dev/null | tr -d '\0')
esp=$(bootctl --print-boot-path 2>/dev/null || echo /boot)
echo "SBR boot=$n mode=$mode version=$IMAGE_VERSION secure_boot=$sb db_entries=$dbn entry=$entry ukis=$(ls "$esp/EFI/Linux" | tr '\n' ' ')"
feed() {
  for t in 10-uki 20-usr 21-usr-verity; do
    mkdir -p /etc/sysupdate.d/$t.transfer.d
    printf '[Source]\nPath=http://10.0.2.2:%s/%s/\n' "$PORT" "$1" >/etc/sysupdate.d/$t.transfer.d/50-test.conf
  done
}
if [ "$IMAGE_VERSION" != "$V2" ] && [ ! -e $d/updated ]; then
  feed "$mode"
  /usr/lib/systemd/systemd-sysupdate --no-pager update >$d/sysupdate.log 2>&1
  echo "SBR update from $mode/: rc=$? $(tail -1 $d/sysupdate.log)"
  touch $d/updated; sync; systemctl --no-block reboot
elif [ "$IMAGE_VERSION" != "$V2" ] && [ "$mode" = new ] && [ ! -e $d/enrolled ]; then
  # Back on the old version: the fallback worked. Enrol the new certificate next to the old one
  # (EFI_VARIABLE_NON_VOLATILE|BOOTSERVICE_ACCESS|RUNTIME_ACCESS|TIME_BASED_AUTHENTICATED_WRITE_ACCESS|APPEND_WRITE),
  # then give the refused version its tries back, as an admin would after enrolling.
  v=/sys/firmware/efi/efivars/db-d719b2cb-3d3a-4596-a3bc-dad00e67656f
  chattr -i $v 2>/dev/null
  { printf '\147\000\000\000'; cat /run/credentials/@system/sbr.db-append; } >$d/append.var
  cat $d/append.var >$v; rc=$?
  touch $d/enrolled
  bad=$(ls "$esp/EFI/Linux" | grep "^omnifile-edge_$V2+")
  [ -n "$bad" ] && mv "$esp/EFI/Linux/$bad" "$esp/EFI/Linux/omnifile-edge_$V2+3.efi"
  echo "SBR enrolled the new certificate: write rc=$rc; reset $bad to +3"
  sync; systemctl --no-block reboot
else
  echo "SBR-END version=$IMAGE_VERSION boot_complete=$(systemctl is-active boot-complete.target)"
  systemctl --no-block poweroff
fi
SH
vm_unit "$WORK/guest.sh" >"$WORK/guest.unit"
fail=0
check() { # check NAME REGEX TEXT
  if grep -Eq "$2" <<<"$3"; then echo "ok   $1"; else echo "FAIL $1"; fail=1; fi
}
run() { # run NAME MODE TIMEOUT DB_CERTS...: one VM, fresh variables
  local name=$1 mode=$2 to=$3; shift 3
  local w=$WORK/$name
  VM_INIT='' OVMF_CODE='' OVMF_VARS='' VM_SB_PK=$KEYS/pk.crt VM_SB_DB="${*:2}"
  vm_init "$WORK/disk.raw"
  vm_secure_boot "$w" "$1"
  vm_prepare "$w" "$WORK/disk.raw" 16G
  mapfile -t extra < <(vm_cred sbr.mode "$mode"; printf '%s\n' -smbios \
    "type=11,value=io.systemd.credential.binary:sbr.db-append=$(base64 -w0 "$WORK/new-db.auth")")
  echo "== $name: db=$(basename "$1")${2:+ +$(basename "$2")}, feed $mode/ (log $w/serial.log)"
  vm_run "$w" "$to" omnifile-sbr.service "$WORK/guest.unit" "${extra[@]}" || true
  grep -ao 'SBR.*' "$w/serial.log" | tr -d '\r' || true
  echo "   firmware and loader:"
  grep -aoE '(Access Denied|Security Violation|rejected probably by Secure Boot|Failed to (start|execute|load)[^\r]*|Error (loading|starting)[^\r]*|Booting.*)' "$w/serial.log" | sort | uniq -c | sed 's/^/     /' || true
}

run 1-enrol new "${SBR_TIMEOUT:-2400}" "$KEYS/sb-old.crt"
R=$(grep -ao 'SBR.*' "$WORK/1-enrol/serial.log" | tr -d '\r' || true)
check "0.1.0 (old key) boots with Secure Boot on, db holds one certificate" '^SBR boot=1 mode=new version=0\.1\.0 secure_boot=1 db_entries=1 ' "$R"
check "it installs $V2 signed by the new key only" '^SBR update from new/: rc=0' "$R"
check "the firmware refuses that UKI, and the box falls back to 0.1.0 by itself" "^SBR boot=2 mode=new version=0\\.1\\.0 secure_boot=1 db_entries=1 .*omnifile-edge_$V2\\+0-3\\.efi" "$R"
R1=$(grep -aoE 'Security Violation|Access Denied|rejected|Error loading EFI binary|Failed to start boot entry' "$WORK/1-enrol/serial.log" | wc -l | tr -d ' ' || true)
check "the firmware said why ($R1 refusals logged)" '^[1-9]' "$R1"
check "the new certificate is appended to db, signed by the KEK" '^SBR enrolled the new certificate: write rc=0' "$R"
check "$V2 (new key) then boots with Secure Boot on, db holding both" "^SBR boot=3 mode=new version=$V2 secure_boot=1 db_entries=2 " "$R"
check "and is blessed" "^SBR-END version=$V2 boot_complete=active" "$R"

run 2-dual dual "${SBR_TIMEOUT:-2400}" "$KEYS/sb-old.crt"
R=$(grep -ao 'SBR.*' "$WORK/2-dual/serial.log" | tr -d '\r' || true)
check "dual-signed: 0.1.0 installs $V2 signed by both keys" '^SBR update from dual/: rc=0' "$R"
check "and $V2 boots on a db with only the old certificate" "^SBR boot=2 mode=dual version=$V2 secure_boot=1 db_entries=1 " "$R"
check "and is blessed" "^SBR-END version=$V2 boot_complete=active" "$R"

# 3: the old certificate removed from db. The ESP still holds 0.1.0's loader (old key only).
run 3-old-removed none "${SBR_REFUSE_TIMEOUT:-240}" "$KEYS/sb-new.crt"
L=$(tr -d '\r' <"$WORK/3-old-removed/serial.log")
if grep -q '^SBR' <<<"$(grep -ao 'SBR.*' "$WORK/3-old-removed/serial.log" || true)"; then
  echo "FAIL with only the new certificate in db, the old-signed boot loader still started"; fail=1
else
  check "with only the new certificate in db, the firmware refuses the old-signed boot loader" \
    'Access Denied|Security Violation|rejected|Error loading EFI binary' "$L"
fi
exit $fail
