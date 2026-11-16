"""Pinning a run to the subscription it was checked against.

The owner signs in once (az login). Each phase first reads the owner's sign-in where it is
(`az account show`, which changes nothing) and checks it against the allow-list. Only then
does it copy the least of it az needs into a private directory, OUT/az-config (0700): the
profile, the MSAL token cache, and the service principal entries only when the sign-in is a
service principal's. It sets the run's subscription there and runs every az call, and every
guide step, with AZURE_CONFIG_DIR pointing at it. An `az account set` in another shell, even in
the middle of a phase, changes the owner's own directory and nothing in the run.

The copy holds tokens, so it lives only as long as the phase that made it: `unpin` deletes it
when the phase ends, however it ends (main's finally, which SIGTERM and SIGHUP also reach), and
a copy left by a killed phase is deleted before the next one copies afresh. OUT is never inside
the repository other than under the gitignored tests/real/out (main refuses such an --out).
Extensions stay where the owner has them (AZURE_EXTENSION_DIR).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from .core import Context, Refused

#: Always copied: the subscriptions az knows, and its token cache (plain or encrypted).
AZ_SIGN_IN = ("azureProfile.json", "msal_token_cache.json", "msal_token_cache.bin")
#: Copied only when the sign-in is a service principal's (its secret or certificate path).
AZ_SP = ("service_principal_entries.json", "service_principal_entries.bin")
#: A sovereign cloud's endpoints, when the owner registered one.
AZ_CLOUDS = ("clouds.config",)


def run_dir(ctx: Context) -> Path:
    return ctx.out / "az-config"


def owner_dir(ctx: Context) -> Path:
    saved = getattr(ctx, "_owner_az", None)
    if saved:
        return Path(saved)
    return Path(ctx.env.get("AZURE_CONFIG_DIR") or Path.home() / ".azure")


def pinned(ctx: Context) -> bool:
    return ctx.env.get("AZURE_CONFIG_DIR") == str(run_dir(ctx))


def owner_account(ctx: Context) -> dict | None:
    """The owner's sign-in, read where it is: nothing is copied before the allow-list check."""
    if pinned(ctx):
        return ctx.az("account", "show")
    return ctx.az("account", "show", check=False)


def pin(ctx: Context, subscription: str, *, service_principal: bool) -> Path:
    """Copy the sign-in (after the caller's allow-list check) and make SUBSCRIPTION the run
    directory's default, explicitly. Idempotent within a phase."""
    d = run_dir(ctx)
    if pinned(ctx):
        return d
    owner = owner_dir(ctx)
    if d.exists():
        shutil.rmtree(d)  # a killed phase's copy: never reused
    d.mkdir(parents=True, mode=0o700)
    names = AZ_SIGN_IN + AZ_CLOUDS + (AZ_SP if service_principal else ())
    for name in names:
        src = owner / name
        if src.is_file():
            dst = d / name
            shutil.copy2(src, dst)
            os.chmod(dst, 0o600)
    ctx._owner_az = str(owner)  # type: ignore[attr-defined]
    ctx.env["AZURE_CONFIG_DIR"] = str(d)
    ctx.env.setdefault("AZURE_EXTENSION_DIR", str(owner / "cliextensions"))
    ctx.run(["az", "account", "set", "--subscription", subscription])
    got = ctx.az("account", "show") or {}
    if got.get("id") != subscription:
        unpin(ctx)
        raise Refused(f"could not pin the run to subscription {subscription} (az shows {got})")
    return d


def unpin(ctx: Context) -> None:
    """Delete the run's copy of the sign-in and point az back at the owner's own directory."""
    d = run_dir(ctx)
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    owner = getattr(ctx, "_owner_az", None)
    if ctx.env.get("AZURE_CONFIG_DIR") == str(d):
        if owner:
            ctx.env["AZURE_CONFIG_DIR"] = owner
        else:
            ctx.env.pop("AZURE_CONFIG_DIR", None)


def is_service_principal(acct: dict) -> bool:
    return (acct.get("user") or {}).get("type") == "servicePrincipal"
