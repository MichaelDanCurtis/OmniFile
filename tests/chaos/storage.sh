#!/usr/bin/env bash
# Moves the dev edge VM's write-heavy state off its own disk for long runs, as root:
#
#   sudo tests/chaos/storage.sh on [BIGDIR]              # before the soak, A8, the line-rate test
#   sudo tests/chaos/storage.sh off [BIGDIR] [--delete]  # back to the VM's own disk
#   sudo tests/chaos/storage.sh status [BIGDIR]
#   STORAGE_PLAN=1 tests/chaos/storage.sh ...            # print what it would act on, and stop
# `off` without BIGDIR acts on the directory the data volume's loop file is in now.
#
# Why: the VM's disk is a file on the Mac's internal disk, and every block written
# there, even one freed or overwritten later, stays allocated on the Mac while Time
# Machine snapshots hold it. So:
# - in RAM (tmpfs; at most 13 GiB, all sparse): Postgres's data directory (4 GiB), the
#   journal (Storage=volatile, 2 GiB), /var/lib/omnifile (the daemon's replica and outbox,
#   1 GiB) and /var/tmp (the e2e tests' source files, 6 GiB);
# - on BIGDIR (default /work/dev/.artifacts/wp3: the repo mount, virtiofs, on the Mac's
#   external drive; git-ignored): new sparse loop files for the data and cache volumes,
#   xfs with the SAME UUIDs and labels, so edge.toml's smb-guard fs_uuid still matches.
#   The xfs sits on the loop device, so fanotify, trusted.* xattrs and hole punching
#   behave the same; throughput reflects a loop file on virtiofs.
# Every e2e test resets the catalog, the sim and the edge's state, so nothing is copied
# into the new places except Postgres's cluster and /var/lib/omnifile's small files.
# `off` stops everything, unmounts, and mounts the original volumes from /etc/fstab again.
set -uo pipefail
ACTION=${1:-status}
[ $# -gt 0 ] && shift
BIG=""
DELETE=0
for a in "$@"; do
  case "$a" in
    --delete) DELETE=1 ;;
    -*) echo "unknown option $a" >&2; exit 2 ;;
    *) BIG=$a ;;
  esac
done
if [ "${STORAGE_PLAN:-0}" = 1 ]; then
  echo "action=$ACTION bigdir=${BIG:-<default>} delete=$DELETE"
  exit 0
fi
[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 2; }
PGDATA=/var/lib/postgresql/17/main
PGRAM=/run/omnifile-wp3-pg
JDROP=/run/systemd/journald.conf.d/90-omnifile-wp3.conf
SERVICES=(smbd nfs-ganesha-omni omnifile-edge omnifile-dev-worker omnifile-dev-api omnifile-dev-enrol
  omnifile-dev-sim omnifile-dev-sim-worm omnifile-dev-fakes omnifile-dev-relay omnifile-dev-web)
log() { printf '[storage %s] %s\n' "$(date +%T)" "$*"; }

stop_all() {
  systemctl stop "${SERVICES[@]}" 2>/dev/null
  systemctl clean --what=fdstore omnifile-edge 2>/dev/null
  systemctl stop postgresql@17-main postgresql 2>/dev/null
}
start_base() {
  systemctl start postgresql@17-main || return 1
  systemctl start omnifile-dev-sim omnifile-dev-sim-worm omnifile-dev-enrol
  systemctl start omnifile-dev-api omnifile-dev-worker
}
uuid_of() { blkid -c /dev/null -s UUID -o value "$(findmnt -n -o SOURCE --mountpoint "$1")"; }
label_of() { blkid -c /dev/null -s LABEL -o value "$(findmnt -n -o SOURCE --mountpoint "$1")"; }
backing_of() { losetup -n -O BACK-FILE "$(findmnt -n -o SOURCE --mountpoint "$1")" 2>/dev/null; }

swap_volume() { # mountpoint size image
  local mp=$1 size=$2 img=$3 uuid label
  [ "$(backing_of "$mp")" = "$img" ] && { log "$mp already on $img"; return 0; }
  uuid=$(uuid_of "$mp") label=$(label_of "$mp")
  [ -n "$uuid" ] || { log "no filesystem at $mp"; return 1; }
  if [ ! -f "$img" ]; then
    truncate -s "$size" "$img" && mkfs.xfs -q -f -L "$label" -m uuid="$uuid" "$img" || return 1
  fi
  umount "$mp" || return 1
  mount -o loop,noatime "$img" "$mp" || { mount "$mp"; return 1; }
  log "$mp: xfs $uuid ($label) on $img"
}
ram_dir() { # path size: a tmpfs over path, keeping the files that are there (small)
  local p=$1 size=$2 keep
  mountpoint -q "$p" && [ "$(findmnt -n -o FSTYPE --mountpoint "$p")" = tmpfs ] && return 0
  keep=$(mktemp -d /run/omnifile-wp3-keep.XXXXXX)
  # Small files only: the volumes' loop files stay where they are (their loop devices
  # keep them open) and are not needed while the volumes live on BIGDIR.
  find "$p" -mindepth 1 -maxdepth 1 ! -name '*.img' -exec cp -a {} "$keep/" \;
  local mode owner
  mode=$(stat -c %a "$p") owner=$(stat -c %u:%g "$p")
  mount -t tmpfs -o "size=$size,mode=$mode" tmpfs "$p" || return 1
  cp -a "$keep/." "$p/" && rm -rf "$keep"
  # cp -a of "dir/." also copies the temporary directory's own mode (0700): set them last.
  chown "$owner" "$p" && chmod "$mode" "$p"
}

case "$ACTION" in
  on)
    BIG=${BIG:-/work/dev/.artifacts/wp3}
    install -d -m 0755 "$BIG" || exit 1
    log "stopping the stack"
    stop_all
    install -d "$(dirname "$JDROP")"
    printf '[Journal]\nStorage=volatile\nRuntimeMaxUse=2G\nRuntimeMaxFileSize=128M\n' >"$JDROP"
    systemctl restart systemd-journald
    ram_dir /var/tmp 6G || exit 1
    ram_dir /var/lib/omnifile 1G || exit 1
    swap_volume /srv/omnifile/data 64G "$BIG/data.img" || exit 1
    swap_volume /srv/omnifile/cache 16G "$BIG/cache.img" || exit 1
    if ! mountpoint -q "$PGDATA"; then
      install -d -m 0700 "$PGRAM"
      mountpoint -q "$PGRAM" || mount -t tmpfs -o size=4G,mode=0700 tmpfs "$PGRAM"
      cp -a "$PGDATA/." "$PGRAM/" && chown postgres:postgres "$PGRAM" && chmod 0700 "$PGRAM"
      mount --bind "$PGRAM" "$PGDATA" || exit 1
    fi
    start_base || exit 1
    log "on: Postgres, journal, /var/lib/omnifile, /var/tmp in RAM; data and cache volumes on $BIG"
    ;;
  off)
    if [ -z "$BIG" ]; then # wherever `on` put the volumes
      cur=$(backing_of /srv/omnifile/data)
      case "$cur" in /var/lib/omnifile/* | "") BIG=/work/dev/.artifacts/wp3 ;; *) BIG=$(dirname "$cur") ;; esac
    fi
    log "stopping the stack"
    stop_all
    mountpoint -q "$PGDATA" && umount "$PGDATA"
    mountpoint -q "$PGRAM" && umount "$PGRAM"
    moved=()
    for mp in /srv/omnifile/data /srv/omnifile/cache; do
      case "$(backing_of "$mp")" in "$BIG"/*) umount "$mp" && moved+=("$mp") ;; esac
    done
    # The tmpfs over /var/lib/omnifile hides the original volumes' loop files: first.
    for p in /var/lib/omnifile /var/tmp; do
      [ "$(findmnt -n -o FSTYPE --mountpoint "$p" 2>/dev/null)" = tmpfs ] && umount -l "$p"
    done
    for mp in "${moved[@]}"; do mount "$mp" && log "$mp back on $(backing_of "$mp")"; done
    rm -f "$JDROP"
    systemctl restart systemd-journald
    systemctl start postgresql@17-main
    if [ "$DELETE" = 1 ]; then rm -f "$BIG/data.img" "$BIG/cache.img"; log "deleted $BIG/{data,cache}.img"; fi
    log "off: the VM's own disk again (the catalog and edge state are as they were before 'on')"
    ;;
  status)
    BIG=${BIG:-/work/dev/.artifacts/wp3}
    for mp in /srv/omnifile/data /srv/omnifile/cache; do echo "$mp: $(backing_of "$mp") ($(uuid_of "$mp"))"; done
    for p in "$PGDATA" /var/lib/omnifile /var/tmp /run; do echo "$p: $(findmnt -n -o FSTYPE,SIZE,USED --mountpoint "$p" 2>/dev/null || echo 'on the VM disk')"; done
    journalctl --header 2>/dev/null | grep -m1 'File path' || true
    du -sh --apparent-size "$BIG"/*.img 2>/dev/null; du -sh "$BIG"/*.img 2>/dev/null
    ;;
  *) echo "usage: storage.sh on [BIGDIR] | off [--delete] | status" >&2; exit 2 ;;
esac
