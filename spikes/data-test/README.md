# Data test (pilot 3) — SEG-Y compression + dedupe profiler

This is one self-contained tool: `segy_profile.py`. It answers the two
questions in `docs/PILOTS.md` #3:

- What lossless compression ratio and single-core throughput can we get on
  the client's own SEG-Y files, for zstd, lz4, Blosc2 (byte-shuffle + zstd),
  and zfp (reversible mode, IEEE-float traces only)?
- What share of bytes across the client's project trees are byte-identical
  copies of each other (what whole-file dedupe would save)?

It never sends anything anywhere — everything runs locally and writes one
JSON file plus a summary on stdout. It does not modify or move the client's
files; it only opens them for reading.

## Prerequisites

- **uv** (https://docs.astral.sh/uv/), any recent version. Install with
  `curl -LsSf https://astral.sh/uv/install.sh | sh` or via your package
  manager. Nothing else needs to be installed by hand — `uv run` reads the
  dependency list embedded at the top of `segy_profile.py` (PEP 723) and
  builds an isolated environment on first run.
- Python **3.12+** (uv will fetch a matching interpreter itself if needed —
  you do not need to have one installed already).
- Read access to the directories you want profiled. No write access is
  needed and nothing under those directories is modified.
- Enough free disk/temp space for uv's package cache (a few tens of MB).

You do **not** need `zfpy` or `segyio` installed for the tool to run — see
"Optional codecs" below. They were both available and tested by us on
Apple Silicon macOS; on other platforms (in particular some Linux/glibc
combinations) `zfpy` may not have a pre-built wheel, in which case it is
skipped automatically.

## Quick check before touching client data

Run the built-in self-test first. It fabricates two tiny synthetic SEG-Y
files (one IEEE-float, one IBM-float) plus a duplicate copy and a plain text
file, runs the full pipeline against them, and asserts the results are what
they should be. No client data is read or required.

```bash
uv run segy_profile.py --self-test
```

Expect it to end with `SELF-TEST PASSED`. If it does not, stop and send us
the full output plus your OS/architecture (`uname -a`) and `uv --version`
before running against real data.

## Running against the client's data

```bash
# Count only, no reading of file contents, no hashing, no compression.
# Good for a first sanity check of scope (how many files, how many bytes)
# before committing to the full run's I/O and CPU time.
uv run segy_profile.py --dry-run /path/to/share1 /path/to/share2

# The real run. Adjust --sample-gb to whatever amount of compression
# testing time you can afford (it reads and compresses that much data
# in total, once per codec).
uv run segy_profile.py \
    --sample-gb 50 \
    --output segy_profile_results.json \
    /path/to/share1 /path/to/share2
```

Useful flags (all optional, see `--help` for the complete list):

| Flag | Default | What it does |
|---|---|---|
| `--sample-gb N` | 50 | Total bytes (in GB) of SEG-Y files to sample for the compression benchmark. Selection is random but seeded, so it is reproducible. |
| `--seed N` | 42 | Random seed for sample selection. |
| `--chunk-mb N` | 64 | Chunk size used when compressing, to bound memory on very large files. |
| `--hash-algo {blake2b,sha256}` | blake2b | Whole-file hash used for dedupe. |
| `--include-paths` | off | Include real file paths in the JSON output. **Default is off** — file paths may be considered sensitive by the client, so by default every file is identified only by a truncated SHA-256 hash of its path (`id` field), which is stable across a re-run but does not reveal the path. |
| `--max-duplicate-groups N` | 50 | Cap on how many duplicate-file groups are listed in the JSON (the largest, by bytes saved, are kept). |
| `-o / --output PATH` | `segy_profile_results.json` | Where to write the JSON results. |

Runtime scales with `--sample-gb` (compression is the CPU-bound part) and
with the total number of files under the given directories (dedupe hashing
and SEG-Y classification touch every file once). Expect the dry-run to be
fast (a directory walk + `stat()` per file); expect the full run with the
default 50 GB sample to take from tens of minutes to a few hours depending
on core speed and disk throughput — there is no way to estimate this
precisely without knowing the client's hardware, so start with a smaller
`--sample-gb` (e.g. 2) to gauge per-GB time on their box before committing
to a full-size run.

## What each part measures, and how

- **SEG-Y detection.** A file counts as SEG-Y if it has a `.segy`/`.sgy`
  extension, **or** if its first 3600 bytes look like a valid SEG-Y header
  (3200-byte textual header + 400-byte binary header, with a recognised
  sample-format code and a sane samples-per-trace value at the standard
  byte offsets). Files that only match by extension but fail the
  structural check are still counted as SEG-Y (`segy_extension_only_no_structural_match`
  in the JSON) but are not sent through the compression benchmark, since we
  can't reliably locate the trace data in them.
- **Duplicates.** Every file (SEG-Y or not) is grouped by exact size first;
  only files that share a size are actually hashed (streamed, 8 MiB chunks
  by default), which keeps the cost down on datasets with few real
  duplicates. `duplicate_byte_fraction` in the JSON is the share of all
  scanned bytes that are "extra" copies (i.e. total bytes minus one copy per
  duplicate group), divided by total bytes scanned — that's the fraction
  whole-file dedupe could reclaim.
- **Compression.** A random (seeded) sample of SEG-Y files, up to
  `--sample-gb` total, is compressed and measured per codec:
  - `zstd-1`, `zstd-3`, `lz4`: run against the raw bytes after the fixed
    3600-byte file header (i.e. the whole trace section, headers and
    samples interleaved as they sit on disk), in `--chunk-mb` chunks.
  - `blosc2-shuffle-zstd`: same input, byte-shuffled at typesize 4 before
    zstd, as specified in `docs/PILOTS.md`.
  - `zfp-reversible`: **only** for files whose sample-format code is 5
    (IEEE float32). It properly strips the 240-byte trace header from each
    trace first, so it only ever sees real sample values, not header
    bytes. Files with format code 1 (IBM float) are reported as
    `skipped_ibm_format` — IBM-to-IEEE conversion is not guaranteed
    lossless, so we don't attempt it. Other format codes are reported as
    `skipped_not_ieee_float`.
  - Every codec is verified end-to-end: at least the first chunk of every
    sampled file is decompressed and byte-compared (or array-compared, for
    zfp) against the original. Any mismatch increments
    `verification_failures` in that codec's stats — this should always be
    zero; a non-zero value means don't trust that codec's ratio numbers
    and tell us immediately.
- **Non-SEG-Y files** are counted and bucketed by extension
  (`non_segy_by_extension`) but are not compression-tested — the codec
  choices above are specific to seismic trace data.

## Optional codecs: zfpy and segyio

`zfpy` (the zfp benchmark) and `segyio` (an extra structural cross-check,
recorded per file as `segyio_validated` when present) are **not** in the
hard dependency list at the top of `segy_profile.py`. That's deliberate:
`zfpy` ships pre-built wheels for a narrower set of platforms than
zstandard/lz4/blosc2/numpy, and we don't want `uv run` to fail outright on
a client box that lacks one, before the tool has produced anything. Both
are imported with a plain `try/except`, and anything that needs them is
skipped cleanly and reported as such (`skipped_missing_dependency` in the
JSON) when they're not importable.

To get them:

```bash
# Try it for one run without changing the file:
uv run --with zfpy --with segyio segy_profile.py ...

# Or persist it into the script's own PEP 723 metadata:
uv add --script segy_profile.py zfpy segyio
```

We ran `uv run --with zfpy --with segyio segy_profile.py --self-test` on an
Apple Silicon Mac and both installed and worked without issue, exercising
the full zfp path (including the IBM-format skip). We could not verify
zfpy's wheel availability on the client's actual OS/architecture — if
`uv run --with zfpy ...` fails to install there, that's expected and
fine: proceed without it and note it in what you send back.

## What to send back

1. The JSON file (`segy_profile_results.json` by default, or whatever you
   passed to `--output`). Leave `--include-paths` off unless the client has
   explicitly said raw paths are fine to share — the default hashed `id`
   values are enough for us to reconstruct duplicate-group sizes and
   per-codec stats without seeing directory/file naming.
2. The human-readable summary that printed to stdout (redirect it to a
   file with `... | tee run_summary.txt` if you'd rather not retype it).
3. `uv --version`, `uname -a` (or `systeminfo` on Windows), and core
   count/CPU model of the machine you ran it on — throughput numbers are
   meaningless without knowing the hardware they came from.
4. Whether `--self-test` passed before you ran against real data, and
   whether zfpy/segyio were installed (and if you tried and either failed
   to install, what the error was).
5. If you had to reduce `--sample-gb` to fit the time available, say what
   value you used and roughly how long the run took, so we can judge
   whether the sample is representative.

## Self-test details (for us, not required reading for the pilot runner)

`--self-test` builds two tiny synthetic SEG-Y files in a temp directory —
one with sample-format code 5 (IEEE float32, real random trace samples) and
one with code 1 (IBM float, opaque random bytes) — plus an exact duplicate
of the IEEE file and one plain-text file, then runs the identical code path
used for a real run against them and asserts:

- both extension-matching files are classified as SEG-Y, and the plain
  text file is not;
- the duplicate pair is found and contributes bytes to the dedupe result;
- zstd-1/zstd-3/lz4 always run with zero round-trip verification failures;
- blosc2 runs (or is reported as cleanly skipped, if not installed) with
  zero verification failures;
- zfp runs on the IEEE file (or is cleanly skipped if `zfpy` is not
  installed), also with zero verification failures, and is explicitly
  skipped with reason `skipped_ibm_format` for the IBM file whenever zfp
  itself is available.

We ran this locally (`uv run segy_profile.py --self-test`, and again with
`--with zfpy --with segyio`) before shipping this kit; both passed.
