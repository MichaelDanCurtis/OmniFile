#!/usr/bin/env bash
# Check what appliance/convert.sh wrote for each hypervisor (decision 38; appliance/README.md
# "Formats"), against the raw disk it came from:
#
#   appliance/tests/formats.sh DISK.raw [--dir DIR] [--boot] [--v2v] [--keep]
#
# DIR (default: the disk's) holds omnifile-edge_V.{qcow2,vhdx,vhd,ova}. Checks:
#   - the OVA: a ustar archive of exactly the descriptor, then the manifest, then the disk
#     (OVF 1.0, section 5.3), with no directories; the manifest's SHA-256 of each file is right
#     and covers every file but itself;
#   - the OVF: valid against DMTF's OVF 1.0 envelope schema (DSP8023 1.0.0 and the CIM schemas
#     it imports, fetched once into build/cache/ovf-schema and pinned by SHA-256, read offline
#     through an XML catalog); its File and Disk agree with the VMDK (href, size, capacity in
#     bytes = the raw disk's size); EFI firmware; the guest OS of the disk's architecture;
#   - capacities: the qcow2, VHDX and VMDK are the raw disk's size; the VHD is fixed, its
#     virtual size the raw size rounded up to a MiB (Azure), with a valid footer;
#   - qemu-img compare: every format holds exactly the raw disk's bytes;
#   - --boot: the VHDX, the OVA's VMDK and the VHD each boot in qemu (tests/vm.sh: HVF, KVM or TCG) to
#     a running appliance that reports its version, verity /usr and root;
#   - --v2v: virt-v2v -i ova reads the OVA (its parser and manifest check), and its conversion
#     reads the disk and stops only at guest inspection (a verity /usr; see below), or converts
#     to a local qcow2 (Linux, with libguestfs; the spike VM).
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source-path=SCRIPTDIR source=vm.sh
. "$HERE/vm.sh"
DISK='' DIR='' BOOT=0 V2V=0 KEEP=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dir) DIR=$2; shift ;;
    --boot) BOOT=1 ;;
    --v2v) V2V=1 ;;
    --keep) KEEP=1 ;;
    -*) echo "unknown option $1" >&2; exit 2 ;;
    *) DISK=$1 ;;
  esac
  shift
done
[ -n "$DISK" ] || { sed -n '2,25p' "$0"; exit 2; }
DIR=${DIR:-$(dirname "$DISK")}
NAME=$(basename "$DISK" .raw)
VERSION=${NAME#omnifile-edge_}
WORK=${WORK:-$HERE/../build/formats-$NAME}
rm -rf "$WORK" && mkdir -p "$WORK"
[ $KEEP = 1 ] || trap 'rm -rf "$WORK/ova"' EXIT
pass=0 fail=0
ok() { echo "ok   $*"; pass=$((pass + 1)); }
bad() { echo "FAIL $*"; fail=$((fail + 1)); }
check() { if eval "$2"; then ok "$1"; else bad "$1"; fi; }
vsize() { qemu-img info --output=json "$1" | python3 -c 'import json, sys; print(json.load(sys.stdin)["virtual-size"])'; }
ARCH=$(vm_disk_arch "$DISK")
RAW=$(wc -c <"$DISK" | tr -d ' ')
echo "disk $DISK: $ARCH, $RAW bytes"
for f in qcow2 vhdx vhd ova; do [ -f "$DIR/$NAME.$f" ] || { echo "missing $DIR/$NAME.$f (appliance/convert.sh)"; exit 1; }; done

# ---- the OVA --------------------------------------------------------------------------------
OVA=$DIR/$NAME.ova
mkdir -p "$WORK/ova"
want="$NAME.ovf $NAME.mf $NAME-disk1.vmdk"
got=$(tar -tf "$OVA" | tr '\n' ' ' | sed 's/ $//')
check "the OVA holds the descriptor, the manifest and the disk, in that order ($got)" '[ "$got" = "$want" ]'
magic=$(dd if="$OVA" bs=1 skip=257 count=5 2>/dev/null)
check "the OVA is a ustar archive" '[ "$magic" = ustar ]'
tar -xf "$OVA" -C "$WORK/ova"
OVF=$WORK/ova/$NAME.ovf VMDK=$WORK/ova/$NAME-disk1.vmdk
mf_ok=$(cd "$WORK/ova" && python3 - "$NAME.mf" <<'PY'
import hashlib, os, re, sys
lines = open(sys.argv[1]).read().splitlines()
seen, ok = set(), True
for l in lines:
    m = re.fullmatch(r"SHA256\((.+)\)= ([0-9a-f]{64})", l)
    if not m:
        print(f"bad line: {l}"); ok = False; continue
    f, h = m.groups()
    seen.add(f)
    got = hashlib.sha256(open(f, "rb").read()).hexdigest()
    if got != h:
        print(f"{f}: {got} != {h}"); ok = False
others = set(os.listdir(".")) - {sys.argv[1]}
if seen != others:
    print(f"covers {sorted(seen)}, archive has {sorted(others)}"); ok = False
print("yes" if ok else "no")
PY
)
check "the manifest's SHA-256s match and cover every file ($(tr '\n' ' ' <<<"$mf_ok"))" '[ "$(tail -1 <<<"$mf_ok")" = yes ]'

# ---- the OVF against DMTF's schema ------------------------------------------------------------
S=$HERE/../build/cache/ovf-schema
mkdir -p "$S"
schema() { # schema FILE URL SHA256
  if [ ! -f "$S/$1" ]; then curl -sSfL -o "$S/$1.part" "$2" && mv "$S/$1.part" "$S/$1"; fi
  [ "$(sha256sum "$S/$1" | cut -d' ' -f1)" = "$3" ] || { echo "$S/$1 is not the pinned schema ($3)"; return 1; }
}
# Pinned 2026-11-03 from schemas.dmtf.org and www.w3.org.
D=http://schemas.dmtf.org/wbem/wscim
schema dsp8023_1.0.0.xsd http://schemas.dmtf.org/ovf/envelope/1/dsp8023_1.0.0.xsd a3c5ee4fdf3a5334b041ae8c8c5b5ab82161f712f7f9da085019aef51876f761
schema xml.xsd http://www.w3.org/2001/xml.xsd 61960fb3131e38022caad5360e2f33a3382578ab3c80cd58bd74320ede61b20c
schema common.xsd $D/1/common.xsd 946c1e2c50ea753e2cf3f40cb4a83c319e0d5693c3b017ad3f9811792319d2ee
schema CIM_VirtualSystemSettingData.xsd "$D/1.0.0/cim-schema/2.19.0+/CIM_VirtualSystemSettingData.xsd" d0fe5376c49041fea0119d54068e614b6410fb3cf0a717edd9826f55962d3043
schema CIM_ResourceAllocationSettingData.xsd "$D/1.0.0/cim-schema/2.19.0+/CIM_ResourceAllocationSettingData.xsd" 334e19154e0a2f6071f33253493f5ed5dfedb67e535393f5fabdb2fa4adab15c
# Read offline: copies whose imports name the pinned files beside them (an XML catalog would do
# the same, but macOS's xmllint ignores XML_CATALOG_FILES for schema imports).
L=$WORK/xsd
mkdir -p "$L"
for f in dsp8023_1.0.0.xsd xml.xsd common.xsd CIM_VirtualSystemSettingData.xsd CIM_ResourceAllocationSettingData.xsd; do
  sed -e 's|schemaLocation="http://www.w3.org/2001/xml.xsd"|schemaLocation="xml.xsd"|' \
    -e "s|schemaLocation=\"$D/1/common.xsd\"|schemaLocation=\"common.xsd\"|" \
    -e "s|schemaLocation=\"$D/1.0.0/cim-schema/2.19.0+/\\(CIM_[A-Za-z]*\\.xsd\\)\"|schemaLocation=\"\\1\"|" "$S/$f" >"$L/$f"
done
if (cd "$L" && xmllint --nonet --noout --schema dsp8023_1.0.0.xsd "$OVF") >"$WORK/xmllint.out" 2>&1; then
  ok "the OVF validates against DSP8023 1.0.0 ($(tail -1 "$WORK/xmllint.out"))"
else
  bad "the OVF validates against DSP8023 1.0.0"; sed 's/^/    /' "$WORK/xmllint.out"
fi
ovf=$(python3 - "$OVF" "$(wc -c <"$VMDK" | tr -d ' ')" "$RAW" "$NAME-disk1.vmdk" "$ARCH" <<'PY'
import sys, xml.etree.ElementTree as ET
O = "{http://schemas.dmtf.org/ovf/envelope/1}"
R = "{http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_ResourceAllocationSettingData}"
V = "{http://www.vmware.com/schema/ovf}"
ovf, size, raw, href, arch = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
t = ET.parse(ovf).getroot()
f = t.find(f"{O}References/{O}File")
d = t.find(f"{O}DiskSection/{O}Disk")
os_ = t.find(f".//{O}OperatingSystemSection")
items = t.findall(f".//{O}VirtualHardwareSection/{O}Item")
disk_item = [i for i in items if i.findtext(f"{R}ResourceType") == "17"]
fw = [c.get(f"{V}value") for c in t.iter(f"{V}Config") if c.get(f"{V}key") == "firmware"]
want_os = {"x86-64": "ubuntu64Guest", "arm64": "arm-ubuntu64Guest"}[arch]
checks = {
    "file_href": f.get(f"{O}href") == href,
    "file_size": f.get(f"{O}size") == size,
    "disk_file": d.get(f"{O}fileRef") == f.get(f"{O}id"),
    "capacity": d.get(f"{O}capacity") == raw and d.get(f"{O}capacityAllocationUnits") == "byte",
    "streamopt": d.get(f"{O}format", "").endswith("#streamOptimized"),
    "host_resource": len(disk_item) == 1 and disk_item[0].findtext(f"{R}HostResource") == "ovf:/disk/" + d.get(f"{O}diskId"),
    "efi": fw == ["efi"],
    "guest_os": os_.get(f"{V}osType") == want_os and os_.get(f"{O}id") == "94",
}
print(" ".join(f"{k}={'ok' if v else 'NO'}" for k, v in checks.items()))
PY
)
check "the OVF's file, disk, capacity, firmware and guest OS agree with the VMDK ($ovf)" '! grep -q =NO <<<"$ovf"'

# ---- capacities and contents --------------------------------------------------------------------
for f in qcow2 vhdx; do
  s=$(vsize "$DIR/$NAME.$f")
  check "$f virtual size is the raw disk's ($s)" '[ "$s" = "$RAW" ]'
done
s=$(vsize "$VMDK")
check "the VMDK's virtual size is the raw disk's ($s)" '[ "$s" = "$RAW" ]'
ct=$(qemu-img info --output=json "$VMDK" | python3 -c 'import json, sys; print(json.load(sys.stdin)["format-specific"]["data"]["create-type"])')
check "the VMDK is streamOptimized ($ct)" '[ "$ct" = streamOptimized ]'
MIB=$(( (RAW + 1048575) / 1048576 * 1048576 ))
vhd=$(python3 - "$DIR/$NAME.vhd" <<'PY'
import os, struct, sys
p = sys.argv[1]
n = os.path.getsize(p)
with open(p, "rb") as f:
    f.seek(n - 512)
    ft = f.read(512)
cookie = ft[0:8]
cur = struct.unpack(">Q", ft[48:56])[0]
dtype = struct.unpack(">I", ft[60:64])[0]
csum = struct.unpack(">I", ft[64:68])[0]
calc = (~sum(ft[:64] + b"\0\0\0\0" + ft[68:]) & 0xffffffff)
print(f"cookie={cookie.decode(errors='replace')} type={dtype} size={cur} file={n} checksum={'ok' if csum == calc else 'bad'}")
PY
)
check "the VHD is fixed (type 2) with a valid footer, $MIB bytes (whole MiB), plus its 512-byte footer ($vhd)" \
  '[ "$vhd" = "cookie=conectix type=2 size=$MIB file=$((MIB + 512)) checksum=ok" ] && [ $((MIB % 1048576)) = 0 ]'
# Each format named, never probed: a fixed VHD probes as raw (its footer is plain data then). The
# VHD's padding to a whole MiB must read as zeros (compare's rule for a larger second image).
for fv in "$DIR/$NAME.qcow2:qcow2" "$DIR/$NAME.vhdx:vhdx" "$DIR/$NAME.vhd:vpc" "$VMDK:vmdk"; do
  f=${fv%:*}
  if out=$(qemu-img compare -f raw -F "${fv##*:}" "$DISK" "$f" 2>&1); then ok "qemu-img compare: ${f##*/} holds the raw disk's bytes ($(tr '\n' ' ' <<<"$out"))"
  else bad "qemu-img compare: ${f##*/} ($(tr '\n' ' ' <<<"$out"))"; fi
done

# ---- boots ------------------------------------------------------------------------------------
if [ $BOOT = 1 ]; then
  export VM_ARCH=$ARCH
  G=$WORK/guest.sh
  cat >"$G" <<'SH'
. /usr/lib/os-release
echo "FMT-BOOTED version=$IMAGE_VERSION arch=$(uname -m) usr=$(findmnt -n -o SOURCE,FSTYPE /usr | tr ' ' ,) root=$(findmnt -n -o FSTYPE /) kernel_check=$(systemctl is-active omnifile-kernel-check) disk=$(lsblk -dno SIZE /dev/vda | tr -d ' ')"
systemctl --no-block poweroff
SH
  vm_unit "$G" >"$G.unit"
  for f in "$DIR/$NAME.vhdx" "$VMDK" "$DIR/$NAME.vhd"; do
    w=$WORK/boot-${f##*.}
    VM_DISK_FORMAT=$(case ${f##*.} in vhd) echo vpc ;; vhdx) echo vhdx ;; vmdk) echo vmdk ;; esac) vm_prepare "$w" "$f" 16G
    t0=$(date +%s)
    vm_run "$w" "${FORMAT_BOOT_TIMEOUT:-1800}" omnifile-format-boot.service "$G.unit" || true
    l=$(grep -ao 'FMT-BOOTED.*' "$w/serial.log" | tr -d '\r' | head -1)
    check "the ${f##*.} boots in qemu ($(( $(date +%s) - t0 )) s): ${l:-no report}" \
      'grep -Eq "^FMT-BOOTED version=$VERSION .*usr=/dev/mapper/usr,erofs root=ext4 kernel_check=active" <<<"$l"'
    rm -f "$w/disk.qcow2"
  done
fi

# ---- virt-v2v ---------------------------------------------------------------------------------
if [ $V2V = 1 ]; then
  command -v virt-v2v >/dev/null || { bad "virt-v2v is not installed (Ubuntu: virt-v2v)"; V2V=0; }
fi
if [ $V2V = 1 ]; then
  export LIBGUESTFS_BACKEND=${LIBGUESTFS_BACKEND:-direct}
  if virt-v2v -i ova "$OVA" --print-source >"$WORK/v2v-source.out" 2>&1; then
    ok "virt-v2v -i ova reads the OVA: $(grep -E 'firmware|disk|source name' "$WORK/v2v-source.out" | tr -s ' ' | tr '\n' ';' | cut -c1-200)"
  else
    bad "virt-v2v -i ova --print-source"; tail -n 15 "$WORK/v2v-source.out" | sed 's/^/    /'
  fi
  rm -rf "$WORK/v2v" && mkdir -p "$WORK/v2v"
  t0=$(date +%s)
  # The conversion itself can't apply: libguestfs's inspection reads the OS from the root
  # filesystem, and the appliance keeps /usr (os-release included) on its own verity partition,
  # which no fstab names. Nor does the image need converting: it carries the virtio, pvscsi and
  # hv_storvsc drivers itself. So the conversion must stop there, and nowhere else.
  if virt-v2v -i ova "$OVA" -o local -os "$WORK/v2v" -of qcow2 >"$WORK/v2v.out" 2>&1; then
    ok "virt-v2v converts it to a local qcow2 ($(( $(date +%s) - t0 )) s; $(ls "$WORK/v2v" | tr '\n' ' '))"
  elif grep -q 'inspection could not detect the source guest' "$WORK/v2v.out"; then
    ok "virt-v2v -i ova -o local reads the disk and stops only at guest inspection, as expected for a verity /usr ($(( $(date +%s) - t0 )) s)"
  else
    bad "virt-v2v -i ova -o local ($(( $(date +%s) - t0 )) s)"; grep -E 'error|warning' "$WORK/v2v.out" | tail -n 8 | sed 's/^/    /'
  fi
  rm -rf "$WORK/v2v"
fi

echo "formats: $pass passed, $fail failed"
[ $fail = 0 ]
