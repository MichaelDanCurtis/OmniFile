"""Publish a scrubbed snapshot of this repo to its public mirror.

This repo is private and keeps its full history, its pull requests and the details of the
owner's own test accounts. The public repo gets one new commit per publish: the tracked tree
at a ref, with every rule of a local scrub file applied, and nothing else. No history, no
pull requests, and the commit is authored with the owner's GitHub no-reply address.

    python3 release/public_snapshot.py --repo OWNER/NAME            # build and check only
    python3 release/public_snapshot.py --repo OWNER/NAME --push     # and publish

The scrub file (default `release/public-scrub.local`, git-ignored, kept by the owner; the
format is in `release/public-scrub.example`) holds the literal strings to replace or drop.
It is never published: it names exactly what must not be.

A snapshot is refused, and nothing is pushed, when:
- a string the scrub file names is still anywhere in it;
- gitleaks (with the repo's `.gitleaks.toml`) finds anything in it;
- any file is over 50 MB (GitHub's warning size).
"""

from __future__ import annotations

import argparse
import fnmatch
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MAX_FILE = 50 * 1024 * 1024


class Refused(Exception):
    """The snapshot must not be published."""


@dataclass
class Rules:
    replace: list[tuple[str, str]] = field(default_factory=list)
    drop_line: list[str] = field(default_factory=list)
    drop_path: list[str] = field(default_factory=list)

    def forbidden(self) -> list[str]:
        """What must be nowhere in the published tree."""
        return [old for old, _ in self.replace] + list(self.drop_line)


def load_rules(text: str) -> Rules:
    """One rule a line, fields separated by tabs; `#` starts a comment line.

    replace<TAB>old<TAB>new     every occurrence of old, in every text file
    drop-line<TAB>text          every line that contains text
    drop-path<TAB>glob          every file whose path matches glob (fnmatch, from the root)
    """
    rules = Rules()
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split("\t")
        kind = parts[0]
        if kind == "replace" and len(parts) == 3 and parts[1]:
            rules.replace.append((parts[1], parts[2]))
        elif kind == "drop-line" and len(parts) == 2 and parts[1]:
            rules.drop_line.append(parts[1])
        elif kind == "drop-path" and len(parts) == 2 and parts[1]:
            rules.drop_path.append(parts[1])
        else:
            raise ValueError(f"scrub rule {n} is not one of replace, drop-line, drop-path: {raw!r}")
    return rules


def _is_text(data: bytes) -> bool:
    if b"\0" in data[:8192]:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def scrub_tree(tree: Path, rules: Rules) -> list[str]:
    """Apply the rules in place. Returns what changed, one line per file."""
    changed = []
    for p in sorted(tree.rglob("*")):
        is_link = p.is_symlink()
        if not is_link and not p.is_file():
            continue
        rel = p.relative_to(tree).as_posix()
        # paths first, links included: a dropped path goes whatever it is
        if any(fnmatch.fnmatch(rel, g) for g in rules.drop_path):
            p.unlink()
            changed.append(f"dropped {rel}")
            continue
        if is_link:
            # a link's target is text too, read without following it
            target = os.readlink(p)
            new_target = target
            for old, rep in rules.replace:
                new_target = new_target.replace(old, rep)
            if new_target != target:
                p.unlink()
                os.symlink(new_target, p)
                changed.append(f"scrubbed {rel} (link)")
            continue
        data = p.read_bytes()
        if not _is_text(data):
            continue
        text = data.decode("utf-8")
        new = text
        if rules.drop_line:
            keep = [ln for ln in new.splitlines(keepends=True) if not any(t in ln for t in rules.drop_line)]
            new = "".join(keep)
        for old, rep in rules.replace:
            new = new.replace(old, rep)
        if new != text:
            p.write_text(new, encoding="utf-8")
            changed.append(f"scrubbed {rel}")
    # folders the drops emptied (git keeps no empty folder, so none was there before)
    for d in sorted((d for d in tree.rglob("*") if d.is_dir() and not d.is_symlink()),
                    key=lambda d: len(d.parts), reverse=True):
        if not any(d.iterdir()):
            d.rmdir()
    return changed


def check_tree(tree: Path, rules: Rules, *, gitleaks: str | None = "gitleaks") -> None:
    """Refuse a snapshot that still holds anything it must not."""
    problems = []
    forbidden = rules.forbidden()
    for p in sorted(tree.rglob("*")):
        rel = p.relative_to(tree).as_posix()
        # every path, file, link or directory, by its name
        if any(f in rel for f in forbidden):
            problems.append(f"a path still holds a scrubbed string: {rel}")
        is_dir = p.is_dir() and not p.is_symlink()
        if not is_dir and any(fnmatch.fnmatch(rel, g) for g in rules.drop_path):
            problems.append(f"{rel} should have been dropped")
        if p.is_symlink():
            # a link's target, read without following it
            if any(f in os.readlink(p) for f in forbidden):
                problems.append(f"{rel}'s link target still holds a scrubbed string")
            continue
        if not p.is_file():
            continue
        if p.stat().st_size > MAX_FILE:
            problems.append(f"{rel} is over 50 MB")
        data = p.read_bytes()
        for f in forbidden:
            if f.encode("utf-8") in data:
                problems.append(f"{rel} still holds a scrubbed string")
                break
    if gitleaks:
        config = tree / ".gitleaks.toml"
        cmd = [gitleaks, "dir", "--redact", "--no-banner", "--exit-code", "3",
               "--report-format", "json", "--report-path", "-", str(tree)]
        if config.exists():
            cmd[2:2] = ["--config", str(config)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 3:
            try:
                found = json.loads(r.stdout or "[]")
            except ValueError:
                found = []
            for x in found:
                problems.append(f"gitleaks: {x.get('RuleID')} in {Path(x.get('File', '')).as_posix()}"
                                f" line {x.get('StartLine')}")
            if not found:
                problems.append("gitleaks found something (no report)")
        elif r.returncode != 0:
            problems.append(f"gitleaks failed: {r.stderr.strip()[-300:]}")
    if problems:
        raise Refused("\n".join(problems))


def export_tree(ref: str, dest: Path) -> None:
    """The tracked tree at ref, nothing else (no .git, no ignored files)."""
    out = subprocess.run(["git", "-C", str(REPO_ROOT), "archive", "--format=tar", ref],
                         check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(out)) as t:
        t.extractall(dest, filter="data")


def noreply_author() -> str:
    r = subprocess.run(["gh", "api", "user", "--jq", '.login + " " + (.id|tostring)'],
                       check=True, capture_output=True, text=True)
    login, uid = r.stdout.split()
    return f"{login} <{uid}+{login}@users.noreply.github.com>"


def publish(tree: Path, repo: str, work: Path, version: str) -> str | None:
    """Commit the tree on top of the public repo's main and push it. Returns the new commit,
    or None when nothing changed."""
    clone = work / "public"
    subprocess.run(["gh", "repo", "clone", repo, str(clone), "--", "-q"], check=True)
    for p in clone.iterdir():
        if p.name == ".git":
            continue
        shutil.rmtree(p) if p.is_dir() and not p.is_symlink() else p.unlink()
    shutil.copytree(tree, clone, dirs_exist_ok=True, symlinks=True)
    git = ["git", "-C", str(clone)]
    subprocess.run([*git, "checkout", "-q", "-B", "main"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    if subprocess.run([*git, "diff", "--cached", "--quiet"]).returncode == 0:
        return None
    author = noreply_author()
    name, email = author.rsplit(" <", 1)
    email = email.rstrip(">")
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    env_args = ["-c", f"user.name={name}", "-c", f"user.email={email}"]
    subprocess.run(["git", "-C", str(clone), *env_args, "commit", "-q", "--author", author,
                    "-m", f"OmniFile Gateway {version}: snapshot of {day}"], check=True)
    subprocess.run([*git, "push", "-q", "-u", "origin", "main"], check=True)
    return subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True,
                          text=True).stdout.strip()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", required=True, help="the public repo, OWNER/NAME")
    ap.add_argument("--ref", default="origin/main", help="what to publish (default origin/main)")
    ap.add_argument("--scrub", default=str(REPO_ROOT / "release/public-scrub.local"))
    ap.add_argument("--push", action="store_true", help="publish; without it, build and check only")
    ap.add_argument("--keep", action="store_true", help="keep the work directory")
    a = ap.parse_args(argv)

    scrub = Path(a.scrub)
    if not scrub.exists():
        print(f"no scrub file at {scrub}: copy release/public-scrub.example and fill it in",
              file=sys.stderr)
        return 2
    rules = load_rules(scrub.read_text())
    work = Path(tempfile.mkdtemp(prefix="omnifile-public-"))
    try:
        tree = work / "tree"
        tree.mkdir()
        export_tree(a.ref, tree)
        for line in scrub_tree(tree, rules):
            print(line)
        check_tree(tree, rules)
        version = (tree / "VERSION").read_text().strip() if (tree / "VERSION").exists() else "snapshot"
        files = sum(1 for p in tree.rglob("*") if p.is_file())
        print(f"snapshot of {a.ref}: {files} files, checks passed")
        if not a.push:
            print(f"not pushed (no --push); the tree is in {tree}")
            a.keep = True
            return 0
        sha = publish(tree, a.repo, work, version)
        print(f"published {a.repo}@{sha}" if sha else f"{a.repo} already matches {a.ref}: nothing pushed")
        return 0
    except Refused as e:
        print(f"refused, nothing pushed:\n{e}", file=sys.stderr)
        return 1
    finally:
        if not a.keep:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
