"""The seam to the Policy preview track (docs/milestones/M4.md, "Interfaces
between tracks"): `policy/preview.py`, `policy/cost.py`, `policy/checks.py`.

`web` gathers the facts from the catalog (`DatasetFacts`) and calls those
pure functions through `PolicyPreviewPort`; it never computes a cost or a
check itself. Tests may pass any object with the same methods.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from ..backends.base import Capabilities, LogicalTier
from ..policy import checks as checks_mod
from ..policy import cost as cost_mod
from ..policy import preview as preview_mod
from .problems import invalid


@dataclass(frozen=True)
class DatasetFacts:
    dataset_id: int
    backend_kind: str
    caps: Capabilities
    layout: str  # two_account | single_account
    #: catalog.share rows of the dataset.
    shares: Sequence[Mapping[str, Any]]
    #: The admin's price-table override body, if any.
    price_override: Mapping[str, Any] | None = None
    #: The request's `assumptions` and `share_overrides`, as sent.
    assumptions: Mapping[str, Any] | None = None
    share_overrides: Sequence[Mapping[str, Any]] | None = None


class PreviewPort(Protocol):
    def policy_preview(self, body: Mapping[str, Any], facts: DatasetFacts) -> dict[str, Any]: ...

    def share_checks(
        self, share: Mapping[str, Any], *, restricted_groups: bool
    ) -> list[dict[str, Any]]: ...

    def pin_checks(
        self,
        target: str,
        pin: Mapping[str, Any],
        facts: DatasetFacts,
        now: datetime,
        *,
        until: datetime | None,
        override_archive: bool,
    ) -> tuple[list[dict[str, Any]], dict[str, str] | None]:
        """(checks, monthly cost change) for one file."""
        ...

    def price_table(
        self, backend_kind: str, override: Mapping[str, Any] | None
    ) -> dict[str, Any]: ...


def share_facts(s: Mapping[str, Any]) -> preview_mod.ShareFacts:
    sec = tuple(s["nfs_sec"] or ())
    return preview_mod.ShareFacts(
        share_id=int(s["share_id"]),
        name=s["name"],
        protocols=tuple(s["protocols"]),
        nfs_sec=sec,
        trust_level=s.get("trust_level") or ("host_trusted" if "sys" in sec else "proven"),
        on_open=s["on_open"],
        wait_threshold_minutes=s.get("wait_threshold_minutes"),
        worm_enabled=bool(s.get("worm_enabled")),
    )


class PolicyPreviewPort:
    """Calls the Policy preview track's pure functions."""

    def context(self, facts: DatasetFacts) -> preview_mod.PreviewContext:
        try:
            assumptions = preview_mod.CostAssumptions.from_json(facts.assumptions)
        except ValueError as e:
            raise invalid("assumptions", str(e)) from None
        try:
            prices = cost_mod.load_price_table(facts.backend_kind, facts.price_override)
        except cost_mod.PriceTableError:
            # A stored override that no longer loads: price with the built-in.
            prices = cost_mod.load_price_table(facts.backend_kind, None)
        shares = preview_mod.apply_share_overrides(
            [share_facts(s) for s in facts.shares], facts.share_overrides
        )
        return preview_mod.PreviewContext(
            caps=facts.caps,
            layout=facts.layout,
            shares=shares,
            prices=prices,
            assumptions=assumptions,
        )

    def policy_preview(self, body: Mapping[str, Any], facts: DatasetFacts) -> dict[str, Any]:
        return preview_mod.preview(body, self.context(facts)).to_json()

    def share_checks(
        self, share: Mapping[str, Any], *, restricted_groups: bool
    ) -> list[dict[str, Any]]:
        found = checks_mod.share_checks(share_facts(share), restricted_groups=restricted_groups)
        return [c.to_json() for c in found]

    def pin_checks(
        self,
        target: str,
        pin: Mapping[str, Any],
        facts: DatasetFacts,
        now: datetime,
        *,
        until: datetime | None,
        override_archive: bool,
    ) -> tuple[list[dict[str, Any]], dict[str, str] | None]:
        ctx = self.context(facts)
        current = checks_mod.PinFacts(**pin)
        tier = LogicalTier(target)
        found = checks_mod.pin_checks(
            tier, current, ctx, now, until=until, override_archive=override_archive
        )
        change = cost_mod.pin_cost_change(tier, current, ctx)
        return [c.to_json() for c in found], (None if change is None else change.to_json())

    def price_table(self, backend_kind: str, override: Mapping[str, Any] | None) -> dict[str, Any]:
        try:
            table = cost_mod.load_price_table(backend_kind, override)
        except cost_mod.PriceTableError as e:
            raise invalid("price_table", str(e)) from None
        out = table.to_json()
        out["overridden"] = override is not None
        return out
