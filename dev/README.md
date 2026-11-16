# OmniFile dev environment (M0–M4)

Two Lima VMs on an Apple-silicon Mac, both Ubuntu 26.04 (kernel 7.0):

| VM | What it is | Size |
|---|---|---|
| `omnifile-dc` | Samba AD domain controller, realm `OMNI.TEST`, NetBIOS `OMNI`. Stands in for the client's on-prem AD. | 2 vCPU, 2 GiB |
| `omnifile-edge` | The edge filer dev box: xfs volumes, Rust and C build deps, Samba + winbind joined to the DC, NFS/SMB client tools. | 4 vCPU, 8 GiB |

The existing `omnifile-spike` VM is separate. These scripts never touch it.

## Commands

```sh
dev/up.sh               # create/start both, DC first, then edge + domain join. Idempotent.
dev/status.sh           # VM state, IPs, samba-ad-dc state, domain trust, data volume
dev/down.sh             # stop both (state kept)
dev/down.sh --destroy   # stop and DELETE both VMs and dev/.secrets/ad.env
```

A from-scratch `up.sh` took about 2.5 minutes with the Ubuntu image already cached (add the image download the very first time). Restarting stopped VMs takes about 25 seconds; a no-op run takes a few seconds.

Shells:

```sh
limactl shell omnifile-dc
limactl shell omnifile-edge
limactl shell --workdir / omnifile-edge -- sudo <command>
```

## What is where

```
dev/
  up.sh down.sh status.sh      entry points
  lima/omnifile-dc.yaml        DC VM definition
  lima/omnifile-edge.yaml      edge VM definition
  scripts/dc-provision.sh      runs in the DC: install, provision domain, seed users/groups
  scripts/edge-provision.sh    runs in the edge: packages, xfs volumes, DNS, Kerberos, join
  scripts/build-artifacts.sh   Mac: /opt/ganesha + libfsalomni.so from the fsal/build image -> dev/.artifacts/
  scripts/install-ganesha.sh   edge: Ganesha V15.6 + FSAL_OMNI + nfs-ganesha-omni.service
  scripts/build-edge.sh        edge (as the user): builds omnifile-edge from a private copy of edge/
  scripts/install-edge.sh      edge: omnifile-edge, its unit, /etc/omnifile/edge.toml (from edge/edge.toml)
  scripts/install-service.sh   edge: Postgres 17 (PGDG), sim facade, enrol, api, worker as systemd units
  scripts/install-e2e-tools.sh edge: what the M5 exit test needs (libnfs, nftables, conntrack, vfs_omnifile_prevver)
  scripts/lib.sh               shared shell helpers
  edge/edge.toml               the dev/e2e daemon config (short timers)
  .secrets/ad.env              GENERATED, git-ignored: AD passwords and DC address
  .artifacts/                  GENERATED, git-ignored: ganesha.tar, fsal/libfsalomni.so, D-Bus policy
```

The repo is mounted at `/work` in both VMs (read-only in the DC, writable in the edge, like the spike VM).

`/work` is whichever worktree `dev/up.sh` mounted (the `location` in `~/.lima/omnifile-edge/lima.yaml`), usually the long-lived parent worktree. It may be at a **different revision than `main`**, and than the branch you are working on: a script run from `/work` is that worktree's version, not yours. To run another checkout's scripts, copy it into the VM and set `REPO` (`REPO=/var/tmp/x /var/tmp/x/dev/scripts/install-service.sh`).

The service (and, with `install-m4.sh`, the relay) is installed **non-editably** into `/opt/omnifile-service/venv` (and `/opt/omnifile-relay/venv`) and rebuilt from the checkout on every run of `install-service.sh`. An editable install left a `.pth` pointing into the checkout it was installed from, so removing that checkout (a deleted work tree, a `/work` mount that moved on) made every `omnifile-dev-*` unit fail to start. The consequence: **a change to `service/` takes effect after you rerun `install-service.sh`** (then restart the units), not on restart alone. The `install-m4.sh` units still run `dev/m4/*.py` and serve `portal/dist` from `$REPO`.

### Network

Both VMs join Lima's rootless `user-v2` network (192.168.104.0/24). It needs no `socket_vmnet` and no sudo. VMs reach each other by IP, and each still reaches the internet through Lima's NAT. The DC gets its address by DHCP and has kept it across restarts, but `up.sh` re-reads it every time.

DNS: the DC runs Samba's internal DNS. The edge sends only `omni.test` queries to the DC (a global systemd-resolved routing domain in `/etc/systemd/resolved.conf.d/omnifile-dev.conf`). Everything else keeps using Lima's resolver, so the edge still resolves the internet while the DC is stopped.

### Edge storage

Sparse loop files on the VM disk, formatted xfs (the filesystem fanotify pre-content events need):

| Mount | Image | Size |
|---|---|---|
| `/srv/omnifile/data` | `/var/lib/omnifile/data.img` | 64 GiB |
| `/srv/omnifile/cache` | `/var/lib/omnifile/cache.img` | 16 GiB |

Both are in `/etc/fstab` and come back after a restart.

### Seed data (for the permission matrix in `docs/design/access-control.md`)

| User | uid | Groups |
|---|---|---|
| `geo-reader` | 20001 | `Proj-Readers` |
| `geo-writer` | 20002 | `Proj-Writers` |
| `geo-none` | 20003 | none (Domain Users only) |
| `omni-admin` | 20004 | `OmniFile-Admins` |
| `geo-owner` | 20005 | `Proj-Owners` |
| `geo-nested` | 20006 | `Proj-Readers-Nested`, which is a member of `Proj-Readers` |

Groups: `Proj-Readers` 30001, `Proj-Writers` 30002, `Proj-Owners` 30003, `OmniFile-Admins` 30004, `Proj-Readers-Nested` 30005. `Domain Users` is gid 10000 and is every user's primary group. All carry RFC2307 `uidNumber`/`gidNumber`, so winbind's `idmap_ad` (schema mode `rfc2307`, range 10000-999999) returns the same ids on every Linux client. The default idmap range is `3000-7999` (tdb).

Passwords are generated when the DC is first provisioned (complexity on, never expire) and saved in `dev/.secrets/ad.env` as `AD_ADMIN_PASSWORD` and `AD_PASSWORD_GEO_READER`, `AD_PASSWORD_GEO_WRITER`, and so on. Nothing prints them. To use one:

```sh
set -a; . dev/.secrets/ad.env; set +a
# inside the edge VM the repo is at /work, so the file is /work/dev/.secrets/ad.env
```

`dev/down.sh --destroy` followed by `dev/up.sh` makes a new domain and new passwords.

## Checking the join (inside the edge VM)

winbind lowercases names in `id`/`getent` output (`proj-readers`); lookups are case-insensitive, so `getent group Proj-Readers` still works.

```sh
limactl shell --workdir / omnifile-edge -- sudo wbinfo -t        # trust secret OK
limactl shell --workdir / omnifile-edge -- wbinfo -u             # AD users
getent passwd geo-reader; id geo-reader; id geo-nested           # AD ids, nested groups
getent group Proj-Readers
# Kerberos (password read from the env file, not typed on the command line):
limactl shell --workdir / omnifile-edge -- bash -c \
  '. /work/dev/.secrets/ad.env; echo "$AD_PASSWORD_GEO_READER" | kinit geo-reader@OMNI.TEST && klist'
```

## Running the spike test suites later

The spike scripts expect the repo at `/work` and xfs volumes under `/srv/omni-b`, which the edge VM also provides:

```sh
limactl shell --workdir / omnifile-edge -- sudo /work/spikes/filer/fsal/setup_volumes.sh
limactl shell --workdir / omnifile-edge -- sudo /work/spikes/filer/fsal/build.sh          # Ganesha + FSAL (spike build)
limactl shell --workdir / omnifile-edge -- sudo bash /work/spikes/filer/guard/tests/a1_a4_policies.sh
```

## The M1 stack on the edge VM (the appliance's layout)

`tests/e2e/run.sh` does all of this and then runs the M1 exit test; the steps by hand:

```sh
dev/scripts/build-artifacts.sh                                             # Mac (Docker)
limactl shell --workdir / omnifile-edge -- sudo /work/dev/scripts/install-ganesha.sh /work/dev/.artifacts
limactl shell --workdir / omnifile-edge -- sudo /work/dev/scripts/install-edge.sh     # builds as you, installs as root
limactl shell --workdir / omnifile-edge -- sudo /work/dev/scripts/install-service.sh
```

| Unit | What | Where |
|---|---|---|
| `omnifile-edge.service` | the daemon (`/usr/local/bin/omnifile-edge`, `/etc/omnifile/edge.toml`), with the smb-guard inside: `Type=notify` (READY once the guard is armed), the fanotify group in the fd store, the private fill mount `/run/omnifile-fill`, `RestartMode=direct`; confined (capability bounding set, `ProtectSystem=strict`, `NoNewPrivileges`) | `edge/crates/omnifile-edge/systemd/` + `edge/systemd/omnifile-edge.service.d/10-smbguard.conf` |
| `smbd.service` | Samba from `edge/config/samba/smb.conf.tmpl`, shares in `/etc/samba/omnifile-shares.conf`; `Requires=`/`BindsTo=`/`After=` the daemon, so it never serves the volume without an armed guard | `edge/systemd/smbd.service.d/` |
| `nfs-ganesha-omni.service` | Ganesha + FSAL_OMNI under its real unit (its capability set, not `--privileged`); `BindsTo=` the daemon | `fsal/systemd/` |
| `omnifile-dev-sim` | sim Azure Blob facade, two-account layout, `http://127.0.0.1:10000`, key in `/var/lib/omnifile-dev/sim-key.json` | generated |
| `omnifile-dev-sim-worm` | sim Azure Blob facade, WORM layout, `http://127.0.0.1:10001`, key in `/var/lib/omnifile-dev/sim-worm-key.json` | generated |
| `omnifile-dev-enrol` | `enrol` over TLS, `:8444` | generated |
| `omnifile-dev-api` | `api`, mTLS, `:8443` | generated |
| `omnifile-dev-worker` | worker; "stub after N minutes" with N = `OMNIFILE_STUB_AFTER_MINUTES` (0) | generated |

Everything generated (dev CA, server certificate, tokens, database password, sim key) lives in `/etc/omnifile-dev` and `/var/lib/omnifile-dev` inside the VM, never in the repository. The service runs from its own environment (`/opt/omnifile-service/venv`), not the Mac's `service/.venv`. The edge is enrolled with `omnifile-edge enrol <bundle-file>` (the bundle comes from `python -m omnifile_service.enrol add-edge`; `tests/e2e/e2e.py seed` does both). Logs: `journalctl -u omnifile-edge -u nfs-ganesha-omni -u 'omnifile-dev-*'`.

### M2: SMB, identity, the smb-guard

```sh
limactl shell --workdir / omnifile-edge -- sudo /work/dev/scripts/install-samba.sh   # smb.conf from the template, nfs/ keytab
tests/e2e/run.sh --m2          # install everything, then tests/e2e/m2_exit_test.sh
tests/e2e/run.sh --m2-test     # the M2 test only
```

`install-edge.sh` also records the data volume's xfs UUID in `edge.toml` (`[smbguard] fs_uuid`), installs the guard's drop-ins (`edge/systemd/install.sh`), `omnifile-guardctl`, `smbguard-tool` and `omniacl`, and a dev-only drop-in that lets the Blob client use the http sim (`OMNIFILE_BLOB_ALLOW_HTTP=1`). Stop the guard unit only with `omnifile-guardctl stop` (it refuses while smbd or Ganesha run). The smb-guard A-suite runs anywhere with systemd and a spare loop volume: `sudo bash edge/crates/omnifile-smbguard/tests/a-suite/run.sh --bin-dir <release dir>`.

Before stopping Ganesha, unmount NFS clients of it: a hard mount of a server that is gone makes `umount` hang in the kernel until the server is back.

Rust: `rustup` (stable, with rustfmt and clippy) is installed for the VM user, so `cargo test` works in `/work/edge`. For speed, set `CARGO_TARGET_DIR=$HOME/target` so build output stays off the virtiofs mount.

### M3: tiering, Archive, WORM on a virtual clock

```sh
tests/e2e/run.sh --m3          # install everything (+ Samba), then tests/e2e/m3_exit_test.sh
tests/e2e/run.sh --m3-test     # the M3 test only
```

Both sim facades, the api and the worker share one virtual clock, `/var/lib/omnifile-dev/clock-offset` (`OMNIFILE_DEV_CLOCK_FILE`, dev mode only). It holds libfaketime's `+<seconds>` and is normally `+0`, real time. `POST /_sim/advance {"seconds": N}` on either facade moves them all; `tests/e2e/m3.py advance N` does the same. The M3 test also runs Postgres under libfaketime on the same file (via `/etc/postgresql/17/main/environment`, restored afterwards), so the catalog's `now()` moves too. At the end it puts the clock back to `+0` and restarts the facades, which issue keys on the clock. The worker's dev knobs are `OMNIFILE_WORKER_MAX_SWEEP_S` and `OMNIFILE_RECALL_POLL_S`; the test sets them in a drop-in under `/run`.

### M5: S3-resident files, migrations, point-in-time trees

```sh
tests/e2e/run.sh --m5          # install everything (+ Samba, + the test's tools), then tests/e2e/m5_exit_test.sh
tests/e2e/run.sh --m5-test     # the M5 test only
```

`install-e2e-tools.sh` (also the last step of `edge-provision.sh`; run it once by hand on a VM provisioned before it existed) installs `libnfs-utils`, `nftables` and `conntrack` (Ubuntu's kernel has no `/proc/net/nf_conntrack`, so the test reads the flow table with `conntrack`), and builds `vfs_omnifile_prevver` (`samba-vfs/`, GPL-3.0) for the VM's smbd, staged at `/usr/local/lib/omnifile/samba-vfs/`. The test installs that module into smbd itself for step 8 and removes it afterwards; nothing of ours links it.

### M4: portal, Teams and the relay, on one machine (no VMs)

M4's stack needs no edge and no kernel features, so it runs anywhere with Python (uv), Node 22 and a Postgres 16+ server: a Mac, a dev VM or a CI runner. `dev/m4/stack.py` starts each part as its own process, with everything it generates (dev CA, server, relay and edge certificates, the intent signing key, secrets, logs) in a work directory, never in the repository:

```sh
(cd portal && npm ci && npm run build)                 # web serves portal/dist
(cd service && uv sync) && (cd relay && uv sync)
export OMNIFILE_DATABASE_URL=postgresql://…/omnifile_m4  # a migrated or empty database
service/.venv/bin/python dev/m4/stack.py up            # prints stack.json: URLs, files, people
service/.venv/bin/python tests/e2e/m4.py seed          # an edge, a share, archived and versioned files
service/.venv/bin/python dev/m4/stack.py down
```

| Process | Port | What |
|---|---|---|
| sim | 18400 | sim_http, two-account layout, `/_sim` admin, on the shared virtual clock |
| api | 18443 | `api`, direct mutual TLS (dev CA); Teams intents from the pinned relay certificate |
| web | 18445 | `web` over TLS (`https://localhost:18445`), the dev OIDC issuer at `/dev-idp`, `portal/dist` |
| worker | | restores, notify (Teams through the fakes), portal jobs; virtual clock, 2 s polls |
| graph | 18460 | fake Microsoft Graph and Entra token endpoint (`dev/m4/fakes.py`) |
| bot | 18461 | fake Bot Connector; the fake Teams client's JWKS (`/_teams/…`) and test controls (`/_fake/…`) |
| relay | 18462 | the real relay from `relay/` (`dev/m4/relay.py`): only its Bot Framework JWKS fetch is pointed at the fake Teams |

- **People** (`dev/m4/world.py`): one tenant, one AD domain (`S-1-5-21-4000-5000-6000`), groups Seismic Owners/Writers/Readers, Finance and OmniFile Admins, and users Ada Admin and Abe Admin (ArchiveAdmin), Olga Owner, Rita Reader, Sid Stranger and Sam Steward (DataSteward). The dev issuer's picker lists them; the fake Graph knows the same SIDs and objectIds.
- **Teams by hand:** `GET http://127.0.0.1:18461/_fake/conversations` shows every card version; `POST /_fake/teams/click {"conversation_id", "title", "clicker"}` clicks a button through the relay, as Teams would.
- `M4_WORK` (default `$TMPDIR/omnifile-m4`) and `M4_PORT_BASE` (default 18400) move the work directory and the ports. Logs: `$M4_WORK/logs/<process>.log`.
- **On the dev VMs** (systemd, next to the M1–M3 units), after `install-service.sh`: `limactl shell --workdir / omnifile-edge -- sudo /work/dev/scripts/install-m4.sh` adds `omnifile-dev-fakes`, `omnifile-dev-relay` and `omnifile-dev-web` on the same ports, and drop-ins that give `api` and the worker their Teams settings. The fake Graph is seeded with the domain's real users and groups by SID (winbind), so a real SMB or NFS read of an archived stub by `geo-reader` sends `geo-reader` a card: `curl http://127.0.0.1:18461/_fake/conversations` inside the VM. Build the portal on the Mac first.
- The M4 exit test does all of this on a fresh database and then runs the browser and service checks: `OMNIFILE_DATABASE_URL=<server URL> tests/e2e/m4_exit_test.sh` (`docs/milestones/M4.md`, results).

## Troubleshooting

- **Join fails with a DNS error:** the DC is stopped or got a new address. Run `dev/up.sh`; it refreshes the edge's DNS route.
- **`wbinfo -t` fails after the DC was rebuilt:** `edge-provision.sh` wipes the stale machine secret and rejoins; just re-run `dev/up.sh`.
- **Start from scratch:** `dev/down.sh --destroy && dev/up.sh`.
