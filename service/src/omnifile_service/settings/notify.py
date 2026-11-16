"""Settings, Teams and Email fallback: health from the catalog's notifications.

The worker sends cards and mail (teams.md); `web` sees what it recorded:
cards and mails sent and failed in the last 24 hours, the latest error, and
conversations the tenant blocked. The settings names come from `web`'s
environment, the same names the worker reads (``OMNIFILE_TEAMS_APP_ID``,
``OMNIFILE_TEAMS_BOT_APP_ID``, ``OMNIFILE_NOTIFY_SENDER``).

The app package: `deploy/teams/build.sh` writes `omnifile-teams-app.zip`.
``OMNIFILE_TEAMS_APP_PACKAGE`` names where `web` can read it (for example a
file mounted into the container); otherwise the page says how to build it.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..api.validate import rfc3339

DAY = timedelta(hours=24)
BUILD_STEPS = [
    "On an admin workstation, from the repository: deploy/teams/build.sh (it writes"
    " deploy/teams/out/omnifile-teams-app.zip from the deployment's ids).",
    "Upload it in the Teams admin centre (Manage apps, Upload), or pass it to"
    " deploy/entra/register.sh --teams-app-zip.",
    "To download it here instead, mount the zip into the web app and set"
    " OMNIFILE_TEAMS_APP_PACKAGE to its path.",
]
MAX_PACKAGE = 5 * 1024 * 1024


def package_path(env: Mapping[str, str]) -> Path | None:
    raw = env.get("OMNIFILE_TEAMS_APP_PACKAGE")
    if not raw:
        return None
    p = Path(raw)
    try:
        if p.is_file() and p.suffix == ".zip" and p.stat().st_size <= MAX_PACKAGE:
            return p
    except OSError:
        return None
    return None


async def _counts(db, channel: str, now: datetime) -> dict[str, Any]:
    row = await (
        await db.execute(
            "SELECT count(*) FILTER (WHERE state = 'sent') AS sent,"
            "       count(*) FILTER (WHERE state IN ('failed', 'fell_back')) AS failed,"
            "       max(updated_at) FILTER (WHERE state = 'sent') AS last_sent_at"
            "  FROM catalog.notification WHERE channel = %s AND updated_at >= %s",
            (channel, now - DAY),
        )
    ).fetchone()
    err = await (
        await db.execute(
            "SELECT last_error FROM catalog.notification WHERE channel = %s"
            "   AND last_error IS NOT NULL ORDER BY updated_at DESC LIMIT 1",
            (channel,),
        )
    ).fetchone()
    return {
        "sent": int(row["sent"] or 0),
        "failed": int(row["failed"] or 0),
        "last_sent_at": rfc3339(row["last_sent_at"]),
        "last_error": err["last_error"] if err else None,
    }


async def teams_settings(db, env: Mapping[str, str], now: datetime) -> dict[str, Any]:
    app_id = env.get("OMNIFILE_TEAMS_APP_ID") or None
    bot_id = env.get("OMNIFILE_TEAMS_BOT_APP_ID") or None
    configured = bool(app_id and bot_id and env.get("OMNIFILE_TENANT_ID"))
    c = await _counts(db, "teams", now)
    blocked = await (
        await db.execute(
            "SELECT count(*) AS n FROM catalog.teams_conversation WHERE state = 'blocked'"
        )
    ).fetchone()
    if not configured:
        status, msg = (
            "not_configured",
            (
                "Teams isn't configured on the web app (OMNIFILE_TENANT_ID, OMNIFILE_TEAMS_APP_ID,"
                " OMNIFILE_TEAMS_BOT_APP_ID). If the worker has them, cards still go out."
            ),
        )
    elif c["failed"] and not c["sent"]:
        status, msg = "attention", "Cards failed in the last 24 hours and none went out."
    elif c["sent"]:
        status, msg = "ok", f"{c['sent']} cards went out in the last 24 hours."
    else:
        status, msg = "idle", "No cards in the last 24 hours. Send yourself a test card."
    pkg = package_path(env)
    return {
        "configured": configured,
        "teams_app_id": app_id,
        "bot_app_id": bot_id,
        "health": {
            "status": status,
            "cards_sent_24h": c["sent"],
            "cards_failed_24h": c["failed"],
            "last_sent_at": c["last_sent_at"],
            "last_error": c["last_error"],
            "conversations_blocked": int(blocked["n"] or 0),
            "message": msg,
        },
        "package": {
            "available": pkg is not None,
            "file_name": pkg.name if pkg else None,
            "instructions": [] if pkg else BUILD_STEPS,
        },
    }


async def email_settings(
    db, env: Mapping[str, str], identity_id: int | None, now: datetime
) -> dict[str, Any]:
    sender = env.get("OMNIFILE_NOTIFY_SENDER") or None
    c = await _counts(db, "email", now)
    mine = None
    if identity_id is not None:
        r = await (
            await db.execute(
                "SELECT mail FROM catalog.identity WHERE identity_id = %s", (identity_id,)
            )
        ).fetchone()
        mine = r["mail"] if r else None
    steps = []
    if not sender:
        steps.append(
            "Set notifySender (OMNIFILE_NOTIFY_SENDER) to the mailbox fallback mail comes from,"
            " and scope the worker's Mail.Send to it alone with Exchange RBAC for Applications"
            " (deploy/entra/register.sh --mail-sender prints the steps)."
        )
    if mine is None:
        steps.append(
            "The catalog has no mail address for you yet: the worker reads it from Graph when"
            " it next resolves your identity."
        )
    return {
        "configured": sender is not None,
        "sender": sender,
        "my_mail": mine,
        "health": {
            "sent_24h": c["sent"],
            "failed_24h": c["failed"],
            "last_sent_at": c["last_sent_at"],
            "last_error": c["last_error"],
        },
        "instructions": steps,
    }
