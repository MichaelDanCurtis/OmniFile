"""The Teams and email jobs (teams.md sections 6-8), loaded by `runner._track_jobs`.

- `notify` (queue): bring a recall's cards up to date, or tell a pin's setter
  (`notify.delivery`).
- `identity_resolve` (queue, created by migration 0010): refresh one identity from Graph.
- `notify_due` (sweep, every minute): re-queue notifications whose retry is overdue.
- `group_directory` (sweep, daily): synced AD groups' names and SIDs.

Configuration is read from the environment (teams.md section 10). Without
OMNIFILE_TENANT_ID, OMNIFILE_TEAMS_BOT_APP_ID and OMNIFILE_TEAMS_APP_ID the worker runs no
notification jobs (the `notify` queue then waits), so a deployment without Teams still runs.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path

import httpx
import psycopg

from ..notify.botconnector import BotConnector
from ..notify.delivery import Notifier, NotifyConfig
from ..notify.graph import GRAPH_URL, GraphClient
from ..notify.intents import IntentSigner, load_private_key
from ..notify.tokens import BOT_SCOPE, GRAPH_SCOPE, ClientCredentials, ManagedIdentity
from .jobs import Job, QueueJob, SweepJob
from .queue import Message

log = logging.getLogger(__name__)

NOTIFY = "notify"
IDENTITY_RESOLVE = "identity_resolve"
ENTRA_AUTHORITY = "https://login.microsoftonline.com"
SERVICE_URL = "https://smba.trafficmanager.net/teams/"
DEV_ONLY = ("OMNIFILE_GRAPH_URL", "OMNIFILE_BOTCONNECTOR_TOKEN_URL", "OMNIFILE_ENTRA_AUTHORITY")


class NotifyConfigError(ValueError):
    pass


def _token_source(
    http: httpx.AsyncClient, credential: str, token_url: str, client_id: str, scope: str
):
    if credential == "managed_identity":
        return ManagedIdentity(scope, client_id=os.environ.get("AZURE_CLIENT_ID") or None)
    secret = Path(credential).read_text().strip()
    if not secret:
        raise NotifyConfigError(f"{credential} is empty")
    return ClientCredentials(http, token_url, client_id, secret, scope)


def notifier_from_env(
    env: Mapping[str, str] | None = None, http: httpx.AsyncClient | None = None
) -> Notifier | None:
    env = dict(os.environ if env is None else env)
    tenant = env.get("OMNIFILE_TENANT_ID", "").lower()
    bot_app = env.get("OMNIFILE_TEAMS_BOT_APP_ID", "")
    teams_app = env.get("OMNIFILE_TEAMS_APP_ID", "")
    if not (tenant and bot_app and teams_app):
        return None
    dev = env.get("OMNIFILE_DEV_MODE") == "1"
    for name in DEV_ONLY:
        if env.get(name) and not dev:
            raise NotifyConfigError(f"{name} points at a fake: it needs OMNIFILE_DEV_MODE=1")
    service_url = env.get("OMNIFILE_TEAMS_SERVICE_URL") or SERVICE_URL
    if not service_url.startswith("https://") and not dev:
        raise NotifyConfigError("OMNIFILE_TEAMS_SERVICE_URL must be https")
    authority = (env.get("OMNIFILE_ENTRA_AUTHORITY") or ENTRA_AUTHORITY).rstrip("/")
    entra_token_url = f"{authority}/{tenant}/oauth2/v2.0/token"
    http = http or httpx.AsyncClient(trust_env=False, follow_redirects=False)
    bot_tokens = _token_source(
        http,
        env.get("OMNIFILE_TEAMS_BOT_CREDENTIAL", "managed_identity"),
        env.get("OMNIFILE_BOTCONNECTOR_TOKEN_URL") or entra_token_url,
        bot_app,
        BOT_SCOPE,
    )
    graph_tokens = _token_source(
        http,
        env.get("OMNIFILE_GRAPH_CREDENTIAL", "managed_identity"),
        entra_token_url,
        env.get("OMNIFILE_GRAPH_CLIENT_ID", bot_app),
        GRAPH_SCOPE,
    )
    signer = None
    key_path = env.get("OMNIFILE_TEAMS_INTENT_SIGNING_KEY")
    if key_path:
        signer = IntentSigner(
            load_private_key(key_path), env.get("OMNIFILE_TEAMS_INTENT_KID", "intent")
        )
    else:
        log.warning("no OMNIFILE_TEAMS_INTENT_SIGNING_KEY: cards will have no buttons")
    return Notifier(
        config=NotifyConfig(
            tenant_id=tenant,
            teams_app_id=teams_app,
            service_url=service_url,
            portal_url=env.get("OMNIFILE_PORTAL_URL") or None,
            sender=env.get("OMNIFILE_NOTIFY_SENDER") or None,
        ),
        graph=GraphClient(http, graph_tokens, env.get("OMNIFILE_GRAPH_URL") or GRAPH_URL),
        bot=BotConnector(http, bot_tokens, allow_http=dev),
        signer=signer,
    )


def build_jobs(notifier: Notifier, clock) -> list[Job]:
    """The jobs for `notifier`, on the worker's clock (`clock.now()`)."""

    async def notify(msg: Message, conn: psycopg.AsyncConnection) -> None:
        await notifier.handle(msg, conn, clock.now())

    async def resolve(msg: Message, conn: psycopg.AsyncConnection) -> None:
        await notifier.handle_identity_resolve(msg, conn, clock.now())

    async def due(conn: psycopg.AsyncConnection, now) -> int:
        return await notifier.sweep_due(conn, now)

    async def directory(conn: psycopg.AsyncConnection, now) -> int:
        return await notifier.sync_group_directory(conn, now)

    return [
        QueueJob(NOTIFY, notify),
        QueueJob(IDENTITY_RESOLVE, resolve),
        SweepJob("notify_due", timedelta(minutes=1), due),
        SweepJob("group_directory", timedelta(hours=24), directory),
    ]


def notify_jobs(worker) -> list[Job]:
    """Called by `runner._track_jobs`."""
    notifier = notifier_from_env()
    if notifier is None:
        log.info("Teams is not configured: no notify jobs")
        return []
    return build_jobs(notifier, worker.clock)
