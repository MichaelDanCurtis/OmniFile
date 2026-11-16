#!/usr/bin/env bash
# Count the recalls an edge filer started in a time window (the Windows client matrix,
# docs/pilots/windows-client-matrix.md).
#
#   omnifile-recall-count.sh --since EPOCH [--until EPOCH] [--unit omnifile-edge]
#
# Reads the edge daemon's journal (JSON lines, one per event) for the window and counts its
# "recall started" events: every time the filer asked the service to recall a file, or joined a
# recall already under way. Prints one line of JSON:
#
#   {"since":E,"until":E,"requests":N,"recalls":M,"recall_ids":[...],"file_ids":[...]}
#
# requests counts the events; recalls the distinct recall ids among them; file_ids the files.
# EPOCH is seconds since 1970 (UTC). Runs as a user who may read the system journal (a member of
# systemd-journal or adm, or root); anyone else is refused unless the unit's entries are visible, since
# a journal they can't read would count zero. Reads only; changes nothing. Exit 2 on bad arguments, 1
# if the journal can't be read.
set -euo pipefail

since='' until='' unit=omnifile-edge
while [ $# -gt 0 ]; do
  case "$1" in
    --since) since=$2; shift ;;
    --until) until=$2; shift ;;
    --unit) unit=$2; shift ;;
    *) echo "usage: omnifile-recall-count.sh --since EPOCH [--until EPOCH] [--unit omnifile-edge]" >&2; exit 2 ;;
  esac
  shift
done
[[ $since =~ ^[0-9]+$ ]] || { echo "omnifile-recall-count.sh: --since takes seconds since 1970" >&2; exit 2; }
[ -z "$until" ] || [[ $until =~ ^[0-9]+$ ]] || { echo "omnifile-recall-count.sh: --until takes seconds since 1970" >&2; exit 2; }
[[ $unit =~ ^[A-Za-z0-9@._-]+$ ]] || { echo "omnifile-recall-count.sh: bad unit name" >&2; exit 2; }

# A user who may not read the system journal sees no entries, and would count zero recalls whatever
# happened. So count only as root, as a member of systemd-journal or adm, or when the unit's entries
# are visible at all.
may_read=0
[ "$(id -u)" = 0 ] && may_read=1
for g in $(id -nG); do
  case $g in systemd-journal | adm) may_read=1 ;; esac
done
if [ "$may_read" = 0 ]; then
  probe=$(journalctl --unit "$unit" --lines 1 --output cat --no-pager --quiet 2>/dev/null || true)
  if [ -z "$probe" ]; then
    echo "omnifile-recall-count.sh: $(id -un) sees no entries of $unit at all, so a count would be wrong:" >&2
    echo "  run it as a member of systemd-journal (sudo gpasswd -a $(id -un) systemd-journal, then log in again)" >&2
    exit 1
  fi
fi

args=(--unit "$unit" --since "@$since" --output cat --no-pager --quiet)
[ -n "$until" ] && args+=(--until "@$until")
if ! lines=$(journalctl "${args[@]}" 2>&1); then
  echo "omnifile-recall-count.sh: can't read the journal of $unit: $lines" >&2
  exit 1
fi

requests=0 recall_list='' file_list=''
while IFS= read -r line; do
  [[ $line == *'"message":"recall started"'* ]] || continue
  requests=$((requests + 1))
  if [[ $line =~ \"recall_id\":([0-9]+) ]]; then recall_list+="${BASH_REMATCH[1]}"$'\n'; fi
  if [[ $line =~ \"file_id\":([0-9]+) ]]; then file_list+="${BASH_REMATCH[1]}"$'\n'; fi
done <<<"$lines"

# Distinct ids, in order, comma-separated.
distinct() { printf '%s' "$1" | grep . | sort -nu | paste -sd, - || true; }
ids=$(distinct "$recall_list")
fids=$(distinct "$file_list")
n=0
[ -z "$ids" ] || n=$(tr ',' '\n' <<<"$ids" | wc -l | tr -d ' ')
printf '{"since":%s,"until":%s,"requests":%d,"recalls":%d,"recall_ids":[%s],"file_ids":[%s]}\n' \
  "$since" "${until:-null}" "$requests" "$n" "$ids" "$fids"
