#!/usr/bin/env python3
"""CycloneDX 1.6 JSON SBOMs for one release, generated from the lockfiles themselves.

    release/sbom.py --out DIR [--version V] [--images-lock FILE] [--appliance-manifest FILE]
                    [--appliance-build-info FILE] [--source-offer DIR] [--rust-target T]
    release/sbom.py --list-lockfiles

One SBOM per input, each named omnifile-<name>-<version>.cdx.json:

  every lockfile git tracks     edge/Cargo.lock (cargo metadata --locked, for --rust-target, default
                                x86_64-unknown-linux-gnu: what the appliance runs), spikes/filer/guard/Cargo.lock,
                                service/uv.lock, relay/uv.lock, tests/real/uv.lock, portal/package-lock.json,
                                portal/e2e/package-lock.json, ... A new lockfile gets an SBOM without a change here.
  images                        deploy/images/images.lock.json (deploy/images/build.sh): each image by digest, its
                                base image (the Containerfile's FROM, pinned by digest) and the locked packages it
                                installs (uv sync --no-dev; the portal's production packages in web)
  appliance                     mkosi's package manifest (omnifile-edge_V.manifest) and BUILD-INFO, plus what is not
                                an Ubuntu package: our binaries, NFS-Ganesha and libntirpc at their pinned commits,
                                FSAL_OMNI, vfs_omnifile_prevver and the pinned aws_signing_helper. With --source-offer,
                                each package's licence is read from its source's debian/copyright (DEP-5).
  tools                         release/tools.toml: the pinned tools that build, test and release it

Every component carries its scope: required (shipped), optional (an extra nothing installs by default) or
excluded (test, build and dev only), and the property omnifile:direct=true when the project names it itself.
Licences come from the package metadata: Cargo's, npm's lockfile, the installed Python distributions' METADATA
(License-Expression, else an SPDX License field, else release/licence-overrides.toml, marked "concluded"). The
dependency graph is in `dependencies`. Output is deterministic: the timestamp is SOURCE_DATE_EPOCH, else the
HEAD commit's time, and the serial number is derived from the content.

release/validate_sbom.py checks each against the CycloneDX 1.6 schema; release/licences.py checks them against
LICENSES.md. Standard library only (Python 3.11+), plus cargo and git.
"""

from __future__ import annotations

import argparse
import base64
import email.parser
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

REPO = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
LOCK_PATTERNS = ("Cargo.lock", "uv.lock", "package-lock.json")
SPEC = "1.6"
SUPPLIER = "AiLuxis Research"
OWN_LICENCE = "Apache-2.0"
# Python markers are evaluated for the images' interpreter: CPython 3.12 on Linux x86-64 (python:3.12-slim).
PY_ENV = {
    "sys_platform": "linux",
    "platform_system": "Linux",
    "os_name": "posix",
    "platform_machine": "x86_64",
    "implementation_name": "cpython",
    "platform_python_implementation": "CPython",
    "python_version": "3.12",
    "python_full_version": "3.12.13",
}
# The separate-process GPL components AGENTS.md names; licences.py allows GPL for these only, outside the image.
NS = uuid.UUID("6f6d6e69-6669-6c65-2d73-626f6d2d7631")


# --- small helpers ------------------------------------------------------------------------------------------


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(REPO), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def timestamp() -> str:
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if not epoch:
        try:
            epoch = git("log", "-1", "--format=%ct", "HEAD")
        except (subprocess.CalledProcessError, FileNotFoundError):
            epoch = "0"
    return datetime.fromtimestamp(int(epoch), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def tracked_lockfiles(repo: Path = REPO) -> list[Path]:
    out = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "-z"], check=True, capture_output=True
    ).stdout.decode()
    return sorted(
        repo / p for p in out.split("\0") if p and Path(p).name in LOCK_PATTERNS and (repo / p).is_file()
    )


def sbom_name(lock: Path, repo: Path = REPO) -> str:
    rel = lock.parent.relative_to(repo)
    return "-".join(rel.parts) or "root"


def prop(name: str, value) -> dict:
    return {"name": f"omnifile:{name}", "value": str(value)}


def licence_entry(expr: str | None, acknowledgement: str = "declared") -> list[dict]:
    if not expr:
        return []
    return [{"expression": expr, "acknowledgement": acknowledgement}]


def purl_pypi(name: str, version: str) -> str:
    return f"pkg:pypi/{re.sub(r'[-_.]+', '-', name).lower()}@{quote(version)}"


def purl_npm(name: str, version: str) -> str:
    if name.startswith("@"):
        scope, _, n = name.partition("/")
        return f"pkg:npm/{quote(scope)}/{quote(n)}@{quote(version)}"
    return f"pkg:npm/{quote(name)}@{quote(version)}"


def purl_cargo(name: str, version: str) -> str:
    return f"pkg:cargo/{name}@{quote(version)}"


def bom(name: str, version: str, root: dict, components: list[dict], deps: dict[str, set[str]],
        props: list[dict]) -> dict:
    """A CycloneDX 1.6 document; the serial number is derived from everything else."""
    seen: set[str] = set()
    for c in components:
        if c["bom-ref"] in seen:
            raise SystemExit(f"{name}: duplicate bom-ref {c['bom-ref']}")
        seen.add(c["bom-ref"])
    doc = {
        "bomFormat": "CycloneDX",
        "specVersion": SPEC,
        "version": 1,
        "metadata": {
            "timestamp": timestamp(),
            "lifecycles": [{"phase": "build"}],
            "tools": {
                "components": [
                    {"type": "application", "name": "omnifile release/sbom.py", "version": version}
                ]
            },
            "supplier": {"name": SUPPLIER},
            "component": root,
            "properties": props,
        },
        "components": components,
        "dependencies": [
            {"ref": ref, "dependsOn": sorted(d)} for ref, d in sorted(deps.items())
        ],
    }
    digest = hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()
    doc = {"bomFormat": "CycloneDX", "specVersion": SPEC,
           "serialNumber": f"urn:uuid:{uuid.uuid5(NS, digest)}", **{k: v for k, v in doc.items() if k not in ("bomFormat", "specVersion")}}
    return doc


def root_component(kind: str, name: str, version: str, ref: str, licence: str = OWN_LICENCE) -> dict:
    return {
        "type": kind,
        "bom-ref": ref,
        "name": name,
        "version": version,
        "supplier": {"name": SUPPLIER},
        "licenses": licence_entry(licence),
    }


def scopes(roots: dict[str, set[str]], edges: dict[str, set[str]]) -> dict[str, str]:
    """Each node's scope: reachable from a required root, else an optional one, else excluded."""
    scope: dict[str, str] = {}
    for level in ("required", "optional", "excluded"):
        todo = [r for r in roots.get(level, ()) if r not in scope]
        while todo:
            n = todo.pop()
            if n in scope:
                continue
            scope[n] = level
            todo.extend(d for d in edges.get(n, ()) if d not in scope)
    return scope


# --- Rust: cargo metadata over the lockfile -------------------------------------------------------------------


def cargo_sbom(lock: Path, version: str, target: str, repo: Path = REPO) -> dict:
    manifest = lock.parent / "Cargo.toml"
    r = subprocess.run(
        ["cargo", "metadata", "--format-version", "1", "--locked", "--manifest-path", str(manifest),
         "--filter-platform", target] + (["--offline"] if os.environ.get("OMNIFILE_OFFLINE") == "1" else []),
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise SystemExit(f"cargo metadata for {lock}: {r.stderr.strip()[-600:]}")
    meta = json.loads(r.stdout)
    return cargo_from_metadata(meta, lock, version, target, repo)


def cargo_from_metadata(meta: dict, lock: Path, version: str, target: str, repo: Path = REPO) -> dict:
    pkgs = {p["id"]: p for p in meta["packages"]}
    members = set(meta["workspace_members"])
    checksums = {}
    for p in tomllib.loads(lock.read_text()).get("package", []):
        if p.get("checksum"):
            checksums[(p["name"], p["version"])] = p["checksum"]
    ref = {pid: (f"omnifile:{p['name']}" if pid in members else purl_cargo(p["name"], p["version"]))
           for pid, p in pkgs.items()}
    edges: dict[str, set[str]] = {}  # the graph: every edge Cargo builds, members' dev edges too
    run_edges: dict[str, set[str]] = {}  # what a release binary is built from: normal and build edges
    roots: dict[str, set[str]] = {"required": set(), "excluded": set()}
    direct: dict[str, set[str]] = {}
    for node in meta["resolve"]["nodes"]:
        for d in node["deps"]:
            kinds = {k["kind"] or "normal" for k in d["dep_kinds"]}
            src, dst = node["id"], d["pkg"]
            runtime = bool(kinds & {"normal", "build"})
            if src in members:
                if dst not in members:
                    direct.setdefault(dst, set()).update(kinds)
                roots["required" if runtime else "excluded"].add(ref[dst])
            if runtime:
                run_edges.setdefault(ref[src], set()).add(ref[dst])
            if runtime or src in members:
                edges.setdefault(ref[src], set()).add(ref[dst])
    # Scope: required if a member's normal or build dependencies reach it; excluded if only dev ones do.
    scope = scopes({"required": roots["required"]}, run_edges)
    for r_, lvl in scopes({"excluded": roots["excluded"]}, edges).items():
        scope.setdefault(r_, lvl)
    for m in members:
        scope[ref[m]] = "required"
    components = []
    for pid, p in sorted(pkgs.items(), key=lambda kv: (kv[1]["name"], kv[1]["version"])):
        r_ = ref[pid]
        if r_ not in scope:
            continue  # not built for this target
        own = pid in members
        c = {
            "type": "library",
            "bom-ref": r_,
            "name": p["name"],
            "version": p["version"],
            "scope": scope[r_],
            "licenses": licence_entry(p.get("license")),
        }
        if not own:
            c["purl"] = purl_cargo(p["name"], p["version"])
            if (p["name"], p["version"]) in checksums:
                c["hashes"] = [{"alg": "SHA-256", "content": checksums[(p["name"], p["version"])]}]
        props = []
        if own:
            props.append(prop("own", "true"))
        if pid in direct:
            props.append(prop("direct", "true"))
            props.append(prop("dependency-kind", ",".join(sorted(direct[pid]))))
        if props:
            c["properties"] = props
        components.append(c)
    name = sbom_name(lock, repo)
    root_ref = f"omnifile:{name}"
    deps = {root_ref: {ref[m] for m in members}}
    for src, dsts in edges.items():
        if src in scope:
            deps[src] = {d for d in dsts if d in scope}
    for c in components:
        deps.setdefault(c["bom-ref"], set())
    return bom(name, version, root_component("application", f"omnifile-{name}", version, root_ref), components,
               deps, [prop("source", lock.relative_to(repo)), prop("source-sha256", sha256_file(lock)),
                      prop("rust-target", target)])


# --- Python: uv.lock -------------------------------------------------------------------------------------------


def _marker_tokens(s: str):
    return re.findall(r"\(|\)|'[^']*'|\"[^\"]*\"|[A-Za-z_][A-Za-z0-9_.]*|===|==|!=|<=|>=|~=|<|>|not in|in", s)


def _ver(v: str):
    return tuple(int(x) if x.isdigit() else x for x in re.split(r"[.]", v))


def marker_true(marker: str, env: dict[str, str]) -> bool:
    """A PEP 508 marker, as uv writes them: comparisons joined by and/or, with parentheses."""
    toks = _marker_tokens(marker)
    pos = 0

    def peek():
        return toks[pos] if pos < len(toks) else None

    def take():
        nonlocal pos
        pos += 1
        return toks[pos - 1]

    def value(t):
        if t[:1] in "'\"":
            return t[1:-1]
        if t not in env:
            raise ValueError(f"unknown marker variable {t} in {marker!r}")
        return env[t]

    def comparison():
        if peek() == "(":
            take()
            v = disjunction()
            if take() != ")":
                raise ValueError(marker)
            return v
        left_tok, op, right_tok = take(), take(), take()
        left, right = value(left_tok), value(right_tok)
        version_var = any(t in ("python_version", "python_full_version") for t in (left_tok, right_tok))
        if op in ("in", "not in"):
            return (left in right) == (op == "in")
        if version_var:
            a, b = _ver(left), _ver(right)
        else:
            a, b = left, right
        return {"==": a == b, "!=": a != b, "<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b,
                "===": left == right, "~=": a >= b}[op]

    def conjunction():
        v = comparison()
        while peek() == "and":
            take()
            v = comparison() and v
        return v

    def disjunction():
        v = conjunction()
        while peek() == "or":
            take()
            v = conjunction() or v
        return v

    result = disjunction()
    if pos != len(toks):
        raise ValueError(f"cannot read marker {marker!r}")
    return result


SPDX_FREE_TEXT = {
    "mit": "MIT", "mit license": "MIT", "bsd-3-clause": "BSD-3-Clause", "3-clause bsd license": "BSD-3-Clause",
    "bsd 3-clause": "BSD-3-Clause", "apache 2.0": "Apache-2.0", "apache-2.0": "Apache-2.0",
    "apache license 2.0": "Apache-2.0", "apache software license": "Apache-2.0", "isc": "ISC",
    "psf-2.0": "PSF-2.0", "mpl-2.0": "MPL-2.0", "bsd-2-clause": "BSD-2-Clause",
}


def python_licences(venv: Path | None, overrides: dict[str, str]) -> dict[str, tuple[str, str]]:
    """Normalised distribution name -> (SPDX expression, acknowledgement), from an installed environment."""
    out: dict[str, tuple[str, str]] = {}
    if venv and venv.is_dir():
        for meta in glob.glob(str(venv / "lib" / "python3*" / "site-packages" / "*.dist-info" / "METADATA")):
            m = email.parser.Parser().parsestr(Path(meta).read_text(encoding="utf-8", errors="replace"),
                                               headersonly=True)
            name = re.sub(r"[-_.]+", "-", m["Name"]).lower()
            expr = (m.get("License-Expression") or "").strip()
            if not expr:
                free = (m.get("License") or "").strip()
                expr = SPDX_FREE_TEXT.get(free.lower(), "") if "\n" not in free else ""
            if not expr:
                # One unambiguous licence classifier (License :: OSI Approved :: MIT License).
                cls = {c.split("::")[-1].strip().lower() for c in m.get_all("Classifier") or []
                       if c.startswith("License ::")}
                mapped = {SPDX_FREE_TEXT.get(c) for c in cls}
                if len(cls) == 1 and None not in mapped:
                    expr = mapped.pop()
            if expr:
                out[name] = (expr, "declared")
    for name, o in overrides.items():
        key = re.sub(r"[-_.]+", "-", name).lower()
        if key not in out:
            out[key] = (o["licence"], "concluded")
    return out


def load_overrides(path: Path | None = None) -> dict[str, dict[str, dict[str, str]]]:
    """release/licence-overrides.toml: {ecosystem: {name: {"licence", "reason"}}}; both fields are required."""
    f = path or HERE / "licence-overrides.toml"
    data = tomllib.loads(f.read_text()) if f.is_file() else {}
    for eco, entries in data.items():
        for name, o in entries.items():
            if not isinstance(o, dict) or not o.get("licence") or not o.get("reason"):
                raise SystemExit(f"{f.name}: [{eco}] {name} needs both a licence and a reason")
            if eco == "deb" and not o.get("versions"):
                raise SystemExit(f"{f.name}: [deb] {name} needs the source versions it was read at (versions)")
    return data


def python_env(project: Path) -> tempfile.TemporaryDirectory:
    """A throwaway environment with every extra and group of the project's lock, for the packages' own licence
    metadata (the project's .venv may lack extras). uv works from its cache; OMNIFILE_OFFLINE=1 forbids the network."""
    tmp = tempfile.TemporaryDirectory(prefix="omnifile-sbom-")
    import pinned

    cmd = [pinned.uv(), "sync", "--locked", "--all-extras", "--all-groups", "--no-install-project", "--quiet"]
    if os.environ.get("OMNIFILE_OFFLINE") == "1":
        cmd.append("--offline")
    r = subprocess.run(cmd, cwd=project, capture_output=True, text=True,
                       env={**os.environ, "UV_PROJECT_ENVIRONMENT": str(Path(tmp.name) / "venv")})
    if r.returncode != 0:
        tmp.cleanup()
        raise SystemExit(f"sbom: uv sync for {project} (licences): {r.stderr.strip()[-600:]}")
    return tmp


def uv_sbom(lock: Path, version: str, repo: Path = REPO, venv: Path | None = None) -> dict:
    data = tomllib.loads(lock.read_text())
    pkgs = data.get("package", [])
    by_name: dict[str, list[dict]] = {}
    for p in pkgs:
        by_name.setdefault(p["name"], []).append(p)
    roots_ = [p for p in pkgs if isinstance(p.get("source"), dict)
              and ("editable" in p["source"] or "virtual" in p["source"])
              and p["source"].get("editable", p["source"].get("virtual")) == "."]
    if len(roots_) != 1:
        raise SystemExit(f"{lock}: expected one project entry (source editable or virtual '.'), found {len(roots_)}")
    root = roots_[0]

    def ref(p: dict) -> str:
        if p is root:
            return f"omnifile:{p['name']}"
        return purl_pypi(p["name"], p["version"])

    def resolve(dep: dict) -> dict:
        cands = by_name.get(dep["name"], [])
        if dep.get("version"):
            cands = [c for c in cands if c["version"] == dep["version"]]
        if len(cands) != 1:
            raise SystemExit(f"{lock}: cannot resolve {dep} ({len(cands)} candidates)")
        return cands[0]

    def wanted(dep: dict, extra: str = "") -> bool:
        m = dep.get("marker")
        return True if not m else marker_true(m, {**PY_ENV, "extra": extra})

    edges: dict[str, set[str]] = {}
    seen: set[tuple[str, tuple[str, ...]]] = set()
    nodes: dict[str, dict] = {ref(root): root}

    def walk(p: dict, extras: tuple[str, ...]):
        key = (ref(p), extras)
        if key in seen:
            return
        seen.add(key)
        deps = [(d, "") for d in p.get("dependencies", [])]
        for e in extras:
            deps += [(d, e) for d in p.get("optional-dependencies", {}).get(e, [])]
        for d, e in deps:
            if not wanted(d, e):
                continue
            q = resolve(d)
            nodes[ref(q)] = q
            edges.setdefault(ref(p), set()).add(ref(q))
            walk(q, tuple(sorted(d.get("extra", []))))

    roots: dict[str, set[str]] = {"required": set(), "optional": set(), "excluded": set()}
    direct: dict[str, set[str]] = {}

    def add_root(level: str, d: dict, kind: str, extra: str = ""):
        if not wanted(d, extra):
            return
        q = resolve(d)
        nodes[ref(q)] = q
        roots[level].add(ref(q))
        direct.setdefault(ref(q), set()).add(kind)
        walk(q, tuple(sorted(d.get("extra", []))))

    for d in root.get("dependencies", []):
        add_root("required", d, "runtime")
    for e, ds in root.get("optional-dependencies", {}).items():
        for d in ds:
            add_root("optional", d, f"extra:{e}", e)
    for g, ds in root.get("dev-dependencies", {}).items():
        for d in ds:
            add_root("excluded", d, f"group:{g}")
    # The project's own dependency edges, for the graph.
    edges.setdefault(ref(root), set()).update(r for lvl in roots.values() for r in lvl)
    scope = scopes(roots, edges)
    scope[ref(root)] = "required"
    overrides = load_overrides().get("pypi", {})
    if venv is None:
        env = python_env(lock.parent)
        try:
            lic = python_licences(Path(env.name) / "venv", overrides)
        finally:
            env.cleanup()
    else:
        lic = python_licences(venv, overrides)
    components = []
    for r_, p in sorted(nodes.items(), key=lambda kv: (kv[1]["name"], kv[1]["version"])):
        if p is root:
            continue
        key = re.sub(r"[-_.]+", "-", p["name"]).lower()
        expr, ack = lic.get(key, (None, ""))
        c = {
            "type": "library",
            "bom-ref": r_,
            "name": p["name"],
            "version": p["version"],
            "purl": purl_pypi(p["name"], p["version"]),
            "scope": scope.get(r_, "excluded"),
            "licenses": licence_entry(expr, ack or "declared"),
        }
        sd = p.get("sdist", {})
        if sd.get("hash", "").startswith("sha256:"):
            c["hashes"] = [{"alg": "SHA-256", "content": sd["hash"][7:]}]
        if r_ in direct:
            c["properties"] = [prop("direct", "true"), prop("dependency-kind", ",".join(sorted(direct[r_])))]
        components.append(c)
    name = sbom_name(lock, repo)
    deps = {r_: set(edges.get(r_, set())) for r_ in nodes}
    deps[f"omnifile:{name}"] = {ref(root)}
    pyproject = lock.parent / "pyproject.toml"
    own_licence = OWN_LICENCE
    if pyproject.is_file():
        declared = tomllib.loads(pyproject.read_text()).get("project", {}).get("license")
        own_licence = declared if isinstance(declared, str) else OWN_LICENCE
    rc = root_component("application", f"omnifile-{name}", version, f"omnifile:{name}")
    components.insert(0, {**root_component("application", root["name"], root["version"], ref(root),
                                           licence=own_licence),
                          "scope": "required", "properties": [prop("own", "true")]})
    return bom(name, version, rc, components, deps,
               [prop("source", lock.relative_to(repo)), prop("source-sha256", sha256_file(lock)),
                prop("python-environment", ",".join(f"{k}={v}" for k, v in sorted(PY_ENV.items())))])


# --- npm: package-lock.json (lockfileVersion 2 or 3) -------------------------------------------------------------


def npm_name(path: str, entry: dict) -> str:
    return entry.get("name") or path.rsplit("node_modules/", 1)[-1]


def npm_sbom(lock: Path, version: str, repo: Path = REPO) -> dict:
    data = json.loads(lock.read_text())
    if data.get("lockfileVersion", 0) < 2:
        raise SystemExit(f"{lock}: lockfileVersion {data.get('lockfileVersion')}; 2 or 3 is needed")
    P = data["packages"]
    root = P[""]

    def ref(path: str) -> str:
        e = P[path]
        return purl_npm(npm_name(path, e), e["version"]) + ("" if path.count("node_modules/") <= 1 else "#" + path)

    def find(from_path: str, dep: str) -> str | None:
        base = from_path
        while True:
            cand = (base + "/" if base else "") + "node_modules/" + dep
            if cand in P:
                return cand
            if not base:
                return None
            i = base.rfind("/node_modules/")
            base = base[:i] if i >= 0 else ""

    edges: dict[str, set[str]] = {}
    for path, e in P.items():
        if not path or e.get("link"):
            continue
        for kind in ("dependencies", "optionalDependencies", "peerDependencies"):
            for dep in e.get(kind, {}):
                t = find(path, dep)
                if t and not P[t].get("link"):
                    edges.setdefault(path, set()).add(t)
    roots: dict[str, set[str]] = {"required": set(), "optional": set(), "excluded": set()}
    direct: dict[str, str] = {}
    for kind, level in (("dependencies", "required"), ("optionalDependencies", "optional"),
                        ("devDependencies", "excluded")):
        for dep in root.get(kind, {}):
            t = find("", dep)
            if t is None:
                raise SystemExit(f"{lock}: {dep} is not in the lockfile")
            roots[level].add(t)
            direct[t] = kind
    components = []
    for path, e in sorted(P.items()):
        if not path or e.get("link"):
            continue
        name = npm_name(path, e)
        # npm's own flags in the lockfile: dev (and devOptional) only for development, optional if optional.
        lvl = ("excluded" if e.get("dev") or e.get("devOptional") else "optional" if e.get("optional")
               else "required")
        c = {
            "type": "library",
            "bom-ref": ref(path),
            "name": name,
            "version": e["version"],
            "purl": purl_npm(name, e["version"]),
            "scope": lvl,
            "licenses": licence_entry(e.get("license") if isinstance(e.get("license"), str) else None),
        }
        integ = e.get("integrity", "")
        if integ.startswith("sha512-"):
            c["hashes"] = [{"alg": "SHA-512", "content": base64.b64decode(integ[7:]).hex()}]
        if e.get("resolved"):
            c["externalReferences"] = [{"type": "distribution", "url": e["resolved"]}]
        props = [prop("lock-path", path)]
        if path in direct:
            props += [prop("direct", "true"), prop("dependency-kind", direct[path])]
        c["properties"] = props
        components.append(c)
    name = sbom_name(lock, repo)
    own = f"omnifile:{root.get('name', name)}"
    deps = {ref(p): {ref(t) for t in ts} for p, ts in edges.items()}
    deps[f"omnifile:{name}"] = {own}
    deps[own] = {ref(t) for lvl in roots.values() for t in lvl}
    for c in components:
        deps.setdefault(c["bom-ref"], set())
    components.insert(0, {**root_component("application", root.get("name", name), root.get("version", ""), own,
                                           licence=root.get("license") or OWN_LICENCE),
                          "scope": "required", "properties": [prop("own", "true")]})
    return bom(name, version, root_component("application", f"omnifile-{name}", version, f"omnifile:{name}"),
               components, deps, [prop("source", lock.relative_to(repo)), prop("source-sha256", sha256_file(lock))])


# --- container images: deploy/images/images.lock.json ------------------------------------------------------------


def containerfile_stages(path: Path) -> list[tuple[str, str]]:
    """(image@digest, stage name) for each FROM line; the last one is the runtime stage."""
    out = []
    for line in path.read_text().splitlines():
        m = re.match(r"^\s*FROM\s+(\S+)(?:\s+AS\s+(\S+))?\s*$", line, re.I)
        if m:
            out.append((m.group(1), m.group(2) or ""))
    if not out:
        raise SystemExit(f"{path}: no FROM line")
    return out


def oci_purl(image: str) -> tuple[str, str, str]:
    """(purl, name, digest) of name[:tag]@sha256:..."""
    m = re.match(r"^(?P<repo>[^@:]+(?::\d+)?(?:/[^@:]+)*)(?::(?P<tag>[^@]+))?@(?P<digest>sha256:[0-9a-f]{64})$", image)
    if not m:
        raise SystemExit(f"base image {image} is not pinned by digest (name[:tag]@sha256:...)")
    repo_ = m.group("repo")
    name = repo_.rsplit("/", 1)[-1]
    q = f"?repository_url={quote(repo_, safe='')}" + (f"&tag={quote(m.group('tag'))}" if m.group("tag") else "")
    return f"pkg:oci/{name}@{quote(m.group('digest'))}{q}", repo_, m.group("digest")


def images_sbom(lock_file: Path, version: str, app_sboms: dict[str, dict], repo: Path = REPO) -> dict:
    lock = json.loads(lock_file.read_text())
    if lock.get("schema") != 1:
        raise SystemExit(f"{lock_file}: schema {lock.get('schema')}, expected 1")
    # What each image installs: the locked runtime packages of these projects.
    contents = {"service": ["service"], "relay": ["relay"], "web": ["service", "portal"]}
    components = []
    deps: dict[str, set[str]] = {}
    root_ref = "omnifile:images"
    deps[root_ref] = set()
    for name, img in sorted(lock["images"].items()):
        digest = img["digest"]
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise SystemExit(f"{lock_file}: {name} has no manifest digest")
        iref = f"image:{name}"
        stages = containerfile_stages(repo / img["containerfile"])
        sub = []
        for i, (base, stage) in enumerate(stages):
            purl, brepo, bdigest = oci_purl(base)
            runtime = i == len(stages) - 1
            o = load_overrides().get("oci", {}).get(brepo)
            sub.append({
                "type": "container",
                "bom-ref": f"{iref}:base:{i}",
                "name": brepo,
                "version": bdigest,
                "purl": purl,
                "hashes": [{"alg": "SHA-256", "content": bdigest.split(":", 1)[1]}],
                "scope": "required" if runtime else "excluded",
                "licenses": licence_entry(o["licence"], "concluded") if o else [],
                "properties": [prop("stage", stage or "runtime"),
                               prop("role", "base image" if runtime else "build stage"), prop("direct", "true")],
            })
        for project in contents.get(name, []):
            s = app_sboms.get(project)
            if s is None:
                raise SystemExit(f"images: no SBOM for {project}, which the {name} image installs")
            for c in s["components"]:
                if c.get("scope") != "required" or any(p["name"] == "omnifile:own" for p in c.get("properties", [])):
                    continue
                sc = {k: v for k, v in c.items() if k not in ("properties",)}
                sc["bom-ref"] = f"{iref}:{project}:{c['bom-ref']}"
                sc["properties"] = [prop("from", project)] + [p for p in c.get("properties", [])
                                                              if p["name"] == "omnifile:direct"]
                sub.append(sc)
        components.append({
            "type": "container",
            "bom-ref": iref,
            "name": img["repository"],
            "version": digest,
            "purl": f"pkg:oci/{img['repository']}@{quote(digest)}",
            "hashes": [{"alg": "SHA-256", "content": digest.split(":", 1)[1]}],
            "supplier": {"name": SUPPLIER},
            "licenses": licence_entry(OWN_LICENCE),
            "scope": "required",
            "components": sub,
            "properties": [prop("own", "true"), prop("containerfile", img["containerfile"]), prop("context", img["context"]),
                           prop("source-commit", img.get("source_commit", "")),
                           prop("platform", lock.get("platform", "")),
                           prop("apps", ",".join(a for a, i in sorted(lock.get("apps", {}).items()) if i == name)),
                           prop("note", "the base image's own OS packages are those of the pinned digest; "
                                "its publisher's SBOM attestation lists them")],
        })
        deps[root_ref].add(iref)
        deps[iref] = {s["bom-ref"] for s in sub}
        for s in sub:
            deps[s["bom-ref"]] = set()
    return bom("images", version, root_component("application", "omnifile-images", version, root_ref),
               components, deps, [prop("source", lock_file.relative_to(repo) if lock_file.is_relative_to(repo)
                                       else lock_file.name), prop("source-sha256", sha256_file(lock_file))])


# --- the appliance: mkosi's manifest -------------------------------------------------------------------------


def mkosi_packages(conf: Path) -> list[str]:
    names, in_list = [], False
    for raw in conf.read_text().splitlines():
        if raw.startswith("Packages="):
            in_list = True
            names += raw.split("=", 1)[1].split()
        elif in_list and raw[:1].isspace() and raw.strip() and not raw.strip().startswith("#"):
            names += raw.split()
        elif in_list and not raw[:1].isspace():
            in_list = False
    return names


def kv_file(path: Path) -> dict[str, str]:
    """KEY=value lines, as sh reads them: a $KEY or ${KEY} of an earlier line is replaced by its value."""
    out: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text().splitlines():
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_.-]*)=(.*)$", line.strip())
            if m:
                out[m.group(1)] = re.sub(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?",
                                         lambda v: out.get(v.group(1), ""), m.group(2).strip().strip('"'))
    return out


DEP5_SPDX = {
    "gpl-2": "GPL-2.0-only", "gpl-2+": "GPL-2.0-or-later", "gpl-2.0": "GPL-2.0-only", "gpl-2.0+": "GPL-2.0-or-later",
    "gpl-2.0-only": "GPL-2.0-only", "gpl-2.0-or-later": "GPL-2.0-or-later", "gpl-3": "GPL-3.0-only",
    "gpl-3+": "GPL-3.0-or-later", "gpl-3.0+": "GPL-3.0-or-later", "gpl-3.0-or-later": "GPL-3.0-or-later",
    "gpl": "GPL-1.0-or-later", "gpl-1+": "GPL-1.0-or-later",
    "lgpl-2": "LGPL-2.0-only", "lgpl-2+": "LGPL-2.0-or-later", "lgpl-2.1": "LGPL-2.1-only",
    "lgpl-2.1+": "LGPL-2.1-or-later", "lgpl-2.1-or-later": "LGPL-2.1-or-later", "lgpl-3": "LGPL-3.0-only",
    "lgpl-3+": "LGPL-3.0-or-later", "lgpl-3.0+": "LGPL-3.0-or-later",
    "agpl-3": "AGPL-3.0-only", "agpl-3+": "AGPL-3.0-or-later", "agpl-3.0+": "AGPL-3.0-or-later",
    "bsd-2-clause": "BSD-2-Clause", "bsd-3-clause": "BSD-3-Clause", "bsd-4-clause": "BSD-4-Clause",
    "mit": "MIT", "expat": "MIT", "isc": "ISC", "zlib": "Zlib", "apache-2.0": "Apache-2.0",
    "mpl-2.0": "MPL-2.0", "mpl-1.1": "MPL-1.1", "public-domain": "LicenseRef-public-domain",
    "cc0-1.0": "CC0-1.0", "cc0": "CC0-1.0", "afl-2.1": "AFL-2.1", "artistic": "Artistic-1.0-Perl",
    "psf-2": "PSF-2.0", "openssl": "OpenSSL", "ssleay": "OpenSSL", "bsl-1.0": "BSL-1.0", "unicode-dfs-2016": "Unicode-DFS-2016",
}


def dep5_licences(copyright_text: str) -> list[str]:
    """The SPDX ids of every `License:` short name in a machine-readable debian/copyright."""
    if not re.search(r"(?im)^Format:\s*https?://(www\.)?debian\.org/doc/packaging-manuals/copyright-format", copyright_text):
        return []
    out = set()
    for m in re.finditer(r"(?m)^License:[ \t]*(\S[^\n]*)$", copyright_text):
        for part in re.split(r"\s+(?:or|and)\s+|\s*[|,]\s*", m.group(1).strip()):
            part = part.strip().strip("()")
            if not part:
                continue
            key = part.lower().split(" with ")[0]
            out.add(DEP5_SPDX.get(key, "LicenseRef-debian-" + re.sub(r"[^A-Za-z0-9.-]+", "-", part).strip("-")))
    return sorted(out)


def debian_copyright(src_dir: Path) -> str:
    """A source package's debian/copyright: from its .debian.tar.*, or, for a native package, its one tarball."""
    tarballs = sorted(src_dir.glob("*.debian.tar.*")) or sorted(
        p for p in src_dir.glob("*.tar.*") if not p.name.endswith((".asc", ".sig")))
    for tarball in tarballs:
        with tarfile.open(tarball) as t:
            for m in t:
                if m.isfile() and re.fullmatch(r"(\./)?([^/]+/)?debian/copyright", m.name):
                    return t.extractfile(m).read().decode("utf-8", "replace")
    return ""


def offer_licences(offer: Path) -> dict[str, list[str]]:
    """Binary package -> SPDX ids, from the source offer's packages.tsv and each source's debian/copyright."""
    tsv = offer / "packages.tsv"
    if not tsv.is_file():
        raise SystemExit(f"{offer}: no packages.tsv (appliance/source-offer/source_offer.py)")
    src_of = {}
    for line in tsv.read_text().splitlines()[1:]:
        b, _bv, s, sv = line.split("\t")
        src_of[b] = (s, sv)
    cache: dict[tuple[str, str], list[str]] = {}
    overrides = load_overrides().get("deb", {})
    for s, sv in set(src_of.values()):
        text = debian_copyright(offer / "ubuntu" / f"{s}_{sv}")
        o = overrides.get(s)
        cache[(s, sv)] = (dep5_licences(text) if text else []) or (
            [o["licence"]] if o and sv in o.get("versions", []) else [])
    return {b: cache[sv] for b, sv in src_of.items()}


def appliance_sbom(manifest: Path, version: str, build_info: Path | None, offer: Path | None,
                   repo: Path = REPO) -> dict:
    m = json.loads(manifest.read_text())
    cfg = m.get("config", {})
    arch = cfg.get("architecture", "")
    direct = set(mkosi_packages(repo / "appliance" / "mkosi.conf"))
    lic = offer_licences(offer) if offer else {}
    components = []
    deps: dict[str, set[str]] = {}
    root_ref = "omnifile:appliance"
    deps[root_ref] = set()
    for p in sorted(m.get("packages", []), key=lambda p: p["name"]):
        if p.get("type", "deb") != "deb":
            continue
        ref = f"pkg:deb/ubuntu/{quote(p['name'])}@{quote(p['version'])}?arch={p.get('architecture', '')}&distro=ubuntu-26.04"
        c = {"type": "library", "bom-ref": ref, "name": p["name"], "version": p["version"], "purl": ref,
             "scope": "required"}
        ids = lic.get(p["name"])
        if ids:
            c["licenses"] = licence_entry(" AND ".join(ids), "concluded")
        props = [prop("architecture", p.get("architecture", ""))]
        if p["name"] in direct:
            props.append(prop("direct", "true"))
            deps[root_ref].add(ref)
        c["properties"] = props
        components.append(c)
        deps[ref] = set()
    missing = sorted(direct - {c["name"] for c in components})
    if missing:
        raise SystemExit(f"{manifest}: mkosi.conf's Packages= are not in the image: {', '.join(missing)}")
    # Not Ubuntu packages: ours, and the upstream sources we build or fetch.
    pins = kv_file(repo / "fsal" / "GANESHA_VERSION")
    helper = kv_file(repo / "appliance" / "third-party" / "aws_signing_helper" / "PIN")
    # The helper's checksum for the image's architecture, where the pin has one per architecture.
    arch_key = {"x86-64": "x86_64", "arm64": "arm64"}.get(arch, "")
    helper["HELPER_SHA256"] = helper.get(f"HELPER_SHA256_{arch_key}") or helper.get("HELPER_SHA256", "")
    samba = re.search(r"samba-(\d+\.\d+\.\d+)\.tar\.gz", (repo / "samba-vfs" / "SHA256SUMS").read_text())
    extra = [
        ("omnifile-edge", version, OWN_LICENCE, f"pkg:generic/omnifile-edge@{version}",
         "the edge daemon and its tools in /usr/local/bin (SBOM omnifile-edge)", None),
        ("FSAL_OMNI", version, "LGPL-3.0-only", f"pkg:generic/omnifile-fsal@{version}",
         "/usr/lib/omnifile/ganesha/lib/ganesha/libfsalomni.so (fsal/)", None),
        ("vfs_omnifile_prevver", version, "GPL-3.0-or-later", f"pkg:generic/omnifile-samba-vfs@{version}",
         "the Samba VFS module (samba-vfs/, decision 36), built in Samba " + (samba.group(1) if samba else "?"), None),
        ("nfs-ganesha", pins.get("GANESHA_TAG", ""), "LGPL-3.0-or-later",
         f"pkg:generic/nfs-ganesha@{pins.get('GANESHA_TAG', '')}?vcs_url="
         f"git%2Bhttps://github.com/nfs-ganesha/nfs-ganesha%40{pins.get('GANESHA_COMMIT', '')}",
         "/usr/lib/omnifile/ganesha, built by fsal/build/Containerfile", None),
        ("ntirpc", pins.get("NTIRPC_COMMIT", "")[:12], "BSD-3-Clause",
         f"pkg:generic/ntirpc@{pins.get('NTIRPC_COMMIT', '')[:12]}?vcs_url="
         f"git%2Bhttps://github.com/nfs-ganesha/ntirpc%40{pins.get('NTIRPC_COMMIT', '')}",
         "libntirpc, Ganesha's submodule", None),
        # Named by its Go module, as osv-scanner looks it up: the Go advisories for that module and version.
        ("github.com/aws/rolesanywhere-credential-helper", helper.get("HELPER_VERSION", ""), "Apache-2.0",
         f"pkg:golang/github.com/aws/rolesanywhere-credential-helper@v{helper.get('HELPER_VERSION', '')}",
         "/usr/libexec/omnifile/aws_signing_helper, pinned in appliance/third-party/aws_signing_helper/PIN",
         helper.get("HELPER_SHA256")),
    ]
    for name, ver, licence, purl, note, digest in extra:
        c = {"type": "application" if name.startswith(("omnifile-edge", "github.com/aws/")) else "library",
             "bom-ref": purl, "name": name, "version": ver, "purl": purl, "scope": "required",
             "licenses": licence_entry(licence), "properties": [prop("direct", "true"), prop("note", note)]}
        if name in ("omnifile-edge", "FSAL_OMNI", "vfs_omnifile_prevver"):
            c["supplier"] = {"name": SUPPLIER}
            c["properties"].append(prop("own", "true"))
        if digest:
            c["hashes"] = [{"alg": "SHA-256", "content": digest}]
        components.append(c)
        deps[root_ref].add(purl)
        deps[purl] = set()
    # The builder's tools (BUILD-INFO): they make the image, and the UKI's stub comes from systemd-boot-efi.
    info = kv_file(build_info) if build_info else {}
    for tool in ("mkosi", "systemd-repart", "systemd-ukify", "systemd-boot-efi", "erofs-utils", "zstd", "patchelf"):
        if tool in info:
            ref = f"pkg:deb/ubuntu/{tool}@{quote(info[tool])}?distro=ubuntu-26.04#builder"
            components.append({"type": "application", "bom-ref": ref, "name": tool, "version": info[tool],
                               "purl": ref.split("#")[0],
                               "licenses": licence_entry(" AND ".join(lic[tool]), "concluded")
                               if lic.get(tool) else [],
                               "scope": "required" if tool == "systemd-boot-efi" else "excluded",
                               "properties": [prop("role", "the UKI's stub" if tool == "systemd-boot-efi"
                                                   else "image builder")]})
            deps[ref] = set()
    props = [prop("source", manifest.name), prop("source-sha256", sha256_file(manifest)),
             prop("architecture", arch), prop("image-version", cfg.get("version", ""))]
    for k in ("snapshot", "source_date_epoch", "commit", "stage_sha256"):
        if k in info:
            props.append(prop(f"build-{k}", info[k]))
    if offer:
        props.append(prop("licences-from", "the source offer's debian/copyright files (DEP-5)"))
    return bom("appliance", version, root_component("operating-system", "omnifile-edge-appliance", version, root_ref),
               components, deps, props)


# --- the release tools: release/tools.toml ---------------------------------------------------------------------


def tools_sbom(version: str, tools_file: Path = HERE / "tools.toml") -> dict:
    data = tomllib.loads(tools_file.read_text())
    components, deps = [], {"omnifile:tools": set()}
    for name, t in sorted(data.get("tool", {}).items()):
        ref = t.get("purl") or f"pkg:generic/{quote(name)}@{quote(t['version'])}"
        components.append({
            "type": "application", "bom-ref": ref, "name": name, "version": t["version"], "purl": ref,
            "scope": "excluded", "licenses": licence_entry(t["licence"], "concluded"),
            "properties": [prop("direct", "true"), prop("used-for", t["used_for"]), prop("pinned-in", t["pinned_in"])],
        })
        deps["omnifile:tools"].add(ref)
        deps[ref] = set()
    return bom("tools", version, root_component("application", "omnifile-release-tools", version, "omnifile:tools"),
               components, deps, [prop("source", "release/tools.toml"), prop("source-sha256", sha256_file(tools_file))])


# --- main ---------------------------------------------------------------------------------------------------------


def write(doc: dict, out: Path, name: str, version: str) -> Path:
    p = out / f"omnifile-{name}-{version}.cdx.json"
    p.write_text(json.dumps(doc, indent=2, sort_keys=False) + "\n")
    return p


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path)
    ap.add_argument("--version", help="default: the repository's VERSION")
    ap.add_argument("--images-lock", type=Path, help="default: deploy/images/images.lock.json")
    ap.add_argument("--appliance-manifest", type=Path, help="mkosi's omnifile-edge_V.manifest")
    ap.add_argument("--appliance-build-info", type=Path, help="the build's BUILD-INFO (default: beside the manifest)")
    ap.add_argument("--source-offer", type=Path, help="a collected source offer, for the appliance's licences")
    ap.add_argument("--rust-target", default="x86_64-unknown-linux-gnu")
    ap.add_argument("--list-lockfiles", action="store_true")
    args = ap.parse_args(argv)
    locks = tracked_lockfiles()
    if args.list_lockfiles:
        for lk in locks:
            print(f"{sbom_name(lk)}\t{lk.relative_to(REPO)}")
        return 0
    if not args.out:
        ap.error("--out is required")
    version = args.version or (REPO / "VERSION").read_text().strip()
    args.out.mkdir(parents=True, exist_ok=True)
    written, docs = [], {}
    for lk in locks:
        name = sbom_name(lk)
        if lk.name == "Cargo.lock":
            doc = cargo_sbom(lk, version, args.rust_target)
        elif lk.name == "uv.lock":
            doc = uv_sbom(lk, version)
        else:
            doc = npm_sbom(lk, version)
        docs[name] = doc
        written.append(write(doc, args.out, name, version))
    images = args.images_lock or REPO / "deploy" / "images" / "images.lock.json"
    if images.is_file():
        written.append(write(images_sbom(images, version, docs), args.out, "images", version))
    elif args.images_lock:
        raise SystemExit(f"no {images}")
    else:
        print(f"sbom: no {images.relative_to(REPO)}: no images SBOM (give --images-lock)", file=sys.stderr)
    if args.appliance_manifest:
        bi = args.appliance_build_info or args.appliance_manifest.parent / "BUILD-INFO"
        written.append(write(appliance_sbom(args.appliance_manifest, version, bi if bi.is_file() else None,
                                            args.source_offer), args.out, "appliance", version))
    written.append(write(tools_sbom(version), args.out, "tools", version))
    for p in written:
        d = json.loads(p.read_text())
        n = len(d["components"]) + sum(len(c.get("components", [])) for c in d["components"])
        print(f"sbom: {p.name}: {n} components")
    return 0


if __name__ == "__main__":
    sys.exit(main())
