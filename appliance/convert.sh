#!/usr/bin/env bash
# Convert the appliance disk for each hypervisor (decision 38; appliance/README.md).
#
#   appliance/convert.sh [DISK] [--out DIR] [--cpus N] [--memory-mib M]
#
# From omnifile-edge_V.raw it writes, next to it or into --out:
#   omnifile-edge_V.qcow2   KVM / Proxmox (compressed)
#   omnifile-edge_V.vhdx    Hyper-V, a Generation 2 VM (UEFI). Secure Boot off unless the
#                           client enrolled OmniFile's certificate (docs/design/secure-boot.md).
#   omnifile-edge_V.vhd     Azure: a fixed-size VHD (Azure takes no VHDX and no dynamic VHD),
#                           its virtual size a whole number of MiB, for a Generation 2 VM
#                           with the Standard security type (Trusted launch would need our
#                           certificate in its db). Azure runs on Hyper-V, so this is the
#                           test of the Hyper-V path that a subscription can run.
#   omnifile-edge_V.ova     vSphere: an OVF 1.0 descriptor (EFI firmware, paravirtual
#                           SCSI, vmxnet3), a SHA256 manifest and a stream-optimized VMDK.
#
# The disk's architecture (its /usr partition's type) sets the OVF's guest OS: ubuntu64Guest
# for x86-64, arm-ubuntu64Guest for the arm64 test build. tests/formats.sh checks every output.
#
# Needs qemu-img (Ubuntu: qemu-utils), python3, tar and sha256sum.
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# vm_disk_arch, and GNU tools first in PATH on macOS.
# shellcheck source=tests/vm.sh
. "$HERE/tests/vm.sh"
DISK='' OUT='' CPUS=4 MEM=8192
while [ $# -gt 0 ]; do
  case "$1" in
    --out) OUT=$2; shift ;;
    --cpus) CPUS=$2; shift ;;
    --memory-mib) MEM=$2; shift ;;
    -*) echo "unknown option $1" >&2; exit 2 ;;
    *) DISK=$1 ;;
  esac
  shift
done
DISKS=("$HERE"/build/out/omnifile-edge_*.raw)
DISK=${DISK:-${DISKS[0]}}
OUT=${OUT:-$(dirname "$DISK")}
NAME=$(basename "$DISK" .raw)
VERSION=${NAME#omnifile-edge_}
log() { printf '[convert] %s\n' "$*"; }
mkdir -p "$OUT"
ARCH=$(vm_disk_arch "$DISK")
case $ARCH in
  x86-64) OSTYPE=ubuntu64Guest ;;
  arm64) OSTYPE=arm-ubuntu64Guest ;;
  *) echo "[convert] $DISK has no /usr partition of a known architecture" >&2; exit 1 ;;
esac
CAP=$(wc -c <"$DISK" | tr -d ' ')
log "$NAME: $ARCH, $CAP bytes"

log "qcow2"
qemu-img convert -O qcow2 -c "$DISK" "$OUT/$NAME.qcow2"
log "vhdx"
qemu-img convert -O vhdx -o subformat=dynamic "$DISK" "$OUT/$NAME.vhdx"
log "vhd (fixed, for Azure)"
# Azure wants the virtual size in whole MiB. The image's size is (repart sizes its
# partitions in MiB), but a disk that isn't is padded: the GPT's backup header then sits
# before the end, which systemd-repart moves when it grows root at first boot.
MIB=$(( (CAP + 1048575) / 1048576 * 1048576 ))
if [ "$MIB" != "$CAP" ]; then
  log "padding to $MIB bytes (a whole number of MiB)"
  cp --sparse=always "$DISK" "$OUT/$NAME.vhd.raw"
  truncate -s "$MIB" "$OUT/$NAME.vhd.raw"
  qemu-img convert -f raw -O vpc -o subformat=fixed,force_size=on "$OUT/$NAME.vhd.raw" "$OUT/$NAME.vhd"
  rm -f "$OUT/$NAME.vhd.raw"
else
  qemu-img convert -f raw -O vpc -o subformat=fixed,force_size=on "$DISK" "$OUT/$NAME.vhd"
fi

log "ova"
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
qemu-img convert -O vmdk -o subformat=streamOptimized,adapter_type=lsilogic "$DISK" "$T/$NAME-disk1.vmdk"
FILESIZE=$(wc -c <"$T/$NAME-disk1.vmdk" | tr -d ' ')
cat >"$T/$NAME.ovf" <<OVF
<?xml version="1.0" encoding="UTF-8"?>
<Envelope xmlns="http://schemas.dmtf.org/ovf/envelope/1"
  xmlns:ovf="http://schemas.dmtf.org/ovf/envelope/1"
  xmlns:rasd="http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_ResourceAllocationSettingData"
  xmlns:vssd="http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_VirtualSystemSettingData"
  xmlns:vmw="http://www.vmware.com/schema/ovf">
  <References>
    <File ovf:id="file1" ovf:href="$NAME-disk1.vmdk" ovf:size="$FILESIZE"/>
  </References>
  <DiskSection>
    <Info>Virtual disks</Info>
    <Disk ovf:diskId="vmdisk1" ovf:fileRef="file1" ovf:capacity="$CAP" ovf:capacityAllocationUnits="byte"
      ovf:format="http://www.vmware.com/interfaces/specifications/vmdk.html#streamOptimized"/>
  </DiskSection>
  <NetworkSection>
    <Info>Networks</Info>
    <Network ovf:name="VM Network"><Description>The network the filer serves its shares on</Description></Network>
  </NetworkSection>
  <VirtualSystem ovf:id="omnifile-edge">
    <Info>OmniFile edge filer $VERSION</Info>
    <Name>omnifile-edge</Name>
    <OperatingSystemSection ovf:id="94" vmw:osType="$OSTYPE">
      <Info>Ubuntu 26.04 LTS ($ARCH)</Info>
    </OperatingSystemSection>
    <ProductSection>
      <Info>Product</Info>
      <Product>OmniFile Gateway edge filer</Product>
      <Version>$VERSION</Version>
    </ProductSection>
    <VirtualHardwareSection>
      <Info>Virtual hardware</Info>
      <System>
        <vssd:ElementName>Virtual Hardware Family</vssd:ElementName>
        <vssd:InstanceID>0</vssd:InstanceID>
        <vssd:VirtualSystemIdentifier>omnifile-edge</vssd:VirtualSystemIdentifier>
        <vssd:VirtualSystemType>vmx-17</vssd:VirtualSystemType>
      </System>
      <Item>
        <rasd:AllocationUnits>hertz * 10^6</rasd:AllocationUnits>
        <rasd:ElementName>$CPUS virtual CPUs</rasd:ElementName>
        <rasd:InstanceID>1</rasd:InstanceID>
        <rasd:ResourceType>3</rasd:ResourceType>
        <rasd:VirtualQuantity>$CPUS</rasd:VirtualQuantity>
      </Item>
      <Item>
        <rasd:AllocationUnits>byte * 2^20</rasd:AllocationUnits>
        <rasd:ElementName>$MEM MB of memory</rasd:ElementName>
        <rasd:InstanceID>2</rasd:InstanceID>
        <rasd:ResourceType>4</rasd:ResourceType>
        <rasd:VirtualQuantity>$MEM</rasd:VirtualQuantity>
      </Item>
      <Item>
        <rasd:ElementName>SCSI controller 0</rasd:ElementName>
        <rasd:InstanceID>3</rasd:InstanceID>
        <rasd:ResourceSubType>VirtualSCSI</rasd:ResourceSubType>
        <rasd:ResourceType>6</rasd:ResourceType>
      </Item>
      <Item>
        <rasd:AddressOnParent>0</rasd:AddressOnParent>
        <rasd:ElementName>Hard disk 1</rasd:ElementName>
        <rasd:HostResource>ovf:/disk/vmdisk1</rasd:HostResource>
        <rasd:InstanceID>4</rasd:InstanceID>
        <rasd:Parent>3</rasd:Parent>
        <rasd:ResourceType>17</rasd:ResourceType>
      </Item>
      <Item>
        <rasd:AddressOnParent>7</rasd:AddressOnParent>
        <rasd:AutomaticAllocation>true</rasd:AutomaticAllocation>
        <rasd:Connection>VM Network</rasd:Connection>
        <rasd:ElementName>Network adapter 1</rasd:ElementName>
        <rasd:InstanceID>5</rasd:InstanceID>
        <rasd:ResourceSubType>VmxNet3</rasd:ResourceSubType>
        <rasd:ResourceType>10</rasd:ResourceType>
      </Item>
      <vmw:Config ovf:required="false" vmw:key="firmware" vmw:value="efi"/>
      <vmw:Config ovf:required="false" vmw:key="bootOptions.efiSecureBootEnabled" vmw:value="false"/>
    </VirtualHardwareSection>
  </VirtualSystem>
</Envelope>
OVF
(cd "$T" && for f in "$NAME.ovf" "$NAME-disk1.vmdk"; do
  printf 'SHA256(%s)= %s\n' "$f" "$(sha256sum "$f" | cut -d' ' -f1)"
done >"$NAME.mf")
# The descriptor comes first in an OVA, then the manifest, then the disk (OVF 1.0, 5.3). No
# build machine's owner or time goes in.
tar -C "$T" --format=ustar --owner=0 --group=0 --numeric-owner --mode=0644 \
  --mtime="@${SOURCE_DATE_EPOCH:-0}" -cf "$OUT/$NAME.ova" "$NAME.ovf" "$NAME.mf" "$NAME-disk1.vmdk"
ls -la "$OUT/$NAME.qcow2" "$OUT/$NAME.vhdx" "$OUT/$NAME.vhd" "$OUT/$NAME.ova"
