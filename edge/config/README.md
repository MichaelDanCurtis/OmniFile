# Edge filer configuration templates (decision 28)

The Samba and Ganesha **global** sections the edge installs, rendered by the
daemon at setup. Per-share sections are rendered from the catalog's
`share.upsert` row by `omnifile_acl::ShareConfig` (`render_smb`,
`render_ganesha`) into included files, so a share change never rewrites the
globals. On the appliance the daemon renders them itself
(`omnifile_edge::shares`, `[shares] render = true`) into
`/var/lib/omnifile/shares/`, which the two includes below include, and
reloads both servers (`docs/design/share-provisioning.md`).

| File | Installed as | Includes |
|---|---|---|
| `samba/smb.conf.tmpl` | `/etc/samba/smb.conf` | `/etc/samba/omnifile-shares.conf` |
| `ganesha/ganesha.conf.tmpl` | `/etc/ganesha/ganesha.conf` | `/etc/ganesha/omnifile-exports.conf` |

`tests/acl/matrix.sh` renders and runs exactly these on the matrix VM.

## The rules they carry

- **One permission store.** POSIX ACLs on xfs. Samba maps Explorer's NT ACLs to
  and from them (`nt acl support = yes`, `inherit acls = yes`); `vfs_acl_xattr`
  and `vfs_acl_tdb` are never loaded (`omnifile_acl::check_smb_conf` rejects
  them). Samba keeps two harmless helpers: `user.SAMBA_PAI` (inheritance flags
  for Explorer's display) and `user.DOSATTRIB`.
- **One identity source.** winbind with `idmap_ad` (RFC2307), for smbd, for
  Ganesha (`Manage_Gids` group lookups and krb5 principal mapping through
  `/etc/idmapd.conf`, method nsswitch) and for the daemon (`wbinfo`).
- **Ganesha must be built with `ENABLE_VFS_POSIX_ACL=ON`** (`fsal/build/Containerfile`).
  FSAL_VFS does not switch to the caller's credentials, so Ganesha's own access
  check is the only one on NFS; without the option it checks mode bits only.
- **Every export:** `Manage_Gids = true` (the client's group list is replaced
  by a winbind lookup, so `sec=sys` clients cannot claim groups),
  `Squash = Root_Squash`, `Attr_Expiration_Time = 10`.
- **Host-trusted shares** (`sec=sys`): `Access_Type = None` at the export and
  one `CLIENT` block listing named hosts or subnets (no `*`, no `/0`).
  **Proven shares**: `SecType = krb5, krb5i, krb5p` only.

## How long changes take (measured in `tests/acl`)

| Change | NFS | SMB |
|---|---|---|
| ACL edited in Explorer (SMB) | enforced within `Attr_Expiration_Time` (10 s; Ganesha's default 60 s) | immediately |
| User removed from an AD group | within `Manage_Gids_Expiration` (300 s; at most 600 s with winbind's `winbind cache time` 300 s); measured 291–306 s | at the user's next session (measured: immediately); an open session keeps its logon token |

Lower values cost more winbind and getattr traffic. The portal must state
these bounds next to group management.

## Kerberos and keytabs

- **One keytab, `/etc/krb5.keytab`,** owned by the machine account
  (`kerberos method = secrets and keytab`, `dedicated keytab file`). winbind
  rotates the machine password and the keytab together.
- **NFS needs `nfs/<fqdn>@REALM`** in that keytab and as an SPN on the
  computer object. At join time:
  ```sh
  net ads setspn add nfs/<fqdn>  -U <admin>     # and nfs/<short name>
  net ads keytab create -P                      # writes host/, cifs/ and nfs/ entries
  klist -k /etc/krb5.keytab | grep nfs/
  ```
  Samba ≥ 4.21 also has `sync machine password to keytab` for keeping extra
  SPNs in the keytab across password changes; the setup wizard should use it
  once a pilot confirms the syntax on the appliance's Samba.
- **Ganesha:** `NFS_KRB5 { PrincipalName = nfs; KeytabPath = /etc/krb5.keytab; }`.
  Principals map to uids through libnfsidmap (`/etc/idmapd.conf`:
  `Domain = <ad dns domain>`), then winbind.
- **Clients:** NFS clients need `rpc.gssd` and a user ticket
  (`/tmp/krb5cc_<uid>` or KCM). CIFS clients mount with `sec=krb5,cruid=<uid>`.
- **Per-share choice and its cost:** the UI shows Microsoft's measured cost
  (krb5 ≈ −53% IOPS, krb5p ≈ −77%) when an admin enables Kerberos.

## Found by the permission matrix

- **Ganesha's POSIX→NFSv4 ACL mapping** (`FSAL/posix_acls.c`) adds
  `DENY GROUP@` for rights `other` has and `group::` lacks, ahead of named-group
  `ALLOW`s. Every AD user is in the owning group (*Domain Users*), so a file
  `chmod`ed to `0644` inside a share denied named-group members over NFS while
  the kernel and SMB allowed them. **Resolved** (access-control.md §1.1): the
  edge normalises every inode (`omnifile_acl::normalize`: `other::---`, mask =
  the group class), and the matrix checks a 0644 file before and after.
- `FSAL_OMNI` reported `sec=krb5` for krb5p sessions and no principal; fixed in
  `fsal/` and verified by the matrix (sec 4, `geo-reader@OMNI.TEST`).
- smbclient 4.23 in the dev VM would not use an MIT ccache
  (`--use-krb5-ccache`); the matrix uses NTLMv2 auth files for smbclient and
  Kerberos for CIFS kernel mounts.
