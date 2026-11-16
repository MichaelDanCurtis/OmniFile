#!/bin/bash
# xfs loop volumes for part B (idempotent). data: stubs + OMNI exports; plain: VFS baseline.
set -e
B=/srv/omni-b
mkdir -p $B/data $B/plain $B/backend $B/mnt /run/omnifile
[ -f $B/data.img ] || { truncate -s 20G $B/data.img; mkfs.xfs -q -f $B/data.img; }
[ -f $B/plain.img ] || { truncate -s 12G $B/plain.img; mkfs.xfs -q -f $B/plain.img; }
mountpoint -q $B/data || mount -o loop $B/data.img $B/data
mountpoint -q $B/plain || mount -o loop $B/plain.img $B/plain
mkdir -p $B/data/wait $B/data/failfast $B/data/quick $B/data/null
