# Dependency licences

Every direct dependency, with its version and licence. Rule: **no AGPL at runtime**. GPL components run as separate processes and are never linked.

This repository is licensed under the **Apache License 2.0** (decision 86; `LICENSE`, `NOTICE`). Two directories are exceptions:
- `fsal/` is LGPL-3.0, to match NFS-Ganesha;
- `samba-vfs/` is GPL-3.0-or-later, to match Samba (decision 36). That module runs inside smbd only, and no OmniFile binary links it.

Anyone may use, change and ship the code under Apache-2.0, commercially included, so every dependency must allow that too; AGPL and linked GPL code are ruled out. See `LICENSE`, `NOTICE` (copyright AiLuxis Research), `fsal/COPYING.LESSER` and `samba-vfs/COPYING`.

## How this file is checked

Each release has a CycloneDX SBOM per lockfile, one for the container images and one for the appliance image (`release/sbom.py`), and every transitive package is in them with its own licence. `release/licences.py` checks those SBOMs against the tables below, both ways:
- every direct dependency has a row here, with the licence its own metadata declares and a version that starts with the row's;
- every package a row names is in an SBOM;
- every licence, here or on any component, transitive ones too, follows the rules: never AGPL, SSPL or a non-commercial licence; code shipped with our software is permissive, with MPL-2.0 (used unmodified) and, for Python, LGPL (imported, not linked) allowed when a row names the package; GPL only for the separate-process components (Samba, winbind, SSSD, lsyncd, fswatch) and our Samba module; dev, test and build-only packages and tools may be copyleft but never AGPL; the appliance's Ubuntu packages may be GPL or LGPL (an aggregate distribution whose sources each release publishes, decision 46).

So a row is one or more package names in backticks, a version prefix (or `-`), and one SPDX expression. A `<!-- licences: ... -->` comment sets the ecosystem of the table after it. Add a row when you add a direct dependency; `release/licences.py` fails the release until you do.

## The edge daemon and its tools (Rust: `edge/Cargo.lock`)

`edge/` is built for `x86_64-unknown-linux-gnu`, the appliance's platform. `spikes/filer/guard/Cargo.lock`, the kernel canary's helpers (test-only, not in the image), uses `libc` alone.

<!-- licences: cargo -->
| Package | Version | Used as | Licence | Notes |
|---|---|---|---|---|
| `aws-sigv4`, `aws-credential-types`, `aws-smithy-runtime-api`, `aws-smithy-types`, `aws-smithy-async` | 1 | edge: SigV4 signing of S3 range GETs (M5, `omnifile-blob::s3`) | Apache-2.0 | AWS's Rust SDK crates, without aws-lc or OpenSSL (`cargo tree -i aws-lc-sys` is empty) |
| `aws-smithy-http` | 0.64 | edge: transitive (aws-sigv4) | Apache-2.0 | |
| `base64` | 0.22 | edge: Azure/S3 checksum headers, block IDs | MIT OR Apache-2.0 | |
| `blake3` | 1.8 | edge: whole-file hash for dedupe (`rayon` feature, `omnifile-integrity`) | CC0-1.0 OR Apache-2.0 OR Apache-2.0 WITH LLVM-exception | |
| `bytes` | 1 | edge: buffers for async I/O | MIT | |
| `crc` | 3.4 | edge: test-only cross-check of CRC64-NVME | MIT OR Apache-2.0 | dev-dependency |
| `crc-fast` | 1.10 | edge: CRC64-NVME (hardware-accelerated) | MIT OR Apache-2.0 | `crc64fast-nvme` is deprecated by its author |
| `fastrand` | 2 | edge: jitter and ids | Apache-2.0 OR MIT | |
| `hyper`, `hyper-util`, `http-body-util` | - | edge: test-only HTTP servers in the fakes; also transitive through reqwest | MIT | dev-dependencies |
| `jsonschema` | 0.58 | edge: test-only, the control API contract checks | MIT | dev-dependency |
| `libc` | 0.2 | edge and the kernel canary: syscalls | MIT OR Apache-2.0 | |
| `nix` | 0.31 | edge: Linux syscalls | MIT | |
| `proptest` | 1 | edge: test-only property tests | MIT OR Apache-2.0 | dev-dependency |
| `rayon` | 1.12 | edge: parallel hashing | MIT OR Apache-2.0 | |
| `rcgen` | 0.14 | edge: `omnifile-setup` and `omnifile-control` make keys and certificate requests in memory (sealed by `systemd-creds`) | MIT OR Apache-2.0 | with ring |
| `reqwest` | 0.12 | edge: HTTP; TLS 1.2 allowed for S3 interface endpoints; no OpenSSL or aws-lc | MIT OR Apache-2.0 | |
| `ring` | 0.17 | edge: crypto for rustls, the SHA-256 of snapshot export parts and of the reconciliation digest | Apache-2.0 AND ISC | |
| `rusqlite` | 0.40 | edge: metadata replica (R2), with its bundled SQLite | MIT | SQLite is public domain |
| `rustls`, `rustls-native-certs` | - | edge: TLS | Apache-2.0 OR ISC OR MIT | |
| `ruzstd` | 0.9 | edge: decoding bulk snapshot export parts (control API §6) | MIT | Pure Rust, no C build; the edge only decodes |
| `serde`, `serde_json` | 1 | edge: config and JSON | MIT OR Apache-2.0 | |
| `serde_yaml_ng` | 0.10 | edge: test-only, reads the OpenAPI specs | MIT | dev-dependency |
| `tempfile` | 3 | edge: test-only | MIT OR Apache-2.0 | dev-dependency |
| `thiserror` | 2 | edge: error types | MIT OR Apache-2.0 | |
| `time` | 0.3 | edge: certificate validity times | MIT OR Apache-2.0 | |
| `tokio` | 1 | edge: async runtime | MIT | |
| `tokio-rustls` | 0.26 | edge: test-only TLS servers; also transitive through reqwest | MIT OR Apache-2.0 | dev-dependency |
| `toml` | 0.9 | edge: config | MIT OR Apache-2.0 | |
| `tracing`, `tracing-subscriber` | 0 | edge: logging | MIT | |
| `uuid` | 1 | edge: ids | Apache-2.0 OR MIT | |
| `x509-parser` | 0.18 | edge: `omnifile-setup` checks the certificate the CA returns (`omnifile-setup::aws`) | MIT OR Apache-2.0 | |
| `zeroize` | 1 | edge: wipes key material | Apache-2.0 OR MIT | |

## The service, the relay and the real-cloud harness (Python: `service/uv.lock`, `relay/uv.lock`, `tests/real/uv.lock`)

<!-- licences: pypi -->
| Package | Version | Used as | Licence | Notes |
|---|---|---|---|---|
| `starlette`, `uvicorn` | - | service and relay: the `api`, `enrol`, `web` and relay ASGI apps and servers | BSD-3-Clause | |
| `httpx` | 0.28 | service and relay: HTTP client | BSD-3-Clause | |
| `pyjwt` | 2 | service and relay: Bot Framework and Entra token checks (`crypto` extra) | MIT | |
| `cryptography` | - | service and relay: X.509 CA and CSR handling, Ed25519 intent signatures, JWKS keys; tests/real: the run's test PKI | Apache-2.0 OR BSD-3-Clause | |
| `certifi` | - | service and relay: transitive (httpx, botocore), the CA bundle | MPL-2.0 | File-level copyleft; used unmodified |
| `psycopg` | 3.3 | service: Postgres driver (`binary` extra) | LGPL-3.0-only | Imported as a library, used unmodified; the binary wheel bundles libpq (PostgreSQL licence). Replaceable by the user, as LGPL requires |
| `psycopg-binary` | 3.3 | service: transitive (psycopg's `binary` extra) | LGPL-3.0-only | Same terms as psycopg |
| `psycopg-pool` | 3.3 | service: connection pool | LGPL-3.0-only | Same terms as psycopg; used unmodified |
| `azure-storage-blob` | 12.31 | service: Azure backend (`ext-checksums` extra, with azure-storage-extensions for CRC64) | MIT | Official Microsoft SDK |
| `azure-identity` | 1.26 | service: Entra auth | MIT | Official Microsoft SDK |
| `azure-mgmt-storage`, `azure-mgmt-core` | - | service: container-level immutability policies through Azure Resource Manager (`retention/`, WP4) | MIT | Official Microsoft SDKs; azure-mgmt-storage 25.3, azure-mgmt-core 1.6 |
| `aiohttp` | 3 | service: async transport for the Azure SDK | Apache-2.0 AND MIT | |
| `fastavro` | 1.12 | service: reading Azure blob change-feed segments | MIT | The official changefeed package is still a sync 12.0.0b5 beta |
| `zstandard` | 0.25 | service: compressing bulk snapshot exports (control API §6) | BSD-3-Clause | Python bindings to Facebook's zstd; the bundled libzstd is BSD-3-Clause |
| `opentelemetry-api`, `opentelemetry-sdk` | 1.45 | service: metrics instruments (decision 37) | Apache-2.0 | |
| `azure-monitor-opentelemetry-exporter` | 1.0.0b58 | service: metrics to Application Insights | MIT | Still versioned as a beta; it is what Microsoft's GA Azure Monitor distro builds on |
| `opentelemetry-exporter-otlp-proto-grpc` | 1.45 | service: metrics over OTLP/gRPC to the Container Apps agent (optional extra `otlp`) | Apache-2.0 | Only when `OMNIFILE_METRICS_EXPORTER=otlp`; pulls in grpcio (Apache-2.0) and protobuf (BSD-3-Clause) |
| `aiobotocore` | 3.9 | service: S3 backend (M5), and SQS for its EventBridge events | Apache-2.0 | AWS's SDK, async via aiohttp; botocore ships a NOTICE file |
| `pyiceberg` | 0.12 | service: seeding from S3 Metadata tables and following their journal, `sources/metadata_s3.py` (M5); the `sql-sqlite` extra in tests | Apache-2.0 | `pyarrow` and `rest-sigv4` extras |
| `pyarrow` | 26 | service: reading the Metadata tables' Parquet files (M5) | Apache-2.0 | A large wheel (bundles Arrow C++, Apache-2.0) |
| `pytest` | 9 | service, relay and tests/real: tests | MIT | dev-dependency |
| `pytest-timeout` | 2 | service: a hung test fails instead of hanging the run | MIT | dev-dependency |
| `boto3` | 1 | tests/real: the real-cloud harness's AWS calls (`crt` extra, for CRC64NVME) | Apache-2.0 | Test-only: the harness is never shipped. AWS's SDK; botocore ships a NOTICE file |
| `awscrt` | - | tests/real: transitive (boto3's `crt` extra), the CRC64NVME checksums | Apache-2.0 | Test-only |
| `moto` | 5 | tests/real: the AWS fake a dry run uses (only the services the harness calls) | Apache-2.0 | dev-dependency |
| `flask` | 3 | tests/real: moto's server mode, so the harness and the aws shim share one state | BSD-3-Clause | dev-dependency |
| `flask-cors` | 6 | tests/real: moto's server mode | MIT | dev-dependency |
| `pyyaml` | 6 | service and relay: test-only, reads the OpenAPI specs | MIT | dev-dependency |
| `jsonschema` | 4 | service: test-only OpenAPI conformance checks | MIT | dev-dependency |
| `ruff` | - | service and relay: lint and format | MIT | dev-dependency |

## The portal (npm: `portal/package-lock.json`, `portal/e2e/package-lock.json`)

<!-- licences: npm -->
| Package | Version | Used as | Licence | Notes |
|---|---|---|---|---|
| `react`, `react-dom` | 19.3 | portal: UI (decision 32), bundled into `portal/dist` and served by `web` | MIT | Runs in the browser only |
| `react-router` | 8.4 | portal: client-side routes | MIT | |
| `@tanstack/react-query` | 5.104 | portal: API calls, caching, polling | MIT | |
| `openapi-fetch` | 0.17 | portal: typed client over `portal-api.yaml` | MIT | |
| `@fontsource/space-grotesk`, `@fontsource/space-mono`, `@fontsource/doto` | 5.3 | portal: the UI, label and hero typefaces (WP17), their woff2 files bundled into `portal/dist` and served by `web` from its own origin, never a font CDN | OFL-1.1 | Fonts, used unmodified; each file keeps its copyright notice and licence in its name table, and the packages ship the full OFL text |
| `vite`, `@vitejs/plugin-react` | - | portal: build (the `web` image's Node stage), dev server | MIT | build-time only; Node is not in the runtime image |
| `typescript` | 5.9 | portal: type checking | Apache-2.0 | dev-dependency |
| `openapi-typescript` | 7.13 | portal: generates the client types from `portal-api.yaml` | MIT | dev-dependency |
| `eslint`, `@eslint/js`, `typescript-eslint`, `eslint-plugin-react-hooks`, `eslint-plugin-jsx-a11y`, `eslint-config-prettier`, `globals`, `prettier` | - | portal: lint and format | MIT | dev-dependencies |
| `vitest`, `jsdom`, `msw`, `@testing-library/dom`, `@testing-library/react`, `@testing-library/user-event`, `@testing-library/jest-dom`, `@types/node`, `@types/react`, `@types/react-dom` | - | portal: unit tests and the API mock (`npm run dev:mock`) | MIT | dev-dependencies; MSW is never in the production bundle |
| `axe-core` | 4.14 | portal: accessibility checks in the unit tests | MPL-2.0 | dev-dependency only, used unmodified, never shipped (accepted by the coordinator, M4) |
| `@playwright/test`, `playwright`, `playwright-core` | 1.63 | portal/e2e: M4 browser tests | Apache-2.0 | test-only; Chromium's own licences apply to the browser Playwright downloads, which is never shipped |

## The container images (`deploy/images/images.lock.json`)

Each image's SBOM entry lists the locked packages it installs (the service's or relay's runtime dependencies, and in `web` the portal's production packages) and its base image by digest. The base images' own OS packages are Debian's, each under its own licence, as their publishers' SBOMs list them.

<!-- licences: oci -->
| Package | Version | Used as | Licence | Notes |
|---|---|---|---|---|
| `python` | - | every image's runtime base (`python:3.12-slim`, pinned by digest) | PSF-2.0 | Debian bookworm underneath |
| `ghcr.io/astral-sh/uv` | - | the images' build stage: `uv sync --locked` (uv 0.9, pinned by digest) | MIT OR Apache-2.0 | build stage only |
| `node` | - | the `web` image's build stage (`node:22-bookworm-slim`, pinned by digest) | MIT | build stage only; Node is not in the runtime image |

## The appliance image (decision 38)

The edge appliance image (`appliance/`) is a distribution of Ubuntu 26.04 packages, built by mkosi, with our components added under `/usr`. Among those packages are GPL ones: the Linux kernel, Samba and winbind, parts of systemd, and GnuPG. Shipping the image is shipping their binaries, so each release must offer the corresponding source (GPL-2.0 §3, GPL-3.0 §6). The release keeps the exact source packages for the image's package list (`apt-get source`, or Ubuntu's archive snapshot for the build date), plus `samba-vfs/` and the pinned Samba tarball, and, because the image's NFS-Ganesha is our own build (LGPL-3.0), Ganesha and libntirpc at their pinned commits with `fsal/`. `appliance/source-offer/source_offer.py` collects all of it from the build's package manifest, with a manifest of checksums and the written offer (`appliance/README.md`, "Source offer"). **Where it is published (decision 46):** a public GitHub repository, `omnifile-gateway-sources` (the owner creates it at the first release), as one GitHub Release per appliance release, tag `v<release>`, holding exactly that bundle; `appliance/source-offer/publish_sources.py` checks the bundle and creates or updates the release. The written offer names that release, and requests under it go through the repository's GitHub issues. The first real publish is a release task. Our own binaries in the image keep their own licences: Apache-2.0 for the edge daemon and its tools, LGPL-3.0 for `FSAL_OMNI`, and GPL-3.0-or-later for `vfs_omnifile_prevver`.

The image's packages are those of `appliance/mkosi.conf`'s `Packages=` and what they depend on (the appliance SBOM lists all of them; `release/check_offer.py` checks the source offer has every one). The rows name each package's main licence; the SBOM, given the source offer, records every licence its source's `debian/copyright` names.

<!-- licences: deb -->
| Package | Version | Used as | Licence | Notes |
|---|---|---|---|---|
| `linux-image-generic` | 7.0 | the kernel (fanotify pre-content events, decision 26) | GPL-2.0-only | with the linux-firmware packages it pulls in |
| `systemd`, `systemd-sysv`, `systemd-boot`, `systemd-container`, `systemd-repart`, `systemd-resolved`, `systemd-timesyncd`, `udev` | 259 | init, boot loader, A/B updates (`systemd-sysupdate`), `systemd-creds` | LGPL-2.1-or-later | some tools GPL-2.0-or-later |
| `dbus`, `libdbus-1-3` | 1.16 | system bus | AFL-2.1 OR GPL-2.0-or-later | |
| `kmod` | 34 | module loading | LGPL-2.1-or-later AND GPL-2.0-or-later | |
| `ca-certificates` | - | the CA bundle | MPL-2.0 AND GPL-2.0-or-later | certificates MPL-2.0, scripts GPL |
| `gpg` | 2.4 | `systemd-sysupdate` checks the feed's signature | GPL-3.0-or-later | separate program |
| `tpm2-tools` | 5.7 | the TSS libraries `systemd-creds` loads to seal the edge's keys | BSD-3-Clause | |
| `openssh-server` | - | the administrator's shell (break-glass account) | SSH-OpenSSH | |
| `sudo` | 1.9 | administration | ISC | |
| `iproute2` | 6 | networking | GPL-2.0-only | |
| `rsyslog` | 8 | logging | GPL-3.0-or-later AND LGPL-3.0-or-later AND Apache-2.0 | |
| `xfsprogs` | 6 | the data disk (xfs) | GPL-2.0-only AND LGPL-2.1-only | |
| `attr`, `acl` | - | xattr and ACL tools | GPL-2.0-or-later AND LGPL-2.0-or-later | |
| `libacl1` | 2.3 | NFS-Ganesha, built with `ENABLE_VFS_POSIX_ACL=ON` (decision 28) | LGPL-2.0-or-later | Linked into Ganesha only, not into our code |
| `e2fsprogs` | 1.47 | filesystem tools | GPL-2.0-only AND LGPL-2.0-only | |
| `samba`, `samba-vfs-modules`, `smbclient` | 2:4.23 | SMB server, separate process | GPL-3.0-or-later | Never linked into our binaries. Our one VFS module, `samba-vfs/` (decision 36), is GPL-3.0-or-later and is built from the matching Samba release (`samba-vfs/build.sh`, SHA-256 pinned) |
| `winbind`, `libnss-winbind`, `libpam-winbind` | 2:4.23 | AD identity, system service | GPL-3.0-or-later | Accessed through NSS and its CLI, not linked |
| `krb5-user`, `libgssapi-krb5-2` | 1.22 | Kerberos (per-share `krb5`) | MIT | |
| `rpcbind` | 1.2 | NFS-Ganesha's runtime | BSD-3-Clause | |
| `cifs-utils` | 2:7.4 | annexes: `mount.cifs` and `cifs.upcall` (SMB Kerberos) for the NAS mount (`docs/design/annex.md`) | GPL-3.0-or-later | separate programs, run by `mount` and the kernel's request-key upcall; its debian/copyright (DEP-5) also names LGPL-3+ |
| `nfs-common` | 1:2.8 | annexes' NFS mounts (`mount.nfs`, `rpc.gssd`); NLM's `rpc.statd` and `sm-notify` | GPL-2.0-or-later AND BSD-3-Clause | separate programs; libnfsidmap is BSD-3-Clause. nfs-utils' debian/copyright isn't DEP-5: `release/licence-overrides.toml` names both |
| `nftables` | 1.1 | the inbound firewall (`omnifile-firewall.service`) | GPL-2.0-only | separate program (`nft -f`) |
| `libcap2` | 1:2.75 | NFS-Ganesha's runtime | BSD-3-Clause OR GPL-2.0-only | |
| `libjemalloc2` | 5.3 | NFS-Ganesha's allocator | BSD-2-Clause | |
| `libkeyutils1` | 1.6 | NFS-Ganesha's runtime | LGPL-2.0-or-later | |
| `libnfsidmap1` | - | NFS-Ganesha's id mapping | BSD-3-Clause | |
| `liburcu8t64` | 0.15 | NFS-Ganesha's runtime | LGPL-2.1-or-later | |
| `libuuid1` | 2.41 | NFS-Ganesha's runtime | BSD-3-Clause | |

What is in the image but not an Ubuntu package (besides our own `omnifile-edge` tools, `FSAL_OMNI` and `vfs_omnifile_prevver`):

<!-- licences: generic -->
| Package | Version | Used as | Licence | Notes |
|---|---|---|---|---|
| `nfs-ganesha` | V15.6 | NFS server; our FSAL builds against it (`/usr/lib/omnifile/ganesha`) | LGPL-3.0-or-later | Our FSAL module is LGPL and stays small; pinned in `fsal/GANESHA_VERSION` |
| `ntirpc` | - | libntirpc, Ganesha's RPC library (its submodule) | BSD-3-Clause | |
| `github.com/aws/rolesanywhere-credential-helper` | 1.8.5 | edge: `aws_signing_helper`, the Go module's binary; turns the appliance's X.509 key (sealed to the TPM) into temporary AWS credentials for S3 reads, on a loopback IMDSv2 endpoint (`aws_signing_helper serve`, `edge/systemd/omnifile-aws-signing-helper.service`). The service no longer uses it: it signs in by web identity from its managed identity | Apache-2.0 | AWS's binary (aws_signing_helper 1.8.5), run as a separate process; not linked. Shipped at `/usr/libexec/omnifile/aws_signing_helper`, pinned in `appliance/third-party/aws_signing_helper/PIN` (Linux x86-64, SHA-256 `beec9ed1c492d93db809890f16713e3556353294b823c2184ad4e891f1b2b54d`, AWS's published checksum); `appliance/build.sh` refuses any other file. Its LICENSE and NOTICE ship with it (`/usr/share/omnifile/licenses/aws_signing_helper/`) |

## Tools that build, test and release it (`release/tools.toml`)

None of these ships. Each is a separate program, and the tools SBOM lists them with where each is pinned.

<!-- licences: tool -->
| Package | Version | Used as | Licence | Notes |
|---|---|---|---|---|
| `mkosi` | 26 | appliance: the image build (`appliance/build.sh`), with systemd-repart and systemd-ukify (LGPL-2.1-or-later), erofs-utils (GPL-2.0-or-later), zstd (BSD-3-Clause OR GPL-2.0-only) and patchelf (GPL-3.0-or-later) | LGPL-2.1-or-later | build-time tools; not linked into anything we ship; the build's BUILD-INFO records their versions |
| `qemu` | - | appliance: the qemu tests, the R3 gate and `convert.sh`, with OVMF/edk2 (BSD-2-Clause-Patent) and swtpm (BSD-3-Clause) | GPL-2.0-only | test and build tools only |
| `sbsigntool` | 0.9 | release: signs the UKI and systemd-boot for Secure Boot, with mtools (GPL-3.0-or-later) | GPL-3.0-or-later | |
| `gnupg` | 2 | release tests: checks the feed's signature | GPL-3.0-or-later | |
| `azure-keyvault-openssl-engine` | - | release: the `e_akv` OpenSSL engine for the Key Vault Secure Boot key | MIT | not pinned yet |
| `azure-cli` | 2 | release: Key Vault signing (`pgp_kv.py`), `az bicep` | MIT | |
| `bicep` | 0.48 | release: compiles the parameters file, compile only | MIT | |
| `gh` | 2 | release: `gh release create` | MIT | |
| `docker-buildx` | 0.38 | the container image builds (`deploy/images/build.sh`) | Apache-2.0 | |
| `uv` | 0.9 | syncs the Python environments from their locks | MIT OR Apache-2.0 | |
| `osv-scanner` | 2.7 | release: the vulnerability scan (`release/scan.py`) | Apache-2.0 | |
| `cyclonedx-python-lib` | 11.12 | release: the CycloneDX 1.6 schema check (`release/validate_sbom.py`) | Apache-2.0 | |
| `pester` | 5.7.1 | `tests/windows`: the Windows client kit's unit tests | Apache-2.0 | test-only, from the PowerShell Gallery |
| `@microsoft/app-manifest` | 1.1.3 | `deploy/teams/build.sh`: the Teams app manifest's schema | MIT | dev tool, fetched with `npm pack` and checked by its sha512; its schema file is also kept as a test fixture in `tests/real/fakes/fixtures/teams-schema/` |
| `ruff` | 0.8.6 | `tests/real`: lint, through uvx | MIT | |
