# The edge setup wizard (M6)

**Status: BUILT (2026-10-16).** `omnifile-setup` (`edge/crates/omnifile-setup`), its first-boot units in the appliance image (`appliance/`), and the portal's Add edge (`portal-api.md` §5.15). `docs/BUILD-PLAN.md` M6 asks for a setup wizard that covers:

- the network;
- the AD join;
- the service endpoint and enrolment token;
- the xfs data disks;
- the private-endpoints switch (decision 22).

`access-control.md` §8 adds the appliance-admin group and a sealed, audited break-glass account.

## What the admin sees

A new filer boots to its console, and the console asks, in plain language, with each answer checked as it is given:

1. What is this filer called on the network?
2. How does it get its network address: DHCP, or a fixed address (with the gateway and DNS servers)? With more than one NIC, which one.
3. Which disk holds the files? Only disks with nothing mounted are offered. A disk that holds anything is formatted only after the admin types `ERASE`. A device typed by hand is resolved first (`/dev/disk/by-path/...` names the disk it points at). It is refused, and the question asked again, when it is not a whole disk (a partition, even the system disk's unformatted inactive `/usr` slot, which the next update would overwrite), or when anything of it is mounted (the system disk), naming where. On a rerun, the data disk an earlier run prepared is offered to keep instead, and setup goes on to the next question: nothing on it is erased, and no `ERASE` is asked. It is recognised only when all of these hold: the xfs mounted at `/srv/omnifile/data` is on that disk, nothing else of the disk is mounted, its label is `omni-data`, and setup's own `srv-omnifile-data.mount` names its UUID. Declining it stops setup with nothing changed: setup never moves the files to another disk.
4. The AD domain, its short name, the account that may join computers, the OU (optional), and the AD group whose members administer the filer. Then the join password, which is not echoed and never stored.
5. Reach Azure storage only through private endpoints? Yes by default.
6. The enrolment code from the portal (Edges, Add edge), or empty to enrol later.
7. The update server, and when the filer may restart to apply an update.
8. Will it serve files still in Amazon S3 (a migration)? If so, the IAM Roles Anywhere trust anchor, profile and role ARNs, and the Roles Anywhere interface endpoint if private DNS doesn't cover it. Each ARN is checked as it is given. Then: should OmniFile's edge CA issue its certificate? Yes by default (decision 50); no if the client's own CA issues it.

It shows a summary and asks once more before changing anything. Every answer can be changed by running `sudo omnifile-setup` again: the previous answers are the defaults, and the steps change only what the new answers touch.

## Steps

The answers are applied in twelve idempotent steps. A rerun with the same answers reports each one "already as asked" and runs no join, format or enrolment.

| Step | Writes | Runs |
|---|---|---|
| hostname | `/etc/hostname`, one marked line in `/etc/hosts` | `hostnamectl` (or `hostname`) |
| network | `/etc/systemd/network/10-omnifile.network` (DHCP or static, DNS, the AD domain as search domain) | `networkctl reload` |
| time | `timesyncd.conf.d/10-omnifile.conf`: the given servers, or the AD domain's name, which resolves to the DCs (Kerberos allows 5 minutes of skew) | restart timesyncd |
| data | `srv-omnifile-data.mount` (by filesystem UUID), a `RequiresMountsFor` drop-in on the daemon | `mkfs.xfs -L omni-data` once, on the real device an alias names; only on a whole disk (`lsblk` type `disk`, never a partition); never on a disk with anything of it mounted, or with data unless erase was asked; once the mount unit exists (an earlier run made the volume it names), never on any disk but that volume, erase or not, because `sdX` names can change between boots. The way out is to reattach the original data disk and run setup again, or to deploy the appliance again. There is no console way to drop the unit: before setup finishes there is no shell (the recovery account is sealed only after the join), and with the volume's disk missing setup can't tell whether it held files; a volume already mounted at the data root, or labelled `omni-data` from an interrupted run, is reused. Once the volume exists, `erase` is cleared from the saved answers, also when the volume was found mounted (an unattended credential asking to erase is read again at every boot) |
| ad | `krb5.conf`, `smb.conf` (`edge/config/samba/smb.conf.tmpl`, checked like the daemon checks shares), `idmapd.conf`, winbind in `nsswitch.conf` | `testparm` (which doesn't load VFS modules, so `omnifile-setup check-smb-module` follows: if `vfs_omnifile_prevver` is installed, smbd must load it); `net ads join` with the password in `PASSWD`, never on a command line; winbind; then checks that winbind sees the admin group |
| admins | `/etc/sudoers.d/omnifile-admins` (0440, checked by `visudo` and removed if it fails), `sshd_config.d/10-omnifile.conf` (`AllowGroups` the admin group, no root, `DenyUsers` the recovery account) | `sshd -t` |
| recovery | sudo for the break-glass account, its PAM profile (below) | seals it: see "The break-glass account" |
| edge | `/etc/omnifile/edge.toml` (the data volume's UUID for the smb-guard, `[network] private_only`, `[service] key_protection = "auto"`, `[shares] render = true`), Ganesha's base config with its grace period on (`edge/config/ganesha/omnifile-base.conf`), and the includes of the shares the daemon renders, with those files empty until it does (`share-provisioning.md`) | `systemd-creds setup` once, for the host key that sealed keys use (below) |
| enrol | the edge's identity (`omnifile-edge enrol`, code on stdin) | the spent code is removed from the saved answers |
| aws | with `[aws]` (IAM Roles Anywhere, decision 43): the key, sealed to the TPM (`/var/lib/omnifile/identity/roles-anywhere.cred`), its certificate request `/etc/omnifile/roles-anywhere.csr` and public half `.pub`, the helper's `/etc/omnifile/aws-signing-helper.env`. By default (decision 50) the request again as `/var/lib/omnifile/roles-anywhere/request.csr`, and the certificate from OmniFile's edge CA in that directory. With `issuer = "client"` and a certificate (`[aws] certificate`, or `omnifile-setup aws-cert FILE`), `/etc/omnifile/roles-anywhere.pem` and its chain | `systemd-creds encrypt` with the key on stdin, once; by default, once enrolled, `omnifile-edge roles-anywhere-cert` (the certificate from the service over mTLS); restarts `omnifile-aws-signing-helper.service` when something changed and the certificate is there. Without `[aws]`, removes the env file and stops the helper (the key is kept) |
| updates | the feed as a drop-in on each sysupdate transfer, the restart window on `systemd-sysupdate-reboot.timer` | `systemd-analyze calendar` checks the window |
| services | | `omnifile-edge`, the annex supervisor `omnifile-annex` (its appliance conditions hold it at boot until there is a config and an identity), `smbd` and `nfs-ganesha-omni`, once enrolled |

The answers are saved in `/etc/omnifile/setup.toml` (0600: it holds the enrolment code until it is used). Once every step has run, `/var/lib/omnifile/setup/done` stops the console from offering setup at boot.

## Unattended

For VM templates and automation, the same answers can come as the systemd credential `omnifile.setup`, through the hypervisor (SMBIOS type 11, or the kernel command line). The join password and the enrolment code can come as `omnifile.join-password` and `omnifile.enrol-bundle`, so one answers file can serve many filers. `omnifile-setup-auto.service` then runs at first boot, and the console asks nothing. `omnifile-setup apply --answers FILE` does the same from a file, and `--root DIR` writes the files below DIR and runs nothing, for review.

## Private networking (decision 22)

When private endpoints are chosen, the edge enforces it itself (`omnifile_blob::net`):

- Every HTTP client the daemon builds resolves names through a resolver that keeps only private addresses: RFC 1918, shared address space, loopback and IPv6 unique-local.
- A storage name that resolves only to public addresses fails, with an error naming the addresses and pointing at the private endpoint's DNS zone.
- A URL with a public IP literal is refused before any connection.

So a missing private DNS zone shows up as an error, never as a quiet fallback to the public endpoint.

## The break-glass account

`access-control.md` §8: "a break-glass local account is sealed and audited". It is `omnifile-recovery`, for when the domain can't be reached and no AD admin can log in.

- **Locked until sealed.** The image creates it locked (`sysusers.d`). The wizard seals it: on the console it makes a password, 30 characters in groups of five (150 bits), and shows it once, to keep in a vault or a sealed envelope. The password goes to `chpasswd` on stdin; only its hash is kept. Unattended, the credential `omnifile.recovery-hash` gives a crypt(3) hash instead, never a password. Without one, the account stays locked until someone runs `sudo omnifile-setup recovery seal` on the console.
- **Console only.** sshd refuses it (`DenyUsers`). It has sudo.
- **Audited.** A PAM profile (`edge/config/pam/omnifile-recovery`, enabled with `pam-auth-update`) runs `omnifile-setup recovery-used` on every session:
  - for this account, it logs at `auth.alert`, which the edge's Azure Monitor agent ships;
  - it marks the account as used. The edge's heartbeat then reports `recovery_used: 1`, so the portal shows the filer as needing attention, and the `edge-recovery-used` alert fires.
- **Resealed after use.** The login banner says to seal it again. `recovery seal` makes a new password and clears the mark. `recovery status` says whether it is sealed and whether it was used since.

## Keys

The edge's private keys are sealed to the box (M6, "TPM-held keys"; `omnifile_control::seal`). They are:
- its mTLS key, which proves it is this edge to the service;
- the key of a certificate request in flight, at enrolment or renewal.

They are never on disk in the clear:
- `[service] key_protection = "auto"` (what the wizard writes) seals each key with `systemd-creds encrypt`. The key is bound to the TPM 2.0 when the box has one, and to systemd's host key (`/var/lib/systemd/credential.secret`) as well. The files are `identity.cred` and `pending-*.cred`, in place of `identity.pem` and `pending-*.json`.
- A copy of the disk, or of the identity directory, is of no use without the box's TPM. The daemon opens the key into memory when it loads its identity.
- The key is bound to no PCRs and no signed PCR policy. So a firmware or Secure Boot database update doesn't lock it out. A cleared or replaced TPM does: the filer then can't reach the service and is enrolled again, as a new edge (Edges, Add edge).
- Each credential is bound to its name, so a sealed identity key opens as nothing else.
- `tpm2` requires a TPM, and `host` uses the host key alone. `file` keeps a 0600 file, as before M6.
- An edge enrolled with `file` is sealed in place the first time the daemon loads it with sealing on. That key was on disk in the clear, so its blocks, backups and snapshots may still hold it. The daemon therefore renews the certificate with a new key at once, retiring that key.
- A plain copy found beside a sealed key, left by a crash between the two writes, is removed and the key replaced the same way.
- `omnifile-edge --check`, which the unit runs before every start, seals and opens a probe. A box that can't seal therefore fails at start, saying why, rather than at its first renewal.
- The daemon's unit is read-only on `/var/lib/systemd`, so the wizard's edge step makes the host key first.

**M5 (decision 43): the IAM Roles Anywhere key** the edge signs S3 reads with is sealed the same way (`systemd-creds encrypt --name=roles-anywhere-key … /var/lib/omnifile/identity/roles-anywhere.cred`). `omnifile-aws-signing-helper.service` (enabled by the appliance preset and by `edge/systemd/install.sh`, idle until its env file, sealed key and binary exist; the daemon starts after it) loads it with `LoadCredentialEncrypted=`, so systemd decrypts it for that unit only, into memory, and `aws_signing_helper serve` reads it from the unit's credentials directory. Its ARNs (not secrets) are in `/etc/omnifile/aws-signing-helper.env`. The binary is AWS's release, pinned by version and SHA-256 and shipped on the appliance's read-only `/usr` (`/usr/libexec/omnifile/aws_signing_helper`; `appliance/README.md`). Presigned URLs from the service remain the operator's fallback (`edge_credentials = presigned`).

**The wizard's `aws` step (M6)** makes that key and the files around it:
- **The key** is an ECDSA P-256 key made in memory and handed to `systemd-creds encrypt --name=roles-anywhere-key --with-key=auto --tpm2-pcrs=` on stdin, which seals it into `roles-anywhere.cred` (0600). It is never written in the clear, nor put on a command line. A rerun keeps it.
- **The certificate request** (`/etc/omnifile/roles-anywhere.csr`) names the edge as its mTLS certificate does (`CN=omnifile-edge-<id>`, SAN `urn:omnifile:edge:<id>`), so the profile's certificate conditions can name this edge (`threat-model.md` T4). Before enrolment it names the host (`omnifile-edge-<hostname>`): run the step after enrolling to get the edge's name, or set the condition on that name.
- **Which CA signs it (decision 50):** by default **OmniFile's edge CA**, whose certificate the client registered as the Roles Anywhere trust anchor (`deploy/aws/README.md`). The step copies the request to `/var/lib/omnifile/roles-anywhere/request.csr` and, once the edge is enrolled, runs `omnifile-edge roles-anywhere-cert`: the daemon's client sends it to `POST /v0/edges/{id}/aws-certificate` over mTLS, with a proof by the mTLS key (`control-api.md` §2), and installs the answer as `cert.pem` (and `chain.pem`, empty for a CA that signs directly) in that directory, 0644, after checking it is for the request's key. The directory is the daemon's to write (under `/var/lib/omnifile`) and the helper's dynamic user can read it; the env file names it (`OMNIFILE_RA_CERTIFICATE`, `OMNIFILE_RA_INTERMEDIATES`). Before enrolment it waits: the daemon asks at its first heartbeat. **Renewal:** the daemon renews it on the heartbeat that renews its mTLS certificate (or from the certificate's `renew_after`, day 20 of 30), with the same request (the key is never opened outside the helper's unit), and restarts the helper (`[s3] roles_anywhere_dir`, `helper_restart`). The install leaves a mark (`helper-restart-pending`) that only a successful restart clears, so a failed restart is retried on later heartbeats, backing off from a minute to an hour, and logged as an error from the third failure. A request in that directory is what turns this on. **Switching issuers** (either way) removes the other issuer's certificate (and the edge CA's request and state), and stops the helper until the new issuer's certificate is installed. The helper unit also runs only when the configured issuer's certificate is there (`ExecCondition`).
- **The client's own CA** (`[aws] issuer = "client"`; the wizard's "no"): the CA the operator registered as the trust anchor, AWS Private CA or their own. The step then removes the daemon's request, and the certificate comes back through `omnifile-setup aws-cert FILE` (or `[aws] certificate = "FILE"`, used once and then dropped from the saved answers). It is refused unless it is for this key, valid now, not a CA, allows digitalSignature, and is signed with SHA-256 or better. Intermediates after it in the file go to `roles-anywhere-chain.pem` (`OMNIFILE_RA_INTERMEDIATES`). That CA renews and revokes it. `aws-cert` is refused in the default mode.
- **The env file** holds the trust anchor, profile and role ARNs, the region (the trust anchor's), port 9911, and the endpoint and session length when given. The ARNs are checked for shape: partition, service, region, a 12-digit account, `trust-anchor/<uuid>` and `profile/<uuid>` in one account and region, a role name of IAM's characters. A session is 900 to 3600 s (T4: at most an hour).
- The helper starts once the env file, the sealed key, a certificate (either of the two) and the binary are all there.

## The portal side

`POST /v1/edges {name}` creates a pending edge and returns its enrolment code once (`portal-api.md` §5.15). `POST /v1/edges/{id}/bundle` replaces a lost or expired code. Both are for a signed-in ArchiveAdmin only, never an app, because a code is a credential. Both are audited. The Edges page has Add edge, and New code on a pending edge.

## Things to know

- **Admins need a login shell in AD.** The appliance's `smb.conf` gives domain users `/usr/sbin/nologin`, so share users can't log in. `idmap_ad` takes `loginShell` from AD, so give the admins one (RFC2307 attributes).
- **The admin group needs a `gidNumber`.** `idmap_ad` shows no group without one, and the wizard stops with that message.

## Tests

- **Unit tests** (`cargo test -p omnifile-setup`): the answers and their checks, every rendered file, the steps against a scripted machine (first run, rerun, each refusal, secrets off command lines), and the console conversation.
  - A rerun after a failed domain join (WP7 found it looping at the console: the first run's mount made the data disk "in use"): on the console, the first run formats and mounts the disk and stops at the join; the rerun offers that disk to keep, asks no `ERASE`, and its steps join without formatting. Declining stops with nothing changed. A disk mounted at the data root that setup didn't make (another UUID, another label, or the system mounted from it too) is never kept, and is refused and asked again, as is the system disk typed by hand. Unattended, a second boot with the credential still asking to erase reuses the mounted volume, joins, and saves `erase = false`; with the volume not yet mounted, its `omni-data` label still keeps it.
  - Review on #37: a partition of the system disk (its free `/usr` slot), typed or given in an answers file, by its name or a `/dev/disk/by-path` alias, is refused with nothing formatted; a by-path alias of a whole disk formats that disk. Once the first boot has made the volume, a later boot on which it isn't mounted and the answered name now points at another disk (with data or empty) formats nothing, even with `erase = true`. The `aws` step: each malformed ARN named; the key sealed by `systemd-creds` from stdin and in no file or argument; the request for that key, naming the edge; a certificate for another key, expired, not yet valid, a CA, or without digitalSignature refused; the helper restarted only when something changed. Decision 50: by default the request is left for the daemon and `omnifile-edge roles-anywhere-cert` is run once enrolled (standing in for the service), the env file names the edge CA's certificate, a rerun asks for nothing, and switching to `issuer = "client"` withdraws the request. `cargo test -p omnifile-control` covers the request itself against a mock service: the proof, renewal with the mTLS certificate, and a certificate for another key refused.
- **A real domain join** (`edge/crates/omnifile-setup/tests/ad/run.sh`): a Samba AD DC and a member with systemd, in containers. 31/31:
  - the join holds;
  - winbind maps users to their RFC2307 ids;
  - sudo and ssh are for the admin group only;
  - Kerberos works;
  - the password is nowhere on disk;
  - the break-glass account: it stays locked until sealed, then its password works, is nowhere on disk, is refused by ssh, and has sudo; a session of it is marked and logged at `auth.alert` (another user's isn't); sealing again clears the mark;
  - a rerun changes nothing.
- **The appliance** (`appliance/tests/setup.sh`): unattended setup on the real image in qemu, with an empty second disk, a vTPM (swtpm) and no domain controller. 36/36:
  - it formats and mounts the data disk and writes the network, time, edge and update configuration;
  - the break-glass account stays locked, with its sessions recorded;
  - it stops at the join, saying why;
  - the daemon accepts its config and volume;
  - the host key for sealed credentials is made, and the daemon's own unit, sandbox included, seals its keys to the TPM;
  - both servers include the shares the daemon renders, and `smb.conf` with that include parses; from inside its unit's sandbox, the daemon may reload units through `systemctl`, and stopped ones stay stopped;
  - Roles Anywhere (the client-CA option, `issuer = "client"`; the edge CA's path needs a service and is not in this run): the `aws` step seals a key to the vTPM with no plain copy anywhere, writes a request that verifies and the helper's settings; a test trust anchor signs the request; `aws-cert` refuses a certificate for another key and installs this one with its chain; the helper then serves credentials on 127.0.0.1:9911 (IMDSv2), signing CreateSession with that key and certificate against a stand-in for the Roles Anywhere endpoint (`appliance/tests/ra-stub.py`, no AWS);
  - a rerun changes nothing.

  `appliance/tests/smoke.sh` checks that a fresh image waits on tty1 with the questions, not a login prompt, and that the break-glass account exists, locked. The setup test found that the image lacked `sudo` (so `visudo`), which the admins step needs.
- **Service and portal:** `service/tests/web/test_edges.py` and `portal/src/features/edges/edges.test.tsx`.
- **Private networking:** `edge/crates/omnifile-blob/tests/private_only.rs`.
