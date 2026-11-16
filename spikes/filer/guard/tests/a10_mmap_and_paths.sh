#!/bin/bash
# A10: mmap of stubs, plus the other kernel read paths (O_DIRECT, sendfile, splice,
# copy_file_range, reflink clone, mremap growth).
. "$(dirname "$0")/lib.sh"
release_unit; stop_plain; : > $LOGS/guard-plain.log
policy wait; delay 1000; mkstubs; dropcache
start_plain -v
ev() { grep "event fd=" $LOGS/guard-plain.log | tail -${1:-1} | sed 's/.*mask=[^ ]* //'; }

say "A10a: mmap 16 MiB at 100 MiB of a stub, policy wait (fill must happen inside mmap())"
$T check $DATA/seis/line_2g.segy --off 100M --len 16M --method mmap --seed 2
ev

say "A10b: mmap of the WHOLE 2 GiB stub (one event for the whole mapping)"
delay 0
$T check $DATA/seis/line_2g.segy --method mmap --seed 2
ev

say "A10c: mmap with policy failfast"
mkstubs; dropcache; policy failfast; delay 0
$T check $DATA/seis/line_2g.segy --off 200M --len 1M --method mmap --seed 2
ev

say "A10d: mremap growth: map the resident first 1 MiB, grow the mapping to 1 GiB, touch it"
policy wait
$T mremap $DATA/seis/line_2g.segy 1M 1G --seed 2
ev 2

say "A10e: other read paths on non-resident ranges (policy wait)"
mkstubs; dropcache
off=300
for m in pread odirect sendfile splice cfr; do
  $T check $DATA/seis/line_2g.segy --off ${off}M --len 1M --method $m --seed 2 --bs 256K
  off=$((off+50))
done
say "A10f: reflink clone of a stub (cp --reflink=always) then read the clone"
mkstubs; dropcache
rm -f $DATA/seis/clone.segy
cp --reflink=always $DATA/seis/small.segy $DATA/seis/clone.segy; echo "cp rc=$?"
ev
$T check $DATA/seis/clone.segy --off 10M --len 1M --seed 1
getfattr -d -m user.omnifile $DATA/seis/clone.segy 2>/dev/null | grep -v '^#' || true
rm -f $DATA/seis/clone.segy
say "A10g: reflink clone with policy failfast"
mkstubs; dropcache; policy failfast
cp --reflink=always $DATA/seis/small.segy $DATA/seis/clone.segy; echo "cp rc=$?"
rm -f $DATA/seis/clone.segy; policy wait

say "A10h: 200 GB synthetic stub (sparse; backend object generated on the fly)"
rm -f $DATA/seis/line_200g.segy
$T mkstub $DATA/seis/line_200g.segy 200G 4M x --synthetic --seed 7
ls -ls $DATA/seis/line_200g.segy; df -h $DATA | tail -1
$T check $DATA/seis/line_200g.segy --off 0 --len 3600 --seed 7
$T check $DATA/seis/line_200g.segy --off 150G --len 4M --seed 7
ev
echo "mmap of the whole 200 GB stub under failfast (would otherwise require a 200 GB fill):"
policy failfast
$T check $DATA/seis/line_200g.segy --off 0 --len 200G --method mmap --seed 7
ev
policy wait
stop_plain
