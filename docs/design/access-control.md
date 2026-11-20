# Access control: Active Directory RBAC on the edge filer

**Status: PROPOSED (2026-10-05), from an owner requirement:** the edge filer must respect Active Directory RBAC, and access must be manageable through AD groups. Decision 28 records the requirement; this note is the design. Items marked **TEST** are proven in M0/M2, not assumed.

## The requirement in one line

Whatever AD says a user may do with a file, OmniFile enforces it on **every path to that file**:
- NFS and SMB;
- while the file is resident, stubbed or recalling;
- in the portal and in Teams;
- and for files that still live in S3 during a migration.

Access is granted by putting people in AD groups, never by managing local accounts on the appliance.

## 1. One enforced permission model for both protocols

- **The filesystem is the source of truth.** File and folder permissions live on the edge's xfs volume as **POSIX ACLs** (access and default ACLs). The ACL entries name **AD groups and users**, mapped to uids and gids by winbind with `idmap_ad`, so every protocol sees the same numbers.
- **SMB:** Windows users see and edit these permissions in Explorer's Security tab. Samba maps NT ACLs to and from POSIX ACLs. We do **not** use `vfs_acl_xattr`: it would store a Windows ACL that NFS never enforces, leaving two truths.
  - The cost is fidelity. Deny entries and some inheritance flags can't be expressed, and the portal and docs must say so. **TEST:** the exact mapping on Samba 4.x for common Explorer edits.
- **NFS:** Ganesha checks permissions before a READ reaches `FSAL_OMNI`, and presents POSIX ACLs to NFSv4 clients as NFSv4 ACLs.
  - **TEST:** FSAL_VFS on xfs, POSIX ACL → NFSv4 ACL display, and whether NFSv4 SETACL should be allowed or refused. Mapping NFSv4 ACLs onto POSIX ACLs doesn't round-trip, so the likely answer is "manage ACLs from Windows or the portal; NFS clients get read-only ACLs".
- **Ganesha must be built with `ENABLE_VFS_POSIX_ACL`.** FSAL_VFS never switches to the caller's credentials, so Ganesha's own access check is the only one on NFS, and without the option it checks mode bits only (found and fixed in M2; `fsal/build/Containerfile` now fails the build without it).

### 1.1 Mode changes from Unix tools (resolved in M2)

A `chmod` on a file that carries the share's ACL, or a `cp -p`, `tar -p` or `rsync -p` of a 0644 file, does two things:
- **It opens `other`** (`other::r--`). The kernel never uses `other` for an AD user here, because every AD user matches the owning group (*Domain Users*). Ganesha's POSIX→NFSv4 mapping, however, turns "other has a right that `group::` lacks" into `DENY GROUP@` ahead of the named-group `ALLOW`s. The permission matrix showed the result: the share's Readers and Writers were denied over NFS while SMB and local access let them in.
- **It narrows the mask** (`mask::r--`), which silently takes write away from Writers and Owners while Explorer and the portal still list the grant.

**Rule: inside a share, AD groups decide; mode bits from Unix clients can't open or narrow group access.** The edge normalises every inode (`omnifile_acl::normalize`):
- `other` is `---` in access and default ACLs;
- the mask is the union of the group class, without `x` on a regular file whose owner has no `x`;
- named entries, the owner entry and the owning-group entry are never changed.

The daemon normalises a file when it sees it created or changed, and the nightly ACL scan repairs anything missed, so a divergence lasts at most until the next event or scan. Users can still remove rights by editing ACLs in Explorer or the portal. Only `chmod` on group and other bits is undone, and the docs say so. **TEST** (matrix): a 0644 file denies a Reader over NFS before normalisation and allows them after it.

- **Stubs and recalls never touch permissions.** Stubbing punches holes and changes only the `trusted.omnifile` marker; owner, mode and ACL xattrs stay as they are. Recall fill writes data through the daemon's private mount. **Tested (M2):** owner, mode, ACLs and every other xattr were identical before stubbing, after stubbing and after the fill (`edge/crates/omnifile-acl/tests/stub_preserves_acl.rs`). A placeholder for a file that was never local is created in the staging directory and doesn't inherit the share's default ACL, so the daemon must put the catalog's ACL on it (`omnifile_acl::to_local`).

## 2. Group membership comes from AD, not the client

- **NFS `sec=sys`:** set Ganesha's **`Manage_Gids = true`** on every export. The group list a client sends is then *replaced by a server lookup* (Ganesha's own export docs), here through winbind against AD. A client can no longer claim extra groups, and the 16-group AUTH_SYS limit stops mattering.
- **NFS `krb5*` and SMB:** the user is proven by Kerberos, and group membership comes from winbind/AD.
- **Nested groups** resolve through winbind. **Tested (M2, dev AD):** nested groups work on NFS (`sec=sys` with `Manage_Gids`, and `krb5p`) and on SMB. After a user is removed from a group, a new SMB session is denied at once, because the token is built at logon; an open SMB session keeps its token. NFS denied the user after 291–306 s, within `Manage_Gids_Expiration` (300 s, set in the edge's Ganesha config; Ganesha's default is 1800 s). Cross-domain (trusted-domain) groups are still **TEST**: they need a second domain.

## 3. Honesty about NFS `sec=sys`

With `sec=sys`, the **uid** is still whatever the client host says. Root on any allowed client can become any user. AD RBAC is only as strong as the hosts allowed to mount. So:

- Every share has a **trust level** shown in the portal:
  - **Proven** for `krb5`, `krb5i` and `krb5p`, and for SMB.
  - **Host-trusted** for `sec=sys`, which requires a `CLIENT` list of named hosts or subnets (no `*`) and `Root_Squash`.
- **A policy check:** a share whose folders carry a restricted AD group can't be host-trusted without an explicit admin override, which is recorded in the audit log. A folder restricted after the share was made is flagged on the share and in the list of shares, and alerts, until the override is recorded or the share moves to Kerberos (decision 39); the edge keeps serving.
- Identity records keep the existing `claimed` / `proven` flag (`AGENTS.md`, identity chain).

## 4. Share-level access, managed through AD groups

Each share in the portal has three AD groups: **Owners** (full control), **Writers** and **Readers**, plus optional extra folder rules. The service pushes them to the edge, which applies them as:

- the **share root's POSIX ACL and default ACL**. Inheritance then gives new files and folders the same rights on both protocols, and this is the real enforcement point;
- **SMB:** `valid users` / `read list` / `write list` mirror the groups, and **access-based enumeration** (`access based share enum`, `hide unreadable`) hides what a user can't open;
- **NFS:** the export's `CLIENT` blocks and `SecType` follow the share's trust level. Ganesha exports carry no per-user allow-list, so the root ACL does that job.

Changes made directly in Windows Explorer stay valid. The edge reads ACLs back and reports them to the catalog (section 6), so the portal never shows a stale picture.

## 5. Everything outside the share respects the same rights

- **Recalls from a read:** the OS has already authorised the open before the guard or FSAL sees the read, so only people allowed to read a file can trigger its recall.
- **Recalls from the portal, Teams or `POST /v1/recalls`** are checked by the service against the file's ACL snapshot and the user's AD groups:
  - The groups are resolved by SID. For a portal or Teams user, the route is Entra objectId → `onPremisesSecurityIdentifier` → transitive group SIDs, from Graph or from the edge's winbind.
  - A Teams button works only for the intended recipient, who passed this check when the card was sent.
- **The portal shows only what the user may read.** Folder and file listings are filtered by the ACL snapshot, so names of files a user can't open aren't revealed.
  - Admin roles (`ArchiveAdmin`, `DataSteward`) are Entra app roles held by **AD groups** synced to Entra, or by **named people** where the installer chose that (decision 89; then every AD user in the tenant is a User).
  - A DataSteward manages folders' tiering, not their content access, unless also an Owner.
- **The partner API** (Prizmanyx) acts as an application with app roles, not as a user. Its recalls are dataset-level and audited.
- **Migration namespace (decision 19):** S3 objects carry no ACLs. Each registered source (`POST /v1/sources`) must name its Owners, Writers and Readers groups, which become the default ACL of its folders. Files presented from S3 inherit them, so they are never world-readable.

## 6. ACLs travel with the data

- **Catalog:** an ACL snapshot per folder, plus a per-file override where one differs, stored as SIDs. It is refreshed by the edge on change, and by a nightly scan.
- **Blob:** at upload, the blob gets owner SID, uid, gid, mode and a compact ACL (or its hash when the ACL is too large for metadata). Metadata can't be changed once a blob is in Archive, so it is set at upload. The catalog holds the full ACL.
- **Restore to a new appliance or DR:** permissions are rebuilt from the catalog, never left open.

## 7. Direct cloud access is outside AD RBAC, so lock it down

Blobs are plain and "readable by any Azure tool", but **Azure RBAC on the storage accounts is a separate system.** File ACLs don't follow the data into Blob, so anyone with Blob data access bypasses AD RBAC.
- Data-plane roles on the live and archive accounts go only to the **OmniFile service identity** and a named **break-glass admin group**.
- End users never get direct Blob access, and the setup guide says so.
- Future option: ADLS Gen2 (hierarchical namespace) ACLs keyed to Entra principals. Its compatibility with Smart and Archive tiers is unverified, so it isn't in v1.

## 8. Administering the appliance

- Local shell and sudo access go to an **AD group** through winbind. SSH is restricted to the management network.
- A break-glass local account is sealed and audited: `omnifile-recovery`, console only, its password shown once at setup, every session logged and shown in the portal until it is sealed again (`docs/design/setup-wizard.md`).
- The setup wizard asks for the appliance-admin group, the default share Owners group, and the portal role groups.

## 9. Audit

- Every allow and deny that matters is recorded with the AD identity and trust level, and the portal shows it: share opens (Samba `full_audit`), recalls, ACL changes, admin actions and overrides.
- Read-level audit of every byte isn't kept. Recalls and opens of stubs are always logged.

## Tests that prove it (M2 exit, regression suite)

Run as a **permission matrix** over three users (in the Readers group, in the Writers group, in no group) × NFS `sec=sys` with `Manage_Gids` / NFS `krb5` / SMB × file state (resident, stub, recalling). It also checks:
- open, read, write and recall;
- that an Explorer ACL edit shows up over NFS;
- nested-group access;
- that removing a user from a group revokes access within the documented cache time;
- that the portal hides unreadable files;
- that ACLs are unchanged by stub and recall.
