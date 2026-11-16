# shellcheck shell=bash
# Shared by the appliance tests and release.sh: boot an appliance disk in qemu (UEFI) with a
# test unit injected through systemd credentials (SMBIOS type 11). Nothing about the test is
# in the image. Sourced, not run.
#
# The guest's architecture is the disk's (vm_init reads its GPT; VM_ARCH=x86-64|arm64
# overrides). The accelerator is chosen per host:
#   - KVM when /dev/kvm is there and the guest is the host's architecture (Linux);
#   - HVF on macOS for an arm64 guest on Apple silicon (build.sh --arch arm64: the gate at
#     native speed);
#   - TCG otherwise (slower: about a minute per boot with KVM-less x86; set VM_TCG_MULTI=1
#     for an x86 guest on an arm64 host).
# VM_ACCEL forces one (kvm, hvf, tcg); VM_CPUS and VM_MEM (MiB) size the guest (4, 3072).
#
# Needs qemu-system-x86_64 or qemu-system-aarch64, qemu-img and python3, and the UEFI
# firmware: Ubuntu's ovmf / qemu-efi-aarch64 in /usr/share/{OVMF,AAVMF}, or the same files
# copied into appliance/build/firmware/{OVMF,AAVMF} (VM_FIRMWARE_DIR overrides). An arm64
# guest under HVF uses qemu's own edk2 build instead (share/qemu/edk2-aarch64-code.fd), since
# Ubuntu's AAVMF doesn't start there. OVMF_CODE, OVMF_VARS and OVMF_SB_CODE override any of
# them. On macOS it also needs GNU coreutils (Homebrew's gnubin is put first in PATH) and
# bash 4 or later.

VM_SH_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
if [ "$(uname -s)" = Darwin ]; then
  for _d in /opt/homebrew/opt/coreutils/libexec/gnubin /usr/local/opt/coreutils/libexec/gnubin; do
    if [ -d "$_d" ]; then case ":$PATH:" in *":$_d:"*) ;; *) PATH=$_d:$PATH ;; esac; break; fi
  done
  unset _d
fi
VM_ARCH=${VM_ARCH:-}
VM_INIT=

# _vm_fw SUBDIR FILE: a firmware file, from VM_FIRMWARE_DIR, /usr/share or build/firmware.
_vm_fw() {
  local d
  for d in ${VM_FIRMWARE_DIR:+"$VM_FIRMWARE_DIR/$1"} "/usr/share/$1" "$VM_SH_DIR/../build/firmware/$1"; do
    if [ -f "$d/$2" ]; then echo "$d/$2"; return; fi
  done
  echo "/usr/share/$1/$2"
}

# vm_disk_arch DISK: x86-64 or arm64, from the type of the disk's /usr partition (any format
# qemu-img reads); nothing if there is none.
vm_disk_arch() {
  local t fmt
  t=$(mktemp)
  fmt=$(qemu-img info --output=json "$1" 2>/dev/null | python3 -c 'import json, sys; print(json.load(sys.stdin)["format"])' 2>/dev/null)
  qemu-img dd -f "${fmt:-raw}" -O raw bs=512 count=64 if="$1" of="$t" >/dev/null 2>&1
  python3 - "$t" <<'PY'
import struct, sys, uuid
TYPES = {
    "8484680c-9521-48c6-9c11-b0720656f69e": "x86-64",  # usr-x86-64
    "b0e01050-ee5f-4390-949a-9101b17104e9": "arm64",   # usr-arm64
}
with open(sys.argv[1], "rb") as f:
    d = f.read()
if d[512:520] != b"EFI PART":
    sys.exit(0)
lba, count, size = struct.unpack_from("<QII", d, 512 + 72)
for i in range(count):
    off = lba * 512 + i * size
    if off + size > len(d):
        break
    t = str(uuid.UUID(bytes_le=d[off:off + 16]))
    if t in TYPES:
        print(TYPES[t])
        break
PY
  rm -f "$t"
}

# vm_init [DISK]: the guest's architecture (VM_ARCH, else DISK's, else x86-64) and what follows
# from it: qemu, machine, firmware, serial console, TPM device. Run once; vm_prepare and
# vm_secure_boot call it. OVMF_CODE/OVMF_VARS (and OVMF_SB_CODE) set beforehand are kept.
vm_init() {
  [ -z "$VM_INIT" ] || return 0
  if [ -z "$VM_ARCH" ] && [ -n "${1:-}" ]; then VM_ARCH=$(vm_disk_arch "$1"); fi
  VM_ARCH=${VM_ARCH:-x86-64}
  case $VM_ARCH in
    x86-64)
      VM_QEMU=qemu-system-x86_64 VM_MACHINE=q35 VM_TTY=ttyS0 VM_TPM_DEV=tpm-tis
      OVMF_CODE=${OVMF_CODE:-$(_vm_fw OVMF OVMF_CODE_4M.fd)}
      OVMF_VARS=${OVMF_VARS:-$(_vm_fw OVMF OVMF_VARS_4M.fd)}
      OVMF_SB_CODE=${OVMF_SB_CODE:-$(_vm_fw OVMF OVMF_CODE_4M.secboot.fd)}
      OVMF_VARS_TEMPLATE=$(_vm_fw OVMF OVMF_VARS_4M.fd) ;;
    arm64)
      VM_QEMU=qemu-system-aarch64 VM_MACHINE=virt VM_TTY=ttyAMA0 VM_TPM_DEV=tpm-tis-device
      # Under HVF and under TCG, Ubuntu's AAVMF never gets past its own setup (no output, one
      # vCPU busy; edk2 2025.11-3ubuntu7.3, 2026-11-03); it starts only under KVM. qemu's own
      # edk2 build boots under all three: from qemu's share directory (Homebrew), or copied into
      # build/firmware/qemu-edk2 (a Linux host's Ubuntu qemu ships AAVMF only).
      local qshare q=''
      qshare=$(cd "$(dirname "$(command -v qemu-system-aarch64 || echo /usr/bin/x)")/../share/qemu" 2>/dev/null && pwd)
      for q in "$qshare" "$VM_SH_DIR/../build/firmware/qemu-edk2" ''; do
        [ -n "$q" ] && [ -f "$q/edk2-aarch64-code.fd" ] && [ -f "$q/edk2-arm-vars.fd" ] && break
      done
      if [ -n "$q" ]; then
        OVMF_CODE=${OVMF_CODE:-$q/edk2-aarch64-code.fd}
        OVMF_VARS=${OVMF_VARS:-$q/edk2-arm-vars.fd}
        OVMF_SB_CODE=${OVMF_SB_CODE:-$q/edk2-aarch64-code.fd}
        OVMF_VARS_TEMPLATE=$q/edk2-arm-vars.fd
      else
        OVMF_CODE=${OVMF_CODE:-$(_vm_fw AAVMF AAVMF_CODE.no-secboot.fd)}
        OVMF_VARS=${OVMF_VARS:-$(_vm_fw AAVMF AAVMF_VARS.fd)}
        OVMF_SB_CODE=${OVMF_SB_CODE:-$(_vm_fw AAVMF AAVMF_CODE.secboot.fd)}
        OVMF_VARS_TEMPLATE=$(_vm_fw AAVMF AAVMF_VARS.fd)
      fi ;;
    *) echo "vm.sh: VM_ARCH $VM_ARCH: x86-64 or arm64" >&2; return 1 ;;
  esac
  local host
  host=$(uname -m)
  case $host in arm64 | aarch64) host=arm64 ;; x86_64 | amd64) host=x86-64 ;; esac
  if [ -n "${VM_ACCEL:-}" ]; then
    VM_ACCEL_ARGS=$VM_ACCEL
  elif [ "$host" = "$VM_ARCH" ] && [ -w /dev/kvm ]; then
    VM_ACCEL_ARGS=kvm
  elif [ "$host" = "$VM_ARCH" ] && [ "$(uname -s)" = Darwin ]; then
    VM_ACCEL_ARGS=hvf
  elif [ "${VM_TCG_MULTI:-0}" = 1 ]; then
    # An x86 guest on an arm64 host runs single-threaded TCG by default (its memory model is
    # stronger than the host's), too slow for a boot with a TPM: VM_TCG_MULTI=1 forces
    # multi-threaded TCG, as the Apple-silicon runs did.
    VM_ACCEL_ARGS=tcg,thread=multi
  else
    VM_ACCEL_ARGS=tcg
  fi
  case $VM_ACCEL_ARGS in tcg*) VM_CPU=max ;; *) VM_CPU=host ;; esac
  VM_INIT=1
  echo "vm.sh: $VM_ARCH guest on $host, accel ${VM_ACCEL_ARGS%%,*}" >&2
}

# vm_secure_boot WORK CERT: Secure Boot on, enforcing, with CERT (PEM) as the only PK, KEK
# and db entry, as a client that enrols OmniFile's key does (docs/design/secure-boot.md).
# Later vm_prepare/vm_run calls use the Secure Boot firmware build (on x86-64 with SMM) and
# those variables. Needs virt-fw-vars (Ubuntu: python3-virt-firmware; pip: virt-firmware).
# VM_SB_DB adds more db certificates (space-separated PEM files) next to CERT.
vm_secure_boot() {
  local work=$1 cert=$2 owner=d4f2c3a1-8f2e-4c7b-9a51-0f1e2d3c4b5a extra=() c
  vm_init "${VM_DISK:-}" || return 1
  mkdir -p "$work"
  OVMF_CODE=$OVMF_SB_CODE
  for c in ${VM_SB_DB:-}; do extra+=(--add-db "$owner" "$c"); done
  virt-fw-vars --input "$OVMF_VARS_TEMPLATE" --output "$work/sb-vars.fd" \
    --set-pk "$owner" "${VM_SB_PK:-$cert}" --add-kek "$owner" "${VM_SB_PK:-$cert}" --add-db "$owner" "$cert" \
    "${extra[@]}" --secure-boot >/dev/null
  OVMF_VARS=$work/sb-vars.fd
  VM_SB=1
}

# vm_prepare WORK DISK SIZE: a fresh qcow2 overlay over DISK (left unchanged; any format
# qemu-img reads) and fresh UEFI variables.
vm_prepare() {
  local work=$1 disk=$2 size=$3 fmt
  vm_init "$disk" || return 1
  mkdir -p "$work"
  rm -f "$work/disk.qcow2" "$work/serial.log"
  cp "$OVMF_VARS" "$work/vars.fd"
  # VM_DISK_FORMAT names it when probing can't tell (a fixed VHD probes as raw).
  fmt=${VM_DISK_FORMAT:-$(qemu-img info --output=json "$disk" | python3 -c 'import json, sys; print(json.load(sys.stdin)["format"])')}
  qemu-img create -q -f qcow2 -F "$fmt" -b "$(realpath "$disk")" "$work/disk.qcow2" "$size"
}

# vm_run WORK TIMEOUT_S UNIT_NAME UNIT_FILE [EXTRA_QEMU_ARGS...]: boots until the guest
# powers off. The unit is added with systemd.extra-unit and pulled in by multi-user.target
# with a drop-in. Further credentials can go in EXTRA_QEMU_ARGS (vm_cred). The serial
# console goes to WORK/serial.log.
vm_run() {
  local work=$1 timeout_s=$2 name=$3 unit=$4
  shift 4
  vm_init || return 1
  # VM_SLOW=1: a loaded TCG host can take minutes to find the root disk in the initrd; give
  # devices and jobs 10 minutes (systemd-stub appends this SMBIOS string to the UKI's
  # command line). The image itself is unchanged.
  local machine=$VM_MACHINE sb=()
  if [ "${VM_SB:-0}" = 1 ] && [ "$VM_ARCH" = x86-64 ]; then
    machine=q35,smm=on
    sb=(-global driver=cfi.pflash01,property=secure,value=on)
  fi
  local slow=()
  if [ "${VM_SLOW:-0}" = 1 ]; then
    slow=(-smbios "type=11,value=io.systemd.stub.kernel-cmdline-extra=rd.systemd.default_device_timeout_sec=600 systemd.default_device_timeout_sec=600 systemd.default_timeout_start_sec=600")
  fi
  local wants
  wants=$(printf '[Unit]\nWants=%s\n' "$name" | base64 -w0)
  # No login prompt on the serial console: agetty would hang up the test unit's output.
  local nogetty
  nogetty=$(printf '[Unit]\nConditionPathExists=/nonexistent\n' | base64 -w0)
  timeout "$timeout_s" "$VM_QEMU" -machine "$machine" -accel "$VM_ACCEL_ARGS" -cpu "$VM_CPU" \
    -smp "${VM_CPUS:-4}" -m "${VM_MEM:-3072}" \
    -nographic -display none -monitor none -serial "file:$work/serial.log" \
    -drive "if=pflash,format=raw,readonly=on,file=$OVMF_CODE" \
    -drive "if=pflash,format=raw,file=$work/vars.fd" \
    -drive "if=virtio,format=qcow2,file=$work/disk.qcow2,detect-zeroes=on" \
    -nic "user,model=virtio-net-pci${VM_NIC_EXTRA:+,$VM_NIC_EXTRA}" \
    -smbios "type=11,value=io.systemd.credential.binary:systemd.extra-unit.$name=$(base64 -w0 "$unit")" \
    -smbios "type=11,value=io.systemd.credential.binary:systemd.unit-dropin.multi-user.target=$wants" \
    -smbios "type=11,value=io.systemd.credential.binary:systemd.unit-dropin.serial-getty@$VM_TTY.service=$nogetty" \
    "${sb[@]}" "${slow[@]}" "$@" >"$work/qemu.out" 2>&1
}

# vm_tpm WORK: starts a software TPM 2.0 (swtpm) for one boot, and prints the qemu
# arguments for it, one per line. Nothing when swtpm isn't installed.
vm_tpm() {
  local work=$1
  command -v swtpm >/dev/null || return 0
  vm_init || return 1
  # qemu's arm64 TPM (tpm-tis-device, a 20 KiB MMIO window on the platform bus) can't be
  # mapped under HVF, whose pages are 16 KiB: qemu stops with HV_BAD_ARGUMENT. Such a guest
  # runs without a TPM, and seals to the host key; KVM and TCG guests get one.
  if [ "$VM_ARCH" = arm64 ] && [ "${VM_ACCEL_ARGS%%,*}" = hvf ]; then
    echo "vm.sh: no vTPM for an arm64 guest under HVF" >&2
    return 0
  fi
  # The TPM's state and control socket in a short local path: a UNIX socket's is at most 104
  # bytes on macOS, and swtpm can't keep its state on a virtiofs share (CMD_INIT fails).
  local t sock
  t=$(mktemp -d /tmp/omnifile-tpm.XXXXXX)
  sock=$t/tpm.sock
  rm -rf "$work/tpm"
  ln -s "$t" "$work/tpm"
  swtpm socket --tpm2 --tpmstate "dir=$t" --ctrl "type=unixio,path=$sock" \
    --terminate --daemon
  printf '%s\n' -chardev "socket,id=chrtpm,path=$sock" \
    -tpmdev emulator,id=tpm0,chardev=chrtpm -device "$VM_TPM_DEV,tpmdev=tpm0"
}

# vm_cred NAME TEXT: qemu arguments for one more credential.
vm_cred() {
  printf '%s\n' -smbios "type=11,value=io.systemd.credential.binary:$1=$(printf '%s' "$2" | base64 -w0)"
}

# vm_unit SCRIPT: a oneshot unit that runs SCRIPT (a file on the host) late in boot, with
# its output on the console (/dev/console: the serial port, ttyS0 or ttyAMA0, which the
# image's command line names last).
vm_unit() {
  cat <<UNIT
[Unit]
Description=Appliance test (injected by credential)
After=multi-user.target boot-complete.target systemd-bless-boot.service
[Service]
Type=oneshot
StandardOutput=tty
StandardError=tty
TTYPath=/dev/console
ExecStart=/bin/sh -c 'echo $(base64 -w0 "$1") | base64 -d >/run/appliance-test.sh; exec /bin/sh /run/appliance-test.sh'
UNIT
}
