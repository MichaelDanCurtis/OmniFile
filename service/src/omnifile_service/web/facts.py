"""Catalog facts for the Policy preview track's functions (`preview.DatasetFacts`)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, time
from typing import Any

from ..backends.base import LogicalTier
from ..worker.backends import capabilities_for
from .preview import DatasetFacts
from .problems import WebError, not_found
from .rows import tier_state


async def price_override(db, backend_kind: str) -> dict[str, Any] | None:
    row = await (
        await db.execute(
            "SELECT body FROM catalog.price_table WHERE backend_kind = %s AND region = ''",
            (backend_kind,),
        )
    ).fetchone()
    return None if row is None else row["body"]


async def dataset_facts(
    db,
    dataset_id: int,
    *,
    assumptions: Mapping[str, Any] | None = None,
    share_overrides: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[DatasetFacts, dict[str, Any]]:
    """(facts, the dataset row with backend kind and config)."""
    ds = await (
        await db.execute(
            "SELECT d.*, b.kind AS backend_kind, b.config AS backend_config,"
            " b.private_network FROM catalog.dataset d"
            " JOIN catalog.backend b ON b.backend_id = d.backend_id WHERE d.dataset_id = %s",
            (dataset_id,),
        )
    ).fetchone()
    if ds is None:
        raise not_found("dataset")
    try:
        caps = capabilities_for(ds["backend_kind"], ds["backend_config"] or {})
    except (ValueError, KeyError, TypeError) as e:
        raise WebError("unavailable", f"backend capabilities unavailable: {e}") from None
    shares = await (
        await db.execute(
            "SELECT * FROM catalog.share WHERE dataset_id = %s ORDER BY share_id", (dataset_id,)
        )
    ).fetchall()
    facts = DatasetFacts(
        dataset_id=dataset_id,
        backend_kind=ds["backend_kind"],
        caps=caps,
        layout=ds["storage_layout"],
        shares=list(shares),
        price_override=await price_override(db, ds["backend_kind"]),
        assumptions=assumptions,
        share_overrides=share_overrides,
    )
    return facts, ds


def pin_facts(
    f: dict[str, Any], loc: dict[str, Any] | None, layout: str, now: datetime
) -> dict[str, Any]:
    """`checks.PinFacts` fields for one file."""
    ts = tier_state(
        {**loc, "restore_state": f.get("restore_state"),
         "restore_expires_at": f.get("restore_expires_at")} if loc else None,
        now,
    )  # fmt: skip
    logical = ts["logical"] or ("smart" if layout == "two_account" else "hot")
    archive_at = f.get("archive_at")
    return {
        "size": int(f["size"] or 0),
        "logical_tier": LogicalTier(logical),
        "native_class": ts["native_class"],
        "tier_changed_at": loc.get("tier_changed_at") if loc else None,
        "explicit_tier_set": ts["explicit_tier_set"],
        "archived": ts["archived"],
        "smart_layout": layout == "two_account",
        "archive_at": datetime.combine(archive_at, time(), tzinfo=UTC) if archive_at else None,
        "shares_blob": bool(loc and loc.get("shares_blob")),
    }
