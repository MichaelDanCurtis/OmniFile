#!/usr/bin/env python3
"""Publish one appliance release's GPL/LGPL source bundle as a GitHub Release in the public sources repository.

Decision 46: the sources live in a public GitHub repository (e.g. <owner>/omnifile-gateway-sources, created by the
owner at the first release), one GitHub Release per appliance release, tag v<release>, holding exactly the bundle
source_offer.py wrote; requests for the written offer go through that repository's issues.

    appliance/source-offer/publish_sources.py --bundle appliance/build/source-offer-1.2.0 \\
        --repo <owner>/omnifile-gateway-sources                    # dry run: checks and prints the plan
    ... --work appliance/build/publish-1.2.0 --write-archives        # also writes the archives, still no gh
    ... --work appliance/build/publish-1.2.0 --execute               # creates or updates the release with gh

It is a dry run unless --execute is given: nothing runs gh, and nothing touches the network. Before any of that it
refuses a bundle that is not a release offer:
- MANIFEST.json must say complete, and name this repository and tag (source_offer.py --sources-repo);
- SOURCE-OFFER.md must carry no DRAFT note and point at this release and the repository's issues;
- every file under ubuntu/, upstream/ and omnifile/ must be in SHA256SUMS with its hash, and nothing else.

GitHub Release assets are flat, renamed when they hold characters like '+' or '~' (Debian versions do), and at most
2 GiB each, so the source tree is packed into numbered tar archives (omnifile-edge-<release>-sources-NN.tar, each
under --volume-size, whole files only), written byte-for-byte reproducibly (sorted, fixed owner, mode and time).
ASSETS.SHA256SUMS lists them. MANIFEST.json, SHA256SUMS, packages.tsv and SOURCE-OFFER.md are attached on their
own, and the release's notes are SOURCE-OFFER.md.

With --execute: `gh release view` decides create or update. A new release is `gh release create` with every asset.
An existing one gets `gh release upload --clobber` and `gh release edit`; assets on it that this bundle doesn't
have are refused unless --prune deletes them. Then the release's assets are read back and compared by name and
size. Republishing a tag replaces its assets (upload --clobber), so never rerun --execute for an appliance version
that has already shipped. Before any release call it checks with `gh api repos/<repo>` that the repository is
public (not private or internal) and has Issues on, and refuses otherwise. gh must already be signed in with
write access to the repository; this tool never signs in. The repository
needs at least one commit (a README will do): `gh release create` tags its default branch.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tarfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import source_offer as so  # noqa: E402

TREES = ("ubuntu", "upstream", "omnifile")
LOOSE = ("MANIFEST.json", "SHA256SUMS", "packages.tsv", "SOURCE-OFFER.md")
GITHUB_ASSET_MAX = 2 * 1024**3  # GitHub: each release asset must be under 2 GiB
GITHUB_ASSETS_PER_RELEASE = 1000
DEFAULT_VOLUME = 1900 * 1024**2
RELEASE_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?$")
BLOCK = 512


class Refused(Exception):
    """The bundle or the request is not something to publish."""


@dataclass
class Volume:
    name: str
    files: list[tuple[str, int]] = field(default_factory=list)  # (relative path, size)
    estimate: int = 20 * BLOCK  # the end-of-archive blocks and tarfile's padding to a 10 KiB record


def tar_cost(rel: str, size: int) -> int:
    """An upper bound on what one regular file adds to a PAX tar: its header, a PAX header for long names, data."""
    pax = 3 * BLOCK if len(rel) > 99 else 0
    return BLOCK + pax + -(-size // BLOCK) * BLOCK


def sha256(path: Path) -> str:
    return so.sha256(path)


# --- checks ---------------------------------------------------------------------------------------------------


def load_bundle(bundle: Path, repo: str, tag: str | None) -> tuple[dict, str, dict[str, str]]:
    """Check the bundle is a complete, final offer for this repository; return its manifest, tag and sums."""
    if not bundle.is_dir():
        raise Refused(f"{bundle} is not a directory")
    for name in LOOSE:
        if not (bundle / name).is_file():
            raise Refused(f"{name} is missing from {bundle} (run source_offer.py first)")
    try:
        manifest = json.loads((bundle / "MANIFEST.json").read_text())
    except ValueError as e:
        raise Refused(f"MANIFEST.json is not JSON: {e}") from e
    release = str(manifest.get("release", ""))
    if not RELEASE_RE.match(release):
        raise Refused(f"MANIFEST.json release {release!r} is not a version like 1.2.0")
    if manifest.get("complete") is not True:
        raise Refused(
            "MANIFEST.json says the bundle is incomplete (not_in_snapshot / failed / not_fetched): not a release offer"
        )
    if not so.repo_is_set(repo):
        raise Refused(f"--repo {repo!r} is not OWNER/NAME of the sources repository (decision 46)")
    named = manifest.get("sources_repo") or ""
    if named != repo:
        raise Refused(
            f"the offer names sources repository {named or '(none)'!r}, not {repo!r}: "
            "regenerate the bundle with source_offer.py --sources-repo " + repo
        )
    want_tag = so.release_tag(release)
    if manifest.get("release_tag") != want_tag or (tag and tag != want_tag):
        raise Refused(
            f"the release's tag must be {want_tag} (one GitHub Release per appliance release)"
        )

    offer = (bundle / "SOURCE-OFFER.md").read_text()
    if "DRAFT" in offer:
        raise Refused("SOURCE-OFFER.md is marked DRAFT")
    for url in (
        f"https://github.com/{repo}/releases/tag/{want_tag}",
        f"https://github.com/{repo}/issues",
    ):
        if url not in offer:
            raise Refused(f"SOURCE-OFFER.md does not point at {url}")

    sums: dict[str, str] = {}
    for n, line in enumerate((bundle / "SHA256SUMS").read_text().splitlines(), 1):
        if not line.strip():
            continue
        m = re.match(r"^([0-9a-f]{64})  (.+)$", line)
        if not m:
            raise Refused(f"SHA256SUMS line {n} is not '<sha256>  <path>'")
        digest, rel = m.groups()
        parts = Path(rel).parts
        if (
            rel.startswith("/")
            or ".." in parts
            or not parts
            or parts[0] not in TREES
            or "\\" in rel
        ):
            raise Refused(f"SHA256SUMS line {n}: {rel!r} is not a path under {', '.join(TREES)}")
        if rel in sums:
            raise Refused(f"SHA256SUMS lists {rel} twice")
        sums[rel] = digest

    on_disk = set()
    for top in TREES:
        root = bundle / top
        if root.is_symlink():
            raise Refused(f"{top}/ is a symlink; the bundle holds plain files only")
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if p.is_symlink():
                raise Refused(
                    f"{p.relative_to(bundle)} is a symlink; the bundle holds plain files only"
                )
            if p.is_file():
                on_disk.add(p.relative_to(bundle).as_posix())
    unlisted = sorted(on_disk - set(sums))
    if unlisted:
        raise Refused(
            f"files not in SHA256SUMS: {', '.join(unlisted[:5])}{' …' if len(unlisted) > 5 else ''}"
        )
    absent = sorted(set(sums) - on_disk)
    if absent:
        raise Refused(f"SHA256SUMS names files that are not there: {', '.join(absent[:5])}")
    for rel, digest in sorted(sums.items()):
        if sha256(bundle / rel) != digest:
            raise Refused(f"{rel} does not match its SHA-256 in SHA256SUMS")
    if not sums:
        raise Refused("SHA256SUMS lists no source files")
    return manifest, want_tag, sums


# --- the plan -------------------------------------------------------------------------------------------------


def plan_volumes(
    bundle: Path, release: str, sums: dict[str, str], volume_size: int
) -> list[Volume]:
    """Whole files, in path order, into archives under volume_size each."""
    vols: list[Volume] = []
    cur = Volume("")
    for rel in sorted(sums):
        size = (bundle / rel).stat().st_size
        cost = tar_cost(rel, size)
        if cost + Volume("").estimate > volume_size:
            raise Refused(
                f"{rel} ({size} bytes) does not fit in one archive of --volume-size {volume_size}"
            )
        if cur.files and cur.estimate + cost > volume_size:
            vols.append(cur)
            cur = Volume("")
        cur.files.append((rel, size))
        cur.estimate += cost
    vols.append(cur)
    width = max(2, len(str(len(vols))))
    for i, v in enumerate(vols, 1):
        v.name = f"omnifile-edge-{release}-sources-{i:0{width}d}.tar"
    if len(vols) + len(LOOSE) + 1 > GITHUB_ASSETS_PER_RELEASE:
        raise Refused(f"{len(vols)} archives are more assets than one GitHub Release holds")
    return vols


def archive_mtime(manifest: dict) -> int:
    try:
        return int(
            datetime.strptime(manifest["generated"], "%Y-%m-%dT%H:%M:%SZ")
            .replace(tzinfo=UTC)
            .timestamp()
        )
    except (KeyError, ValueError):
        return 0


def write_volume(bundle: Path, vol: Volume, dest: Path, mtime: int) -> None:
    """A reproducible PAX tar: entries in order, owner root, mode 0644, the bundle's generation time."""
    tmp = dest.with_suffix(".tar.part")
    with (
        tmp.open("wb") as raw,
        tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar,
    ):
        for rel, size in vol.files:
            info = tarfile.TarInfo(rel)
            info.size = size
            info.mtime = mtime
            info.mode = 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            with (bundle / rel).open("rb") as f:
                tar.addfile(info, f)
    if tmp.stat().st_size >= GITHUB_ASSET_MAX:
        raise Refused(
            f"{vol.name} came out at {tmp.stat().st_size} bytes, over GitHub's 2 GiB asset limit"
        )
    tmp.replace(dest)


def write_assets(bundle: Path, manifest: dict, vols: list[Volume], work: Path) -> list[Path]:
    """The archives, ASSETS.SHA256SUMS and the loose files, in work/; returns every asset path."""
    work.mkdir(parents=True, exist_ok=True)
    mtime = archive_mtime(manifest)
    assets = []
    for v in vols:
        write_volume(bundle, v, work / v.name, mtime)
        assets.append(work / v.name)
    sums = "".join(f"{sha256(a)}  {a.name}\n" for a in assets)
    (work / "ASSETS.SHA256SUMS").write_text(sums)
    for name in LOOSE:
        shutil.copyfile(bundle / name, work / name)
    return assets + [work / "ASSETS.SHA256SUMS"] + [work / n for n in LOOSE]


def title(release: str) -> str:
    return f"OmniFile edge appliance {release}: corresponding source"


def commands(
    gh: str, repo: str, tag: str, release: str, assets: list[str], notes: str
) -> dict[str, list]:
    """What --execute runs, for a new release and for an existing one."""
    return {
        "create": [
            [
                gh,
                "release",
                "create",
                tag,
                *assets,
                "--repo",
                repo,
                "--title",
                title(release),
                "--notes-file",
                notes,
            ]
        ],
        "update": [
            [gh, "release", "upload", tag, *assets, "--repo", repo, "--clobber"],
            [
                gh,
                "release",
                "edit",
                tag,
                "--repo",
                repo,
                "--title",
                title(release),
                "--notes-file",
                notes,
            ],
        ],
    }


# --- gh -------------------------------------------------------------------------------------------------------


def gh_run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, text=True, capture_output=True)


def check_repo(gh: str, repo: str) -> None:
    """The sources repository must be public with Issues on: the written offer promises both to anyone."""
    r = gh_run([gh, "api", f"repos/{repo}"])
    if r.returncode != 0:
        raise Refused(f"gh api repos/{repo} failed: {r.stderr.strip() or r.stdout.strip()}")
    try:
        info = json.loads(r.stdout)
    except ValueError as e:
        raise Refused(f"gh api repos/{repo} returned something unexpected: {e}") from e
    visibility = info.get("visibility")
    if visibility != "public" or info.get("private") is not False:
        raise Refused(
            f"{repo} is {visibility or 'not public'}: the written offer promises the source to anyone, "
            "so the sources repository must be public"
        )
    if info.get("has_issues") is not True:
        raise Refused(
            f"{repo} has Issues turned off: the written offer takes requests through them; turn them on"
        )


def remote_assets(gh: str, repo: str, tag: str) -> dict[str, int] | None:
    """The release's assets by name and size, or None when the release does not exist."""
    r = gh_run([gh, "release", "view", tag, "--repo", repo, "--json", "assets"])
    if r.returncode != 0:
        if "release not found" in (r.stderr + r.stdout).lower():
            return None
        raise Refused(f"gh release view {tag} failed: {r.stderr.strip() or r.stdout.strip()}")
    try:
        return {a["name"]: int(a["size"]) for a in json.loads(r.stdout).get("assets", [])}
    except (ValueError, KeyError, TypeError) as e:
        raise Refused(f"gh release view returned something unexpected: {e}") from e


def execute(gh: str, repo: str, tag: str, release: str, assets: list[Path], prune: bool) -> None:
    names = {a.name: a.stat().st_size for a in assets}
    notes = str(next(a for a in assets if a.name == "SOURCE-OFFER.md"))
    cmds = commands(gh, repo, tag, release, [str(a) for a in assets], notes)
    check_repo(
        gh, repo
    )  # before any release call, so nothing is published where nobody can read it
    existing = remote_assets(gh, repo, tag)
    if existing is None:
        print(f"creating release {tag} in {repo}")
        steps = cmds["create"]
    else:
        stale = sorted(set(existing) - set(names))
        if stale and not prune:
            raise Refused(
                f"release {tag} has assets this bundle doesn't: {', '.join(stale)} (--prune deletes them)"
            )
        steps = [[gh, "release", "delete-asset", tag, s, "--repo", repo, "--yes"] for s in stale]
        steps += cmds["update"]
        print(
            f"updating release {tag} in {repo}"
            + (f", deleting {len(stale)} stale assets" if stale else "")
        )
    for argv in steps:
        r = gh_run(argv)
        if r.returncode != 0:
            raise Refused(f"{' '.join(argv[:4])} failed: {r.stderr.strip() or r.stdout.strip()}")
    after = remote_assets(gh, repo, tag)
    if after != names:
        raise Refused(
            f"release {tag} does not hold exactly the bundle's assets after the upload: {after}"
        )
    print(f"release {tag} in {repo}: {len(names)} assets, verified by name and size")


# --- main -----------------------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--bundle", type=Path, required=True, help="source_offer.py's --out for the release"
    )
    ap.add_argument(
        "--repo", required=True, help="OWNER/NAME of the public sources repository (decision 46)"
    )
    ap.add_argument("--tag", help="must be v<release>; given only as a check")
    ap.add_argument(
        "--work", type=Path, help="where the archives are written (--write-archives, --execute)"
    )
    ap.add_argument(
        "--volume-size", type=int, default=DEFAULT_VOLUME, help="bytes per archive, under 2 GiB"
    )
    ap.add_argument(
        "--write-archives", action="store_true", help="dry run that also writes the archives"
    )
    ap.add_argument(
        "--execute", action="store_true", help="create or update the GitHub Release with gh"
    )
    ap.add_argument(
        "--prune", action="store_true", help="with --execute: delete assets the bundle lacks"
    )
    ap.add_argument("--gh", default="gh", help="the gh executable")
    args = ap.parse_args(argv)
    if not 4 * BLOCK <= args.volume_size < GITHUB_ASSET_MAX:
        ap.error("--volume-size must be under 2 GiB")
    if (args.write_archives or args.execute) and not args.work:
        ap.error("--write-archives and --execute need --work")
    if args.work and args.work.resolve() == args.bundle.resolve():
        ap.error("--work must be another folder than --bundle")
    if args.prune and not args.execute:
        ap.error("--prune needs --execute")

    bundle = args.bundle.resolve()
    try:
        manifest, tag, sums = load_bundle(bundle, args.repo, args.tag)
        release = manifest["release"]
        vols = plan_volumes(bundle, release, sums, args.volume_size)
        total = sum(s for v in vols for _, s in v.files)
        mode = "EXECUTE" if args.execute else "DRY RUN (nothing runs gh; --execute publishes)"
        print(f"{mode}: release {tag} in https://github.com/{args.repo}")
        print(f"  {len(sums)} source files, {total / 1e6:.1f} MB, checked against SHA256SUMS")
        for v in vols:
            print(f"  {v.name}: {len(v.files)} files, about {v.estimate / 1e6:.1f} MB")
        print(f"  ASSETS.SHA256SUMS, {', '.join(LOOSE)}")
        work = args.work.resolve() if args.work else None
        if args.write_archives or args.execute:
            assets = write_assets(bundle, manifest, vols, work)
            print(f"  wrote {len(assets)} assets to {work}")
        else:
            where = work or Path("<work>")
            assets = (
                [where / v.name for v in vols]
                + [where / "ASSETS.SHA256SUMS"]
                + [where / n for n in LOOSE]
            )
        if not args.execute:
            notes = str((work or Path("<work>")) / "SOURCE-OFFER.md")
            cmds = commands(args.gh, args.repo, tag, release, [str(a) for a in assets], notes)
            print(
                f"--execute would first check `{args.gh} api repos/{args.repo}`: visibility public "
                "(not private or internal) and has_issues true, refusing otherwise"
            )
            print(
                f"--execute would run `{args.gh} release view {tag} --repo {args.repo} --json assets`, then"
            )
            print("  if the release does not exist:")
            for c in cmds["create"]:
                print("    " + " ".join(_short(c, len(assets))))
            print("  else (refusing assets the bundle lacks unless --prune):")
            for c in cmds["update"]:
                print("    " + " ".join(_short(c, len(assets))))
            return 0
        execute(args.gh, args.repo, tag, release, assets, args.prune)
        return 0
    except Refused as e:
        print(f"publish_sources.py: refused: {e}", file=sys.stderr)
        return 1


def _short(argv: list[str], n_assets: int) -> list[str]:
    """A command with its asset list folded to one '<N assets>' for printing."""
    out, i = [], 0
    while i < len(argv):
        if i == 4 and argv[2] in ("create", "upload"):
            out.append(f"<{n_assets} assets>")
            i += n_assets
            continue
        out.append(argv[i] if " " not in argv[i] else repr(argv[i]))
        i += 1
    return out


if __name__ == "__main__":
    sys.exit(main())
