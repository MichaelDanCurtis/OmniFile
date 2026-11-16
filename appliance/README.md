# The edge appliance image

Decision 38; design and spike results in `docs/design/appliance-image.md`.

The edge filer ships as a disk image: Ubuntu 26.04 LTS, minimized (decision 26), built by
[mkosi](https://github.com/systemd/mkosi) from packages, with no installer run. It updates as a
whole, A/B, with `systemd-sysupdate`, and a new version that can't hold the smb-guard's
fail-closed promise never stays booted.

## Layout

| Partition | Contents | Updated by |
|---|---|---|
| ESP (1 GiB, vfat) | systemd-boot and one unified kernel image (UKI) per installed version | sysupdate (`10-uki.transfer`) |
| usr A (3 GiB, erofs) + verity A (256 MiB) | `/usr` of one version: Ubuntu, Ganesha with `FSAL_OMNI`, Samba with `vfs_omnifile_prevver`, our binaries and units | sysupdate (`20-`, `21-`) |
| usr B + verity B | the other version, empty (`_empty`) until the first update | sysupdate |
| root (ext4, grows to fill the disk at first boot) | `/etc` and `/var`: the box's own config and state | never by an update |

The xfs data volumes the shares live on are separate disks, which the setup wizard formats.

Each UKI carries `usrhash=`, the dm-verity root hash of its own `/usr`. The initrd finds the
`/usr` partition whose UUID matches that hash, so a UKI always boots exactly the `/usr` it
was built with, read-only and verified. Our components live under `/usr` too
(`/usr/local/bin`, `/usr/lib/omnifile/ganesha`, `/usr/lib/systemd/system`), so they update
with the OS and are never out of step with it.

## Building

```sh
cargo build --release --manifest-path edge/Cargo.toml   # omnifile-edge, guardctl, smbguard-tool
cargo build --release --manifest-path edge/Cargo.toml -p omnifile-acl --example omniacl
dev/scripts/build-artifacts.sh                          # Ganesha with FSAL_OMNI (dev/.artifacts)
samba-vfs/build.sh                                      # vfs_omnifile_prevver
appliance/build.sh --version 1.2.0 --pubring appliance/keys/omnifile-updates.pgp   # once the key exists
appliance/release.sh --out appliance/build/out \
  --bin-dir edge/target/release --canary-bin-dir spikes/filer/guard/target/release \
  --source-offer build/source-offer-1.2.0     # signs with release.conf's Key Vault keys
```

`build.sh` stages our components into `build/stage`, then runs mkosi in a privileged
Ubuntu 26.04 container (docker; about 8 GB of disk), or with `--native` on an Ubuntu 26.04
host as root (`apt install mkosi systemd-repart systemd-ukify systemd-boot-efi dosfstools
mtools erofs-utils cryptsetup-bin bubblewrap debootstrap ubuntu-keyring zstd cpio kmod
patchelf`, plus `qemu-user-binfmt` to build another architecture). `--arch arm64` builds
the same image for arm64, a test build that runs the gate and the tests at native speed on
Apple silicon (`tests/vm.sh`, HVF); the components must then be arm64 builds, and the build
refuses an ELF file of another architecture. `APPLIANCE_STAGE`, `APPLIANCE_WORKSPACE` and
`APPLIANCE_PACKAGE_CACHE` move the stage, mkosi's workspace (about 6 GB, a tmpfs is fine)
and its package cache. The build is reproducible: `mkosi.conf` pins the archive snapshot and
the seed, `SOURCE_DATE_EPOCH` is set, and `BUILD-INFO` records the inputs
(`docs/design/appliance-image.md`, "Reproducible builds"). It writes into `build/out`:

- `omnifile-edge_V.raw`: the disk image, to convert for each hypervisor;
- `feed/`: what the update server serves: the UKI, `/usr` and its verity hash (zstd,
  each named with its partition UUID) and `SHA256SUMS`, unsigned. Only `release.sh` signs
  it, after the R3 gate (below);
- `omnifile-edge_V.manifest`: mkosi's JSON list of every package in the image, with its
  version, which the source offer is collected from.

The image carries AWS's IAM Roles Anywhere helper (`aws_signing_helper`, Apache-2.0) at
`/usr/libexec/omnifile/aws_signing_helper`, on the read-only `/usr`, where
`omnifile-aws-signing-helper.service` runs it. It is pinned by version and SHA-256 in
`third-party/aws_signing_helper/PIN` (AWS's published checksum): `build.sh` fetches it
(into `build/cache/`), or takes `--signing-helper FILE` for an offline build, and refuses
any file that doesn't match. Its LICENSE and NOTICE ship in
`/usr/share/omnifile/licenses/aws_signing_helper/`.

`--pubring` puts the public keyring the updates are checked against into the image, at
`/usr/lib/systemd/import-pubring.pgp`, in place of the distribution's, so the box trusts
no other key. An image built without it refuses every update.

Behind a proxy, pass it into the container: `APPLIANCE_DOCKER_ARGS="-e http_proxy=…"`.
`APPLIANCE_BUILDER_IMAGE` picks another Ubuntu 26.04 image to run mkosi in.

## Releasing: the R3 gate

A feed is signed by `release.sh` alone, and only once the new image's kernel has passed the
smb-guard A-suite and the kernel canary (risk R3: a kernel whose fanotify pre-content events
regress would let a stub read as zeros). It boots the image being released in qemu, so the
suites run on exactly its kernel, systemd, Samba and read-only `/usr`, with the test trees
and binaries on a second disk. It signs only when both suites exit 0 and every one of
their checks reports PASS (a suite that ran nothing passes nothing). Otherwise it exits 1,
removes any `SHA256SUMS.gpg`, and names the failing check. A signed feed carries `R3-GATE`,
the gate's record (kernel, checks, times, commit), covered by the signature.

`--a-suite-only` and `--canary-only` run a subset, to try the gate; a subset never signs
without `--allow-partial`, which is for test feeds with a throwaway key and is recorded in
`R3-GATE`. The full suites take hours under TCG (A6's kill loops, A8's 17,000 readers):
release where `/dev/kvm` is there. The boot-time half of the gate, the kernel check on every
boot (below), still guards a box against a kernel that slipped through.

The signed feed is bound to the disk the gate booted (`feed_binding.py`): before the boot,
the feed's `/usr` and verity files must carry that disk's partition UUIDs and its exact bytes,
and after it, the UKI the guest booted must have the feed UKI's SHA-256. So a feed from
another build of the same version can't borrow this build's pass. `R3-GATE` records the
binding. Offline tests: `python3 -m unittest discover -s appliance/tests -p 'test_*.py'`.

`release.sh --list-checks` prints the checks the gate requires.

**Signing and publishing (decisions 47 and 48; `docs/design/secure-boot.md`).** After the gate,
`release.sh` signs the UKI (in the feed and in the disk's ESP, with systemd-boot) for Secure
Boot with OmniFile's Key Vault key, then `SHA256SUMS` with the Key Vault update key
(`pgp_kv.py`: Key Vault signs only a digest). The keys are named in `release.conf`. A box
checks the feed against the keyring `build.sh --pubring` put in its image. The release's
assets land in `build/out/release-assets/` with the `gh release create` command that makes
them the latest GitHub Release of `RELEASE_REPO`, the feed every image reads by default
(`build.sh --feed-url` overrides it). Local `fake:` keys sign test feeds only.

A release also needs its source offer (below): `release.sh --source-offer DIR` refuses to sign
unless that collection's `MANIFEST.json` says it is complete for this version and its files
match their `SHA256SUMS`, and the build's `omnifile-edge_V.manifest` it was collected from is
there. `R3-GATE` records the offer's `SHA256SUMS` digest. Only a test feed
(`--allow-partial`) goes without.

## First boot: setup

A fresh image has no configuration. Its console (tty1) shows the setup questions until setup is done (`omnifile-setup`, `docs/design/setup-wizard.md`). Setup covers:

- the name and network;
- the data disk;
- the AD domain and admin group;
- private endpoints;
- the enrolment code from the portal;
- the update feed and restart window.

The edge daemon and the file servers start once the edge has enrolled.

For VM templates, pass the answers as the systemd credential `omnifile.setup`, through SMBIOS type 11 or the kernel command line, and optionally `omnifile.join-password` and `omnifile.enrol-bundle`. `omnifile-setup-auto.service` then sets the filer up unattended at first boot.

## Updates

An update feed is a directory served over HTTPS that holds every version's `feed/` files,
plus one `SHA256SUMS` over all of them, signed. The setup wizard points the box at it with
drop-ins:

```ini
# /etc/sysupdate.d/10-uki.transfer.d/10-omnifile-feed.conf (and the same for 20-usr, 21-usr-verity)
[Source]
Path=https://updates.example/omnifile-edge/
```

`systemd-sysupdate update` (in the admin's maintenance window) then:

1. checks `SHA256SUMS` against its signature and each file against `SHA256SUMS`;
2. writes the new `/usr` and its hash into the inactive slot (never the running version's:
   `ProtectVersion=%A`), labelled with the new version and with the UUIDs its UKI expects;
3. adds the new UKI with 3 tries (`omnifile-edge_V+3.efi`). systemd-boot boots the newest
   version first.

A new version is good once its boot reaches `boot-complete.target`;
`systemd-bless-boot` then drops the counter from its name. `omnifile-kernel-check.service`
is required by that target. It checks that the running kernel holds a read for a fanotify
pre-content event and honours a deny, which is what the smb-guard needs to fail closed. A
kernel that fails the check is never blessed. After three failed boots, systemd-boot goes
back to the previous version, whose `/usr` is still in the other slot.

A version that failed stays installed, marked out of tries (`omnifile-edge_V+0-3.efi`).
sysupdate won't fetch it again, so the fix ships as a newer version, which takes its slot.

## Tests

They need qemu for the disk's architecture and its UEFI firmware (`tests/vm.sh` says which),
and use KVM when it is there, HVF for an arm64 disk on a Mac, TCG otherwise. On a Mac they
also need Homebrew's bash, coreutils and swtpm, and virt-fw-vars (pip: virt-firmware). They
inject their test unit through systemd credentials, so the image under test is the one that
ships.

- `tests/smoke.sh [DISK]` boots once and checks the image's promises: `/usr` is
  verity-backed erofs and read-only, root grew, the kernel check passed, the edge unit
  waits for its config, Ganesha resolves all its libraries, smbd has the prevver module and
  loads it (`omnifile-setup check-smb-module`), and no unit failed. It checks the annex
  copier: `omnifile-annex` runs, both its units load, the preset enables the supervisor,
  which waits for setup instead of restarting, and the NAS mounts' tools are there
  (`mount.cifs`, `mount.nfs`, `cifs.upcall` with its request-key rule, `kinit`, and `rpc.gssd`,
  which an annex unit's start pulls in for NFS `sec=krb5`). A control
  run with each of those removed must find none of them; it runs in a mount namespace of its own.
  Then it checks that an annex unit, as sandboxed, mounts read-only where no other process
  sees the mount. Then it provisions the
  Roles Anywhere helper by hand (a test trust anchor, a key sealed by `systemd-creds` to the
  vTPM) and checks that it starts and serves credentials on 127.0.0.1:9911, the IMDSv2 way,
  from `tests/ra-stub.py`, a stand-in for the Roles Anywhere endpoint on the host. Nothing
  reaches AWS.
- `tests/setup.sh [DISK]` boots with an empty second disk and the answers as a credential, and checks unattended setup. It stops at the domain join, since no DC is reachable, and says why. The steps before it format and mount the disk and write the configuration, and a rerun changes nothing.
- `tests/update-round.sh --disk 0.1.0.raw --feed out-0.2.0/feed --feed out-0.3.0/feed
  --sign-key KEYID` runs a whole A/B round. 0.1.0 downloads 0.2.0 at once, then waits for
  its maintenance window (the wizard's restart window on `systemd-sysupdate-reboot.timer`,
  set a few minutes ahead, `WINDOW_DELAY`), and `systemd-sysupdate reboot` restarts it
  then, not before. 0.2.0 then updates to 0.3.0, which is made to fail its kernel check.
  0.3.0 spends its three tries, and the box falls back to 0.2.0. It signs its merged test
  feed itself with a throwaway key: 0.3.0 is bad on purpose.
- `release.sh` above, with a subset and a throwaway key, is the gate's own test.
- `tests/signed-update.sh` boots with Secure Boot enforcing and only a test certificate
  enrolled: an unsigned disk doesn't start, a release-signed one does, a release whose
  `SHA256SUMS.gpg` another key made is refused, and the signed release installs from a
  GitHub-Releases-style feed (`tests/gh-releases-stub.py`) and boots.
- `tests/sb-rotation.sh` rotates the Secure Boot key under OVMF: a UKI signed by a key the
  box's `db` lacks is refused and the box falls back by itself; the new certificate appended to
  `db`; a dual-signed UKI; and an old-signed loader once the old certificate is gone
  (`docs/design/secure-boot.md`, "Rotating a key").
- `tests/proxy.sh` takes an update through a client's HTTP proxy (squid): over HTTP and
  HTTPS it installs; a proxy that rewrites `/usr` or `SHA256SUMS` is refused.
- `tests/formats.sh DISK --boot [--v2v]` checks what `convert.sh` wrote (the OVA, its OVF
  against DMTF's schema, the capacities, `qemu-img compare`), boots the VHDX, the VMDK and
  the fixed VHD, and runs virt-v2v on the OVA.
- `tests/setup.sh --dc ENVFILE` runs unattended setup to the end: a real AD join (a Samba
  DC) and an enrolment with a running service (`dev/m4/stack.py` plus `enrol`).
- `tests/grace.sh` serves an NFS export from the appliance for WP3's
  `tests/e2e/nfs_grace_test.sh`, across a reboot and an A/B update.
- `tests/arc-sysext.sh` merges the Arc agent as a sysext (`arc/make-sysext.sh`) and starts
  it, offline.

## Source offer

The image ships GPL and LGPL binaries (the kernel, Samba, systemd, GnuPG, our Ganesha build with
`FSAL_OMNI`, `vfs_omnifile_prevver`), so each release publishes their source next to the image,
with a written offer (`LICENSES.md`, "The appliance image"). For a built image:

```sh
appliance/source-offer/source_offer.py --packages build/out/omnifile-edge_1.2.0.manifest \
  --snapshot 20261023T000000Z --release 1.2.0 --sources-repo <owner>/omnifile-gateway-sources \
  --out build/source-offer-1.2.0
```

`--sources-repo` names the public repository that publishes it (decision 46): the offer points at
its release `v1.2.0` and takes requests through its issues (`--contact` adds a postal address).
Without it the offer is marked DRAFT.

`--snapshot` is the `snapshot.ubuntu.com` id of the build date: apt reads Ubuntu's archive as it
was then (resolute, -updates and -security), so each binary package maps to the source package
and version it was built from, and `apt-get source --download-only` fetches exactly that,
checked against the signed index. It runs apt in a throwaway Ubuntu 26.04 container with an
empty dpkg status, so nothing is installed. Then it adds the Samba release the module is built
in (checked against `samba-vfs/SHA256SUMS`, with its signature), NFS-Ganesha and libntirpc at
the commits `fsal/GANESHA_VERSION` pins, and `fsal/` and `samba-vfs/` at HEAD. It writes:

- `ubuntu/`, `upstream/`, `omnifile/`: the sources;
- `packages.tsv`: binary package, version, source package, source version;
- `MANIFEST.json`: every source with its binaries and files (SHA-256 and size), and anything
  missing; `complete` is true only when every package's source is there;
- `SHA256SUMS`, and `SOURCE-OFFER.md`, the offer text (`source-offer/OFFER.md.in`), marked
  DRAFT unless complete and a sources repository is named.

It exits non-zero when a package is not in the snapshot or a fetch fails. `--packages` can be
given more than once: add the packages whose bits reach the image from elsewhere (the UKI's
systemd-stub, from the builder's `systemd-boot-efi`; the initrd's, if mkosi's default initrd
pulls in any the image lacks). `--from-config mkosi.conf` resolves the list from the config
instead (the essential set plus `Packages=`, no recommends), as a preview before an image
exists; `--only` and `--limit` fetch a subset, for a test. Offline tests of the parsing:
`python3 -m unittest discover -s appliance/source-offer`.

Tried 2026-10-21 (snapshot 20261020T000000Z): the config resolves to 277 binary packages from
168 source packages (the kernel's firmware packages among them); five were fetched (acl,
attr, kmod, tpm2-tools and samba 2:4.23.6+dfsg-1ubuntu2.2, 26 MB) with the upstream and
OmniFile sources (46 MB, the Samba tarball matching its pin). A version not in the snapshot
fails the run. The full set, from a built image's manifest, is the release task.

Publishing (decision 46): one GitHub Release per appliance release in the public sources
repository, by `publish_sources.py`. It is a dry run unless `--execute` is given:

```sh
appliance/source-offer/publish_sources.py --bundle build/source-offer-1.2.0 \
  --repo <owner>/omnifile-gateway-sources                      # checks and prints the plan
appliance/source-offer/publish_sources.py --bundle build/source-offer-1.2.0 \
  --repo <owner>/omnifile-gateway-sources --work build/publish-1.2.0 --execute
```

It refuses a bundle that is incomplete, marked DRAFT, made for another repository or tag, or
whose files don't match `SHA256SUMS` exactly (no file missing, changed or unlisted). It packs
the tree into reproducible tar archives, `omnifile-edge-<release>-sources-NN.tar`, each under
GitHub's 2 GiB asset limit (and flat names, since GitHub renames assets with `+` or `~`), lists
them in `ASSETS.SHA256SUMS`, and attaches `MANIFEST.json`, `SHA256SUMS`, `packages.tsv` and
`SOURCE-OFFER.md` (also the release notes). With `--execute` it runs `gh release create`, or
for an existing release `gh release upload --clobber` and `gh release edit` (assets the bundle
lacks are refused unless `--prune`), then reads the release back and compares names and sizes.
Republishing a tag replaces its assets, by design, so don't rerun `--execute` for a version that
has already shipped. Before any release call it checks `gh api repos/<repo>`: the repository must
be public (not private or internal) with Issues on, or it refuses.
`gh` must already be signed in with write access, and the repository needs one commit (a
README) for `gh release create` to tag. Tested offline only, with a fake `gh`
(`test_publish_sources.py`); nothing has been published.

## Files

- `mkosi.conf`: distribution, packages, the kernel command line and the initrd's modules.
- `mkosi.repart/`: the partition table above.
- `mkosi.extra/`: added to the image as-is. It holds the sysupdate transfers, the first-boot
  root growth, tmpfiles, the presets (what starts on boot), the kernel check unit, and
  DHCP until the wizard configures the network.
- `build.sh`: staging, mkosi, the feed (unsigned).
- `release.sh`: the R3 gate, then the signature.
- `feed_binding.py`: the check that the signed feed is the booted disk's.
- `third-party/aws_signing_helper/`: the helper's pin, LICENSE and NOTICE.
- `source-offer/`: the release's GPL and LGPL source collection, the written offer's text, and
  the publisher of the sources repository's GitHub Release (decision 46).
