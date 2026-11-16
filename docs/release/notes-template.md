# OmniFile Gateway {{VERSION}}

{{COUNT}} pull requests merged from `{{FROM}}` to `{{TO}}`. What ships, with this version on every part:

- the edge appliance image (`omnifile-edge_{{VERSION}}.raw`, `.qcow2`, `.vhdx`, `.ova`) and its signed update feed;
- the service, web and relay container images, by digest, with the Bicep parameters that deploy them
  (`omnifile-images-{{VERSION}}.bicepparam`);
- the SBOMs (CycloneDX 1.6, `omnifile-*-{{VERSION}}.cdx.json`), the vulnerability scan (`scan.json`) and the
  appliance's GPL and LGPL sources, in the public sources repository's release `v{{VERSION}}` (decision 46).

## Changes

{{CHANGES}}

## Security

What was scanned: every lockfile, the appliance's Ubuntu packages and Go helper (its SBOM), and the container
images' OS layers when they were built where the scan ran. An OS finding Ubuntu hasn't fixed yet is listed, not
blocking. Anything not scanned is named below.

{{SECURITY}}

## Known issues

{{KNOWN_ISSUES}}

## Verifying this release

- The feed: `SHA256SUMS` is signed by the update key in Key Vault (`SHA256SUMS.gpg`), and every box checks it against
  the keyring its image embeds. `R3-GATE` records the kernel checks the image passed before it was signed.
- Secure Boot: the UKIs are signed with OmniFile's key; its certificate is `omnifile-secure-boot-db.crt`
  (`docs/design/secure-boot.md` shows how to enrol it).
- Each SBOM's `metadata.properties` names the lockfile it was made from and that file's SHA-256.
