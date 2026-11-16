#!/usr/bin/env python3
"""Collect the corresponding source for one appliance release (LICENSES.md, "The appliance image").

The image is a distribution of Ubuntu packages, some GPL (the kernel, Samba, parts of systemd, GnuPG), plus our
own builds of NFS-Ganesha (LGPL-3.0, with FSAL_OMNI) and vfs_omnifile_prevver (GPL-3.0-or-later). Shipping it
means offering their source (GPL-2.0 s3, GPL-3.0 s6, LGPL-3.0 s4). For a built image's package list this tool:

1. maps every binary package to its source package and version, from Ubuntu's archive as it was at the build
   (snapshot.ubuntu.com, the pinned date), never from today's archive;
2. fetches each source package (`apt-get source --download-only`: the .dsc, the upstream tarballs and the
   Debian changes, checked by apt against the signed index);
3. adds what is not an Ubuntu package: the pinned Samba release tarball the module is built in (checked against
   samba-vfs/SHA256SUMS) with its signature, the module's source (samba-vfs/), NFS-Ganesha and libntirpc at the
   commits fsal/GANESHA_VERSION pins, and fsal/ (FSAL_OMNI and the build recipe);
4. writes MANIFEST.json (binary -> source, every file with its SHA-256 and size), packages.tsv, SHA256SUMS over
   everything, and SOURCE-OFFER.md, the written offer that ships with the release.

The bundle is published as one GitHub Release per appliance release, tag v<release>, in the public sources
repository (decision 46; --sources-repo, e.g. <owner>/omnifile-gateway-sources), by publish_sources.py. The
offer names that release and takes requests through that repository's issues. Until --sources-repo is given the
offer is marked DRAFT.

apt runs in a throwaway Ubuntu 26.04 container (docker) configured for the image's architecture with an empty
dpkg status, so it only reads indexes and downloads; it installs nothing. Downloads land in --out.

    appliance/source-offer/source_offer.py --packages appliance/build/out/omnifile-edge_1.2.0.manifest \\
        --build-info appliance/build/out/BUILD-INFO --initrd appliance/build/out/initrd.cpio.zst \\
        --snapshot 20261021T000000Z --release 1.2.0 --sources-repo <owner>/omnifile-gateway-sources \\
        --out appliance/build/source-offer-1.2.0

--packages takes mkosi's JSON manifest (ManifestFormat=json, written next to the image), or text lines
`name version [arch]` (dpkg-query -W output works). It may be given more than once. Two more inputs make the
offer complete for a whole build, since bits of other packages land in it:
  --build-info BUILD-INFO   the builder packages whose bits are in the image, at the build's versions: the UKI's EFI
                            stub, from systemd-boot-efi (BUILDER_PACKAGES_IN_IMAGE)
  --initrd initrd.cpio.zst  mkosi's default initrd, which has a package list of its own and no manifest: that list
                            (MKOSI_INITRD_PACKAGES, mkosi 26's for Ubuntu) is resolved from the snapshot, and every
                            file in the initrd is looked up in the snapshot's Contents index; a file shipped by a
                            package that is in neither list makes the offer incomplete (MANIFEST.json "initrd")
--from-config resolves the package list from mkosi.conf instead (Packages= plus the essential set, without
recommends): a preview for before an image exists, not the release list.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
SNAPSHOT_RE = re.compile(r"^\d{8}T\d{6}Z$")
SUITES = ("resolute", "resolute-updates", "resolute-security")
COMPONENTS = ("main", "universe")
# The public repository whose GitHub Releases hold each release's sources (decision 46). The owner creates it at
# the first release; until --sources-repo (or OMNIFILE_SOURCES_REPO) names it, the offer is a DRAFT.
SOURCES_REPO_PLACEHOLDER = "<owner>/omnifile-gateway-sources"
REPO_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
# mkosi's default initrd, which appliance/mkosi.conf uses, is built from its own package list and mkosi writes no
# manifest for it (its mkosi.conf sets ManifestFormat= empty). This is that list for Ubuntu in mkosi 26:
# resources/mkosi-initrd/mkosi.conf and mkosi.conf.d/debian-kali-ubuntu/ (the regexes are apt's, as mkosi passes
# them). --initrd resolves it from the build's snapshot and checks the initrd's files against it.
MKOSI_INITRD_PACKAGES = (
    "systemd", "udev", "bash", "less", "gzip", "systemd-cryptsetup", "systemd-repart", "kmod", "dmsetup",
    "libcryptsetup12", "libseccomp2", "btrfs-progs", "e2fsprogs", "dosfstools", "procps", "util-linux",
    "libfido2-1", "^libtss2-esys-[0-9.]+-0(t64)?$", "^libtss2-mu[0-9.-]+(t64)?$", "^libtss2-rc0(t64)?$",
    "^libtss2-tcti-device0(t64)?$",
)
# The builder's packages whose bits land in the image: the UKI's EFI stub (systemd-ukify takes it from
# systemd-boot-efi). Their versions are in the build's BUILD-INFO.
BUILDER_PACKAGES_IN_IMAGE = ("systemd-boot-efi",)


def release_tag(release: str) -> str:
    """The sources repository's tag for an appliance release: one GitHub Release each."""
    return f"v{release}"


def repo_is_set(repo: str) -> bool:
    return (
        repo != SOURCES_REPO_PLACEHOLDER
        and bool(REPO_RE.match(repo))
        and repo.split("/")[1].strip(".") != ""
    )


@dataclass(frozen=True)
class Binary:
    name: str
    version: str
    arch: str = ""


@dataclass
class Source:
    name: str
    version: str
    origin: str  # "ubuntu" | "upstream" | "omnifile"
    binaries: list[str] = field(default_factory=list)
    files: list[dict] = field(default_factory=list)
    note: str = ""


# --- inputs -------------------------------------------------------------------------------------------------


def read_packages(path: Path) -> list[Binary]:
    """mkosi's JSON manifest, or `name version [arch]` lines (`name:arch` is split; # starts a comment)."""
    text = path.read_text()
    if text.lstrip().startswith("{"):
        data = json.loads(text)
        return [
            Binary(p["name"], p["version"], p.get("architecture", ""))
            for p in data.get("packages", [])
            if p.get("type", "deb") == "deb"
        ]
    out = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            raise SystemExit(f"{path}: not 'name version [arch]': {line!r}")
        name, _, arch = parts[0].partition(":")
        out.append(Binary(name, parts[1], parts[2] if len(parts) > 2 else arch))
    return out


def config_packages(conf: Path) -> list[str]:
    """The Packages= list of a mkosi.conf (continuation lines are indented)."""
    names: list[str] = []
    in_list = False
    for raw in conf.read_text().splitlines():
        if raw.lstrip().startswith("#"):
            continue  # a comment, at any column, neither adds names nor ends the list
        if raw.startswith("Packages="):
            in_list = True
            names += raw.split("=", 1)[1].split()
        elif in_list and raw[:1].isspace() and raw.strip():
            names += raw.split()
        elif in_list and not raw[:1].isspace():
            in_list = False
    return names


def parse_records(text: str) -> list[dict[str, str]]:
    """Debian control records (apt-cache show), first line of each field only."""
    out, cur = [], {}
    for line in text.splitlines():
        if not line.strip():
            if cur:
                out.append(cur)
            cur = {}
        elif not line[:1].isspace() and ":" in line:
            k, v = line.split(":", 1)
            cur[k] = v.strip()
    if cur:
        out.append(cur)
    return out


def source_of(record: dict[str, str]) -> tuple[str, str]:
    """(source name, source version) of a binary record: `Source: s (v)`, `Source: s`, or the binary itself."""
    src = record.get("Source", "")
    if not src:
        return record["Package"], record["Version"]
    m = re.match(r"^(\S+)(?:\s+\((\S+)\))?$", src)
    if not m:
        raise ValueError(f"unreadable Source field {src!r} for {record.get('Package')}")
    return m.group(1), m.group(2) or record["Version"]


# --- apt in a container ---------------------------------------------------------------------------------------


class Apt:
    """A throwaway Ubuntu container whose apt reads the archive as it was at `snapshot`, for `arch`."""

    def __init__(self, image: str, snapshot: str, arch: str, src_dir: Path, ca_bundle: Path | None):
        self.name = f"omnifile-source-offer-{os.getpid()}-{int(time.time())}"
        args = ["docker", "run", "-d", "--rm", "--name", self.name, "-v", f"{src_dir}:/src"]
        if ca_bundle:
            args += ["-v", f"{ca_bundle}:/etc/ssl/certs/ca-certificates.crt:ro"]
        subprocess.run([*args, image, "sleep", "infinity"], check=True, stdout=subprocess.DEVNULL)
        uri = f"https://snapshot.ubuntu.com/ubuntu/{snapshot}/"
        # The snapshot service is HTTPS only, and the base image has no CA certificates: take them from the
        # image's own archive first (apt checks every index and file against the archive's signature anyway).
        bootstrap = (
            ""
            if ca_bundle
            else (
                "apt-get update -qq --error-on=any && "
                "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ca-certificates >/dev/null"
            )
        )
        setup = f"""
set -e
{bootstrap}
rm -f /etc/apt/sources.list /etc/apt/sources.list.d/* /var/lib/apt/lists/*_*
cat >/etc/apt/sources.list.d/snapshot.sources <<EOF
Types: deb deb-src
URIs: {uri}
Suites: {" ".join(SUITES)}
Components: {" ".join(COMPONENTS)}
Architectures: {arch}
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
EOF
: >/tmp/empty-status
cat >/etc/apt/apt.conf.d/99source-offer <<EOF
APT::Architecture "{arch}";
APT::Architectures {{ "{arch}"; }};
Dir::State::status "/tmp/empty-status";
APT::Install-Recommends "false";
Acquire::Retries "3";
EOF
apt-get update -qq --error-on=any
"""
        self.sh(setup)

    def sh(self, script: str, *, check: bool = True) -> subprocess.CompletedProcess:
        res = subprocess.run(
            ["docker", "exec", "-i", self.name, "bash", "-c", script],
            capture_output=True,
            text=True,
        )
        if check and res.returncode != 0:
            raise SystemExit(
                f"in the apt container: {script.strip()[:200]}\n{res.stderr.strip()[-2000:]}"
            )
        return res

    def show(self, binaries: list[Binary]) -> tuple[dict[Binary, tuple[str, str]], list[Binary]]:
        """Each binary's (source, source version); and the ones the snapshot doesn't have."""
        found: dict[Binary, tuple[str, str]] = {}
        missing: list[Binary] = []
        for i in range(0, len(binaries), 200):
            batch = binaries[i : i + 200]
            specs = " ".join(shlex.quote(f"{b.name}={b.version}") for b in batch)
            res = self.sh(f"apt-cache show {specs}", check=False)
            recs = {(r["Package"], r["Version"]): r for r in parse_records(res.stdout)}
            for b in batch:
                r = recs.get((b.name, b.version))
                if r is None:
                    missing.append(b)
                else:
                    found[b] = source_of(r)
        return found, missing

    def resolve(self, packages: list[str]) -> list[Binary]:
        """What installing `packages` (and the essential set) from nothing would install, without recommends."""
        names = " ".join(shlex.quote(p) for p in packages)
        res = self.sh(f"apt-get install -s -qq '?essential' {names}")
        out = []
        for line in res.stdout.splitlines():
            m = re.match(r"^Inst (\S+) \((\S+) .*\[(\S+)\]\)", line)
            if m:
                out.append(Binary(m.group(1), m.group(2), m.group(3)))
        return out

    def enable_contents(self) -> None:
        """Download the snapshot's Contents indexes too (which package ships each path), for --initrd. Ubuntu has
        one per suite and architecture (dists/<suite>/Contents-<arch>), checked by apt against the signed Release
        like every other index."""
        self.sh(
            "cat >/etc/apt/apt.conf.d/98contents <<'EOF'\n"
            "Acquire::IndexTargets::deb::Contents-deb {\n"
            '    MetaKey "Contents-$(ARCHITECTURE)";\n'
            '    ShortDescription "Contents-$(ARCHITECTURE)";\n'
            '    Description "$(RELEASE) $(ARCHITECTURE) Contents";\n'
            '    flatMetaKey "Contents-$(ARCHITECTURE)";\n'
            '    flatDescription "$(RELEASE) Contents";\n'
            '    KeepCompressed "true";\n'
            '    DefaultEnabled "true";\n'
            "};\n"
            "EOF\n"
            "apt-get update -qq --error-on=any\n"
            "ls /var/lib/apt/lists/*Contents-* >/dev/null"
        )

    def owners(self, paths: list[str], scratch: Path) -> dict[str, set[str]]:
        """Each path's packages, from the Contents indexes; paths the archive doesn't ship are left out."""
        (scratch / "initrd-files.txt").write_text("".join(p + "\n" for p in paths))
        res = self.sh(
            "for f in /var/lib/apt/lists/*Contents-*; do /usr/lib/apt/apt-helper cat-file \"$f\"; done | "
            "awk 'NR==FNR { want[$0] = 1; next } "
            "{ pk = $NF; p = substr($0, 1, length($0) - length(pk)); sub(/[ \\t]+$/, \"\", p); "
            "if (p in want) print p \"\\t\" pk }' /src/initrd-files.txt -"
        )
        out: dict[str, set[str]] = {}
        for line in res.stdout.splitlines():
            path, _, pkgs = line.partition("\t")
            out.setdefault(path, set()).update(x.rsplit("/", 1)[-1] for x in pkgs.split(","))
        (scratch / "initrd-files.txt").unlink()
        return out

    def download_source(self, name: str, version: str) -> tuple[list[str], str]:
        """Fetch one source package into /src/<name>_<version>/; returns its file names, and an error if any."""
        d = f"/src/ubuntu/{name}_{version}"
        res = self.sh(
            f"mkdir -p {shlex.quote(d)} && cd {shlex.quote(d)} && "
            f"apt-get source --download-only --only-source -qq {shlex.quote(f'{name}={version}')} && ls -1",
            check=False,
        )
        if res.returncode != 0:
            return [], (res.stderr.strip() or res.stdout.strip())[-500:]
        return [f for f in res.stdout.split() if f], ""

    def close(self) -> None:
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True)


def cpio_regular_files(data: bytes) -> list[str]:
    """The regular files of one or more concatenated newc cpio archives (an initrd)."""
    out, pos = [], 0
    while pos + 110 <= len(data):
        if data[pos : pos + 6] not in (b"070701", b"070702"):
            if data[pos : pos + 1] == b"\0":  # padding between archives
                pos += 1
                continue
            raise SystemExit(f"initrd: not a newc cpio archive at byte {pos}")
        f = [int(data[pos + 6 + 8 * i : pos + 14 + 8 * i], 16) for i in range(13)]
        mode, size, namesize = f[1], f[6], f[11]
        name = data[pos + 110 : pos + 110 + namesize - 1].decode("utf-8", "replace")
        pos = (pos + 110 + namesize + 3) & ~3
        if name == "TRAILER!!!":
            pos = (pos + size + 3) & ~3
            continue
        if mode & 0o170000 == 0o100000:
            out.append(name.lstrip("./"))
        pos = (pos + size + 3) & ~3
    return sorted(set(out))


def initrd_files(path: Path) -> list[str]:
    data = path.read_bytes()
    if data[:4] == b"\x28\xb5\x2f\xfd":
        data = subprocess.run(["zstd", "-dc", str(path)], check=True, capture_output=True).stdout
    return cpio_regular_files(data)


def build_info(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text().splitlines():
        k, sep, v = line.partition("=")
        if sep:
            out[k.strip()] = v.strip()
    return out


# --- upstream and our own -------------------------------------------------------------------------------------


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(url: str, dest: Path) -> None:
    if dest.exists():
        return
    part = dest.with_suffix(dest.suffix + ".part")
    subprocess.run(["curl", "-sSfL", "--retry", "3", "-o", str(part), url], check=True)
    part.rename(dest)


def pinned_samba() -> tuple[str, str]:
    """(version, sha256) of the Samba release samba-vfs/SHA256SUMS pins (the newest one, if several)."""
    pins = []
    for line in (REPO / "samba-vfs" / "SHA256SUMS").read_text().splitlines():
        m = re.match(r"^([0-9a-f]{64})\s+samba-(\d+\.\d+\.\d+)\.tar\.gz$", line.strip())
        if m:
            pins.append((tuple(int(x) for x in m.group(2).split(".")), m.group(2), m.group(1)))
    if not pins:
        raise SystemExit("samba-vfs/SHA256SUMS pins no Samba release")
    _, ver, digest = max(pins)
    return ver, digest


def ganesha_pins() -> dict[str, str]:
    pins = {}
    for line in (REPO / "fsal" / "GANESHA_VERSION").read_text().splitlines():
        m = re.match(r"^([A-Z_]+)=(\S+)$", line.strip())
        if m:
            pins[m.group(1)] = m.group(2)
    return pins


def git_archive(paths: list[str], dest: Path, prefix: str) -> str:
    """A tarball of `paths` at HEAD; returns the commit (with +dirty if the tree differs)."""
    commit = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "-C", str(REPO), "diff", "--quiet", "HEAD", "--", *paths]
    ).returncode
    subprocess.run(
        [
            "git",
            "-C",
            str(REPO),
            "archive",
            "--format=tar.gz",
            f"--prefix={prefix}/",
            "-o",
            str(dest),
            "HEAD",
            *paths,
        ],
        check=True,
    )
    return commit + ("+dirty" if dirty else "")


def upstream(out: Path, samba_dir: Path | None) -> list[Source]:
    """What is not an Ubuntu package: the Samba release the module is built in, NFS-Ganesha, and our own two."""
    up, ours = out / "upstream", out / "omnifile"
    up.mkdir(parents=True, exist_ok=True)
    ours.mkdir(parents=True, exist_ok=True)

    def files(*paths: Path) -> list[dict]:
        return [file_entry(out, p.relative_to(out)) for p in paths]

    ver, digest = pinned_samba()
    tar = up / f"samba-{ver}.tar.gz"
    if samba_dir and (samba_dir / tar.name).exists() and not tar.exists():
        tar.write_bytes((samba_dir / tar.name).read_bytes())
    fetch(f"https://download.samba.org/pub/samba/stable/{tar.name}", tar)
    if sha256(tar) != digest:
        tar.rename(tar.with_suffix(".mismatch"))
        raise SystemExit(f"{tar.name} does not match its pin in samba-vfs/SHA256SUMS")
    asc = up / f"samba-{ver}.tar.asc"
    fetch(f"https://download.samba.org/pub/samba/stable/{asc.name}", asc)

    pins = ganesha_pins()
    gan = up / f"nfs-ganesha-{pins['GANESHA_COMMIT']}.tar.gz"
    fetch(
        f"https://codeload.github.com/nfs-ganesha/nfs-ganesha/tar.gz/{pins['GANESHA_COMMIT']}", gan
    )
    rpc = up / f"ntirpc-{pins['NTIRPC_COMMIT']}.tar.gz"
    fetch(f"https://codeload.github.com/nfs-ganesha/ntirpc/tar.gz/{pins['NTIRPC_COMMIT']}", rpc)

    vfs_commit = git_archive(["samba-vfs"], ours / "samba-vfs.tar.gz", "samba-vfs")
    fsal_commit = git_archive(["fsal"], ours / "fsal.tar.gz", "fsal")
    return [
        Source(
            "samba (upstream release)",
            ver,
            "upstream",
            ["the vfs_omnifile_prevver build tree"],
            files(tar, asc),
            "the module is compiled inside this release (samba-vfs/build.sh); "
            "the signature is Samba's, key AA99442FB680B620",
        ),
        Source(
            "nfs-ganesha",
            f"{pins['GANESHA_TAG']} ({pins['GANESHA_COMMIT']})",
            "upstream",
            ["/usr/lib/omnifile/ganesha"],
            files(gan, rpc),
            "LGPL-3.0; libntirpc is its src/libntirpc submodule; built by fsal/build/Containerfile",
        ),
        Source(
            "vfs_omnifile_prevver (samba-vfs/)",
            vfs_commit,
            "omnifile",
            ["<samba MODULESDIR>/vfs/omnifile_prevver.so"],
            files(ours / "samba-vfs.tar.gz"),
            "GPL-3.0-or-later (decision 36)",
        ),
        Source(
            "FSAL_OMNI (fsal/)",
            fsal_commit,
            "omnifile",
            ["/usr/lib/omnifile/ganesha/lib/ganesha/libfsalomni.so"],
            files(ours / "fsal.tar.gz"),
            "LGPL-3.0, with the Ganesha build recipe (fsal/build/Containerfile, fsal/GANESHA_VERSION)",
        ),
    ]


def file_entry(out: Path, rel: Path) -> dict:
    p = out / rel
    return {"path": str(rel), "sha256": sha256(p), "size": p.stat().st_size}


# --- the offer ------------------------------------------------------------------------------------------------


def write_offer(
    out: Path,
    release: str,
    snapshot: str,
    sources_repo: str,
    sources: list[Source],
    contact: str = "",
) -> None:
    """SOURCE-OFFER.md: where the release's source is (its GitHub Release) and the written offer (decision 46)."""
    template = (HERE / "OFFER.md.in").read_text()
    ubuntu = sum(1 for s in sources if s.origin == "ubuntu")
    total = sum(f["size"] for s in sources for f in s.files)
    extra = f"Or write to:\n\n{contact.strip()}\n\n" if contact.strip() else ""
    text = (
        template.replace("{{RELEASE}}", release)
        .replace("{{TAG}}", release_tag(release))
        .replace("{{SOURCES_REPO}}", sources_repo)
        .replace("{{SNAPSHOT}}", snapshot)
        .replace("{{CONTACT_EXTRA}}", extra)
        .replace("{{UBUNTU_SOURCES}}", str(ubuntu))
        .replace("{{TOTAL_MB}}", f"{total / 1e6:.0f}")
        .replace("{{DATE}}", datetime.now(UTC).strftime("%Y-%m-%d"))
    )
    (out / "SOURCE-OFFER.md").write_text(text)


def mark_draft(offer: Path, complete: bool, sources_repo: str) -> None:
    """Prefix the offer with why it must not ship: sources missing, or no sources repository named."""
    notes = []
    if not complete:
        notes.append(
            "> **DRAFT, INCOMPLETE: not every package's source is here (MANIFEST.json). Do not ship.**"
        )
    if not repo_is_set(sources_repo):
        notes.append(
            "> **DRAFT: the sources repository is not set (--sources-repo, decision 46). Do not ship.**"
        )
    if notes:
        offer.write_text("\n".join(notes) + "\n\n" + offer.read_text())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument(
        "--packages", type=Path, action="append", help="mkosi JSON manifest or 'name version' lines"
    )
    g.add_argument(
        "--from-config", type=Path, help="preview: resolve mkosi.conf's Packages= instead"
    )
    ap.add_argument(
        "--snapshot",
        required=True,
        help="snapshot.ubuntu.com id of the build, e.g. 20261021T000000Z",
    )
    ap.add_argument("--release", required=True, help="the appliance version this offer is for")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--arch", default="amd64")
    ap.add_argument("--image", default=os.environ.get("APPLIANCE_BUILDER_IMAGE", "ubuntu:26.04"))
    ap.add_argument(
        "--sources-repo",
        default=os.environ.get("OMNIFILE_SOURCES_REPO", SOURCES_REPO_PLACEHOLDER),
        help="OWNER/NAME of the public sources repository whose release v<release> holds this bundle "
        "and whose issues take requests (decision 46); default $OMNIFILE_SOURCES_REPO",
    )
    ap.add_argument(
        "--contact",
        default="",
        help="an extra postal or mail address for the written offer (optional)",
    )
    ap.add_argument(
        "--samba-tarball-dir", type=Path, help="where samba-<v>.tar.gz already is (else fetched)"
    )
    ap.add_argument(
        "--only", default="", help="comma-separated source packages to fetch (a test subset)"
    )
    ap.add_argument(
        "--limit", type=int, default=0, help="fetch only the first N source packages (a test)"
    )
    ap.add_argument(
        "--no-upstream", action="store_true", help="skip Samba, Ganesha and our module sources"
    )
    ap.add_argument(
        "--build-info",
        type=Path,
        help="the build's BUILD-INFO: adds the builder packages whose bits land in the image (the UKI's stub)",
    )
    ap.add_argument(
        "--initrd",
        type=Path,
        help="the build's initrd (initrd.cpio.zst): adds mkosi's default initrd packages, resolved from the "
        "snapshot, and checks every file in it is from those or the image's packages",
    )
    ap.add_argument(
        "--ca-bundle",
        type=Path,
        help="a PEM bundle to mount as the container's CA certificates (a site's TLS proxy); "
        "default: install ca-certificates from the base image's archive",
    )
    args = ap.parse_args(argv)
    if not SNAPSHOT_RE.match(args.snapshot):
        ap.error("--snapshot must look like 20261021T000000Z")
    if args.sources_repo != SOURCES_REPO_PLACEHOLDER and not repo_is_set(args.sources_repo):
        ap.error("--sources-repo must be OWNER/NAME, e.g. ailuxis/omnifile-gateway-sources")

    out = args.out.resolve()
    (out / "ubuntu").mkdir(parents=True, exist_ok=True)
    apt = Apt(args.image, args.snapshot, args.arch, out, args.ca_bundle)
    try:
        if args.from_config:
            binaries = apt.resolve(config_packages(args.from_config))
            preview = f"resolved from {args.from_config.name} (preview, not a built image's list)"
        else:
            binaries = sorted(
                {b for p in args.packages for b in read_packages(p)}, key=lambda b: b.name
            )
            preview = ""
        extra_lists: dict[str, object] = {}
        if args.build_info:
            info = build_info(args.build_info)
            builder = [Binary(n, info[n], args.arch) for n in BUILDER_PACKAGES_IN_IMAGE if n in info]
            lacking = [n for n in BUILDER_PACKAGES_IN_IMAGE if n not in info]
            if lacking:
                raise SystemExit(f"{args.build_info}: no version for {', '.join(lacking)}")
            binaries = sorted(set(binaries) | set(builder), key=lambda b: b.name)
            extra_lists["builder"] = [f"{b.name}={b.version}" for b in builder]
        initrd_report = None
        if args.initrd:
            apt.enable_contents()
            initrd_bins = apt.resolve(list(MKOSI_INITRD_PACKAGES))
            files = initrd_files(args.initrd)
            owners = apt.owners(files, out)
            if files and not owners:
                raise SystemExit("--initrd: none of the initrd's files is in the archive's Contents index: the lookup "
                                 "failed, so the initrd is not checked")
            known = {b.name for b in binaries} | {b.name for b in initrd_bins}
            unexplained = sorted(f"{p} ({', '.join(sorted(o))})" for p, o in owners.items() if not o & known)
            # The initrd is built from the same snapshot: a package the image also has is the image's version.
            have = {b.name for b in binaries}
            added = [b for b in initrd_bins if b.name not in have]
            binaries = sorted(set(binaries) | set(added), key=lambda b: b.name)
            initrd_report = {
                "file": args.initrd.name,
                "regular_files": len(files),
                "files_the_archive_ships": len(owners),
                "packages_resolved": len(initrd_bins),
                "packages_not_in_the_image": [f"{b.name}={b.version}" for b in added],
                "unexplained": unexplained,
            }
        mapped, missing = apt.show(binaries)
        by_source: dict[tuple[str, str], Source] = {}
        for b, (sname, sver) in sorted(mapped.items(), key=lambda kv: kv[0].name):
            by_source.setdefault((sname, sver), Source(sname, sver, "ubuntu")).binaries.append(
                f"{b.name}={b.version}"
            )
        sources = sorted(by_source.values(), key=lambda s: s.name)
        wanted = sources
        if args.only:
            only = {x.strip() for x in args.only.split(",") if x.strip()}
            wanted = [s for s in sources if s.name in only]
        if args.limit:
            wanted = wanted[: args.limit]
        failed = []
        for s in wanted:
            names, err = apt.download_source(s.name, s.version)
            if err:
                failed.append({"source": s.name, "version": s.version, "error": err})
                continue
            s.files = [file_entry(out, Path("ubuntu") / f"{s.name}_{s.version}" / n) for n in names]
    finally:
        apt.close()

    extra = [] if args.no_upstream else upstream(out, args.samba_tarball_dir)
    everything = [s for s in sources if s.files] + extra
    manifest = {
        "release": args.release,
        "snapshot": args.snapshot,
        "archive": f"https://snapshot.ubuntu.com/ubuntu/{args.snapshot}/",
        "suites": list(SUITES),
        "architecture": args.arch,
        "sources_repo": args.sources_repo if repo_is_set(args.sources_repo) else "",
        "release_tag": release_tag(args.release),
        "generated": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "package_list": preview or [str(p) for p in (args.packages or [])],
        "binaries": len(binaries),
        "source_packages": len(sources),
        "fetched": len([s for s in sources if s.files]),
        "complete": not missing
        and not failed
        and len(wanted) == len(sources)
        and not args.no_upstream
        and not (initrd_report and initrd_report["unexplained"]),
        "builder_packages": extra_lists.get("builder", []),
        "initrd": initrd_report,
        "not_in_snapshot": [f"{b.name}={b.version}" for b in missing],
        "failed": failed,
        "not_fetched": [
            f"{s.name}={s.version}" for s in sources if not s.files and s not in wanted
        ],
        "sources": [vars(s) for s in sources + extra],
    }
    (out / "MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n")
    with (out / "packages.tsv").open("w") as f:
        f.write("binary\tversion\tsource\tsource_version\n")
        for b, (sn, sv) in sorted(mapped.items(), key=lambda kv: kv[0].name):
            f.write(f"{b.name}\t{b.version}\t{sn}\t{sv}\n")
    with (out / "SHA256SUMS").open("w") as f:
        for s in everything:
            for e in s.files:
                f.write(f"{e['sha256']}  {e['path']}\n")
    write_offer(out, args.release, args.snapshot, args.sources_repo, everything, args.contact)
    mark_draft(out / "SOURCE-OFFER.md", manifest["complete"], args.sources_repo)

    size = sum(e["size"] for s in everything for e in s.files)
    print(
        f"binaries {len(binaries)}, source packages {len(sources)}, fetched {manifest['fetched']}"
        f" + {len(extra)} upstream/ours, {size / 1e6:.1f} MB in {out}"
    )
    for b in missing:
        print(f"NOT IN SNAPSHOT {b.name}={b.version}", file=sys.stderr)
    for e in failed:
        print(f"FAILED {e['source']}={e['version']}: {e['error']}", file=sys.stderr)
    for f in (initrd_report or {}).get("unexplained", []):
        print(f"INITRD FILE FROM AN UNLISTED PACKAGE {f}", file=sys.stderr)
    if manifest["complete"]:
        print("complete: every binary package has its source")
    else:
        print(
            "INCOMPLETE: not a release offer (see MANIFEST.json not_in_snapshot / failed / not_fetched)"
        )
    return 1 if (missing or failed or (initrd_report and initrd_report["unexplained"])) else 0


if __name__ == "__main__":
    sys.exit(main())
