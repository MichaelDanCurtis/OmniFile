#!/usr/bin/env bash
# Install NFS-Ganesha V15.6 + FSAL_OMNI on the edge (dev VM or the e2e CI
# runner), as the appliance runs it: /opt/ganesha, the module, and Ganesha
# under its real unit (fsal/systemd/nfs-ganesha-omni.service), never
# --privileged.
#
#   # on the Mac (or any Docker host of the same architecture):
#   dev/scripts/build-artifacts.sh
#   # then, inside the edge VM as root (the repo is at /work):
#   /work/dev/scripts/install-ganesha.sh /work/dev/.artifacts
#
# The artefacts directory holds ganesha.tar (/opt/ganesha exported from the
# fsal/build image: the pinned build every FSAL test runs against) and
# fsal/libfsalomni.so (built against that same tree). A directory holding an
# unpacked /opt/ganesha tree (bin/ganesha.nfsd ...) also works, with the
# module at <dir>/lib/ganesha/libfsalomni.so.
#
# Lays down /etc/ganesha/omnifile-base.conf (dev/edge/ganesha-base.conf: the
# appliance settings, including MDCACHE Close_Fast) and, only when absent, a
# ganesha.conf that includes it with no exports (the exports belong to whoever
# runs Ganesha: tests/e2e writes its own). Does not start Ganesha.
set -euo pipefail

SRC="${1:-}"
PREFIX=/opt/ganesha
REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
UNIT_SRC="$REPO/fsal/systemd/nfs-ganesha-omni.service"

log() { printf '[install-ganesha] %s\n' "$*"; }
die() { printf '[install-ganesha] ERROR: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "must run as root (inside the edge VM)"
[ -n "$SRC" ] || { sed -n '2,/^set -euo/p' "$0" | sed '$d' >&2; exit 2; }
[ -f "$UNIT_SRC" ] || die "$UNIT_SRC not found (set REPO=)"

# Runtime libraries of the pinned build (the dev VM already has their -dev
# packages; a bare runner may not).
export DEBIAN_FRONTEND=noninteractive
need=()
for p in rpcbind nfs-common libjemalloc2 libdbus-1-3 libnfsidmap1 libcap2 libblkid1 \
         libkrb5-3 libgssapi-krb5-2 libacl1 libnsl2 libsqlite3-0 attr xfsprogs; do
  dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q 'install ok installed' || need+=("$p")
done
# liburcu's runtime package name carries its soname; take whichever exists.
dpkg -l 'liburcu*' 2>/dev/null | grep -q '^ii' || need+=(liburcu-dev)
if [ "${#need[@]}" -gt 0 ]; then
  log "installing ${need[*]}"
  apt-get update -qq
  apt-get install -y -qq -o DPkg::Lock::Timeout=600 "${need[@]}" >/dev/null
fi

systemctl stop nfs-ganesha-omni.service 2>/dev/null || true

stage="$PREFIX.new"
rm -rf "$stage"
install -d "$stage"
if [ -f "$SRC/ganesha.tar" ]; then
  tar -xf "$SRC/ganesha.tar" -C "$stage" --strip-components=1
  module="$SRC/fsal/libfsalomni.so"
elif [ -x "$SRC/bin/ganesha.nfsd" ]; then
  cp -a "$SRC"/. "$stage"/
  module="$SRC/lib/ganesha/libfsalomni.so"
else
  die "$SRC holds neither ganesha.tar nor an /opt/ganesha tree"
fi
[ -f "$module" ] || die "FSAL_OMNI module not found at $module (dev/scripts/build-artifacts.sh)"
install -m 0755 "$module" "$stage/lib/ganesha/libfsalomni.so"
rm -rf "$PREFIX.old"
[ -d "$PREFIX" ] && mv "$PREFIX" "$PREFIX.old"
mv "$stage" "$PREFIX"
rm -rf "$PREFIX.old"

echo "$PREFIX/lib" >/etc/ld.so.conf.d/ganesha.conf
ldconfig
missing=$(ldd "$PREFIX/bin/ganesha.nfsd" "$PREFIX/lib/ganesha/libfsalomni.so" \
  "$PREFIX/lib/ganesha/libfsalvfs.so" | grep 'not found' || true)
[ -z "$missing" ] || die "unresolved libraries: $missing"
ldd "$PREFIX/lib/ganesha/libfsalomni.so" | grep -q libjemalloc \
  || die "libfsalomni.so is not linked with jemalloc (fsal/README.md)"

install -d -m 0755 /etc/ganesha /var/lib/nfs/ganesha "$PREFIX/var/lib/nfs/ganesha" \
  "$PREFIX/var/run/ganesha"
install -m 0644 "$UNIT_SRC" /etc/systemd/system/nfs-ganesha-omni.service
# The base configuration (core, NFSv4, MDCACHE Close_Fast, OMNI): always
# replaced. ganesha.conf (which includes it and holds the exports) is the
# operator's: written only when absent, with no exports.
install -m 0644 "$REPO/dev/edge/ganesha-base.conf" /etc/ganesha/omnifile-base.conf
[ -f /etc/ganesha/ganesha.conf ] || printf '%%include "/etc/ganesha/omnifile-base.conf"\n# EXPORT { ... FSAL { Name = OMNI; Policy = "wait"; FSAL { Name = VFS; } } }\n' \
  >/etc/ganesha/ganesha.conf
if [ -f "$SRC/org.ganesha.nfsd.conf" ]; then
  install -D -m 0644 "$SRC/org.ganesha.nfsd.conf" /etc/dbus-1/system.d/org.ganesha.nfsd.conf
  systemctl reload dbus >/dev/null 2>&1 || true
fi
# The distribution's Ganesha or kernel NFS server must not hold port 2049.
for u in nfs-ganesha nfs-server nfs-kernel-server; do
  systemctl disable --now "$u" >/dev/null 2>&1 || true
done
systemctl enable --now rpcbind.socket >/dev/null 2>&1 || systemctl enable --now rpcbind >/dev/null 2>&1 || true
systemctl daemon-reload

"$PREFIX/bin/ganesha.nfsd" -v | head -n 2
log "installed $PREFIX with FSAL_OMNI and nfs-ganesha-omni.service ($(sha256sum "$PREFIX/lib/ganesha/libfsalomni.so" | cut -c1-16))"
