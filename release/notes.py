#!/usr/bin/env python3
"""Release notes from the merged pull requests between two tags or commits, from local git only.

    release/notes.sh FROM TO [--version V] [--security scan.md] [--known-issues FILE] [--out FILE]

A pull request is a commit on TO's first-parent line after FROM: a merge commit "Merge pull request #N from
OWNER/BRANCH" (its title is the commit body's first line, as GitHub writes it), or a squash merge "TITLE (#N)".
Each goes under one area: the part of the tree with most of its changed files (edge, appliance, service, ...;
docs only if it changed nothing else). The notes fill docs/release/notes-template.md: the changes, the security
section (the scan's scan.md, release/scan.py --out) and the known issues (docs/release/known-issues.md, which the
release manager edits for each release). No GitHub API: the history is enough.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TEMPLATE = REPO / "docs" / "release" / "notes-template.md"
KNOWN = REPO / "docs" / "release" / "known-issues.md"
# Path prefix -> area, the first match wins; the order is also the notes' order.
AREAS = [
    ("edge/", "Edge daemon and tools"),
    ("fsal/", "NFS (FSAL_OMNI)"),
    ("samba-vfs/", "SMB (Samba module)"),
    ("appliance/", "Appliance image and updates"),
    ("ci/kernel-canary/", "Appliance image and updates"),
    ("service/", "Service"),
    ("relay/", "Teams relay"),
    ("portal/", "Portal"),
    ("deploy/", "Azure deployment"),
    ("release/", "Release engineering"),
    ("tests/", "Tests"),
    ("dev/", "Tests"),
    ("spikes/", "Tests"),
    ("", "Documentation"),
]
ORDER = list(dict.fromkeys(a for _, a in AREAS))
MERGE_RE = re.compile(r"^Merge pull request #(\d+) from (\S+)$")
SQUASH_RE = re.compile(r"^(.*) \(#(\d+)\)$")


def git(*args: str, repo: Path = REPO) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def area_of(files: list[str]) -> str:
    def one(f: str) -> str:
        return next(a for p, a in AREAS if f.startswith(p))

    counts = Counter(one(f) for f in files if not f.startswith("docs/") and f not in ("AGENTS.md", "CLAUDE.md"))
    if not counts:
        return "Documentation"
    best = max(counts.values())
    return next(a for a in ORDER if counts.get(a) == best)


def pull_requests(frm: str, to: str, repo: Path = REPO) -> list[dict]:
    out = []
    log = git("log", "--first-parent", "--format=%H%x1f%P%x1f%s%x1f%b%x1e", f"{frm}..{to}", repo=repo)
    for rec in log.split("\x1e"):
        rec = rec.strip("\n")
        if not rec:
            continue
        sha, parents, subject, body = (rec.split("\x1f") + ["", "", "", ""])[:4]
        parents = parents.split()
        m = MERGE_RE.match(subject)
        if m and len(parents) >= 2:
            number, branch = int(m.group(1)), m.group(2)
            title = next((ln.strip() for ln in body.splitlines() if ln.strip()), branch)
            base = parents[0]
        else:
            s = SQUASH_RE.match(subject)
            if not s:
                continue  # a direct commit, not a pull request
            title, number, branch = s.group(1), int(s.group(2)), ""
            base = parents[0] if parents else f"{sha}^"
        files = [f for f in git("diff", "--name-only", base, sha, repo=repo).splitlines() if f]
        out.append({"number": number, "title": title, "branch": branch, "commit": sha[:12],
                    "area": area_of(files), "files": len(files)})
    return sorted(out, key=lambda p: p["number"])


def render(version: str, frm: str, to: str, prs: list[dict], security: str, known: str) -> str:
    lines = []
    for area in ORDER:
        mine = [p for p in prs if p["area"] == area]
        if mine:
            lines += [f"### {area}", ""] + [f"- {p['title']} (#{p['number']})" for p in mine] + [""]
    changes = "\n".join(lines).rstrip() or "No pull requests were merged in this range."
    t = TEMPLATE.read_text()
    return (t.replace("{{VERSION}}", version).replace("{{FROM}}", frm).replace("{{TO}}", to)
            .replace("{{COUNT}}", str(len(prs))).replace("{{CHANGES}}", changes)
            .replace("{{SECURITY}}", security.strip()).replace("{{KNOWN_ISSUES}}", known.strip()))


def strip_heading(text: str) -> str:
    """A section file's comments and its own top heading are dropped: the template has the heading."""
    lines = re.sub(r"<!--.*?-->", "", text, flags=re.S).strip().splitlines()
    while lines and (lines[0].startswith("#") or not lines[0].strip()):
        lines.pop(0)
    return "\n".join(lines).strip()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("frm", metavar="FROM")
    ap.add_argument("to", metavar="TO")
    ap.add_argument("--version", default=(REPO / "VERSION").read_text().strip() if (REPO / "VERSION").is_file() else "")
    ap.add_argument("--security", type=Path, help="the scan's scan.md (release/scan.py --out)")
    ap.add_argument("--known-issues", type=Path, default=KNOWN)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args(argv)
    for ref in (args.frm, args.to):
        if subprocess.run(["git", "-C", str(REPO), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
                          capture_output=True).returncode:
            ap.error(f"{ref}: not a commit or tag here")
    prs = pull_requests(args.frm, args.to)
    security = strip_heading(args.security.read_text()) if args.security else \
        "_No scan attached: run release/scan.py --out DIR and pass --security DIR/scan.md._"
    known = strip_heading(args.known_issues.read_text()) if args.known_issues.is_file() else "None recorded."
    text = render(args.version, args.frm, args.to, prs, security, known)
    if args.out:
        args.out.write_text(text)
        print(f"notes: {len(prs)} pull requests -> {args.out}")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
