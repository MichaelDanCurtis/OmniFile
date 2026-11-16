# WP14 A2.3: the annex in the appliance image

Branch `claude/annex-appliance`, from main at 2079e22 (PR #51, the annex). PR #51 staged `omnifile-annex` and both of its units in `appliance/build.sh`, added `cifs-utils` and `nfs-common` to `mkosi.conf`, and enabled `omnifile-annex.service` in the preset. None of that had been built into an image or booted. This is the first build, run on 2026-11-04: an arm64 test image (`build.sh --arch arm64`), booted under HVF on Apple silicon.

## Result

- **The image builds and boots.** `appliance/tests/smoke.sh` passed **67/67** on the first PR's final image, and passes **70/70** after the review fixes ("After the review of #53", below).
  - 57 checks were already there.
  - 10 are new, for the annex (below).
  - Exit code 0.
- **The first boot found three bugs, all now fixed.** The first build found a fourth (below).
  - In the first smoke run, 61 of 66 checks passed.
  - Of the 5 that failed, 3 were product bugs: the annex supervisor's restart loop, the failed unit that loop caused, and the missing `hwdb.bin`.
  - The other 2 were a bug in the new control check itself.
- **Image size:**
  - The disk, `omnifile-edge_0.1.0.raw`, is 9.5 GiB. It is sparse, with 1.5 GiB allocated.
  - `/usr` is a 3 GiB partition. Compressed in the feed it is 1.0 GB (zstd).
  - The verity partition is 256 MiB, or 10.7 MB in the feed.
  - The UKI is 61 MB.
  - OmniFile's staged components come to 78 MB.
- **Build time:** about 7 minutes per mkosi build, in Docker Desktop's arm64 `ubuntu:26.04`.
- **Smoke time:** about 8 minutes for both boots.

## The new smoke checks (`appliance/tests/smoke.sh`)

The guest-side probe `annex_probe` reports on the annex's files. A separate line reports systemd's view of the units. The host checks:

1. `omnifile-annex` is in `/usr/local/bin`, runs (`--version`: `omnifile-annex 0.1.0`), and `ldd` finds all its libraries.
2. `omnifile-annex.service` and `omnifile-annex@.service` are installed in `/usr/lib/systemd/system`.
3. The preset enables the supervisor: its `multi-user.target.wants` link points at the unit in `/usr`.
4. The NAS mounts' tools are there:
   - `mount.cifs`, `mount.nfs` and `mount.nfs4`;
   - `cifs.upcall`, and `request-key` (keyutils, pulled in by `nfs-common`);
   - the `cifs.spnego` rule in `/etc/request-key.d/cifs.spnego.conf`, which is how the kernel reaches `cifs.upcall` for an SMB Kerberos mount;
   - `kinit`.
5. **A control.** The same probe runs again with each item removed and must find none of them:
   - each file has an empty file bound over it (on the read-only `/usr`);
   - an empty tmpfs covers the preset's `.wants` directory.

   This runs in a mount namespace of its own (`unshare -m --propagation private`), so nothing outside it changes.
6. After the control, outside that namespace, the probe's output is identical to the first run.
7. systemd loads both units (the template as `omnifile-annex@smoke`), and `systemctl is-enabled` says `enabled`.
8. `systemd-analyze verify` prints nothing for either unit.
9. **The supervisor waits for setup:** `inactive`, condition not met, 0 restarts (fix 2 below).
10. **An annex unit, as sandboxed, can mount.**
    - The test starts `omnifile-annex@smoke` with its real sandbox: capabilities, seccomp filter, `ProtectSystem=strict`, `PrivateMounts=yes`.
    - Its `ExecStart` is replaced, for this test only, by a `ro` tmpfs mount under `/run/omnifile/annex/smoke`.
    - The unit sees the mount (`tmpfs ro`), and the test shell outside it does not (`outside=0`).

**How we know each check fails when its item is missing:**
- Checks 1–4 fail under the control in step 5. That run reported `bin=none units= preset_link=no helpers= spnego=no`.
- Check 9 failed on the first image (before fix 2), as did "no failed units".
- Checks 7, 8 and 10 have no in-guest control.
  - Check 7 needs the unit files that check 2 covers.
  - Check 8 counts `systemd-analyze` output lines, so any warning fails it.
  - Check 10 needs a mount that only the sandboxed unit can see.

The first version of the control bound files over the real paths and unmounted them afterwards. That left the binary covered after the test: one bind followed the `mount.nfs4` symlink to `mount.nfs`, so `mount.nfs` was covered twice and an unmount went astray. Bash's command hash also hid the change. That gave the control's two failures in the first run. Running the control in its own namespace avoids both problems.

## Fixes

1. **The builder container had no CA store** (`appliance/build.sh`). The `Snapshot=` in `mkosi.conf` is `https://snapshot.ubuntu.com`. The bare `ubuntu:26.04` container has no `ca-certificates`, and the tool install doesn't pull it in. So apt in mkosi's sandbox failed every TLS handshake, and the first build stopped at `E: Unable to locate package base-files`. Earlier builds ran natively, on hosts that have `ca-certificates`. `ca-certificates` is now in the tool list. Test: `appliance/tests/test_build_tools.py`, `test_a_ca_store_for_the_https_snapshot`, which fails on main's `build.sh`.
2. **The annex supervisor restart-looped on a fresh box.**
   - **The bug.** `omnifile-annex.service` is enabled by the preset, `Restart=always`, `RestartSec=10`, `StartLimitIntervalSec=0`. Before setup there is no `/etc/omnifile/edge.toml` and no enrolment, so `omnifile-annex supervise` exits at once. It restarts every 10 s, forever, and smoke showed the unit `activating`, then in `systemctl --failed`.
   - **The fix.** An appliance drop-in, `mkosi.extra/usr/lib/systemd/system/omnifile-annex.service.d/05-appliance.conf`, holds the unit until `/etc/omnifile/edge.toml` and `/var/lib/omnifile/identity/identity.json` exist. It follows the pattern of `omnifile-edge.service.d/05-appliance.conf`. With the unit held at boot, setup must start it: the wizard's services step now enables and starts `omnifile-annex.service` too (`omnifile-setup/src/steps.rs`).
   - **Tests.**
     - Smoke checks 9 and "no failed units" failed on the first image and pass now.
     - `omnifile-setup/tests/steps.rs` asserts `systemctl enable --now omnifile-annex.service` in the full setup run. With the line removed from `steps.rs`, that test fails at `steps.rs:235`.
   - `cargo test -p omnifile-setup`: 53 passed. `cargo clippy -p omnifile-setup --all-targets -D warnings`: clean.
3. **No `hwdb.bin` in the image** (`appliance/build.sh`).
   - **The bug.** mkosi writes `/usr/lib/udev/hwdb.bin` with the builder's `systemd-hwdb`. That binary is in Ubuntu's `udev` package, which the container lacked, so mkosi logged "systemd-hwdb is not installed, not generating hwdb". The read-only `/usr` then had none. The existing smoke check `every_boot … hwdb_in_usr=yes` caught it.
   - **The fix.** `udev` is now in the tool list.
   - **Tests.** `test_build_tools.py`, `test_systemd_hwdb_for_the_images_hwdb_bin`, and the smoke check, which passes on the final image.

`python3 -m unittest discover -s appliance/tests -p 'test_*.py'`: 32 tests, OK. `shellcheck -S warning` on `smoke.sh` and `build.sh`: clean.

## After the review of #53

The review found nothing blocking, and three things to fix. Each fix has a test that fails without it.

1. **Licence rows for the image's new packages.** `cifs-utils` and `nfs-common` are in the `Packages=` of `mkosi.conf`, which makes them direct dependencies (`release/sbom.py`). `LICENSES.md` had no rows for them, so the release licence gate would have failed. So did `nftables`, the firewall (WP2), which the old test manifest predated.
   - **The rows.** `cifs-utils` (2:7.4) is GPL-3.0-or-later. `nfs-common` (1:2.8) is GPL-2.0-or-later AND BSD-3-Clause, the BSD part for libnfsidmap. `nftables` (1.1) is GPL-2.0-only.
   - **Their debian/copyright files**, read from the image's snapshot (20261023T000000Z):
     - `cifs-utils` and `nftables` are machine-readable (DEP-5), so they need no override.
     - `nfs-utils` is not DEP-5. Its override in `release/licence-overrides.toml` was already there, at the image's exact version, 1:2.8.5-1ubuntu1.
   - **The test manifest.** `release/testdata/appliance.manifest` is now this build's manifest: 275 packages, among them `cifs-utils`, `nfs-common` and `nftables`. `release/testdata/appliance.BUILD-INFO` is now this build's too.
   - **The test.** The new gate test, `test_every_package_the_image_names_has_a_row`, builds the appliance SBOM from that manifest. Every direct package must have a row at its version, and every deb row must be in the image. Before the rows, it listed exactly three missing: `cifs-utils`, `nfs-common` and `nftables`.
   - **Gate tests**, on the merge with main at 1ecc31f: `test_licences` 24 OK, `test_sbom` 11 OK; all of `release/tests` 108 OK; `appliance/tests` 37 OK.
2. **rpc.gssd.** An NFS annex mounts with `sec=krb5`, which needs `rpc.gssd`. Ubuntu's `rpc-gssd.service` is static (no `[Install]`). The smoke run shows its condition is checked once at boot, before any keytab exists. At that point `auth-rpcgss-module.service` and `nfs-utils.service` were what wanted it. So on a box joined to AD after it booted, nothing started it.
   - **The fix.** `omnifile-annex@.service` now has `Wants=rpc-gssd.service`, next to its `After=`.
   - **Why not the preset.** A static unit can't be enabled by a preset. Wanting it from the annex unit runs it only on an edge that has an annex, and each annex start checks its condition again. Its own `ConditionPathExists=/etc/krb5.keytab` keeps it idle until the AD join.
   - **Tests.**
     - `edge/crates/omnifile-edge/tests/units.rs`, `an_annex_unit_brings_up_rpc_gssd`, fails with the `Wants=` taken out (`units.rs:137`).
     - The same file's `the_appliance_annex_supervisor_waits_for_setup` is an offline test of fix 2 above.
     - Smoke checks that `rpc.gssd` is with the other tools, and that `rpc-gssd.service` is installed and wanted by the annex unit. The namespaced control finds neither once they are removed.
     - Smoke checks that the start of `omnifile-annex@smoke` pulled `rpc-gssd` in: its condition was checked again, and it stayed inactive with no keytab. A control unit ordered after `rpc-gssd`, but without `Wants=`, leaves it alone.
   - **Unverified** until there is a real NAS with Kerberos: that `rpc.gssd` then gets a ticket from the host keytab, and that an NFS `sec=krb5` annex mount succeeds.
3. **Minor.**
   - **The builder's udev.** `build.sh` now records the builder's `udev` version in `BUILD-INFO`, with the other tools, since its `systemd-hwdb` wrote `hwdb.bin` (`udev=259.9-0ubuntu0.1`). Test: `test_build_info_records_the_builders_udev`.
   - **The duplicate `nfs-common`.** `mkosi.conf` named `nfs-common` twice; it is now named once, with a comment that covers both uses. Test: `test_each_package_is_named_once`.

Both of those tests fail on the previous `build.sh` and `mkosi.conf`.

**Smoke after these fixes, on the merge with main at 1ecc31f:** **70/70**, exit code 0. It ran on a fresh build: the image's `/usr` changed, with the unit's `Wants=` and `mkosi.conf`. That is the 67 checks from before plus 3 new ones for `rpc.gssd`: the unit wanted, an annex start that checks it again, and its control. `rpc.gssd` also joined the tools check.
- The first try at that build failed on snapshot.ubuntu.com's 502 and 503 errors, and the retry built.
- The first smoke run on it had one failure. It came from a wrong assumption in a new check: that nothing would check `rpc-gssd` before an annex unit did. Boot already checks it, so the check now asks for a fresh check of its condition, against a control.

The new build's `BUILD-INFO` is now `release/testdata/appliance.BUILD-INFO`, with `udev=259.9-0ubuntu0.1`. Those tool versions come from the builder container's own archive, not from the image's snapshot; recording them is the reproducibility step taken here.

## Commands

Docker Desktop could not bind-mount anything under `/Volumes/Main External` during this run: "mkdir /host_mnt/Volumes/Main External: file exists". It had a stale file-sharing entry, and fixing it means restarting Docker Desktop, which would have stopped other sessions' containers. So the Docker steps ran from a copy of the branch on the internal disk, made with rsync, without `.git`. That is why `BUILD-INFO` says `commit=unknown+dirty`; the tree is this branch. `SOURCE_DATE_EPOCH` was given as HEAD's commit time (1793849119).

```sh
# Edge binaries, arm64, in the Lima VM omnifile-acl (Ubuntu 26.04), source and target under /mnt/ext/wp14-app
cargo build --release                                  # in edge/: omnifile-edge, -annex, -guardctl, smbguard-tool, -setup
cargo build --release -p omnifile-acl --example omniacl
# Ganesha + FSAL_OMNI, from the existing omnifile-ganesha:main image (arm64)
IMAGE=omnifile-ganesha:main dev/scripts/build-artifacts.sh
# vfs_omnifile_prevver for the image's Samba 4.23.6, in an arm64 ubuntu:26.04 container with its build deps
SAMBA_VERSION=4.23.6 samba-vfs/build.sh
# A throwaway update key and its keyring (the smoke test wants an OmniFile key, alone)
openssl ecparam -name prime256v1 -genkey -noout | openssl pkcs8 -topk8 -nocrypt -out update.pem
python3 appliance/pgp_kv.py pubkey --signer fake:update.pem --created 1792540800 \
  --uid "OmniFile test updates <test@omnifile.invalid>" --out update-ring.pgp
# The image (docker builder; aws_signing_helper 1.8.5 fetched and checked against its PIN)
appliance/build.sh --arch arm64 --version 0.1.0 --bin-dir BIN --artifacts dev/.artifacts \
  --prevver samba-vfs/build/omnifile_prevver.so --pubring update-ring.pgp \
  --source-date-epoch 1793849119 --out OUT
# The boot (Homebrew bash, coreutils, qemu 11.1.2, swtpm; virt-fw-vars from pip)
WORK=... appliance/tests/smoke.sh OUT/omnifile-edge_0.1.0.raw
```

`BUILD-INFO` for the final image:
- architecture arm64, snapshot `20261023T000000Z`, seed `a6fd4b8a-…`;
- mkosi 26, systemd-repart, -ukify and -boot-efi 259.9-0ubuntu0.1, erofs-utils 1.9.1-1;
- `stage_sha256=b482484650bb7882b7693967e2af61faa88cf3e5605ca9bb735bb0c208a6a3d7`.

The guest ran kernel 7.0.0-38-generic, Samba 4.23.6-Ubuntu-4.23.6+dfsg-1ubuntu2.2 and aws_signing_helper 1.8.5. The smb-guard kernel check passed: "pre-content event held the read; EIO answer honoured".

## What didn't run, and why

- **No vTPM.** qemu's arm64 TPM device can't be mapped under HVF (`tests/vm.sh`). So the Roles Anywhere key was sealed to the host key, and smoke checked `tpm=no` (as it does for every arm64 HVF run).
- **The product architecture was not built.** x86-64 under TCG on this Mac was not attempted; the arm64 test build is the one the task asked for. The image's x86-64 build, Hyper-V, vSphere and Arc wait for a host with KVM or a client site.
- **No real annex mount:**
  - There was no NAS, no AD domain and no enrolment. So no annex ran end to end on the appliance: no SMB Kerberos mount through `cifs.upcall`, no NFS `sec=krb5` through `rpc.gssd`, and no `supervise` starting a unit over D-Bus. Smoke checks that the tools and the rule are in the image, and that the unit's sandbox can mount; that is as far as the box gets offline.
  - One thing to look at when a real NAS is reachable (not tested here). `omnifile-annex@.service` has `PrivateTmp=yes`, and `kinit` writes `FILE:/tmp/krb5cc_0` inside the unit. But `cifs.upcall` is started by the kernel's request-key upcall, outside the unit. Whether it finds that cache, or gets its own ticket from the keytab, decides whether SMB Kerberos annex mounts work.
- **A/B update from an older image.** The request-key rule `cifs.spnego.conf` is in `/etc`, from `cifs-utils`. A box updated A/B from an image that predates the annex would not get it, since updates never touch `/etc`. No such image has shipped, so nothing was changed; it matters only if one ships before this.
- **The setup test** (`tests/setup.sh`) and the update round were not rerun: this change doesn't touch them beyond the services step, which `omnifile-setup`'s own tests cover.

## Free disk at the end

- Mac `/Volumes/Main External`: 2.4 TiB.
- Mac internal disk: 642 GiB.
- The Docker Desktop VM: 1.6 TiB.
- The `omnifile-acl` VM: `/mnt/ext` 2.4 TiB, root 2.5 GiB, unchanged (nothing was written there).
