"""What each app may do in the catalog database (threat model T6).

Each app's identity logs in as its own database role (`pgaadauth_create_principal`,
`deploy/azure/README.md` step 5) and is made a member of one group role here:

| Group role | App |
|---|---|
| `omnifile_api` | `api`: the edges' control API |
| `omnifile_enrol` | `enrol`: edge enrolment |
| `omnifile_web` | `web`: the portal and the partner API |
| `omnifile_worker` | `worker`, and the verifier job (it runs the worker's code) |

No app owns a table, so none can change the schema, drop or truncate a table,
or turn a trigger off. Beyond that:

- **Feed and audit side tables are written by triggers only.** The catalog's
  trigger functions run as their owner (`SECURITY DEFINER`, with a fixed
  `search_path`), and no role may EXECUTE them or create temporary tables
  (the worker aside) to attach them to, so `feed_event`, `recall_state_log` and the audit rows the
  triggers write need no grant. No app can insert a feed event, which edges
  act on: the `api` adds the `conflict` events of `POST …/changes` through an
  owner-run function that writes nothing else.
- **The audit log is append-only** (a trigger refuses UPDATE and DELETE), and
  apps get only INSERT on it.
- **Sessions and sign-ins** (`web_session`, `oidc_login`) are `web`'s alone.
- **Owner work** the worker needs: making and dropping time partitions
  (`ensure_time_partitions`, `feed_drop_before`), reading the DEFAULT
  partitions (`default_partition_rows`), the seeder's ANALYZE
  (`analyze_namespace`) and the bulk seed's merge (`seed_merge`, migration
  0033: the per-row INSERT triggers it makes up for skip only inside it).
  These run as their owner, and only `omnifile_worker` may call them.
  `feed_restored` (after a catalog restore) is the administrator's.
- **Worker-only tables:** `seed_stage`, a seed batch between its COPY and
  `seed_merge`; no other app may read it.
- **Queues:** an app may send only to the queues it sends to; only the worker
  reads, deletes and archives.

`apply()` runs at the end of every `migrate()`, so tables a migration adds get
their grants in the same run. It needs `CREATEROLE` the first time (the roles
are made then); after that, ownership of the catalog is enough. Memberships
come from ``OMNIFILE_DB_MEMBERS`` (``api=id-x-api,worker=id-x-worker,worker=id-x-verifier,...``).

The lists below were checked by running every service test suite with each
app's connections under its group role (`tests/conftest` helpers): a test
fails with `permission denied` when a grant is missing.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

log = logging.getLogger(__name__)

ROLES = {
    "api": "omnifile_api",
    "enrol": "omnifile_enrol",
    "web": "omnifile_web",
    "worker": "omnifile_worker",
}

#: Readable by one app only (the worker deletes expired rows).
PRIVATE: dict[str, set[str]] = {
    "web_session": {"web"},
    "oidc_login": {"web"},
    # A Connect Azure sign-in in flight holds a PKCE verifier (0044).
    "azure_connect_op": {"web"},
}
WORKER_ON_PRIVATE = "SELECT, DELETE"

#: The worker's alone, with these privileges; no other app may even read
#: them. `seed_stage` holds a seed batch between its COPY and
#: `catalog.seed_merge` (which removes the rows, as their owner).
WORKER_ONLY: dict[str, str] = {
    "seed_stage": "SELECT, INSERT",
}

#: Never written by an app (triggers and owner functions do).
TRIGGER_WRITTEN = {
    "feed_event",
    "time_partition",
    "feed_retention",
    "recall_state_log",
    "partner_event",
    # Decision 63: each annex folder's place in the export, kept by a trigger.
    "annex_dir_gate",
}

#: Writes per app, by table: privileges beyond SELECT.
WRITES: dict[str, dict[str, str]] = {
    "api": {
        "acl": "INSERT, UPDATE",
        "acl_scan": "INSERT, UPDATE",
        # Annex events from the edge (api/annex.py): the copier's files, its
        # timeline, scan times and the filer's mount check.
        "annex": "UPDATE (state, baseline_at, baseline_files, baseline_bytes, last_quick_scan_at,"
        " last_full_scan_at, last_reconcile_at, last_event_at, reconcile_asked_at,"
        " folders_read_at, folders_read)",
        "annex_event": "INSERT",
        # The catalog folders an annex makes for the NAS's (decision 63).
        "annex_folder": "INSERT",
        "annex_file": "INSERT, UPDATE (size, mtime_ns, ctime_ns, btime_ns, change_id, last_seen,"
        " updated_at, copied_version_id, copied_size, copied_mtime_ns, copied_ctime_ns, copied_at,"
        " verified_version_id, verified_at, gone_since, nas_state, path, rule, removed_at,"
        " nas_file_id, hold_until, pinned_by_breaker, first_seen, purge_refused_at,"
        " copy_refusals, copy_refused_version_id, copy_refused_at,"
        # A copy verified at its event ends an admin's stop and acknowledgement (0058).
        " copy_stopped_at, copy_stopped_by, copy_refused_ack_at, copy_refused_ack_by)",
        # The edge's answers to dry runs (control API section 4.8).
        "annex_dry_run": "UPDATE (state, taken_at, done_at, report, error)",
        # Removal tickets and their steps (phase A2.2).
        "annex_removal": "INSERT, UPDATE (state, hidden_name, hidden_at, deleted_at, done_at,"
        " pointer_written, cancel_reason, updated_at)",
        "audit_log": "INSERT",
        "edge": "UPDATE",
        # Placements on a NAS (A3.3): their tickets and steps, the annex file
        # back where it came from, and the recall a placement closes.
        "annex_placement": "UPDATE (state, outcome, fail_reason, fail_detail, final_name,"
        " final_path, hold_until, crc64, done_at, updated_at, before_stuck, placed_ino,"
        " last_report_at, last_edge_report,"
        # A replace at the origin (A3.4, 0060).
        " replace_state, replace_refused, replaced_version_id, replaced_crc64,"
        " replace_hidden_name, replace_hidden_at, replaced_aside_name, replace_asked_at,"
        " replace_go_at, replace_place_at, replace_delete_at)",
        # Folder ACLs and free space the edge reads for the return path (A3.1).
        "nas_folder_acl": "INSERT, UPDATE, DELETE",
        "nas_target_space": "INSERT, UPDATE",
        "file": "INSERT, UPDATE",
        "file_version": "INSERT, UPDATE, DELETE",
        "filer": "UPDATE (health, health_checked_at, placement_account, placement_account_at,"
        " acls_hidden_since, acls_hidden_ack_by, acls_hidden_ack_at)",
        "hold": "INSERT, UPDATE",
        "idempotency_key": "INSERT, UPDATE, DELETE",
        "identity": "INSERT, UPDATE",
        "identity_observation": "INSERT, UPDATE",
        # Uploads commit and verify a location; a removal asks for its read.
        "location": "INSERT, DELETE, UPDATE (state, committed_at, verified, verified_at,"
        " verified_by, crc64nvme, etag, backend_version_id, logical_tier, storage_class,"
        " tier_changed_at, size, updated_at, azure_read_requested_at)",
        "notification": "INSERT, UPDATE",
        "recall": "INSERT, UPDATE",
        "recall_requester": "INSERT, UPDATE",
        # Annex reconciliation's drift (source 'annex').
        "reconcile_drift": "INSERT, UPDATE (last_seen_at, observed, expected)",
        # The service's reachable window, kept by every heartbeat (0040).
        "heartbeat_window": "INSERT, UPDATE (started_at, last_at)",
        "snapshot_export": "INSERT, UPDATE (prefix)",
        "teams_intent": "INSERT, UPDATE",
    },
    "enrol": {
        "audit_log": "INSERT",
        "edge": "INSERT, UPDATE",
        # Roles Anywhere certificates it signs (decision 50), for the CRL.
        "edge_ra_certificate": "INSERT",
        "idempotency_key": "INSERT, UPDATE, DELETE",
    },
    "web": {
        # Settings, API clients (migration 0035): register, edit, disable, revoke;
        # last_used_at from the bearer check.
        "api_client": "INSERT, UPDATE",
        # OmniFile-issued keys: created, used (last_used_*), shortened or revoked.
        "api_key": "INSERT, UPDATE (expires_at, revoked_at, last_used_at, last_used_addr)",
        # Annexes (routes_annex.py): filers (their `nas` backend row through
        # catalog.nas_backend_add), the annex and its fixed Copy rule set, the
        # defaults, and purge requests (the worker purges).
        "annex": "INSERT, UPDATE (placements_paused, state, quiet_minutes, quick_scan_minutes,"
        " full_scan_minutes,"
        " reconcile_days, deleted_retention_days, ruleset_version, mode, removals_paused,"
        " dry_run_until, removal_rate_per_hour, removal_window, pointer_files, grace_minutes,"
        " removals_confirmed_version, removals_confirmed_by, removals_confirmed_at)",
        # Previews asked for; drafts replaced and runs expired (dry_runs.py).
        "annex_dry_run": "INSERT, UPDATE (state, done_at, error)",
        "annex_defaults": "UPDATE",
        "annex_event": "INSERT",
        "annex_file": "UPDATE (purge_requested_at, purge_requested_by, purge_refused_at,"
        " pinned_by_breaker, breaker_cleared_at, updated_at,"
        # A refused copy stopped or acknowledged by an ArchiveAdmin (0058).
        " copy_stopped_at, copy_stopped_by, copy_refused_ack_at, copy_refused_ack_by)",
        # A recall that places the file somewhere (phase A3.1).
        "annex_placement": "INSERT, UPDATE (state, updated_at, done_at, outcome, fail_reason,"
        " fail_detail, recall_id)",
        # An app's proposed rule set, approved by a person.
        "annex_ruleset": "INSERT, UPDATE (state, decided_by_identity_id, decided_at, decision_note,"
        " dry_run_id, decided_as_archive_admin)",
        "audit_log": "INSERT",
        # Connect Azure and AWS (migration 0044, cloud-connect.md): sign-ins in
        # flight, the connection, the grants made, confirmed AWS entries, and
        # tasks for the worker (an AWS fetch, a Storage Mover deployment, a scan).
        "azure_connect_op": "INSERT, UPDATE, DELETE",
        "azure_connection": "INSERT, UPDATE",
        "azure_grant": "INSERT, UPDATE",
        "aws_connection": "INSERT, UPDATE",
        "cloud_task": "INSERT",
        # `SELECT ... FOR UPDATE` (policy and share changes lock their
        # dataset) needs UPDATE on some column; a source starts a migration.
        "dataset": "INSERT, UPDATE (updated_at, migration_state, source_backend_id)",
        "download_request": "INSERT",
        "edge": "INSERT, UPDATE",
        "filer": "INSERT, UPDATE (placement_sid, entity_version, access_limited, access_groups,"
        " access_confirmed_by, access_confirmed_at, acls_hidden_ack_by, acls_hidden_ack_at)",
        # A policy change reschedules the dataset's files.
        "file": "INSERT, UPDATE (next_action, next_action_at)",
        # Legal holds and WORM retention extensions mark the versions under a
        # folder; nothing else of a version.
        "file_version": "UPDATE (legal_hold, retention_until)",
        "hold": "INSERT, UPDATE",
        "identity": "INSERT, UPDATE",
        "idempotency_key": "INSERT, UPDATE, DELETE",
        # The partner API's migration actions (routes_migrations.py):
        # cutover, cancel, retry and an external copier's run reports.
        "migration_exclusion": "INSERT",
        "migration_prefix": "INSERT, UPDATE (state, updated_at, last_error, cutover_requested_at,"
        " cutover_requested_by, cancelled_at, cancelled_by, last_run_id, run_state, run_counters,"
        " last_copy_started_at, verified_at, rerun_requested_at, restore_requested_at)",
        "migration_run": "INSERT, UPDATE",
        # Staged plans (routes_plans.py): made, decided and rescheduled in web;
        # the worker's plan sweep starts the waves.
        "migration_plan": "INSERT, UPDATE",
        "migration_wave": "INSERT, UPDATE",
        "oidc_login": "INSERT, DELETE",
        "policy": "INSERT, UPDATE",
        "policy_proposal": "INSERT, UPDATE",
        "price_table": "INSERT, UPDATE, DELETE",
        # Settings, Prices and alerts (migration 0043): who sees prices.
        "price_display": "UPDATE",
        "recall": "INSERT, UPDATE",
        "recall_batch": "INSERT, UPDATE",
        "recall_batch_item": "INSERT, UPDATE",
        "recall_requester": "INSERT, UPDATE",
        "settings_test": "INSERT",
        "share": "INSERT, UPDATE, DELETE",
        "source": "INSERT, UPDATE",
        "tier_pin": "INSERT, UPDATE",
        "version_restore": "INSERT",
        "web_session": "INSERT, UPDATE, DELETE",
    },
}

#: Queues each app sends to (the worker reads them all).
SENDS: dict[str, set[str]] = {
    "api": {"identity_resolve", "notify", "restores", "snapshot_export", "verify"},
    "enrol": set(),
    # roles_anywhere_crl: a revocation publishes the edge CA's CRL (decision 50).
    "web": {
        "identity_resolve",
        "migrate",
        "notify",
        "portal",
        "restores",
        "retention",
        "roles_anywhere_crl",
        "seed",
        # Settings: a test card, email or AWS role assumption (0035).
        "settings_test",
        # Connect AWS, Storage Mover deployment, source scans (0044).
        "cloud_task",
    },
}

#: Owner-run functions and who may call them.
OWNER_FUNCTIONS: dict[str, set[str]] = {
    "catalog.ensure_time_partitions(regclass, text, integer)": {"worker"},
    "catalog.feed_drop_before(timestamp with time zone)": {"worker"},
    "catalog.feed_restored()": set(),
    # The seeder's ANALYZE (sources/run.py), which needs the owner.
    "catalog.analyze_namespace()": {"worker"},
    # The bulk seed's merge (sources/seed.py, migration 0033): it skips the
    # per-row INSERT triggers it makes up for, which only the owner can.
    "catalog.seed_merge(bigint, bigint, bigint, text, bigint, bigint, bigint, integer, text,"
    " timestamp with time zone)": {"worker"},
    # It reads the DEFAULT partitions themselves, which no app is granted.
    "catalog.default_partition_rows()": {"worker"},
    # The conflict events of POST .../changes (api/changes.py).
    "catalog.feed_conflict(bigint, bigint, text, text)": {"api"},
    # A filer's `nas` backend row (routes_annex.py, migration 0038): web has no INSERT on
    # catalog.backend; this adds one row of kind `nas`, private, and nothing else.
    "catalog.nas_backend_add(text, jsonb)": {"web"},
    # Settings, Storage (portal-api.md 5.20): web's only write of catalog.backend, always
    # private, audited as the person (migration 0035). web has no INSERT or UPDATE on it.
    "catalog.backend_register(text, text, jsonb, text, text, inet, jsonb)": {"web"},
}

#: Function bodies are schema-qualified, so nothing is looked up in `public`
#: (where, on PostgreSQL 14 and older, any role may create objects).
_SEARCH_PATH = "pg_catalog, pg_temp"


def _ident(*parts: str) -> sql.Composable:
    return sql.Identifier(*parts)


def _tables(conn: psycopg.Connection, schema: str) -> list[str]:
    """Tables an app reaches: plain and partitioned parents, not partitions."""
    rows = conn.execute(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = %s AND c.relkind IN ('r', 'p') AND NOT c.relispartition"
        " ORDER BY 1",
        (schema,),
    ).fetchall()
    return [r[0] for r in rows]


def _sequences(conn: psycopg.Connection, schema: str) -> set[str]:
    rows = conn.execute(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = %s AND c.relkind = 'S'",
        (schema,),
    ).fetchall()
    return {r[0] for r in rows}


def _queues(conn: psycopg.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT queue_name FROM pgmq.meta ORDER BY 1").fetchall()]


def _ensure_role(conn: psycopg.Connection, role: str) -> None:
    conn.execute(
        sql.SQL(
            "DO $$ BEGIN CREATE ROLE {} NOLOGIN;"
            " EXCEPTION WHEN duplicate_object OR unique_violation THEN NULL; END $$"
        ).format(_ident(role))
    )


def apply(
    conn: psycopg.Connection,
    members: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
) -> None:
    """Create the group roles, make the trigger functions owner-run, and grant
    each role exactly its privileges (revoking first, so a grant removed here
    is removed in the database too). One transaction."""
    with conn.transaction():
        for role in ROLES.values():
            _ensure_role(conn, role)
        _owner_run_triggers(conn)
        # Temporary tables are the worker's alone: with TEMP, a role could
        # attach an owner-run trigger function to a table of its own (EXECUTE
        # is revoked too; this is the second lock).
        db = conn.execute("SELECT current_database()").fetchone()[0]  # type: ignore[index]
        conn.execute(sql.SQL("REVOKE TEMP ON DATABASE {} FROM PUBLIC").format(_ident(db)))
        for app, role in ROLES.items():
            verb = (
                "GRANT {} ON DATABASE {} TO {}"
                if app == "worker"
                else "REVOKE {} ON DATABASE {} FROM {}"
            )
            conn.execute(sql.SQL(verb).format(sql.SQL("TEMP"), _ident(db), _ident(role)))
        pairs = list(members.items() if isinstance(members, Mapping) else (members or []))
        for app, _login in pairs:
            if app not in ROLES:
                raise ValueError(f"unknown app {app!r}: one of {', '.join(ROLES)}")
        # Grants made before these roles existed (to PUBLIC, or straight to an
        # app's login, as the old setup guide said) go: only the group roles grant.
        for who in [sql.SQL("PUBLIC")] + [_ident(login) for _a, login in pairs]:
            for schema in ("catalog", "pgmq"):
                for kind in ("TABLES", "SEQUENCES"):
                    conn.execute(
                        sql.SQL("REVOKE ALL ON ALL {} IN SCHEMA {} FROM {}").format(
                            sql.SQL(kind), _ident(schema), who
                        )
                    )
        catalog = _tables(conn, "catalog")
        pgmq_tables = _tables(conn, "pgmq")
        queues = _queues(conn)
        pgmq_seqs = _sequences(conn, "pgmq")
        for app, role in ROLES.items():
            r = _ident(role)
            for schema in ("catalog", "pgmq"):
                conn.execute(
                    sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA {} FROM {}").format(
                        _ident(schema), r
                    )
                )
                conn.execute(
                    sql.SQL("REVOKE ALL ON ALL SEQUENCES IN SCHEMA {} FROM {}").format(
                        _ident(schema), r
                    )
                )
                conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(_ident(schema), r))
            for t in catalog:
                privs = "SELECT"
                if t in WORKER_ONLY:
                    if app != "worker":
                        continue
                    privs = WORKER_ONLY[t]
                elif t in PRIVATE and app not in PRIVATE[t]:
                    if app != "worker":
                        continue
                    privs = WORKER_ON_PRIVATE
                elif app == "worker":
                    if t in TRIGGER_WRITTEN:
                        pass
                    elif t == "audit_log":
                        privs += ", INSERT"
                    else:
                        privs += ", INSERT, UPDATE, DELETE"
                elif t in WRITES.get(app, {}):
                    privs += ", " + WRITES[app][t]
                conn.execute(
                    sql.SQL("GRANT {} ON {} TO {}").format(sql.SQL(privs), _ident("catalog", t), r)
                )
            conn.execute(sql.SQL("GRANT USAGE ON ALL SEQUENCES IN SCHEMA catalog TO {}").format(r))
            # Queues. pgmq.send reads pgmq.meta and the insert throttle.
            for t in ("meta", "notify_insert_throttle"):
                if t in pgmq_tables:
                    conn.execute(sql.SQL("GRANT SELECT ON {} TO {}").format(_ident("pgmq", t), r))
            for q in queues:
                for prefix, worker_privs in (("q_", "SELECT, INSERT, UPDATE, DELETE"),
                                             ("a_", "SELECT, INSERT, DELETE")):  # fmt: skip
                    t = prefix + q
                    if t not in pgmq_tables:
                        continue
                    if app == "worker":
                        privs = worker_privs
                    elif prefix == "q_" and q in SENDS.get(app, set()):
                        privs = "SELECT, INSERT"  # INSERT ... RETURNING msg_id
                    else:
                        continue
                    conn.execute(
                        sql.SQL("GRANT {} ON {} TO {}").format(sql.SQL(privs), _ident("pgmq", t), r)
                    )
                    # pgmq.metrics reads each queue's sequence (worker only).
                    if app == "worker" and (seq := f"{t}_msg_id_seq") in pgmq_seqs:
                        conn.execute(
                            sql.SQL("GRANT USAGE, SELECT ON SEQUENCE {} TO {}").format(
                                _ident("pgmq", seq), r
                            )
                        )
        for fn, apps in OWNER_FUNCTIONS.items():
            conn.execute(sql.SQL("ALTER FUNCTION {} SECURITY DEFINER SET search_path = {}").format(
                sql.SQL(fn), sql.SQL(_SEARCH_PATH)))  # fmt: skip
            conn.execute(sql.SQL("REVOKE ALL ON FUNCTION {} FROM PUBLIC").format(sql.SQL(fn)))
            for app, role in ROLES.items():
                verb = "GRANT" if app in apps else "REVOKE"
                prep = "TO" if verb == "GRANT" else "FROM"
                conn.execute(
                    sql.SQL("{} EXECUTE ON FUNCTION {} {} {}").format(
                        sql.SQL(verb), sql.SQL(fn), sql.SQL(prep), _ident(role)
                    )
                )
        for app, login in pairs:
            conn.execute(sql.SQL("GRANT {} TO {}").format(_ident(ROLES[app]), _ident(login)))
        # An app named in the list has exactly the logins listed: a login moved
        # to another app, or dropped from the list, loses the role. Members
        # that may administer the role (its creator) stay.
        for app in {a for a, _l in pairs}:
            keep = {login for a, login in pairs if a == app}
            stale = conn.execute(
                "SELECT m.rolname FROM pg_auth_members am"
                " JOIN pg_roles g ON g.oid = am.roleid JOIN pg_roles m ON m.oid = am.member"
                " WHERE g.rolname = %s AND NOT am.admin_option AND m.rolname <> current_user",
                (ROLES[app],),
            ).fetchall()
            for (login,) in stale:
                if login not in keep:
                    conn.execute(
                        sql.SQL("REVOKE {} FROM {}").format(_ident(ROLES[app]), _ident(login))
                    )
        _warn_foreign_owners(conn)
        _check_temp(conn)


def _owner_run_triggers(conn: psycopg.Connection) -> None:
    """Every trigger function in `catalog` runs as its owner, with a fixed
    search_path, and no one may EXECUTE it: EXECUTE is checked only when a
    trigger is created, so the catalog's own triggers keep firing, but no role
    can attach one of these to a table of its own and run it as the owner."""
    rows = conn.execute(
        "SELECT p.oid::regprocedure::text FROM pg_proc p"
        " WHERE p.pronamespace = 'catalog'::regnamespace AND p.prorettype = 'trigger'::regtype"
    ).fetchall()
    for (fn,) in rows:
        conn.execute(
            sql.SQL("ALTER FUNCTION {} SECURITY DEFINER SET search_path = {}").format(
                sql.SQL(fn), sql.SQL(_SEARCH_PATH)
            )
        )
        conn.execute(sql.SQL("REVOKE ALL ON FUNCTION {} FROM PUBLIC").format(sql.SQL(fn)))
        for role in ROLES.values():
            conn.execute(
                sql.SQL("REVOKE ALL ON FUNCTION {} FROM {}").format(sql.SQL(fn), _ident(role))
            )


def _check_temp(conn: psycopg.Connection) -> bool:
    """A REVOKE the administrator may not make (it doesn't own the database)
    is only a warning in PostgreSQL. Say so if an app other than the worker
    can still make temporary tables (the second lock on the trigger
    functions; EXECUTE stays revoked either way)."""
    ok = True
    for app, role in ROLES.items():
        if app == "worker":
            continue
        (has,) = conn.execute(  # type: ignore[misc]
            "SELECT has_database_privilege(%s, current_database(), 'TEMP')", (role,)
        ).fetchone()
        if has:
            ok = False
            log.error(
                "%s can still create temporary tables: the administrator could not revoke"
                " TEMP on the database (make it the database's owner and run migrate again)",
                role,
            )
    return ok


def _warn_foreign_owners(conn: psycopg.Connection) -> None:
    """A table or sequence owned by anyone but the administrator gives its
    owner DDL on it (for example a dead-letter queue a worker created before
    this module). Logged; `ALTER ... OWNER TO` it by hand."""
    rows = conn.execute(
        "SELECT n.nspname || '.' || c.relname, pg_get_userbyid(c.relowner)"
        "  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname IN ('catalog', 'pgmq') AND c.relkind IN ('r', 'p', 'S')"
        "   AND c.relowner <> (SELECT oid FROM pg_roles WHERE rolname = current_user)"
    ).fetchall()
    for name, owner in rows:
        log.warning(
            "%s is owned by %s, not the administrator: ALTER it OWNER TO the admin", name, owner
        )


def as_app(conninfo: str, app: str) -> str:
    """The same database, with the session acting as `app`'s group role (the
    login must be a member, or a superuser). Tests run each app's connections
    this way, so a missing grant fails them."""
    params = conninfo_to_dict(conninfo)
    params["options"] = f"{params.get('options', '')} -c role={ROLES[app]}".strip()
    params.setdefault("application_name", ROLES[app])
    return make_conninfo(**params)


def parse_members(raw: str | None) -> list[tuple[str, str]]:
    """``api=id-x-api,worker=id-x-worker,worker=id-x-verifier`` -> [("api",
    "id-x-api"), ...]. An app may have several logins."""
    out: list[tuple[str, str]] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        app, sep, login = part.partition("=")
        if not sep or not login.strip():
            raise ValueError(f"OMNIFILE_DB_MEMBERS: {part!r} is not app=role")
        out.append((app.strip(), login.strip()))
    return out
