#!/usr/bin/env python3
"""Bind an update feed to the disk image the R3 gate boots (appliance/release.sh).

    feed_binding.py check DISK.raw FEED_DIR VERSION

The gate runs the suites on one raw disk, then signs one feed. Both are omnifile-edge_V,
but a feed from another build of the same version would carry another kernel and /usr
(Codex on #28). So before the boot this checks, from the disk's own GPT, that the feed is
that disk's:

- the /usr partition (named omnifile-edge_V, type usr-<arch>) has the partition UUID the
  feed's omnifile-edge_V.usr-<arch>.<uuid>.raw.zst names, and the same bytes (SHA-256 of
  the partition against SHA-256 of the decompressed file);
- the same for the verity partition (omnifile-edge_V_verity, usr-<arch>-verity);
- <arch> is x86-64 (the product) or arm64 (the test build, build.sh --arch arm64), and the
  feed holds one architecture, the disk's;
- the feed has exactly one UKI, omnifile-edge_V.efi, and nothing else of that version.

It prints the binding as `key=value` lines (the UKI's SHA-256 too, which release.sh then
compares with the SHA-256 of the UKI the guest booted), and exits 1 naming the first
mismatch. Python's standard library and the zstd command; nothing is written.
"""

import hashlib
import os
import re
import struct
import subprocess
import sys
import uuid

USR_X86_64 = uuid.UUID("8484680c-9521-48c6-9c11-b0720656f69e")
USR_VERITY_X86_64 = uuid.UUID("77ff5f63-e7b6-4633-acf4-1565b864c0e6")
USR_ARM64 = uuid.UUID("b0e01050-ee5f-4390-949a-9101b17104e9")
USR_VERITY_ARM64 = uuid.UUID("6e11a4e7-fbca-4ded-b9e9-e1a512bb664e")
# The partition types of /usr and its verity per architecture (systemd's Discoverable
# Partitions Specification; `systemd-id128 show usr-arm64`).
ARCHES = {"x86-64": (USR_X86_64, USR_VERITY_X86_64), "arm64": (USR_ARM64, USR_VERITY_ARM64)}
CHUNK = 4 << 20


class Mismatch(Exception):
    pass


def gpt_partitions(path):
    """[(name, type_uuid, part_uuid, offset, size)] from a raw disk's primary GPT."""
    with open(path, "rb") as f:
        f.seek(512)
        hdr = f.read(92)
        if hdr[:8] != b"EFI PART":
            raise Mismatch(f"{path}: no GPT header")
        entries_lba, count, size = struct.unpack_from("<QII", hdr, 72)
        f.seek(entries_lba * 512)
        table = f.read(count * size)
    parts = []
    for i in range(count):
        e = table[i * size:(i + 1) * size]
        ptype = uuid.UUID(bytes_le=e[0:16])
        if ptype.int == 0:
            continue
        puuid = uuid.UUID(bytes_le=e[16:32])
        first, last = struct.unpack_from("<QQ", e, 32)
        name = e[56:128].decode("utf-16-le").rstrip("\x00")
        parts.append((name, ptype, puuid, first * 512, (last - first + 1) * 512))
    return parts


def sha256_range(path, offset, size):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        f.seek(offset)
        left = size
        while left:
            b = f.read(min(CHUNK, left))
            if not b:
                raise Mismatch(f"{path}: short read at {offset + size - left}")
            h.update(b)
            left -= len(b)
    return h.hexdigest()


def sha256_zstd(path):
    """SHA-256 and length of a .zst file's decompressed bytes."""
    h, n = hashlib.sha256(), 0
    with subprocess.Popen(["zstd", "-dc", "--", path], stdout=subprocess.PIPE) as p:
        while True:
            b = p.stdout.read(CHUNK)
            if not b:
                break
            h.update(b)
            n += len(b)
    if p.returncode != 0:
        raise Mismatch(f"{path}: zstd failed ({p.returncode})")
    return h.hexdigest(), n


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(CHUNK), b""):
            h.update(b)
    return h.hexdigest()


def check(disk, feed, version):
    parts = gpt_partitions(disk)
    files = sorted(os.listdir(feed))
    mine = [f for f in files if f.startswith(f"omnifile-edge_{version}.")]
    out = {"version": version}
    ukis = [f for f in mine if f == f"omnifile-edge_{version}.efi"]
    if len(ukis) != 1:
        raise Mismatch(f"the feed has no omnifile-edge_{version}.efi")
    out["uki_sha256"] = sha256_file(os.path.join(feed, ukis[0]))
    arches = set()
    for f in mine:
        m = re.search(r"\.usr-(x86-64|arm64)\.[0-9a-f]{32}\.raw\.zst$", f)
        if m:
            arches.add(m.group(1))
    if len(arches) != 1:
        raise Mismatch(f"the feed has /usr files for {len(arches)} architectures, not one")
    arch = arches.pop()
    out["architecture"] = arch
    usr_type, verity_type = ARCHES[arch]
    wanted = {
        "usr": (usr_type, f"omnifile-edge_{version}", r"\.usr-" + arch + r"\.([0-9a-f]{32})\.raw\.zst$"),
        "verity": (
            verity_type,
            f"omnifile-edge_{version}_verity",
            r"\.usr-" + arch + r"-verity\.([0-9a-f]{32})\.raw\.zst$",
        ),
    }
    seen = set(ukis)
    for key, (ptype, pname, pattern) in wanted.items():
        cands = [f for f in mine if re.search(pattern, f)]
        if len(cands) != 1:
            raise Mismatch(f"the feed has {len(cands)} {key} files for {version}, not one")
        seen.add(cands[0])
        fuuid = uuid.UUID(re.search(pattern, cands[0]).group(1))
        ondisk = [p for p in parts if p[1] == ptype and p[0] == pname]
        if len(ondisk) != 1:
            raise Mismatch(f"{disk} has no {arch} {key} partition named {pname}")
        _, _, puuid, off, size = ondisk[0]
        if puuid != fuuid:
            raise Mismatch(
                f"the feed's {key} is partition {fuuid}, the booted disk's is {puuid}: "
                "this feed is from another build"
            )
        fsha, flen = sha256_zstd(os.path.join(feed, cands[0]))
        if flen != size:
            raise Mismatch(f"the feed's {key} is {flen} bytes, the disk's partition {size}")
        dsha = sha256_range(disk, off, size)
        if dsha != fsha:
            raise Mismatch(f"the feed's {key} bytes differ from the booted disk's partition")
        out[f"{key}_partuuid"] = str(puuid)
        out[f"{key}_sha256"] = dsha
    extra = [f for f in mine if f not in seen]
    if extra:
        raise Mismatch(f"the feed has other files of {version}: {', '.join(extra)}")
    return out


def main(argv):
    if len(argv) != 5 or argv[1] != "check":
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    try:
        for k, v in check(argv[2], argv[3], argv[4]).items():
            print(f"{k}={v}")
    except (Mismatch, OSError) as e:
        print(f"feed binding: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
