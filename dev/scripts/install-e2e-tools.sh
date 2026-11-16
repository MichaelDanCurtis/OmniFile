#!/usr/bin/env bash
# What tests/e2e/m5_exit_test.sh needs on the edge besides the M1 stack. Runs as root
# INSIDE omnifile-edge (dev/scripts/edge-provision.sh runs it; on a VM provisioned
# before it existed, run it once by hand):
#
#   limactl shell --workdir / omnifile-edge -- sudo /work/dev/scripts/install-e2e-tools.sh
#
# - libnfs-utils (the userspace NFS client, fsal/tests/nfsc.py), nftables (step 12's
#   packet drops) and conntrack: Ubuntu's kernel has no /proc/net/nf_conntrack
#   (CONFIG_NF_CONNTRACK_PROCFS is off), so step 12 reads the flow table over netlink.
# - vfs_omnifile_prevver (samba-vfs/, GPL-3.0, decision 36) for this host's smbd, built
#   with samba-vfs/build.sh and staged at /usr/local/lib/omnifile/samba-vfs/, where step
#   8 finds it. It is NOT installed into smbd's MODULESDIR: step 8 installs it there
#   itself (samba-vfs/install.sh) after first showing the gap without it, and removes it
#   again. The module runs only inside smbd; no OmniFile binary links it.
#   The build fetches the Samba release tarball matching smbd (checked against
#   samba-vfs/SHA256SUMS) and needs about 1 GB of scratch space for a few minutes,
#   under /var/tmp, removed afterwards.
# Idempotent: the module is rebuilt only when smbd's version changes (or with --force).
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
STAGE=/usr/local/lib/omnifile/samba-vfs
SO=$STAGE/omnifile_prevver.so
WORK=/var/tmp/omnifile-samba-vfs-build
FORCE=0
[ "${1:-}" = --force ] && FORCE=1

log() { printf '[install-e2e-tools] %s\n' "$*"; }
die() { printf '[install-e2e-tools] ERROR: %s\n' "$*" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || die "must run as root"

pkgs=(libnfs-utils nftables conntrack)
missing=()
for p in "${pkgs[@]}"; do
  dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q 'install ok installed' || missing+=("$p")
done
if [ "${#missing[@]}" -gt 0 ]; then
  log "installing ${missing[*]}"
  apt-get update -qq
  apt-get install -y -qq -o DPkg::Lock::Timeout=600 "${missing[@]}" >/dev/null
fi

command -v smbd >/dev/null || die "no smbd on this host (edge-provision.sh installs samba)"
ver=$(smbd --version)
if [ "$FORCE" = 0 ] && [ -f "$SO" ] && [ "$(cat "$STAGE/smbd-version" 2>/dev/null)" = "$ver" ]; then
  log "vfs_omnifile_prevver already built for $ver ($SO)"
  exit 0
fi
log "building vfs_omnifile_prevver for $ver (GPL-3.0, runs only inside smbd)"
apt-get install -y -qq -o DPkg::Lock::Timeout=600 --no-install-recommends build-essential \
  python3-dev libgnutls28-dev flex bison perl pkg-config zlib1g-dev libtirpc-dev libacl1-dev \
  libpopt-dev libicu-dev libparse-yapp-perl libreadline-dev >/dev/null
rm -rf "$WORK"
install -d -m 0755 "$STAGE"
SAMBA_VFS_WORK=$WORK bash "$REPO/samba-vfs/build.sh" "$WORK/omnifile_prevver.so"
install -m 0644 "$WORK/omnifile_prevver.so" "$SO"
echo "$ver" >"$STAGE/smbd-version"
rm -rf "$WORK"
log "staged $SO (step 8 of tests/e2e/m5_exit_test.sh installs it into smbd for its run)"
