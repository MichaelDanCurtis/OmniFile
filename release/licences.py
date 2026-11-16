#!/usr/bin/env python3
"""Check the release's SBOMs against LICENSES.md, both ways, and against the licence rules.

    release/licences.py --sboms DIR [--licenses LICENSES.md]

LICENSES.md's tables are machine-readable: a `<!-- licences: ECOSYSTEM -->` comment starts a section (cargo, pypi,
npm, oci, deb, generic or tool), and in each row the first cell names one or more packages in backticks, the
"Version" cell is a version prefix (or "-"), and the "Licence" cell is one SPDX expression. It fails when:

  1. a direct dependency in an SBOM has no row (each SBOM marks them omnifile:direct=true);
  2. a row names a package that no SBOM has (so the file lists nothing that isn't in the release);
  3. a direct dependency's own licence (its metadata, in the SBOM) is not the row's, or its version doesn't start
     with the row's;
  4. any licence breaks the rules below, in a row or on any component of any SBOM, transitive ones included;
  5. a shipped component (scope required or optional) has no licence at all: its metadata gives none and
     release/licence-overrides.toml doesn't name one with a reason (an appliance SBOM made without the source
     offer has no Debian licences, so it fails here).

The rules (AGENTS.md "License hygiene", decisions 36, 46 and 86):
  - never AGPL, SSPL, a non-commercial or source-available licence (BUSL, Elastic, Commons Clause);
  - code linked into or shipped with our software (cargo, pypi, npm, generic; scope required or optional) is
    permissive; MPL-2.0 (file-level, used unmodified) and, for Python only, LGPL (imported, not linked) are allowed
    when LICENSES.md names the package, transitive or not; GPL only for the separate-process components AGENTS.md
    names (Samba, winbind, SSSD, lsyncd, fswatch) and our own Samba module (decision 36);
  - dev, test and build-only components (scope excluded) and tools may be any licence above but AGPL and the like;
  - the appliance's Ubuntu packages (deb) and the images' base images (oci) may carry any licence but those in the
    first rule: they are operating systems, distributed as an aggregate whose sources the release publishes
    (decision 46);
  - elsewhere, a licence nobody recognises fails.
Standard library only.
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ECOSYSTEMS = ("cargo", "pypi", "npm", "oci", "deb", "generic", "tool")
SEPARATE_PROCESS_GPL = {"samba", "winbind", "sssd", "lsyncd", "fswatch", "vfs_omnifile_prevver"}
PERMISSIVE = {
    "0BSD", "Apache-2.0", "Artistic-2.0", "BlueOak-1.0.0", "BSD-1-Clause", "BSD-2-Clause", "BSD-2-Clause-Patent",
    "BSD-3-Clause", "BSD-3-Clause-Clear", "BSL-1.0", "CC-BY-3.0", "CC-BY-4.0", "CC0-1.0", "CDLA-Permissive-2.0",
    "curl", "ISC", "MIT", "MIT-0", "MIT-CMU", "NCSA", "OFL-1.1", "OpenSSL", "PostgreSQL", "PSF-2.0", "Python-2.0",
    "Unicode-3.0", "Unicode-DFS-2016", "Unlicense", "W3C", "X11", "Zlib", "AFL-2.1", "SSH-OpenSSH",
    "LicenseRef-public-domain",
}
WEAK = re.compile(r"^(LGPL-[0-9.]+(-only|-or-later|\+)?|MPL-[0-9.]+|EPL-2\.0|CDDL-1\.[01])$")
STRONG = re.compile(r"^GPL-[0-9.]+(-only|-or-later|\+)?$")
NEVER = re.compile(r"^(AGPL-.*|SSPL-.*|CC-BY-NC.*|Commons-Clause|BUSL-.*|Elastic-2\.0)$", re.I)
EXCEPTIONS = {"LLVM-exception", "Classpath-exception-2.0", "GCC-exception-3.1", "Autoconf-exception-3.0",
              "Bison-exception-2.2", "OpenSSL-exception", "Linux-syscall-note"}


# --- SPDX expressions ------------------------------------------------------------------------------------------


class SpdxError(ValueError):
    pass


def parse_spdx(expr: str):
    """('or'|'and', [children]) / ('with', id, exception) / id. Cargo's old 'MIT/Apache-2.0' is OR."""
    s = expr.strip().replace("/", " OR ")
    toks = re.findall(r"\(|\)|[A-Za-z0-9.+:-]+", s)
    if "".join(toks).replace(" ", "") != re.sub(r"\s+", "", s):
        raise SpdxError(f"not an SPDX expression: {expr!r}")
    pos = 0

    def peek():
        return toks[pos] if pos < len(toks) else None

    def take():
        nonlocal pos
        pos += 1
        return toks[pos - 1]

    def atom():
        t = take() if peek() else None
        if t is None:
            raise SpdxError(f"unfinished SPDX expression {expr!r}")
        if t == "(":
            v = disj()
            if take() != ")":
                raise SpdxError(f"unbalanced {expr!r}")
            return v
        if t.upper() in ("AND", "OR", "WITH", ")"):
            raise SpdxError(f"unexpected {t} in {expr!r}")
        if peek() and peek().upper() == "WITH":
            take()
            return ("with", t, take())
        return t

    def conj():
        parts = [atom()]
        while peek() and peek().upper() == "AND":
            take()
            parts.append(atom())
        return parts[0] if len(parts) == 1 else ("and", parts)

    def disj():
        parts = [conj()]
        while peek() and peek().upper() == "OR":
            take()
            parts.append(conj())
        return parts[0] if len(parts) == 1 else ("or", parts)

    v = disj()
    if pos != len(toks):
        raise SpdxError(f"trailing tokens in {expr!r}")
    return v


def canonical(node) -> str:
    if isinstance(node, str):
        return node
    if node[0] == "with":
        return f"{node[1]} WITH {node[2]}"
    op = node[0].upper()
    flat = []
    for c in node[1]:
        if isinstance(c, tuple) and c[0] == node[0]:
            flat.extend(c[1])
        else:
            flat.append(c)
    parts = sorted({canonical(c) if not (isinstance(c, tuple) and c[0] in ("and", "or")) else f"({canonical(c)})"
                    for c in flat})
    return f" {op} ".join(parts)


def leaves(node):
    if isinstance(node, str):
        yield node
    elif node[0] == "with":
        yield node[1]
    else:
        for c in node[1]:
            yield from leaves(c)


def allowed(node, ok) -> bool:
    """OR: any alternative; AND: all; WITH: the licence (an exception only grants more)."""
    if isinstance(node, str):
        return ok(node)
    if node[0] == "with":
        return ok(node[1]) and node[2] in EXCEPTIONS
    if node[0] == "or":
        return any(allowed(c, ok) for c in node[1])
    return all(allowed(c, ok) for c in node[1])


def rule(eco: str, scope: str, name: str, named_in_file: bool):
    """The licence test for one component: returns ok(id) -> bool."""

    def ok(lid: str) -> bool:
        if NEVER.match(lid) or "agpl" in lid.lower() or "sspl" in lid.lower():
            return False
        if lid in PERMISSIVE:
            return True
        if eco in ("deb", "oci"):
            # An operating system's packages, distributed as an aggregate with their source (decision 46): any
            # licence but those above (BSD-4-Clause, Artistic, Sleepycat and the GPL family are all fine here).
            return True
        if eco == "tool" or scope == "excluded":
            return bool(WEAK.match(lid) or STRONG.match(lid))
        if WEAK.match(lid):
            if lid.startswith("MPL"):
                return named_in_file
            if lid.startswith("LGPL"):
                return named_in_file and eco in ("pypi", "generic")
            return False
        if STRONG.match(lid):
            return name.lower() in SEPARATE_PROCESS_GPL
        return False

    return ok


# --- LICENSES.md -----------------------------------------------------------------------------------------------


@dataclass
class Row:
    line: int
    eco: str
    names: list[str]
    version: str
    licence: str
    tree: object = None


def read_licenses(path: Path) -> list[Row]:
    rows: list[Row] = []
    eco = None
    header: list[str] | None = None
    for i, raw in enumerate(path.read_text().splitlines(), 1):
        m = re.match(r"^\s*<!--\s*licences:\s*(\S+)\s*-->\s*$", raw)
        if m:
            eco = m.group(1)
            if eco not in ECOSYSTEMS:
                raise SystemExit(f"{path}:{i}: unknown ecosystem {eco} ({', '.join(ECOSYSTEMS)})")
            header = None
            continue
        if raw.startswith("#"):
            eco, header = None, None
            continue
        if eco is None or not raw.startswith("|"):
            if not raw.strip():
                header = None
            continue
        cells = [c.strip() for c in raw.strip().strip("|").split("|")]
        if header is None:
            header = [c.lower() for c in cells]
            for need in ("package", "version", "licence"):
                if need not in header:
                    raise SystemExit(f"{path}:{i}: a licences table needs a {need!r} column")
            continue
        if set(raw.replace("|", "").strip()) <= set("-: "):
            continue
        row = dict(zip(header, cells))
        names = re.findall(r"`([^`]+)`", row["package"])
        if not names:
            raise SystemExit(f"{path}:{i}: no `package` name in the first cell")
        rows.append(Row(i, eco, names, row["version"].strip(), row["licence"].strip()))
    return rows


# --- the SBOMs -------------------------------------------------------------------------------------------------


@dataclass
class Comp:
    sbom: str
    eco: str
    name: str
    version: str
    scope: str
    licence: str | None
    acknowledgement: str
    direct: bool
    own: bool
    props: dict = field(default_factory=dict)


def eco_of(purl: str, tools: bool) -> str:
    if tools:
        return "tool"
    t = purl.split(":", 1)[1].split("/", 1)[0] if purl.startswith("pkg:") else "generic"
    return t if t in ("cargo", "pypi", "npm", "oci", "deb") else "generic"


def read_sboms(d: Path) -> list[Comp]:
    out = []
    files = sorted(glob.glob(str(d / "*.cdx.json")))
    if not files:
        raise SystemExit(f"no *.cdx.json in {d}")
    for f in files:
        doc = json.loads(Path(f).read_text())
        tools = doc["metadata"]["component"]["name"] == "omnifile-release-tools"
        stack = list(doc.get("components", []))
        while stack:
            c = stack.pop()
            stack.extend(c.get("components", []))
            props = {p["name"]: p["value"] for p in c.get("properties", [])}
            lic = (c.get("licenses") or [{}])[0]
            out.append(Comp(Path(f).name, eco_of(c.get("purl", ""), tools), c["name"], c.get("version", ""),
                            c.get("scope", "required"), lic.get("expression"), lic.get("acknowledgement", ""),
                            props.get("omnifile:direct") == "true", props.get("omnifile:own") == "true", props))
    return out


# --- the check -------------------------------------------------------------------------------------------------


def version_ok(prefix: str, version: str) -> bool:
    if prefix in ("", "-", "—"):
        return True
    return version == prefix or version.startswith(prefix + ".") or version.startswith(prefix + "-") \
        or version.startswith(prefix + "+") or version.startswith(prefix + "~")


UNCHECKED: list[str] = []  # excluded-scope components with no licence: not shipped, so reported, not failed


def check(rows: list[Row], comps: list[Comp]) -> list[str]:
    problems: list[str] = []
    UNCHECKED.clear()
    by_key: dict[tuple[str, str], Row] = {}
    for r in rows:
        try:
            r.tree = parse_spdx(r.licence)
        except SpdxError as e:
            problems.append(f"LICENSES.md:{r.line}: {e}")
            continue
        for n in r.names:
            k = (r.eco, n.lower())
            if k in by_key:
                problems.append(f"LICENSES.md:{r.line}: {n} ({r.eco}) is also on line {by_key[k].line}")
            by_key[k] = r
    present = {(c.eco, c.name.lower()) for c in comps}
    # 2. Every row's packages are in the release.
    for r in rows:
        for n in r.names:
            if (r.eco, n.lower()) not in present:
                problems.append(f"LICENSES.md:{r.line}: {n} ({r.eco}) is in no SBOM: remove the row, or it is missing "
                                "from the release")
    # 1, 3. Every direct dependency has a row, with its own licence and version.
    for c in comps:
        if not c.direct or c.own:
            continue
        r = by_key.get((c.eco, c.name.lower()))
        if r is None:
            problems.append(f"{c.sbom}: {c.name} {c.version} ({c.eco}, {c.scope}) is a direct dependency with no row "
                            "in LICENSES.md")
            continue
        if r.tree is None:
            continue
        # A Debian package's licence in the SBOM is every licence its copyright file names, so it is not
        # compared with the row's main one; the rules below still apply to it.
        if c.licence and c.eco != "deb":
            try:
                mine = canonical(parse_spdx(c.licence))
            except SpdxError as e:
                problems.append(f"{c.sbom}: {c.name}: {e}")
                mine = None
            if mine is not None and mine != canonical(r.tree):
                problems.append(f"LICENSES.md:{r.line}: {c.name} {c.version} is {c.licence} by its own metadata "
                                f"({c.sbom}), not {r.licence}")
        if not version_ok(r.version, c.version):
            problems.append(f"LICENSES.md:{r.line}: {c.name} is {c.version} in {c.sbom}, not {r.version}")
    # 4. The rules, for every row and every component.
    for r in rows:
        if r.tree is None:
            continue
        for n in r.names:
            scopes_ = {c.scope for c in comps if (c.eco, c.name.lower()) == (r.eco, n.lower())} or {"required"}
            for s in scopes_:
                if not allowed(r.tree, rule(r.eco, s, n, True)):
                    problems.append(f"LICENSES.md:{r.line}: {n} ({r.eco}, {s}) is {r.licence}, which the licence "
                                    "rules do not allow")
    seen = set()
    unlicensed = set()
    for c in comps:
        if c.own:
            continue
        if not c.licence:
            # Nothing to check the rules against: a shipped component needs a licence, from its own metadata or
            # from release/licence-overrides.toml (licence and reason). A test- or build-only one (scope excluded)
            # is not shipped, so it is counted and named in the summary, not failed.
            if (c.eco, c.name, c.version) in unlicensed:
                continue
            unlicensed.add((c.eco, c.name, c.version))
            if c.scope in ("required", "optional"):
                problems.append(f"{c.sbom}: {c.name} {c.version} ({c.eco}, {c.scope}) has no licence: its "
                                "metadata gives none; add it to release/licence-overrides.toml with the reason")
            else:
                UNCHECKED.append(f"{c.name} {c.version} ({c.eco}, {c.sbom})")
            continue
        key = (c.eco, c.name, c.version, c.scope, c.licence)
        if key in seen:
            continue
        seen.add(key)
        try:
            tree = parse_spdx(c.licence)
        except SpdxError as e:
            problems.append(f"{c.sbom}: {c.name} {c.version}: {e}")
            continue
        named = (c.eco, c.name.lower()) in by_key
        if not allowed(tree, rule(c.eco, c.scope, c.name, named)):
            bad = sorted({lid for lid in leaves(tree) if not rule(c.eco, c.scope, c.name, named)(lid)})
            problems.append(f"{c.sbom}: {c.name} {c.version} ({c.eco}, {c.scope}{', direct' if c.direct else ''}) is "
                            f"{c.licence}: {', '.join(bad)} not allowed here"
                            + ("" if named else " (copyleft must at least be named in LICENSES.md)"))
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sboms", type=Path, required=True, help="the directory of *.cdx.json (release/sbom.py --out)")
    ap.add_argument("--licenses", type=Path, default=REPO / "LICENSES.md")
    args = ap.parse_args(argv)
    rows = read_licenses(args.licenses)
    comps = read_sboms(args.sboms)
    problems = check(rows, comps)
    for p in problems:
        print(f"licences: {p}", file=sys.stderr)
    direct = {(c.eco, c.name.lower()) for c in comps if c.direct and not c.own}
    print(f"licences: {len(rows)} rows naming {sum(len(r.names) for r in rows)} packages; {len(direct)} direct "
          f"dependencies and {len(comps)} components in {len({c.sbom for c in comps})} SBOMs; "
          f"{len(problems)} problem(s)")
    if UNCHECKED:
        print(f"licences: {len(UNCHECKED)} test/build-only component(s) have no licence and were not checked "
              f"(not shipped): {'; '.join(sorted(UNCHECKED)[:10])}{' ...' if len(UNCHECKED) > 10 else ''}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
