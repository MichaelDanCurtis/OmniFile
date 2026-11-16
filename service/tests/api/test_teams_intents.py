"""`POST /internal/v1/teams/intents` (teams.md section 4; control-api.md section 13).

Direct calls with the relay's certificate check every step and refusal, against the spec.
The last test drives real clicks through the relay's own code (relay/src, unchanged), with a
test Bot Framework JWKS, into `api`, and the worker's cards through the fake Bot Connector.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import psycopg
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from notify.support import (
    BOT_APP_ID,
    KID,
    PORTAL,
    TENANT,
    Person,
    Scenario,
    add_person,
    audits,
    graph_user,
    make_fakes,
    relay_modules,
    seed_scenario,
    set_state,
    start_recall,
)
from starlette.testclient import TestClient

from omnifile_service.api.app import create_app
from omnifile_service.api.clientauth import EdgeAuthenticator, ForwardedClientCert
from omnifile_service.api.context import ApiSettings, Services
from omnifile_service.api.dataplane import DataPlanes
from omnifile_service.api.db import Catalog
from omnifile_service.api.teams import PATH, TeamsSettings
from omnifile_service.catalog.privileges import as_app
from omnifile_service.enrol.ca import FileCASigner
from omnifile_service.notify.fakes import FakeTeams
from omnifile_service.notify.intents import IntentSigner, IntentVerifier, Minted, public_key_b64
from omnifile_service.worker.notify_jobs import build_jobs
from omnifile_service.worker.queue import Consumer

from . import spec
from .conftest import INGRESS_CIDR, INGRESS_PEER, Api, xfcc

KEY = Ed25519PrivateKey.generate()


def relay_cert(ca: FileCASigner, san: str = "urn:omnifile:relay") -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "omnifile-relay")]))
        .issuer_name(ca.ca_certificate.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        .add_extension(
            x509.SubjectAlternativeName([x509.UniformResourceIdentifier(san)]), critical=False
        )
    )
    cert = asyncio.run(ca.sign(builder))
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def teams_settings() -> TeamsSettings:
    return TeamsSettings(
        tenant_id=TENANT, verifier=IntentVerifier({KID: KEY.public_key()}), portal_url=PORTAL
    )


@pytest.fixture
def relay_pem(ca: FileCASigner) -> str:
    return relay_cert(ca)


@pytest.fixture
def teams_api(api: Api) -> Api:
    api.client.app.state.teams = teams_settings()
    return api


@dataclass
class Card:
    recall_id: int
    notification_id: int
    intents: dict[str, Minted]


def card(
    sql: psycopg.Connection,
    s: Scenario,
    p: Person,
    actions: tuple[str, ...],
    *,
    state: str = "failed",
    audience: str = "requester",
    now: datetime | None = None,
    signer: IntentSigner | None = None,
) -> Card:
    """A recall in `state` with a card sent to `p`, carrying one intent per action."""
    rid = start_recall(sql, s, p)
    set_state(sql, rid, state)
    (nid,) = sql.execute(
        "INSERT INTO catalog.notification (recall_id, identity_id, channel, audience, state,"
        " teams_conversation_id, teams_activity_id, card_version)"
        " VALUES (%s, %s, 'teams', %s, 'sent', '19:conv', '1:act', 1) RETURNING notification_id",
        (rid, p.identity_id, audience),
    ).fetchone()
    signer = signer or IntentSigner(KEY, KID)
    out = {}
    for action in actions:
        m = signer.mint(
            action=action,
            file_id=s.file_id,
            recipient=p.oid,
            tenant=TENANT,
            now=now or datetime.now(UTC),
        )
        sql.execute(
            "INSERT INTO catalog.teams_intent (intent_id, notification_id, action, file_id,"
            " version_id, recipient_identity_id, issued_at, expires_at, card_version)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 1)",
            (
                m.intent_id,
                nid,
                action,
                s.file_id,
                s.version_id,
                p.identity_id,
                m.issued_at,
                m.expires_at,
            ),
        )
        out[action] = m
    return Card(rid, nid, out)


def body(m: Minted, action: str, clicker: str, **kw: Any) -> dict[str, Any]:
    stamp = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    out = {
        "intent": m.token,
        "intent_id": m.intent_id,
        "action": action,
        "clicker_aad_object_id": clicker,
        "tenant_id": TENANT,
        "activity_time": stamp,
        "received_at": stamp,
    }
    out.update(kw)
    return out


def post(api: Api, cert_pem: str, payload: dict, key: str | None = None) -> httpx.Response:
    spec.check_request("POST", PATH, payload)
    resp = api.client.post(
        PATH,
        json=payload,
        headers={
            "X-Forwarded-Client-Cert": xfcc(cert_pem),
            "Idempotency-Key": key or payload["intent_id"],
        },
    )
    spec.check_response(
        "POST", PATH, resp.status_code, resp.headers.get("content-type", ""), resp.content
    )
    assert resp.status_code != 401  # the relay would treat 401 as transient
    return resp


def scene(sql, api: Api) -> tuple[Scenario, Person]:
    s = seed_scenario(sql, backend_id=api.backend_id)
    p = add_person(sql, groups=[s.readers_sid])
    return s, p


def queue(sql, name: str) -> list[dict]:
    return [r[0] for r in sql.execute(f"SELECT message FROM pgmq.q_{name} ORDER BY msg_id")]


def no_execute(c: dict) -> bool:
    return all(a["type"] != "Action.Execute" for a in c.get("actions", []))


# --- accepted clicks -------------------------------------------------------------------


def test_recall_on_a_failed_card_queues_a_new_recall(teams_api, sql, relay_pem) -> None:
    s, p = scene(sql, teams_api)
    c = card(sql, s, p, ("recall",))
    for q in ("notify", "restores"):
        sql.execute(f"DELETE FROM pgmq.q_{q}")
    b = body(c.intents["recall"], "recall", p.oid)
    resp = post(teams_api, relay_pem, b)
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["result"] == "queued" and int(out["recall_id"]) != c.recall_id
    new = int(out["recall_id"])
    assert no_execute(out["card"]) and out["card"]["body"][0]["text"].startswith("Recalling")
    assert sql.execute(
        "SELECT state FROM catalog.recall WHERE recall_id = %s", (new,)
    ).fetchone() == ("requested",)
    assert sql.execute(
        "SELECT via, proof FROM catalog.recall_requester WHERE recall_id = %s AND identity_id = %s",
        (new, p.identity_id),
    ).fetchone() == ("teams", "proven")
    assert queue(sql, "restores") == [{"recall_id": str(new)}]
    (msg, delay) = sql.execute(
        "SELECT message, vt > now() + interval '4 seconds' FROM pgmq.q_notify"
    ).fetchone()
    assert msg == {"recall_id": str(new), "reason": "click"} and delay
    # The clicked card follows the new recall: same activity, old buttons revoked.
    assert sql.execute(
        "SELECT teams_activity_id FROM catalog.notification WHERE recall_id = %s", (new,)
    ).fetchone() == ("1:act",)
    accepted = audits(sql, "teams.intent.accepted", s.file_id)
    assert accepted[0]["actor"] == f"user:{p.identity_id}"
    assert accepted[0]["details"]["recall_id"] == str(new)
    assert any(a["details"].get("via") == "teams" for a in audits(sql, "recall.request", s.file_id))

    # A second relay replica forwards the same click: the stored reply, not a refusal.
    later = dict(b, received_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"))
    again = post(teams_api, relay_pem, later)
    assert again.status_code == 200 and again.headers.get("Idempotent-Replayed") == "true"
    assert again.json() == out
    # A different intent under the same key: its jti differs, so step 3 refuses it.
    set_state(sql, new, "available")
    other = card(sql, s, p, ("recall",)).intents["recall"]
    reused = post(teams_api, relay_pem, body(other, "recall", p.oid, intent_id=b["intent_id"]))
    assert reused.status_code == 400 and reused.json()["code"] == "intent_mismatch"
    # A key that isn't the intent_id is a bad request.
    wrong_key = post(teams_api, relay_pem, body(other, "recall", p.oid), key=str(uuid.uuid4()))
    assert wrong_key.status_code == 400
    # Once the stored reply is gone (24 h), the intent itself refuses a repeat.
    sql.execute("DELETE FROM catalog.idempotency_key WHERE principal = 'relay'")
    repeat = post(teams_api, relay_pem, b)
    assert repeat.status_code == 409 and repeat.json()["code"] == "intent_already_used"


def test_keep_online_sets_and_extends_a_hold(teams_api, sql, relay_pem) -> None:
    s, p = scene(sql, teams_api)
    c = card(sql, s, p, ("keep_online",), state="rehydrating")
    resp = post(teams_api, relay_pem, body(c.intents["keep_online"], "keep_online", p.oid))
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"] == "done" and "recall_id" not in resp.json()
    assert "Kept online until" in json.dumps(resp.json()["card"])
    rows = sql.execute(
        "SELECT hold_id, reason, until - now() FROM catalog.hold"
        " WHERE file_id = %s AND kind = 'keep_online' AND set_by_identity_id = %s",
        (s.file_id, p.identity_id),
    ).fetchall()
    assert len(rows) == 1 and rows[0][1] == "Teams"
    assert timedelta(days=13, hours=23) < rows[0][2] <= timedelta(days=14)
    assert audits(sql, "hold.set", s.file_id)[0]["actor"] == f"user:{p.identity_id}"
    # A shorter request never shortens it; a later one extends the same hold.
    with sql.transaction():
        sql.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        sql.execute(
            "UPDATE catalog.hold SET until = now() + interval '1 day' WHERE hold_id = %s",
            (rows[0][0],),
        )
    set_state(sql, c.recall_id, "available")
    c2 = card(sql, s, p, ("keep_online",), state="available")
    assert (
        post(
            teams_api, relay_pem, body(c2.intents["keep_online"], "keep_online", p.oid)
        ).status_code
        == 200
    )
    (count, left) = sql.execute(
        "SELECT count(*), max(until) - now() FROM catalog.hold"
        " WHERE file_id = %s AND kind = 'keep_online'",
        (s.file_id,),
    ).fetchone()
    assert count == 1 and left > timedelta(days=13)


def test_track_and_track_on_a_finished_recall(teams_api, sql, relay_pem) -> None:
    s, p = scene(sql, teams_api)
    c = card(sql, s, p, ("track",), state="rehydrating", audience="owner")
    resp = post(teams_api, relay_pem, body(c.intents["track"], "track", p.oid))
    assert resp.status_code == 200 and resp.json()["result"] == "done"
    assert "You're tracking this recall." in json.dumps(resp.json()["card"])
    assert sql.execute(
        "SELECT tracking FROM catalog.notification WHERE notification_id = %s",
        (c.notification_id,),
    ).fetchone() == (True,)
    set_state(sql, c.recall_id, "available")
    done = card(sql, s, p, ("track",), state="available", audience="owner")
    late = post(teams_api, relay_pem, body(done.intents["track"], "track", p.oid))
    assert late.status_code == 422 and late.json()["code"] == "action_not_allowed"


# --- refusals ------------------------------------------------------------------------------


def test_refusals_and_their_audit(teams_api, sql, relay_pem, ca) -> None:
    s, p = scene(sql, teams_api)
    other = add_person(sql, groups=[s.readers_sid])

    def refused(payload: dict, status: int, code: str) -> None:
        resp = post(teams_api, relay_pem, payload)
        assert (resp.status_code, resp.json()["code"]) == (status, code), resp.text

    # Clicked by someone else (the relay checks this too; the service doesn't trust it).
    c = card(sql, s, p, ("recall",))
    refused(body(c.intents["recall"], "recall", other.oid), 403, "not_intended_recipient")
    # Expired.
    old = card(sql, s, p, ("recall",), now=datetime.now(UTC) - timedelta(days=8))
    refused(body(old.intents["recall"], "recall", p.oid), 410, "intent_expired")
    # Signed by a key we don't know (a forged card).
    forged = card(sql, s, p, ("recall",), signer=IntentSigner(Ed25519PrivateKey.generate(), KID))
    refused(body(forged.intents["recall"], "recall", p.oid), 403, "intent_invalid")
    # The body says another action than the intent.
    c2 = card(sql, s, p, ("recall",))
    refused(body(c2.intents["recall"], "track", p.oid), 400, "intent_mismatch")
    # A card replaced by a newer one.
    c3 = card(sql, s, p, ("recall",))
    sql.execute(
        "UPDATE catalog.teams_intent SET revoked_at = now() WHERE notification_id = %s",
        (c3.notification_id,),
    )
    refused(body(c3.intents["recall"], "recall", p.oid), 403, "intent_invalid")
    # A click too far from our clock.
    c4 = card(sql, s, p, ("recall",))
    stale = (datetime.now(UTC) - timedelta(minutes=20)).isoformat().replace("+00:00", "Z")
    refused(
        body(c4.intents["recall"], "recall", p.oid, activity_time=stale), 400, "invalid_request"
    )
    # The clicker lost read access since the card was sent.
    lost = add_person(sql, groups=[s.readers_sid])
    c5 = card(sql, s, lost, ("recall",))
    sql.execute(
        "UPDATE catalog.identity SET group_sids = '{}' WHERE identity_id = %s",
        (lost.identity_id,),
    )
    refused(body(c5.intents["recall"], "recall", lost.oid), 403, "forbidden")
    # ... and the intent was not consumed by the refusal.
    assert sql.execute(
        "SELECT used_at FROM catalog.teams_intent WHERE notification_id = %s",
        (c5.notification_id,),
    ).fetchone() == (None,)

    ids = [x.intents["recall"].intent_id for x in (c, old, forged, c2, c3, c4, c5)]
    reasons = [
        r[0]
        for r in sql.execute(
            "SELECT details->>'reason' FROM catalog.audit_log"
            " WHERE action = 'teams.intent.refused' AND details->>'intent_id' = ANY(%s)",
            (ids,),
        ).fetchall()
    ]
    assert sorted(reasons) == sorted(
        [
            "not_intended_recipient",
            "intent_expired",
            "intent_invalid",
            "intent_mismatch",
            "intent_invalid",
            "invalid_request",
            "forbidden",
        ]
    )
    lost_audit = [
        a
        for a in audits(sql, "teams.intent.refused", s.file_id)
        if a["details"]["reason"] == "forbidden"
    ]
    assert lost_audit[0]["actor"] == f"user:{lost.identity_id}"
    assert lost_audit[0]["decision"] == "deny"


def test_a_click_on_stale_groups_refreshes_them_and_acts_on_fresh_ones(
    teams_api, sql, relay_pem
) -> None:
    """Codex P1 on #8: a card stays clickable for 7 days, so step 8 must not act on groups
    cached for days. Groups older than 1 h are refreshed and the click is answered 503
    (transient: the relay releases it); the next click uses the refreshed groups."""
    s, p = scene(sql, teams_api)
    c = card(sql, s, p, ("recall",))
    sql.execute(
        "UPDATE catalog.identity SET groups_resolved_at = now() - interval '61 minutes'"
        " WHERE identity_id = %s",
        (p.identity_id,),
    )
    payload = body(c.intents["recall"], "recall", p.oid)
    resp = post(teams_api, relay_pem, payload)
    assert (resp.status_code, resp.json()["code"]) == (503, "unavailable"), resp.text
    assert {"identity_id": str(p.identity_id)} in queue(sql, "identity_resolve")
    assert sql.execute(
        "SELECT used_at FROM catalog.teams_intent WHERE intent_id = %s",
        (c.intents["recall"].intent_id,),
    ).fetchone() == (None,)
    assert sql.execute(
        "SELECT count(*) FROM catalog.recall WHERE file_id = %s AND recall_id <> %s",
        (s.file_id, c.recall_id),
    ).fetchone() == (0,)
    # The worker's refresh finds the user removed from the readers group.
    sql.execute(
        "UPDATE catalog.identity SET group_sids = '{}', groups_resolved_at = now()"
        " WHERE identity_id = %s",
        (p.identity_id,),
    )
    resp = post(teams_api, relay_pem, payload)
    assert (resp.status_code, resp.json()["code"]) == (403, "forbidden"), resp.text


def test_only_the_relay_certificate_may_call(teams_api, sql, relay_pem, ca, enrol_edge) -> None:
    s, p = scene(sql, teams_api)
    c = card(sql, s, p, ("recall",))
    edge = enrol_edge()
    as_edge = post(teams_api, edge.cert_pem, body(c.intents["recall"], "recall", p.oid))
    assert as_edge.status_code == 403 and as_edge.json()["code"] == "caller_not_allowed"
    stranger = relay_cert(ca, san="urn:omnifile:something-else")
    resp = post(teams_api, stranger, body(c.intents["recall"], "recall", p.oid))
    assert resp.status_code == 403 and resp.json()["code"] == "caller_not_allowed"
    # The relay's certificate on an edge path.
    hb = teams_api.client.post(
        f"/v0/edges/{edge.edge_id}/heartbeat",
        json={},
        headers={"X-Forwarded-Client-Cert": xfcc(relay_pem), "Idempotency-Key": str(uuid.uuid4())},
    )
    assert hb.status_code == 403 and hb.json()["code"] == "caller_not_allowed"
    # Not yet configured: 503, which the relay treats as transient.
    teams_api.client.app.state.teams = None
    import os

    if not os.environ.get("OMNIFILE_TEAMS_INTENT_PUBLIC_KEYS"):
        unconfigured = post(teams_api, relay_pem, body(c.intents["recall"], "recall", p.oid))
        assert unconfigured.status_code == 503


# --- through the relay's real code ----------------------------------------------------------


class _Sink:
    """The relay's HttpsMtlsSink, plus closing the second api's catalog pool."""

    def __init__(self, inner, catalog: Catalog) -> None:
        self.inner, self.catalog = inner, catalog

    async def submit(self, forward):
        return await self.inner.submit(forward)

    async def aclose(self) -> None:
        await self.inner.aclose()
        await self.catalog.close()


class Clock:
    def __init__(self) -> None:
        self.t = datetime.now(UTC)

    def now(self) -> datetime:
        return self.t


def test_clicks_through_the_real_relay(db_url, ca, api, sql, relay_pem, tmp_path) -> None:
    app_mod, config, sink_mod = relay_modules()
    fakes = make_fakes(tmp_path)
    clock = Clock()
    s = seed_scenario(sql, backend_id=api.backend_id)
    p = add_person(sql, groups=[s.readers_sid], resolved=False)
    graph_user(fakes, p, groups=[s.readers_sid])
    for q in ("notify", "restores"):
        sql.execute(f"DELETE FROM pgmq.q_{q}")

    async def connect() -> psycopg.AsyncConnection:
        return await psycopg.AsyncConnection.connect(as_app(db_url, "worker"), autocommit=True)

    notify_job = next(j for j in build_jobs(fakes.notifier, clock) if j.queue == "notify")

    def worker(advance: float = 11) -> None:
        clock.t += timedelta(seconds=advance)
        sql.execute("UPDATE pgmq.q_notify SET vt = now()")  # skip the 6 s delay

        async def go():
            consumer = Consumer(connect, "notify", notify_job.handler)
            await consumer.drain()
            await consumer.close()

        asyncio.run(go())

    # The user opens the stub; the worker sends the card.
    rid = start_recall(sql, s, p)
    sql.execute("SELECT pgmq.send('notify', %s::jsonb)", (json.dumps({"recall_id": str(rid)}),))
    worker(0)
    (conv,) = sql.execute(
        "SELECT teams_conversation_id FROM catalog.notification WHERE recall_id = %s", (rid,)
    ).fetchone()
    first = fakes.bot.card(conv)
    assert [a["title"] for a in first["actions"]] == ["Keep online", "Open in portal"]

    def relay(teams: FakeTeams) -> TestClient:
        """The relay's real app, forwarding to its own api app on the same catalog over
        "mTLS" (the relay certificate in X-Forwarded-Client-Cert, from the ingress range)."""
        catalog = Catalog(as_app(db_url, "api"), max_size=2)
        services = Services(
            catalog=catalog,
            auth=EdgeAuthenticator(
                ForwardedClientCert(trust_ingress=True, trusted_proxies=(INGRESS_CIDR,)),
                ca.ca_certificate,
                catalog,
            ),
            planes=DataPlanes(),
            signer=None,
            settings=ApiSettings(),
        )
        api_app = create_app(services, manage_lifecycle=False)
        api_app.state.teams = TeamsSettings(
            tenant_id=TENANT,
            verifier=IntentVerifier({KID: fakes.key.public_key()}),
            portal_url=PORTAL,
        )
        pub = json.loads(fakes.public_keys_json)[KID]
        settings = config.Settings(
            bot_app_id=BOT_APP_ID,
            tenant_id=TENANT,
            intent_public_keys={KID: config.load_public_key(pub)},
        )
        inner = sink_mod.HttpsMtlsSink.with_client(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=api_app, client=INGRESS_PEER),
                base_url="https://api.test",
                headers={"X-Forwarded-Client-Cert": xfcc(relay_pem)},
            )
        )
        app = app_mod.create_app(
            settings,
            sink=_Sink(inner, catalog),
            http_client=httpx.AsyncClient(transport=teams.transport()),
        )
        return TestClient(app, client=("52.112.0.10", 443))

    teams = FakeTeams(bot_app_id=BOT_APP_ID, tenant_id=TENANT, bot=fakes.bot)
    with relay(teams) as rc:
        teams.post = lambda path, raw, headers: rc.post(path, content=raw, headers=headers)

        # Someone else clicking the card is stopped at the relay.
        other = teams.click(conv, "Keep online", clicker=str(uuid.uuid4()))
        assert other.json()["statusCode"] == 403

        # Keep online: the reply card has no buttons to press; the worker's update follows.
        reply = teams.click(conv, "Keep online", clicker=p.oid).json()
        assert reply["statusCode"] == 200
        assert reply["type"] == "application/vnd.microsoft.card.adaptive"
        assert no_execute(reply["value"]) and "Kept online until" in json.dumps(reply["value"])
        assert sql.execute(
            "SELECT count(*) FROM catalog.hold WHERE file_id = %s AND kind = 'keep_online'",
            (s.file_id,),
        ).fetchone() == (1,)
        worker()
        updated = fakes.bot.card(conv)
        assert [a["title"] for a in updated["actions"]] == ["Open in portal"]
        assert "Kept online until" in json.dumps(updated)

        # The old card's button, clicked again: refused at the relay (replay store) ...
        again = teams.click(conv, "Keep online", clicker=p.oid, card=first).json()
        assert again["type"] == "application/vnd.microsoft.activity.message"

        # The restore fails; the card offers Recall.
        set_state(sql, rid, "failed")
        sql.execute("SELECT pgmq.send('notify', %s::jsonb)", (json.dumps({"recall_id": str(rid)}),))
        worker()
        failed_card = fakes.bot.card(conv)
        assert failed_card["body"][0]["text"].startswith("Couldn't recall")
        reply = teams.click(conv, "Recall", clicker=p.oid).json()
        assert reply["statusCode"] == 200
        assert reply["value"]["body"][0]["text"].startswith("Recalling")
        (new_rid,) = sql.execute(
            "SELECT recall_id FROM catalog.recall WHERE file_id = %s AND state = 'requested'",
            (s.file_id,),
        ).fetchone()
        assert new_rid != rid
        worker()
        # The same card now follows the new recall to available.
        assert len(fakes.bot.activities(conv)) == 1
        assert fakes.bot.card(conv)["body"][0]["text"].startswith("Recalling")
        for state in ("rehydrating", "downloading", "available"):
            set_state(sql, new_rid, state)
            sql.execute(
                "SELECT pgmq.send('notify', %s::jsonb)",
                (json.dumps({"recall_id": str(new_rid)}),),
            )
            worker()
        assert fakes.bot.card(conv)["body"][0]["text"].endswith("is ready")

    # A replay of the Recall click via a second relay replica (its own replay store) gets
    # the stored first reply from api, not a second recall.
    with relay(teams) as rc2:
        teams.post = lambda path, raw, headers: rc2.post(path, content=raw, headers=headers)
        twice = teams.click(conv, "Recall", clicker=p.oid, card=failed_card).json()
        assert twice["statusCode"] == 200
    assert sql.execute(
        "SELECT count(*) FROM catalog.recall WHERE file_id = %s", (s.file_id,)
    ).fetchone() == (2,)
    asyncio.run(fakes.http.aclose())


# --- review regressions ----------------------------------------------------------------------


def fingerprint(cert_pem: str) -> str:
    cert = x509.load_pem_x509_certificate(cert_pem.encode())
    return cert.fingerprint(hashes.SHA256()).hex()


def test_a_stored_reply_never_reaches_another_clicker(teams_api, sql, relay_pem) -> None:
    s, p = scene(sql, teams_api)
    c = card(sql, s, p, ("track",), state="rehydrating", audience="owner")
    b = body(c.intents["track"], "track", p.oid)
    assert post(teams_api, relay_pem, b).status_code == 200
    stranger = post(teams_api, relay_pem, dict(b, clicker_aad_object_id=str(uuid.uuid4())))
    assert stranger.status_code == 403
    assert stranger.json()["code"] == "not_intended_recipient"
    assert stranger.headers.get("Idempotent-Replayed") is None and "card" not in stranger.json()


def test_a_strangers_or_stale_click_does_not_block_the_recipient(teams_api, sql, relay_pem) -> None:
    s, p = scene(sql, teams_api)
    c = card(sql, s, p, ("track",), state="rehydrating", audience="owner")
    b = body(c.intents["track"], "track", p.oid)
    stranger = post(teams_api, relay_pem, dict(b, clicker_aad_object_id=str(uuid.uuid4())))
    assert stranger.json()["code"] == "not_intended_recipient"
    stale = (datetime.now(UTC) - timedelta(minutes=20)).isoformat().replace("+00:00", "Z")
    skewed = post(teams_api, relay_pem, dict(b, activity_time=stale))
    assert skewed.status_code == 400
    good = post(teams_api, relay_pem, b)
    assert good.status_code == 200, good.text
    assert good.json()["result"] == "done"


def test_recall_is_refused_when_the_file_is_online(teams_api, sql, relay_pem) -> None:
    s, p = scene(sql, teams_api)
    c = card(sql, s, p, ("recall",), state="available")
    sql.execute(
        "UPDATE catalog.location SET logical_tier = 'hot', storage_class = 'Hot'"
        " WHERE version_id = %s",
        (s.version_id,),
    )
    sql.execute("DELETE FROM pgmq.q_restores")
    resp = post(teams_api, relay_pem, body(c.intents["recall"], "recall", p.oid))
    assert resp.status_code == 422 and resp.json()["code"] == "action_not_allowed"
    assert sql.execute(
        "SELECT count(*) FROM catalog.recall WHERE file_id = %s", (s.file_id,)
    ).fetchone() == (1,)
    assert queue(sql, "restores") == []
    assert sql.execute(
        "SELECT used_at FROM catalog.teams_intent WHERE notification_id = %s",
        (c.notification_id,),
    ).fetchone() == (None,)


def test_missing_or_foreign_certificates_are_403_never_401(teams_api, sql) -> None:
    s, p = scene(sql, teams_api)
    c = card(sql, s, p, ("track",), state="rehydrating", audience="owner")
    b = body(c.intents["track"], "track", p.oid)
    bare = teams_api.client.post(PATH, json=b, headers={"Idempotency-Key": b["intent_id"]})
    assert bare.status_code == 403 and bare.json()["code"] == "caller_not_allowed"
    foreign = relay_cert(FileCASigner.generate("not the OmniFile CA"))
    resp = post(teams_api, foreign, b)
    assert resp.status_code == 403 and resp.json()["code"] == "caller_not_allowed"


def test_keep_online_is_fixed_at_14_days() -> None:
    from omnifile_service.api import teams as teams_mod

    env = {
        "OMNIFILE_TENANT_ID": TENANT,
        "OMNIFILE_TEAMS_INTENT_PUBLIC_KEYS": json.dumps({KID: public_key_b64(KEY)}),
        "OMNIFILE_TEAMS_KEEP_ONLINE_DAYS": "90",
        "OMNIFILE_DEV_MODE": "1",
    }
    settings = TeamsSettings.from_env(env)
    assert settings is not None and not hasattr(settings, "keep_online_days")
    assert teams_mod.KEEP_ONLINE_DAYS == 14


def test_relay_certificates_are_pinned(teams_api, sql, relay_pem, ca) -> None:
    env = {
        "OMNIFILE_TENANT_ID": TENANT,
        "OMNIFILE_TEAMS_INTENT_PUBLIC_KEYS": json.dumps({KID: public_key_b64(KEY)}),
    }
    with pytest.raises(ValueError, match="OMNIFILE_TEAMS_RELAY_CERT_SHA256"):
        TeamsSettings.from_env(env)
    pinned = TeamsSettings.from_env(
        {**env, "OMNIFILE_TEAMS_RELAY_CERT_SHA256": fingerprint(relay_pem)}
    )
    assert pinned is not None and pinned.relay_cert_sha256 == {fingerprint(relay_pem)}
    teams_api.client.app.state.teams = pinned
    s, p = scene(sql, teams_api)
    c = card(sql, s, p, ("track",), state="rehydrating", audience="owner")
    b = body(c.intents["track"], "track", p.oid)
    unpinned = post(teams_api, relay_cert(ca), b)  # same CA and SAN, not pinned
    assert unpinned.status_code == 403 and unpinned.json()["code"] == "caller_not_allowed"
    assert post(teams_api, relay_pem, b).status_code == 200


def test_intent_times_follow_the_wall_clock_not_the_service_clock(
    teams_api, sql, relay_pem
) -> None:
    """Clicks are judged on `Services.wall_clock` (teams.md section 2): a dev virtual clock
    days ahead doesn't expire them, and a wall clock past `exp` does."""
    svc = teams_api.client.app.state.svc
    saved = svc.clock, svc.wall_clock
    s, p = scene(sql, teams_api)
    try:
        ahead = datetime.now(UTC) + timedelta(days=30)
        svc.clock = lambda: ahead  # the dev virtual clock, a month ahead
        c = card(sql, s, p, ("keep_online",))
        ok = post(teams_api, relay_pem, body(c.intents["keep_online"], "keep_online", p.oid))
        assert ok.status_code == 200, ok.text
        svc.clock = saved[0]
        later = datetime.now(UTC) + timedelta(days=8)  # past the intent's 7 days
        svc.wall_clock = lambda: later
        c2 = card(sql, s, p, ("keep_online",))
        stamp = later.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        late = post(
            teams_api,
            relay_pem,
            body(
                c2.intents["keep_online"],
                "keep_online",
                p.oid,
                activity_time=stamp,
                received_at=stamp,
            ),
        )
        assert (late.status_code, late.json()["code"]) == (410, "intent_expired"), late.text
    finally:
        svc.clock, svc.wall_clock = saved
