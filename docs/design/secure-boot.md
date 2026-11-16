# Secure Boot and signed releases

Decisions 47 and 48. **Status: BUILT** (`appliance/release.sh`, `appliance/pgp_kv.py`,
`appliance/release.conf`). Real Key Vault and GitHub are checks in
`docs/milestones/real-azure-checks.md` ("Release signing").

## What is signed, and with what

| What | Key | Where it lives | Checked by |
|---|---|---|---|
| The feed's `SHA256SUMS` (so every update file) | update key: EC-HSM P-256 | Key Vault Premium, not exportable | `systemd-sysupdate` (gpg) against the image's `/usr/lib/systemd/import-pubring.pgp` |
| The UKIs (kernel, initrd, command line with `usrhash=`) and systemd-boot | Secure Boot key: RSA-HSM 2048 | Key Vault Premium, not exportable | the firmware, against `db` |

Both are signed only by `appliance/release.sh`, after the R3 gate, never on a developer
machine (decision 47). Every signature is a Key Vault `sign` operation, so it is in the
vault's audit log.

- **The feed.** systemd-sysupdate wants an OpenPGP signature, and gpg can't hold a key that
  never leaves an HSM. `pgp_kv.py` builds the OpenPGP v4 packets itself (ECDSA P-256, SHA-256,
  RFC 4880 and RFC 6637) and asks Key Vault to sign only their 32-byte digest
  (`az keyvault key sign --algorithm ES256 --digest`). The keyring that goes into the image
  is made the same way, once (`pgp_kv.py pubkey`), with the key's fixed OpenPGP creation
  time from `release.conf` (it is part of the fingerprint).
- **Secure Boot.** `sbsign` through Microsoft's Azure Key Vault OpenSSL engine (`e_akv`,
  key `vault:<vault>:<key>`) signs the feed's UKI and, in the disk image's ESP, the UKI,
  `systemd-bootx64.efi` and the fallback `BOOTX64.EFI`. `release.sh` checks each file with
  `sbverify` against the certificate, and that stripping the signature gives back exactly the
  bytes the gate booted.
- **The boot loader stays as released.** `systemd-boot-update.service` is disabled (preset):
  it would copy the unsigned loader from a new `/usr` over the signed one. Kernel updates come
  as signed UKIs in the feed; a new boot loader comes with a new image (or a later signed
  transfer).

## The feed on GitHub Releases

`build.sh` writes the feed into the image's sysupdate transfers: by default
`https://github.com/<RELEASE_REPO>/releases/latest/download/` (`release.conf`). GitHub answers
that with a redirect to the latest release's asset, which systemd-sysupdate follows. One
release per appliance version holds the feed files, `SHA256SUMS`, `SHA256SUMS.gpg` and
`R3-GATE`; `release.sh` lays them out in `DIR/release-assets` and prints the
`gh release create vV --latest` command. It uploads nothing itself.

- The repository must be **public** (or mirrored to a public one): a box downloads without a
  token, and GitHub answers 404 for a private repository's assets.
- A box can still be pointed at another feed by the setup wizard (a drop-in per transfer); it
  is checked against the same keyring.
- Only the latest release is reachable at that URL, so a box always moves to the newest
  version. That is what sysupdate does anyway.

## Setting up the keys (the owner, once)

In the release subscription's Key Vault **Premium** (`release.conf` names it):

1. `az keyvault key create --vault-name <vault> --name update-signing --kty EC-HSM --curve P-256 --ops sign verify`
2. `az keyvault key create --vault-name <vault> --name uki-signing --kty RSA-HSM --size 2048 --ops sign verify`
3. Give only the release pipeline's identity `Key Vault Crypto User` on those two keys.
4. Set `UPDATE_KEY` (the full key id, **with its version**), `UPDATE_KEY_CREATED` (now, once), `UKI_KEY` and `UKI_KEY_ID` (with its version) in `release.conf`, then make
   the image keyring: `appliance/pgp_kv.py pubkey --signer kv:<key id> --created <that time>
   --uid "$UPDATE_KEY_UID" --out appliance/keys/omnifile-updates.pgp`, and build with
   `--pubring appliance/keys/omnifile-updates.pgp`.
5. The Secure Boot certificate, self-signed by the Key Vault key through `e_akv`:
   `openssl req -new -x509 -engine e_akv -keyform engine -key vault:<vault>:uki-signing
   -subj "/CN=OmniFile Gateway Secure Boot" -days 3650 -sha256 -out appliance/keys/omnifile-secure-boot-db.crt`.

Both public halves are committed; the private keys never exist outside the HSM.

## Rotating a key

A key rotation is a deliberate release step, never something Key Vault does under a release
(Codex on #31). Key Vault signs with a key's latest version when none is given, so
`release.conf` names each key with its version, and `release.sh` refuses to sign unless:

- the update key, as Key Vault holds that version now (`az keyvault key show`), has the
  fingerprint of a key in `UPDATE_RING`, the keyring images embed, **and** the image the gate
  booted carries it in `/usr/lib/systemd/import-pubring.pgp`;
- the Secure Boot key (`UKI_KEY_ID`) is the key of `UKI_CERT`, the certificate clients
  enrolled; `sbverify` then checks every signature against that certificate.

To rotate the **update key**:

1. Create a new version (or a new key), and make a keyring that holds **both** the old and
   new keys: `pgp_kv.py pubkey` for the new one, appended to the old ring.
2. Release an image built with that ring, still signed by the old key. Boxes take it,
   because they trust the old key, and from then on they trust both.
3. Point `UPDATE_KEY` at the new version, and release. Once no box runs an image without the
   new key, the old key can be dropped from the ring in a later image.

To rotate the **Secure Boot key**, what `appliance/tests/sb-rotation.sh` showed under OVMF
(x86-64, Secure Boot enforcing, 2026-11-03; results in `docs/milestones/results/wp2.md`):

- **The fallback works by itself.** Suppose a box's `db` lacks the new certificate and it
  installs a UKI signed only with the new key. The firmware refuses it ("Error loading EFI
  binary"). systemd-boot counts each refused start as a try, and after three
  (`omnifile-edge_V+0-3.efi`) it boots the old version, all in the same power-on, with no one
  at the console. The box then stays on the old version, because sysupdate won't fetch a
  version that ran out of tries.
- **Enrolling the new certificate from the running box works.** An authenticated append to
  `db`, signed by the client's KEK, can be written through efivarfs (the test does it from the
  guest). Once that is done and the version's tries are reset, the new-key UKI boots with
  Secure Boot on.
- **A dual-signed UKI needs no client action.** A UKI with two Authenticode signatures, the
  old key's and the new key's (`sbsign`, then `pe-addsigs` with the second, detached
  signature), boots on a `db` that holds only the old certificate. So the rotation should
  sign every UKI with both keys until every client has enrolled the new certificate. Signing
  twice is not built into `release.sh` yet.
- **The old certificate can't simply be removed from `db`.** The boot loaders on the ESP
  (`systemd-boot*.efi`, the fallback `BOOT*.EFI`) are the ones the box's first image was
  released with, signed by the old key only. They stay that way, because
  `systemd-boot-update` is off and the feed carries UKIs, not loaders. With only the new
  certificate in `db`, the firmware refuses the loader and the box doesn't start. So the old
  certificate stays in `db` until a signed loader can be delivered to every box, which needs a
  boot-loader transfer in the feed, signed with both keys. That transfer isn't built.

## Enrolling OmniFile's Secure Boot certificate

Secure Boot is optional in v1. A client whose policy requires it enrols our certificate,
`appliance/keys/omnifile-secure-boot-db.crt` (public; its SHA-256 is in every release's
`R3-GATE`), in the VM's UEFI `db`. MOK through shim comes only with Microsoft-signed shim, if a
client needs it later (decision 48). Then:

- **Hyper-V (Generation 2):** keep the template "Microsoft UEFI Certificate Authority" off for
  this VM and add our certificate to `db` from the firmware's Secure Boot setup, or ship the
  VM's firmware variables from a template that has it.
- **vSphere:** enable Secure Boot in VM Options, then in the VM's EFI setup (Device Manager,
  Secure Boot Configuration, Custom mode) enrol the certificate into `db` (DER form:
  `openssl x509 -in omnifile-secure-boot-db.crt -outform DER -out omnifile-db.cer`).
- **KVM/libvirt:** `virt-fw-vars --input OVMF_VARS_4M.fd --output vm_VARS.fd --add-db <owner
  guid> omnifile-secure-boot-db.crt --secure-boot` (python3-virt-firmware), with the Secure
  Boot OVMF build. `appliance/tests/vm.sh` (`vm_secure_boot`) does exactly this.
- **Any firmware, from the ESP:** systemd-boot can enrol keys it finds in
  `/loader/keys/<name>/{PK,KEK,db}.auth` when the firmware is in setup mode
  (`secure-boot-enroll` in `loader.conf`). Not shipped by default: it replaces the platform key.

Then turn Secure Boot on. The appliance boots only UKIs we signed; an unsigned or altered one
is refused by the firmware (`appliance/tests/signed-update.sh`).

## Tests

- `appliance/tests/test_pgp_kv.py`: the keyring and signatures pass gpg as systemd-pull runs
  it; the Key Vault path through `tests/fake_az.py` (Key Vault's answers from a local key; no
  Azure) signs only 32-byte digests; another key, another creation time or a tampered file is
  refused.
- `appliance/tests/signed-update.sh`, in qemu with Secure Boot enforcing and only our test
  certificate enrolled: the unsigned disk doesn't start; the signed one does; a release whose
  `SHA256SUMS.gpg` another key made is refused and nothing is installed; the signed one
  installs from a GitHub-Releases-style feed (`tests/gh-releases-stub.py`, both redirects) and
  boots, Secure Boot still on.
- `appliance/tests/sb-rotation.sh`, under OVMF with Secure Boot enforcing ("Rotating a key"
  above), runs three VMs. The first: a new-key UKI refused and the fallback to the old
  version, then the new certificate appended to `db` and the new UKI booted. The second: a
  dual-signed UKI booted on an old-only `db`. The third: an old-signed loader refused once
  the old certificate is gone. It needs the x86-64 build: qemu's arm64 edk2 has no Secure
  Boot, and Ubuntu's AAVMF doesn't start under HVF.
