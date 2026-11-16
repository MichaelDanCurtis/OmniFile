#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "zstandard>=0.22",
#   "lz4>=4.3",
#   "blosc2>=2.5",
#   "numpy>=1.26",
# ]
# ///
"""segy_profile.py -- pilot 3 (data test) profiling tool for OmniFile Gateway.

Walks one or more directories, classifies files as SEG-Y or not, measures the
whole-file-dedupe opportunity (byte-identical files), and benchmarks lossless
compression codecs against a random (seeded) sample of the SEG-Y trace data.

Run with uv, which reads the PEP 723 block above and builds an isolated venv:

    uv run segy_profile.py /mnt/dataset1 /mnt/dataset2
    uv run segy_profile.py --self-test
    uv run segy_profile.py --dry-run /mnt/dataset1

Optional codecs (zfpy, segyio) are NOT in the hard dependency list above on
purpose: zfpy ships compiled wheels for a narrower set of platforms than the
rest of this list, and a client-site box that lacks a wheel for it would
otherwise make `uv run` fail before the tool does anything useful. Both are
imported with a plain try/except and the codecs/checks that need them are
skipped cleanly (and reported as skipped) when they are not importable. To
get them, either:

    uv add --script segy_profile.py zfpy segyio   # persists into this file
    uv run --with zfpy --with segyio segy_profile.py ...   # one-off

See README.md for exact prerequisites and what to send back.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import random
import shutil
import struct
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from typing import Iterable, Iterator, Optional

import lz4.frame as lz4frame
import numpy as np
import zstandard

try:
    import blosc2

    HAVE_BLOSC2 = True
except ImportError:  # pragma: no cover - exercised only on broken installs
    HAVE_BLOSC2 = False

try:
    import zfpy

    HAVE_ZFP = True
except ImportError:
    HAVE_ZFP = False

try:
    import segyio  # noqa: F401  -- only used for an optional cross-check

    HAVE_SEGYIO = True
except ImportError:
    HAVE_SEGYIO = False


# --------------------------------------------------------------------------
# SEG-Y structural constants (SEG-Y rev 1, all fields big-endian per spec)
# --------------------------------------------------------------------------

TEXT_HEADER_SIZE = 3200
BINARY_HEADER_SIZE = 400
FILE_HEADER_SIZE = TEXT_HEADER_SIZE + BINARY_HEADER_SIZE  # 3600
TRACE_HEADER_SIZE = 240

# Offsets are given in the spec as 1-indexed byte numbers within the whole
# file; converting to 0-indexed offsets *within the 400-byte binary header*:
#   samples per trace: bytes 3221-3222 (file) -> 20-22 (binary header)
#   sample format code: bytes 3225-3226 (file) -> 24-26 (binary header)
SAMPLES_PER_TRACE_OFFSET = 3221 - 1 - TEXT_HEADER_SIZE
SAMPLE_FORMAT_CODE_OFFSET = 3225 - 1 - TEXT_HEADER_SIZE

SEGY_EXTENSIONS = {".segy", ".sgy"}

# SEG-Y rev0/rev1/rev2 sample format codes -> bytes per sample.
FORMAT_CODE_SAMPLE_SIZE = {
    1: 4,   # IBM float
    2: 4,   # int32
    3: 2,   # int16
    4: 4,   # fixed point with gain (obsolete)
    5: 4,   # IEEE float32
    6: 8,   # IEEE float64 (rev2)
    7: 3,   # int24 (rev2)
    8: 1,   # int8
    9: 8,   # int64 (rev2)
    10: 4,  # uint32 (rev2)
    11: 2,  # uint16 (rev2)
    12: 8,  # uint64 (rev2)
    15: 3,  # uint24 (rev2)
    16: 1,  # uint8 (rev2)
}
IEEE_FLOAT32_FORMAT_CODE = 5
IBM_FLOAT32_FORMAT_CODE = 1

MAX_SANE_SAMPLES_PER_TRACE = 1_000_000

CODEC_NAMES = ["zstd-1", "zstd-3", "lz4", "blosc2-shuffle-zstd", "zfp-reversible"]


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def eprint(*args, **kwargs) -> None:
    print(*args, file=sys.stderr, **kwargs)


def path_identifier(path: Path, include_paths: bool) -> str:
    """Identifier used in JSON output. Hashed by default; clients may
    consider raw file paths sensitive, so we never emit them unless asked."""
    if include_paths:
        return str(path)
    return hashlib.sha256(str(path).encode("utf-8", "surrogateescape")).hexdigest()[:16]


def iter_files(directories: Iterable[str]) -> Iterator[Path]:
    """Walk directories, never following symlinks (dirs or files)."""
    for root_dir in directories:
        root = Path(root_dir)
        if not root.exists():
            eprint(f"warning: directory does not exist, skipping: {root}")
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            # Don't descend into symlinked directories. os.walk(followlinks=False)
            # already refuses to recurse into them, but keep this explicit and
            # also drop them from dirnames so nothing downstream touches them.
            dirnames[:] = [
                d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))
            ]
            for name in filenames:
                full = Path(dirpath) / name
                try:
                    if full.is_symlink():
                        continue
                    if not full.is_file():
                        continue
                except OSError as exc:
                    eprint(f"warning: could not stat {full}: {exc}")
                    continue
                yield full


# --------------------------------------------------------------------------
# SEG-Y classification
# --------------------------------------------------------------------------


@dataclasses.dataclass
class SegyInfo:
    sample_format_code: Optional[int]
    samples_per_trace: Optional[int]
    sample_size: Optional[int]
    structurally_valid: bool
    segyio_validated: Optional[bool]  # None if segyio not installed


def probe_binary_header(path: Path, size: int) -> Optional[SegyInfo]:
    """Read the 400-byte binary header (if the file is big enough) and pull
    out sample format code + samples per trace. Returns None if the file is
    too small to have a binary header at all."""
    if size < FILE_HEADER_SIZE:
        return None
    try:
        with open(path, "rb") as f:
            f.seek(TEXT_HEADER_SIZE)
            binhdr = f.read(BINARY_HEADER_SIZE)
    except OSError as exc:
        eprint(f"warning: could not read header of {path}: {exc}")
        return None
    if len(binhdr) < BINARY_HEADER_SIZE:
        return None

    (samples_per_trace,) = struct.unpack_from(
        ">H", binhdr, SAMPLES_PER_TRACE_OFFSET
    )
    (sample_format_code,) = struct.unpack_from(
        ">H", binhdr, SAMPLE_FORMAT_CODE_OFFSET
    )

    valid = (
        sample_format_code in FORMAT_CODE_SAMPLE_SIZE
        and 0 < samples_per_trace <= MAX_SANE_SAMPLES_PER_TRACE
    )
    sample_size = FORMAT_CODE_SAMPLE_SIZE.get(sample_format_code)

    return SegyInfo(
        sample_format_code=sample_format_code if valid else None,
        samples_per_trace=samples_per_trace if valid else None,
        sample_size=sample_size if valid else None,
        structurally_valid=valid,
        segyio_validated=None,
    )


def segyio_cross_check(path: Path) -> Optional[bool]:
    """Best-effort structural confirmation using segyio, when installed.
    Many real-world SEG-Y files trip segyio's stricter trace-geometry
    assumptions, so a False here is informational, not disqualifying."""
    if not HAVE_SEGYIO:
        return None
    try:
        with segyio.open(str(path), "r", ignore_geometry=True):
            return True
    except Exception:
        return False


def classify_file(path: Path, size: int) -> tuple[bool, Optional[SegyInfo]]:
    """Returns (is_segy, info). info is None when classified purely by
    extension and the structural probe could not confirm/extract fields."""
    ext_match = path.suffix.lower() in SEGY_EXTENSIONS
    info = probe_binary_header(path, size)
    structurally_segy = info is not None and info.structurally_valid
    is_segy = ext_match or structurally_segy
    if is_segy and info is not None and HAVE_SEGYIO:
        info.segyio_validated = segyio_cross_check(path)
    return is_segy, info


# --------------------------------------------------------------------------
# Whole-file dedupe (duplicates)
# --------------------------------------------------------------------------


def hash_file(path: Path, algo: str, chunk_size: int) -> Optional[str]:
    h = hashlib.new(algo)
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                h.update(chunk)
    except OSError as exc:
        eprint(f"warning: could not hash {path}: {exc}")
        return None
    return h.hexdigest()


def find_duplicates(
    all_files: list[tuple[Path, int]],
    algo: str,
    hash_chunk_size: int,
    include_paths: bool,
    max_groups_reported: int,
) -> dict:
    by_size: dict[int, list[Path]] = defaultdict(list)
    total_bytes = 0
    for path, size in all_files:
        total_bytes += size
        by_size[size].append(path)

    hashed_files = 0
    duplicate_bytes = 0
    groups: list[dict] = []

    for size, paths in by_size.items():
        if size == 0 or len(paths) < 2:
            continue
        by_hash: dict[str, list[Path]] = defaultdict(list)
        for p in paths:
            digest = hash_file(p, algo, hash_chunk_size)
            hashed_files += 1
            if digest is None:
                continue
            by_hash[digest].append(p)
        for digest, plist in by_hash.items():
            if len(plist) < 2:
                continue
            saved = size * (len(plist) - 1)
            duplicate_bytes += saved
            groups.append(
                {
                    "hash": f"{algo}:{digest}",
                    "size_bytes": size,
                    "copy_count": len(plist),
                    "bytes_saved_by_dedupe": saved,
                    "example_paths": [
                        path_identifier(p, include_paths) for p in plist[:5]
                    ],
                }
            )

    groups.sort(key=lambda g: g["bytes_saved_by_dedupe"], reverse=True)
    truncated = len(groups) > max_groups_reported
    groups = groups[:max_groups_reported]

    fraction = (duplicate_bytes / total_bytes) if total_bytes else 0.0
    return {
        "hash_algorithm": algo,
        "total_files_considered": len(all_files),
        "total_bytes_considered": total_bytes,
        "files_hashed": hashed_files,
        "duplicate_groups_found": len(groups) if not truncated else None,
        "duplicate_bytes": duplicate_bytes,
        "duplicate_byte_fraction": fraction,
        "top_duplicate_groups": groups,
        "top_duplicate_groups_truncated": truncated,
    }


# --------------------------------------------------------------------------
# Compression benchmarking
# --------------------------------------------------------------------------


def _zstd_compress(data: bytes, level: int) -> bytes:
    return zstandard.ZstdCompressor(level=level).compress(data)


def _zstd_decompress(data: bytes, original_size: int) -> bytes:
    return zstandard.ZstdDecompressor().decompress(data, max_output_size=original_size)


def _lz4_compress(data: bytes) -> bytes:
    return lz4frame.compress(data)


def _lz4_decompress(data: bytes) -> bytes:
    return lz4frame.decompress(data)


def _blosc2_compress(data: bytes, typesize: int) -> bytes:
    return blosc2.compress(
        data,
        typesize=typesize,
        clevel=5,
        filter=blosc2.Filter.SHUFFLE,
        codec=blosc2.Codec.ZSTD,
    )


def _blosc2_decompress(data: bytes) -> bytes:
    return blosc2.decompress(data)


def _zfp_compress(arr: np.ndarray) -> bytes:
    return zfpy.compress_numpy(arr)  # reversible/lossless by default for integers;
    # for floats zfpy.compress_numpy uses a tolerance/precision unless told
    # otherwise -- see bench_zfp_chunk() which forces reversible mode.


@dataclasses.dataclass
class CodecTotals:
    input_bytes: int = 0
    compressed_bytes: int = 0
    seconds: float = 0.0
    chunks: int = 0
    verified_chunks: int = 0
    verification_failures: int = 0
    status: str = "ran"  # ran | skipped_missing_dependency | skipped_ibm_format | no_data

    def add(self, input_bytes: int, compressed_bytes: int, seconds: float) -> None:
        self.input_bytes += input_bytes
        self.compressed_bytes += compressed_bytes
        self.seconds += seconds
        self.chunks += 1

    def to_dict(self) -> dict:
        ratio = (self.input_bytes / self.compressed_bytes) if self.compressed_bytes else None
        mb_per_s = (
            (self.input_bytes / (1024 * 1024)) / self.seconds if self.seconds > 0 else None
        )
        return {
            "status": self.status,
            "input_bytes": self.input_bytes,
            "compressed_bytes": self.compressed_bytes,
            "ratio": ratio,
            "seconds": self.seconds,
            "throughput_mb_per_s": mb_per_s,
            "chunks_processed": self.chunks,
            "chunks_verified_roundtrip": self.verified_chunks,
            "verification_failures": self.verification_failures,
        }


def bench_one_chunk(name: str, chunk: bytes, typesize: int) -> tuple[bytes, float]:
    t0 = time.perf_counter()
    if name == "zstd-1":
        out = _zstd_compress(chunk, level=1)
    elif name == "zstd-3":
        out = _zstd_compress(chunk, level=3)
    elif name == "lz4":
        out = _lz4_compress(chunk)
    elif name == "blosc2-shuffle-zstd":
        out = _blosc2_compress(chunk, typesize=typesize)
    else:
        raise ValueError(name)
    return out, time.perf_counter() - t0


def verify_one_chunk(name: str, chunk: bytes, compressed: bytes) -> bool:
    try:
        if name == "zstd-1" or name == "zstd-3":
            back = _zstd_decompress(compressed, len(chunk))
        elif name == "lz4":
            back = _lz4_decompress(compressed)
        elif name == "blosc2-shuffle-zstd":
            back = _blosc2_decompress(compressed)
        else:
            raise ValueError(name)
    except Exception as exc:  # noqa: BLE001 -- any decompress failure = not verified
        eprint(f"warning: {name} round-trip raised {exc!r}")
        return False
    return back == chunk


def bench_zfp_chunk(arr: np.ndarray) -> tuple[bytes, float]:
    t0 = time.perf_counter()
    compressed = zfpy.compress_numpy(arr, write_header=True, tolerance=-1)  # reversible
    return compressed, time.perf_counter() - t0


def verify_zfp_chunk(arr: np.ndarray, compressed: bytes) -> bool:
    try:
        back = zfpy.decompress_numpy(compressed)
    except Exception as exc:  # noqa: BLE001
        eprint(f"warning: zfp round-trip raised {exc!r}")
        return False
    return np.array_equal(back, arr)


def read_trace_section_chunks(path: Path, file_size: int, chunk_size: int) -> Iterator[bytes]:
    """Raw bytes after the fixed 3600-byte SEG-Y file header, in chunks.
    This includes interleaved 240-byte trace headers -- fine for zstd/lz4,
    and for blosc2 which is only asked to byte-shuffle at typesize 4."""
    start = FILE_HEADER_SIZE if file_size > FILE_HEADER_SIZE else 0
    with open(path, "rb") as f:
        f.seek(start)
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                return
            yield chunk


def read_trace_sample_float32_chunks(
    path: Path, samples_per_trace: int, target_floats_per_chunk: int
) -> Iterator[np.ndarray]:
    """Trace-aware extraction of IEEE float32 sample values only (trace
    headers stripped), for the zfp benchmark. Only called when the file's
    sample format code is 5 (IEEE float)."""
    trace_data_bytes = samples_per_trace * 4
    trace_stride = TRACE_HEADER_SIZE + trace_data_bytes
    buf: list[np.ndarray] = []
    buffered_floats = 0
    with open(path, "rb") as f:
        f.seek(FILE_HEADER_SIZE)
        while True:
            trace = f.read(trace_stride)
            if len(trace) < trace_stride:
                break
            sample_bytes = trace[TRACE_HEADER_SIZE:]
            arr = np.frombuffer(sample_bytes, dtype=">f4").astype(np.float32)
            buf.append(arr)
            buffered_floats += arr.size
            if buffered_floats >= target_floats_per_chunk:
                yield np.concatenate(buf)
                buf = []
                buffered_floats = 0
    if buf:
        yield np.concatenate(buf)


def profile_file_compression(
    path: Path,
    file_size: int,
    info: SegyInfo,
    chunk_size: int,
) -> dict:
    totals = {name: CodecTotals() for name in CODEC_NAMES}

    if not HAVE_BLOSC2:
        totals["blosc2-shuffle-zstd"].status = "skipped_missing_dependency"
    if not HAVE_ZFP:
        totals["zfp-reversible"].status = "skipped_missing_dependency"
    elif info.sample_format_code == IBM_FLOAT32_FORMAT_CODE:
        totals["zfp-reversible"].status = "skipped_ibm_format"
    elif info.sample_format_code != IEEE_FLOAT32_FORMAT_CODE:
        totals["zfp-reversible"].status = "skipped_not_ieee_float"

    first_chunk_done = {name: False for name in CODEC_NAMES}

    for chunk in read_trace_section_chunks(path, file_size, chunk_size):
        usable_len = len(chunk) - (len(chunk) % 4)
        chunk4 = chunk[:usable_len]

        for name in ("zstd-1", "zstd-3", "lz4"):
            compressed, seconds = bench_one_chunk(name, chunk, typesize=4)
            totals[name].add(len(chunk), len(compressed), seconds)
            if not first_chunk_done[name]:
                ok = verify_one_chunk(name, chunk, compressed)
                totals[name].verified_chunks += int(ok)
                totals[name].verification_failures += int(not ok)
                first_chunk_done[name] = True

        if HAVE_BLOSC2 and usable_len > 0:
            name = "blosc2-shuffle-zstd"
            compressed, seconds = bench_one_chunk(name, chunk4, typesize=4)
            totals[name].add(len(chunk4), len(compressed), seconds)
            if not first_chunk_done[name]:
                ok = verify_one_chunk(name, chunk4, compressed)
                totals[name].verified_chunks += int(ok)
                totals[name].verification_failures += int(not ok)
                first_chunk_done[name] = True

    if (
        HAVE_ZFP
        and info.sample_format_code == IEEE_FLOAT32_FORMAT_CODE
        and info.samples_per_trace
    ):
        name = "zfp-reversible"
        target_floats = max(chunk_size // 4, 1)
        for arr in read_trace_sample_float32_chunks(
            path, info.samples_per_trace, target_floats
        ):
            if arr.size == 0:
                continue
            compressed, seconds = bench_zfp_chunk(arr)
            totals[name].add(arr.nbytes, len(compressed), seconds)
            if not first_chunk_done[name]:
                ok = verify_zfp_chunk(arr, compressed)
                totals[name].verified_chunks += int(ok)
                totals[name].verification_failures += int(not ok)
                first_chunk_done[name] = True

    for name, t in totals.items():
        if t.chunks == 0 and t.status == "ran":
            t.status = "no_data"

    return {name: t.to_dict() for name, t in totals.items()}


def run_compression_sample(
    segy_files: list[tuple[Path, int, SegyInfo]],
    sample_gb: float,
    seed: int,
    chunk_mb: int,
    include_paths: bool,
) -> dict:
    rng = random.Random(seed)
    candidates = list(segy_files)
    rng.shuffle(candidates)

    budget_bytes = int(sample_gb * (1024**3))
    chosen: list[tuple[Path, int, SegyInfo]] = []
    running = 0
    for path, size, info in candidates:
        if running >= budget_bytes and chosen:
            break
        chosen.append((path, size, info))
        running += size

    chunk_size = chunk_mb * 1024 * 1024
    aggregate = {name: CodecTotals() for name in CODEC_NAMES}
    per_file = []

    for path, size, info in chosen:
        try:
            result = profile_file_compression(path, size, info, chunk_size)
        except OSError as exc:
            eprint(f"warning: could not compress-profile {path}: {exc}")
            continue
        per_file.append(
            {
                "id": path_identifier(path, include_paths),
                "size_bytes": size,
                "sample_format_code": info.sample_format_code,
                "samples_per_trace": info.samples_per_trace,
                "segyio_validated": info.segyio_validated,
                "codecs": result,
            }
        )
        for name, stats in result.items():
            agg = aggregate[name]
            if stats["status"] not in ("ran",) and agg.status == "ran":
                # Preserve the most informative "why nothing ran" reason we
                # have seen so far, but let a later real run override it.
                agg.status = stats["status"]
            if stats["chunks_processed"]:
                agg.status = "ran"
                agg.input_bytes += stats["input_bytes"]
                agg.compressed_bytes += stats["compressed_bytes"]
                agg.seconds += stats["seconds"]
                agg.chunks += stats["chunks_processed"]
                agg.verified_chunks += stats["chunks_verified_roundtrip"]
                agg.verification_failures += stats["verification_failures"]

    return {
        "sample_file_count": len(chosen),
        "sample_total_bytes": sum(s for _, s, _ in chosen),
        "sample_budget_bytes": budget_bytes,
        "seed": seed,
        "chunk_mb": chunk_mb,
        "codec_totals": {name: t.to_dict() for name, t in aggregate.items()},
        "per_file": per_file,
    }


# --------------------------------------------------------------------------
# Top-level run
# --------------------------------------------------------------------------


def run_profile(args: argparse.Namespace) -> dict:
    started = time.time()
    all_files: list[tuple[Path, int]] = []
    segy_files: list[tuple[Path, int, SegyInfo]] = []
    non_segy_by_ext: dict[str, dict] = defaultdict(lambda: {"count": 0, "bytes": 0})
    segy_ext_only_count = 0

    for path in iter_files(args.directories):
        try:
            size = path.stat().st_size
        except OSError as exc:
            eprint(f"warning: could not stat {path}: {exc}")
            continue
        all_files.append((path, size))

        if args.dry_run:
            continue

        is_segy, info = classify_file(path, size)
        if is_segy:
            if info is None or not info.structurally_valid:
                segy_ext_only_count += 1
                info = SegyInfo(None, None, None, False, None)
            segy_files.append((path, size, info))
        else:
            ext = path.suffix.lower() or "(none)"
            non_segy_by_ext[ext]["count"] += 1
            non_segy_by_ext[ext]["bytes"] += size

    total_bytes = sum(s for _, s in all_files)

    result: dict = {
        "run": {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
            "directories": [str(Path(d)) for d in args.directories],
            "dry_run": args.dry_run,
            "self_test": False,
            "include_paths": args.include_paths,
            "sample_gb": args.sample_gb,
            "seed": args.seed,
            "chunk_mb": args.chunk_mb,
            "hash_algorithm": args.hash_algo,
            "optional_codecs": {
                "blosc2_available": HAVE_BLOSC2,
                "zfpy_available": HAVE_ZFP,
                "segyio_available": HAVE_SEGYIO,
            },
        },
        "files": {
            "total_count": len(all_files),
            "total_bytes": total_bytes,
        },
    }

    if args.dry_run:
        result["files"]["note"] = "dry-run: counts only, no classification/hashing/compression"
        return result

    result["files"]["segy_count"] = len(segy_files)
    result["files"]["segy_bytes"] = sum(s for _, s, _ in segy_files)
    result["files"]["segy_extension_only_no_structural_match"] = segy_ext_only_count
    result["files"]["non_segy_count"] = sum(v["count"] for v in non_segy_by_ext.values())
    result["files"]["non_segy_bytes"] = sum(v["bytes"] for v in non_segy_by_ext.values())
    result["files"]["non_segy_by_extension"] = dict(non_segy_by_ext)

    result["duplicates"] = find_duplicates(
        all_files,
        algo=args.hash_algo,
        hash_chunk_size=args.hash_chunk_mb * 1024 * 1024,
        include_paths=args.include_paths,
        max_groups_reported=args.max_duplicate_groups,
    )

    result["compression"] = run_compression_sample(
        segy_files,
        sample_gb=args.sample_gb,
        seed=args.seed,
        chunk_mb=args.chunk_mb,
        include_paths=args.include_paths,
    )

    result["run"]["elapsed_seconds"] = time.time() - started
    return result


# --------------------------------------------------------------------------
# Human-readable summary
# --------------------------------------------------------------------------


def human_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if abs(n) < 1024.0:
            return f"{n:,.1f} {unit}"
        n /= 1024.0
    return f"{n:,.1f} EiB"


def print_summary(result: dict) -> None:
    run = result["run"]
    files = result["files"]
    print("=" * 72)
    print("SEG-Y data test summary")
    print("=" * 72)
    print(f"Directories:        {', '.join(run['directories'])}")
    print(f"Total files:        {files['total_count']:,}  ({human_bytes(files['total_bytes'])})")

    if run["dry_run"]:
        print("(dry-run: no classification, hashing, or compression performed)")
        return

    print(
        f"SEG-Y files:        {files['segy_count']:,}  ({human_bytes(files['segy_bytes'])})"
        f"  [{files['segy_extension_only_no_structural_match']} matched by extension only]"
    )
    print(f"Non-SEG-Y files:    {files['non_segy_count']:,}  ({human_bytes(files['non_segy_bytes'])})")
    if files["non_segy_by_extension"]:
        top_ext = sorted(
            files["non_segy_by_extension"].items(), key=lambda kv: kv[1]["bytes"], reverse=True
        )[:10]
        print("  top non-SEG-Y extensions by bytes:")
        for ext, stats in top_ext:
            print(f"    {ext:<12} {stats['count']:>8,} files  {human_bytes(stats['bytes'])}")

    dup = result["duplicates"]
    print()
    print("Whole-file dedupe (duplicates)")
    print("-" * 72)
    print(f"  hash algorithm:          {dup['hash_algorithm']}")
    print(f"  files hashed:            {dup['files_hashed']:,}")
    print(f"  duplicate bytes:         {human_bytes(dup['duplicate_bytes'])}")
    print(f"  duplicate byte fraction: {dup['duplicate_byte_fraction']:.2%}")
    if dup["top_duplicate_groups"]:
        print("  largest duplicate groups (bytes saved by keeping one copy):")
        for g in dup["top_duplicate_groups"][:5]:
            print(
                f"    {human_bytes(g['bytes_saved_by_dedupe']):>12}  "
                f"{g['copy_count']} copies x {human_bytes(g['size_bytes'])}"
            )

    comp = result["compression"]
    print()
    print("Compression sample")
    print("-" * 72)
    print(f"  sampled files:  {comp['sample_file_count']:,}  ({human_bytes(comp['sample_total_bytes'])})")
    print(f"  seed:           {comp['seed']}   chunk size: {comp['chunk_mb']} MiB")
    print(f"  {'codec':<22}{'status':<28}{'ratio':>8}{'MB/s':>10}{'verify fails':>14}")
    for name in CODEC_NAMES:
        stats = comp["codec_totals"][name]
        ratio = f"{stats['ratio']:.3f}" if stats["ratio"] else "-"
        mbps = f"{stats['throughput_mb_per_s']:.1f}" if stats["throughput_mb_per_s"] else "-"
        print(
            f"  {name:<22}{stats['status']:<28}{ratio:>8}{mbps:>10}{stats['verification_failures']:>14}"
        )
    print("=" * 72)


# --------------------------------------------------------------------------
# Self-test: synthetic SEG-Y files, no client data required
# --------------------------------------------------------------------------


def _write_binary_header(f, sample_format_code: int, samples_per_trace: int) -> None:
    header = bytearray(BINARY_HEADER_SIZE)
    struct.pack_into(">H", header, SAMPLES_PER_TRACE_OFFSET, samples_per_trace)
    struct.pack_into(">H", header, SAMPLE_FORMAT_CODE_OFFSET, sample_format_code)
    f.write(bytes(header))


def _write_synthetic_segy(
    path: Path,
    sample_format_code: int,
    samples_per_trace: int = 50,
    num_traces: int = 8,
    seed: int = 0,
) -> None:
    rng = np.random.default_rng(seed)
    with open(path, "wb") as f:
        f.write(b" " * TEXT_HEADER_SIZE)  # dummy textual header
        _write_binary_header(f, sample_format_code, samples_per_trace)
        for _ in range(num_traces):
            f.write(bytes(TRACE_HEADER_SIZE))  # zeroed trace header
            if sample_format_code == IEEE_FLOAT32_FORMAT_CODE:
                samples = rng.standard_normal(samples_per_trace).astype(">f4")
                f.write(samples.tobytes())
            else:
                # IBM float (or any other opaque 4-byte-per-sample format):
                # content doesn't need to be valid IBM-754, only present, so
                # zstd/lz4/blosc2 have real bytes to compress and the format
                # code correctly steers zfp to skip.
                f.write(rng.integers(0, 256, size=samples_per_trace * 4, dtype=np.uint8).tobytes())


def self_test(args: argparse.Namespace) -> int:
    tmpdir = Path(tempfile.mkdtemp(prefix="segy_profile_selftest_"))
    ok = True
    try:
        ieee_path = tmpdir / "synthetic_ieee.sgy"
        ibm_path = tmpdir / "synthetic_ibm.segy"
        ieee_dup_path = tmpdir / "synthetic_ieee_copy.sgy"
        other_path = tmpdir / "notes.txt"

        _write_synthetic_segy(ieee_path, IEEE_FLOAT32_FORMAT_CODE, seed=1)
        _write_synthetic_segy(ibm_path, IBM_FLOAT32_FORMAT_CODE, seed=2)
        shutil.copyfile(ieee_path, ieee_dup_path)  # exact duplicate, for dedupe check
        other_path.write_text("this is not a SEG-Y file\n" * 20)

        selftest_args = argparse.Namespace(
            directories=[str(tmpdir)],
            dry_run=False,
            self_test=False,
            sample_gb=0.001,
            seed=args.seed,
            chunk_mb=1,
            hash_algo="blake2b",
            hash_chunk_mb=8,
            include_paths=True,
            max_duplicate_groups=10,
            output=None,
        )
        result = run_profile(selftest_args)

        print(json.dumps(result, indent=2, default=str))
        print()

        checks = []
        # 3 SEG-Y files: the IEEE original, its exact duplicate, and the IBM one.
        checks.append(("found 3 SEG-Y files", result["files"]["segy_count"] == 3))
        checks.append(("found 1 non-SEG-Y file", result["files"]["non_segy_count"] == 1))
        dup = result["duplicates"]
        checks.append(("dedupe found the exact-copy pair", dup["duplicate_bytes"] > 0))

        codecs = result["compression"]["codec_totals"]
        for name in ("zstd-1", "zstd-3", "lz4"):
            checks.append((f"{name} ran with 0 verify failures", codecs[name]["status"] == "ran" and codecs[name]["verification_failures"] == 0))
        if HAVE_BLOSC2:
            checks.append(("blosc2 ran with 0 verify failures", codecs["blosc2-shuffle-zstd"]["status"] == "ran" and codecs["blosc2-shuffle-zstd"]["verification_failures"] == 0))
        else:
            checks.append(("blosc2 skipped cleanly (not installed)", codecs["blosc2-shuffle-zstd"]["status"] == "skipped_missing_dependency"))

        if HAVE_ZFP:
            checks.append(("zfp ran on the IEEE file with 0 verify failures", codecs["zfp-reversible"]["status"] == "ran" and codecs["zfp-reversible"]["verification_failures"] == 0))
        else:
            checks.append(("zfp skipped cleanly (not installed)", codecs["zfp-reversible"]["status"] == "skipped_missing_dependency"))

        # Confirm the per-file record for the IBM file reports skip-for-IBM
        # reasoning somewhere (either at the aggregate level when zfp is
        # installed, or as missing-dependency when it is not).
        ibm_records = [
            pf for pf in result["compression"]["per_file"] if pf["sample_format_code"] == IBM_FLOAT32_FORMAT_CODE
        ]
        if HAVE_ZFP:
            checks.append(
                (
                    "IBM-format file's zfp entry is skipped_ibm_format",
                    bool(ibm_records) and ibm_records[0]["codecs"]["zfp-reversible"]["status"] == "skipped_ibm_format",
                )
            )

        print("Self-test checks:")
        for name, passed in checks:
            print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
            ok = ok and passed

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    if ok:
        print("\nSELF-TEST PASSED")
        return 0
    print("\nSELF-TEST FAILED")
    return 1


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Profile SEG-Y datasets for whole-file dedupe potential and "
        "lossless compression ratio/throughput (OmniFile Gateway pilot 3)."
    )
    p.add_argument("directories", nargs="*", help="Directories to walk (ignored with --self-test)")
    p.add_argument("--dry-run", action="store_true", help="Only count files/bytes, skip everything else")
    p.add_argument("--self-test", action="store_true", help="Run against generated synthetic SEG-Y files, ignore directories")
    p.add_argument("--sample-gb", type=float, default=50.0, help="Total bytes (in GB) of SEG-Y files to sample for compression testing (default: 50)")
    p.add_argument("--seed", type=int, default=42, help="Random seed for sample selection (default: 42)")
    p.add_argument("--chunk-mb", type=int, default=64, help="Chunk size in MiB for compression (default: 64)")
    p.add_argument("--hash-algo", choices=["blake2b", "sha256"], default="blake2b", help="Whole-file hash algorithm for dedupe (default: blake2b)")
    p.add_argument("--hash-chunk-mb", type=int, default=8, help="Streaming read chunk size in MiB while hashing (default: 8)")
    p.add_argument("--include-paths", action="store_true", help="Include real file paths in JSON output (default: hashed path identifiers only)")
    p.add_argument("--max-duplicate-groups", type=int, default=50, help="Cap on duplicate groups listed in JSON output (default: 50)")
    p.add_argument("--output", "-o", default="segy_profile_results.json", help="Path to write JSON results (default: segy_profile_results.json)")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test(args)

    if not args.directories:
        parser.error("at least one directory is required (or pass --self-test)")

    result = run_profile(args)
    print_summary(result)

    if args.output:
        with open(args.output, "w") as f:
            json.dump(result, f, indent=2, default=str)
        print(f"\nJSON results written to: {args.output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
