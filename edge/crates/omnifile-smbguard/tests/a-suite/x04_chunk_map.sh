#!/bin/bash
# X4 (M5): chunk maps under the smb-guard (fsal-socket.md §8; gate G1 step 3).
# Local (and SMB) reads of a mapped placeholder: a chunk whose bit is set is
# served with no recall; an unmapped chunk needs a fill; with the guard not
# answering, a read of an unmapped chunk blocks (never zeros); a map of the
# wrong length fails the whole file closed.
. "$(dirname "$0")/lib.sh"
begin "X4: chunk maps (M5)"
mkdir -p "$DATA/seis"
policy wait; delay 0
MiB=$((1 << 20))

# map_stub <name> <seed> <maplen-override or ->: a 16 MiB ONLINE placeholder
# made as the daemon makes one (O_TMPFILE, bytes, size, map, marker, then
# linkat): chunks 0 and 2 (4 MiB each) local, the map {0,2}, nothing
# resident.
map_stub() {
  rm -f "$DATA/seis/$1"
  python3 - "$DATA/seis" "$1" "$2" "$3" "$REPO/fsal/tests" <<'PY'
import os, struct, sys
sys.path.insert(0, sys.argv[5])
import omni_proto as P
d, name, seed, override = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
PAT = 0x4F4D4E4946494C45
def pat(off, n):
    return b"".join(struct.pack("<Q", (o ^ PAT ^ seed) & (2**64 - 1)) for o in range(off, off + n, 8))
fd = os.open(d, os.O_TMPFILE | os.O_WRONLY, 0o644)
for c in (0, 2):
    os.pwrite(fd, pat(c << 22, 4 << 20), c << 22)
os.ftruncate(fd, 16 << 20)
os.fsync(fd)
m = P.map_bits(16 << 20, 22, [0, 2])
if override != "-":
    m = (m + bytes(8))[:int(override)]
os.setxattr(fd, P.MAP_XATTR, m)
os.setxattr(fd, P.MARKER_XATTR, P.marker(seed, 1, 16 << 20, 0, flags=P.F_ONLINE | P.F_CHUNK_MAP,
                                         seq=2, share_id=1, chunk_shift=22))
os.link("/proc/self/fd/%d" % fd, os.path.join(d, name), follow_symlinks=True)
os.close(fd)
PY
  dropcache
}

section "X4a mapped chunks are local, unmapped ones are fetched"
map_stub x2a.segy 201 -
r0=$(recalls)
read_now 20 "$DATA/seis/x2a.segy" --off 0 --len 1M --seed 201
expect "read in chunk 0 (mapped)" "$VERDICT" OK
read_now 20 "$DATA/seis/x2a.segy" --off $((8 * MiB + 4096)) --len 1M --seed 201
expect "read in chunk 2 (mapped)" "$VERDICT" OK
expect "recalls for mapped chunks" "$(recalls)" "$r0"
read_now 30 "$DATA/seis/x2a.segy" --off $((4 * MiB)) --len 1M --seed 201
expect "read in chunk 1 (not mapped): filled, correct bytes" "$VERDICT" OK
expect_ge "a recall was asked for the unmapped chunk" "$(recalls)" $((r0 + 1))

section "X4b guard not answering: an unmapped chunk blocks, never zeros"
map_stub x2b.segy 202 -
gp=$(guard_pid) || fail "no guard"
kill -STOP "$gp"
start_reader x2b 4 check "$DATA/seis/x2b.segy" --off $((4 * MiB)) --len 1M --seed 202
finish_reader x2b "$RPID"
kill -CONT "$gp"
expect "unmapped chunk with the guard stopped" "$VERDICT" TIMEOUT
read_now 30 "$DATA/seis/x2b.segy" --off $((4 * MiB)) --len 1M --seed 202
expect "same read once the guard answers again" "$VERDICT" OK

section "X4c a map of the wrong length fails the file closed"
map_stub x2c.segy 203 2
read_now 20 "$DATA/seis/x2c.segy" --off 0 --len 1M --seed 203
expect "read of a 'mapped' chunk with a 2-byte map" "$VERDICT" ERROR
end_check
