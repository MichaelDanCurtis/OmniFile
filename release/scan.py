#!/usr/bin/env python3
"""The release's vulnerability scan: osv-scanner over every lockfile, with written waivers.

    release/scan.py [--out DIR] [--waivers release/vuln-waivers.toml] [--offline] [--today YYYY-MM-DD]
                    [--lockfile FILE]... [--sbom FILE]... [--images-lock FILE [--require-images]] [--osv-json FILE]
    release/scan.py --download-db [--sbom FILE]...    fetch OSV's offline databases once, for --offline runs

What it scans:
  - every lockfile git tracks (the same set release/sbom.py makes SBOMs of), plus any --lockfile;
  - each --sbom, a CycloneDX SBOM: the appliance's, whose packages are Ubuntu 26.04's (deb purls with
    distro=ubuntu-26.04, checked against Ubuntu's advisories) and whose aws_signing_helper is the Go module
    github.com/aws/rolesanywhere-credential-helper at its pinned version (checked against the Go advisories);
  - with --images-lock, each container image by its local tag (`osv-scanner scan image`), for its OS layers. An
    image that isn't built here is recorded as not scanned (scan.json "not_scanned", and the notes); with
    --require-images (a real release) that fails.
Each with the
pinned osv-scanner (OSV_SCANNER_VERSION below; release/tools.toml), `--all-vulns`, and groups each package's
advisories as OSV does (aliases are one finding). A finding's severity is the highest of its CVSS score (Critical
>= 9.0, High >= 7.0, Medium >= 4.0) and its database's own label (GHSA's CRITICAL/HIGH/MODERATE/LOW). An advisory
with neither is UNKNOWN; a RustSec advisory marked informational (unmaintained, unsound, notice) is INFO.

The scan FAILS (exit 1) when:
  - a CRITICAL, HIGH or UNKNOWN finding has no waiver (UNKNOWN: nobody scored it, so someone has to read it). For an
    operating system's package (Ubuntu, Debian) only when its distribution has a fixed version: one it hasn't fixed
    can't be fixed by a rebuild, so it is reported as "no fix in the distribution yet";
  - a waiver has expired (its `expires` is before today), or lacks an id, package, reason, owner or expiry.
A waiver that names no finding is reported as stale (remove it), but doesn't fail; one that names a finding but
doesn't apply (expired, or dry_run_only in a real release) is a waiver problem, not stale. MEDIUM, LOW and INFO findings
are reported only.

Waivers (release/vuln-waivers.toml), one table each:
    [[waiver]]
    id = "RUSTSEC-2026-0344"          # any of the finding's ids or aliases
    package = "ruzstd"
    lockfile = "edge/Cargo.lock"      # optional: only there (sbom:appliance for the appliance SBOM, image:web)
    dry_run_only = true               # optional: a known blocker a rehearsal may pass on; a real release (--release)
                                      # fails on it
    reason = "why it can't be reached, or why it doesn't matter here"
    owner = "who removes it, and what fixes it"
    expires = 2026-12-31

--out writes scan.json (every finding, with its waiver) and scan.md (the release notes' security section).
--offline uses only OSV databases already downloaded (OSV_SCANNER_LOCAL_DB_CACHE_DIRECTORY, default
release/build/osv-db): the release dry run makes no network call. --osv-json reads a recorded osv-scanner JSON
result instead of running it (the tests' fixtures).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
OSV_SCANNER_VERSION = "2.7.0"  # osv-scanner, Apache-2.0 (release/tools.toml)
LEVELS = ["NONE", "INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL", "UNKNOWN"]
FAILING = {"CRITICAL", "HIGH", "UNKNOWN"}
DB_LABELS = {"CRITICAL": "CRITICAL", "HIGH": "HIGH", "MODERATE": "MEDIUM", "MEDIUM": "MEDIUM", "LOW": "LOW"}
DEFAULT_DB = HERE / "build" / "osv-db"
OS_ECOSYSTEMS = {"Ubuntu", "Debian", "Alpine"}
WAIVER_FIELDS = ("id", "package", "reason", "owner", "expires")


@dataclass
class Finding:
    lockfile: str
    ecosystem: str
    package: str
    version: str
    ids: list[str]
    severity: str
    score: str
    summary: str
    fixed: list[str] = field(default_factory=list)
    waiver: dict | None = None
    primary: str = ""  # the advisory's own id (GHSA if there is one), not an alias

    @property
    def os_package(self) -> bool:
        return self.ecosystem.split(":", 1)[0] in OS_ECOSYSTEMS

    @property
    def fails(self) -> bool:
        if self.os_package and not self.fixed:
            return False
        return self.severity in FAILING and self.waiver is None


def level_of_score(score: str) -> str:
    try:
        s = float(score)
    except (TypeError, ValueError):
        return "NONE"
    return "CRITICAL" if s >= 9.0 else "HIGH" if s >= 7.0 else "MEDIUM" if s >= 4.0 else "LOW" if s > 0 else "NONE"


def rank(level: str) -> int:
    return LEVELS.index(level)


def fixed_versions(v: dict, ecosystem: str = "") -> list[str]:
    """The fixed versions, from the advisory's entries for this package's ecosystem (Ubuntu:26.04:LTS, not 22.04)."""
    out = []
    for a in v.get("affected", []):
        eco = (a.get("package") or {}).get("ecosystem", "")
        if ecosystem and eco and eco != ecosystem:
            continue
        for r in a.get("ranges", []):
            out += [e["fixed"] for e in r.get("events", []) if "fixed" in e]
    return sorted(set(out))


def source_key(path: str, repo: Path = REPO) -> str:
    """Where a finding is, as a waiver names it wherever the scan ran: a lockfile relative to the repository,
    sbom:<name> for an SBOM (omnifile-appliance-0.1.0.cdx.json is sbom:appliance), image:<name> for an image."""
    if path.startswith("image:"):
        return path.split("@", 1)[0]
    m = re.match(r"^omnifile-(.+)-\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?\.cdx\.json$", Path(path).name)
    if m:
        return f"sbom:{m.group(1)}"
    try:
        return str(Path(path).resolve().relative_to(repo))
    except ValueError:
        return path


def findings_from(osv: dict, repo: Path = REPO) -> list[Finding]:
    out = []
    for res in osv.get("results", []):
        rel = source_key(res["source"]["path"], repo)
        for p in res.get("packages", []):
            pkg = p["package"]
            vulns = {v["id"]: v for v in p.get("vulnerabilities", [])}
            groups = p.get("groups") or [{"ids": [i], "max_severity": ""} for i in vulns]
            for g in groups:
                members = [vulns[i] for i in g["ids"] if i in vulns]
                if not members:
                    continue
                ids = sorted({i for v in members for i in [v["id"], *v.get("aliases", [])]})
                level = level_of_score(g.get("max_severity", ""))
                for v in members:
                    lab = DB_LABELS.get(str(v.get("database_specific", {}).get("severity", "")).upper())
                    if lab and rank(lab) > rank(level):
                        level = lab
                if level == "NONE":
                    informational = all(
                        any((a.get("database_specific") or {}).get("informational") for a in v.get("affected", []))
                        for v in members
                    )
                    level = "INFO" if informational else "UNKNOWN"
                summary = next((v.get("summary") for v in members if v.get("summary")), "")
                own = [v["id"] for v in members]
                out.append(Finding(rel, pkg.get("ecosystem", ""), pkg["name"], pkg["version"], ids, level,
                                   g.get("max_severity", "") or "", summary,
                                   sorted({f for v in members for f in fixed_versions(v, pkg.get("ecosystem", ""))}),
                                   primary=next((i for i in own if i.startswith("GHSA-")), own[0])))
    return out


def load_waivers(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return tomllib.loads(path.read_text()).get("waiver", [])


def apply_waivers(findings: list[Finding], waivers: list[dict], today: dt.date, release: bool = False) -> list[str]:
    """Attach each valid waiver to the findings it covers; returns the waiver problems (which fail the scan). In a
    real release (release=True) a waiver marked dry_run_only = true covers nothing: it exists so a rehearsal can
    pass on a known blocker, never a release."""
    problems = []
    valid = []
    for i, w in enumerate(waivers, 1):
        missing = [f for f in WAIVER_FIELDS if not w.get(f)]
        if missing:
            problems.append(f"waiver {i} ({w.get('id', '?')}): missing {', '.join(missing)}")
            continue
        exp = w["expires"]
        if isinstance(exp, str):
            try:
                exp = dt.date.fromisoformat(exp)
            except ValueError:
                problems.append(f"waiver {w['id']}: expires {w['expires']!r} is not a date (YYYY-MM-DD)")
                continue
        if isinstance(exp, dt.datetime):
            exp = exp.date()
        if exp < today:
            problems.append(f"waiver {w['id']} ({w['package']}) expired on {exp.isoformat()}: fix it or renew the "
                            f"waiver with a new reason (owner: {w['owner']})")
            continue
        if release and w.get("dry_run_only") is True:
            problems.append(f"waiver {w['id']} ({w['package']}) is for dry runs only: it blocks a real release "
                            f"(owner: {w['owner']})")
            continue
        valid.append(w)
    for f in findings:
        for w in valid:
            if covers(w, f):
                f.waiver = {k: str(v) for k, v in w.items()}
                break
    return problems


def covers(w: dict, f: Finding) -> bool:
    """Whether waiver w names finding f: its id or an alias, its package, and its lockfile when the waiver names one."""
    return w["id"] in f.ids and w["package"].lower() == f.package.lower() and (
        not w.get("lockfile") or w["lockfile"] == f.lockfile
    )


def stale_waivers(findings: list[Finding], waivers: list[dict]) -> list[dict]:
    """Complete waivers that name no finding at all: those can go. A waiver that names a finding but doesn't apply
    (expired, or dry_run_only in a real release) is a waiver problem, reported as such, never as stale: removing it
    would not fix anything."""
    return [w for w in waivers if all(w.get(k) for k in WAIVER_FIELDS) and not any(covers(w, f) for f in findings)]


def tracked_lockfiles() -> list[Path]:
    sys.path.insert(0, str(HERE))
    import sbom  # noqa: E402  (the same set the SBOMs are made of)

    return sbom.tracked_lockfiles()


def osv_env() -> dict:
    env = dict(os.environ)
    env.setdefault("OSV_SCANNER_LOCAL_DB_CACHE_DIRECTORY", str(DEFAULT_DB))
    return env


def check_version() -> None:
    r = subprocess.run(["osv-scanner", "--version"], capture_output=True, text=True)
    m = re.search(r"osv-scanner version: (\S+)", r.stdout)
    if not m or m.group(1) != OSV_SCANNER_VERSION:
        raise SystemExit(f"scan: osv-scanner {m.group(1) if m else '(not found)'}, not the pinned "
                         f"{OSV_SCANNER_VERSION} (release/tools.toml)")


def offline_flags() -> list[str]:
    db = Path(osv_env()["OSV_SCANNER_LOCAL_DB_CACHE_DIRECTORY"])
    if not db.is_dir() or not any(db.rglob("*.zip")):
        raise SystemExit(f"scan: --offline, but no OSV databases in {db}: run release/scan.py --download-db "
                         "first (that is the one step that fetches them)")
    return ["--offline", "--offline-vulnerabilities"]


def run_osv(lockfiles: list[Path], offline: bool) -> dict:
    check_version()
    cmd = ["osv-scanner", "scan", "--format", "json", "--all-vulns", "--all-packages", "--verbosity", "error"]
    if offline:
        cmd += offline_flags()
    for lk in lockfiles:
        cmd += ["-L", str(lk)]
    r = subprocess.run(cmd, capture_output=True, text=True, env=osv_env())
    # 0: nothing found; 1: vulnerabilities found. Anything else is the scanner failing.
    if r.returncode not in (0, 1):
        raise SystemExit(f"scan: osv-scanner exited {r.returncode}: {r.stderr.strip()[-800:]}")
    out = json.loads(r.stdout or "{}")
    scanned = {Path(res["source"]["path"]).resolve() for res in out.get("results", [])}
    return out | {"_scanned": sorted(str(p) for p in scanned)}


def scan_images(lock: Path, offline: bool) -> tuple[dict, list[str]]:
    """Each image of the lock by its local tag, for its OS layers: (osv-scanner results, the images not here)."""
    images = json.loads(lock.read_text()).get("images", {})
    results, not_here = [], []
    for name, img in sorted(images.items()):
        tag = img.get("local_tag", "")
        if not tag or subprocess.run(["docker", "image", "inspect", tag], capture_output=True).returncode != 0:
            not_here.append(f"image {name} ({tag or 'no local tag'}): not built here, its OS layers not scanned")
            continue
        cmd = ["osv-scanner", "scan", "image", "--format", "json", "--all-vulns", "--verbosity", "error"]
        cmd += (offline_flags() if offline else []) + [tag]
        r = subprocess.run(cmd, capture_output=True, text=True, env=osv_env())
        if r.returncode not in (0, 1):
            raise SystemExit(f"scan: osv-scanner scan image {tag} exited {r.returncode}: {r.stderr.strip()[-600:]}")
        for res in json.loads(r.stdout or "{}").get("results", []):
            res["source"] = {"path": f"image:{name}@{img.get('digest', '')}", "type": "image"}
            results.append(res)
    return {"results": results}, not_here


def download_db(lockfiles: list[Path]) -> int:
    check_version()
    cmd = ["osv-scanner", "scan", "--format", "json", "--offline-vulnerabilities", "--download-offline-databases",
           "--verbosity", "error"]
    for lk in lockfiles:
        cmd += ["-L", str(lk)]
    r = subprocess.run(cmd, capture_output=True, text=True, env=osv_env())
    if r.returncode not in (0, 1):
        print(r.stderr[-800:], file=sys.stderr)
        return 1
    print(f"scan: OSV databases in {osv_env()['OSV_SCANNER_LOCAL_DB_CACHE_DIRECTORY']}")
    return 0


def report_md(findings: list[Finding], problems: list[str], stale: list[dict], lockfiles: list[str],
              not_scanned: list[str] | None = None) -> str:
    lines = ["## Security", "",
             f"osv-scanner {OSV_SCANNER_VERSION} scanned {len(lockfiles)} lockfiles and SBOMs: "
             + ", ".join(f"`{x}`" for x in lockfiles) + ".", ""]
    for n in not_scanned or []:
        lines.append(f"- **Not scanned:** {n}.")
    if not_scanned:
        lines.append("")
    if not findings:
        lines.append("No known vulnerabilities.")
    else:
        lines += ["| Severity | Package | Version | Lockfile | Advisory | Fixed in | Status |", "|---|---|---|---|---|---|---|"]
        for f in sorted(findings, key=lambda f: (-rank(f.severity), f.package, f.version)):
            status = ("waived until " + f.waiver["expires"] + ": " + f.waiver["reason"]) if f.waiver else (
                "**FAILS the release**" if f.fails else
                "reported: no fix in the distribution yet" if f.os_package and not f.fixed else "reported")
            adv = f.primary + "".join(f" ({i})" for i in f.ids if i.startswith("CVE-"))
            lines.append(f"| {f.severity} | {f.package} | {f.version} | `{f.lockfile}` | {adv}: {f.summary} | "
                         f"{', '.join(f.fixed) or '-'} | {status} |")
    for p in problems:
        lines += ["", f"- **Waiver problem:** {p}"]
    for w in stale:
        lines += ["", f"- Stale waiver {w['id']} ({w['package']}): it matches no finding; remove it."]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path)
    ap.add_argument("--waivers", type=Path, default=HERE / "vuln-waivers.toml")
    ap.add_argument("--offline", action="store_true", help="use only the OSV databases already downloaded")
    ap.add_argument("--download-db", action="store_true", help="download OSV's databases for --offline, and stop")
    ap.add_argument("--today", type=dt.date.fromisoformat, default=dt.date.today())
    ap.add_argument("--lockfile", type=Path, action="append", default=[], help="scan this lockfile as well")
    ap.add_argument("--sbom", type=Path, action="append", default=[],
                    help="scan a CycloneDX SBOM too (the appliance's: Ubuntu packages, the Go helper)")
    ap.add_argument("--images-lock", type=Path, help="scan each image of this lock by its local tag (OS layers)")
    ap.add_argument("--require-images", action="store_true", help="fail when an image isn't built here")
    ap.add_argument("--release", action="store_true",
                    help="a real release: dry_run_only waivers don't apply (release/runbook.sh without --dry-run)")
    ap.add_argument("--osv-json", type=Path, help="read a recorded osv-scanner JSON result instead of scanning")
    args = ap.parse_args(argv)
    lockfiles = tracked_lockfiles() + [p.resolve() for p in args.lockfile] + [p.resolve() for p in args.sbom]
    if args.download_db:
        return download_db(lockfiles)
    if args.osv_json:
        osv = json.loads(args.osv_json.read_text())
    else:
        osv = run_osv(lockfiles, args.offline)
        missing = sorted(str(p) for p in lockfiles if str(p) not in osv["_scanned"])
        if missing:
            print(f"scan: osv-scanner read nothing from {', '.join(missing)}", file=sys.stderr)
            return 1
    not_scanned: list[str] = []
    if args.images_lock:
        imgs, not_scanned = scan_images(args.images_lock, args.offline)
        osv["results"] = osv.get("results", []) + imgs["results"]
    if not args.images_lock:
        not_scanned.append("the container images' OS layers: no --images-lock given")
    findings = findings_from(osv)
    problems = apply_waivers(findings, load_waivers(args.waivers), args.today, release=args.release)
    stale = stale_waivers(findings, load_waivers(args.waivers))
    rels = [str(p.relative_to(REPO)) if p.is_relative_to(REPO) else str(p) for p in lockfiles]
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "scan.json").write_text(json.dumps({
            "scanner": f"osv-scanner {OSV_SCANNER_VERSION}", "date": args.today.isoformat(),
            "offline": args.offline, "lockfiles": rels,
            "findings": [vars(f) for f in findings], "waiver_problems": problems,
            "stale_waivers": stale, "not_scanned": not_scanned,
            "passed": not problems and not any(f.fails for f in findings)
            and not (args.require_images and not_scanned),
        }, indent=2, default=str) + "\n")
        (args.out / "scan.md").write_text(report_md(findings, problems, stale, rels, not_scanned))
    for f in sorted(findings, key=lambda f: (-rank(f.severity), f.package)):
        tag = "FAIL  " if f.fails else "waived" if f.waiver else "report"
        print(f"scan: {tag} {f.severity:<8} {f.package} {f.version} ({f.lockfile}) {f.primary}: {f.summary}")
    for p in problems:
        print(f"scan: FAIL   {p}", file=sys.stderr)
    for n in not_scanned:
        print(f"scan: {'FAIL  ' if args.require_images and n.startswith('image') else 'gap   '} {n}", file=sys.stderr)
    for w in stale:
        print(f"scan: stale  waiver {w['id']} ({w['package']}) matches nothing: remove it", file=sys.stderr)
    failing = [f for f in findings if f.fails]
    print(f"scan: {len(findings)} finding(s) in {len(rels)} lockfiles; {len(failing)} unwaived at HIGH or above "
          f"(or unscored); {len(problems)} waiver problem(s)")
    if args.require_images and any(n.startswith("image") for n in not_scanned):
        return 1
    return 1 if failing or problems else 0


if __name__ == "__main__":
    sys.exit(main())
