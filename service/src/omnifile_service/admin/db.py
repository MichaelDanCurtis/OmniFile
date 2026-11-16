"""The catalog as its administrator, through `catalog/connect.py`.

No app may INSERT or UPDATE `catalog.backend` (`catalog/privileges.py`). The
portal's Settings, Storage page writes only through the owner-run
`catalog.backend_register` (migration 0035), always private. The CLI registers
storage as the administrator, who owns the catalog. This is the apps' own connection path
(`connect.from_env`, with every check it makes), with two differences:

- the session acts as itself, never as an app's group role (no ``-c role=...``);
- with Entra sign-in, the token is the administrator's own: ``AZURE_CLIENT_ID``'s
  managed identity when set (a job), else `DefaultAzureCredential` (``az login``).

With ``OMNIFILE_DATABASE_URL`` the connection string is used as it is, as the
migrations use it; libpq then reads the password (the administrator's Entra
token) from ``PGPASSWORD``.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
from collections.abc import Mapping

from psycopg.conninfo import conninfo_to_dict, make_conninfo

from ..catalog import connect
from ..catalog.privileges import ROLES

#: Any app: only its connection checks are used, never its role.
_APP = "worker"
APPLICATION_NAME = "omnifile-admin"


def _admin_credential(env: Mapping[str, str]) -> connect.TokenCredential:
    client_id = (env.get("AZURE_CLIENT_ID") or "").strip()
    if client_id:
        from azure.identity import ManagedIdentityCredential

        return ManagedIdentityCredential(client_id=client_id)
    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential()


def admin_db(
    env: Mapping[str, str] | None = None, *, credential: connect.TokenCredential | None = None
) -> connect.CatalogDb:
    """The catalog for the administrator, from the environment. Raises ValueError."""
    env = os.environ if env is None else env
    entra = (
        bool((env.get("OMNIFILE_PG_HOST") or "").strip())
        and not (env.get("OMNIFILE_DATABASE_URL") or "").strip()
    )
    if entra and credential is None:
        credential = _admin_credential(env)
    db = connect.from_env(_APP, env, credential=credential)
    if db.password is None:
        return db
    params = conninfo_to_dict(db.conninfo)
    role = f"-c role={ROLES[_APP]}"
    options = " ".join(str(params.pop("options", "")).replace(role, "").split())
    if options:
        params["options"] = options
    params["application_name"] = APPLICATION_NAME
    return connect.CatalogDb(make_conninfo(**params), db.password)


#: Claims kept for the audit record, each from the first of its names present.
_CLAIMS = (
    ("oid", ("oid",)),
    ("upn", ("upn", "preferred_username", "unique_name")),
    ("appid", ("appid", "azp")),
    ("tid", ("tid",)),
)


def jwt_claims(token: str | None) -> dict[str, str]:
    """Who an Entra access token is for: its object id, user name, app id and
    tenant, when present. Read, not verified: PostgreSQL verified the token at
    sign-in, and this only records it. Anything not a JWT gives {}."""
    parts = (token or "").split(".")
    if len(parts) != 3:
        return {}
    try:
        body = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(body))
    except (ValueError, binascii.Error):
        return {}
    if not isinstance(claims, dict):
        return {}
    out: dict[str, str] = {}
    for key, names in _CLAIMS:
        for n in names:
            v = claims.get(n)
            if isinstance(v, str) and 0 < len(v) <= 256:
                out[key] = v
                break
    return out


def signed_in_as(db: connect.CatalogDb, env: Mapping[str, str] | None = None) -> dict[str, str]:
    """The Entra claims of the token this connection signs in with, for the
    audit record: under group sign-in the database user is the group, not the
    person. The token is the credential's (Entra sign-in), else the connection
    string's password, else ``PGPASSWORD`` (libpq's order). Never the token
    itself."""
    env = os.environ if env is None else env
    if db.password is not None:
        token = db.password()
    else:
        token = conninfo_to_dict(db.conninfo).get("password") or env.get("PGPASSWORD")
    return jwt_claims(str(token) if token else None)
