# vfs_omnifile_prevver (Samba VFS module)

This is a Samba VFS module, licensed **GPL-3.0-or-later** to match Samba (`COPYING`). The rest of the repository is Apache-2.0, and `fsal/` is LGPL-3.0. The module runs inside smbd and is never linked into any OmniFile binary (decision 36). It doesn't talk to any OmniFile process.

## What it is for

A share's rewind trees (`<data>/.omni/snap/<share>/<label>/…`, decision 30) are shown to Windows as **Previous Versions** through Samba's `vfs_shadow_copy2`. The edge daemon fills a rewind directory only when someone opens it: the smb-guard holds smbd's open of an unfilled directory (`DIR_UNPOPULATED`) while the daemon fills it from the service's as-of listing (`docs/design/fsal-namespace.md` §7).

- **Browsing** a previous version's folders opens them, so they fill. That needs no module.
- **Opening a file directly** ("Previous Versions → Open", or any `@GMT-…` path) opens no directory. `shadow_copy2` only stats the path, which doesn't exist yet, so the client gets `NOT_FOUND`. The client probes each version this way, so an unfilled label wouldn't even be listed for the file.

This module, stacked before `shadow_copy2`, opens each directory of such a path under `shadow:snapdir/<label>/` first. The guard holds each open until that directory is filled, so the file is there by the time `shadow_copy2` resolves the path.

```
vfs objects = full_audit omnifile_prevver shadow_copy2
```

`omniacl render` (`ShareConfig::smb_prevver`) writes this line only when the module is installed in smbd's `MODULESDIR/vfs`. smbd refuses to connect a share that names a module it can't load.

**Behaviour:**
- It reads `shadow_copy2`'s own parameters (`shadow:snapdir`, `shadow:format`, `shadow:localtime`). It does nothing unless `shadow:snapdir` is absolute.
- It acts only on paths with a previous-version timestamp (`smb_fname->twrp`), in `stat`, `lstat`, `fstatat` and `openat`.
- It opens directories only, read-only, as the session's user, and closes them at once. It stops at `..`.
- A directory it can't open is left to `shadow_copy2`, which answers as it always does. A fill that fails gives the client an SMB error.

**Cost:** each open of a previous version's file costs one `open`/`close` per directory of its path. The first open of an unfilled label also waits for the fill, which is one as-of call per directory. The Windows Previous Versions tab probes a file in every label. Over 53 labels that took 1.7 s and 75 directory fills the first time (measured on the M5 stack). Idle directories are emptied again after the daemon's `idle_evict_s`.

## Build and install

```
samba-vfs/build.sh              # → samba-vfs/build/omnifile_prevver.so
sudo samba-vfs/install.sh       # → $(smbd -b MODULESDIR)/vfs/omnifile_prevver.so
```

Samba has no out-of-tree module SDK, and Ubuntu's `samba-dev` ships no VFS headers. So `build.sh` builds the module inside the upstream release that matches the installed smbd:
- The tarball must match a SHA-256 pinned in `SHA256SUMS`. Each one was recorded after its signature checked out with Samba's key `AA99442FB680B620`.
- It links the object against **no Samba library** (only glibc: libc, and on arm64 the dynamic loader): smbd already holds every symbol the module uses. So the same `.so` loads into the distribution's smbd, whose private libraries are named differently from an upstream build's.
- This is sound because nothing the module touches depends on configure options: `smb_filename`, `files_struct`, `connection_struct`, `share_params` and `vfs_fn_pointers` have no conditional fields.
- smbd refuses a module built for another VFS interface version (51 in Samba 4.23); see "Does smbd load it?" below for how that is checked.
- On a host with smbd, `build.sh` checks that every Samba symbol the module uses is exported by the libraries smbd loads.

A Samba upgrade needs the module rebuilt for the new release: add its checksum, then build. Build dependencies are listed in `build.sh`. A build takes about 2 minutes on 4 cores.

## Does smbd load it?

smbd refuses a module built for another VFS interface version, and then every share that names it stops connecting. A Samba upgrade can bring one (51 in Samba 4.23). Checking that the file exists is not enough, so there is a load check (`edge/crates/omnifile-acl/src/smb_module.rs`):

1. **Build info.** `build.sh` bakes `OMNIFILE-PREVVER-BUILD vfs_interface=51 samba=4.23.6` into the `.so` (from the macros it was compiled with). The check reads it without loading anything and compares the Samba release with the running `smbd --version`. A different release series (4.23 against 4.24) is a failure, since interface versions change between series. A different patch release is a note. `smbd -b` doesn't print its VFS interface version, so the release series stands in for it; if a future smbd prints it, it is compared exactly.
2. **A real load.** A throwaway `smbd` on a loopback port serves a scratch guest share whose `vfs objects` is just this module, and `smbclient` lists it. smbd loads a share's modules when a client connects, so this is the proof. (`testparm` does not load VFS modules.) It needs root and `smbclient`; without them the check says it did not load-test.

A failure names the module, its path, and both Samba versions, and says to rebuild and reinstall.

Where it runs:
- `omniacl check-prevver [--build-only]` (the `omnifile-acl` example CLI). `omniacl render` runs it for `OMNIACL_SMB_PREVVER=auto|on` and stops with the message rather than writing a stanza smbd would refuse.
- The edge daemon (`[shares] smb_prevver = "auto"`) runs step 1 each time it renders shares. If the installed module is wrong, it logs the error and renders the shares without the module, so they still connect (Previous Versions then work through `shadow_copy2` alone, except for files opened directly in an unfilled label).
- `omnifile-setup check-smb-module`, run by the `ad` step after `testparm` and by `appliance/tests/smoke.sh`.

## Tests

- **smb-guard A-suite `x03_prevver_open.sh`**: Ubuntu's smbd with the module installed, against the guard harness. It checks:
  - without the module, opening a file directly is `NOT_FOUND`;
  - with it, the label fills for smbd's open and the file reads;
  - there is no second fill, and no wait once filled;
  - a failed fill is an SMB error, and smbd keeps serving.
- **`tests/e2e/m5_exit_test.sh` step 8**: on the M1 stack with the real daemon, a file in a subfolder of yesterday's unfilled label. It is `NOT_FOUND` through `shadow_copy2` alone. With the module, the file's bytes arrive, and the label and its folder are filled by the daemon's guard.

Both CI lanes build the module with `build.sh`.
