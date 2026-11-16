"""`python -m omnifile_service.admin dead-letters list|resend|drop` on a real catalog."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import psycopg
import pytest
from psycopg.types.json import Jsonb

from omnifile_service.admin import dead_letters

from .conftest import run_cli


def _dead(url: str, queue: str, body: dict, error: str = "boom") -> int:
    with psycopg.connect(url, autocommit=True) as c:
        (msg_id,) = c.execute(
            "SELECT pgmq.send(%s::text, %s::jsonb)",
            (
                f"{queue}_dlq",
                Jsonb({"queue": queue, "msg_id": 7, "read_ct": 5, "failures": 5, "error": error,
                       "traceback": "t", "message": body}),
            ),
        ).fetchone()  # fmt: skip
    return msg_id


def _depth(url: str, table: str) -> int:
    with psycopg.connect(url, autocommit=True) as c:
        (n,) = c.execute(f"SELECT count(*) FROM pgmq.{table}").fetchone()  # noqa: S608
    return n


def _env(url: str) -> dict[str, str]:
    return {"OMNIFILE_DATABASE_URL": url}


def test_list_says_there_are_none(catalog: str) -> None:
    r = run_cli("dead-letters", "list", env=_env(catalog))
    assert (r.code, r.out.strip()) == (0, "no dead letters")


def test_list_shows_what_died_and_why(catalog: str) -> None:
    msg = _dead(catalog, "restores", {"recall_id": "9", "_failures": 5}, "503 from Azure")
    _dead(catalog, "verify", {"location_id": "3"})
    r = run_cli("dead-letters", "list", env=_env(catalog))
    assert r.code == 0
    assert "503 from Azure" in r.out and f"{msg}" in r.out and "restores" in r.out
    assert "verify" in r.out
    one = run_cli("dead-letters", "list", "--queue", "restores", "--json", env=_env(catalog))
    rows = [json.loads(line) for line in one.out.splitlines()]
    assert len(rows) == 1
    assert rows[0]["msg_id"] == str(msg) and rows[0]["message"]["recall_id"] == "9"
    assert rows[0]["gave_up"] is False


def test_resend_puts_it_back_with_all_its_tries_warns_and_audits(catalog: str) -> None:
    msg = _dead(
        catalog, "restores",
        {"recall_id": "9", "_failures": 5, "_last_error": "x", "_redrives": 6,
         "_first_dead_at": "2026-01-01T00:00:00+00:00"},
    )  # fmt: skip
    r = run_cli("dead-letters", "resend", "--queue", "restores", "--msg-id", str(msg),
                env=_env(catalog))  # fmt: skip
    assert r.code == 0, r.err
    assert f"resent 1 message(s): {msg}" in r.out
    # What it means for a message that fails every time.
    assert "warning:" in r.out and "fails every time comes back" in r.out
    with psycopg.connect(catalog, autocommit=True) as c:
        (body,) = c.execute("SELECT message FROM pgmq.q_restores").fetchone()
        assert body == {"recall_id": "9"}
        (actor, details) = c.execute(
            "SELECT actor, details FROM catalog.audit_log WHERE action = 'dead_letter.resend'"
        ).fetchone()
    assert actor == "system:admin"
    assert details["queue"] == "restores" and details["msg_id"] == str(msg)
    assert details["error"] == "boom" and "db_user" in details
    assert _depth(catalog, "q_restores_dlq") == 0
    assert _depth(catalog, "a_restores_dlq") == 1, "kept in the archive"


def test_resend_all_of_one_queue_only(catalog: str) -> None:
    _dead(catalog, "retention", {"op": "delete_blob", "location_id": "1"})
    _dead(catalog, "retention", {"op": "delete_blob", "location_id": "2"})
    _dead(catalog, "verify", {"location_id": "3"})
    r = run_cli("dead-letters", "resend", "--queue", "retention_dlq", "--all", env=_env(catalog))
    assert r.code == 1 and "--yes" in r.err and "2 message(s)" in r.err
    assert _depth(catalog, "q_retention") == 0
    r = run_cli("dead-letters", "resend", "--queue", "retention_dlq", "--all", "--yes",
                env=_env(catalog))  # fmt: skip
    assert r.code == 0, r.err
    assert _depth(catalog, "q_retention") == 2
    assert _depth(catalog, "q_retention_dlq") == 0
    assert _depth(catalog, "q_verify_dlq") == 1


def test_resend_forgets_the_read_errors_and_the_time_throttled(catalog: str) -> None:
    """After a day of read errors, a person's resend gets a day again, not a minute and a half."""
    msg = _dead(
        catalog, "verify",
        {"location_id": "3", "_read_errors": 100, "_throttled_since": "2026-01-01T00:00:00+00:00",
         "_last_try": True},
    )  # fmt: skip
    r = run_cli("dead-letters", "resend", "--queue", "verify", "--msg-id", str(msg),
                env=_env(catalog))  # fmt: skip
    assert r.code == 0, r.err
    with psycopg.connect(catalog, autocommit=True) as c:
        (body,) = c.execute("SELECT message FROM pgmq.q_verify").fetchone()
    assert body == {"location_id": "3"}


def _files(url: str, *, state: str = "committed", verified: bool = False) -> int:
    """One location, on its own file, in the given state."""
    tag = uuid.uuid4().hex[:8]
    with psycopg.connect(url, autocommit=True) as c, c.transaction():
        c.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        (backend,) = c.execute(
            "INSERT INTO catalog.backend (name, kind, config, private_network)"
            " VALUES (%s, 'sim', '{}', true) RETURNING backend_id",
            (f"sim-{tag}",),
        ).fetchone()
        (edge,) = c.execute(
            "INSERT INTO catalog.edge (name, enrolment_token_sha256, enrolment_expires_at)"
            " VALUES (%s, %s, now() + interval '1 day') RETURNING edge_id",
            (f"edge-{tag}", uuid.uuid4().bytes * 2),
        ).fetchone()
        (ds,) = c.execute(
            "INSERT INTO catalog.dataset (name, backend_id, edge_id) VALUES (%s, %s, %s)"
            " RETURNING dataset_id",
            (f"ds-{tag}", backend, edge),
        ).fetchone()
        (root,) = c.execute(
            "INSERT INTO catalog.file (dataset_id, parent_id, name, kind)"
            " VALUES (%s, NULL, '', 'dir') RETURNING file_id",
            (ds,),
        ).fetchone()
        (fid,) = c.execute(
            "INSERT INTO catalog.file (dataset_id, parent_id, name, kind, state, size,"
            " resident_bytes) VALUES (%s, %s, 'a', 'file', 'resident', 1, 1) RETURNING file_id",
            (ds, root),
        ).fetchone()
        (vid,) = c.execute(
            "INSERT INTO catalog.file_version (file_id, dataset_id, seq, parent_id, name, kind,"
            " state, size) VALUES (%s, %s, 1, %s, 'a', 'content', 'pending', 1)"
            " RETURNING version_id",
            (fid, ds, root),
        ).fetchone()
        (lid,) = c.execute(
            "INSERT INTO catalog.location (file_id, version_id, backend_id, container, object_key,"
            " role, state, logical_tier, storage_class, size, crc64nvme, verified, verified_at,"
            " verified_by, committed_at) VALUES (%s, %s, %s, 'd', 'k', 'primary', 'committed',"
            " 'hot', 'Hot', 1, %s, %s, CASE WHEN %s THEN now() END,"
            " CASE WHEN %s THEN 'upload_block_crc' END, now()) RETURNING location_id",
            (fid, vid, backend, b"\0" * 8, verified, verified, verified),
        ).fetchone()
        if state != "committed":
            c.execute(
                "UPDATE catalog.location SET state = %s, verified = false WHERE location_id = %s",
                (state, lid),
            )
    return lid


def test_drop_refuses_a_verify_nothing_would_send_again(catalog: str) -> None:
    lid = _files(catalog)
    msg = _dead(catalog, "verify", {"location_id": str(lid)})
    r = run_cli("dead-letters", "drop", "--queue", "verify", "--msg-id", str(msg),
                env=_env(catalog))  # fmt: skip
    assert r.code == 1
    assert "stays unverified" in r.err and "--force" in r.err
    assert _depth(catalog, "q_verify_dlq") == 1, "nothing changed"
    r = run_cli("dead-letters", "drop", "--queue", "verify", "--msg-id", str(msg), "--force",
                env=_env(catalog))  # fmt: skip
    assert r.code == 0, r.err
    assert f"stranded: message {msg}" in r.out and "stays unverified" in r.out
    with psycopg.connect(catalog, autocommit=True) as c:
        (details,) = c.execute(
            "SELECT details FROM catalog.audit_log WHERE action = 'dead_letter.drop'"
        ).fetchone()
    assert "stays unverified" in details["stranded"]


def test_drop_refuses_a_delete_nothing_would_send_again(catalog: str) -> None:
    lid = _files(catalog, state="deleting")
    msg = _dead(catalog, "retention", {"op": "delete_blob", "location_id": str(lid)})
    r = run_cli("dead-letters", "drop", "--queue", "retention", "--msg-id", str(msg),
                env=_env(catalog))  # fmt: skip
    assert r.code == 1 and "billed" in r.err
    assert _depth(catalog, "q_retention_dlq") == 1


def test_drop_refuses_an_annex_purge_still_wanted(catalog: str, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    wanted: list[object] = []

    class Row:
        def fetchone(self) -> object:
            return wanted.pop() if wanted else None

    letter = dead_letters.Letter(
        "retention", 1, datetime.now(UTC), 5, "x", False,
        {"op": "annex_purge", "annex_id": "1", "file_id": "2"},
    )  # fmt: skip
    with psycopg.connect(catalog, autocommit=True) as c:
        # No such annex row: it isn't wanted, or has run.
        assert dead_letters.stranded(c, letter) is None

    class Conn:
        def execute(self, *_a: object) -> Row:
            return Row()

    wanted.append((1,))
    assert "never runs" in dead_letters.stranded(Conn(), letter)  # type: ignore[arg-type,operator]


def test_drop_lets_go_of_a_letter_whose_work_has_ended(catalog: str) -> None:
    lid = _files(catalog, verified=True)
    msg = _dead(catalog, "verify", {"location_id": str(lid)})
    r = run_cli("dead-letters", "drop", "--queue", "verify", "--msg-id", str(msg),
                env=_env(catalog))  # fmt: skip
    assert r.code == 0, r.err
    assert "stranded" not in r.out


def test_drop_all_needs_yes_and_refuses_the_lot_if_any_would_strand(catalog: str) -> None:
    lid = _files(catalog)
    _dead(catalog, "verify", {"location_id": str(lid)})
    _dead(catalog, "verify", {"location_id": "999999"})
    r = run_cli("dead-letters", "drop", "--queue", "verify", "--all", env=_env(catalog))
    assert r.code == 1 and "--yes" in r.err and "2 message(s)" in r.err
    assert _depth(catalog, "q_verify_dlq") == 2
    r = run_cli("dead-letters", "drop", "--queue", "verify", "--all", "--yes", env=_env(catalog))
    assert r.code == 1 and "stays unverified" in r.err
    assert _depth(catalog, "q_verify_dlq") == 2, "not even the one that would strand nothing"
    r = run_cli("dead-letters", "drop", "--queue", "verify", "--all", "--yes", "--force",
                env=_env(catalog))  # fmt: skip
    assert r.code == 0, r.err
    assert _depth(catalog, "q_verify_dlq") == 0


@pytest.mark.parametrize("queue", ["migrate", "seed", "portal"])
def test_drop_refuses_a_queue_with_nothing_to_send_it_again(catalog: str, queue: str) -> None:
    msg = _dead(catalog, queue, {"x": "1"})
    r = run_cli("dead-letters", "drop", "--queue", queue, "--msg-id", str(msg), env=_env(catalog))
    assert r.code == 1 and "--force" in r.err


def test_drop_runs_nothing_and_is_audited(catalog: str) -> None:
    msg = _dead(catalog, "verify", {"location_id": "3"})
    r = run_cli("dead-letters", "drop", "--queue", "verify", "--msg-id", str(msg),
                env=_env(catalog))  # fmt: skip
    assert r.code == 0, r.err
    assert f"dropped 1 message(s): {msg}" in r.out
    assert _depth(catalog, "q_verify") == 0
    assert _depth(catalog, "q_verify_dlq") == 0
    assert _depth(catalog, "a_verify_dlq") == 1
    with psycopg.connect(catalog, autocommit=True) as c:
        assert c.execute(
            "SELECT count(*) FROM catalog.audit_log WHERE action = 'dead_letter.drop'"
        ).fetchone() == (1,)


@pytest.mark.parametrize(
    ("args", "says"),
    [
        (("--queue", "nonsense", "--all"), "no dead-letter queue"),
        (("--queue", "restores"), "--msg-id"),
        (("--queue", "restores", "--all", "--msg-id", "1"), "not both"),
        (("--queue", "restores", "--msg-id", "12345"), "no message"),
    ],
)
def test_a_bad_request_changes_nothing(catalog: str, args: tuple[str, ...], says: str) -> None:
    keep = _dead(catalog, "restores", {"recall_id": "9"})
    r = run_cli("dead-letters", "resend", *args, env=_env(catalog))
    assert r.code == 1 and says in r.err
    assert keep and _depth(catalog, "q_restores_dlq") == 1
    assert _depth(catalog, "q_restores") == 0


@pytest.mark.parametrize(
    ("queue", "body"),
    [
        ("verify", {"location_id": "abc"}),
        ("verify", {"location_id": "99999999999999999999999"}),
        ("verify", {}),
        ("retention", {"op": "delete_blob", "location_id": "abc"}),
        ("retention", {"op": "annex_purge", "annex_id": "abc", "file_id": "1"}),
    ],
)
def test_a_malformed_letter_can_be_listed_and_dropped(catalog: str, queue: str, body: dict) -> None:
    """It names nothing that can exist, so dropping it strands nothing. Before this it raised
    InvalidTextRepresentation, even with --force, and only psql could remove it."""
    msg = _dead(catalog, queue, body)
    assert run_cli("dead-letters", "list", "--queue", queue, env=_env(catalog)).code == 0
    r = run_cli("dead-letters", "drop", "--queue", queue, "--msg-id", str(msg), env=_env(catalog))
    assert r.code == 0, r.err
    assert "stranded" not in r.out
    assert _depth(catalog, f"q_{queue}_dlq") == 0


def test_a_letter_that_isnt_even_an_object_can_be_dropped(catalog: str) -> None:
    with psycopg.connect(catalog, autocommit=True) as c:
        (msg,) = c.execute(
            "SELECT pgmq.send('verify_dlq'::text, %s::jsonb)", (Jsonb(["not", "an", "object"]),)
        ).fetchone()
        (msg2,) = c.execute(
            "SELECT pgmq.send('verify_dlq'::text, %s::jsonb)",
            (Jsonb({"failures": "many", "message": "not an object"}),),
        ).fetchone()
    assert run_cli("dead-letters", "list", "--json", env=_env(catalog)).code == 0
    for m in (msg, msg2):
        r = run_cli("dead-letters", "drop", "--queue", "verify", "--msg-id", str(m),
                    env=_env(catalog))  # fmt: skip
        assert r.code == 0, r.err


def test_drop_refuses_a_read_back_a_nas_removal_is_waiting_on(catalog: str) -> None:
    """The copy is verified, but this read is what the removal waits for."""
    lid = _files(catalog, verified=True)
    msg = _dead(catalog, "verify", {"location_id": str(lid), "purpose": "removal"})
    r = run_cli("dead-letters", "drop", "--queue", "verify", "--msg-id", str(msg),
                env=_env(catalog))  # fmt: skip
    assert r.code == 1 and "NAS removal" in r.err
    assert _depth(catalog, "q_verify_dlq") == 1
