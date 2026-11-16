"""Annexes on the edge (annex.md phase A1; control-api.md section 4.8).

The edge next to a NAS runs one `omnifile-annex@<id>` unit per annex. It
reads the NAS read-only, keeps its own listing (`annex.db`), and reports what
it sees here in batches. The service never reads NAS bytes.

- `GET annexes`: the annexes this edge runs, with their filer, settings and
  running rule set (A1: the fixed Copy template).
- `POST annex-events`: up to 1000 events of one annex, applied in order, each
  in its own savepoint so one bad event never loses the rest. The result per
  event tells the edge what to do next: for `arrived` and `changed`, the
  catalog `file_id` to upload to through the usual `uploads` path (and its
  current `content_version_id`, the upload's `base_version_id`).
- `GET annexes/{annex_id}/records`: the catalog's record of the annex's files
  (path, NAS file id, attributes as copied, verified), paged, for the edge's
  weekly reconciliation (section 2.3).

Event kinds (open; an unknown kind is rejected for that event only):

- `mount`: the filer's health (`catalog.filer.health`).
- `scan`: the annex's last quick, full or reconcile scan time.
- `baseline`: the first scan's count; the annex becomes `active`.
- `arrived`, `changed`: the file in the annex's dataset (folders made as
  needed), its NAS attributes and captured ACL, and its `annex_file` row.
- `copied`: the version the edge uploaded and the NAS attributes it read the
  bytes with. The verify queue's in-Azure CRC read (decision 24) then marks it
  verified (trigger `location_annex_verified`).
- `left`: deleted on the NAS: the catalog file is tombstoned and kept
  (decisions 40, 51); only an ArchiveAdmin purges the copy.
- `renamed`: the same NAS file id at a new path: the catalog file moves.
- `drift`: reconciliation found a missed arrival, departure or change, or a
  copy that no longer matches: a `reconcile_drift` row (source `annex`).
- `skipped`, `copy_failed`: recorded for the annex's Problems list.

Annex datasets are never in the edge's feed (`feed.edge_datasets`): the edge
holds no placeholders for annexed files, which are never stubs (section 6.3).
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from ..annex import dry_runs, removals
from ..annex import replace as RP
from ..annex import rules as annex_rules
from ..annex.acl import AclError, acl_body, folder_search_sets, folder_write_sets, valid_sid
from ..annex.readback import MAX_REFUSALS
from ..sources.acl import Groups
from ..sources.acl import acl_body as source_acl_body
from .changes import valid_name
from .context import Ctx
from .db import id_str, map_db_error
from .idempotency import Result
from .problems import ApiError
from .validate import (
    bad,
    nanos,
    opt_id,
    opt_int,
    opt_str,
    parse_id,
    req,
    req_enum,
    req_id,
    req_int,
    rfc3339,
    timestamp,
)

log = logging.getLogger(__name__)

#: annex_file states of a file the annex itself removed or is removing.
REMOVED = ("removed", "removing")

MAX_EVENTS = 1000
MAX_PATH = 4096
#: The edge reports its placement account hourly and when its unit starts. Older
#: than this, it isn't what the edge currently says: six reports missed, enough
#: for an outage to be noticed and short enough that a changed account isn't
#: registered from an old report.
PLACEMENT_REPORT_FRESH = timedelta(hours=6)

EVENT_KINDS = (
    "mount",
    "placement_account",
    "scan",
    "baseline",
    "arrived",
    "changed",
    "copied",
    "left",
    "renamed",
    "drift",
    "skipped",
    "copy_failed",
    "would_remove",
    # The return path (A3.1): folder ACLs and free space, for where a
    # recalled file may be placed.
    "folder_acl",
    "target_space",
    # Decision 63: a folder pass that read every folder above the annex's files.
    "folders_read",
)
#: A dry run's report, at most (bytes of JSON).
MAX_REPORT = 1 << 20
SCAN_KINDS = ("quick", "full", "reconcile")
DRIFT_KINDS = ("missed_arrival", "missed_departure", "changed_no_event", "copy_mismatch")
#: The drift an applied event answers (resolved_at is set then).
RESOLVES = {
    "arrived": ("missed_arrival", "changed_no_event"),
    "changed": ("missed_arrival", "changed_no_event"),
    "left": ("missed_departure",),
    # A copy_mismatch is resolved when a version verifies (migration 0051), not when
    # it is copied: its read-back may refuse it again.
    "copied": ("changed_no_event",),
}
#: Annexes the edge runs. Retired ones stop their unit.
LIVE_STATES = ("registered", "baseline", "active", "paused")
FILE_DIR_MODE = 0o750


class Rejected(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code, self.detail = code, detail


ANNEX_SQL = """
SELECT a.*, s.path AS source_path, s.backend_id AS nas_backend_id,
       fl.host, fl.protocol, fl.export, fl.vendor, fl.event_source, fl.credential_mode,
       r.version AS rs_version, r.body AS rs_body,
       -- An ask the refusals' backoff put ahead of now isn't shown until it comes.
       CASE WHEN a.reconcile_asked_at <= now() THEN a.reconcile_asked_at END AS reconcile_ask_due,
       (SELECT file_id FROM catalog.file f
         WHERE f.dataset_id = a.dataset_id AND f.parent_id IS NULL) AS root_id
  FROM catalog.annex a
  JOIN catalog.source s ON s.source_id = a.source_id
  JOIN catalog.filer fl ON fl.filer_id = a.filer_id
  LEFT JOIN catalog.annex_ruleset r ON r.annex_id = a.annex_id AND r.state = 'active'
"""


def annex_json(a: dict[str, Any]) -> dict[str, Any]:
    return {
        "annex_id": id_str(a["annex_id"]),
        "name": a["name"],
        "mode": a["mode"],
        "state": a["state"],
        "path": a["path"],
        "dataset_id": id_str(a["dataset_id"]),
        "filer": {
            "filer_id": id_str(a["filer_id"]),
            "vendor": a["vendor"],
            "host": a["host"],
            "protocol": a["protocol"],
            "export": a["export"],
            "event_source": a["event_source"],
            "credential_mode": a["credential_mode"],
        },
        "settings": {
            "quiet_minutes": a["quiet_minutes"],
            "quick_scan_minutes": a["quick_scan_minutes"],
            "full_scan_minutes": a["full_scan_minutes"],
            "reconcile_days": a["reconcile_days"],
        },
        "ruleset": (
            {"version": a["rs_version"], "body": a["rs_body"]}
            if a["rs_version"] is not None
            else None
        ),
        "baseline_at": rfc3339(a["baseline_at"]),
        # A reconciliation asked for now (annex/readback.py), until the edge has run one.
        "reconcile_asked_at": rfc3339(a.get("reconcile_ask_due")),
        "entity_version": id_str(a["entity_version"]),
    }


def removals_json(lv: dict[str, Any], now: datetime) -> dict[str, Any]:
    """What the unit needs to know of removals: whether they are live (it
    mounts the NAS read-write only then), else why not, and the limits."""
    return {
        "live": bool(lv["live"]),
        "why_not": removals.why_not_live(lv, now),
        "dry_run": not lv["live"] and bool(lv["rs_removes"]),
        "grace_minutes": lv["grace_minutes"],
        "pointer_files": lv["pointer_files"],
        "rate_per_hour": lv["removal_rate_per_hour"],
    }


async def list_annexes(ctx: Ctx) -> Result:
    """`GET annexes`: what this edge's annex units run."""
    async with ctx.svc.catalog.read() as db:
        rows = await (
            await db.execute(
                # Only with an active rule set: an app's annex waits for a
                # person's approval (anti-pattern 7).
                ANNEX_SQL + " WHERE a.edge_id = %s AND a.state = ANY(%s) AND r.version IS NOT NULL"
                " ORDER BY a.annex_id",
                (ctx.edge_id, list(LIVE_STATES)),
            )
        ).fetchall()
        # The service's clock: the edge's bound on its own before an age-based
        # removal (a clock run ahead makes every file look older).
        served = rfc3339(datetime.now(UTC))
        out = []
        for a in rows:
            j = annex_json(a)
            j["removals"] = removals_json(await removals.live(db, a["annex_id"], ctx.now), ctx.now)
            j["served_at"] = served
            out.append(j)
    return Result(200, {"annexes": out})


async def load_annex(db, edge_id: int, annex_id: int, *, lock: bool = False) -> dict[str, Any]:
    row = await (
        await db.execute(
            ANNEX_SQL
            + " WHERE a.annex_id = %s AND a.edge_id = %s"
            + (" FOR UPDATE OF a" if lock else ""),
            (annex_id, edge_id),
        )
    ).fetchone()
    if row is None:
        raise ApiError("not_found", f"annex {annex_id} is not on this edge")
    return row


def rel_path(value: Any, field: str = "path") -> tuple[str, ...]:
    """A path under the annex's root: '/'-separated names, no '.', '..' or
    empty parts."""
    if not isinstance(value, str) or not value or len(value) > MAX_PATH:
        raise bad(f"{field} must be a path of 1 to {MAX_PATH} characters")
    parts = tuple(value.split("/"))
    for p in parts:
        try:
            valid_name(p)
        except Exception:  # noqa: BLE001 - valid_name raises the changes module's error
            raise bad(f"{field} has an invalid part {p!r}") from None
    return parts


def _nas_id(ev: dict[str, Any]) -> str:
    v = req(ev, "nas_file_id")
    if not isinstance(v, str) or not v or len(v) > 128:
        raise bad("nas_file_id must be a string of 1 to 128 characters")
    return v


def _observed(ev: dict[str, Any]) -> datetime | None:
    v = ev.get("observed_at")
    return None if v is None else timestamp(v, "observed_at")


class Batch:
    def __init__(self, ctx: Ctx, db: psycopg.AsyncConnection, annex: dict[str, Any]) -> None:
        self.ctx, self.db, self.a = ctx, db, annex
        self.folders: dict[tuple[str, ...], int] = {}
        self._folder_acl: int | None = None

    # --- helpers --------------------------------------------------------------

    def catalog_parts(self, parts: tuple[str, ...]) -> tuple[str, ...]:
        """The path in the dataset: the source's folder, then the NAS path."""
        base = tuple(p for p in self.a["source_path"].split("/") if p)
        return base + parts

    def object_key(self, path: str) -> str:
        return f"{self.a['path']}/{path}" if self.a["path"] else path

    async def owners_folder_acl(self) -> int:
        """Folders the annex makes carry its Owners group's ACL (as a
        source's do); files carry the ACL captured from the NAS."""
        if self._folder_acl is None:
            b = source_acl_body(Groups(self.a["owners_sid"]), folder=True)
            row = await (
                await self.db.execute(
                    "INSERT INTO catalog.acl"
                    " (dataset_id, sha256, owner_sid, group_sid, mode, entries, read_sids,"
                    "  deny_sids)"
                    " VALUES (%s, %s, NULL, %s, %s, %s, %s::catalog.sid[], %s::catalog.sid[])"
                    " ON CONFLICT (dataset_id, sha256) DO UPDATE SET sha256 = EXCLUDED.sha256"
                    " RETURNING acl_id",
                    (
                        self.a["dataset_id"],
                        b["sha256"],
                        b["group_sid"],
                        b["mode"],
                        Jsonb(b["entries"]),
                        b["read"],
                        b["deny"],
                    ),
                )
            ).fetchone()
            self._folder_acl = row["acl_id"]
        return self._folder_acl

    async def folder(self, parts: tuple[str, ...]) -> int:
        """The catalog folder at `parts` (dataset-relative), made if missing.
        A file in the way is a path conflict."""
        cur = self.a["root_id"]
        base = len(self.catalog_parts(()))
        for i in range(len(parts)):
            key = parts[: i + 1]
            hit = self.folders.get(key)
            if hit is not None:
                cur = hit
                continue
            row = await (
                await self.db.execute(
                    "SELECT file_id, kind FROM catalog.file"
                    " WHERE parent_id = %s AND name = %s AND deleted_at IS NULL",
                    (cur, key[-1]),
                )
            ).fetchone()
            if row is None:
                row = await (
                    await self.db.execute(
                        "INSERT INTO catalog.file"
                        " (dataset_id, parent_id, name, kind, acl_id, uid, gid, mode, origin)"
                        " VALUES (%s, %s, %s, 'dir', %s, 0, 0, %s, 'annex')"
                        " RETURNING file_id, kind",
                        (
                            self.a["dataset_id"],
                            cur,
                            key[-1],
                            await self.owners_folder_acl(),
                            FILE_DIR_MODE,
                        ),
                    )
                ).fetchone()
            if row["kind"] != "dir":
                raise Rejected("path_conflict", f"{'/'.join(key)} is a file in the catalog")
            if i >= base:
                # A NAS folder's: its name is narrowed by the folders above it on
                # the NAS, as a file's is (decision 63; the review of #83).
                await self.db.execute(
                    "INSERT INTO catalog.annex_folder (file_id, annex_id, path)"
                    " VALUES (%s, %s, %s) ON CONFLICT (file_id) DO NOTHING",
                    (row["file_id"], self.a["annex_id"], "/".join(key[base:])),
                )
            cur = self.folders[key] = row["file_id"]
        return cur

    async def acl_id(self, ev: dict[str, Any]) -> tuple[int | None, str | None]:
        """(acl_id, error): an ACL the service can't read leaves the file
        unreadable in the portal (fail closed), never guessed."""
        if ev.get("acl") is None:
            return None, "no ACL captured"
        try:
            body = acl_body(ev["acl"], self.a["protocol"])
        except (AclError, ApiError) as e:
            return None, str(e)
        row = await (
            await self.db.execute(
                """
                INSERT INTO catalog.acl
                    (dataset_id, sha256, owner_sid, group_sid, mode, entries, read_sids,
                     deny_sids, deny_first, raw_kind, raw)
                VALUES (%s, %s, %s, %s, %s, %s, %s::catalog.sid[], %s::catalog.sid[], %s, %s, %s)
                ON CONFLICT (dataset_id, sha256) DO UPDATE SET sha256 = EXCLUDED.sha256
                RETURNING acl_id
                """,
                (
                    self.a["dataset_id"],
                    body["sha256"],
                    body["owner_sid"],
                    body["group_sid"],
                    body["mode"],
                    Jsonb(body["entries"]),
                    body["read"],
                    body["deny"],
                    body["deny_first"],
                    body["raw_kind"],
                    body["raw"],
                ),
            )
        ).fetchone()
        return row["acl_id"], None

    async def live(self, path: str) -> dict[str, Any] | None:
        return await (
            await self.db.execute(
                "SELECT af.*, f.content_version_id, f.parent_id, f.name"
                "  FROM catalog.annex_file af JOIN catalog.file f ON f.file_id = af.file_id"
                " WHERE af.annex_id = %s AND af.path = %s AND af.gone_since IS NULL"
                " FOR UPDATE OF af",
                (self.a["annex_id"], path),
            )
        ).fetchone()

    async def event(self, kind: str, ev: dict[str, Any], **cols: Any) -> None:
        details = cols.pop("details", None) or {}
        await self.db.execute(
            "INSERT INTO catalog.annex_event"
            " (annex_id, kind, path, file_id, version_id, size, observed_at, details)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                self.a["annex_id"],
                kind,
                cols.get("path", ev.get("path")),
                cols.get("file_id"),
                cols.get("version_id"),
                cols.get("size", ev.get("size")),
                _observed(ev),
                Jsonb(details),
            ),
        )

    async def resolve_drift(self, kind: str, path: str) -> None:
        kinds = RESOLVES.get(kind)
        if not kinds:
            return
        await self.db.execute(
            "UPDATE catalog.reconcile_drift SET last_seen_at = now(), observed ="
            " coalesce(observed, '{}'::jsonb) || jsonb_build_object('resolved_by', %s::text)"
            " WHERE source = 'annex' AND backend_id = %s AND container = %s"
            "   AND object_key = %s AND kind = ANY(%s) AND resolved_at IS NULL",
            (kind, self.a["nas_backend_id"], self.a["export"], self.object_key(path), list(kinds)),
        )

    async def tombstone_file(self, file_id: int, op: str) -> int:
        # A file can go more than once (back from a placement, then deleted
        # again): each tombstone is one of the head it follows.
        head = await (
            await self.db.execute(
                "SELECT current_version_id FROM catalog.file WHERE file_id = %s", (file_id,)
            )
        ).fetchone()
        await self.db.execute(
            "UPDATE catalog.file SET deleted_at = now() WHERE file_id = %s AND deleted_at IS NULL",
            (file_id,),
        )
        return await self.version(file_id, "tombstone", f"{op}:{head['current_version_id']}")

    async def version(self, file_id: int, kind: str, op: str) -> int:
        row = await (
            await self.db.execute(
                """
                INSERT INTO catalog.file_version
                    (file_id, dataset_id, parent_id, name, kind, state, committed_at, size,
                     mtime_ns, uid, gid, mode, acl_id, edge_id, edge_op_id, actor_via,
                     actor_proof)
                SELECT f.file_id, f.dataset_id, f.parent_id, f.name, %s, 'committed', now(),
                       f.size, f.mtime_ns, f.uid, f.gid, f.mode, f.acl_id, %s, %s,
                       'unknown', 'unknown'
                  FROM catalog.file f WHERE f.file_id = %s
                RETURNING version_id
                """,
                (kind, self.ctx.edge_id, op, file_id),
            )
        ).fetchone()
        return row["version_id"]

    # --- events ---------------------------------------------------------------

    async def mount(self, ev: dict[str, Any]) -> dict[str, Any]:
        details = ev.get("details")
        if not isinstance(details, dict) or len(json.dumps(details)) > 8192:
            raise bad("mount needs details (an object of at most 8 KiB)")
        await self.db.execute(
            "UPDATE catalog.filer SET health = %s, health_checked_at = now() WHERE filer_id = %s",
            (Jsonb(details), self.a["filer_id"]),
        )
        # Decision 64: a server that shows no ACLs is flagged from its first report
        # until one says they show; that clears its acknowledgement too.
        shown = details.get("acls_shown")
        # Fail closed (the re-review of #85): an edge from before decision 64 never
        # sends `acls_shown`, and one that couldn't open the annex root sends null, so
        # either is taken as hidden. Only a mount that failed, from an edge that sends
        # the field, says nothing either way.
        why = "The NAS shows no ACLs through this mount"
        if "acls_shown" not in details:
            shown, why = False, "The edge doesn't say whether the NAS shows its ACLs"
        elif shown is None and details.get("ok") is True:
            shown, why = False, "The edge couldn't open the annex root to see the NAS's ACLs"
        if shown is False:
            at = _observed(ev) or self.ctx.now
            cur = await self.db.execute(
                "UPDATE catalog.filer SET acls_hidden_since = %s"
                " WHERE filer_id = %s AND acls_hidden_since IS NULL",
                (at, self.a["filer_id"]),
            )
            if cur.rowcount:
                await self.event("problem", ev, details={
                    "reason": "acls_hidden",
                    "detail": f"{why}: access there may rest on mode bits alone, so"
                    " group-readable placements and share copies are refused until an"
                    " ArchiveAdmin acknowledges it on the filer."})  # fmt: skip
        elif shown is True:
            await self.db.execute(
                "UPDATE catalog.filer SET acls_hidden_since = NULL, acls_hidden_ack_by = NULL,"
                " acls_hidden_ack_at = NULL WHERE filer_id = %s AND acls_hidden_since IS NOT NULL",
                (self.a["filer_id"],),
            )
        await self.event("mount", ev, details=details)
        # A mount that stopped answering is an admin's to see at once: the unit may
        # not get out of its wait until the NAS answers (`annex.md` section 3.2).
        if details.get("hung") is True:
            await self.event(
                "problem", ev,
                details={"reason": "nas_not_answering", "detail": str(details.get("error"))[:500]},
            )  # fmt: skip
        return {}

    async def placement_account(self, ev: dict[str, Any]) -> dict[str, Any]:
        """Which account this edge places files on this filer with, as winbind
        names it, or why it can't say. Only the filer's own edge can write this:
        the annex is loaded by the mTLS identity (`load_annex`), and the filer
        checked by it again here; the body names neither. It registers nothing
        (`web/routes_annex.py`, `patch_filer`)."""
        d = ev.get("details")
        if not isinstance(d, dict):
            raise bad("placement_account needs details")
        report: dict[str, Any] = {"annex_id": id_str(self.a["annex_id"])}
        error = d.get("error")
        if error is not None:
            if not isinstance(error, str):
                raise bad("details.error must be text")
            report["error"] = error.replace("\x00", "").encode("utf-8", "replace").decode()[:500]
        else:
            try:
                report["sid"] = valid_sid(d.get("sid"), "details.sid")
            except AclError as e:
                raise bad(str(e)) from None
            kind, name = d.get("kind"), d.get("name")
            if not isinstance(kind, str) or not 0 < len(kind) <= 32:
                raise bad("details.kind must be text")
            if not isinstance(name, str) or not 0 < len(name) <= 256 or "\x00" in name:
                raise bad("details.name must be text")
            report.update(kind=kind, name=name.encode("utf-8", "replace").decode())
        cur = await self.db.execute(
            "SELECT placement_account AS r, placement_account_at AS at FROM catalog.filer"
            " WHERE filer_id = %s AND edge_id = %s FOR UPDATE",
            (self.a["filer_id"], self.ctx.edge_id),
        )
        f = await cur.fetchone()
        if f is None:
            raise Rejected("not_this_edges_filer", "this edge doesn't serve the annex's filer")
        # One annex with no account of its own (a Copy annex, say) doesn't speak
        # for another that has one, while that one's report is current.
        if (
            error is not None
            and f["r"]
            and f["r"].get("sid")
            and f["r"].get("annex_id") != report["annex_id"]
            and self.ctx.now - f["at"] <= PLACEMENT_REPORT_FRESH
        ):
            return {}
        await self.db.execute(
            "UPDATE catalog.filer SET placement_account = %s, placement_account_at = %s"
            " WHERE filer_id = %s AND edge_id = %s",
            (Jsonb(report), self.ctx.now, self.a["filer_id"], self.ctx.edge_id),
        )
        return {}

    async def scan(self, ev: dict[str, Any]) -> dict[str, Any]:
        details = ev.get("details") or {}
        if not isinstance(details, dict):
            raise bad("details must be an object")
        which = req_enum(details, "scan", SCAN_KINDS)
        col = {"quick": "last_quick_scan_at", "full": "last_full_scan_at",
               "reconcile": "last_reconcile_at"}[which]  # fmt: skip
        at = _observed(ev)
        await self.db.execute(
            f"UPDATE catalog.annex SET {col} = coalesce(%s, now()) WHERE annex_id = %s",
            (at, self.a["annex_id"]),
        )
        await self.event("scan", ev, details=details)
        return {}

    async def baseline(self, ev: dict[str, Any]) -> dict[str, Any]:
        details = ev.get("details") or {}
        files = req_int(details, "files", minimum=0)
        nbytes = req_int(details, "bytes", minimum=0)
        await self.db.execute(
            "UPDATE catalog.annex SET baseline_at = coalesce(baseline_at, coalesce(%s, now())),"
            " baseline_files = coalesce(baseline_files, %s),"
            " baseline_bytes = coalesce(baseline_bytes, %s),"
            " state = CASE WHEN state IN ('registered', 'baseline') THEN 'active' ELSE state END"
            " WHERE annex_id = %s",
            (_observed(ev), files, nbytes, self.a["annex_id"]),
        )
        await self.event("baseline", ev, details={"files": files, "bytes": nbytes})
        return {}

    async def arrived(self, ev: dict[str, Any], kind: str = "arrived") -> dict[str, Any]:
        path = ev.get("path")
        parts = rel_path(path)
        nas_id = _nas_id(ev)
        size = req_int(ev, "size", minimum=0)
        mtime = nanos(req(ev, "mtime_ns"), "mtime_ns")
        ctime = nanos(ev["ctime_ns"], "ctime_ns") if ev.get("ctime_ns") is not None else None
        btime = nanos(ev["btime_ns"], "btime_ns") if ev.get("btime_ns") is not None else None
        uid = opt_int(ev, "uid", minimum=0)
        gid = opt_int(ev, "gid", minimum=0)
        mode = opt_int(ev, "mode", minimum=0)
        if mode is not None and mode > 0o7777:
            raise bad("mode must be at most 4095")
        baseline = bool(ev.get("baseline", False))
        seen = _observed(ev)
        acl_id, acl_error = await self.acl_id(ev)
        owner = ev["acl"].get("owner_sid") if isinstance(ev.get("acl"), dict) else None
        if await self.placing_at(path):
            raise Rejected(
                "placement_in_progress",
                f"{path} may be a file a placement is making: asked again after it ends",
            )
        cur = await self.live(path)
        if cur is not None and cur["nas_file_id"] != nas_id and cur["nas_state"] in REMOVED:
            # A new file where the annex removed one (annex.md section 3.7):
            # a new version of the same catalog file, the removed one an older
            # version. Never a NAS delete: that copy may be the only one.
            await self.db.execute(
                "UPDATE catalog.annex_file SET nas_file_id = %s, nas_state = 'present',"
                " removed_at = NULL, updated_at = now() WHERE annex_id = %s AND file_id = %s",
                (nas_id, self.a["annex_id"], cur["file_id"]),
            )
            cur = await self.live(path)
        if cur is not None and cur["nas_file_id"] != nas_id:
            # Another file now has the name (deleted and recreated, or
            # replaced by a rename): the old one is gone, its copy kept.
            await self._gone(cur, ev, reason="replaced")
            cur = None
        if cur is None:
            parent = await self.folder(self.catalog_parts(parts[:-1]))
            taken = await (
                await self.db.execute(
                    "SELECT f.file_id, af.annex_id FROM catalog.file f"
                    " LEFT JOIN catalog.annex_file af ON af.file_id = f.file_id"
                    " WHERE f.parent_id = %s AND f.name = %s AND f.deleted_at IS NULL",
                    (parent, parts[-1]),
                )
            ).fetchone()
            if taken is not None:
                raise Rejected("path_conflict", f"{path} is taken in the catalog")
            row = await (
                await self.db.execute(
                    """
                    INSERT INTO catalog.file
                        (dataset_id, parent_id, name, kind, size, mtime_ns, uid, gid, mode,
                         owner_sid, acl_id, state, resident_bytes, origin)
                    VALUES (%s, %s, %s, 'file', %s, %s, %s, %s, %s, %s, %s, 'remote', 0, 'annex')
                    RETURNING file_id, content_version_id
                    """,
                    (
                        self.a["dataset_id"],
                        parent,
                        parts[-1],
                        size,
                        mtime,
                        uid,
                        gid,
                        mode,
                        owner,
                        acl_id,
                    ),
                )
            ).fetchone()
            file_id = row["file_id"]
            await self.db.execute(
                """
                INSERT INTO catalog.annex_file
                    (annex_id, file_id, path, nas_file_id, size, mtime_ns, ctime_ns, btime_ns,
                     change_id, first_seen, last_seen, baseline, rule)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, coalesce(%s, now()),
                        coalesce(%s, now()), %s, 'copy')
                """,
                (
                    self.a["annex_id"],
                    file_id,
                    path,
                    nas_id,
                    size,
                    mtime,
                    ctime,
                    btime,
                    opt_str(ev, "change_id"),
                    seen,
                    seen,
                    baseline,
                ),
            )
            content = None
            kind = "arrived"
        else:
            file_id = cur["file_id"]
            now = (size, mtime, ctime)
            if now == (cur["copied_size"], cur["copied_mtime_ns"], cur["copied_ctime_ns"]):
                kind = "seen"  # the copy is of these bytes: nothing to do
            elif now == (cur["size"], cur["mtime_ns"], cur["ctime_ns"]):
                kind = "pending"  # reported before, not copied yet: no new event
            else:
                kind = "changed" if cur["copied_version_id"] is not None else "pending"
            await self.db.execute(
                "UPDATE catalog.file SET size = %s, mtime_ns = %s,"
                " uid = coalesce(%s, uid), gid = coalesce(%s, gid), mode = coalesce(%s, mode),"
                " owner_sid = coalesce(%s, owner_sid), acl_id = %s"
                " WHERE file_id = %s",
                # The NAS's ACL now: one that can't be read leaves no ACL
                # (nobody sees the file), never the old, perhaps wider, one.
                (size, mtime, uid, gid, mode, owner, acl_id, file_id),
            )
            await self.db.execute(
                "UPDATE catalog.annex_file SET size = %s, mtime_ns = %s, ctime_ns = %s,"
                " btime_ns = coalesce(%s, btime_ns), change_id = %s,"
                " last_seen = coalesce(%s, now()), updated_at = now()"
                " WHERE annex_id = %s AND file_id = %s",
                (
                    size,
                    mtime,
                    ctime,
                    btime,
                    opt_str(ev, "change_id"),
                    seen,
                    self.a["annex_id"],
                    file_id,
                ),
            )
            content = cur["content_version_id"]
        details: dict[str, Any] = {"nas_file_id": nas_id}
        if acl_error is not None:
            details["acl_error"] = acl_error[:500]
        if kind in ("arrived", "changed") and not baseline:
            await self.event(kind, ev, file_id=file_id, size=size, details=details)
        if acl_error is not None and kind in ("arrived", "changed"):
            # Copied, but nobody sees it in the portal until its ACL reads.
            await self.event(
                "problem", ev, file_id=file_id, details={"reason": "acl_unreadable", **details}
            )
        rule = ev.get("rule")
        if rule is not None:
            if not isinstance(rule, str) or not 1 <= len(rule) <= 64:
                raise bad("rule must be the id of the rule that chose the file")
            await self.db.execute(
                "UPDATE catalog.annex_file SET rule = %s WHERE annex_id = %s AND file_id = %s",
                (rule, self.a["annex_id"], file_id),
            )
        await self.resolve_drift("arrived", path)
        return {
            "file_id": id_str(file_id),
            "content_version_id": id_str(content),
            "copy": kind != "seen",
        }

    async def changed(self, ev: dict[str, Any]) -> dict[str, Any]:
        return await self.arrived(ev, "changed")

    async def placing_at(self, path: str) -> bool:
        """A placement between its `go` and its `done` may have put this file
        here: until `done` says which, it isn't a new file or a change."""
        from .annex_placements import allowed_name

        export = f"{self.a['path']}/{path}" if self.a["path"] else path
        folder, _, name = export.rpartition("/")
        rows = await (
            await self.db.execute(
                "SELECT target FROM catalog.annex_placement"
                " WHERE (state = 'placing' OR (state = 'stuck' AND before_stuck = 'placing'))"
                " AND target->>'kind' = 'nas' AND (target->>'filer_id')::bigint = %s"
                " AND target->>'folder' = %s",
                (self.a["filer_id"], folder),
            )
        ).fetchall()
        return any(allowed_name({"target": r["target"]}, name) for r in rows)

    async def replacing(self, file_id: int) -> bool:
        """A placement may have this file hidden to replace it (A3.4)."""
        row = await (
            await self.db.execute(
                # Keyed on the replace, not on the placement's end (#98's S5).
                f"SELECT 1 FROM catalog.annex_placement p WHERE p.file_id = %s AND {RP.OPEN_SQL}"
                " LIMIT 1",
                (file_id,),
            )
        ).fetchone()
        return row is not None

    async def adopt_placed(
        self,
        path: str,
        p: dict[str, Any],
        attrs: dict[str, Any],
        hold_until: Any,
        version: dict[str, Any] | None,
    ) -> str | None:
        """A placement's file under a name of its own in this annex (A3.3
        `done`): a new annexed file, held from leaving, its age counted from
        now. With `version`, it references that version's blob, verified, so
        the next scan finds it copied. Without, it is copied like any new
        file. None if the catalog has something else there."""
        from .data import commit_version, insert_version

        parts = rel_path(path)
        cur = await self.live(path)
        if cur is not None:
            await self.db.execute(
                "UPDATE catalog.annex_file SET hold_until = greatest(hold_until, %s),"
                " updated_at = now() WHERE annex_id = %s AND file_id = %s",
                (hold_until, self.a["annex_id"], cur["file_id"]),
            )
            return id_str(cur["file_id"])
        parent = await self.folder(self.catalog_parts(parts[:-1]))
        taken = await (
            await self.db.execute(
                "SELECT 1 FROM catalog.file WHERE parent_id = %s AND name = %s"
                " AND deleted_at IS NULL",
                (parent, parts[-1]),
            )
        ).fetchone()
        if taken is not None:
            return None
        size, mtime = attrs.get("size"), attrs.get("mtime_ns")
        src = await (
            await self.db.execute(
                "SELECT uid, gid, mode, owner_sid, acl_id FROM catalog.file WHERE file_id = %s",
                (p["file_id"],),
            )
        ).fetchone()
        f = await (
            await self.db.execute(
                "INSERT INTO catalog.file (dataset_id, parent_id, name, kind, size, mtime_ns, uid,"
                " gid, mode, owner_sid, acl_id, state, resident_bytes, origin)"
                " VALUES (%s, %s, %s, 'file', %s, %s, %s, %s, %s, %s, %s, 'remote', 0, 'annex')"
                " RETURNING *",
                (self.a["dataset_id"], parent, parts[-1], size, mtime, src["uid"], src["gid"],
                 src["mode"], src["owner_sid"], src["acl_id"]),
            )
        ).fetchone()  # fmt: skip
        fid = f["file_id"]
        await self.db.execute(
            "INSERT INTO catalog.annex_file (annex_id, file_id, path, nas_file_id, size, mtime_ns,"
            " ctime_ns, first_seen, last_seen, baseline, rule, hold_until)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, now(), now(), false, 'copy', %s)",
            (self.a["annex_id"], fid, path, str(attrs.get("nas_file_id")), size, mtime,
             attrs.get("ctime_ns"), hold_until),
        )  # fmt: skip
        if version is not None and version["location_id"] is not None:
            crc = version["crc64nvme"]
            vid = await insert_version(self.db, self.ctx, f, f"placement-{p['placement_id']}",
                                       size, mtime, version["blake3"], crc)  # fmt: skip
            await self.db.execute(
                "INSERT INTO catalog.location (file_id, version_id, backend_id, container,"
                " object_key, backend_version_id, role, state, logical_tier, storage_class, size,"
                " crc64nvme, etag, verified, verified_at, verified_by, committed_at)"
                " SELECT %s, %s, backend_id, container, object_key, backend_version_id, 'primary',"
                " 'committed', logical_tier, storage_class, %s, crc64nvme, etag, verified,"
                " verified_at, verified_by, now() FROM catalog.location WHERE location_id = %s",
                (fid, vid, size, version["location_id"]),
            )
            await commit_version(self.db, fid, vid, size, mtime, version["blake3"], crc,
                                 remote=True)  # fmt: skip
            await self.db.execute(
                "UPDATE catalog.annex_file SET copied_version_id = %s, copied_size = %s,"
                " copied_mtime_ns = %s, copied_ctime_ns = %s, copied_at = now(),"
                " verified_version_id = (SELECT %s::bigint FROM catalog.location l"
                "                         WHERE l.version_id = %s AND l.verified),"
                " verified_at = (SELECT l.verified_at FROM catalog.location l"
                "                 WHERE l.version_id = %s AND l.verified), updated_at = now()"
                " WHERE annex_id = %s AND file_id = %s",
                (vid, size, mtime, attrs.get("ctime_ns"), vid, vid, vid, self.a["annex_id"], fid),
            )
        return id_str(fid)

    async def copied(self, ev: dict[str, Any]) -> dict[str, Any]:
        path = ev.get("path")
        rel_path(path)
        file_id = req_id(ev, "file_id")
        version_id = req_id(ev, "version_id")
        size = req_int(ev, "size", minimum=0)
        mtime = nanos(req(ev, "mtime_ns"), "mtime_ns")
        ctime = nanos(ev["ctime_ns"], "ctime_ns") if ev.get("ctime_ns") is not None else None
        # The location's row first, in the order the verify worker takes them (its trigger
        # then updates the annex file). A verification either has committed, and reads as
        # verified below, or waits for this, and its trigger then finds the copy. Without
        # the lock, one between its trigger and its commit was lost for good: the location
        # verified, and the annex file never saying so.
        await self.db.execute(
            "SELECT 1 FROM catalog.location WHERE version_id = %s AND file_id = %s"
            " AND role = 'primary' AND state = 'committed' FOR SHARE",
            (version_id, file_id),
        )
        af = await (
            await self.db.execute(
                "SELECT * FROM catalog.annex_file WHERE annex_id = %s AND file_id = %s FOR UPDATE",
                (self.a["annex_id"], file_id),
            )
        ).fetchone()
        if af is None:
            raise Rejected("not_found", f"file {file_id} is not in this annex")
        loc = await (
            await self.db.execute(
                "SELECT v.state, v.size, l.verified, l.verified_at, l.verified_by, l.crc64nvme"
                "  FROM catalog.file_version v"
                "  LEFT JOIN catalog.location l ON l.version_id = v.version_id"
                "       AND l.role = 'primary' AND l.state = 'committed'"
                " WHERE v.version_id = %s AND v.file_id = %s AND v.kind = 'content'",
                (version_id, file_id),
            )
        ).fetchone()
        if loc is None or loc["state"] != "committed":
            raise Rejected(
                "version_conflict", f"version {version_id} of {file_id} is not committed"
            )
        if loc["size"] != size:
            raise bad("size differs from the committed version")
        if af["copied_version_id"] == version_id:
            return {"file_id": id_str(file_id), "version_id": id_str(version_id)}  # a replay
        verified = bool(loc["verified"])
        # Refused before this event came (a small file is read back at once): said here.
        refused = None
        if not verified:
            refused = await (
                await self.db.execute(
                    "SELECT a.details->>'reason' AS why, a.details->>'cause' AS cause"
                    "  FROM catalog.audit_log a"
                    " WHERE a.file_id = %s AND a.version_id = %s AND a.action = 'location.verify'"
                    "   AND a.decision = 'deny' ORDER BY a.at DESC LIMIT 1",
                    (file_id, version_id),
                )
            ).fetchone()
        await self.db.execute(
            "UPDATE catalog.annex_file SET copied_version_id = %s, copied_size = %s,"
            " copied_mtime_ns = %s, copied_ctime_ns = %s, copied_at = now(),"
            " verified_version_id = CASE WHEN %s THEN %s::bigint END,"
            " verified_at = CASE WHEN %s THEN %s::timestamptz END,"
            # A copy that is already verified (a dedupe) ends a run of refusals.
            " copy_refusals = CASE WHEN %s THEN 0 ELSE copy_refusals END,"
            " copy_refused_version_id = CASE WHEN %s THEN NULL ELSE copy_refused_version_id END,"
            # And the stop and acknowledgement an ArchiveAdmin set (migration 0058).
            " copy_stopped_at = CASE WHEN %s THEN NULL ELSE copy_stopped_at END,"
            " copy_stopped_by = CASE WHEN %s THEN NULL ELSE copy_stopped_by END,"
            " copy_refused_ack_at = CASE WHEN %s THEN NULL ELSE copy_refused_ack_at END,"
            " copy_refused_ack_by = CASE WHEN %s THEN NULL ELSE copy_refused_ack_by END,"
            " updated_at = now()"
            " WHERE annex_id = %s AND file_id = %s",
            (
                version_id,
                size,
                mtime,
                ctime,
                verified,
                version_id,
                verified,
                loc["verified_at"],
                verified,
                verified,
                verified,
                verified,
                verified,
                verified,
                self.a["annex_id"],
                file_id,
            ),
        )
        await self.event(
            "copied",
            ev,
            file_id=file_id,
            version_id=version_id,
            size=size,
            details={"crc64nvme": (loc["crc64nvme"] or b"").hex() or None},
        )
        if verified:
            # Verified before this event (a dedupe, or the verify queue was
            # quicker than the edge's report): the trigger saw no copy yet.
            await self.event(
                "verified",
                ev,
                file_id=file_id,
                version_id=version_id,
                size=size,
                details={"verified_by": loc["verified_by"]},
            )
        if refused is not None:
            from ..annex.readback import refused_on

            await refused_on(self.db, self.a["annex_id"], path, file_id, version_id,
                             refused["why"] or "refused",
                             refused["cause"] or "mismatch")  # fmt: skip
        await self.resolve_drift("copied", path)
        return {"file_id": id_str(file_id), "version_id": id_str(version_id),
                "verified": verified}  # fmt: skip

    async def _gone(self, cur: dict[str, Any], ev: dict[str, Any], *, reason: str) -> None:
        if cur.get("nas_state") in REMOVED:
            # The annex removed it (or is removing it): it lives in Azure now,
            # never a NAS delete, never purged after a deleted-file retention.
            return
        await self.tombstone_file(
            cur["file_id"], f"annex:{self.a['annex_id']}:left:{cur['file_id']}"
        )
        await self.db.execute(
            "UPDATE catalog.annex_file SET gone_since = coalesce(%s, now()), nas_state = 'gone',"
            " updated_at = now() WHERE annex_id = %s AND file_id = %s",
            (_observed(ev), self.a["annex_id"], cur["file_id"]),
        )
        await self.event(
            "left",
            ev,
            path=cur["path"],
            file_id=cur["file_id"],
            size=cur["size"],
            details={
                "reason": reason,
                "last_seen": rfc3339(cur["last_seen"]),
                "nas_file_id": cur["nas_file_id"],
            },
        )

    async def left(self, ev: dict[str, Any]) -> dict[str, Any]:
        path = ev.get("path")
        rel_path(path)
        cur = await self.live(path)
        if cur is None:
            return {"file_id": None}  # never copied, or already gone: nothing to record
        if await self.replacing(cur["file_id"]):
            # A3.4: hidden by a placement that replaces it, not a user's delete. The
            # edge doesn't say so either; this is the service's own guard.
            raise Rejected(
                "placement_in_progress",
                f"{path} is being replaced by a placement: asked again after it ends",
            )
        nas_id = ev.get("nas_file_id")
        if nas_id is not None and nas_id != cur["nas_file_id"]:
            raise Rejected("version_conflict", f"{path} is another NAS file now")
        await self._gone(cur, ev, reason=str(ev.get("reason") or "deleted"))
        await self.resolve_drift("left", path)
        return {"file_id": id_str(cur["file_id"])}

    async def renamed(self, ev: dict[str, Any]) -> dict[str, Any]:
        path = ev.get("path")
        parts = rel_path(path)
        old = ev.get("old_path")
        rel_path(old, "old_path")
        nas_id = _nas_id(ev)
        cur = await self.live(old)
        if cur is None or cur["nas_file_id"] != nas_id:
            raise Rejected("not_found", f"{old} is not a live annex file with that NAS id")
        victim = await self.live(path)
        if victim is not None:
            await self._gone(victim, ev, reason="replaced")
        parent = await self.folder(self.catalog_parts(parts[:-1]))
        taken = await (
            await self.db.execute(
                "SELECT file_id FROM catalog.file"
                " WHERE parent_id = %s AND name = %s AND deleted_at IS NULL AND file_id <> %s",
                (parent, parts[-1], cur["file_id"]),
            )
        ).fetchone()
        if taken is not None:
            raise Rejected("path_conflict", f"{path} is taken in the catalog")
        await self.db.execute(
            "UPDATE catalog.file SET parent_id = %s, name = %s WHERE file_id = %s",
            (parent, parts[-1], cur["file_id"]),
        )
        await self.version(
            cur["file_id"],
            "rename",
            f"annex:{self.a['annex_id']}:rename:{cur['file_id']}:{path}"[:128],
        )
        await self.db.execute(
            "UPDATE catalog.annex_file SET path = %s, last_seen = coalesce(%s, now()),"
            " updated_at = now() WHERE annex_id = %s AND file_id = %s",
            (path, _observed(ev), self.a["annex_id"], cur["file_id"]),
        )
        await self.event(
            "renamed",
            ev,
            file_id=cur["file_id"],
            size=cur["size"],
            details={"old_path": old, "nas_file_id": nas_id},
        )
        return {"file_id": id_str(cur["file_id"])}

    async def drift(self, ev: dict[str, Any]) -> dict[str, Any]:
        path = ev.get("path")
        rel_path(path)
        details = ev.get("details") or {}
        if not isinstance(details, dict):
            raise bad("details must be an object")
        kind = req_enum(details, "drift", DRIFT_KINDS)
        expected = details.get("expected")
        observed = details.get("observed")
        for name, v in (("expected", expected), ("observed", observed)):
            if v is not None and (not isinstance(v, dict) or len(json.dumps(v)) > 4096):
                raise bad(f"details.{name} must be an object of at most 4 KiB")
        file_id = opt_id(ev, "file_id")
        await self.db.execute(
            """
            INSERT INTO catalog.reconcile_drift
                (source, backend_id, container, object_key, kind, expected, observed,
                 detected_at, last_seen_at)
            VALUES ('annex', %s, %s, %s, %s, %s, %s, now(), now())
            ON CONFLICT (backend_id, container, object_key, kind,
                         (coalesce(backend_version_id, ''))) WHERE resolved_at IS NULL
            DO UPDATE SET last_seen_at = now(), observed = EXCLUDED.observed
            """,
            (
                self.a["nas_backend_id"],
                self.a["export"],
                self.object_key(path),
                kind,
                Jsonb(expected) if expected is not None else None,
                Jsonb(observed) if observed is not None else None,
            ),
        )
        await self.event(
            "drift",
            ev,
            file_id=file_id,
            details={"drift": kind, "expected": expected, "observed": observed},
        )
        return {}

    async def skipped(self, ev: dict[str, Any], kind: str = "skipped") -> dict[str, Any]:
        path = ev.get("path")
        if path == ".":
            ev = {**ev, "path": None}  # the annex as a whole (its root, its rule set)
        else:
            rel_path(path)
        reason = ev.get("reason")
        if not isinstance(reason, str) or not reason or len(reason) > 64:
            raise bad("reason must be a short string")
        detail = ev.get("detail")
        if detail is not None and (not isinstance(detail, str) or len(detail) > 1000):
            raise bad("detail must be a string of at most 1000 characters")
        await self.event(
            kind, ev, file_id=opt_id(ev, "file_id"), details={"reason": reason, "detail": detail}
        )
        return {}

    async def copy_failed(self, ev: dict[str, Any]) -> dict[str, Any]:
        return await self.skipped(ev, "copy_failed")

    async def would_remove(self, ev: dict[str, Any]) -> dict[str, Any]:
        """A removing rule chose a copied file (A2.1: recorded, never acted
        on; the removal pipeline is A2.2's)."""
        path = ev.get("path")
        rel_path(path)
        file_id = req_id(ev, "file_id")
        version_id = req_id(ev, "version_id")
        rule = ev.get("rule")
        if not isinstance(rule, str) or not 1 <= len(rule) <= 64:
            raise bad("rule must be the id of the rule that chose the file")
        cur = await (
            await self.db.execute(
                "SELECT af.copied_version_id, af.verified_version_id, l.verified_by"
                "  FROM catalog.annex_file af"
                "  LEFT JOIN catalog.location l ON l.version_id = af.verified_version_id"
                "       AND l.role = 'primary' AND l.state = 'committed'"
                " WHERE af.annex_id = %s AND af.file_id = %s",
                (self.a["annex_id"], file_id),
            )
        ).fetchone()
        if cur is None:
            raise Rejected("not_found", f"file {file_id} is not in this annex")
        await self.event(
            "would_remove",
            ev,
            file_id=file_id,
            version_id=version_id,
            details={
                "rule": rule,
                "reason": str(ev.get("reason") or "")[:64] or None,
                # What A2.2's ticket will ask for: the copy of this version,
                # read back in Azure.
                "current": cur["copied_version_id"] == version_id,
                "verified": cur["verified_version_id"] == version_id,
                "verified_by": cur["verified_by"],
            },
        )
        return {}

    # --- the return path (A3.1) -------------------------------------------------

    def export_path(self, ev: dict[str, Any], *, above: bool = False) -> str:
        """A folder relative to the filer's export ('' is its root). It must
        be the annex's own tree or a return location of its running rules on
        this filer (what folders.rs reads), nothing else on the NAS. `above`:
        or a folder above the annex's root, up to the export's root, whose ACL
        decides who may read the annex's files too (decision 63)."""
        v = ev.get("path")
        if v == "":
            path = ""
        else:
            path = "/".join(rel_path(v))
        root = self.a["path"]
        inside = root == "" or path == root or path.startswith(root + "/")
        if above and (path == "" or root.startswith(path + "/")):
            inside = True
        if not inside:
            allowed = []
            for r in annex_rules.returns((self.a["rs_body"] or {}).get("rules", [])):
                for loc in annex_rules.targets(r):
                    if loc["kind"] == "nas_folder" and loc.get("filer_id") == str(
                        self.a["filer_id"]
                    ):
                        allowed.append(loc["path"])
            if not any(path == p or path.startswith(p + "/") or p == "" for p in allowed):
                raise Rejected("out_of_scope", f"{path!r} is neither the annex's nor a return"
                                               " location of its rules")  # fmt: skip
        return path

    async def folder_acl(self, ev: dict[str, Any]) -> dict[str, Any]:
        """A folder's ACL as the edge read it, who may add a file there with
        it, or the folder gone. An ACL the service can't read lets nobody
        place a file there (fail closed)."""
        path = self.export_path(ev, above=True)
        observed = _observed(ev) or self.ctx.now
        if ev.get("gone") is True:
            # Kept, with when it went: a file deleted or removed from the NAS is still
            # judged by the folders it was in (decision 63). The return path skips it.
            await self.db.execute(
                "UPDATE catalog.nas_folder_acl SET gone_at = %s, updated_at = now()"
                " WHERE filer_id = %s AND path = %s AND observed_at <= %s",
                (observed, self.a["filer_id"], path, observed),
            )
            return {}
        acl_id, error, search, search_deny = None, None, None, []
        if ev.get("error") is not None:
            error = str(ev["error"])[:500] or "unreadable"
        else:
            try:
                body = acl_body(ev.get("acl"), self.a["protocol"])
                write, write_deny = folder_write_sets(ev["acl"], body)
                search, search_deny = folder_search_sets(ev["acl"], body)
            except (AclError, ApiError) as e:
                error = str(e)[:500] or "unreadable"
            else:
                # A principal the edge couldn't map may be kept out (a deny, or a POSIX
                # group entry that doesn't grant), and who it is isn't known (Codex on
                # #83). `acl_body` drops such an entry, so an identical ACL without it
                # has the same hash, and its row's sets would pass for this folder's.
                if search is None:
                    # Nobody is known to pass: hidden and reported, not stored.
                    error = "a principal the edge couldn't map may be kept out of it"
                elif write is None:
                    # Reads are judged as usual; nobody may place a file there. A row
                    # of its own, so a twin's write set can't stand in.
                    sha = hashlib.sha256(body["sha256"] + b"\0write unknown").digest()
                    body = {**body, "sha256": sha}
            if error is None:
                row = await (
                    await self.db.execute(
                        """
                        INSERT INTO catalog.acl
                            (dataset_id, sha256, owner_sid, group_sid, mode, entries, read_sids,
                             deny_sids, deny_first, raw_kind, raw, write_sids, write_deny_sids,
                             search_sids, search_deny_sids)
                        VALUES (%s, %s, %s, %s, %s, %s, %s::catalog.sid[], %s::catalog.sid[],
                                %s, %s, %s, %s::catalog.sid[], %s::catalog.sid[],
                                %s::catalog.sid[], %s::catalog.sid[])
                        ON CONFLICT (dataset_id, sha256) DO UPDATE
                           SET write_sids = coalesce(catalog.acl.write_sids,
                                                     EXCLUDED.write_sids),
                               write_deny_sids = CASE WHEN catalog.acl.write_sids IS NULL
                                                      THEN EXCLUDED.write_deny_sids
                                                      ELSE catalog.acl.write_deny_sids END,
                               search_sids = coalesce(catalog.acl.search_sids,
                                                      EXCLUDED.search_sids),
                               search_deny_sids = CASE WHEN catalog.acl.search_sids IS NULL
                                                       THEN EXCLUDED.search_deny_sids
                                                       ELSE catalog.acl.search_deny_sids END
                        RETURNING acl_id
                        """,
                        (
                            self.a["dataset_id"],
                            body["sha256"],
                            body["owner_sid"],
                            body["group_sid"],
                            body["mode"],
                            Jsonb(body["entries"]),
                            body["read"],
                            body["deny"],
                            body["deny_first"],
                            body["raw_kind"],
                            body["raw"],
                            write,
                            write_deny,
                            search,
                            None if search is None else search_deny,
                        ),
                    )
                ).fetchone()
                acl_id = row["acl_id"]
        await self.db.execute(
            """
            INSERT INTO catalog.nas_folder_acl
                (filer_id, path, acl_id, error, annex_id, observed_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (filer_id, path) DO UPDATE
               SET acl_id = EXCLUDED.acl_id, error = EXCLUDED.error,
                   annex_id = EXCLUDED.annex_id, observed_at = EXCLUDED.observed_at,
                   gone_at = NULL, updated_at = now()
             WHERE catalog.nas_folder_acl.observed_at <= EXCLUDED.observed_at
            """,
            (self.a["filer_id"], path, acl_id, error, self.a["annex_id"], observed),
        )
        if (error is not None or search is None) and self.above_files(path):
            # Decision 63: everything below it is hidden from everyone until it reads.
            why = error or "a deny names a principal the edge couldn't map"
            detail = (
                f"The permissions of folder {path or '/'!r} can't be read ({why[:300]})."
                " Every annexed file below it is hidden from everyone until they can be."
            )
            await self.event(
                "problem",
                {**ev, "path": None},
                details={"reason": "folder_unreadable", "folder": path, "detail": detail},
            )
        return {}

    def above_files(self, path: str) -> bool:
        """The folder is the annex's, or above its root up to the export's root."""
        root = self.a["path"]
        return (
            root == ""
            or path == ""
            or path == root
            or path.startswith(root + "/")
            or root.startswith(path + "/")
        )

    async def folders_read(self, ev: dict[str, Any]) -> dict[str, Any]:
        """The edge read every folder of the annex and every one above its root
        (decision 63). Until the first such pass, the annex's files are hidden."""
        details = ev.get("details") or {}
        if not isinstance(details, dict) or len(json.dumps(details)) > 4096:
            raise bad("details must be an object of at most 4 KiB")
        counts = {
            k: v
            for k in ("folders", "unreadable", "ancestors")
            if isinstance(v := details.get(k), int) and not isinstance(v, bool)
        }
        complete = details.get("complete") is True
        if complete:
            at = _observed(ev) or self.ctx.now
            await self.db.execute(
                "UPDATE catalog.annex SET folders_read_at = %s, folders_read = %s"
                " WHERE annex_id = %s AND (folders_read_at IS NULL OR folders_read_at <= %s)",
                (at, Jsonb(counts), self.a["annex_id"], at),
            )
        await self.event("folders_read", ev, details={**counts, "complete": complete})
        return {}

    async def target_space(self, ev: dict[str, Any]) -> dict[str, Any]:
        path = self.export_path(ev)
        free, total = ev.get("free_bytes"), ev.get("total_bytes")
        for k, v in (("free_bytes", free), ("total_bytes", total)):
            if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v < 2**63:
                raise bad(f"{k} must be a count of bytes")
        await self.db.execute(
            """
            INSERT INTO catalog.nas_target_space
                (filer_id, path, free_bytes, total_bytes, annex_id, observed_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (filer_id, path) DO UPDATE
               SET free_bytes = EXCLUDED.free_bytes, total_bytes = EXCLUDED.total_bytes,
                   annex_id = EXCLUDED.annex_id, observed_at = EXCLUDED.observed_at
            """,
            (self.a["filer_id"], path, free, total, self.a["annex_id"],
             _observed(ev) or self.ctx.now),
        )  # fmt: skip
        return {}

    async def apply(self, ev: Any, index: int) -> dict[str, Any]:
        if not isinstance(ev, dict):
            raise bad(f"events[{index}] must be an object")
        kind = ev.get("kind")
        if kind not in EVENT_KINDS:
            return {"index": index, "status": "rejected", "code": "unknown_kind",
                    "detail": f"unknown event kind {kind!r}"}  # fmt: skip
        try:
            async with self.db.transaction():  # a savepoint per event
                out = await getattr(self, kind)(ev)
        except Rejected as e:
            return {"index": index, "status": "rejected", "code": e.code, "detail": e.detail}
        except ApiError as e:
            if e.status >= 500:
                raise
            return {"index": index, "status": "rejected", "code": e.code, "detail": e.detail}
        except psycopg.Error as exc:
            err = map_db_error(exc)
            if err.status >= 500:
                raise err from exc
            return {"index": index, "status": "rejected", "code": err.code, "detail": err.detail}
        return {"index": index, "status": "applied", **out}


async def post_events(ctx: Ctx) -> Result:
    """`POST annex-events`: one annex's events, in order."""
    annex_id = req_id(ctx.body, "annex_id")
    events = req(ctx.body, "events")
    if not isinstance(events, list) or not 1 <= len(events) <= MAX_EVENTS:
        raise bad(f"events must be a list of 1 to {MAX_EVENTS} events")
    async with ctx.svc.catalog.tx(ctx.edge.principal, origin_edge_id=ctx.edge_id) as db:
        a = await load_annex(db, ctx.edge_id, annex_id, lock=True)
        if a["state"] == "retired":
            raise ApiError("version_conflict", f"annex {annex_id} is retired")
        batch = Batch(ctx, db, a)
        results = [await batch.apply(ev, i) for i, ev in enumerate(events)]
        await db.execute(
            "UPDATE catalog.annex SET last_event_at = now() WHERE annex_id = %s", (annex_id,)
        )
    return Result(200, {"results": results})


async def get_records(ctx: Ctx) -> Result:
    """`GET annexes/{annex_id}/records`: the catalog's view of the annex's
    live files, by path, for the edge's reconciliation."""
    annex_id = ctx.path_id("annex_id")
    q = ctx.request.query_params
    after = q.get("after") or ""
    if len(after) > MAX_PATH:
        raise bad("after is too long")
    try:
        limit = int(q.get("limit", "1000"))
    except ValueError:
        raise bad("limit must be an integer") from None
    if not 1 <= limit <= 5000:
        raise bad("limit must be 1 to 5000")
    async with ctx.svc.catalog.read() as db:
        await load_annex(db, ctx.edge_id, annex_id)
        rows = await (
            await db.execute(
                """
                SELECT af.path, af.file_id, af.nas_file_id, af.size, af.mtime_ns, af.ctime_ns,
                       af.copied_version_id, af.copied_size, af.copied_mtime_ns,
                       af.copied_ctime_ns, af.verified_version_id,
                       (l.location_id IS NOT NULL AND l.verified) AS copy_ok,
                       (af.copy_refused_version_id = af.copied_version_id
                        AND af.copy_refusals BETWEEN 1 AND %s
                        AND af.copy_stopped_at IS NULL
                        AND NOT coalesce(l.verified, false)) AS refused,
                       af.copy_stopped_at IS NOT NULL AS copy_stopped,
                       f.content_version_id
                  FROM catalog.annex_file af
                  JOIN catalog.file f ON f.file_id = af.file_id
                  LEFT JOIN catalog.location l ON l.version_id = af.copied_version_id
                       AND l.role = 'primary' AND l.state = 'committed'
                 WHERE af.annex_id = %s AND af.gone_since IS NULL AND af.path > %s
                 ORDER BY af.path LIMIT %s
                """,
                (MAX_REFUSALS - 1, annex_id, after, limit + 1),
            )
        ).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]

    def n(v: int | None) -> str | None:
        return None if v is None else str(v)

    return Result(
        200,
        {
            "records": [
                {
                    "path": r["path"],
                    "file_id": id_str(r["file_id"]),
                    "nas_file_id": r["nas_file_id"],
                    "size": r["size"],
                    "mtime_ns": n(r["mtime_ns"]),
                    "ctime_ns": n(r["ctime_ns"]),
                    "copied_version_id": id_str(r["copied_version_id"]),
                    "copied_size": r["copied_size"],
                    "copied_mtime_ns": n(r["copied_mtime_ns"]),
                    "copied_ctime_ns": n(r["copied_ctime_ns"]),
                    "verified": r["verified_version_id"] is not None
                    and r["verified_version_id"] == r["copied_version_id"],
                    "copy_ok": bool(r["copy_ok"]),
                    # The read-back refused this copy and another is wanted: what a
                    # reconciliation the service asked for copies again (copy_ok alone
                    # is false while a read-back is only pending).
                    "refused": bool(r["refused"]),
                    # An ArchiveAdmin stopped copies of these bytes again (migration
                    # 0058): no reconciliation copies them, until a version verifies or
                    # the admin resumes. A changed file is still copied.
                    "copy_stopped": bool(r["copy_stopped"]),
                }
                for r in rows
            ],
            "next": rows[-1]["path"] if more and rows else None,
        },
    )


__all__ = ["get_records", "list_annexes", "post_events", "parse_id"]


# --- dry runs (control API section 4.8) ---------------------------------------------------


async def list_dry_runs(ctx: Ctx) -> Result:
    """`GET annex-dry-runs?annex_id`: previews waiting for this edge's annexes."""
    raw = ctx.request.query_params.get("annex_id")
    annex_id = None
    if raw is not None:
        if not raw.isdigit() or len(raw) > 19:
            raise bad("annex_id must be an id")
        annex_id = int(raw)
    async with ctx.svc.catalog.tx(ctx.edge.principal, origin_edge_id=ctx.edge_id) as db:
        await dry_runs.expire(db, annex_id)
        rows = await (
            await db.execute(
                "SELECT d.run_id, d.annex_id, d.body FROM catalog.annex_dry_run d"
                "  JOIN catalog.annex a ON a.annex_id = d.annex_id"
                " WHERE a.edge_id = %s AND d.state = 'requested'"
                "   AND (%s::bigint IS NULL OR d.annex_id = %s::bigint)"
                " ORDER BY d.run_id LIMIT 20",
                (ctx.edge_id, annex_id, annex_id),
            )
        ).fetchall()
    return Result(200, {"dry_runs": [
        {"run_id": id_str(r["run_id"]), "annex_id": id_str(r["annex_id"]), "body": r["body"]}
        for r in rows
    ]})  # fmt: skip


async def post_dry_run(ctx: Ctx) -> Result:
    """`POST annex-dry-runs/{run_id}`: `running` when the edge takes a run
    (`409 version_conflict` if it is no longer waiting: replaced by a newer
    draft, or expired), then its answer. Once: a run that is answered
    already keeps its answer."""
    run_id = ctx.path_id("run_id")
    b = ctx.body
    state = b.get("state")
    if state not in ("running", "done", "failed"):
        raise bad("state must be running, done or failed")
    report = b.get("report")
    error = b.get("error")
    if state == "running":
        pass
    elif state == "done":
        if not isinstance(report, dict) or len(json.dumps(report)) > MAX_REPORT:
            raise bad("a done run has its report, an object of at most 1 MiB")
    elif not isinstance(error, str) or not error or len(error) > 2000:
        raise bad("a failed run says why (error, at most 2000 characters)")
    async with ctx.svc.catalog.tx(ctx.edge.principal, origin_edge_id=ctx.edge_id) as db:
        row = await (
            await db.execute(
                "SELECT d.state FROM catalog.annex_dry_run d"
                "  JOIN catalog.annex a ON a.annex_id = d.annex_id"
                " WHERE d.run_id = %s AND a.edge_id = %s FOR UPDATE OF d",
                (run_id, ctx.edge_id),
            )
        ).fetchone()
        if row is None:
            raise ApiError("not_found", f"dry run {run_id} is not this edge's")
        if state == "running":
            if row["state"] != "requested":
                raise ApiError("version_conflict", f"dry run {run_id} is {row['state']}")
            await db.execute(
                "UPDATE catalog.annex_dry_run SET state = 'running', taken_at = now()"
                " WHERE run_id = %s",
                (run_id,),
            )
            return Result(200, {"run_id": id_str(run_id), "state": "running"})
        if row["state"] in ("requested", "running"):
            await db.execute(
                "UPDATE catalog.annex_dry_run SET state = %s, done_at = now(), report = %s,"
                " error = %s WHERE run_id = %s",
                (state, Jsonb(report) if state == "done" else None,
                 error if state == "failed" else None, run_id),
            )  # fmt: skip
            state_now = state
        else:
            state_now = row["state"]
    return Result(200, {"run_id": id_str(run_id), "state": state_now})


# --- removals (phase A2.2; wp14-a2.md) -------------------------------------------------------

REMOVAL_STEPS = ("hide", "hidden", "delete", "deleted", "pointer", "done", "cancelled", "stuck")
#: The state each step needs (a step asked or reported again after a restart
#: from the state it left is answered as it is).
STEP_FROM = {
    "hide": ("ticketed",),
    "hidden": ("ticketed",),
    "delete": ("hidden", "deleting"),
    "deleted": ("deleting",),
    "pointer": ("deleted",),
    "done": ("deleted",),
    # The edge reports its own cancel, or the outcome of the W3 that follows
    # the service's: what it says is recorded.
    "cancelled": ("ticketed", "hidden", "deleting", "cancelled", "stuck"),
    # The edge couldn't put the file back: it stays hidden, raised.
    "stuck": ("hidden", "deleting", "cancelled", "stuck"),
}


def _candidate(c: Any, i: int) -> dict[str, Any]:
    if not isinstance(c, dict):
        raise bad(f"candidates[{i}] must be an object")
    rel_path(c.get("path"))
    rv = c.get("rules_version")
    return {
        "file_id": req_id(c, "file_id"),
        "version_id": req_id(c, "version_id"),
        "path": c["path"],
        "nas_file_id": _nas_id(c),
        "size": opt_int(c, "size", minimum=0),
        "mtime_ns": nanos(c.get("mtime_ns"), "mtime_ns"),
        "ctime_ns": nanos(c["ctime_ns"], "ctime_ns") if c.get("ctime_ns") is not None else None,
        "rule": str(c.get("rule") or "")[:64],
        # The rule set version the edge decided the removal under.
        "rules_version": int(rv) if isinstance(rv, (int, str)) and str(rv).isdigit() else None,
    }


def removal_json(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "removal_id": id_str(r["removal_id"]),
        "file_id": id_str(r["file_id"]),
        "version_id": id_str(r["version_id"]),
        "path": r["path"],
        "state": r["state"],
        "expires_at": rfc3339(r["expires_at"]),
        "ruleset_version": r["ruleset_version"],
        "expect": {
            "nas_file_id": r["nas_file_id"],
            "size": r["size"],
            "mtime_ns": str(r["mtime_ns"]),
            "ctime_ns": None if r["ctime_ns"] is None else str(r["ctime_ns"]),
            # The verified copy's CRC-64/NVME: the edge reads the file again
            # before the delete where the NAS lets a client set the ChangeTime.
            "crc64": None if r.get("crc64nvme") is None else bytes(r["crc64nvme"]).hex(),
        },
    }


async def _audit_step(
    db, ctx: Ctx, r: dict[str, Any], action: str, decision: str, **details: Any
) -> None:
    """Every removal step is audited (annex.md section 3.4): the file, the
    version, its verified CRC and the rule."""
    crc = r.get("crc64nvme")
    await db.execute(
        "INSERT INTO catalog.audit_log (actor, action, decision, file_id, version_id, details)"
        " VALUES (%s, %s, %s, %s, %s, %s)",
        (ctx.edge.principal, f"annex.removal.{action}", decision, r["file_id"], r["version_id"],
         Jsonb({"removal_id": id_str(r["removal_id"]), "rule": r["rule"], "path": r["path"],
                "ruleset_version": r["ruleset_version"],
                "crc64": None if crc is None else bytes(crc).hex(), **details})),
    )  # fmt: skip


async def post_removal_tickets(ctx: Ctx) -> Result:
    """`POST annex-removal-tickets`: a ticket per file the rules choose, or
    why not (`not_yet`: ask again later; `refused`: don't). Only while the
    annex's removals are live."""
    annex_id = req_id(ctx.body, "annex_id")
    cands = req(ctx.body, "candidates")
    if not isinstance(cands, list) or not 1 <= len(cands) <= 100:
        raise bad("candidates must be a list of 1 to 100 files")
    now = ctx.now
    async with ctx.svc.catalog.tx(ctx.edge.principal, origin_edge_id=ctx.edge_id) as db:
        await load_annex(db, ctx.edge_id, annex_id, lock=True)
        lv = await removals.live(db, annex_id, now)
        why = removals.why_not_live(lv, now)
        results = []
        for i, raw in enumerate(cands):
            c = _candidate(raw, i)
            fid = id_str(c["file_id"])
            if why is not None:
                results.append({"file_id": fid, "status": "not_yet", "reason": why})
                continue
            # The file must be this annex's before anything else is looked up.
            member = await (
                await db.execute(
                    "SELECT path FROM catalog.annex_file WHERE annex_id = %s AND file_id = %s",
                    (annex_id, c["file_id"]),
                )
            ).fetchone()
            if member is None:
                results.append({"file_id": fid, "status": "refused", "reason": "not_in_annex"})
                continue
            if member["path"] != c["path"]:
                # The catalog's path, never the edge's word for it.
                results.append({"file_id": fid, "status": "refused", "reason": "path_mismatch"})
                continue
            if c["rules_version"] is None:
                # Every candidate says which rule set decided it.
                results.append({"file_id": fid, "status": "refused",
                                "reason": "rules_version_required"})  # fmt: skip
                continue
            if c["rules_version"] != lv["rs_version"]:
                # Decided under another rule set: the edge decides again.
                results.append({"file_id": fid, "status": "not_yet",
                                "reason": "rule_set_changed"})  # fmt: skip
                continue
            open_ = await (
                await db.execute(
                    "SELECT r.*, l.crc64nvme FROM catalog.annex_removal r"
                    "  LEFT JOIN catalog.location l ON l.version_id = r.version_id"
                    "       AND l.role = 'primary' AND l.state = 'committed'"
                    " WHERE r.annex_id = %s AND r.file_id = %s"
                    "   AND r.state IN ('ticketed', 'hidden', 'deleting', 'stuck')",
                    (annex_id, c["file_id"]),
                )
            ).fetchone()
            if open_ is not None:
                # The same removal, asked again (a restarted unit): its ticket.
                results.append({"file_id": fid, "status": "ticket",
                                "removal": removal_json(open_)})  # fmt: skip
                continue
            status, reason, row = await removals.file_gate(db, lv, c, now)
            if status != "ticket":
                results.append({"file_id": fid, "status": status, "reason": reason})
                continue
            r = await (
                await db.execute(
                    "INSERT INTO catalog.annex_removal (annex_id, file_id, version_id, path,"
                    " nas_file_id, size, mtime_ns, ctime_ns, rule, ruleset_version, issued_at,"
                    " expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                    " RETURNING *",
                    (
                        annex_id,
                        c["file_id"],
                        c["version_id"],
                        member["path"],
                        c["nas_file_id"],
                        c["size"],
                        c["mtime_ns"],
                        c["ctime_ns"],
                        c["rule"] or "unknown",
                        lv["rs_version"],
                        now,
                        now + removals.TICKET_TTL,
                    ),
                )  # fmt: skip
            ).fetchone()
            r = {**r, "crc64nvme": row["crc64nvme"] if row else None}
            await db.execute(
                "UPDATE catalog.annex_file SET nas_state = 'removing', updated_at = now()"
                " WHERE annex_id = %s AND file_id = %s",
                (annex_id, c["file_id"]),
            )
            await db.execute(
                "INSERT INTO catalog.annex_event (annex_id, kind, path, file_id, version_id,"
                " size, details) VALUES (%s, 'removal_scheduled', %s, %s, %s, %s, %s)",
                (annex_id, member["path"], c["file_id"], c["version_id"], c["size"],
                 Jsonb({"removal_id": id_str(r["removal_id"]), "rule": c["rule"]})),
            )  # fmt: skip
            await _audit_step(db, ctx, r, "ticket", "allow")
            results.append({"file_id": fid, "status": "ticket", "removal": removal_json(r)})
    return Result(200, {"results": results})


async def post_removal_step(ctx: Ctx) -> Result:
    """`POST annex-removals/{removal_id}`: the edge asks before each write
    (`hide`, `delete`: `{proceed, reason}`; every gate is checked on every
    ask), and reports each step after it (`hidden`, `deleted`, `pointer`,
    `done`, `cancelled`, `stuck`). A report of a step already recorded is
    answered as it is."""
    removal_id = ctx.path_id("removal_id")
    b = ctx.body
    step = b.get("step")
    if step not in REMOVAL_STEPS:
        raise bad("step must be one of " + ", ".join(REMOVAL_STEPS))
    now = ctx.now
    async with ctx.svc.catalog.tx(ctx.edge.principal, origin_edge_id=ctx.edge_id) as db:
        r = await (
            await db.execute(
                "SELECT r.*, l.crc64nvme FROM catalog.annex_removal r"
                "  JOIN catalog.annex a ON a.annex_id = r.annex_id"
                "  LEFT JOIN catalog.location l ON l.version_id = r.version_id"
                "       AND l.role = 'primary' AND l.state = 'committed'"
                " WHERE r.removal_id = %s AND a.edge_id = %s FOR UPDATE OF r",
                (removal_id, ctx.edge_id),
            )
        ).fetchone()
        if r is None:
            raise ApiError("not_found", f"removal {removal_id} is not this edge's")
        if r["state"] not in STEP_FROM[step]:
            # Reported again after a restart: the answer is the state now. An
            # ask (hide, delete) is never answered from a state: see below.
            proceed = step == "delete" and r["state"] in ("deleted", "done")
            return Result(200, {"removal_id": id_str(removal_id), "state": r["state"],
                                "proceed": proceed, "reason": None})  # fmt: skip
        sets: dict[str, Any] = {}
        proceed = True
        reason = None
        if step in ("hide", "delete"):
            # Every gate, on every ask: a delete asked again after a restart
            # is checked as the first was (holds, windows and rule sets may
            # have changed while the edge was away).
            lv = await removals.live(db, r["annex_id"], now)
            reason = removals.why_not_live(lv, now)
            hold = reason in removals.HOLD_REASONS
            if reason is None and r["ruleset_version"] != lv["rs_version"]:
                reason = "rule_set_changed"
            if reason is None and step == "hide" and r["expires_at"] < now:
                reason = "ticket_expired"
            if reason is None and step == "delete":
                grace = timedelta(minutes=int(lv["grace_minutes"]))
                if r["hidden_at"] is None or r["hidden_at"] + grace > now:
                    # The service keeps the grace too, whatever the edge does.
                    reason, hold = "grace", True
            if reason is None:
                c = {k: r[k] for k in ("file_id", "version_id", "nas_file_id", "size",
                                       "mtime_ns", "ctime_ns", "rule")}  # fmt: skip
                status, reason, _ = await removals.file_gate(db, lv, c, now, recheck=True)
                hold = status == "not_yet"
                if status == "ticket":
                    reason = None
            if reason is not None and step == "delete" and hold:
                # Paused, outside the window, or in its grace: the file stays
                # hidden. A pause or a window starts the grace again.
                if reason in ("paused", "outside_window"):
                    await db.execute(
                        "UPDATE catalog.annex_removal SET hidden_at = %s, updated_at = now()"
                        " WHERE removal_id = %s",
                        (now, removal_id),
                    )
                await _audit_step(db, ctx, r, "delete", "deny", reason=reason, hold=True)
                return Result(200, {"removal_id": id_str(removal_id), "state": r["state"],
                                    "proceed": False, "reason": reason})  # fmt: skip
            if reason is not None:
                proceed = False
                sets = {"state": "cancelled", "cancel_reason": reason}
            elif step == "delete":
                sets = {"state": "deleting"}
            await _audit_step(db, ctx, r, step, "allow" if proceed else "deny", reason=reason)
        elif step == "hidden":
            name = b.get("hidden_name")
            if not isinstance(name, str) or not name.startswith(".omnifile-removing-"):
                raise bad("hidden_name must be the .omnifile-removing- name")
            sets = {"state": "hidden", "hidden_name": name[:512], "hidden_at": now}
        elif step == "deleted":
            sets = {"state": "deleted", "deleted_at": now}
        elif step == "pointer":
            sets = {"pointer_written": bool(b.get("written"))}
        elif step == "done":
            sets = {"state": "done", "done_at": now}
        elif step == "cancelled":
            reason = str(b.get("reason") or "cancelled")[:200]
            if r["state"] == "cancelled" and r["cancel_reason"] == reason:
                # The same report again (a retry): already recorded, once.
                return Result(200, {"removal_id": id_str(removal_id), "state": "cancelled",
                                    "proceed": False, "reason": reason})  # fmt: skip
            sets = {"state": "cancelled", "cancel_reason": reason}
        elif step == "stuck":
            reason = str(b.get("reason") or "left hidden")[:200]
            sets = {"state": "stuck", "cancel_reason": reason}
            if isinstance(b.get("hidden_name"), str):
                sets["hidden_name"] = b["hidden_name"][:512]
        if sets:
            cols = ", ".join(f"{k} = %({k})s" for k in sets)
            await db.execute(
                f"UPDATE catalog.annex_removal SET {cols}, updated_at = now()"
                " WHERE removal_id = %(id)s",
                {**sets, "id": removal_id},
            )
        kind = {"hidden": "renamed_for_removal", "deleted": "removed",
                "stuck": "removal_stuck"}.get(step)  # fmt: skip
        if sets.get("state") == "cancelled":
            kind = "removal_cancelled"
        if kind:
            await db.execute(
                "INSERT INTO catalog.annex_event (annex_id, kind, path, file_id, version_id,"
                " size, details) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (r["annex_id"], kind, r["path"], r["file_id"], r["version_id"], r["size"],
                 Jsonb({"removal_id": id_str(removal_id), "rule": r["rule"],
                        "reason": sets.get("cancel_reason"),
                        "hidden_name": sets.get("hidden_name") or r["hidden_name"]})),
            )  # fmt: skip
        if step not in ("hide", "delete"):
            await _audit_step(db, ctx, r, step, "deny" if step in ("cancelled", "stuck")
                              else "allow", reason=sets.get("cancel_reason"),
                              written=sets.get("pointer_written"))  # fmt: skip
        kept = b.get("kept_as") if step == "cancelled" else None
        if isinstance(kept, str) and 0 < len(kept.encode()) <= 255 and "/" not in kept \
                and "\x00" not in kept and "-replaced-" in kept:  # fmt: skip
            # A user's file the removal's W1 hid by mistake (a save-by-rename just
            # before it), its own name taken since: kept under this name (#98's E2).
            folder = r["path"].rpartition("/")[0]
            aside = "/".join(x for x in [folder, kept] if x)
            log.error("annex removal %s: a user's file it hid is kept as %s", removal_id, aside)
            await db.execute(
                "INSERT INTO catalog.annex_event (annex_id, kind, path, file_id, version_id,"
                " details) VALUES (%s, 'problem', %s, %s, %s, %s)",
                (r["annex_id"], r["path"], r["file_id"], r["version_id"],
                 Jsonb({"removal_id": id_str(removal_id), "reason": "removal_kept_aside",
                        "kept_as": aside,
                        "detail": f"A user's file at {r['path']} was hidden by a removal"
                                  f" (it was saved just before the hide). Its name is"
                                  f" taken now, so it is kept as {aside}. Nothing was"
                                  " deleted."})),
            )  # fmt: skip
        if step == "stuck":
            # An alert: a file the annex hid can't be put back by itself.
            log.error("annex removal %s of %s is left hidden as %s: %s", removal_id, r["path"],
                      sets.get("hidden_name") or r["hidden_name"], reason)  # fmt: skip
        if step == "deleted":
            await db.execute(
                "UPDATE catalog.annex_file SET nas_state = 'removed', removed_at = %s,"
                " updated_at = now() WHERE annex_id = %s AND file_id = %s",
                (now, r["annex_id"], r["file_id"]),
            )
        elif sets.get("state") == "cancelled":
            await db.execute(
                "UPDATE catalog.annex_file SET nas_state = 'present', updated_at = now()"
                " WHERE annex_id = %s AND file_id = %s AND nas_state = 'removing'",
                (r["annex_id"], r["file_id"]),
            )
    state = sets.get("state", r["state"])
    return Result(200, {"removal_id": id_str(removal_id), "state": state, "proceed": proceed,
                        "reason": reason})  # fmt: skip
