#!/usr/bin/env python3
"""Write the one release version (the repository's VERSION file) everywhere it is declared, or check it.

    release/stamp.sh [--version V]            write V (default: VERSION's) into every manifest and lockfile
    release/stamp.sh --check [--no-tools] [--edge-bin FILE]

VERSION holds one SemVer version, MAJOR.MINOR.PATCH with an optional pre-release of alpha.N, beta.N or rc.N
(0.1.0, 1.2.0-rc.1). It is written, as exact line edits that change nothing else, into:

  edge/Cargo.toml            [workspace.package] version (every crate inherits it, so CARGO_PKG_VERSION, which the
                             edge's heartbeat and `omnifile-edge --version` report, is this version)
  edge/Cargo.lock            the workspace crates' own entries (cargo --locked refuses a stale one)
  service/pyproject.toml     [project] version, as PEP 440 (1.2.0-rc.1 is 1.2.0rc1); omnifile_service.__version__
  relay/pyproject.toml       reads it back from the installed package's metadata
  service/uv.lock            the project's own (editable) entry (uv --locked refuses a stale one)
  relay/uv.lock
  portal/package.json        "version"
  portal/package-lock.json   the root "version" and packages[""].version

--check reads every one of them back, and, unless --no-tools:
  - `cargo metadata --no-deps --locked --offline`: each workspace crate resolves to VERSION;
  - `uv run --locked` in service/ and relay/: the installed package's __version__ is VERSION (and the lockfile is
    current for the pyproject);
and, with --edge-bin (cargo and uv or not), that binary's `--version` says `omnifile-edge VERSION`.
It exits 1 and names each file that disagrees. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import pinned  # noqa: E402
SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-(alpha|beta|rc)\.(0|[1-9]\d*))?$")
PEP440_PRE = {"alpha": "a", "beta": "b", "rc": "rc"}
PY_PROJECTS = {"service": "omnifile-service", "relay": "omnifile-relay"}
PY_MODULES = {"service": "omnifile_service", "relay": "omnifile_relay"}


class StampError(Exception):
    pass


def parse_version(v: str) -> str:
    v = v.strip()
    if not SEMVER_RE.match(v):
        raise StampError(
            f"VERSION {v!r} is not MAJOR.MINOR.PATCH with an optional -alpha.N, -beta.N or -rc.N (SemVer)"
        )
    return v


def pep440(v: str) -> str:
    m = SEMVER_RE.match(v)
    assert m
    base = ".".join(m.group(i) for i in (1, 2, 3))
    return base + (f"{PEP440_PRE[m.group(4)]}{m.group(5)}" if m.group(4) else "")


def read_version(repo: Path) -> str:
    f = repo / "VERSION"
    if not f.is_file():
        raise StampError(f"no {f}")
    return parse_version(f.read_text())


# --- the edits: each returns (new text, the version it found) and touches exactly one kind of line -------------


def _toml_table_version(text: str, table: str, path: str, new: str | None) -> tuple[str, str]:
    """The `version = "..."` line directly in [table] (not in a sub-table)."""
    lines = text.splitlines(keepends=True)
    in_table = False
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("["):
            in_table = s == f"[{table}]"
            continue
        m = re.match(r'^version\s*=\s*"([^"]*)"\s*$', s)
        if in_table and m:
            if new is not None:
                lines[i] = re.sub(r'"[^"]*"', f'"{new}"', line, count=1)
            return "".join(lines), m.group(1)
    raise StampError(f"{path}: no version line in [{table}]")


def _lock_entry_version(
    text: str, name: str, path: str, new: str | None, *, source: str | None
) -> tuple[str, str]:
    """The version line of the [[package]] entry named `name` whose `source` is `source` (None: no source line,
    as Cargo writes for workspace crates). Exactly one entry must match."""
    blocks = re.split(r"(?m)^(?=\[\[package\]\]\s*$)", text)
    hits = []
    for bi, block in enumerate(blocks):
        if not block.startswith("[[package]]"):
            continue
        if not re.search(rf'(?m)^name = "{re.escape(name)}"\s*$', block):
            continue
        src = re.search(r"(?m)^source = (.*)$", block)
        if (src.group(1).strip() if src else None) != source:
            continue
        hits.append(bi)
    if len(hits) != 1:
        raise StampError(f"{path}: {len(hits)} entries for {name} (source {source}), expected one")
    bi = hits[0]
    m = re.search(r'(?m)^version = "([^"]*)"\s*$', blocks[bi])
    if not m:
        raise StampError(f"{path}: the {name} entry has no version line")
    if new is not None:
        blocks[bi] = blocks[bi][: m.start(1)] + new + blocks[bi][m.end(1) :]
    return "".join(blocks), m.group(1)


def _json_version_lines(text: str, path: str, new: str | None, *, lock: bool) -> tuple[str, list[str]]:
    """package.json's top-level "version"; for a package-lock.json also packages[""].version. Edited as lines, so
    the file's formatting is kept, then parsed again to prove only those values changed."""
    data = json.loads(text)
    lines = text.splitlines(keepends=True)
    found: list[str] = []
    targets: list[int] = []
    # The top level: the first "version" key at two spaces of indent.
    for i, line in enumerate(lines):
        if re.match(r'^  "version": "[^"]*",?\s*$', line):
            targets.append(i)
            break
    if lock:
        start = next(
            (i for i, line in enumerate(lines) if re.match(r'^    "": \{\s*$', line)), None
        )
        if start is None:
            raise StampError(f'{path}: no packages[""] entry')
        for i in range(start + 1, len(lines)):
            if re.match(r"^    \}", lines[i]):
                break
            if re.match(r'^      "version": "[^"]*",?\s*$', lines[i]):
                targets.append(i)
                break
    if len(targets) != (2 if lock else 1):
        raise StampError(f"{path}: could not find its version line(s)")
    for i in targets:
        found.append(re.search(r'"version": "([^"]*)"', lines[i]).group(1))
        if new is not None:
            lines[i] = re.sub(r'("version": ")[^"]*(")', rf"\g<1>{new}\g<2>", lines[i], count=1)
    out = "".join(lines)
    want = json.loads(out)
    if new is not None:
        expect = dict(data)
        expect["version"] = new
        if lock:
            expect["packages"] = dict(data["packages"])
            expect["packages"][""] = dict(data["packages"][""], version=new)
        if want != expect:
            raise StampError(f"{path}: the edit changed more than its version lines")
    else:
        if data.get("version") != found[0] or (lock and data["packages"][""].get("version") != found[1]):
            raise StampError(f"{path}: its version lines are not the ones JSON reads")
    return out, found


def workspace_crates(repo: Path) -> list[str]:
    names = []
    for toml in sorted((repo / "edge" / "crates").glob("*/Cargo.toml")):
        m = re.search(r'(?ms)^\[package\]\s*$.*?^name\s*=\s*"([^"]+)"', toml.read_text())
        if not m:
            raise StampError(f"{toml}: no [package] name")
        names.append(m.group(1))
    if not names:
        raise StampError(f"no crates under {repo / 'edge' / 'crates'}")
    return names


def edits(repo: Path):
    """Every (path, edit, kind): edit(text, new or None) -> (text, [versions found]); kind is the version's
    spelling there, "semver" or "pep440"."""

    def toml_table(path, table):
        def edit(text, new):
            out, found = _toml_table_version(text, table, path, new)
            return out, [found]

        return edit

    def lock_entry(path, name, source):
        def edit(text, new):
            out, found = _lock_entry_version(text, name, path, new, source=source)
            return out, [found]

        return edit

    def cargo_lock(text, new):
        found = []
        for crate in workspace_crates(repo):
            text, v = _lock_entry_version(text, crate, "edge/Cargo.lock", new, source=None)
            found.append(v)
        return text, found

    def npm(path, lock):
        return lambda text, new: _json_version_lines(text, path, new, lock=lock)

    yield "edge/Cargo.toml", toml_table("edge/Cargo.toml", "workspace.package"), "semver"
    yield "edge/Cargo.lock", cargo_lock, "semver"
    for d, dist in PY_PROJECTS.items():
        yield f"{d}/pyproject.toml", toml_table(f"{d}/pyproject.toml", "project"), "pep440"
        yield f"{d}/uv.lock", lock_entry(f"{d}/uv.lock", dist, '{ editable = "." }'), "pep440"
    yield "portal/package.json", npm("portal/package.json", False), "semver"
    yield "portal/package-lock.json", npm("portal/package-lock.json", True), "semver"


def stamp(repo: Path, version: str) -> list[str]:
    changed = []
    for rel, fn, kind in edits(repo):
        want = version if kind == "semver" else pep440(version)
        p = repo / rel
        text = p.read_text()
        new, _ = fn(text, want)
        if new != text:
            p.write_text(new)
            changed.append(rel)
    return changed


def check_files(repo: Path, version: str) -> list[str]:
    problems = []
    for rel, fn, kind in edits(repo):
        want = version if kind == "semver" else pep440(version)
        p = repo / rel
        if not p.is_file():
            problems.append(f"{rel}: missing")
            continue
        try:
            _, found = fn(p.read_text(), None)
        except StampError as e:
            problems.append(str(e))
            continue
        bad = sorted({v for v in found if v != want})
        if bad:
            problems.append(f"{rel}: {', '.join(bad)}, not {want}")
    return problems


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def check_tools(repo: Path, version: str) -> list[str]:
    problems = []
    r = _run(
        ["cargo", "metadata", "--no-deps", "--format-version", "1", "--locked", "--offline"],
        repo / "edge",
    )
    if r.returncode != 0:
        problems.append(f"cargo metadata --locked: {r.stderr.strip()[-400:]}")
    else:
        meta = json.loads(r.stdout)
        ws = set(meta["workspace_members"])
        for pkg in meta["packages"]:
            if pkg["id"] in ws and pkg["version"] != version:
                problems.append(f"cargo: {pkg['name']} resolves to {pkg['version']}, not {version}")
    for d, mod in PY_MODULES.items():
        r = _run(
            [pinned.uv(), "run", "--locked", "--quiet", "python", "-c", f"import {mod}; print({mod}.__version__)"],
            repo / d,
        )
        got = r.stdout.strip()
        if r.returncode != 0:
            problems.append(f"{d}: uv run --locked failed: {r.stderr.strip()[-400:]}")
        elif got != pep440(version):
            problems.append(f"{d}: the installed {mod}.__version__ is {got}, not {pep440(version)}")
    return problems


def check_edge_bin(repo: Path, version: str, edge_bin: str) -> list[str]:
    """The built daemon reports the version: CARGO_PKG_VERSION, the same string its heartbeat sends."""
    r = _run([edge_bin, "--version"], repo)
    if r.stdout.strip() != f"omnifile-edge {version}":
        return [f"{edge_bin} --version says {r.stdout.strip()!r}, not 'omnifile-edge {version}'"]
    return []


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--version", help="write this version into VERSION first")
    ap.add_argument("--check", action="store_true", help="only check that everything says VERSION")
    ap.add_argument("--no-tools", action="store_true", help="--check without cargo and uv (files only)")
    ap.add_argument("--edge-bin", help="--check also runs this omnifile-edge --version")
    ap.add_argument("--repo", type=Path, default=REPO, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    repo = args.repo.resolve()
    try:
        if args.check:
            if args.version:
                ap.error("--check reads VERSION; it takes no --version")
            v = read_version(repo)
            problems = check_files(repo, v)
            if not args.no_tools and not problems:
                problems += check_tools(repo, v)
            if args.edge_bin and not problems:
                problems += check_edge_bin(repo, v, args.edge_bin)
            for p in problems:
                print(f"stamp: {p}", file=sys.stderr)
            if problems:
                print(f"stamp: NOT stamped: {len(problems)} place(s) disagree with VERSION {v}", file=sys.stderr)
                return 1
            print(f"stamp: every manifest and lockfile says {v}" + ("" if args.no_tools else ", and so do cargo and uv"))
            return 0
        if args.version:
            v = parse_version(args.version)
            (repo / "VERSION").write_text(v + "\n")
        v = read_version(repo)
        changed = stamp(repo, v)
        print(f"stamp: {v} (Python {pep440(v)}); changed: {', '.join(changed) or 'nothing'}")
        return 0
    except StampError as e:
        print(f"stamp: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
