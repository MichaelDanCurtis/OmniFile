# WP6: release engineering

Branch `claude/m7-release`. Mac only. Process: `docs/release/README.md`.

## Built

**One version.** `VERSION` holds 0.1.0 (the docs name no other first version). `release/stamp.sh` writes it into the Cargo workspace, both pyprojects (PEP 440), the portal's `package.json`, and the matching lockfile entries: line edits only, checked by re-parsing. `--check` reads them all back, runs `cargo metadata --locked` (every crate resolves to `VERSION`) and `uv run --locked` (the installed `__version__`), and with `--edge-bin` runs `omnifile-edge --version`.
- The heartbeat already sent `env!("CARGO_PKG_VERSION")` (`core.rs` `Health.version`, and the control client's `OmniFile-Edge-Version`), and every crate inherits the workspace version, so no Rust source changed. `release/tests/test_stamp.py` builds the daemon and checks `--version`.
- `omnifile_service.__version__` and `omnifile_relay.__version__` now read the installed package's metadata (`0+unknown` when not installed), and their tests compare it with `VERSION` (lead's approval for those three files).

**SBOMs** (`release/sbom.py`, CycloneDX 1.6 JSON, generated from the lockfiles, deterministic):
- one per lockfile git tracks (edge, the kernel canary's helper, service, relay, portal, portal/e2e; `tests/real/uv.lock` joins when #36 lands): scope required/optional/excluded, direct dependencies marked, licences from the packages' own metadata (Cargo, npm's lockfile, the installed Python distributions; four Python packages without SPDX metadata in `release/licence-overrides.toml`, marked concluded);
- the images, from `images.lock.json`: each image by digest, its base and build-stage images by digest, and the locked packages it installs. **Fixture:** `release/testdata/images.lock.json` is PR #34's lock (not merged here);
- the appliance, from mkosi's manifest plus our binaries, Ganesha and libntirpc at their pins, the Samba module and the pinned `aws_signing_helper`; with the source offer, each package's licences from its `debian/copyright`. **Fixture:** WP2's arm64 build (`out-arm64-final-a`, byte-identical to its two clean builds), `release/testdata/appliance.manifest`;
- the tools, from `release/tools.toml`.
`release/validate_sbom.py` checks each with the official `cyclonedx-python-lib` 11.12.0 strict validator.

**Licences** (`release/licences.py`). `LICENSES.md` is now one row per package (or group), with an ecosystem per table. The check passes only when every direct dependency has a row with its own licence and version, every row names something in an SBOM, and every licence, transitive ones too, follows the rules (no AGPL/SSPL/NC anywhere; shipped code permissive, MPL and Python LGPL only when named; GPL only for the separate-process components; the image's Ubuntu packages any licence but AGPL). Added: Pester 5.7.1 and `@microsoft/app-manifest` 1.1.3 (tools), and every direct dependency the check found: the portal's dev tools, the image's 40 `Packages=`, the base images, and the release tools.

**Vulnerability scan** (`release/scan.py`, osv-scanner 2.7.0, `--all-vulns`, every lockfile). An unwaived HIGH, CRITICAL or unscored finding fails, and so does an expired or incomplete waiver. `--offline` uses databases fetched once (`--download-db`), for the dry run.

**Release notes** (`release/notes.sh`): merged PRs between two refs from local git, grouped by the area with most changed files, with the scan's table and `docs/release/known-issues.md` in `docs/release/notes-template.md`.

**Runbook** (`release/runbook.sh`): stamp, build, source offer, SBOMs and licences, scan, notes, the R3 gate and signing (`appliance/release.sh`), the Bicep parameters (`release/bicep_params.py`: the six images by digest, compiled with `bicep build-params`), and publishing (`gh release create`, `publish_sources.py --execute`, image pushes checked by digest), each step in `RELEASE-RECORD`. It publishes only with `--publish`. `--dry-run` uses `release/fakes/gh` (releases kept in the layout `gh-releases-stub.py` serves), `release/fakes/az` (`fake_az.py`, so `pgp_kv.py`'s Key Vault path runs), a `fake:` UKI key, offline OSV data, and cached build, offer and images-lock inputs, recorded as cached. `release/docker-tools/` runs sbsigntool 0.9.4 in a container on a Mac.

**Source offer.** `source_offer.py` now covers the whole build, not only the image's manifest: `--build-info` adds the UKI's stub (`systemd-boot-efi`), and `--initrd` resolves mkosi 26's default initrd package list from the snapshot and looks up every initrd file in the snapshot's Contents index. `release/check_offer.py` checks an offer against a build independently.

## Results (2026-11-03, on the Mac)

**The release dry run installs.** `release/tests/dry_run_install.sh --no-secure-boot` ran on a local copy of this branch with WP2's `appliance/` (their working tree before b50ed24, never committed here), on two arm64 images WP2 built with the dry-run keyring and feed URL, under HVF:
- `runbook.sh --dry-run` for 0.1.0 and for 0.2.0: all 14 recorded steps ok. Stamp check; source offer COMPLETE; 9 SBOMs valid; licences 0 problems; scan 5 findings, 1 waived, 0 failing (offline); notes; the gate's quick subset (a01, k1; `partial=yes`) on kernel 7.0.0-38; the feed signed through `pgp_kv.py`'s Key Vault path (the fake az logged `key show` and one ES256 `key sign`), the UKI with a `fake:` key; the parameters compiled; the fake gh published the feed release and the sources release.
- `gpg --verify` of v0.2.0's `SHA256SUMS` against the image's keyring: good.
- `signed-update.sh`: the forged release was refused (`Signature verification failed`, nothing installed); v0.2.0, as the fake gh published it, installed through both GitHub-style redirects; the box rebooted into 0.2.0.
- **Pending:** its three Secure Boot checks (the unsigned disk refused, `SecureBoot=1` on 0.1.0 and 0.2.0). qemu's edk2 for arm64 under HVF does not enforce Secure Boot (WP2 checked). They need x86 OVMF (TCG, or KVM on the release host), with the same command without `--no-secure-boot`.
- Cached inputs, as the record says: the two builds (WP2's), the source offers (collected beforehand), the images lock (the #34 fixture), the OSV databases.

**The source offer is complete for the WP2 build** (`out-arm64-final-a`, byte-identical to its two clean builds and to the 0.1.0 dry-run image), collected from snapshot 20261020T000000Z: 283 binary packages, 170 source packages, all fetched, plus the Samba release, Ganesha, libntirpc, `fsal/` and `samba-vfs/`, 1.75 GB. `check_offer.py`: COMPLETE, 272 image packages, 541 files checked against `SHA256SUMS`, 101 GPL or LGPL source packages by their `debian/copyright`.
- **Found:** the initrd holds packages the image's manifest doesn't list, so the offer was incomplete for a whole build: 12 of them (btrfs-progs, dosfstools, less, dmsetup, libcryptsetup12, systemd-cryptsetup, libseccomp2, liblzo2-2, apt, libapt-pkg7.0, gpgv, ubuntu-keyring). Now in the offer, and 5,608 of the initrd's 5,665 files were matched to packages in the snapshot's Contents index, none from a package without its source. The UKI's stub (`systemd-boot-efi` 259.9) is covered by systemd's source.

**SBOMs:** edge 267 components, service 90, relay 22, portal 470, portal/e2e 4, the canary helper 2, images 181 (3 images), appliance 284, tools 15. All valid CycloneDX 1.6; two runs are byte-identical.

**Licences:** 112 rows naming 157 packages; 150 direct dependencies; 0 problems. Corrected from the packages' own data: iproute2 is GPL-2.0-only, and acl, attr and libkeyutils1 are LGPL-2.0-or-later.

**Scan, the real findings** (osv-scanner 2.7.0, 6 lockfiles, 2026-11-03): 23 findings before, 10 of them failing.
- Fixed by lockfile updates inside the allowed ranges: certifi 2026.7.22 -> 2026.10.23 (service, relay; GHSA-6q48-6c7w-9gw2, CRITICAL), rustls-webpki 0.103.16 (RUSTSEC-2026-0382), ctutils 0.4.4 (RUSTSEC-2026-0385), tokio 1.53.3 (RUSTSEC-2026-0386), rustls 0.23.46 (four MEDIUM/LOW), and in the portal's dev tree undici 8.12.2 (four HIGH) and zod 4.6.6 (one HIGH).
- ruzstd 0.8.3, RUSTSEC-2026-0344 (HIGH, decompression bomb): fixed by `ruzstd = "0.9"` in `edge/crates/omnifile-edge/Cargo.toml` (0.9.1; no code change needed; `cargo test -p omnifile-edge` and clippy pass), so the waiver is gone and `vuln-waivers.toml` has none.
- Left, reported: minimatch (3 versions, MEDIUM, portal dev; the fix is a major version) and unsafe-libyaml (unmaintained, edge dev).
- After: 4 findings (minimatch x3, unsafe-libyaml), 0 failing, 0 waivers. Online and offline scans gave the same findings.

**Lanes after the stamp:**
- `release/stamp.sh --check`: passes (files, `cargo metadata`, uv's `__version__`).
- service `uv run pytest -q` on my own Postgres 17 (port 55451, `max_connections=300`): 1250 passed, 50 skipped.
- relay `uv run pytest -q`: 126 passed.
- portal `npm ci && npm test`: 105 passed.
- edge `cargo test --workspace --locked`: 428 passed, 0 failed (macOS; the Linux-only suites run on the VM lanes). An earlier run caught `units.rs` reading the phrase "aws_signing_helper 1.8.5" from LICENSES.md; the new table keeps it.
- `python3 -m unittest discover -s release/tests`: 77 passed. `appliance/source-offer`: 39 passed. `appliance/tests` unit tests: 16 passed.
- `shellcheck -S warning` on the new scripts: clean.

## Review on #38 (at 9f750a0): fixed

Each with a test that fails without it, except where noted.
- **The tag is made on the built commit** (`gh release create --target`), and a real run refuses a commit no remote branch has. `test_runbook.py`.
- **Publishing order:** the sources first, then the feed as a draft, then `gh release edit --draft=false --latest` last. The fake gh keeps a draft out of `LATEST`. `test_runbook.py`, and the install proof checks the order in the fake gh's log.
- **The images lock:** a real run refuses `--images-lock`, and every image in the lock it builds must name this commit (`release/images_lock.py`; `test_runbook.py`). The committed lock (#34, built at 652176d) is correctly refused for a real release at HEAD and used as-is by the dry run.
- **A shipped component with no licence fails** (`licences.py` rule 5). Python licences now come from a throwaway environment with every extra and group, so the otlp extra's packages (grpcio, protobuf, opentelemetry-*) are checked. Native Debian packages' `debian/copyright` is read too. The 16 Ubuntu sources whose copyright isn't machine-readable, and the three base images, are in `release/licence-overrides.toml`, each with a licence and a reason. An appliance SBOM made without the source offer fails. `test_licences.py`.
- **OS layers are scanned:** the appliance SBOM, as Ubuntu 26.04 packages, and `aws_signing_helper` under its Go module path. Before this, osv-scanner looked it up as `github.com/aws/aws_signing_helper`, and Ganesha as a GitHub Action. Each image is scanned by its local tag. An OS finding fails only when Ubuntu has a fix for 26.04. Images not built here are a recorded gap, and they fail with `--require-images` (a real run). `test_scan.py`. Result on WP2's build: 117 OS findings with no Ubuntu fix yet, reported. One finding has a fix: ca-certificates USN-8944-1, fixed after the image's snapshot. It is waived until 2026-11-17, with the owner set as the appliance's next snapshot. The images' OS layers were not scanned here: the images aren't built on this machine. That gap is in the README and the notes template.
- **Minor:** `--stop-after` is checked against the step names. `--dry-run --full-gate` keeps `--allow-partial`. An EXIT trap always writes `result=failed` (`test_runbook.py`). The docs now match the code on BUILD-INFO, `initrd.cpio.zst`, `deploy/images/build.sh` and the sources-repository precedence. The dry run is now offline: `OMNIFILE_OFFLINE`, `UV_OFFLINE`, `cargo metadata --offline`, and a refusal when the sbsigntool image isn't built (no test: it needs Docker). The real-SBOM licence test no longer skips without a `.venv`.
- Also: ruzstd 0.9 (lead's request) removed its waiver; #35's `azure-mgmt-storage` and `azure-mgmt-core` rows are in.

## Re-review on #38 (at 3582b14): fixed

Each with a test in `release/tests/test_last_round.py`.
- `uv` comes from `$UV` or PATH and is refused unless `uv --version` is tools.toml's 0.9.26 (`release/pinned.py`; sbom.py, stamp.py, validate_sbom.py).
- Waivers may be `dry_run_only`. The ca-certificates waiver is now dry-run only, and scoped `sbom:appliance`. A real run (`scan.py --release`) fails on it: on WP2's build, 1 unwaived finding and 1 waiver problem. Findings in an SBOM or an image are keyed `sbom:<name>` and `image:<name>`, not by path.
- The record is emptied before the EXIT trap is set.
- Test- and build-only components with no licence are named in the check's output (5 on WP2's build: the image builder's tools).
- Debian overrides hold only for the source versions they were read at. python-dateutil is `Apache-2.0 AND BSD-3-Clause`.
- A release's commit must be on `origin/main` or `origin/release/*`.

## Not done

- **L** The x86 install proof with Secure Boot enforcing (`dry_run_install.sh` without `--no-secure-boot`, on x86 builds under OVMF): one x86 TCG guest at a time, and it is WP2's slot.
- **L** After #33, #34 and #36 merge: their new direct dependencies get rows (`boto3`, `awscrt`, `moto` from `tests/real/uv.lock`; #35's `azure-mgmt-storage` and `azure-mgmt-core` are in), the images SBOM reads the real `deploy/images/images.lock.json`, and the tools' pins are checked in the files that hold them. `release/licences.py` fails until the rows are there.
- **L** Merge WP2's branch and rerun the proof from the merged tree (this run used their working tree).
- **P** The real release: Key Vault Premium keys and their committed keyring and certificate, the public feed and sources repositories, the image registry (`runbook.sh --publish`).
- **O** The `e_akv` engine's version on the release host is not pinned yet (`release/tools.toml`).

## Tools installed

- osv-scanner 2.7.0 (Homebrew), and its offline databases in `release/build/osv-db` (249 MB, git-ignored).
- cyclonedx-python-lib 11.12.0 (PyPI, through `uv run --with`, not installed globally).
- `omnifile-sbsigntool:0.9.4`, a local Docker image (ubuntu:26.04 with sbsigntool 0.9.4-3.1ubuntu9), built by `release/docker-tools/sbtool`.
