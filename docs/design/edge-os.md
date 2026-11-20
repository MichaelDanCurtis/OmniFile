# Edge filer OS: Flatcar Container Linux

**Status: NOT ADOPTED (2026-09-26).** The owner chose **Ubuntu 26.04 LTS, minimized**, and kept the fanotify SMB guard (decision 26). Rebuilding the SMB guard as a Samba VFS module was judged too much work. This note is kept as the record of the Flatcar evaluation. Revisit it if Flatcar's kernel reaches 6.14 or later on Stable (expected around 2027), or if the fanotify guard proves hard to make fail closed.

The note below checks Flatcar against the design, and proposed one design change to make it work. Evidence was read at primary sources on 2026-09-26; source links are at the end.

## Why the OS matters at all

Almost nothing in the filer depends on the distribution. NFS-Ganesha, Samba, winbind and our Rust daemon run on any recent Linux. **One thing does: the SMB stub guard.**

- **NFS reads** go through `FSAL_OMNI`, inside Ganesha, so we can say "wait" or "fail" to them on any kernel.
- **SMB reads** don't go through us. Samba reads the local file directly. The current design catches those reads with fanotify pre-content events (`FAN_PRE_ACCESS`), which first shipped in **Linux 6.14**.
- So the kernel version decides whether SMB users can safely open archived files. That is the only reason RHEL 10 (kernel 6.12) came up.

## Flatcar against our requirements

| Requirement | Flatcar today | Verdict |
|---|---|---|
| Kernel ≥ 6.14 for fanotify pre-content | **Every channel is on 6.12.109; LTS is on 6.6.** A move to 6.18 is open as a PR (flatcar/scripts#4191), not merged. At Flatcar's usual cadence, Stable gets 6.18 around Q1–Q2 2027. | **Blocker for the current SMB design** |
| fanotify permission events and xfs in the kernel config | `CONFIG_FANOTIFY=y`, `CONFIG_FANOTIFY_ACCESS_PERMISSIONS=y`, `CONFIG_XFS_FS=m`, also in the 6.18 config | OK |
| Ganesha, Samba and winbind | Not on the host image, and no sysext exists. The host has only sssd, realmd, adcli, krb5 and nfs-utils. | Ship our own containers or sysexts; we'd build Ganesha ourselves anyway because of the FSAL |
| Samba as an AD member in a container | samba-in-kubernetes' `samba-server` supports a domain-member file server, with smbd and winbind sharing `/run/samba/winbindd` | OK. Its scripted join takes a plaintext password, so we write our own join step |
| Updates | A/B `/usr` partitions with automatic rollback; reboot strategy `off` or reboot windows; **Nebraska**, a self-hosted update server, works air-gapped | **Strong fit for an appliance** |
| Provisioning | Ignition/Butane. Images: VMware OVA, Hyper-V VHD/VHDX, QEMU/KVM, ISO/PXE | Strong fit: the setup wizard can emit the Ignition config |
| TPM2 | tpm2-tools and `systemd-cryptenroll` present; LUKS TPM2 unlock is documented | OK: it holds the mTLS and IAM Roles Anywhere keys |
| Secure Boot | The shim is signed, but **not by Microsoft's UEFI CA** (flatcar/Flatcar#501 is still open) | Customers would have to enrol keys. Leave it out of v1 |
| Azure Arc | **Not supported.** Microsoft says the agent can't run on Flatcar | Lose the Arc managed identity. Use the other designed route: mTLS with a TPM-held client key, and service-minted SAS |
| Backing | CNCF Incubating; Microsoft staff among the maintainers; commercial support from Cloudbase Solutions | Acceptable |

**Verdict:** Flatcar fits an appliance better than a general-purpose distribution: image updates with automatic rollback, a self-hostable update server, Ignition, and a small attack surface. Two things stand in the way:
- the **kernel**, which blocks the fanotify-based SMB guard until about 2027;
- **Arc**, which the design can live without.

## The proposal: guard SMB with a Samba VFS module instead of fanotify

Handle SMB the same way as NFS. Samba loads VFS modules, shared objects listed in `vfs objects =` in `smb.conf`, which sit in its I/O path. `vfs_ceph`, `vfs_glusterfs` and `vfs_gpfs` are existing examples. A small **`vfs_omni`** would:
- catch opens and reads of stubs;
- ask the Rust daemon over a local socket, the same way `FSAL_OMNI` does;
- answer "wait" or "fail" according to the share policy.

What that gains:

1. **No kernel requirement.** Flatcar Stable (6.12) works today, and so would RHEL 10, so the OS choice stops being forced.
2. **It fails closed by construction.** fanotify fails *open*: if the daemon dies, reads go through and stubs read as zeros. That's the worst risk in `AGENTS.md`. A VFS module that can't reach the daemon returns an error.
3. **Better identity.** The module sees the SMB session's user (SID and uid) directly. The fanotify path sees only a process id, which then has to be mapped to a session.
4. **Windows sees the files as offline.** The module can set `FILE_ATTRIBUTE_OFFLINE` on stubs, so Explorer shows them as offline and should stop thumbnailing and previewing them. That would stop accidental recalls, which is a real problem with seismic folders. *Verify in the pilot which clients honour it.* (Built since without a module: the edge sets the bit in Samba's `user.DOSATTRIB` as the marker changes, `omnifile_marker::dosattr`; the pilot's checks are in `docs/pilots/windows-client-matrix.md`.)
5. **SMB can join the migration namespace sooner.** The design doc already names a Samba VFS module as the way to present S3-resident files over SMB. It currently sits in v2; one module would cover both jobs.

What it costs:

- **Licence:** the module links into smbd, so it must be **GPL-3**. It follows the FSAL pattern: small, in its own directory (`smb/`), its own licence files, and the logic stays in our Apache-2.0 daemon behind the socket. This does **not** break the "don't link GPL code into our binaries" rule; the GPL code is ours, in a separate module.
- **Maintenance:** Samba's VFS interface changes between releases, so the module is built against the exact Samba version we ship. We control that version in our own container image, as we do for Ganesha and the FSAL.
- **Coverage:** only access through Samba is guarded. On an appliance nothing else reads the data filesystem, so that's acceptable. fanotify can come back later, as an extra safeguard on kernels that have it.
- **Async I/O:** the module implements Samba's async read path (`pread_send`/`pread_recv`). `sendfile` stays off on stub-capable shares.

## Proposed edge image

- **OS:** Flatcar Stable. Move to 6.18 when it reaches Stable (no dependency on it).
- **Containers:** privileged, host network, managed by systemd units:
  - `omnifile-ganesha`: NFS-Ganesha plus `FSAL_OMNI`;
  - `omnifile-samba`: smbd, winbind and `vfs_omni`, sharing the winbind socket and the NFS Kerberos keytab with the Ganesha container;
  - `omnifile-edge`: the Rust daemon.

  These could later move to our own sysexts if container start-up or image size becomes a problem.
- **Updates:**
  - The OmniFile service hosts **Nebraska** over the private network, so edges update with no internet path.
  - The reboot strategy is `off`. The service schedules reboots inside a maintenance window the admin sets in the portal, and NFS clients ride the grace period.
  - Our own container images are updated through the same service.
- **Credentials:** no Arc on the edge.
  - Azure: mTLS to the service with a TPM-held client key, and service-minted user-delegation SAS.
  - AWS: IAM Roles Anywhere with its key in the TPM.
- **Provisioning:** the setup wizard produces the Ignition config: network, AD join parameters, the service endpoint and enrolment token, and data-disk layout (xfs).

## If the owner prefers to keep fanotify

Two routes, both worse today:

- **Build our own Flatcar image with 6.18,** using the Flatcar SDK and the open PR, and serve it from our Nebraska until Stable catches up. We would be running our own OS build.
- **Use a different OS.** The only immutable options with a kernel new enough today are Fedora CoreOS/bootc, which gets roughly 13 months of support and no Arc, or an image we build ourselves from Ubuntu 26.04 with mkosi and systemd-sysupdate, which keeps Arc but means maintaining the OS. RHEL 10, CentOS Stream 10 and SUSE Micro are all on 6.12.

## Pilot changes

The filer spike runs on Flatcar Stable with the three containers. It proves `vfs_omni` alongside `FSAL_OMNI`:
- a stub read over SMB waits or fails according to policy;
- killing the daemon makes SMB reads **fail**, not return zeros;
- the session user's SID reaches the daemon;
- Windows Explorer's handling of `FILE_ATTRIBUTE_OFFLINE`.

## Sources

- Flatcar channel kernels: flatcar.org `releases-json/releases-{alpha,beta,stable,lts}.json`; 6.18 PR https://github.com/flatcar/scripts/pull/4191; cadence https://github.com/flatcar/Flatcar/blob/main/README.md
- Kernel config: https://github.com/flatcar/scripts/blob/main/sdk_container/src/third_party/coreos-overlay/sys-kernel/coreos-modules/files/commonconfig-6.12
- Image contents and formats: https://stable.release.flatcar-linux.net/amd64-usr/4757.2.0/
- Updates: https://www.flatcar.org/docs/latest/diagnostics/manual-rollbacks/ ; Nebraska https://github.com/flatcar/flatcar-website/blob/main/content/docs/latest/updates-releases/nebraska/managing-updates.md
- Secure Boot: https://github.com/flatcar/Flatcar/issues/501 ; TPM/LUKS https://www.flatcar.org/docs/latest/security/encryption/luks/
- Samba in containers: https://github.com/samba-in-kubernetes/samba-container/tree/master/examples/podman
- Azure Arc supported OSes: https://learn.microsoft.com/en-us/azure/azure-arc/servers/prerequisites ; Flatcar answer https://learn.microsoft.com/en-us/answers/questions/5667492/azure-arc-machine-support-for-immutable-systems
- Commercial support: https://cloudbase.it/flatcar-support/ ; CNCF https://www.cncf.io/blog/2024/10/29/flatcar-brings-container-linux-to-the-cncf-incubator/
- fanotify capability checks (init user namespace): `fs/notify/fanotify/fanotify_user.c` in v6.14
- Alternatives: Fedora CoreOS streams https://raw.githubusercontent.com/coreos/fedora-coreos-streams/main/streams/stable.json ; Talos https://github.com/siderolabs/talos/releases ; Debian kernels https://packages.debian.org/trixie/linux-image-amd64 ; SUSE lifecycle https://www.suse.com/lifecycle/ ; Azure Linux 4 https://learn.microsoft.com/en-us/azure/azure-linux/whats-new-azure-linux-4
