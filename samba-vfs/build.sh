#!/usr/bin/env bash
# Build omnifile_prevver.so for the smbd on this host (GPL-3, see COPYING).
#
#   samba-vfs/build.sh [OUT.so]          default: samba-vfs/build/omnifile_prevver.so
#
# Samba has no out-of-tree module SDK (Ubuntu's samba-dev ships no VFS
# headers), so the module is compiled inside the matching upstream release:
#
# 1. The release is the installed smbd's version (or $SAMBA_VERSION). Its
#    tarball is fetched from download.samba.org (or found in $SAMBA_TARBALL_DIR)
#    and must match the SHA-256 pinned in SHA256SUMS, which was recorded after
#    checking the release's signature with Samba's key (AA99442FB680B620).
# 2. The module is added to source3/modules, Samba is configured with almost
#    everything off, and waf builds the module's object.
# 3. That object is linked alone, against no library: smbd already holds every
#    symbol it uses when it loads the module. So the same .so loads into the
#    distribution's smbd (Ubuntu names its private libraries differently from
#    an upstream build). It is safe because nothing the module touches depends
#    on configure options: smb_filename, files_struct, connection_struct,
#    share_params and vfs_fn_pointers have no conditional fields, and smbd
#    refuses a module built for another VFS interface version.
#
# Build dependencies (Ubuntu 26.04): build-essential python3-dev
# libgnutls28-dev flex bison perl pkg-config zlib1g-dev libtirpc-dev
# libacl1-dev libpopt-dev libicu-dev libparse-yapp-perl libreadline-dev.
# Takes about 3 minutes on 4 cores; the work tree ($SAMBA_VFS_WORK, default
# samba-vfs/build) needs about 1 GB.
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
WORK=${SAMBA_VFS_WORK:-$HERE/build}
OUT=${1:-$WORK/omnifile_prevver.so}
log() { printf '[samba-vfs] %s\n' "$*"; }
die() { printf '[samba-vfs] ERROR: %s\n' "$*" >&2; exit 1; }

VER=${SAMBA_VERSION:-}
if [ -n "$VER" ] && command -v smbd >/dev/null &&
  [ "$(smbd --version | sed -nE 's/^Version ([0-9]+\.[0-9]+\.[0-9]+).*/\1/p')" != "$VER" ]; then
  SAMBA_VERSION_OVERRIDDEN=1 # built for another release than this host's smbd
fi
if [ -z "$VER" ]; then
  command -v smbd >/dev/null || die "no smbd here: set SAMBA_VERSION"
  VER=$(smbd --version | sed -nE 's/^Version ([0-9]+\.[0-9]+\.[0-9]+).*/\1/p')
  [ -n "$VER" ] || die "cannot read smbd's version from: $(smbd --version)"
fi
TAR=samba-$VER.tar.gz
SUM=$(awk -v f="$TAR" '$2 == f { print $1 }' "$HERE/SHA256SUMS")
[ -n "$SUM" ] || die "no pinned checksum for $TAR: check its signature, then add it to SHA256SUMS"

mkdir -p "$WORK"
if [ -n "${SAMBA_TARBALL_DIR:-}" ] && [ -f "$SAMBA_TARBALL_DIR/$TAR" ]; then
  cp "$SAMBA_TARBALL_DIR/$TAR" "$WORK/$TAR.part"
  mv "$WORK/$TAR.part" "$WORK/$TAR"
elif [ ! -f "$WORK/$TAR" ]; then
  log "fetching $TAR"
  curl -sSfL -o "$WORK/$TAR.part" "https://download.samba.org/pub/samba/stable/$TAR"
  mv "$WORK/$TAR.part" "$WORK/$TAR"
fi
echo "$SUM  $WORK/$TAR" | sha256sum -c --quiet - || die "$TAR does not match its pinned SHA-256"

SRC=$WORK/samba-$VER
rm -rf "$SRC"
tar -xzf "$WORK/$TAR" -C "$WORK"
cp "$HERE/vfs_omnifile_prevver.c" "$SRC/source3/modules/"
# The release goes into the object as build info (see vfs_omnifile_prevver.c), as three
# plain numbers: no quoting to get wrong between waf and the compiler.
IFS=. read -r SAMBA_MAJOR SAMBA_MINOR SAMBA_PATCH <<<"$VER"
cat >>"$SRC/source3/modules/wscript_build" <<EOF

bld.SAMBA3_MODULE('vfs_omnifile_prevver',
                 subsystem='vfs',
                 source='vfs_omnifile_prevver.c',
                 deps='samba-util',
                 init_function='vfs_omnifile_prevver_init',
                 internal_module=False,
                 cflags='-DOMNIFILE_SAMBA_MAJOR=$SAMBA_MAJOR -DOMNIFILE_SAMBA_MINOR=$SAMBA_MINOR -DOMNIFILE_SAMBA_PATCH=$SAMBA_PATCH',
                 enabled=True)
EOF

log "configuring Samba $VER (minimal)"
cd "$SRC"
export PYTHONHASHSEED=1
./configure --without-ad-dc --without-ldap --without-ads --disable-python --without-json \
  --without-libarchive --without-pam --without-systemd --without-quotas --disable-cups \
  --disable-iprint --without-gpgme --without-winbind --without-syslog --disable-cephfs \
  --disable-glusterfs --without-lttng --without-regedit --without-fam --without-libunwind \
  --without-ldb-lmdb --bundled-libraries=ALL '--with-shared-modules=!vfs_snapper' \
  >"$WORK/configure.log" 2>&1 || { tail -20 "$WORK/configure.log"; die "configure failed ($WORK/configure.log)"; }

log "building the module"
./buildtools/bin/waf build -j"$(nproc)" --targets=vfs_omnifile_prevver \
  >"$WORK/build.log" 2>&1 || { grep -m20 -E 'error:|Error' "$WORK/build.log"; die "build failed ($WORK/build.log)"; }
OBJ=$(find bin/default/source3/modules -name 'vfs_omnifile_prevver.c.*.o' | head -n 1)
[ -n "$OBJ" ] || die "no object for vfs_omnifile_prevver"

mkdir -p "$(dirname "$OUT")"
cc -shared -Wl,-z,relro,-z,now -o "$OUT.part" "$OBJ"
mv "$OUT.part" "$OUT"
# The entry point smbd looks for, and no library but glibc's: libc, and on arm64 the
# dynamic loader too (glibc keeps __stack_chk_guard there, not in libc). smbd, like
# every dynamically linked program, has both loaded already.
nm -D --defined-only "$OUT" | grep -qw samba_init_module || die "$OUT has no samba_init_module"
extra=$(readelf -d "$OUT" | sed -nE 's/.*\(NEEDED\).*\[(.*)\]/\1/p' |
  grep -vxE 'libc\.so\.6|ld-linux[-a-z0-9_]*\.so\.[0-9]+' || true)
[ -z "$extra" ] || die "$OUT links $extra; it must link only glibc (libc and the dynamic loader)"
# The build info the module carries (see vfs_omnifile_prevver.c): omniacl check-prevver and
# the edge read it to catch a module built for another VFS interface than the smbd it runs in.
info=$(grep -ao 'OMNIFILE-PREVVER-BUILD [ -~]*' "$OUT" | head -n 1 || true)
[ -n "$info" ] || die "$OUT carries no OMNIFILE-PREVVER-BUILD info"
log "build info: $info"
# On a host with smbd: every Samba symbol the module uses must be exported by
# the libraries that smbd loads, or smbd would refuse it at load time.
if command -v smbd >/dev/null && [ -z "${SAMBA_VERSION_OVERRIDDEN:-}" ]; then
  have=$(ldd "$(command -v smbd)" | awk '$3 ~ /^\// { print $3 }' |
    xargs -r nm -D --defined-only 2>/dev/null | awk '{ print $NF }' | sed 's/@.*//' | sort -u)
  missing=$(nm -D --undefined-only "$OUT" | awk '$1 == "U" { print $2 }' | grep -v '@GLIBC' |
    sort -u | comm -23 - <(echo "$have"))
  [ -z "$missing" ] || die "this host's smbd does not export: $(echo $missing)"
  log "every symbol it uses is exported by this host's smbd ($(smbd --version))"
fi
log "built $OUT for Samba $VER (VFS interface $(sed -nE 's/^#define SMB_VFS_INTERFACE_VERSION ([0-9]+).*/\1/p' source3/include/vfs.h))"
