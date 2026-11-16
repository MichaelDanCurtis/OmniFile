#!/usr/bin/env python3
"""Check that a collected source offer is complete for one appliance build, independently of the tool that made it.

    release/check_offer.py --offer DIR --manifest omnifile-edge_V.manifest [--build-info BUILD-INFO]
                           [--release V] [--no-initrd] [--allow-draft]

The offer (appliance/source-offer/source_offer.py) must hold the corresponding source of everything the image
ships under the GPL or LGPL; it holds the source of every package, which is more. It passes only when:
  - MANIFEST.json says complete, for this release;
  - every binary package of the image's manifest, at its exact version, has a row in packages.tsv, and that row's
    source package is in MANIFEST.json with its files;
  - the builder packages whose bits land in the image (BUILD-INFO: the UKI's stub, systemd-boot-efi) are there too;
  - the initrd was checked (source_offer.py --initrd) and no file in it came from a package without its source
    (unless --no-initrd);
  - the Samba release tarball samba-vfs/SHA256SUMS pins, NFS-Ganesha and libntirpc at the commits
    fsal/GANESHA_VERSION pins, and our fsal/ and samba-vfs/ are there;
  - every file SHA256SUMS lists is there with that SHA-256, and every file MANIFEST.json lists is in SHA256SUMS;
  - SOURCE-OFFER.md is there and not a DRAFT (unless --allow-draft).
With --offer's debian/copyright files it also counts the GPL and LGPL source packages it covered. Standard library
only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
BUILDER_PACKAGES_IN_IMAGE = ("systemd-boot-efi",)


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def kv(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text().splitlines():
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_.-]*)=(\S*)", line.strip())
        if m:
            out[m.group(1)] = m.group(2)
    return out


def check(offer: Path, manifest: Path, build_info: Path | None, release: str | None, need_initrd: bool,
          allow_draft: bool, repo: Path = REPO) -> tuple[list[str], dict]:
    problems: list[str] = []
    stats: dict = {}
    image = json.loads(manifest.read_text())
    release = release or image.get("config", {}).get("version", "")
    mf = offer / "MANIFEST.json"
    if not mf.is_file():
        return [f"no MANIFEST.json in {offer}"], stats
    m = json.loads(mf.read_text())
    if m.get("complete") is not True:
        problems.append("MANIFEST.json does not say complete "
                        f"(not_in_snapshot {len(m.get('not_in_snapshot', []))}, failed {len(m.get('failed', []))}, "
                        f"not_fetched {len(m.get('not_fetched', []))})")
    if m.get("release") != release:
        problems.append(f"the offer is for release {m.get('release')}, the build is {release}")
    # packages.tsv: binary -> source
    rows: dict[tuple[str, str], tuple[str, str]] = {}
    tsv = offer / "packages.tsv"
    if not tsv.is_file():
        problems.append("no packages.tsv")
    else:
        for line in tsv.read_text().splitlines()[1:]:
            b, bv, s, sv = line.split("\t")
            rows[(b, bv)] = (s, sv)
    sources = {(s["name"], s["version"]): s for s in m.get("sources", [])}
    wanted = [(p["name"], p["version"]) for p in image.get("packages", []) if p.get("type", "deb") == "deb"]
    if build_info:
        info = kv(build_info)
        for n in BUILDER_PACKAGES_IN_IMAGE:
            if n not in info:
                problems.append(f"{build_info.name}: no {n} version")
            else:
                wanted.append((n, info[n]))
    stats["binary_packages"] = len(wanted)
    used_sources = set()
    for b, bv in wanted:
        src = rows.get((b, bv))
        if src is None:
            problems.append(f"{b}={bv} is in the image but not in packages.tsv")
            continue
        s = sources.get(src)
        if s is None or not s.get("files"):
            problems.append(f"{b}={bv}: its source {src[0]}={src[1]} has no files in the offer")
            continue
        used_sources.add(src)
    stats["source_packages"] = len(used_sources)
    # The initrd.
    ini = m.get("initrd")
    if need_initrd:
        if not ini:
            problems.append("the initrd was not checked (source_offer.py --initrd initrd.cpio.zst)")
        elif not ini.get("files_the_archive_ships"):
            problems.append("the initrd check matched none of its files to a package: it checked nothing")
        elif ini.get("unexplained"):
            problems.append(f"{len(ini['unexplained'])} initrd files come from packages without source: "
                            + "; ".join(ini["unexplained"][:5]))
        else:
            stats["initrd_packages_not_in_the_image"] = len(ini.get("packages_not_in_the_image", []))
            for spec in ini.get("packages_not_in_the_image", []):
                b, _, bv = spec.partition("=")
                src = rows.get((b, bv))
                if src is None or not sources.get(src, {}).get("files"):
                    problems.append(f"initrd package {spec} has no source in the offer")
                else:
                    used_sources.add(src)
    # Upstream and our own.
    samba = re.search(r"^([0-9a-f]{64})\s+(samba-\d+\.\d+\.\d+\.tar\.gz)$",
                      (repo / "samba-vfs" / "SHA256SUMS").read_text(), re.M)
    pins = kv(repo / "fsal" / "GANESHA_VERSION")
    need = {
        f"upstream/{samba.group(2)}": samba.group(1),
        f"upstream/nfs-ganesha-{pins['GANESHA_COMMIT']}.tar.gz": None,
        f"upstream/ntirpc-{pins['NTIRPC_COMMIT']}.tar.gz": None,
        "omnifile/fsal.tar.gz": None,
        "omnifile/samba-vfs.tar.gz": None,
    }
    sums: dict[str, str] = {}
    sf = offer / "SHA256SUMS"
    if not sf.is_file():
        problems.append("no SHA256SUMS")
    else:
        for line in sf.read_text().splitlines():
            h, _, name = line.partition("  ")
            sums[name] = h
    for rel, digest in need.items():
        if rel not in sums or not (offer / rel).is_file():
            problems.append(f"{rel} is not in the offer")
        elif digest and sums[rel] != digest:
            problems.append(f"{rel} is not the pinned file (SHA-256 {sums[rel]}, pinned {digest})")
    # Every file is what SHA256SUMS says, and SHA256SUMS lists every file MANIFEST.json names.
    bad = 0
    for rel, h in sums.items():
        p = offer / rel
        if not p.is_file() or sha256(p) != h:
            bad += 1
            if bad <= 5:
                problems.append(f"{rel} does not match SHA256SUMS")
    if bad > 5:
        problems.append(f"... and {bad - 5} more files that don't match SHA256SUMS")
    listed = {f["path"]: f["sha256"] for s in m.get("sources", []) for f in s.get("files", [])}
    for rel, h in listed.items():
        if sums.get(rel) != h:
            problems.append(f"{rel} is in MANIFEST.json but not in SHA256SUMS with the same SHA-256")
    stats["files"] = len(sums)
    stats["bytes"] = sum((offer / r).stat().st_size for r in sums if (offer / r).is_file())
    offer_md = offer / "SOURCE-OFFER.md"
    if not offer_md.is_file():
        problems.append("no SOURCE-OFFER.md (the written offer)")
    elif "DRAFT" in offer_md.read_text() and not allow_draft:
        problems.append("SOURCE-OFFER.md is a DRAFT")
    # The copyleft ones, by their own copyright files (informational: every package is covered anyway).
    try:
        sys.path.insert(0, str(HERE))
        import sbom

        lic = sbom.offer_licences(offer)
        copyleft = {rows.get((b, bv)) for b, bv in wanted
                    if any(re.match(r"^L?GPL-", x) for x in lic.get(b, []))}
        stats["gpl_or_lgpl_source_packages"] = len({c for c in copyleft if c})
    except SystemExit:
        pass
    return problems, stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--offer", type=Path, required=True)
    ap.add_argument("--manifest", type=Path, required=True, help="the build's omnifile-edge_V.manifest")
    ap.add_argument("--build-info", type=Path, help="the build's BUILD-INFO (default: beside the manifest)")
    ap.add_argument("--release", help="default: the manifest's version")
    ap.add_argument("--no-initrd", action="store_true", help="don't require the initrd check")
    ap.add_argument("--allow-draft", action="store_true")
    args = ap.parse_args(argv)
    bi = args.build_info or args.manifest.parent / "BUILD-INFO"
    problems, stats = check(args.offer, args.manifest, bi if bi.is_file() else None, args.release,
                            not args.no_initrd, args.allow_draft)
    for p in problems:
        print(f"check_offer: {p}", file=sys.stderr)
    print("check_offer: " + ", ".join(f"{k} {v}" for k, v in stats.items()))
    print(f"check_offer: {'COMPLETE' if not problems else 'NOT COMPLETE'} for {args.manifest.name}"
          f" ({len(problems)} problem(s))")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
