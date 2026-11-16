"""Settings tests: the `web` fixtures of tests/web (the app over a migrated
catalog, the dev issuer, the spec-checking client), with fake probes and a
deployment's settings environment set per test."""

from __future__ import annotations

from web.conftest import (  # noqa: F401 - fixtures
    API_AUDIENCE,
    APP_ID,
    ORIGIN,
    TENANT,
    App,
    World,
    db_url,
    seed,
    sql,
    web,
)
