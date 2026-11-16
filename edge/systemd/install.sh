#!/bin/bash
# Install the smb-guard's systemd pieces (run as root, or with --root for an
# image build). The daemon's own unit, omnifile-edge.service, is installed by
# its owner (edge/crates/omnifile-edge/systemd/); this adds:
#
#   <unitdir>/omnifile-edge.service.d/10-smbguard.conf   guard requirements on the daemon unit
#   <unitdir>/smbd.service.d/10-omnifile-guard.conf       smbd Requires/BindsTo/After the guard
#   <unitdir>/nfs-ganesha-omni.service.d/10-omnifile-guard.conf
#   <unitdir>/multi-user.target.wants/omnifile-edge.service  -> ../omnifile-edge.service
#   <bindir>/omnifile-guardctl                            the ops interlock
#   <unitdir>/omnifile-aws-signing-helper.service         Roles Anywhere credentials for S3 reads (M5)
#   /usr/libexec/omnifile/aws_signing_helper              AWS's helper binary, with --signing-helper
#
# The vendor .wants link is the systemd-level interlock: `systemctl disable`
# only removes links under /etc, so the unit stays referenced, and a stopped
# guard keeps its fd store (a stopped, unreferenced unit is garbage-collected
# with it: filer spike A6c-1). Removing the link is part of an uninstall,
# which goes through `omnifile-guardctl uninstall` (refused while smbd or
# Ganesha run).
#
# Usage: install.sh [--root DIR] [--unitdir DIR] [--bindir DIR] [--guardctl PATH]
#                   [--without-daemon-dropin] [--signing-helper FILE]
# --without-daemon-dropin: until M2 integration makes the daemon start the
# guard, Type=notify on the daemon unit would make its start time out.
# --signing-helper: AWS's aws_signing_helper, already checked against its pinned
# SHA-256 (appliance/build.sh fetches and checks it; LICENSES.md). The helper's unit
# runs it only from /usr/libexec/omnifile, never from a PATH an operator can change.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=
UNITDIR=/usr/lib/systemd/system
BINDIR=/usr/local/bin
GUARDCTL=
DAEMON_DROPIN=1
SIGNING_HELPER=
while [ $# -gt 0 ]; do
  case "$1" in
    --root) ROOT=$2; shift ;;
    --unitdir) UNITDIR=$2; shift ;;
    --bindir) BINDIR=$2; shift ;;
    --guardctl) GUARDCTL=$2; shift ;;
    --without-daemon-dropin) DAEMON_DROPIN=0 ;;
    --signing-helper) SIGNING_HELPER=$2; shift ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
  shift
done
U=$ROOT$UNITDIR
# Every directory first: uutils' install -D (Ubuntu 26.04's coreutils) has failed to make
# them, now and then, on a freshly emptied staging tree.
mkdir -p "$U/omnifile-edge.service.d" "$U/smbd.service.d" "$U/nfs-ganesha-omni.service.d"
if [ "$DAEMON_DROPIN" = 1 ]; then
  install -D -m 0644 "$HERE/omnifile-edge.service.d/10-smbguard.conf" "$U/omnifile-edge.service.d/10-smbguard.conf"
fi
install -D -m 0644 "$HERE/smbd.service.d/10-omnifile-guard.conf" "$U/smbd.service.d/10-omnifile-guard.conf"
install -D -m 0644 "$HERE/nfs-ganesha-omni.service.d/10-omnifile-guard.conf" \
  "$U/nfs-ganesha-omni.service.d/10-omnifile-guard.conf"
# M5: the IAM Roles Anywhere helper for S3 reads (starts only once its env
# file and sealed key exist).
install -D -m 0644 "$HERE/omnifile-aws-signing-helper.service" "$U/omnifile-aws-signing-helper.service"
mkdir -p "$U/multi-user.target.wants"
ln -sfn ../omnifile-edge.service "$U/multi-user.target.wants/omnifile-edge.service"
# Enabled like the daemon; its Condition*= checks hold it until provisioned.
ln -sfn ../omnifile-aws-signing-helper.service "$U/multi-user.target.wants/omnifile-aws-signing-helper.service"
if [ -n "$SIGNING_HELPER" ]; then
  mkdir -p "$ROOT/usr/libexec/omnifile"
  install -D -m 0755 "$SIGNING_HELPER" "$ROOT/usr/libexec/omnifile/aws_signing_helper"
fi
if [ -n "$GUARDCTL" ]; then
  install -D -m 0755 "$GUARDCTL" "$ROOT$BINDIR/omnifile-guardctl"
fi
if [ -z "$ROOT" ] && [ -d /run/systemd/system ]; then
  systemctl daemon-reload
fi
echo "installed smb-guard units under $U"
