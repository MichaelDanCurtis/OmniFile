# WP2: appliance gate and formats (results)

Branch `claude/m6-appliance-gate`. Every run was on the owner's Mac (Apple silicon, 16 CPUs) or in the Lima VM `omnifile-spike` (Ubuntu 26.04, kernel 7.0.0-38, 10 CPUs and 24 GiB, with nested virtualisation and the user-v2 network added). Nothing reached a real Azure, AWS or GitHub account: the keys are throwaway `fake:` keys, and the service is `dev/m4/stack.py`. Design notes: `docs/design/appliance-image.md` ("Architectures, reproducibility, formats and Arc") and `docs/design/secure-boot.md` ("Rotating a key").

## What was built

- **An arm64 test build** (`build.sh --arch arm64`) and HVF in `tests/vm.sh`.
  - `build.sh --native` runs mkosi on an Ubuntu 26.04 host as root, with no Docker.
  - The arm64 kernel is unwrapped from Ubuntu's stub image (`mkosi.arm64/unwrap-kernel.py`). Without that the UKI never boots.
  - `vm.sh` picks qemu, machine, firmware, console and TPM device from the disk's architecture, and KVM, HVF or TCG from the host. It runs on macOS too, with GNU coreutils.
- **Reproducible builds:**
  - `Snapshot=20261023T000000Z` (first 20261020; moved after the review for ca-certificates USN-8944-1, published 2026-10-20: only ca-certificates and openssh-server change, the kernel, Samba and systemd don't), with apt's snapshot server mapped for ports.ubuntu.com in `mkosi.sandbox/`;
  - a fixed `Seed=`;
  - `SOURCE_DATE_EPOCH`;
  - `BUILD-INFO`;
  - no build logs and no CPU microcode in the image.
- **Formats.** `convert.sh` writes a fixed VHD for Azure Gen2. `tests/formats.sh` checks the outputs, boots them, and runs virt-v2v.
- **New tests:**
  - `tests/sb-rotation.sh` (Secure Boot key rotation);
  - `tests/proxy.sh` (the update feed through squid);
  - `tests/arc-sysext.sh` with `arc/make-sysext.sh` (the Arc spike);
  - `tests/grace.sh` (the hook for WP3's grace test);
  - `tests/setup.sh --dc` (a real AD join plus enrolment).
- **The R3 gate.** `release.sh --budget-scale N` multiplies the suites' time budgets for an emulated guest (the A-suite's, the canary's, and A8's and K4's inner limits). The guest gets 8 GiB. `R3-GATE` now records per-check times and the A8 and K4 evidence. The loaders are named per architecture.
- **NFSv3 locking is on** (owner, 2026-11-04), with fixed ports and an inbound firewall (below).

## Bugs found and fixed (each with a check that fails without the fix)

1. **Every box shared one set of SSH host keys.** openssh-server's postinst made them at build time. The first two-build comparison found it. The image now ships none, and `omnifile-ssh-keygen.service` makes them at first boot. Check: smoke "the box made its own SSH host keys at first boot". It failed on the earlier image, with 3 keys older than the boot.
2. **The guard unit's vendor `.wants` link was deleted by `build.sh`.** So `systemctl disable --now omnifile-edge` would have dropped the fd store and let pending reads through as zeros. A-suite A7 found it: it failed on the appliance, and on the read-only `/usr` it now checks the image ships the link.
3. **Ganesha could never start on the appliance.**
   - Its compiled pid file and NFSv4 recovery root are under `/opt/ganesha`, the read-only `/usr`. smoke found the pid-file FATAL; WP3 found the recovery root.
   - Fixed with `ganesha/{etc,var}` linked to `/etc` and `/var`, and `/var/lib/nfs/ganesha` in tmpfiles.d.
   - Its D-Bus policy was missing ("server bus reg failed", CRIT), and is now installed.
   - Checks: smoke's Ganesha checks. They failed on the earlier image with 5 FATALs and no `/var/lib/nfs/ganesha`.
4. **The image dropped every package's copyright file** (`RemoveFiles=/usr/share/doc`). It now keeps them, and installs the written GPL source offer as `/usr/share/doc/omnifile/GPL-SOURCE-OFFER.txt`, with our modules' licences (WP7's finding). Check: smoke's two offer checks.
5. **An ordering cycle from my own SSH-key unit** (default dependencies, plus `Before=ssh.socket`). systemd broke the cycle by dropping `ssh.socket`, so from the second boot on there was no SSH. `grace.sh` found it. The unit is now an early one. Check: smoke's second boot (SSH active, `cycles=0`).
6. **x86-64 UKIs weren't reproducible.** mkosi's microcode cpio carries the build time on its directories. A VM can't load microcode, so it is removed before the UKI is built.
7. **Test harness bugs:**
   - With `pipefail`, `printf … | grep -q` failed on long output. All checks now use here-strings.
   - The arm64 OVF named guest OS id 96 (Debian 64-bit). It's now 94 (Ubuntu 64-bit).
   - virtiofs caches a negative lookup of a file the Mac just recreated, which made `release.sh`'s signing fail through my VM shims. The shims now copy files instead.

## Counts

**Smoke** (`tests/smoke.sh`):
- final arm64 build, HVF, no vTPM: **32/32**. That's 28 before NLM and the firewall, 29 with NLM, plus the rpcinfo, firewall and second-boot checks.
- x86-64, TCG with a vTPM: **32/32** on the final build (29/29 on the build before the firewall).
- The module-load check ("smbd loads the prevver module") passes on both. It is the first time it booted.

**The R3 gate on arm64 under HVF** (`release.sh`, fake keys, `--allow-partial` for the fake signer, every check):
- canary k1 to k5: **5/5 PASS** (k2, k3 and k4 for the first time);
- A-suite: **13/13 PASS**;
- signed: `R3-GATE` says `partial=no`, and gpg verifies `SHA256SUMS.gpg`. The final build (NLM, fixed ports, firewall) passed again: canary 5/5 in 93 s, A-suite 13/13 in 318 s, signed.
- **A8: 17,000 of 17,000 readers** blocked behind a stopped guard, 0 zeros, all 17,000 correct after the drain (6 s; 8 s on the final build). A8b: 3,000 readers with `LimitNOFILE=1024`: 0 zeros, the guard never exited, and 2,992 got EIO (fail-closed).
- **K4:** 17,000 readers blocked, 0 zeros, no `FAN_Q_OVERFLOW`, drained in 52 s (about 326 answers/s, risk R6); 28 s (about 607/s) on the final build.
- **x03 passes at native speed in 3 s.** The 300 s timeout under TCG was TCG's slowness, not an appliance difference.
- Times: a01 5 s, a06 70 s, a08 23 s, a13 45 s, x01 24 s. The whole A-suite is 203 to 324 s, the canary 114 to 145 s.

**A/B update round** (`tests/update-round.sh`, final arm64 build, HVF): **12/12**. 0.1.0 waits for its restart window, 0.2.0 boots and is blessed, 0.2.0 fetches 0.3.0, 0.3.0 fails its check, spends its tries and falls back to 0.2.0.

**The x86-64 gate under TCG** (the final x86 build at 3879f6d, `--budget-scale 8`, multi-threaded TCG, 4 CPUs, 8 GiB): **passed in 19 minutes**, not overnight. Canary 5/5 in 469 s (k4 362 s), A-suite 13/13 in 639 s. A8: 17,000 of 17,000 readers blocked, 0 zeros, all correct after a 53 s drain; A8b 3,000 readers, 0 zeros, 2,984 EIO. K4: 17,000 blocked, no overflow, drained in 342 s (about 49 answers/s). It signed with `partial=no`; since the review, a scaled run is refused without `--allow-partial` and recorded as partial.

**Reproducible builds:**
- arm64: three builds of 0.1.0 from the same inputs gave the same 10.2 GB disk (`cmp`). Then the final pair after the last config change matched too (SHA-256 `884f146d…`): usr, verity, UKI, initrd, manifest, ESP, root and feed.
- x86-64: two builds matched in usr, verity, initrd, manifest and root, but not the UKI (`.ucode` only, 15 bytes of cpio mtimes). After fix 6, the next pair was identical, whole disk. The final pair (NLM, ports, firewall) is identical too: `cmp` of the whole 10.2 GB disk, and all 16 parts (usr, verity, UKI, initrd, manifest, feed, every partition).

**Formats** (`tests/formats.sh`, arm64 final candidate):
- **17/17** with `--boot` on the Mac: the OVA's order, ustar format and manifest; the OVF valid against DSP8023 1.0.0; references and capacities; every virtual size; `qemu-img compare` of all four formats; the VHDX, the VMDK and the VHD each booted (12 to 13 s).
- With `--v2v` in the spike VM: virt-v2v 2.8.1 reads the OVA (source, UEFI, disks). Its conversion stops at guest inspection: the appliance's `/usr` and os-release are on a verity partition no fstab names. The image doesn't need converting anyway, since it carries virtio, pvscsi and hv_storvsc. The check now requires exactly that stop.
- **Final x86-64 build** (`convert.sh` in the spike VM, `formats.sh --v2v`): **16/16**, the static checks above plus virt-v2v's read of the OVA and its conversion stopping at inspection (171 s). The boots were run on arm64 only, to keep the one TCG slot for the x86 gate.

**Secure Boot rotation** (`tests/sb-rotation.sh`, x86-64 OVMF 2025.11 secboot, TCG):
- 1-enrol: 0.1.0 (old key) boots with Secure Boot on. 0.2.0, signed only by the new key, installs. The firmware refuses it ("Error loading EFI binary", 3 times), and the box falls back to 0.1.0 by itself (`omnifile-edge_0.2.0+0-3.efi`). The new certificate is appended to `db` (authenticated, KEK-signed, from the guest), and 0.2.0 boots and is blessed with `db` holding 2.
- 2-dual: a UKI with both keys' signatures boots on an old-only `db`, and is blessed.
- 3-old-removed: with only the new certificate in `db`, OVMF refuses the old-signed loader ("Access Denied -- rejected probably by Secure Boot") and the box has nothing to boot.
- Result: **11/11** on the final x86-64 builds (0.1.0 and 0.2.0, TCG), run 3's check included. Earlier, runs 1 and 2 passed 10/10, and the harness stopped before run 3's check (fixed).
- Under HVF, qemu's arm64 edk2 has no Secure Boot, so this test needs x86-64.

**Update feed through a proxy** (`tests/proxy.sh`, arm64, squid 7.8 on the Mac): **11/11**.
- Without the proxy, the feed is unreachable.
- Through it over HTTP, check-new sees 0.2.0.
- A `/usr` the proxy rewrote is refused (rc 1, nothing installed).
- A rewritten `SHA256SUMS` is refused by the signature ("BAD signature", nothing installed).
- Over HTTPS (CONNECT; a client CA), 0.2.0 installs and boots.
- squid's log shows the GETs, the rewrites and the CONNECT.

**setup.sh --dc** (arm64 guest under TCG in the spike VM, omnifile-dc `OMNI.TEST`, the dev stack on the Mac through a reverse SSH tunnel): **11/11**.
- Unattended setup ran every step from credentials and joined the domain (`net ads testjoin`: Join is OK; the winbind trust secret checks).
- `omni-admin` resolves to uid 20004, and its group may sudo.
- It enrolled (`edge_id=2`), and the edge, smbd, winbind and Ganesha run.
- The identity is sealed, and the api answered the edge's heartbeats (3) and feed polls (6) over mTLS.
- It left the domain at the end ("Deleted account for 'EDGE-WP2'"). The DC is otherwise unchanged.
- Rerun on the final build (NLM, ports, firewall): **11/11** again. Nested KVM in the spike VM stalls at boot, so it runs under TCG.
- The offline `setup.sh` (no DC, final arm64 build, HVF, so the keys seal to the host key): **36/36**.

**Arc spike** (`tests/arc-sysext.sh`, arm64 HVF): **6/6**.
- The sysext merges, and `/usr` becomes a read-only overlay on the verified erofs.
- The users and the state directory are made.
- `himdsd` listens on 40342.
- `azcmagent show` reports 1.69.03579.1568, Disconnected.
- An unmerge leaves `/usr` unchanged.
- Recommendation: ship the agent as a sysext (`appliance-image.md`, "Arc agent").

**Grace hook** (`tests/grace.sh`, final arm64 build, HVF): **6/6** (rebooted, boot id `64838970` to `04da1721`; A/B update to 0.2.0). WP3's `tests/e2e/nfs_grace_test.sh` is not on main (nor on any pushed branch) yet, so `--grace-test` is **pending**. The hook checks the restart paths itself: Ganesha active and in grace after a reboot and after an A/B update to 0.2.0, the records and data kept, and a port the firewall doesn't name unreachable from the host.

**Other lanes:**
- `python3 -m unittest discover -s appliance/tests -p 'test_*.py'`: 18 tests, OK (feed binding 7, with 2 new for arm64);
- `cargo test -p omnifile-setup`: 45, OK;
- `cargo test -p omnifile-edge --test units`: 4, OK.

## Review of #44 (3879f6d): 4 should-fix, 3 minor, all fixed

Each has a check that fails without its fix.
1. **Ubuntu's `nftables.service` could flush the firewall.**
   - Fix: disabled by preset, and a drop-in condition so it never starts. The network and every listener (networkd, ssh, smbd, Ganesha, rpcbind, statd) `Requires=` the firewall, so it fails closed.
   - smoke: nftables disabled; the table is there at boot and after `systemctl restart nftables`; the requirements; and with the rules made to fail, `failclosed: firewall=failed table=no networkd=inactive ssh=inactive`. The first smoke run of the new checks found the stop-based version raced (a start cancelled the stop), so it now restarts the broken firewall.
2. **An A/B update never touches `/etc`.**
   - Fix:
     - vendor links in `/usr` for the firewall and the SSH key unit (rpc.statd has no `[Install]`; Ganesha's drop-in wants it);
     - `statd` in `sysusers.d`;
     - SSH keys replaced once on a box without the `/etc/ssh/omnifile-own-host-keys` marker.
   - The update round from a pre-WP2 image (x86-64, built from 0810ac4 with its own `build.sh` and mkosi tree) found one more bug: `systemd-sysusers` never ran. Its `ConditionNeedsUpdate` compares mtimes, and reproducible builds date `/usr` by SOURCE_DATE_EPOCH. It now runs at every boot (a drop-in).
   - That first round: 15/16, failing only "the statd user". The new smoke on the build without the drop-in: 38/40 ("statd user" and the NLM check, `back= sysusers=no`).
3. **Comments at column 0 ended `Packages=` for the source offer.**
   - Fix: indented, and `config_packages` skips comment lines.
   - source-offer tests: 8 OK; 2 fail on the old parser.
4. **`release.sh` didn't bind the source bundle to the offer in the image.**
   - Fix: BUILD-INFO records `sources_repo`, and `offer_binding.py` refuses any difference in release, snapshot, sources repository or architecture from `MANIFEST.json`. The README example is fixed.
   - `test_offer_binding.py`: 7 tests.
5. `grace.sh` writes a marker in the recovery root and checks it, instead of counting entries.
6. A7 checks that the vendor link resolves to `/usr/lib/systemd/system/omnifile-edge.service`.
7. `--budget-scale` other than 1 needs `--allow-partial`, is checked to be a positive number, and is recorded as partial. `test_release_args.py`: 4 tests, which fail on the old `release.sh`.

**Counts after the review:**
- smoke, arm64 HVF: **40/40**;
- update round, arm64 0.1.0 to 0.2.0 to 0.3.0: **16/16**;
- update round from the pre-WP2 x86 image (TCG) to this branch's 0.2.0 and 0.3.0: **16/16**. Before: `fw=no statd= sshmark=no`; after the update: `fw=yes fw_unit=active nftables=inactive statd=statd sshmark=yes`, the shipped SSH key replaced once and kept through the fallback;
- grace.sh, arm64: **6/6** (the marker kept across the reboot and the A/B update);
- the R3 gate on the reviewed arm64 image (before the sysusers drop-in): canary 5/5, A-suite 13/13 (A7 with the target check), A8 17,000 drained in 18 s, K4 17,000 in 79 s, signed;
- the new smoke on the 3879f6d x86 image (TCG): **30/40**. Every new check fails there. So does the firewall's port check, because the smoke's `systemctl restart nftables` flushed the table (bug 1 itself), and the NLM check, because there's no statd user after the reboot;
- appliance unit tests: **29 OK**; source-offer: OK.

## Re-review of #44 (d6b9363): two gaps, fixed

1. **systemd-resolved could start before the firewall.**
   - Fix: the firewall is ordered `Before=sysinit.target`, and resolved requires it; LLMNR and mDNS are off in `resolved.conf.d`.
   - smoke on the d6b9363 image: `early=systemd-resolved.service`, and resolved missing from the requirements. LLMNR and mDNS were already off there (Ubuntu's defaults), so the drop-in only pins that.
   - The initrd's networkd has no `.network` for a real NIC, so a box whose firewall fails at boot has no address. One that fails later is "addressed but no listeners" (`appliance-image.md`).
2. **Ubuntu's `sshd-keygen.service` raced ours at first boot.** It is blocked by a drop-in condition. On d6b9363 it ran (`sshd_keygen=yes/active`); its pairs happened to match.
3. **`ldconfig` and the journal catalog** were skipped under reproducible mtimes (`ldconfig=no catalog=no` on d6b9363). They now run at every boot. hwdb needs nothing.

Counts:
- smoke, arm64: **45/45**;
- the new smoke on the d6b9363 image: 41/45, the four failing checks above;
- update round, arm64: **16/16**.

**Final check (af53bee):**
- The firewall's `Requires=` drop-ins never reach mkosi's default initrd, which doesn't take the image's extra tree: its cpio holds no omnifile file or drop-in.
- smoke now reads the initrd's journal up to the switch-root: 727 lines, 0 failed units, no mention of `omnifile-firewall.service`.
- smoke: **46/46**. The serial log's only `FAILED` lines are the deliberate fail-closed step in the second boot.

## NFS ports and the firewall

All inbound ports are fixed, and `omnifile-firewall.service` (nftables, `/usr/lib/omnifile/firewall.nft`, loaded before the network) drops everything else:
- 22/tcp: SSH;
- 445/tcp: SMB;
- 111/tcp+udp: rpcbind;
- 2049/tcp: NFS;
- 20048/tcp+udp: MOUNT (Ganesha `MNT_Port`);
- 32803/tcp+udp: NLM (Ganesha `NLM_Port`, `Enable_NLM = true`). Ganesha V15.6 registers NLM over TCP only;
- 32765/tcp+udp: rpc.statd (`--port 32765 --outgoing-port 32766`, a drop-in in `/usr`);
- DHCP replies and ICMP.

rquotad doesn't run. smoke checks `rpcinfo -p` against the firewall: every registered port is open.

**WP3's `RecoveryRoot` line.** With `/opt/ganesha/var` linked to `/var`, Ganesha's compiled default is already `/var/lib/nfs/ganesha`, so WP3's `RecoveryRoot = "/var/lib/nfs/ganesha";` changes nothing on the appliance. It is still right to keep, because it states the path rather than relying on the link. It needs no other change. `edge/config/ganesha/omnifile-base.conf` was edited here, outside WP2's globs, at the lead's request (NLM ports). WP3's line goes in the NFSV4 block, these in NFS_CORE_PARAM, so a merge should be clean.

## Left

- **L:**
  - dual-signing UKIs in `release.sh` during a Secure Boot key rotation;
  - a boot-loader transfer in the feed, so the old Secure Boot certificate can ever leave `db`;
  - a signed Arc sysext in the release pipeline;
  - the setup wizard has no client-proxy answer, so the proxy is set by hand in the test (a drop-in on `systemd-sysupdate.service`); outside WP2's globs;
  - WP3's grace script through `tests/grace.sh --grace-test`, across a reboot and an A/B update (NFSv3 locks included), once it is on main.
- **P:**
  - the signed x86-64 gate under KVM;
  - the fixed VHD on an Azure Gen2 VM;
  - Arc connecting;
  - the Azure Monitor Agent extension without dpkg;
  - signed-update.sh's Secure Boot checks on x86 KVM.
- **O:**
  - Arc agent redistribution (counsel);
  - the vSphere and Hyper-V imports at a client site.

## Recommendation (not built)

A guard against formatting the inactive `/usr` slot (WP8's finding: `/dev/vda4`, label `_empty`): a drop-in on `systemd-sysupdate.service` whose `ExecStartPre=` refuses when any `_empty` or inactive usr or usr-verity partition holds a filesystem signature (`blkid -p`). It would be small and in `appliance/**`. It doesn't cover a `systemd-sysupdate` run by hand, so WP8's setup-side fix stays the main one.

## Tools installed

- Homebrew on the Mac: bash 5.3.20, coreutils 9.12_1, swtpm 0.10.2_1 (libtpms 0.10.2_1), mtools 4.0.50, squid 7.8.
- uv on the Mac: virt-firmware 26.10.
- apt in omnifile-spike (Ubuntu 26.04 archive): mkosi 26-3, systemd-repart, systemd-ukify and systemd-boot-efi 259.9-0ubuntu0.1, erofs-utils 1.9.1-1, qemu-user-binfmt, ovmf and qemu-efi-aarch64 2025.11-3ubuntu7.3, python3-virt-firmware 25.12-1, swtpm 0.10.1, sbsigntool, efitools 1.9.2-5, libxml2-utils, the Samba build dependencies, gcc-x86-64-linux-gnu, qemu-system-arm 10.2.1, nfs-common 2.8.5, virt-v2v 2.8.1.
- rustup in omnifile-spike: the x86_64-unknown-linux-gnu target.
- The images now also include nfs-common and nftables (Ubuntu's).
