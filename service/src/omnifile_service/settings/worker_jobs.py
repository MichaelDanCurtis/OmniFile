"""The worker's side of Settings' test sends (`tests.py`): the ``settings_test`` queue.

- ``teams_card``: through the notify path (`notify.delivery.Notifier`): the
  bot's conversation with the person (installing the app for them if needed,
  as for a recall's card), then a plain card saying the test worked.
- ``email``: Graph ``sendMail`` from the configured sender to the person's
  mail address, as the fallback email goes.
- ``aws_assume_role``: STS ``AssumeRoleWithWebIdentity`` for the pasted role
  with the worker's own web-identity token (decision 43), through the account's
  configured STS endpoint. Only the role and the expiry are recorded, never
  the credentials. The account must be in the AWS settings, so its endpoints
  and private networking apply.

Each test ends `passed` or `failed` with a plain detail; a test is never
retried (the person presses the button again).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ..worker.jobs import Job, QueueJob
from ..worker.queue import Message
from .aws import ROLE_ARN
from .tests import QUEUE

log = logging.getLogger("omnifile_service.settings")

CARD = {
    "type": "AdaptiveCard",
    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
    "version": "1.4",
    "body": [
        {"type": "TextBlock", "text": "OmniFile test card", "weight": "Bolder", "size": "Medium"},
        {
            "type": "TextBlock",
            "wrap": True,
            "text": "This card came from Settings, Teams. Recall cards will reach you here.",
        },
    ],
}
SUBJECT = "OmniFile test email"
BODY = (
    "<p>This email came from OmniFile's Settings, Email fallback.</p>"
    "<p>When Teams can't reach you, recall notices come this way.</p>"
)

#: (role_arn, account_id) -> {"expires_at": ...}; raises on refusal.
Assume = Callable[[str, str], Awaitable[dict[str, Any]]]


def assume_from_env(env=None) -> Assume:
    """AssumeRoleWithWebIdentity with the worker's identity, per the AWS settings."""

    async def assume(role_arn: str, account_id: str) -> dict[str, Any]:
        from dataclasses import replace

        from aiobotocore.session import AioSession

        from ..backends.s3.config import AwsDeployment, S3Config
        from ..backends.s3.credentials import (
            WebIdentityCredentials,
            entra_token_provider,
        )

        dep = AwsDeployment.from_env(env)
        acct = dep.accounts.get(account_id)
        if acct is None:
            raise LookupError(
                f"account {account_id} is not in the service's AWS settings"
                " (OMNIFILE_AWS_ACCOUNTS): add it first, so its endpoints apply"
            )
        config = S3Config.for_backend(
            {"account_id": account_id, "region": acct.region}, deployment=dep
        )
        if config.web_identity_audience is None:
            raise LookupError("the AWS settings have no web-identity audience")
        config = replace(config, role_arn=role_arn, credentials="web_identity")
        token = entra_token_provider(None, config.web_identity_audience)
        got = await WebIdentityCredentials(
            AioSession(), config, token, session_name="omnifile-settings-test"
        ).fetch()
        return {"expires_at": got["expiry_time"]}

    return assume


@dataclass
class SettingsTests:
    #: The worker's notifier (None: Teams and Graph aren't configured on the worker).
    notifier: Any
    assume: Assume

    async def handle(self, msg: Message, conn: psycopg.AsyncConnection, now: datetime) -> None:
        cur = conn.cursor(row_factory=dict_row)
        try:
            test_id = int(msg.body["test_id"])
        except (KeyError, TypeError, ValueError):
            return
        t = await (
            await cur.execute(
                "SELECT t.*, i.entra_object_id, i.mail FROM catalog.settings_test t"
                "  JOIN catalog.identity i USING (identity_id)"
                " WHERE t.test_id = %s FOR UPDATE OF t",
                (test_id,),
            )
        ).fetchone()
        if t is None or t["state"] != "pending":
            return
        try:
            detail, result = await self._run(conn, t, now)
            state = "passed"
        except _Failed as e:
            detail, result, state = str(e), {}, "failed"
        except Exception as e:  # noqa: BLE001 - every failure is the test's answer
            log.warning("settings test %s failed: %s", t["kind"], type(e).__name__)
            detail, result, state = f"{type(e).__name__}: {str(e)[:300]}", {}, "failed"
        await cur.execute(
            "UPDATE catalog.settings_test SET state = %s, detail = %s, result = %s,"
            " finished_at = %s WHERE test_id = %s",
            (state, detail, Jsonb(result), now, test_id),
        )
        await cur.execute(
            "INSERT INTO catalog.audit_log (actor, action, decision, details)"
            " VALUES ('system:settings', 'settings.test', %s, %s)",
            (
                "allow" if state == "passed" else "deny",
                Jsonb(
                    {
                        "test_id": str(test_id),
                        "kind": t["kind"],
                        "state": state,
                        "identity_id": str(t["identity_id"]),
                    }
                ),
            ),
        )

    async def _run(self, conn, t: dict[str, Any], now: datetime) -> tuple[str, dict]:
        kind = t["kind"]
        if kind == "aws_assume_role":
            arn = str((t["params"] or {}).get("role_arn") or "")
            m = ROLE_ARN.match(arn)
            if not m:
                raise _Failed("not an IAM role ARN")
            try:
                got = await self.assume(arn, m[1])
            except LookupError as e:
                raise _Failed(str(e)) from None
            return f"The worker assumed {arn} with its web identity.", {
                "assumed_role_arn": arn,
                "expires_at": got.get("expires_at"),
            }
        n = self.notifier
        if n is None:
            raise _Failed(
                "Teams and Graph aren't configured on the worker (OMNIFILE_TENANT_ID,"
                " OMNIFILE_TEAMS_BOT_APP_ID, OMNIFILE_TEAMS_APP_ID)"
            )
        if kind == "email":
            if not n.config.sender:
                raise _Failed("no sender mailbox is configured (OMNIFILE_NOTIFY_SENDER)")
            if not t["mail"]:
                raise _Failed("the catalog has no mail address for you yet (Graph sets it)")
            try:
                await n.graph.send_mail(n.config.sender, t["mail"], SUBJECT, BODY)
            except Exception as e:  # GraphError
                raise _Failed(f"Graph refused the mail: {getattr(e, 'code', e)}") from None
            return f"Sent from {n.config.sender} to {t['mail']}.", {"sender": n.config.sender}
        if kind == "teams_card":
            oid = t["entra_object_id"]
            if oid is None:
                raise _Failed("the catalog has no Entra object id for you")
            conv = await n._conversation(conn, t["identity_id"], str(oid), now)
            if conv is None:
                raise _Failed(
                    "your tenant blocked the OmniFile app for you, so cards can't"
                    " reach you; recall notices fall back to email"
                )
            if isinstance(conv, float):
                raise _Failed(
                    "Teams was busy or the app is still installing; try again in a minute"
                )
            try:
                await n.bot.send(
                    conv["service_url"], conv["conversation_id"], CARD, "OmniFile test card"
                )
            except Exception as e:  # BotError
                raise _Failed(f"the bot couldn't send it: {getattr(e, 'code', e)}") from None
            return "Card sent to your Teams chat with OmniFile.", {}
        raise _Failed(f"unknown test {kind}")


class _Failed(Exception):
    pass


def settings_jobs(worker: Any) -> list[Job]:
    from ..worker.notify_jobs import NotifyConfigError, notifier_from_env

    try:
        notifier = notifier_from_env()
    except NotifyConfigError as e:
        log.warning("settings tests: no notifier (%s)", e)
        notifier = None
    tests = SettingsTests(notifier, assume_from_env())

    async def handle(msg: Message, conn: psycopg.AsyncConnection) -> None:
        await tests.handle(msg, conn, worker.clock.now())

    return [QueueJob(QUEUE, handle)]
