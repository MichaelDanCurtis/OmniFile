#!/bin/bash
# A10 follow-up: copy_file_range on the same fs, and reflink clone after the (0,0) fix.
. "$(dirname "$0")/lib.sh"
release_unit; stop_plain; : > $LOGS/guard-plain.log
policy wait; delay 0; mkstubs; dropcache
start_plain -v
ev() { grep "event fd=" $LOGS/guard-plain.log | tail -${1:-1} | sed 's/.*mask=[^ ]* //'; }
say "copy_file_range, destination on the same xfs"
OMNITOOL_TMPDIR=$DATA $T check $DATA/seis/line_2g.segy --off 524288000 --len 1M --method cfr --seed 2 --bs 1M
ev 2
say "cp --reflink=always (FICLONE whole file), policy wait"
rm -f $DATA/seis/clone.segy
cp --reflink=always $DATA/seis/small.segy $DATA/seis/clone.segy; echo "cp rc=$?"
ev 3
$T check $DATA/seis/clone.segy --off 10M --len 1M --seed 1
say "cp --reflink=always, policy failfast"
mkstubs; dropcache; policy failfast; rm -f $DATA/seis/clone.segy
cp --reflink=always $DATA/seis/small.segy $DATA/seis/clone.segy; echo "cp rc=$?"
say "O_TRUNC overwrite of a stub (cost of the ambiguity), policy failfast"
: > $DATA/seis/small.segy; echo "truncate-by-shell rc=$?"
policy wait; rm -f $DATA/seis/clone.segy
stop_plain
