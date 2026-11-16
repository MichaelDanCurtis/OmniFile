# Appliance image: how to build Ubuntu 26.04 minimized (M6)

**Status: BUILT (spike, 2026-10-15).** The owner picked route A, mkosi with A/B updates (decision 38). The image definition, build and tests are in `appliance/` (its README is the operator's view). The spike results are below, under "Spike results". This note keeps the comparison that led to the pick. Decision 26 fixed the OS: Ubuntu 26.04 LTS, minimized. `docs/BUILD-PLAN.md` M6 left the build tool open ("autoinstall/cloud-init or an image builder").

## What the image must do

- **Reproducible:** the same inputs (package versions, our artefacts, config) give the same image.
- **Outputs:** VMware OVA, Hyper-V VHDX and KVM qcow2, from one build.
- **Contents:** Ubuntu 26.04 minimized; NFS-Ganesha with `FSAL_OMNI` (our build, `fsal/build`); Samba and winbind; `omnifile-edge` and its units; xfsprogs, attr, acl; tpm2-tools.
- **Kernel:** 7.0 (fanotify pre-content events, decision 26).
- **Updates (M6):** OS and components inside the admin's maintenance window, with a kernel update gated by the fanotify regression suite (R3). A bad update must be undoable.
- **First boot:** the setup wizard (network, AD join, service endpoint and enrolment token, xfs data disks, the private-endpoint switch).

## Route A: an image builder (mkosi), recommended

mkosi builds a GPT disk image from packages directly, with no installer run. It lists Ubuntu among its supported distributions, builds disk images with systemd-repart, and can also build sysext images.

- **Reproducible by construction:** a package list, a pinned archive snapshot (`snapshot.ubuntu.com` serves 26.04, both architectures: "Reproducible builds" below), and our artefacts dropped in. No installer heuristics.
- **Outputs:** mkosi writes a raw GPT image. `qemu-img convert` turns it into qcow2, VHDX and VMDK, and the OVA is the VMDK plus a generated OVF descriptor in a tar. **VERIFY** that this OVF imports into the vSphere versions customers run.
- **Updates:** two `/usr` partitions (A/B) with `systemd-sysupdate`. An update writes the inactive partition, and the reboot happens in the maintenance window. A failed boot falls back to the old partition (boot counting; **VERIFY** with the Ubuntu 26.04 bootloader). Our components could ship as sysexts and update on their own.
- **Cost:** we maintain the image definition, and Ubuntu's `apt` is not the update path on the box. The Arc agent can't install itself on the read-only `/usr`; it comes as a sysext ("Arc agent" below).

## Route B: autoinstall in a VM (Packer)

Packer boots the Ubuntu installer ISO in a VM, serves an autoinstall (cloud-init) file, and then provisions over SSH. Packer templates for OVA appliances built this way exist.

- **Familiar:** an ordinary Ubuntu install, updated with `apt` and `unattended-upgrades` inside the window.
- **Less reproducible:** the installer's choices and the archive's state at build time leak into the image, unless the archive is pinned too.
- **Updates in place:** there is no A/B; undoing a bad update means a snapshot or a re-image. The kernel gate (R3) has to hold the kernel package (`apt-mark hold`) until the suite passes.
- **Outputs:** one Packer build per hypervisor, or one qcow2 converted as in route A.

## Recommendation

**Route A (mkosi).** Its A/B updates with rollback are what an appliance needs when an update can take a filer down. Holding a new kernel until the fanotify suite passes is a matter of not activating its partition. Route B remains the fallback if the spike finds that Arc or the bootloader can't live with an A/B layout.

## Spike plan (about 3 days)

1. Build a minimized 26.04 image with mkosi from a pinned snapshot, with Ganesha and `FSAL_OMNI`, Samba and winbind, and `omnifile-edge`. Boot it on KVM and run the M1 exit test.
2. Convert it to VHDX and OVA, and boot each on Hyper-V and vSphere.
3. Install the Arc agent and confirm the managed identity works.
4. Do one `systemd-sysupdate` round: a new `/usr`, a reboot, and a forced bad boot that falls back.

## Spike results (2026-10-15)

Built with mkosi 26 (Ubuntu 26.04's own) in a privileged container, booted in qemu (q35, OVMF, TCG: no KVM here).

**Step 1, the image: done, except the M1 exit test inside it.**

- **The layout** (`appliance/mkosi.repart/`):
  - an ESP with systemd-boot and one UKI per version;
  - `/usr` as erofs with a dm-verity hash partition, in slot A, plus an empty slot B (`_empty`) for the next version;
  - root as ext4 holding `/etc` and `/var`. systemd-repart grows it to fill the disk at first boot.
- **Each UKI's command line carries `usrhash=`.** The initrd opens `/usr` as `/dev/mapper/usr`, read-only and verified.
- **Our components live under `/usr`,** so they update with the OS:
  - the edge binaries;
  - Ganesha with `FSAL_OMNI` (`/opt/ganesha` links to `/usr/lib/omnifile/ganesha`);
  - `vfs_omnifile_prevver` in smbd's MODULESDIR;
  - the units and the smb-guard drop-ins.
- **Kernel:** 7.0.0-38-generic. Samba 4.23.6 matches the pinned `samba-vfs/` build.
- **Build size:** the disk image is 9.5 GiB sparse, 1.3 GB written. The UKI is 82 MB.
- **`appliance/tests/smoke.sh` passed 10/10** in the spike (it has 13 checks now; `docs/milestones/M6.md`). It checks `/usr` (verity, erofs, read-only) and that root grew. It checks the kernel check, that the edge unit waits for `/etc/omnifile/edge.toml`, and that Ganesha resolves all its libraries. It also checks for the prevver module, that updates are checked against the OmniFile key alone, and that no unit failed.
- **Found and fixed on the way:**
  - **Ganesha's libraries:** they were found through `/etc/ld.so.conf.d` on the dev VMs. In the image, `/etc` is on root, which an update doesn't touch, so `build.sh` sets each Ganesha binary's RUNPATH to `$ORIGIN` instead.
  - **The initrd** was 420 MB with every module, so the list is pinned to the storage drivers of KVM, Hyper-V, vSphere and NVMe.
  - **Not run:** the M1 exit test inside the image. It needs the service and an NFS client next to the VM, and here there is neither KVM nor a kernel NFS client (the same limit as the M1 lane, `docs/milestones/M1.md`). The image runs the same binaries and units that test runs.
- **A pinned archive snapshot:** done in WP2 (2026-11-03), `Snapshot=20261023T000000Z` (moved from 20261020 for ca-certificates USN-8944-1); see "Reproducible builds".

**Step 2, the formats:** `appliance/convert.sh` writes qcow2, VHDX, a fixed VHD (Azure) and an OVA. The OVA holds an OVF 1.0 descriptor with EFI firmware, paravirtual SCSI and vmxnet3, plus a SHA-256 manifest and a stream-optimized VMDK. `tests/formats.sh` checks them all and boots the VHDX, the VMDK and the VHD in qemu ("Formats" below). Booting them on Hyper-V and vSphere needs those hypervisors: **VERIFY at the client site**, with pilot 1's on-prem part (`docs/pilots/remote-pilots.md`); the fixed VHD on an Azure Gen2 VM is a check that needs a subscription.

**Step 3, Arc:** the offline half is done ("Arc agent" below). Connecting needs an Azure subscription (`docs/milestones/real-azure-checks.md`).

**Step 4, the update round: done.** `appliance/tests/update-round.sh` passes 9/9, over six boots in one VM:

| Boot | Runs | Result |
|---|---|---|
| 1 | 0.1.0 (slot A) | updates to 0.2.0 from a signed local feed, into slot B |
| 2 | 0.2.0 (slot B) | passes the kernel check, is blessed (`+3-0` dropped), and updates to 0.3.0 into slot A, over 0.1.0 |
| 3–5 | 0.3.0 (slot A) | its kernel check is made to fail; it is not blessed and spends its tries (`+2-1`, `+1-2`, `+0-3`) |
| 6 | 0.2.0 | systemd-boot falls back to it |

0.2.0's `/usr` was never touched while it ran (`ProtectVersion=%A`).

**Found and fixed in step 4.** Each of these would have broken updates in the field:

- **Partition UUIDs.** sysupdate writes the new `/usr` with the partition UUID its file name carries (`@u`). The initrd finds `/usr` by the UUID that `usrhash=` implies. So the split files are named with their UUIDs (`SplitName=%t.%U`). Without that, the new version can't find its `/usr`.
- **The UKI's name.** mkosi names a UKI `<id>-<kernel>-<hash>.efi` by default. sysupdate then doesn't recognise the installed version, so `UnifiedKernelImageFormat=%i_%v` names it `omnifile-edge_<version>.efi`.
- **The keyring.** systemd-pull reads `/etc/systemd/import-pubring.{pgp,gpg}` or `/usr/lib/systemd/import-pubring.pgp`, not `/usr/lib/…/import-pubring.gpg`. Ours now replaces the distribution's `.pgp`, so the box trusts only the OmniFile key.
- **The UKI's mode.** `Mode=0444` on the ESP sets the FAT read-only attribute. systemd-boot then can't rename the file to count a try ("Access denied"), so boot counting never started and a bad version would have been blessed by default. It is now `0644`.

**A failed version stays installed, marked bad (`+0-3`).** sysupdate won't fetch the same version again, so a fix ships as a newer version. That version replaces the bad one: the oldest instance not running (`InstancesMax=2`).

**The update gate.** `omnifile-kernel-check.service` runs `smbguard-tool kernel-check` on every boot. It is required by `boot-complete.target`, which `systemd-bless-boot` waits for before it marks a new version good. The check:

- marks a file for `FAN_PRE_ACCESS` in a `FAN_CLASS_PRE_CONTENT` group;
- reads the file from a second thread;
- answers with a deny (`EIO`);
- passes only if the read was held and then failed with `EIO`.

A kernel that drops pre-content events, or ignores the answer, is never blessed. Its mutant (answering allow) fails the check.

This is the boot-time half of the R3 gate. The release half is `appliance/release.sh` (M6), the only thing that signs a feed: it boots the image being released in qemu, runs the kernel canary and the full smb-guard A-suite on its kernel, and signs `SHA256SUMS` only when both exit 0 and every check reports PASS. `build.sh` no longer signs (`appliance/README.md`, "Releasing: the R3 gate").

## Architectures, reproducibility, formats and Arc (WP2, 2026-11-03)

Results and counts are in `docs/milestones/results/wp2.md`.

### An arm64 test build

The product is x86-64. `build.sh --arch arm64` builds the same image for arm64, so the R3 gate and the appliance tests run at native speed on Apple silicon (HVF) instead of under TCG. The arm64-only settings are in `mkosi.conf.d/20-arm64.conf`.

- **The console** is the PL011, `ttyAMA0` (x86-64: `ttyS0`). The test unit writes to `/dev/console`, so it works on both.
- **The kernel.** Ubuntu 26.04's arm64 `vmlinuz` is not the kernel. It is a stub image (`.linux`, `.dtbauto`, `.hwids`) that wraps the EFI zboot kernel, for boards that pick a devicetree by hardware id. Built into a UKI as is, the box never boots: systemd-stub refuses it ("Inner kernel image contains base relocations"). `mkosi.arm64/unwrap-kernel.py` builds the UKI from the zboot kernel inside. VMs describe themselves with ACPI, so the devicetrees aren't needed.
- **Firmware.** Under HVF and under TCG, Ubuntu's AAVMF (2025.11-3ubuntu7.3) never gets past its own setup: no output, one vCPU busy. It starts only under KVM. qemu's own edk2 build boots under all three, so `tests/vm.sh` uses it. That build has no Secure Boot, so the Secure Boot tests run on x86-64 with OVMF.
- **No vTPM under HVF.** qemu's arm64 TPM (`tpm-tis-device`) can't be mapped under HVF (`HV_BAD_ARGUMENT`), so an HVF guest seals to the host key. KVM and TCG guests get a vTPM.
- **Nested KVM** inside a Lima VM (vz) boots the kernel, then stalls ("rcu_preempt kthread starved"). Guests that must reach the Lima network (the DC) run under TCG in the spike VM instead.

### Reproducible builds

- `mkosi.conf` pins `Snapshot=20261023T000000Z`. apt knows snapshot.ubuntu.com for archive.ubuntu.com, but not for ports.ubuntu.com, where mkosi takes arm64 packages from. `mkosi.sandbox/` maps it, because snapshot.ubuntu.com/ubuntu serves every architecture.
- It also pins `Seed=`, so the UUIDs and the verity salt are fixed.
- `build.sh` sets `SOURCE_DATE_EPOCH`: the environment's, else HEAD's commit time, else the snapshot's. It records the inputs in `BUILD-INFO`.
- **Result:** two builds of the same inputs give byte-identical `/usr`, verity, UKI, initrd, manifest, ESP, root partition and feed, and the whole 10 GB disk. That's three arm64 builds, and two x86-64 builds (`results/wp2.md`).
- **The first comparison found two problems.** The root partitions differed in `/etc/ssh/ssh_host_*`, `/var/log/alternatives.log` and `/var/cache/ldconfig/aux-cache`.
  - The host keys were a real bug: openssh-server's postinst made them at build time, so every box from one image shared one set of SSH host keys. The image now ships none. `omnifile-ssh-keygen.service` makes each box's own keys at first boot, and smoke.sh checks the keys are newer than the boot.
  - The log and the cache are build records, and are removed.
- **Not covered:** the build host's tools are part of the inputs (`BUILD-INFO` lists mkosi, systemd-repart, ukify, erofs-utils and zstd). Our binaries are built before `build.sh`, and their reproducibility is cargo's, not this build's.

### Formats

`convert.sh` writes:
- qcow2;
- VHDX (dynamic);
- a fixed VHD for Azure, padded to a whole MiB (the disk is 20 KiB over one);
- the OVA.

The OVF's guest OS is the disk's architecture's (`ubuntu64Guest`, CIM id 94; it was 96, which is Debian 64-bit). `tests/formats.sh` checks:
- the OVA's order and ustar format, and its manifest;
- the OVF against DMTF's DSP8023 1.0.0 schema, read offline from pinned copies;
- the OVF's references, capacities and firmware;
- every format's virtual size;
- `qemu-img compare` of every format against the raw disk;
- with `--boot`, a boot of the VHDX, the OVA's VMDK and the VHD;
- with `--v2v`, `virt-v2v -i ova`.

### Ganesha on the read-only `/usr`

Ganesha's compiled defaults are under its prefix `/opt/ganesha`, which on the appliance is the read-only `/usr`. So it died at start: first its pid file (`/opt/ganesha/var/run/ganesha/ganesha.pid`), then its NFSv4 recovery root (`/opt/ganesha/var/lib/nfs/ganesha`, WP3's finding). It had never run on the appliance, because the smoke test never started it.
- `/usr/lib/omnifile/ganesha/{etc,var}` are now links to `/etc` and `/var`. The recovery records land in `/var/lib/nfs/ganesha` (tmpfiles.d), on the root partition, and survive reboots and A/B updates.
- Its D-Bus policy is installed too.
- smoke.sh starts Ganesha as its unit runs it, writes a grace record, reboots, and checks the record and that Ganesha is in grace again.

### NFSv3 locking, fixed ports and the firewall

NFSv3 locking (NLM) is on in the default image (owner, 2026-11-04). It needs `rpc.statd` (nfs-common) and `sm-notify` at Ganesha's start (a drop-in on `nfs-ganesha-omni.service`). Its state is in `/var/lib/nfs/sm` and `sm.bak` (tmpfiles.d, owned by `statd`).

Every inbound port is fixed, so a client's firewall and ours can name them:

| Port | Protocol | What |
|---|---|---|
| 22 | tcp | SSH (keys made at first boot) |
| 445 | tcp | SMB |
| 111 | tcp, udp | rpcbind |
| 2049 | tcp | NFS |
| 20048 | tcp, udp | MOUNT (Ganesha `MNT_Port`) |
| 32803 | tcp, udp | NLM (Ganesha `NLM_Port`; V15.6 registers it over TCP only) |
| 32765 | tcp, udp | rpc.statd (`--port 32765 --outgoing-port 32766`) |

rquotad doesn't run (`Enable_RQUOTA = false`). The firewall fails closed: `systemd-networkd` and every listener require `omnifile-firewall.service` and start after it, so if its rules can't load there is no network. Ubuntu's `nftables.service` (whose `/etc/nftables.conf` starts with `flush ruleset`) is disabled and can't start at all (a drop-in condition).
- **Ordering:** the firewall is ordered before `sysinit.target`, so its table is loaded before anything of the normal boot starts. That includes `systemd-resolved`, an early unit that also requires it.
- **resolved:** LLMNR and mDNS are off (`resolved.conf.d`), so its only listener is the loopback stub.
- **No address without the firewall:** the initrd's networkd has no `.network` for a real NIC, and there is no `ip=` on the command line, so nothing is addressed there. A box whose firewall fails at boot has no address at all.
- **A firewall that fails later:** a firewall stopped or failing while running takes networkd and the listeners down with it, but networkd leaves the addresses it set. The box is then addressed, with no listeners.

### Servers restart after a crash

smbd, nmbd and winbind restart on failure, 5 s apart (Ubuntu's units have no `Restart=`). At most 5 starts in 10 minutes, then the unit stays failed. These are `30-omnifile-restart.conf` drop-ins in `/usr`. A 6.75 h chaos soak found SMB down for good after one smbd crash.

Ganesha already restarted on failure, 2 s apart, from its own unit. That is kept, and its drop-in only adds the same start limit.

smbd and Ganesha stay bound to the guard. A restart is a start, which requires the guard and waits for it, so neither comes back while the guard is down. A stop of the guard isn't a failure, so it doesn't restart them either.

smoke checks this:
- the guard dependencies;
- neither real unit starts while the guard is down;
- a control: with the guard drop-in shadowed, the same smbd start brings smbd up;
- smbd and Ganesha, killed with SIGKILL, come back.

### What an A/B update brings to an old `/etc`

An update replaces `/usr` and never touches `/etc`: no presets run and no package scripts. So everything a release adds must work from `/usr` alone:
- **Units are wanted from `/usr`:** the firewall and the SSH key unit through vendor links in `sysinit.target.wants`, the guard through `multi-user.target.wants`. rpc.statd has no `[Install]`, and Ganesha's drop-in wants it.
- **Users from `sysusers.d`:** `statd` (nfs-common's postinst makes it only in a new image). `systemd-sysusers` runs at every boot (a drop-in resets its `ConditionNeedsUpdate=`, which compares mtimes that reproducible builds fix: the update round from a pre-WP2 image got no `statd` without it).
- **Units that compare mtimes** (`ConditionNeedsUpdate`) are skipped when reproducible builds date the new `/usr` no later than `/etc/.updated`:
  - `systemd-sysusers` would leave new users missing;
  - `ldconfig` would leave `/etc/ld.so.cache` describing the old `/usr`;
  - the journal catalog would go stale.

  All three run at every boot (drop-ins reset their conditions; each takes well under a second). `systemd-hwdb-update` needs nothing, since the image ships `/usr/lib/udev/hwdb.bin`.
- **SSH host keys:** images from before WP2 shipped one set for every box. Ubuntu's own first-boot `sshd-keygen.service` is blocked (a drop-in condition), so ours is the only generator, and no private key can end up next to another key's `.pub`. `omnifile-ssh-keygen` replaces the keys once on a box without `/etc/ssh/omnifile-own-host-keys`, the marker it writes, and only fills in missing key types after that. Clients see a changed host key once, after that update.

`tests/update-round.sh --disk` an image built from origin/main before WP2 checks all of it. `omnifile-firewall.service` loads `/usr/lib/omnifile/firewall.nft` before the network comes up: an `inet omnifile` table whose input and forward chains drop everything except the ports above, loopback, established traffic, ICMP and DHCP replies. smoke.sh checks every port in `rpcinfo -p` is open in it, and grace.sh that a port it doesn't name can't be reached from the host.

### Arc agent (spike, offline)

The package is azcmagent 1.69.03579.1568, from packages.microsoft.com, arm64 and amd64.
- **Contents:** all its files are under `/opt`: `azcmagent` 60 MB, `GC_Service` 222 MB, `GC_Ext` 33 MB. The binaries are static.
- **What its maintainer scripts write outside `/opt` and `/var/opt`:**
  - units into `/lib/systemd/system` (`himdsd` and four sockets, `arcproxyd`, and `gcad` and `extd` from the GC install scripts);
  - `/usr/bin` links;
  - an auto-upgrade cron job (through apt);
  - a udev rule;
  - the `himds` and `arcproxy` users.
- **On the appliance:** `/usr` is read-only, `/opt` is on the root partition (which an update never touches), and there is no dpkg and no cron. So the package can't be installed as Microsoft ships it.

Two ways to carry it:

| | Baked into `/usr` | A sysext |
|---|---|---|
| What it takes | `build.sh` stages the pinned package (version and SHA-256, like `aws_signing_helper`) under `/usr/lib/omnifile/arc`, with `/opt` links (tmpfiles), units, sysusers.d and tmpfiles.d declared instead of its scripts | `appliance/arc/make-sysext.sh` makes an erofs sysext (`/opt/...` and the units, users and directories declared as above); `systemd-sysext` overlays it on `/usr` and `/opt` at boot |
| Size | +315 MB in every image's `/usr` (1.2 to 1.3 GiB of the 3 GiB slot today), whether the client uses Arc or not | only on boxes that use Arc |
| Updates | only with an OmniFile release; the agent's own auto-upgrade can't work | on its own schedule: another sysupdate transfer, or a new image on the box |
| Integrity | under `/usr`'s verity | needs its own verity and signature (the image policy); unsigned in the spike |
| Redistribution | we ship Microsoft's binaries in our image: **counsel (O)** | the same if we build it; none if the box builds it from the client's own download of Microsoft's package |
| Extensions | the ones that install packages (the Azure Monitor Agent: dpkg and units in `/lib`) fail either way: **verify on real Arc (P)**; observability's edge logs (decision 37) depend on it | the same |

**Tried offline (`tests/arc-sysext.sh`, arm64 under HVF):**
- the sysext merges;
- `/usr` becomes a read-only overlay on the verified erofs;
- the agent's users and directories are made;
- `himdsd` starts and listens on 40342;
- `azcmagent show` reports version 1.69.03579.1568, `Disconnected`;
- an unmerge removes it all.

**Recommendation: the sysext.**
- Boxes that don't use Arc carry nothing.
- The agent updates on its own schedule.
- `/usr` stays exactly the release.

We should build it in the release pipeline from the pinned package, with dm-verity and our signature. If counsel won't clear redistribution, the box can build it from the client's own download, at the cost of an unsigned extension (a looser image policy).

Still open:
- Connecting needs a subscription (**P**).
- Whether the Azure Monitor Agent extension works without dpkg needs real Arc (**P**). If it can't, edge logs need another path, such as shipping AMA as a sysext too.

## Open

- **Secure Boot.** The UKIs are unsigned (`UnifiedKernelImages=unsigned`), so VMs boot with Secure Boot off. **Decided (decision 48, 2026-10-21):** Secure Boot is optional in v1; the UKIs are signed with OmniFile's own key, in Key Vault Premium, and the docs show a client whose policy requires Secure Boot how to enrol it (MOK through shim, or the firmware's `db`). Microsoft (shim) signing only if a client requires it later. **Built:** `release.sh` signs the feed's UKI and the disk's ESP (UKI, systemd-boot, fallback loader) through Key Vault, and `docs/design/secure-boot.md` is the enrolment guide (firmware `db`; MOK waits for a signed shim).
- **The update feed's signing key.** **Decided (decision 47, 2026-10-21):** the feed is hosted on GitHub Releases and `systemd-sysupdate` downloads from there; the signing key is in Azure Key Vault Premium (HSM-backed, non-exportable), and only the release script signs, never a developer machine. **Built:** `appliance/pgp_kv.py` makes the OpenPGP signature with a Key Vault ES256 operation, the image's transfers read `releases/latest/download/` of `release.conf`'s repository, and `release.sh` lays out the release's assets (`docs/design/secure-boot.md`). CI signs its test feeds with a throwaway key made for each run (`.github/workflows/appliance.yml`).
- **erofs compression** (`Compression=` in `mkosi.repart/10-usr.conf`) would leave more headroom in the 3 GiB slots. Today `/usr` is about 1 GB zstd-compressed in the feed, and the slots are fixed at build time.
- **GPL source offer.** The image redistributes GPL binaries, among them the kernel, Samba and parts of systemd, so each release must offer their corresponding source (`LICENSES.md`, "The appliance image"). This is an M7 release task.
- **The setup wizard** (`docs/design/setup-wizard.md`, built) writes the feed drop-ins and `/etc/omnifile/edge.toml`. Until it has run, the edge unit stays inactive and tty1 shows its questions.

## Sources

- mkosi: its man pages (Ubuntu's are dated; read the current README for systemd-repart, sysext and the distributions) https://manpages.ubuntu.com/manpages/jammy/man1/mkosi.1.html
- Packer with Ubuntu autoinstall: https://oneuptime.com/blog/post/2026-03-02-how-to-use-packer-to-build-ubuntu-vm-templates/markdown ; OVA appliance templates https://github.com/HariSekhon/Packer
- Autoinstall reference: https://discourse.ubuntu.com/t/automated-server-install-schema/16616
