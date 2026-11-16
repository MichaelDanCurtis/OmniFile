#!/usr/bin/env bash
# Build the OmniFile edge appliance image (decision 38; appliance/README.md).
#
#   appliance/build.sh [--arch x86-64|arm64] [--native] [--version V] [--bin-dir DIR]
#                      [--artifacts DIR] [--prevver SO] [--out DIR] [--pubring FILE]
#                      [--signing-helper FILE] [--feed-url URL] [--source-date-epoch N]
#
# 1. Stages our components into build/stage (or $APPLIANCE_STAGE), laid out as the image has
#    them (all under /usr, so an update replaces them with the OS):
#      /usr/local/bin           omnifile-edge, omnifile-annex, omnifile-guardctl, smbguard-tool, omnifile-setup, omniacl
#      /usr/lib/systemd/system  omnifile-edge.service, nfs-ganesha-omni.service, and the
#                               smb-guard drop-ins (edge/systemd/install.sh --root)
#      /usr/lib/omnifile/ganesha  NFS-Ganesha with FSAL_OMNI (dev/.artifacts, the pinned
#                               build every FSAL test runs against); /opt/ganesha links here
#      <samba MODULESDIR>/vfs   vfs_omnifile_prevver (samba-vfs/, GPL-3, decision 36)
#      /usr/share/omnifile      templates the setup wizard renders
#      /usr/libexec/omnifile/aws_signing_helper  AWS's IAM Roles Anywhere helper (Apache-2.0),
#                               pinned by version and SHA-256 per architecture in
#                               third-party/aws_signing_helper/PIN: fetched from AWS, or
#                               --signing-helper FILE for an offline build, and refused
#                               unless it matches
# 2. Runs mkosi (Ubuntu 26.04's own, 26): in a privileged Ubuntu 26.04 container (docker), or
#    with --native on this host, which must then be Ubuntu 26.04 with mkosi and its tools
#    (README.md, "Building"), as root.
#
# --arch: x86-64 is the product (the default). arm64 builds the same image for arm64, a test
# build: on Apple silicon its gate and tests run at native speed under HVF
# (tests/vm.sh). The binaries, Ganesha, the prevver module and the Roles Anywhere helper must
# be that architecture's; the build refuses any ELF file of another.
#
# Reproducible (docs/design/appliance-image.md, "Reproducible builds"): mkosi.conf pins the
# archive snapshot (Snapshot=) and systemd-repart's seed (Seed=), and every file's mtime is
# clamped to SOURCE_DATE_EPOCH: the environment's, --source-date-epoch, else the commit time of
# HEAD, else the snapshot's time. The same inputs give the same /usr, verity and UKI. The
# build records what it used in OUT/BUILD-INFO.
#
# Outputs in --out (default appliance/build/out): omnifile-edge_V.raw (the disk), and in
# feed/ what an update server serves to systemd-sysupdate: omnifile-edge_V.efi,
# omnifile-edge_V.usr-<arch>.<partuuid>.raw.zst, omnifile-edge_V.usr-<arch>-verity.<partuuid>.raw.zst
# and SHA256SUMS. The feed is not signed here: appliance/release.sh signs it, and only after
# the new image's kernel passed the smb-guard A-suite and the kernel canary (risk R3).
# --pubring puts the keyring the updates are checked against into the image
# (/usr/lib/systemd/import-pubring.pgp, replacing the distribution's, so only our key is
# trusted). Without it the image trusts only the distribution's keys, which sign none of
# our feeds, so every update is refused (README.md, "Updates").
# The build needs about 8 GB of disk.
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$HERE/.." && pwd)
VERSION=0.1.0
ARCH=x86-64
NATIVE=0
BIN=$REPO/edge/target/release
ART=$REPO/dev/.artifacts
PREVVER=$REPO/samba-vfs/build/omnifile_prevver.so
OUT=$HERE/build/out
PUBRING=
SDE=${SOURCE_DATE_EPOCH:-}
# The update feed the image's sysupdate transfers read (release.conf: GitHub Releases).
# shellcheck source=release.conf
. "$HERE/release.conf"
SIGNING_HELPER=
while [ $# -gt 0 ]; do
  case "$1" in
    --arch) ARCH=$2; shift ;;
    --native) NATIVE=1 ;;
    --version) VERSION=$2; shift ;;
    --bin-dir) BIN=$2; shift ;;
    --artifacts) ART=$2; shift ;;
    --prevver) PREVVER=$2; shift ;;
    --out) OUT=$2; shift ;;
    --pubring) PUBRING=$2; shift ;;
    --feed-url) FEED_URL=$2; shift ;;
    --signing-helper) SIGNING_HELPER=$2; shift ;;
    --source-date-epoch) SDE=$2; shift ;;
    --sources-repo) SOURCES_REPO=$2; shift ;;
    --sign-key) echo "build.sh no longer signs: appliance/release.sh signs a feed once its kernel passed the R3 gate" >&2; exit 2 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
  shift
done
mkdir -p "$OUT" && OUT=$(cd "$OUT" && pwd)
log() { printf '[appliance] %s\n' "$*"; }
die() { printf '[appliance] ERROR: %s\n' "$*" >&2; exit 1; }
# The architecture's names: Debian's multiarch triple (Samba's module directory), the ELF
# machine number every staged binary must have, and the PIN entry of the helper.
case $ARCH in
  x86-64) TRIPLE=x86_64-linux-gnu ELF_MACHINE=62 PIN_ARCH=x86_64 ;;
  arm64) TRIPLE=aarch64-linux-gnu ELF_MACHINE=183 PIN_ARCH=arm64 ;;
  *) die "--arch $ARCH: x86-64 (the product) or arm64 (a test build)" ;;
esac
for f in "$BIN/omnifile-edge" "$BIN/omnifile-annex" "$BIN/omnifile-guardctl" "$BIN/smbguard-tool" "$BIN/omnifile-setup" "$BIN/examples/omniacl" \
  "$ART/ganesha.tar" "$ART/fsal/libfsalomni.so" "$PREVVER"; do
  [ -f "$f" ] || die "missing $f (edge: cargo build --release; Ganesha: dev/scripts/build-artifacts.sh; samba-vfs/build.sh)"
done
# elf_machine FILE: the e_machine field of an ELF file (little-endian), or nothing.
elf_machine() {
  [ "$(head -c 4 "$1" | od -An -c | tr -d ' ')" = '177ELF' ] || return 0
  od -An -j18 -N2 -tu2 "$1" | tr -d ' '
}
check_arch() { # check_arch FILE...: every ELF file is for ARCH
  local f m
  for f in "$@"; do
    m=$(elf_machine "$f")
    [ -z "$m" ] || [ "$m" = "$ELF_MACHINE" ] || die "$f is not an $ARCH binary (ELF machine $m, want $ELF_MACHINE)"
  done
}
check_arch "$BIN/omnifile-edge" "$BIN/omnifile-annex" "$BIN/omnifile-guardctl" "$BIN/smbguard-tool" "$BIN/omnifile-setup" \
  "$BIN/examples/omniacl" "$ART/fsal/libfsalomni.so" "$PREVVER"

# aws_signing_helper, pinned (third-party/aws_signing_helper/PIN): the helper's unit runs it
# from /usr/libexec/omnifile, which on the appliance is the read-only, verity-checked /usr.
# shellcheck source=third-party/aws_signing_helper/PIN
. "$HERE/third-party/aws_signing_helper/PIN"
eval "HELPER_URL=\$HELPER_URL_$PIN_ARCH HELPER_SHA256=\$HELPER_SHA256_$PIN_ARCH"
if [ -z "$SIGNING_HELPER" ]; then
  SIGNING_HELPER=$HERE/build/cache/aws_signing_helper-$HELPER_VERSION-$PIN_ARCH
  if [ ! -f "$SIGNING_HELPER" ]; then
    mkdir -p "$HERE/build/cache"
    log "fetching aws_signing_helper $HELPER_VERSION ($ARCH)"
    curl -sSfL -o "$SIGNING_HELPER.part" "$HELPER_URL" || die "could not fetch $HELPER_URL (or give --signing-helper FILE)"
    mv "$SIGNING_HELPER.part" "$SIGNING_HELPER"
  fi
fi
[ -f "$SIGNING_HELPER" ] || die "missing $SIGNING_HELPER"
got=$(sha256sum "$SIGNING_HELPER" | cut -d' ' -f1)
[ "$got" = "$HELPER_SHA256" ] ||
  die "aws_signing_helper $HELPER_VERSION ($ARCH): SHA-256 $got is not the pinned $HELPER_SHA256 ($SIGNING_HELPER)"
log "aws_signing_helper $HELPER_VERSION ($ARCH) matches its pinned SHA-256"

# SOURCE_DATE_EPOCH: given, else HEAD's commit time, else the pinned snapshot's time.
SNAPSHOT=$(sed -n 's/^Snapshot=//p' "$HERE/mkosi.conf")
SEED=$(sed -n 's/^Seed=//p' "$HERE/mkosi.conf")
if [ -z "$SDE" ]; then
  SDE=$(git -C "$REPO" log -1 --format=%ct 2>/dev/null || true)
fi
if [ -z "$SDE" ] && [ -n "$SNAPSHOT" ]; then
  SDE=$(date -u -d "$(sed -E 's/^(....)(..)(..)T(..)(..)(..)Z$/\1-\2-\3 \4:\5:\6/' <<<"$SNAPSHOT")" +%s)
fi
case $SDE in '' | *[!0-9]*) die "no SOURCE_DATE_EPOCH (set it, or --source-date-epoch N)" ;; esac
log "architecture $ARCH, snapshot $SNAPSHOT, seed $SEED, SOURCE_DATE_EPOCH $SDE ($(date -u -d "@$SDE" +%FT%TZ))"

STAGE=${APPLIANCE_STAGE:-$HERE/build/stage}
rm -rf "$STAGE"
mkdir -p "$STAGE/usr/local/bin" "$STAGE/usr/lib/systemd/system" "$STAGE/usr/lib/omnifile" \
  "$STAGE/usr/share/omnifile" "$OUT"
for b in omnifile-edge omnifile-annex omnifile-guardctl smbguard-tool omnifile-setup; do
  install -m 0755 "$BIN/$b" "$STAGE/usr/local/bin/$b"
done
install -m 0755 "$BIN/examples/omniacl" "$STAGE/usr/local/bin/omniacl"
install -m 0644 "$REPO/edge/crates/omnifile-edge/systemd/omnifile-edge.service" "$STAGE/usr/lib/systemd/system/"
# Annexes: the supervisor and the per-annex template (docs/design/annex.md).
install -m 0644 "$REPO/edge/crates/omnifile-edge/systemd/omnifile-annex.service" \
  "$REPO/edge/crates/omnifile-edge/systemd/omnifile-annex@.service" "$STAGE/usr/lib/systemd/system/"
install -m 0644 "$REPO/fsal/systemd/nfs-ganesha-omni.service" "$STAGE/usr/lib/systemd/system/"
bash "$REPO/edge/systemd/install.sh" --root "$STAGE" --unitdir /usr/lib/systemd/system \
  --signing-helper "$SIGNING_HELPER" >/dev/null
# Apache-2.0 asks for its licence and NOTICE with the binary (/usr/share/doc is removed).
# (mkdir first: Ubuntu 26.04's install -D makes one missing directory level, not two.)
mkdir -p "$STAGE/usr/share/omnifile/licenses/aws_signing_helper"
install -D -m 0644 "$HERE/third-party/aws_signing_helper/LICENSE" \
  "$STAGE/usr/share/omnifile/licenses/aws_signing_helper/LICENSE"
install -D -m 0644 "$HERE/third-party/aws_signing_helper/NOTICE" \
  "$STAGE/usr/share/omnifile/licenses/aws_signing_helper/NOTICE"
echo "aws_signing_helper $HELPER_VERSION sha256 $HELPER_SHA256" \
  >"$STAGE/usr/share/omnifile/licenses/aws_signing_helper/VERSION"
# The written GPL source offer (decision 46), in the image itself, with the licences of the
# GPL and LGPL parts: the image keeps every package's /usr/share/doc/<package>/copyright and
# base-files' /usr/share/common-licenses; our two modules' licences go next to the offer. The
# offer is source-offer/OFFER.md.in (WP6's, read here, never changed), filled in with what a
# build knows: the release, its tag in SOURCES_REPO (release.conf) and the archive snapshot.
# The package count and size come with the release's own SOURCE-OFFER.md.
case $SOURCES_REPO in */*) ;; *) die "--sources-repo $SOURCES_REPO: owner/name" ;; esac
DOC=$STAGE/usr/share/doc/omnifile
mkdir -p "$DOC/FSAL_OMNI" "$DOC/vfs_omnifile_prevver"
python3 - "$HERE/source-offer/OFFER.md.in" "$DOC/GPL-SOURCE-OFFER.txt" "$VERSION" "$SOURCES_REPO" \
  "$(sed -n 's/^Snapshot=//p' "$HERE/mkosi.conf")" "$(date -u -d "@$SDE" +%F)" <<'PY' || die "the source offer's template no longer fits build.sh"
import re, sys
src, out, release, repo, snapshot, date = sys.argv[1:]
t = open(src).read()
t = t.replace("{{UBUNTU_SOURCES}} Ubuntu source packages", "The Ubuntu source packages")
t = re.sub(r" ?About \{\{TOTAL_MB\}\} MB in all\.", "", t)
t = re.sub(r"Generated \{\{DATE\}\} by `appliance/source-offer/source_offer.py`\.",
           "Installed in this image ({{DATE}}) by appliance/build.sh from appliance/source-offer/OFFER.md.in.\n"
           "The release's own SOURCE-OFFER.md, attached to the GitHub Release above, adds the package\n"
           "count and size.", t)
for k, v in {"RELEASE": release, "TAG": "v" + release, "SOURCES_REPO": repo, "SNAPSHOT": snapshot,
             "CONTACT_EXTRA": "", "DATE": date}.items():
    t = t.replace("{{%s}}" % k, v)
left = sorted(set(re.findall(r"\{\{[A-Z_]+\}\}", t)))
if left:
    sys.exit(f"unfilled: {left}")
open(out, "w").write(t)
PY
install -m 0644 "$REPO/fsal/COPYING" "$REPO/fsal/COPYING.LESSER" "$DOC/FSAL_OMNI/"
install -m 0644 "$REPO/samba-vfs/COPYING" "$DOC/vfs_omnifile_prevver/"
cat >"$DOC/README" <<EOF
OmniFile edge appliance $VERSION: licences and source.

GPL-SOURCE-OFFER.txt   where this release's GPL and LGPL source is, and the written offer
FSAL_OMNI/             the NFS-Ganesha module's licence (LGPL-3.0; COPYING is the GPL-3.0 it extends)
vfs_omnifile_prevver/  the Samba module's licence (GPL-3.0-or-later)
/usr/share/doc/<package>/copyright   each Ubuntu package's copyright and licence
/usr/share/common-licenses/          the full GPL-2, GPL-3, LGPL-2.1 and LGPL-3 texts
/usr/share/omnifile/licenses/        third-party binaries OmniFile adds (aws_signing_helper, Apache-2.0)
EOF
# The presets decide what starts (mkosi.extra/usr/lib/systemd/system-preset), but install.sh's
# vendor .wants link of the guard unit stays: it is the systemd-level interlock (`systemctl
# disable` removes links under /etc only, so a stopped guard stays referenced and keeps its fd
# store; without it a `disable --now` would release the group and let pending reads through).
# A-suite A7 checks the image has it.
for l in "$STAGE"/usr/lib/systemd/system/multi-user.target.wants/*; do
  case ${l##*/} in omnifile-edge.service | omnifile-aws-signing-helper.service) ;; *) rm -f "$l" ;; esac
done
[ -L "$STAGE/usr/lib/systemd/system/multi-user.target.wants/omnifile-edge.service" ] ||
  die "edge/systemd/install.sh made no vendor .wants link for omnifile-edge.service"
# Presets only apply to a new /etc, and an A/B update never touches /etc: the units this image
# adds must start on an updated box too, so they are wanted from /usr (vendor links).
mkdir -p "$STAGE/usr/lib/systemd/system/sysinit.target.wants"
for u in omnifile-firewall.service omnifile-ssh-keygen.service; do
  [ -f "$HERE/mkosi.extra/usr/lib/systemd/system/$u" ] || die "no $u in mkosi.extra"
  ln -sfn "../$u" "$STAGE/usr/lib/systemd/system/sysinit.target.wants/$u"
done
# The firewall fails closed: with no firewall loaded there is no network, and nothing listens.
# Each of these requires omnifile-firewall.service and starts after it, so if `nft -f` fails
# they don't start (Before=network-pre.target alone would only order them).
for u in systemd-networkd.service systemd-resolved.service ssh.socket ssh.service smbd.service nfs-ganesha-omni.service \
  rpcbind.socket rpcbind.service rpc-statd.service; do
  mkdir -p "$STAGE/usr/lib/systemd/system/$u.d"
  printf '%s\n' "# The inbound firewall first, or not at all (build.sh)." "[Unit]" \
    "Requires=omnifile-firewall.service" "After=omnifile-firewall.service" \
    >"$STAGE/usr/lib/systemd/system/$u.d/05-omnifile-firewall.conf"
done
tar -xf "$ART/ganesha.tar" -C "$STAGE/usr/lib/omnifile"
install -m 0755 "$ART/fsal/libfsalomni.so" "$STAGE/usr/lib/omnifile/ganesha/lib/ganesha/libfsalomni.so"
check_arch "$STAGE/usr/lib/omnifile/ganesha/bin/ganesha.nfsd" "$STAGE/usr/libexec/omnifile/aws_signing_helper"
# Its config and state belong outside /usr. Ganesha is built with prefix /opt/ganesha, so its
# compiled defaults are /opt/ganesha/etc/... and /opt/ganesha/var/... (among them the NFSv4
# recovery root, var/lib/nfs/ganesha: the clients' grace records). /opt/ganesha is this tree on
# the read-only /usr, so etc and var point at the root partition's /etc and /var: the records
# land in /var/lib/nfs/ganesha (tmpfiles.d/omnifile.conf), which survives reboots and A/B
# updates. Without that, Ganesha can't make its recovery root and stops at start.
rm -rf "$STAGE/usr/lib/omnifile/ganesha/etc" "$STAGE/usr/lib/omnifile/ganesha/var"
ln -s /etc "$STAGE/usr/lib/omnifile/ganesha/etc"
ln -s /var "$STAGE/usr/lib/omnifile/ganesha/var"
# Ganesha's D-Bus policy (root may own org.ganesha.nfsd): without it Ganesha logs "server bus
# reg failed" (CRIT) and has no admin interface. dev/scripts/build-artifacts.sh exports it.
if [ -f "$ART/org.ganesha.nfsd.conf" ]; then
  mkdir -p "$STAGE/usr/share/dbus-1/system.d"
  install -m 0644 "$ART/org.ganesha.nfsd.conf" "$STAGE/usr/share/dbus-1/system.d/org.ganesha.nfsd.conf"
else
  log "no $ART/org.ganesha.nfsd.conf: Ganesha will run without its D-Bus interface"
fi
mkdir -p "$STAGE/usr/lib/$TRIPLE/samba/vfs" "$STAGE/usr/share/pam-configs"
install -D -m 0644 "$PREVVER" "$STAGE/usr/lib/$TRIPLE/samba/vfs/omnifile_prevver.so"
install -m 0644 "$REPO/edge/config/ganesha/omnifile-base.conf" "$STAGE/usr/share/omnifile/ganesha-base.conf"
# The pam-auth-update profile that records every session of the break-glass account.
install -D -m 0644 "$REPO/edge/config/pam/omnifile-recovery" "$STAGE/usr/share/pam-configs/omnifile-recovery"
install -m 0644 "$REPO/edge/config/samba/smb.conf.tmpl" "$STAGE/usr/share/omnifile/smb.conf.tmpl"
install -m 0644 "$REPO/dev/edge/edge.toml" "$STAGE/usr/share/omnifile/edge.toml.tmpl"
# The sysupdate transfers, with the feed written in (sysupdate.d/*.transfer.in), and the
# same URL for the setup wizard to offer.
case $FEED_URL in https://* | http://10.0.2.2:*) ;; *) die "--feed-url $FEED_URL: give an https:// URL" ;; esac
case $FEED_URL in */) ;; *) FEED_URL=$FEED_URL/ ;; esac
mkdir -p "$STAGE/usr/lib/sysupdate.d" "$STAGE/usr/lib/omnifile"
for t in "$HERE"/sysupdate.d/*.transfer.in; do
  n=${t##*/}
  sed "s|@FEED_URL@|$FEED_URL|" "$t" >"$STAGE/usr/lib/sysupdate.d/${n%.in}"
done
printf '%s\n' "$FEED_URL" >"$STAGE/usr/lib/omnifile/update-feed"
log "update feed: $FEED_URL"
if [ -n "$PUBRING" ]; then
  install -D -m 0644 "$PUBRING" "$STAGE/usr/lib/systemd/import-pubring.pgp"
else
  log "no --pubring: this image will refuse every update"
fi
log "staged $(du -sh "$STAGE" | cut -f1) of OmniFile components (version $VERSION)"

# A previous build of the same version would leave split files with other UUIDs behind.
rm -rf "${OUT:?}/feed" "${OUT:?}/omnifile-edge_${VERSION:?}".*
# The mkosi step, the same in the container and natively. Its inputs come as variables:
# APPLIANCE (this directory), STAGE, OUT, WORKSPACE, VERSION, ARCH, SDE, and TOOLS=1 to
# install mkosi and its tools first (the container).
# shellcheck disable=SC2016
STEP='
set -euo pipefail
if [ "${TOOLS:-0}" = 1 ]; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq >/dev/null
  # ca-certificates: the Snapshot= in mkosi.conf is https://snapshot.ubuntu.com, which apt in
  # the mkosi sandbox cannot verify without a CA store. The ubuntu image ships none, and
  # nothing here pulls it in without recommends (the first container build, WP14 A2.3).
  # udev: systemd-hwdb, which mkosi runs from here to write /usr/lib/udev/hwdb.bin into
  # the image ("systemd-hwdb is not installed, not generating hwdb" otherwise). /usr is
  # read-only, so the box cannot make it at boot (appliance-image.md, "Every boot").
  apt-get install -y -qq --no-install-recommends mkosi systemd-repart systemd-ukify \
    systemd-boot-efi dosfstools mtools erofs-utils xfsprogs e2fsprogs cryptsetup-bin \
    bubblewrap debootstrap ubuntu-keyring zstd cpio kmod patchelf ca-certificates udev >/dev/null
fi
# Ganesha finds its own libraries next to it, not through /etc/ld.so.cache: /etc is on
# the root partition, which an A/B update of /usr does not touch.
g=$STAGE/usr/lib/omnifile/ganesha
patchelf --set-rpath "\$ORIGIN/../lib" "$g/bin/ganesha.nfsd"
for f in "$g"/lib/ganesha/*.so; do patchelf --set-rpath "\$ORIGIN/.." "$f"; done
# RUNPATH is not inherited: libganesha_nfsd needs its own to find libntirpc.
for f in "$g"/lib/lib*.so.*.*; do patchelf --set-rpath "\$ORIGIN" "$f"; done
cd "$APPLIANCE"
mkosi --architecture="$ARCH" --image-version="$VERSION" --source-date-epoch="$SDE" \
  ${PKGCACHE:+--package-cache-dir="$PKGCACHE"} \
  --extra-tree="$STAGE" --output-directory="$OUT" --workspace-directory="$WORKSPACE" --force build
rm -rf "$OUT/feed" && mkdir "$OUT/feed"
cp "$OUT/omnifile-edge_$VERSION.efi" "$OUT/feed/"
for f in "$OUT"/omnifile-edge_"$VERSION".usr-*.raw; do
  zstd -q -T0 -f "$f" -o "$OUT/feed/${f##*/}.zst"
done
{
  echo "mkosi=$(mkosi --version | cut -d" " -f2)"
  # udev too: its systemd-hwdb wrote the image hwdb.bin, so its version is an input of /usr.
  for p in systemd-repart systemd-ukify systemd-boot-efi erofs-utils zstd patchelf udev; do
    echo "$p=$(dpkg-query -W -f "\${Version}" "$p" 2>/dev/null || echo unknown)"
  done
} >"$OUT/BUILD-INFO.tools"
'
if [ $NATIVE = 1 ]; then
  [ "$(id -u)" = 0 ] || die "--native runs mkosi as root (sudo)"
  for t in mkosi patchelf zstd systemd-repart ukify mkfs.erofs; do
    command -v $t >/dev/null || die "--native: $t is missing (README.md, \"Building\")"
  done
  log "running mkosi natively ($(mkosi --version))"
  # APPLIANCE_WORKSPACE (default /var/tmp) holds the image tree while it is built, about 6 GB;
  # APPLIANCE_PACKAGE_CACHE keeps the downloaded packages between builds.
  APPLIANCE=$HERE STAGE=$STAGE OUT=$OUT WORKSPACE=${APPLIANCE_WORKSPACE:-/var/tmp} VERSION=$VERSION \
    ARCH=$ARCH SDE=$SDE TOOLS=0 PKGCACHE=${APPLIANCE_PACKAGE_CACHE:-} bash -c "$STEP"
else
  IMG=${APPLIANCE_BUILDER_IMAGE:-ubuntu:26.04}
  log "running mkosi in $IMG"
  # Extra docker run arguments, e.g. a site's proxy: APPLIANCE_DOCKER_ARGS="-e http_proxy=..."
  # (mkosi passes http_proxy on to apt).
  # shellcheck disable=SC2086
  docker run --rm --privileged ${APPLIANCE_DOCKER_ARGS:-} \
    -v /dev:/dev -v "$HERE:/appliance" -v "$STAGE:/stage" -v "$OUT:/out" \
    -e APPLIANCE=/appliance -e STAGE=/stage -e OUT=/out -e WORKSPACE=/var/tmp -e VERSION="$VERSION" \
    -e ARCH="$ARCH" -e SDE="$SDE" -e TOOLS=1 "$IMG" bash -c "$STEP"
fi
(cd "$OUT/feed" && sha256sum -- * >SHA256SUMS)
{
  echo "version=$VERSION"
  echo "architecture=$ARCH"
  echo "snapshot=$SNAPSHOT"
  echo "sources_repo=$SOURCES_REPO"
  echo "seed=$SEED"
  echo "source_date_epoch=$SDE"
  echo "commit=$(git -C "$REPO" rev-parse HEAD 2>/dev/null || echo unknown)$(git -C "$REPO" diff --quiet 2>/dev/null || echo +dirty)"
  echo "builder=$([ $NATIVE = 1 ] && echo native || echo "docker ${IMG:-}")"
  cat "$OUT/BUILD-INFO.tools"
  echo "stage_sha256=$(cd "$STAGE" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | cut -d' ' -f1)"
} >"$OUT/BUILD-INFO"
rm -f "$OUT/BUILD-INFO.tools"
log "feed in $OUT/feed is unsigned: sign it with appliance/release.sh (the R3 gate)"
ls -la "$OUT"
