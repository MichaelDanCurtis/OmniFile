#!/usr/bin/env bash
# Install the omnifile-edge daemon and its unit on the edge (dev VM or the
# e2e CI runner). Runs as root.
#
#   install-edge.sh [BINARY]
#
# Without BINARY it builds one first, as the invoking (sudo) user, with
# dev/scripts/build-edge.sh. Installs:
#   /usr/local/bin/omnifile-edge
#   /etc/systemd/system/omnifile-edge.service   (edge/crates/omnifile-edge/systemd/)
#   /etc/omnifile/edge.toml                      (dev/edge/edge.toml, with the volume's UUID)
#   edge/systemd/ drop-ins (10-smbguard.conf, smbd + Ganesha bound to the guard),
#   omnifile-guardctl, smbguard-tool, omniacl    (/usr/local/bin)
# Does not enrol or start the daemon (tests/e2e does both).
set -euo pipefail

REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
BIN="${1:-}"
log() { printf '[install-edge] %s\n' "$*"; }
die() { printf '[install-edge] ERROR: %s\n' "$*" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || die "must run as root"

if [ -z "$BIN" ]; then
  user="${SUDO_USER:-}"
  [ -n "$user" ] && [ "$user" != root ] || die "give a BINARY, or run through sudo so the build runs as the user"
  log "building omnifile-edge as $user"
  BIN=$(sudo -u "$user" -H bash -lc "REPO='$REPO' '$REPO/dev/scripts/build-edge.sh'" | tail -n 1)
fi
[ -x "$BIN" ] || die "no binary at $BIN"

systemctl stop nfs-ganesha-omni.service omnifile-edge.service 2>/dev/null || true
install -m 0755 "$BIN" /usr/local/bin/omnifile-edge.new
mv /usr/local/bin/omnifile-edge.new /usr/local/bin/omnifile-edge
install -m 0644 "$REPO/edge/crates/omnifile-edge/systemd/omnifile-edge.service" \
  /etc/systemd/system/omnifile-edge.service
install -d -m 0755 /etc/omnifile
install -d -m 0700 /var/lib/omnifile/identity /var/lib/omnifile/outbox
# The config, with the data volume's UUID for the smb-guard.
DATA=/srv/omnifile/data
mountpoint -q "$DATA" || die "$DATA is not mounted (the guard marks only the data volume)"
uuid=$(blkid -c /dev/null -s UUID -o value "$(findmnt -n -o SOURCE --mountpoint "$DATA")")
[ -n "$uuid" ] || die "no filesystem UUID for $DATA"
sed -e "s/@FS_UUID@/$uuid/" "$REPO/dev/edge/edge.toml" >/etc/omnifile/edge.toml.new
if [ -f /etc/omnifile/edge.toml ] && ! cmp -s /etc/omnifile/edge.toml /etc/omnifile/edge.toml.new; then
  cp /etc/omnifile/edge.toml /etc/omnifile/edge.toml.bak
fi
install -m 0644 /etc/omnifile/edge.toml.new /etc/omnifile/edge.toml
rm -f /etc/omnifile/edge.toml.new
# The smb-guard's drop-ins (Type=notify, fd store, private fill mount; smbd and
# Ganesha bound to the guard) and the ops interlock. The daemon starts the
# guard first thing in main (M2), so the daemon drop-in goes in too.
bindir=$(dirname "$BIN")
guardctl=
[ -x "$bindir/omnifile-guardctl" ] && guardctl=$bindir/omnifile-guardctl
bash "$REPO/edge/systemd/install.sh" ${guardctl:+--guardctl "$guardctl"}
for t in smbguard-tool examples/omniacl; do
  [ -x "$bindir/$t" ] && install -m 0755 "$bindir/$t" "/usr/local/bin/$(basename "$t")"
done
install -d -m 0755 /run/omnifile-fill
# Dev/e2e only: the sim Blob facade is plain http on 127.0.0.1:10000, which the
# Blob client refuses unless told (never on an appliance).
install -d -m 0755 /etc/systemd/system/omnifile-edge.service.d
cat >/etc/systemd/system/omnifile-edge.service.d/50-dev-sim.conf <<'EOF2'
# Written by dev/scripts/install-edge.sh: dev VM and e2e runner only.
[Service]
Environment=OMNIFILE_BLOB_ALLOW_HTTP=1
EOF2
systemctl daemon-reload
log "installed $(/usr/local/bin/omnifile-edge --version) (smb-guard on $DATA, xfs UUID $uuid)"
