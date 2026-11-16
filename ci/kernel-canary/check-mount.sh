#!/bin/bash
# check-mount.sh <mount path> <uuid file>
#
# Exit 0 only if <mount path> is the canary's xfs loop volume: a mount point, xfs, on a loop
# device, on a different device from /, with the filesystem UUID recorded at mkfs time.
# Run before arming any FAN_MARK_FILESYSTEM mark (plain guard) and as the guard unit's
# ExecStartPre: if the volume is not mounted, the mark would land on the root filesystem.
set -u
M=${1:?mount path}
UF=${2:?uuid file}

die() { echo "check-mount: $M: $*" >&2; exit 1; }

want=$(cat "$UF" 2>/dev/null) || die "no recorded UUID in $UF"
[ -n "$want" ] || die "empty recorded UUID"
mountpoint -q "$M" || die "not a mount point"
fstype=$(findmnt -n -o FSTYPE --mountpoint "$M")
src=$(findmnt -n -o SOURCE --mountpoint "$M")
[ "$fstype" = xfs ] || die "fstype is '$fstype', expected xfs"
case "$src" in /dev/loop*) ;; *) die "source is '$src', expected a loop device" ;; esac
[ "$(stat -c %d "$M")" != "$(stat -c %d /)" ] || die "same device as /"
got=$(blkid -c /dev/null -s UUID -o value "$src")
[ "$got" = "$want" ] || die "filesystem UUID is '$got', expected '$want'"
echo "check-mount: $M is xfs on $src, UUID $got"
