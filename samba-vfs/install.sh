#!/usr/bin/env bash
# Install omnifile_prevver.so where this host's smbd loads VFS modules from
# (`smbd -b` MODULESDIR/vfs). smbd loads modules by name only.
#
#   samba-vfs/install.sh [SO]            default: samba-vfs/build/omnifile_prevver.so
#   samba-vfs/install.sh --uninstall
#
# Shares pick it up only when their stanza names it: `omniacl render` does so
# when the file is there (ShareConfig::smb_prevver). Reload or restart smbd
# afterwards; already-connected sessions keep their old module set.
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
die() { printf '[samba-vfs] ERROR: %s\n' "$*" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || die "must run as root"
command -v smbd >/dev/null || die "no smbd on this host"
DIR=$(smbd -b | sed -nE 's/^ *MODULESDIR: *(.*)$/\1/p')
[ -n "$DIR" ] && [ -d "$DIR" ] || die "cannot find smbd's MODULESDIR (smbd -b)"
DEST=$DIR/vfs/omnifile_prevver.so
if [ "${1:-}" = --uninstall ]; then
  rm -f "$DEST"
  printf '[samba-vfs] removed %s\n' "$DEST"
  exit 0
fi
SO=${1:-$HERE/build/omnifile_prevver.so}
[ -f "$SO" ] || die "no $SO (run samba-vfs/build.sh)"
install -D -m 0644 -o root -g root "$SO" "$DEST"
printf '[samba-vfs] installed %s (for %s)\n' "$DEST" "$(smbd --version)"
