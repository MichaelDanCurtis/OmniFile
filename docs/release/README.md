# Releasing OmniFile Gateway

One version, `VERSION` (SemVer: `MAJOR.MINOR.PATCH`, optionally `-alpha.N`, `-beta.N` or `-rc.N`), is the version of every part: the edge daemon and its tools (and so the heartbeat's `version` and the `OmniFile-Edge-Version` header, both `CARGO_PKG_VERSION`), the service, the relay, the portal, the appliance image and the container images. `release/runbook.sh` makes a release from one commit.

## Before a release

1. Set the version and stamp it: `release/stamp.sh --version 0.2.0`. It writes the Cargo workspace, both pyprojects (in PEP 440: `0.2.0-rc.1` is `0.2.0rc1`), the portal's `package.json` and the matching lockfile entries, and nothing else. `release/stamp.sh --check` reads them all back, with `cargo metadata` and the installed `omnifile_service.__version__` and `omnifile_relay.__version__`.
2. Write the known issues: `docs/release/known-issues.md`.
3. Commit, and have the PR merged. A release is built from a clean tree.

## The release

```sh
release/runbook.sh --out /path/to/release-0.2.0 --sources-repo <owner>/omnifile-gateway-sources \
  --registry <registry>.azurecr.io            # stops before publishing
release/runbook.sh ... --publish               # the same, then publishes
```

Each step writes a line to `RELEASE-RECORD`, and any failure stops the run:

| Step | What | Fails when |
|---|---|---|
| 1 stamp | `release/stamp.sh --check` | anything says another version |
| 2 build | the edge binaries (`omnifile-edge --version` must be `VERSION`), Ganesha, the Samba module, `appliance/build.sh` (the disk, its manifest, `initrd.cpio.zst` from mkosi, and `BUILD-INFO`, which the runbook requires) and `deploy/images/build.sh` | a build fails, or an image in the lock names another commit than this one |
| 3 offer | `appliance/source-offer/source_offer.py` from the build's manifest, BUILD-INFO and initrd; then `release/check_offer.py` | a package of the image, the initrd or the UKI's stub has no source, or a file doesn't match its checksum |
| 4 sbom | `release/sbom.py`, `release/validate_sbom.py` (CycloneDX 1.6), `release/licences.py` | an SBOM is invalid, a direct dependency has no row in `LICENSES.md` (or a row names nothing in the release), or a licence breaks the rules |
| 5 scan | `release/scan.py`: osv-scanner 2.7.0 over every lockfile, the appliance SBOM (its Ubuntu 26.04 packages, and `aws_signing_helper` as its Go module and version) and each image by its local tag (its OS layers), with `release/vuln-waivers.toml` | an unwaived HIGH, CRITICAL or unscored finding (for an OS package, only one its distribution has fixed), an expired waiver, or an image that isn't there to scan |
| 6 notes | `release/notes.sh` from the last `v*` tag: merged pull requests by area, the scan's security table, the known issues | |
| 7 gate+sign | `appliance/release.sh`: the R3 gate, then the UKI signed for Secure Boot (e_akv) and `SHA256SUMS` by the Key Vault update key (`pgp_kv.py`) | a suite fails, the feed isn't the booted disk's, or a key isn't the one boxes trust |
| 8 params | `release/bicep_params.py`: `omnifile-images-V.bicepparam`, the example parameters with the six images by digest, compiled with `bicep build-params` | an image has no digest, or the file doesn't compile |
| 9 publish | images pushed and their digests compared with the lock; `publish_sources.py --execute` on the sources repository; then `gh release create vV --draft --target <the built commit>` on `RELEASE_REPO` (the feed, SBOMs, scan, notes, parameters), and last `gh release edit vV --draft=false --latest`, so no box sees a feed before its sources are out | only with `--publish`; the commit must be on the remote |

The sources repository is `--sources-repo`, else `$OMNIFILE_SOURCES_REPO`, else `release.conf`'s `SOURCES_REPO`. A real run refuses `--images-lock`: it builds the images, and every image in the lock must name the release's commit.

**What the scan doesn't cover.** An operating-system finding that Ubuntu hasn't fixed yet is reported, not failed: a rebuild can't fix it. The base images' OS layers are scanned only when the images are built where the scan runs (a dry run with a cached lock records them as "Not scanned" in the notes' security section). The Go helper is checked by its module version, not by the dependencies compiled into the binary.

The tools are pinned in `release/tools.toml` (their SBOM is `omnifile-tools-V.cdx.json`), and the account-bound parts are the owner's: the Key Vault Premium keys (`appliance/release.conf`), the public feed and sources repositories, and the registry.

## Dry run

`release/runbook.sh --dry-run` runs every step without an account or the network (`OMNIFILE_OFFLINE=1`, `UV_OFFLINE=1`: cargo, uv and osv-scanner use their caches, so run `uv sync --locked --all-extras` once online in `service/` and `relay/` first; on a Mac, the sbsigntool image of `release/docker-tools` must be built beforehand):
- signing: the update key is a `kv:` key id answered by `release/fakes/az` (`appliance/tests/fake_az.py` and a local key), so `pgp_kv.py`'s Key Vault path runs; the UKI key is a local `fake:` key. The gate runs a quick subset unless `--full-gate`; either way with `--allow-partial`, the only way fake keys sign.
- publishing: `release/fakes/gh` is first on `PATH`. It keeps releases in `OUT/gh-releases/<owner>/<repo>/<tag>/`, which `appliance/tests/gh-releases-stub.py` serves the way GitHub Releases does, so `appliance/tests/signed-update.sh` installs exactly what the dry run published. Image pushes are written to `push-plan.txt`, not run.
- inputs it would build or fetch come from a cached build: `--appliance-out` (an `appliance/build.sh` output, changed in place by the gate, so give it a copy), `--keys` (the keys the build's keyring holds), `--source-offer` (collected beforehand), `--images-lock`, and the OSV databases (`release/scan.py --download-db --sbom <the appliance SBOM>`, once, so Ubuntu's and Go's are there too).

Credentials in the environment (`AZURE_*`, `AWS_*`, `ARM_*`, `GH_TOKEN`, `GITHUB_TOKEN`) are unset for the run.

## Checks on their own

```sh
python3 -m unittest discover -s release/tests      # the tooling's own tests
release/stamp.sh --check
python3 release/sbom.py --out build/sbom --appliance-manifest <build>/omnifile-edge_V.manifest
python3 release/validate_sbom.py build/sbom/*.cdx.json && python3 release/licences.py --sboms build/sbom
python3 release/scan.py --out build/scan
python3 release/check_offer.py --offer <offer> --manifest <build>/omnifile-edge_V.manifest
release/notes.sh v0.1.0 HEAD --security build/scan/scan.md
```

## Waivers

A waiver (`release/vuln-waivers.toml`) names the finding's id, the package, why it can't be reached or doesn't matter here, who removes it with what fix, and an expiry a release or two away. Prefer a lockfile update inside the allowed range: it needs no waiver. An expired waiver fails the scan until it is fixed or renewed with a new reason.

## The public mirror

This repo stays private, with its full history and pull requests. The public repo is a mirror that gets one
commit per publish: the tracked tree at a ref, scrubbed, with no history (`release/public_snapshot.py`).

```sh
cp release/public-scrub.example release/public-scrub.local     # once; fill it in (git-ignored)
python3 release/public_snapshot.py --repo OWNER/NAME           # build and check; prints where the tree is
python3 release/public_snapshot.py --repo OWNER/NAME --push    # publish origin/main
```

The scrub file lists the literal strings to replace or drop (the owner's test account and IAM user, private
links) and the paths to leave out (the real-run evidence). A snapshot is refused, and nothing is pushed,
when any of those strings is still in it, when gitleaks finds anything (`.gitleaks.toml` lists the known
false positives), or when a file is over 50 MB. The commit is authored with the owner's GitHub no-reply
address. Publish after a merge to `main` that should be public; nothing publishes by itself.
