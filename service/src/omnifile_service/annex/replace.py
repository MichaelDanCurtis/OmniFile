"""A placement that replaces the file at its origin (annex phase A3.4;
docs/milestones/results/a34.md; decisions 82 to 85).

With `overwrite_keep_version`, a file coming back to its own name in its original
folder may replace the file there, but only when that file is exactly the annex's
current, verified copy: whatever it holds is then a version in Azure already, and
nothing is lost. Anywhere else the placement takes a `-restored` name (decision 82).

The edge hides the old file (the remover's W1), waits the annex's grace period,
places the new file (P4) and deletes the hidden one (W2). The service decides at the
ticket and again before each destructive step (`gate`), all of:

- the target is the file's own name in its own folder, on its own filer;
- the annex's file there is present, and its current version is the one copied, with
  the NAS attributes it was copied with (`changed` otherwise);
- that copy is verified, and was read back in Azure within `AZURE_READ_FRESH`, the
  removal gate's 24 hours; a read is queued otherwise, and the replace waits for it
  for `AZURE_READ_WAIT` before the placement takes a `-restored` name instead;
- no legal hold stands on it or a folder above it;
- no removal of it is open, and no other placement is replacing it.

The file there already holding the placed version's bytes (the same size and CRC)
needs no replace: `same`, and the edge reads it back as for `same_content`.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from . import removals

#: How long a replace waits for the in-Azure read of the file it would replace.
AZURE_READ_WAIT = timedelta(minutes=30)
#: The open replace states: a ticket carried one, or the old file is hidden.
OPEN = ("offered", "hidden")
#: Replace states in which the file's own name is the placement's to take
#: (`gone`: the old file went by a user's hand, nothing deleted by the edge).
OWN_NAME = ("offered", "hidden", "replaced", "kept_aside", "same", "gone")
#: A replace that may have the file hidden now (the review of #98, S5), as SQL on
#: `catalog.annex_placement` aliased `p`: hidden, or offered and either still under
#: way or given a `replace_go` yes (the edge may have hidden it and not said so yet).
#: Only an offer that ended with no yes provably hid nothing. Never keyed on the
#: placement's own state alone: a failed placement's old file stays hidden until the
#: edge reports where it put it.
OPEN_SQL = (
    "(p.replace_state = 'hidden' OR (p.replace_state = 'offered' AND"
    " (p.replace_go_at IS NOT NULL OR p.state NOT IN ('placed', 'failed', 'cancelled'))))"
)
#: The longest name a hidden name holds (the NAS's 255 bytes, E5).
NAME_MAX = 255
#: The hidden name's prefix (the scanner skips it, as it skips the remover's).
HIDDEN_PREFIX = ".omnifile-replacing-"
#: Removals that are open (A2.2).
REMOVAL_OPEN = ("ticketed", "hidden", "deleting", "stuck")


def hidden_name(placement_id: int, name: str) -> str:
    return f"{HIDDEN_PREFIX}{placement_id}-{name}"


def at_own_path(p: dict[str, Any], src: dict[str, Any] | None) -> bool:
    """The target is the file's own name in its own folder on its own filer
    (decision 82: the original folder only)."""
    t = p["target"]
    if src is None or t.get("kind") != "nas" or src.get("file_filer_id") is None:
        return False
    try:
        if int(t["filer_id"]) != int(src["file_filer_id"]):
            return False
    except (KeyError, TypeError, ValueError):
        return False
    full = "/".join(x for x in [src["file_annex_path"], src["path"]] if x)
    folder, _, name = full.rpartition("/")
    return t.get("folder") == folder and t.get("name") == name


def wanted(p: dict[str, Any], src: dict[str, Any] | None) -> bool:
    """The rule asks to overwrite, and the target is the file's own place."""
    return p["on_conflict"] == "overwrite_keep_version" and at_own_path(p, src)


async def gate(
    db, p: dict[str, Any], src: dict[str, Any] | None, now: datetime, *, recheck: bool = False
) -> tuple[str, str | None, dict[str, Any] | None]:
    """(`ok` | `not_yet` | `refused` | `none` | `same`, reason, the file there).

    `none`: no replace is asked for, or the name is free (the file removed or gone).
    `recheck`: a question on a replace already offered: the file there must still be
    the version the ticket named."""
    if p["on_conflict"] != "overwrite_keep_version" or p["target"].get("kind") != "nas":
        return "none", None, None
    if not at_own_path(p, src):
        return "refused", "not_origin", None
    assert src is not None
    if len(hidden_name(p["placement_id"], p["target"]["name"]).encode()) > NAME_MAX:
        # The old file couldn't be hidden under its name (E5): no replace.
        return "refused", "name_too_long", None
    row = await (
        await db.execute(
            """
            SELECT af.file_id, af.path, af.nas_file_id, af.size, af.mtime_ns, af.ctime_ns,
                   af.copied_version_id, af.copied_size, af.copied_mtime_ns, af.copied_ctime_ns,
                   af.gone_since, af.nas_state, f.content_version_id, f.deleted_at,
                   l.location_id, l.verified, l.azure_read_at, l.azure_read_requested_at,
                   l.crc64nvme, v.size AS copied_version_size
              FROM catalog.annex_file af
              JOIN catalog.file f ON f.file_id = af.file_id
              LEFT JOIN catalog.file_version v ON v.version_id = af.copied_version_id
              -- At most one row: one primary pending or committed location per version
              -- (the unique index location_one_primary); an unverified one is `not_yet`.
              LEFT JOIN catalog.location l ON l.version_id = af.copied_version_id
                   AND l.role = 'primary' AND l.state = 'committed'
             WHERE af.annex_id = %s AND af.file_id = %s
             FOR UPDATE OF af
            """,
            (src["file_annex_id"], p["file_id"]),
        )
    ).fetchone()
    if row is None:
        return "refused", "not_in_annex", None
    if row["nas_state"] == "removing":
        return "refused", "removal_open", row
    if row["gone_since"] is not None or row["deleted_at"] is not None:
        return ("refused", "changed", row) if recheck else ("none", None, row)
    if row["nas_state"] != "present":
        # Removed: the name is free as far as the catalog knows. A file a user made
        # there since is a new version of this one once scanned, and until then it
        # isn't known to be anything: never replaced.
        return ("refused", "changed", row) if recheck else ("none", None, row)
    open_removal = await (
        await db.execute(
            "SELECT 1 FROM catalog.annex_removal WHERE file_id = %s AND state = ANY(%s) LIMIT 1",
            (p["file_id"], list(REMOVAL_OPEN)),
        )
    ).fetchone()
    if open_removal is not None:
        return "refused", "removal_open", row
    if row["copied_version_id"] is None or row["copied_version_id"] != row["content_version_id"]:
        return "refused", "changed", row
    if recheck and row["copied_version_id"] != p.get("replaced_version_id"):
        return "refused", "changed", row
    seen = (row["nas_file_id"], row["size"], row["mtime_ns"], row["ctime_ns"])
    copied = (row["nas_file_id"], row["copied_size"], row["copied_mtime_ns"],
              row["copied_ctime_ns"])  # fmt: skip
    if seen != copied or row["copied_ctime_ns"] is None:
        return "refused", "changed", row
    # The placed version's bytes are there already: nothing to replace.
    if (
        not recheck
        and row["crc64nvme"] is not None
        and src["crc64nvme"] is not None
        and bytes(row["crc64nvme"]) == bytes(src["crc64nvme"])
        and row["copied_version_size"] == src["size"]
    ):
        return "same", None, row
    other = await (
        await db.execute(
            "SELECT 1 FROM catalog.annex_placement p WHERE p.file_id = %s"
            f" AND p.placement_id <> %s AND {OPEN_SQL} LIMIT 1",
            (p["file_id"], p["placement_id"]),
        )
    ).fetchone()
    if other is not None:
        return "refused", "other_replace", row
    held = await (
        await db.execute(
            "SELECT 1 FROM catalog.hold h WHERE h.kind = 'legal' AND h.released_at IS NULL"
            "   AND (h.until IS NULL OR h.until > %s)"
            "   AND h.file_id IN (SELECT file_id FROM catalog.file_ancestors(%s))"
            " LIMIT 1",
            (now, p["file_id"]),
        )
    ).fetchone()
    if held is not None:
        return "refused", "legal_hold", row
    if row["location_id"] is None or not row["verified"]:
        return "not_yet", "copy_not_verified", row
    fresh = (
        row["azure_read_at"] is not None and row["azure_read_at"] > now - removals.AZURE_READ_FRESH
    )
    if not fresh:
        asked = row["azure_read_requested_at"]
        if asked is None or asked < now - removals.AZURE_READ_RETRY:
            await db.execute(
                "UPDATE catalog.location SET azure_read_requested_at = %s WHERE location_id = %s",
                (now, row["location_id"]),
            )
            await db.execute(
                "SELECT pgmq.send('verify', %s::jsonb)",
                (json.dumps({"location_id": str(row["location_id"]), "purpose": "replace"}),),
            )
        return "not_yet", "azure_read_pending", row
    return "ok", None, row


async def decide(
    db, p: dict[str, Any], src: dict[str, Any] | None, now: datetime, *, recheck: bool = False
) -> tuple[str, str | None, dict[str, Any] | None]:
    """`gate`, with the wait for the in-Azure read bounded: `not_yet` is held for
    `AZURE_READ_WAIT` from the first time it was asked, then refused with its reason.
    Records when the wait began."""
    status, why, row = await gate(db, p, src, now, recheck=recheck)
    if status != "not_yet":
        return status, why, row
    first = p.get("replace_asked_at")
    if first is None:
        await db.execute(
            "UPDATE catalog.annex_placement SET replace_asked_at = %s WHERE placement_id = %s",
            (now, p["placement_id"]),
        )
        p["replace_asked_at"] = now
        return "not_yet", why, row
    if now - first >= AZURE_READ_WAIT:
        return "refused", why, row
    return "not_yet", why, row
