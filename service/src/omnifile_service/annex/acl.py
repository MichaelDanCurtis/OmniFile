"""ACLs captured from a NAS (annex.md section 5, decision 28).

The edge sends what the NAS holds, and the service derives who may read it
(never the edge's word, as for share ACLs in `api/acls.py`):

- ``ntfs_sd``: the raw self-relative NT security descriptor, read over SMB
  (the cifs client's `system.cifs_ntsd` xattr). The service parses its DACL.
- ``nfs4``: the NFSv4 ACL as XDR (`system.nfs4_acl`), plus the edge's mapping
  of its principals to SIDs (`aces`, one per ACE, in order): NFSv4 names
  (`user@domain`) need the edge's winbind to map, the service can't.
- ``posix``: mode bits with the owner's and group's SIDs (and POSIX ACL
  entries if the server gives them), evaluated as `api/acls.py` does.

NT and NFSv4 ACLs are stored deny-first (`catalog.acl.deny_first`, migration
0038): any deny that matches refuses, whatever a group allows. Inherit-only
entries don't apply to the object; an ACE the parser can't read is a refusal
of the whole descriptor (`AclError`), never skipped, so a deny it couldn't
read can't widen the read set. The raw descriptor is kept whole for a later
restore (phase A3).
"""

from __future__ import annotations

import base64
import hashlib
import json
import struct
from typing import Any

from ..api.acls import access_sets, canonical_json
from ..api.validate import sid as valid_sid

EVERYONE = "S-1-1-0"
#: Groups every signed-in AD user is in: read as Everyone.
EVERYONE_LIKE = {"S-1-5-11", "S-1-5-32-545"}  # Authenticated Users, BUILTIN\Users
OWNER_RIGHTS = "S-1-3-4"
#: Bits that let a principal read a file's data.
FILE_READ_DATA = 0x0000_0001
GENERIC_READ = 0x8000_0000
GENERIC_ALL = 0x1000_0000
READ_BITS = FILE_READ_DATA | GENERIC_READ | GENERIC_ALL
SE_DACL_PRESENT = 0x0004
INHERIT_ONLY_ACE = 0x08
#: ACE types (MS-DTYP 2.4.4.1).
ALLOW_TYPES = {0x00, 0x05}
#: Callback allows (conditional ACEs) grant only when their condition holds,
#: which the service can't evaluate: they grant nothing. Callback denies
#: stay denies.
CALLBACK_ALLOW_TYPES = {0x09, 0x0B}
DENY_TYPES = {0x01, 0x06, 0x0A, 0x0C}
OBJECT_TYPES = {0x05, 0x06, 0x0B, 0x0C}
#: Audit, alarm, mandatory label, resource attribute, scoped policy: no
#: effect on who may read.
NEUTRAL_TYPES = {0x02, 0x03, 0x07, 0x08, 0x0D, 0x0E, 0x0F, 0x10, 0x11, 0x12, 0x13}
MAX_RAW = 64 * 1024
MAX_ACES = 1000
RAW_KINDS = ("ntfs_sd", "nfs4", "posix")
#: The only ACL kind each filer protocol may send: over SMB the client's
#: owner, group and mode are the mount's, not the file's, and over NFSv4 the
#: mode is only part of the ACL, so a `posix` ACL from either would grant
#: the wrong people (fail closed: an acl_error).
KIND_FOR_PROTOCOL = {"smb": "ntfs_sd", "nfs4": "nfs4", "nfs3": "posix"}


class AclError(ValueError):
    """The descriptor can't be read; the file is reported, not copied with a
    guessed ACL."""


def sid_from_bytes(b: bytes, at: int) -> tuple[str, int]:
    """(SID string, bytes used) of the binary SID at `at`."""
    if len(b) < at + 8:
        raise AclError("SID runs past the descriptor")
    rev, count = b[at], b[at + 1]
    if rev != 1 or count > 15:
        raise AclError(f"bad SID revision {rev} or {count} sub-authorities")
    end = at + 8 + 4 * count
    if len(b) < end:
        raise AclError("SID runs past the descriptor")
    auth = int.from_bytes(b[at + 2 : at + 8], "big")
    subs = struct.unpack_from(f"<{count}I", b, at + 8)
    return "S-1-" + str(auth) + "".join(f"-{s}" for s in subs), end - at


def parse_sd(raw: bytes) -> dict[str, Any]:
    """Owner, group and the DACL's ACEs of a self-relative descriptor.

    Returns ``{"owner", "group", "dacl": None | [{"type", "sid", "mask",
    "flags"}]}``; ``dacl`` None means no DACL (everyone has full access)."""
    if len(raw) < 20 or len(raw) > MAX_RAW:
        raise AclError("descriptor too short or too long")
    rev, _sbz, control, o_owner, o_group, _o_sacl, o_dacl = struct.unpack_from("<BBHIIII", raw, 0)
    if rev != 1:
        raise AclError(f"descriptor revision {rev}")
    owner = sid_from_bytes(raw, o_owner)[0] if o_owner else None
    group = sid_from_bytes(raw, o_group)[0] if o_group else None
    if not control & SE_DACL_PRESENT or o_dacl == 0:
        return {"owner": owner, "group": group, "dacl": None}
    if len(raw) < o_dacl + 8:
        raise AclError("DACL runs past the descriptor")
    _arev, _s1, acl_size, count, _s2 = struct.unpack_from("<BBHHH", raw, o_dacl)
    if count > MAX_ACES or o_dacl + acl_size > len(raw):
        raise AclError("DACL size or ACE count out of range")
    aces = []
    at = o_dacl + 8
    for _ in range(count):
        if at + 8 > o_dacl + acl_size:
            raise AclError("ACE runs past the DACL")
        t, flags, size = struct.unpack_from("<BBH", raw, at)
        if size < 8 or at + size > o_dacl + acl_size:
            raise AclError("bad ACE size")
        if t in NEUTRAL_TYPES or t in CALLBACK_ALLOW_TYPES:
            at += size
            continue
        if t not in ALLOW_TYPES | DENY_TYPES:
            raise AclError(f"unknown ACE type {t:#x}")
        (mask,) = struct.unpack_from("<I", raw, at + 4)
        sid_at = at + 8
        if t in OBJECT_TYPES:
            (oflags,) = struct.unpack_from("<I", raw, sid_at)
            sid_at += 4 + (16 if oflags & 1 else 0) + (16 if oflags & 2 else 0)
        who, used = sid_from_bytes(raw, sid_at)
        if sid_at + used > at + size:
            raise AclError("ACE SID runs past the ACE")
        aces.append(
            {
                "type": "deny" if t in DENY_TYPES else "allow",
                "sid": who,
                "mask": mask,
                "flags": flags,
            }  # fmt: skip
        )
        at += size
    return {"owner": owner, "group": group, "dacl": aces}


def deny_first_sets(owner: str | None, aces: list[dict[str, Any]] | None) -> tuple[list, list]:
    """(read_sids, deny_sids) of NT-style ACEs, evaluated deny-first."""
    if aces is None:
        return [EVERYONE], []
    read: set[str] = set()
    deny: set[str] = set()
    for a in aces:
        if a.get("flags", 0) & INHERIT_ONLY_ACE:
            continue
        if not a["mask"] & READ_BITS:
            continue
        who = a["sid"]
        if who == OWNER_RIGHTS:
            if owner is None:
                continue
            who = owner
        if who in EVERYONE_LIKE:
            who = EVERYONE
        (deny if a["type"] == "deny" else read).add(who)
    return sorted(read - deny), sorted(deny)


#: Bits that let a principal add a file to a folder: SMB FILE_ADD_FILE (the
#: same bit as NFSv4 ACE4_ADD_FILE), GENERIC_WRITE and GENERIC_ALL.
FILE_ADD_FILE = 0x0000_0002
GENERIC_WRITE = 0x4000_0000
WRITE_BITS = FILE_ADD_FILE | GENERIC_WRITE | GENERIC_ALL


def _deny_first_bits(
    owner: str | None, aces: list[dict[str, Any]] | None, bits: int
) -> tuple[list, list]:
    """(allow_sids, deny_sids) of a folder's NT-style ACEs for `bits`, deny-first."""
    if aces is None:
        return [EVERYONE], []
    allow: set[str] = set()
    deny: set[str] = set()
    for a in aces:
        if a.get("flags", 0) & INHERIT_ONLY_ACE or not a["mask"] & bits:
            continue
        who = a["sid"]
        if who == OWNER_RIGHTS:
            if owner is None:
                continue
            who = owner
        if who in EVERYONE_LIKE:
            who = EVERYONE
        (deny if a["type"] == "deny" else allow).add(who)
    return sorted(allow - deny), sorted(deny)


def _deny_first_write(owner: str | None, aces: list[dict[str, Any]] | None) -> tuple[list, list]:
    """(write_sids, write_deny_sids) of a folder's NT-style ACEs, deny-first."""
    return _deny_first_bits(owner, aces, WRITE_BITS)


#: Bits that let a principal pass through a folder (decision 63): SMB
#: FILE_TRAVERSE (the same bit as NFSv4 ACE4_EXECUTE), GENERIC_EXECUTE and
#: GENERIC_ALL. On SMB this is strict: Windows' "bypass traverse checking" isn't
#: counted on (catalog/read_set.py).
FILE_TRAVERSE = 0x0000_0020
GENERIC_EXECUTE = 0x2000_0000
SEARCH_BITS = FILE_TRAVERSE | GENERIC_EXECUTE | GENERIC_ALL


def _folder_sets(
    value: dict[str, Any], body: dict[str, Any], bits: int, need: int, unmapped: int
) -> tuple[list[str] | None, list[str]]:
    kind = body["raw_kind"]
    if kind == "ntfs_sd":
        sd = parse_sd(body["raw"])
        return _deny_first_bits(body["owner_sid"], sd["dacl"], bits)
    if kind == "nfs4":
        for a in value.get("aces") or []:
            if (
                isinstance(a, dict)
                and a.get("sid") is None
                and a.get("type") == "deny"
                and isinstance(a.get("mask"), int)
                and a["mask"] & unmapped
            ):
                return None, []
        aces = [{"type": e["ace"], "sid": e["sid"], "mask": e["mask"], "flags": e["flags"]}
                for e in body["entries"]]  # fmt: skip
        return _deny_first_bits(body["owner_sid"], aces, bits)
    if posix_unmapped_restricts(value, need):
        return None, []
    return access_sets(body["owner_sid"], body["group_sid"], body["entries"], need=need)


def posix_unmapped_restricts(value: dict[str, Any], need: int) -> bool:
    """Whether a POSIX ACL keeps out, for the permission bits `need`, members of a
    group the edge couldn't map whom `other` lets in (Codex and the review of #83).
    POSIX consults `other` only for someone no group entry matches: a matching group
    entry that doesn't grant `need`, after the mask, denies it to its members. With
    no SID for that group, who they are isn't known, so the set for `need` can't be
    told. Two ways: a named group with no SID (`acl_body` drops one that grants
    read, which is safe for read sets, not for search or write), and an owning
    group with no SID (`access_sets` skips it). The original entries are read
    here, not `acl_body`'s."""
    entries = value.get("entries")
    if not entries:
        # The mode alone: its group bits are the owning group's, its low bits other's.
        mode = value.get("mode")
        if value.get("group_sid") or not isinstance(mode, int):
            return False
        return mode & need == need and mode >> 3 & need != need
    access = [e for e in entries if isinstance(e, dict) and e.get("scope", "access") == "access"]
    if not any(e.get("tag") == "other" and _bits(e.get("perms")) & need == need for e in access):
        return False
    mask = next((_bits(e.get("perms")) for e in access if e.get("tag") == "mask"), 7)
    for e in access:
        unmapped = (e.get("tag") == "group" and e.get("sid") is None) or (
            e.get("tag") == "owning_group" and not value.get("group_sid")
        )
        if unmapped and _bits(e.get("perms")) & mask & need != need:
            return True
    return False


def _bits(p: Any) -> int:
    p = str(p)
    return sum(b for c, b in zip(p, (4, 2, 1), strict=False) if c in "rwx")


def folder_write_sets(
    value: dict[str, Any], body: dict[str, Any]
) -> tuple[list[str] | None, list[str]]:
    """Who may add a file to a folder (annex.md section 5, `write_sids`):
    `body` is `acl_body(value)`. None when it can't be told: a deny for a
    principal the edge couldn't map could be the requester's, so nobody may
    place a file there (fail closed, toward "can't place it here")."""
    return _folder_sets(value, body, WRITE_BITS, need=3, unmapped=WRITE_BITS)


def folder_search_sets(
    value: dict[str, Any], body: dict[str, Any]
) -> tuple[list[str] | None, list[str]]:
    """Who may pass through a folder to what is below it (decision 63,
    `search_sids`): POSIX search (x); NT and NFSv4 traverse. None when it can't
    be told (a deny for a principal the edge couldn't map): nobody passes."""
    return _folder_sets(value, body, SEARCH_BITS, need=1, unmapped=SEARCH_BITS)


def _aces(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > MAX_ACES:
        raise AclError("aces must be a list of at most 1000")
    out = []
    for i, a in enumerate(value):
        if not isinstance(a, dict) or a.get("type") not in ("allow", "deny"):
            raise AclError(f"aces[{i}].type must be allow or deny")
        mask, flags = a.get("mask"), a.get("flags", 0)
        if not isinstance(mask, int) or not 0 <= mask < 2**32:
            raise AclError(f"aces[{i}].mask must be a 32-bit integer")
        if not isinstance(flags, int) or not 0 <= flags < 2**32:
            raise AclError(f"aces[{i}].flags must be a 32-bit integer")
        if a.get("sid") is None:
            # A principal the edge couldn't map: harmless if it allows,
            # fatal if it denies (it could be the reader).
            if a["type"] == "deny" and mask & READ_BITS:
                raise AclError(f"aces[{i}] denies read to a principal with no SID")
            continue
        out.append({"type": a["type"], "sid": valid_sid(a["sid"], f"aces[{i}].sid"),
                    "mask": mask, "flags": flags})  # fmt: skip
    return out


#: Bits that let a principal give itself read: WRITE_DAC and WRITE_OWNER, and
#: GENERIC_ALL, which holds them (the same values in NT and NFSv4 ACEs).
CONTROL_BITS = 0x0004_0000 | 0x0008_0000 | GENERIC_ALL


def control_sids(owner: str | None, aces: list[dict[str, Any]] | None) -> set[str]:
    """Who an ACL lets change the ACL or take the file (the owner aside)."""
    out: set[str] = set()
    for a in aces or []:
        if a["type"] != "allow" or a.get("flags", 0) & INHERIT_ONLY_ACE:
            continue
        if not a["mask"] & CONTROL_BITS:
            continue
        who = a["sid"]
        if who == OWNER_RIGHTS:
            if owner is None:
                continue
            who = owner
        out.add(EVERYONE if who in EVERYONE_LIKE else who)
    return out


def unmapped_reader(value: Any) -> bool:
    """An allow of read for a principal the edge couldn't map. Harmless for a
    catalog read set, which can only miss a reader, but the wrong way round
    for "the new ACL lets nobody new read it": whoever that is isn't known to
    be a reader of the file. The same goes for a POSIX owning group with no SID
    whose bits let it read. Control bits are `unmapped_control`'s: `acl_body`
    drops an unmapped allow, so `control_sids` never sees one."""
    if not isinstance(value, dict):
        return False
    for a in value.get("aces") or []:
        if (
            isinstance(a, dict)
            and a.get("sid") is None
            and a.get("type") == "allow"
            and isinstance(a.get("mask"), int)
            and a["mask"] & READ_BITS
        ):
            return True
    if value.get("kind") == "posix" and not value.get("group_sid"):
        entries = value.get("entries") or []
        if entries:
            if any(
                isinstance(e, dict)
                and e.get("tag") == "owning_group"
                and "r" in str(e.get("perms"))
                for e in entries
            ):
                return True
        elif isinstance(value.get("mode"), int) and value["mode"] & 0o040:
            return True
    for e in value.get("entries") or []:
        if (
            isinstance(e, dict)
            and e.get("tag") in ("user", "group")
            and e.get("sid") is None
            and "r" in str(e.get("perms"))
        ):
            return True
    return False


def unmapped_control(value: Any) -> bool:
    """An allow of WRITE_DAC or WRITE_OWNER for a principal the edge couldn't
    map. It can grant itself read, so a file anyone may read is no excuse."""
    if not isinstance(value, dict):
        return False
    return any(
        isinstance(a, dict)
        and a.get("sid") is None
        and a.get("type") == "allow"
        and isinstance(a.get("mask"), int)
        and a["mask"] & CONTROL_BITS
        and not (isinstance(a.get("flags"), int) and a["flags"] & INHERIT_ONLY_ACE)
        for a in value.get("aces") or []
    )


def literally_anyone_reads(raw_kind: str | None, entries: Any, read_sids: Any = None) -> bool:
    """Whether an ACL gives read to anyone at all, by its raw entries: `S-1-1-0`
    (or a NULL DACL), or POSIX's other class. Not Authenticated Users or
    BUILTIN\\Users, which the read sets normalise to Everyone: a NAS-local or
    anonymous principal isn't one of those."""
    rows = entries if isinstance(entries, list) else []
    if raw_kind == "posix":
        return any(
            isinstance(e, dict)
            and e.get("tag") == "other"
            and e.get("scope", "access") == "access"
            and "r" in str(e.get("perms"))
            for e in rows
        )
    if not rows:
        # A NULL DACL has no entries, and its read set is Everyone.
        return EVERYONE in (read_sids or [])
    return any(
        isinstance(e, dict)
        and e.get("ace") == "allow"
        and e.get("sid") == EVERYONE
        and isinstance(e.get("mask"), int)
        and e["mask"] & READ_BITS
        and not e.get("flags", 0) & INHERIT_ONLY_ACE
        for e in rows
    )


def acl_body(value: Any, protocol: str | None = None) -> dict[str, Any]:
    """The `catalog.acl` row for a captured ACL (`acl` of an annex event).
    `protocol` is the filer's: an ACL of another kind than it sends is
    refused."""
    if not isinstance(value, dict):
        raise AclError("acl must be an object")
    kind = value.get("kind")
    if kind not in RAW_KINDS:
        raise AclError("acl.kind must be ntfs_sd, nfs4 or posix")
    want = KIND_FOR_PROTOCOL.get(protocol or "")
    if protocol is not None and kind != want:
        raise AclError(f"a {protocol} filer's ACL must be {want or 'known'}, not {kind}")
    mode = value.get("mode", 0)
    if not isinstance(mode, int) or not 0 <= mode <= 0o7777:
        raise AclError("acl.mode must be 0 to 4095")
    raw = None
    if value.get("raw") is not None:
        try:
            raw = base64.b64decode(value["raw"], validate=True)
        except (ValueError, TypeError):
            raise AclError("acl.raw must be base64") from None
        if len(raw) > MAX_RAW:
            raise AclError("acl.raw is too long")
    owner = valid_sid(value["owner_sid"], "acl.owner_sid") if value.get("owner_sid") else None
    group = valid_sid(value["group_sid"], "acl.group_sid") if value.get("group_sid") else None
    entries: list[dict[str, Any]] = []
    read_unknown = False
    if kind == "ntfs_sd":
        if raw is None:
            raise AclError("an ntfs_sd ACL needs raw")
        sd = parse_sd(raw)
        owner, group = sd["owner"] or owner, sd["group"] or group
        aces = sd["dacl"]
        read, deny = deny_first_sets(owner, aces)
        entries = [{"ace": a["type"], "sid": a["sid"], "mask": a["mask"], "flags": a["flags"]}
                   for a in (aces or [])]  # fmt: skip
        deny_first = True
    elif kind == "nfs4":
        aces = _aces(value.get("aces"))
        read, deny = deny_first_sets(owner, aces)
        entries = [{"ace": a["type"], "sid": a["sid"], "mask": a["mask"], "flags": a["flags"]}
                   for a in aces]  # fmt: skip
        deny_first = True
    else:
        entries = _posix_entries(value.get("entries"), mode)
        read, deny = access_sets(owner, group, entries)
        if posix_unmapped_restricts(value, 4):
            # Members of an owning group with no SID are kept out where `other`
            # lets everyone else in: who may read can't be told, so nobody may
            # (the review of #83).
            read, deny, read_unknown = [], [], True
        deny_first = False
    canon = (
        canonical_json(owner, group, mode, entries)
        if kind == "posix"
        else json.dumps(
            {
                "deny_first": True,
                "entries": entries,
                "group_sid": group,
                "mode": mode,
                "owner_sid": owner,
            },
            sort_keys=True,
            separators=(",", ":"),  # fmt: skip
        )
    )
    sha = hashlib.sha256(canon.encode() + b"\0" + kind.encode() + b"\0" + (raw or b"")).digest()
    if read_unknown:
        # `entries` may have dropped the group whose entry makes the set unknown, so
        # a twin without it hashes the same, and its row's wider set would stand in
        # (the re-review of #83). A row of its own, as an unknown write set has.
        sha = hashlib.sha256(sha + b"\0read unknown").digest()
    return {
        "sha256": sha,
        "owner_sid": owner,
        "group_sid": group,
        "mode": mode,
        "entries": entries,
        "read": read,
        "deny": deny,
        "deny_first": deny_first,
        "raw_kind": kind,
        "raw": raw,
    }


def _posix_entries(value: Any, mode: int) -> list[dict[str, Any]]:
    """POSIX ACL entries as `omnifile-acl` writes them; without any, the mode
    bits as owner, owning group and other."""
    if value is None or value == []:

        def perms(bits: int) -> str:
            return (
                ("r" if bits & 4 else "-") + ("w" if bits & 2 else "-") + ("x" if bits & 1 else "-")
            )

        return [
            {"scope": "access", "tag": "owner", "perms": perms(mode >> 6 & 7)},
            {"scope": "access", "tag": "owning_group", "perms": perms(mode >> 3 & 7)},
            {"scope": "access", "tag": "other", "perms": perms(mode & 7)},
        ]
    if not isinstance(value, list) or len(value) > MAX_ACES:
        raise AclError("acl.entries must be a list of at most 1000")
    out = []
    for i, e in enumerate(value):
        if not isinstance(e, dict):
            raise AclError(f"acl.entries[{i}] must be an object")
        if e.get("scope") not in ("access", "default") or e.get("tag") not in (
            "owner",
            "owning_group",
            "user",
            "group",
            "mask",
            "other",
        ):
            raise AclError(f"acl.entries[{i}] has a bad scope or tag")
        p = e.get("perms")
        if (
            not isinstance(p, str)
            or len(p) != 3
            or any(c not in s for c, s in zip(p, ("r-", "w-", "x-"), strict=True))
        ):  # noqa: E501
            raise AclError(f"acl.entries[{i}].perms must look like r-x")
        item = {"scope": e["scope"], "tag": e["tag"], "perms": p}
        if e["tag"] in ("user", "group"):
            if e.get("sid") is None:
                if e["tag"] == "user" or "r" not in p:
                    # An unmapped named entry could be the reader's: refuse.
                    raise AclError(f"acl.entries[{i}] names a principal with no SID")
                continue  # an unmapped group that grants: it can only add readers
            item["sid"] = valid_sid(e["sid"], f"acl.entries[{i}].sid")
        out.append(item)
    out.sort(key=lambda e: json.dumps(e, sort_keys=True, separators=(",", ":")))
    return out
