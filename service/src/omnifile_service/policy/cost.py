"""Price tables and the first-year cost the policy editor shows.

Pure functions, no database (M4.md, Policy preview track). Prices come from a
dated, per-region table, `policy/prices/<kind>.json`, or from an admin's
override stored in the catalog (portal-api.md section 5.8). Every built-in
number carries ``verify: true`` until someone checks it against the
provider's price page.

The model, per TB (1,024 GB: Azure bills a GB as 2^30 bytes) over the first
365 days of a file written on day 0 and never read:

- **storage** by day, at the rate of the tier and account the file is in.
  Smart is a range: all of its days at the Cold rate (low) to all at the
  Hot rate (high), because Microsoft moves Smart blobs by use;
- **transitions**: a move to a cooler tier is charged as a write at the
  destination tier, per object (``mean_file_bytes`` sets objects per TB);
- **archive copy and read-back** on two-account layouts: Copy Blob into the
  archive account (landing in Hot), a CRC64 read-back, then Set Blob Tier to
  Archive there (`worker.tiering`);
- **early deletion**: leaving a tier before its minimum is charged as if the
  file stayed the remaining days;
- **Smart monitoring**, per 10,000 objects over 128 KiB per month;
- **recall**, when ``recall_fraction_per_year`` is set: that share of the
  data archived during the year is recalled once at Standard priority.

Recall cost per TB at each speed is given apart, with the provider's stated
hours. Everything is compared with keeping the data Hot all year.

The model prices every file as a blob of its own. So it counts no saving from
whole-file dedupe, and a file that leaves a shared blob for a copy of its own
(decision 66) doesn't change it: the bill moves towards this number, not past it.
A pin preview is about one file on one blob, so it does say: `pin_own_copy`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from importlib import resources
from typing import TYPE_CHECKING, Any

from ..backends.base import Capabilities, LogicalTier, RestoreOption, RestoreSpeed, TierInfo
from .model import Mode, Policy

if TYPE_CHECKING:
    from .checks import PinFacts
    from .preview import CostAssumptions, PreviewContext

GiB = 1024**3
TB_BYTES = 1024 * GiB
GB_PER_TB = Decimal(1024)
YEAR_DAYS = 365
DAYS_PER_MONTH = Decimal(YEAR_DAYS) / 12
#: Smart's monitoring fee applies to objects over 128 KiB [V].
SMART_MONITORED_ABOVE = 128 * 1024
CENT = Decimal("0.01")

#: The storage redundancy each account role is priced at. The two-account
#: layout's live account is zonal (Smart needs ZRS); the archive account and
#: the single-account layout are priced as LRS [A][W].
REDUNDANCY = {"live": "ZRS", "archive": "LRS", "single": "LRS"}

#: Backend kinds that use another kind's built-in table. Sim plays Azure.
TABLE_FOR_KIND = {"azure_blob": "azure_blob", "sim": "azure_blob"}

_PRICE_FIELDS = (
    "gb_month",
    "write_per_10k",
    "read_per_10k",
    "retrieval_per_gb",
    "retrieval_per_gb_high",
    "read_per_10k_high",
    "monitoring_per_10k_objects_month",
)


class PriceTableError(ValueError):
    """A price table (built-in or override) is missing or malformed."""


#: The largest rate a table may hold, in its currency per GB-month, per GB or per 10,000.
MAX_RATE = Decimal(10000)


def _dec(v: Any, where: str) -> Decimal:
    if isinstance(v, bool) or not isinstance(v, str | int | float):
        raise PriceTableError(f"{where} must be a decimal string, not {v!r}")
    try:
        d = Decimal(str(v))
    except InvalidOperation:
        raise PriceTableError(f"{where} must be a decimal string, not {v!r}") from None
    if not d.is_finite() or d < 0:
        raise PriceTableError(f"{where} must be a number >= 0, not {v!r}")
    # No provider charges anywhere near this per GB or per 10,000 operations; a bigger
    # number is a typo, and an exponent like 1e400 failed later, as a 500 (#63 review).
    if d > MAX_RATE or len(str(v)) > 24:
        raise PriceTableError(f"{where} must be at most {MAX_RATE}, with at most 24 characters")
    return d


def _fmt(d: Decimal) -> str:
    return str(d.normalize()) if d != d.to_integral_value() else str(d.quantize(Decimal(1)))


@dataclass(frozen=True)
class Money:
    """A decimal amount, or a range (``amount`` is the low end)."""

    amount: Decimal
    currency: str = "USD"
    high: Decimal | None = None

    @classmethod
    def of(cls, low: Decimal, high: Decimal, currency: str) -> Money:
        return cls(low, currency, None if high == low else high)

    @property
    def upper(self) -> Decimal:
        return self.amount if self.high is None else self.high

    def to_json(self) -> dict[str, str]:
        out = {"amount": str(self.amount.quantize(CENT, ROUND_HALF_UP)), "currency": self.currency}
        if self.high is not None:
            out["high"] = str(self.high.quantize(CENT, ROUND_HALF_UP))
        return out


@dataclass(frozen=True)
class ClassPrice:
    """Prices of one native class, as the provider states them."""

    gb_month: Decimal | None = None
    write_per_10k: Decimal | None = None
    read_per_10k: Decimal | None = None
    retrieval_per_gb: Decimal | None = None
    retrieval_per_gb_high: Decimal | None = None
    read_per_10k_high: Decimal | None = None
    monitoring_per_10k_objects_month: Decimal | None = None
    minimum_days: int | None = None
    verify: bool = True

    @classmethod
    def from_json(cls, d: Any, where: str) -> ClassPrice:
        if not isinstance(d, Mapping):
            raise PriceTableError(f"{where} must be an object")
        kw: dict[str, Any] = {
            f: _dec(d[f], f"{where}.{f}") for f in _PRICE_FIELDS if d.get(f) is not None
        }
        md = d.get("minimum_days")
        if md is not None and (isinstance(md, bool) or not isinstance(md, int) or md < 0):
            raise PriceTableError(f"{where}.minimum_days must be a whole number >= 0")
        verify = d.get("verify", True)
        if not isinstance(verify, bool):
            raise PriceTableError(f"{where}.verify must be true or false")
        return cls(**kw, minimum_days=md, verify=verify)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            f: _fmt(getattr(self, f)) for f in _PRICE_FIELDS if getattr(self, f) is not None
        }
        out["minimum_days"] = self.minimum_days
        out["verify"] = self.verify
        return out


@dataclass(frozen=True)
class PriceTable:
    """One backend kind's prices in one region (the API's ``PriceTable``)."""

    id: str
    backend_kind: str
    region: str | None
    currency: str
    as_of: date
    classes: Mapping[str, ClassPrice]
    verify: bool = True
    overridden: bool = False
    source: str | None = None
    notes: tuple[str, ...] = ()

    def price(self, native_class: str, account: str | None = None) -> ClassPrice | None:
        """The class's prices for an account role (``live``, ``archive``,
        ``single``): ``"<Class>:<redundancy>"`` first, then ``"<Class>"``."""
        red = REDUNDANCY.get(account or "")
        if red is not None and f"{native_class}:{red}" in self.classes:
            return self.classes[f"{native_class}:{red}"]
        return self.classes.get(native_class)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "backend_kind": self.backend_kind,
            "region": self.region,
            "currency": self.currency,
            "as_of": self.as_of.isoformat(),
            "source": self.source,
            "verify": self.verify,
            "overridden": self.overridden,
            "classes": {k: v.to_json() for k, v in self.classes.items()},
            "notes": list(self.notes),
        }

    def ref(self) -> dict[str, Any]:
        """The ``PriceTableRef`` a preview carries."""
        return {
            "id": self.id,
            "backend_kind": self.backend_kind,
            "region": self.region,
            "as_of": self.as_of.isoformat(),
            "verify": self.verify,
            "overridden": self.overridden,
        }


def _classes(d: Any, where: str) -> dict[str, ClassPrice]:
    if not isinstance(d, Mapping):
        raise PriceTableError(f"{where} must be an object keyed by native class")
    return {str(k): ClassPrice.from_json(v, f"{where}.{k}") for k, v in d.items()}


def _as_of(v: Any, where: str) -> date:
    try:
        return date.fromisoformat(v)
    except (TypeError, ValueError):
        raise PriceTableError(f"{where} must be a date (YYYY-MM-DD), not {v!r}") from None


def _currency(v: Any) -> str:
    if not isinstance(v, str) or len(v) != 3 or not v.isascii() or not v.isupper():
        raise PriceTableError(f"currency must be a 3-letter code, not {v!r}")
    return v


def builtin_table(name: str) -> Mapping[str, Any]:
    """The raw built-in file `policy/prices/<name>.json`."""
    path = resources.files(__package__).joinpath("prices", f"{name}.json")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PriceTableError(f"no built-in price table {name!r}") from None


def load_price_table(
    backend_kind: str, override: Mapping[str, Any] | None, *, region: str | None = None
) -> PriceTable:
    """The table a preview prices with: the admin's override if there is
    one, else the built-in table for `region` (None: the file's default).

    `override` has the API's ``PriceTable`` shape (portal-api.yaml). A kind
    with no built-in table (``s3`` today) gets an empty one, so the preview
    names the missing prices instead of failing.
    """
    if override is not None:
        if not isinstance(override, Mapping):
            raise PriceTableError("an override must be an object")
        kind = override.get("backend_kind", backend_kind)
        if kind != backend_kind:
            raise PriceTableError(f"override is for {kind!r}, not {backend_kind!r}")
        as_of = _as_of(override.get("as_of"), "as_of")
        reg = override.get("region", region)
        verify = override.get("verify")
        if not isinstance(verify, bool):
            raise PriceTableError("verify must be true or false")
        classes = _classes(override.get("classes"), "classes")
        notes = override.get("notes") or ()
        if not isinstance(notes, list | tuple) or not all(isinstance(n, str) for n in notes):
            raise PriceTableError("notes must be a list of strings")
        return PriceTable(
            id=str(override.get("id") or f"{backend_kind}/{reg or 'any'}/override/{as_of}"),
            backend_kind=backend_kind,
            region=reg,
            currency=_currency(override.get("currency")),
            as_of=as_of,
            classes=classes,
            verify=verify or any(c.verify for c in classes.values()),
            overridden=True,
            source=override.get("source"),
            notes=tuple(notes),
        )

    name = TABLE_FOR_KIND.get(backend_kind)
    if name is None:
        if backend_kind != "s3":
            raise PriceTableError(f"unknown backend kind {backend_kind!r}")
        return PriceTable(
            id=f"{backend_kind}/none",
            backend_kind=backend_kind,
            region=region,
            currency="USD",
            as_of=date(2026, 10, 6),
            classes={},
            verify=True,
            notes=(f"No built-in prices for {backend_kind}; an ArchiveAdmin can set them.",),
        )
    raw = builtin_table(name)
    reg = region or raw["default_region"]
    regions = raw["regions"]
    if reg not in regions:
        raise PriceTableError(f"no {name} prices for region {reg!r}; known: {sorted(regions)}")
    classes = _classes(regions[reg]["classes"], f"regions.{reg}.classes")
    as_of = _as_of(raw["as_of"], "as_of")
    return PriceTable(
        id=f"{backend_kind}/{reg}/{as_of}",
        backend_kind=backend_kind,
        region=reg,
        currency=_currency(raw["currency"]),
        as_of=as_of,
        classes=classes,
        verify=bool(raw.get("verify", True)) or any(c.verify for c in classes.values()),
        source=raw.get("source"),
        notes=tuple(raw.get("notes", ())),
    )


# --- the cost model --------------------------------------------------------


@dataclass(frozen=True)
class CostItem:
    item: str  # storage | transitions | archive_copy | read_back | early_deletion | ...
    tier: LogicalTier | None
    amount: Money
    note: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "item": self.item,
            "tier": None if self.tier is None else self.tier.value,
            "amount": self.amount.to_json(),
            "note": self.note,
        }


@dataclass(frozen=True)
class RecallCost:
    priority: str  # standard | high
    per_tb: Money
    hours: tuple[float, float]

    def to_json(self) -> dict[str, Any]:
        return {
            "priority": self.priority,
            "per_tb": self.per_tb.to_json(),
            "hours": list(self.hours),
        }


@dataclass(frozen=True)
class CostEstimate:
    per_tb_first_year: Money
    keep_hot_per_tb_first_year: Money
    saving: Money
    breakdown: tuple[CostItem, ...]
    recall_per_tb: tuple[RecallCost, ...]
    assumptions: CostAssumptions
    prices: PriceTable
    #: Native classes (with account role) the table has no price for. Their
    #: cost is left out; the preview says so (check ``price_missing``).
    missing: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "per_tb_first_year": self.per_tb_first_year.to_json(),
            "keep_hot_per_tb_first_year": self.keep_hot_per_tb_first_year.to_json(),
            "saving": self.saving.to_json(),
            "breakdown": [i.to_json() for i in self.breakdown],
            "recall_per_tb": [r.to_json() for r in self.recall_per_tb],
            "assumptions": self.assumptions.to_json(),
            "price_table": self.prices.ref(),
        }


def account_for(layout: str, tier: LogicalTier) -> str:
    """The account role holding a tier: ``live``/``archive`` on the
    two-account layout, else ``single``."""
    if layout == "two_account":
        return "archive" if tier is LogicalTier.ARCHIVE else "live"
    return "single"


def tier_info(caps: Capabilities, tier: LogicalTier) -> TierInfo | None:
    infos = caps.tiers.get(tier)
    return infos[0] if infos else None


def archive_restore_options(caps: Capabilities) -> list[RestoreOption]:
    """Restore options for the default archive class, in caps order."""
    arch = tier_info(caps, LogicalTier.ARCHIVE)
    if arch is None:
        return []
    return [o for o in caps.restore_options if arch.native_class in o.from_classes]


class _Pricer:
    """Looks prices up and remembers what was missing."""

    def __init__(self, prices: PriceTable) -> None:
        self.prices = prices
        self.missing: list[str] = []

    def get(self, native: str, account: str, attr: str) -> Decimal:
        cp = self.prices.price(native, account)
        v = None if cp is None else getattr(cp, attr)
        if v is None:
            label = f"{native} ({account} account): {attr}"
            if label not in self.missing:
                self.missing.append(label)
            return Decimal(0)
        return v

    def native(self, caps: Capabilities, tier: LogicalTier) -> str:
        info = tier_info(caps, tier)
        return info.native_class if info else tier.value


def early_deletion_per_gb(
    pricer: _Pricer, native: str, account: str, days_short: Decimal | int
) -> Decimal:
    """Leaving a class `days_short` days before its minimum: charged as if it stayed."""
    return pricer.get(native, account, "gb_month") * Decimal(days_short) / DAYS_PER_MONTH


def _phases(policy: Policy, end: int) -> list[tuple[int, int, LogicalTier]]:
    """(from_day, to_day, tier) within [0, end)."""
    marks = [(0, policy.start_tier), *policy.steps()]
    out = []
    for i, (day, tier) in enumerate(marks):
        stop = marks[i + 1][0] if i + 1 < len(marks) else end
        lo, hi = min(day, end), min(stop, end)
        if hi > lo:
            out.append((lo, hi, tier))
    return out


def _sum(items: list[CostItem]) -> tuple[Decimal, Decimal]:
    lo = hi = Decimal(0)
    for i in items:
        lo += i.amount.amount
        hi += i.amount.upper
    return lo, hi


def keep_hot_per_tb(ctx: PreviewContext, pricer: _Pricer | None = None) -> Decimal:
    """A year in Hot, in the account new data lands in."""
    p = pricer or _Pricer(ctx.prices)
    hot = p.native(ctx.caps, LogicalTier.HOT)
    acct = account_for(ctx.layout, LogicalTier.HOT)
    return GB_PER_TB * p.get(hot, acct, "gb_month") * YEAR_DAYS / DAYS_PER_MONTH


def keep_hot_estimate(ctx: PreviewContext) -> CostEstimate:
    """The estimate shown for a body that can't run: the data stays Hot."""
    pricer = _Pricer(ctx.prices)
    cur = ctx.prices.currency
    hot = Money(keep_hot_per_tb(ctx, pricer), cur)
    return CostEstimate(
        per_tb_first_year=hot,
        keep_hot_per_tb_first_year=hot,
        saving=Money(Decimal(0), cur),
        breakdown=(),
        recall_per_tb=(),
        assumptions=ctx.assumptions,
        prices=ctx.prices,
        missing=tuple(pricer.missing),
    )


def objects_per_tb(mean_file_bytes: int) -> Decimal:
    return Decimal(TB_BYTES) / Decimal(mean_file_bytes)


def _recall_price(
    caps: Capabilities, pricer: _Pricer, layout: str, speed: RestoreSpeed, gb: Decimal, ops: Decimal
) -> Decimal:
    """Rehydrate by copy: retrieval and the archive read at the speed's
    rates, plus the write of the copy (Hot) in the archive's account.
    `ops` is in units of 10,000 operations."""
    arch = pricer.native(caps, LogicalTier.ARCHIVE)
    hot = pricer.native(caps, LogicalTier.HOT)
    acct = account_for(layout, LogicalTier.ARCHIVE)
    sfx = "_high" if speed is RestoreSpeed.HIGH else ""
    per_gb = pricer.get(arch, acct, f"retrieval_per_gb{sfx}")
    read = pricer.get(arch, acct, f"read_per_10k{sfx}")
    write = pricer.get(hot, acct, "write_per_10k")
    return gb * per_gb + ops * (read + write)


def _recall_per_tb(
    caps: Capabilities, pricer: _Pricer, layout: str, ops: Decimal
) -> list[RecallCost]:
    out: list[RecallCost] = []
    seen: set[RestoreSpeed] = set()
    for o in archive_restore_options(caps):
        if o.speed in seen or o.speed not in (RestoreSpeed.STANDARD, RestoreSpeed.HIGH):
            continue
        seen.add(o.speed)
        amount = _recall_price(caps, pricer, layout, o.speed, GB_PER_TB, ops)
        out.append(
            RecallCost(o.speed.value, Money(amount, pricer.prices.currency), o.typical_hours)
        )
    return out


def first_year_cost(policy: Policy, ctx: PreviewContext) -> CostEstimate:
    """First-year cost per TB under `policy`, against keeping it Hot."""
    caps, layout, a = ctx.caps, ctx.layout, ctx.assumptions
    pricer = _Pricer(ctx.prices)
    cur = ctx.prices.currency
    ops = objects_per_tb(a.mean_file_bytes) / 10_000
    native = lambda t: pricer.native(caps, t)  # noqa: E731
    hot, cold = native(LogicalTier.HOT), native(LogicalTier.COLD)
    items: list[CostItem] = []

    # Storage, by tier.
    storage: dict[LogicalTier, tuple[Decimal, Decimal]] = {}
    smart_months = Decimal(0)
    for lo, hi, tier in _phases(policy, YEAR_DAYS):
        months = Decimal(hi - lo) / DAYS_PER_MONTH
        acct = account_for(layout, tier)
        if tier is LogicalTier.SMART:
            low = GB_PER_TB * pricer.get(cold, acct, "gb_month") * months
            high = GB_PER_TB * pricer.get(hot, acct, "gb_month") * months
            smart_months += months
        else:
            low = high = GB_PER_TB * pricer.get(native(tier), acct, "gb_month") * months
        was = storage.get(tier, (Decimal(0), Decimal(0)))
        storage[tier] = (was[0] + low, was[1] + high)
    for tier, (low, high) in storage.items():
        acct = account_for(layout, tier)
        note = (
            f"billed at the {cold} rate (low) to the {hot} rate (high): Microsoft moves Smart "
            "blobs among Hot, Cool and Cold by use"
            if tier is LogicalTier.SMART
            else f"{native(tier)}, {acct} account"
        )
        items.append(CostItem("storage", tier, Money.of(low, high, cur), note))

    if smart_months and a.mean_file_bytes > SMART_MONITORED_ABOVE:
        fee = pricer.get(native(LogicalTier.SMART), "live", "monitoring_per_10k_objects_month")
        items.append(
            CostItem(
                "smart_monitoring",
                LogicalTier.SMART,
                Money(ops * fee * smart_months, cur),
                "per 10,000 objects over 128 KiB per month",
            )
        )

    # Moves: transitions, the two-account archive copy, early deletion.
    transitions = copy = read_back = early = Decimal(0)
    early_notes: list[str] = []
    if (
        policy.mode is Mode.SCHEDULED
        and layout == "two_account"
        and LogicalTier.SMART in caps.tiers
    ):
        # Uploads land in Smart (the live account's default); a scheduled
        # policy sets Hot at once (policy.engine).
        transitions += ops * pricer.get(hot, "live", "write_per_10k")
    two_account_archive = layout == "two_account" and caps.archive_needs_separate_account
    prev_day, prev = 0, policy.start_tier
    for day, tier in policy.steps():
        if day >= YEAR_DAYS:
            break
        src_acct = account_for(layout, prev)
        # Smart reads are priced as Hot reads; Smart has no retrieval charge [V].
        src = hot if prev is LogicalTier.SMART else native(prev)
        info = tier_info(caps, prev)
        held = day - prev_day
        if info is not None and info.minimum_days and held < info.minimum_days:
            short = info.minimum_days - held
            early += GB_PER_TB * early_deletion_per_gb(pricer, info.native_class, src_acct, short)
            early_notes.append(f"{info.native_class} left after {held} of {info.minimum_days} days")
        if tier is LogicalTier.ARCHIVE and two_account_archive:
            copy += ops * (
                pricer.get(hot, "archive", "write_per_10k")
                + pricer.get(src, src_acct, "read_per_10k")
            )
            if prev is not LogicalTier.SMART:
                copy += GB_PER_TB * pricer.get(src, src_acct, "retrieval_per_gb")
            read_back += ops * pricer.get(hot, "archive", "read_per_10k")
            read_back += GB_PER_TB * pricer.get(hot, "archive", "retrieval_per_gb")
            transitions += ops * pricer.get(native(tier), "archive", "write_per_10k")
        else:
            transitions += ops * pricer.get(
                native(tier), account_for(layout, tier), "write_per_10k"
            )
        prev_day, prev = day, tier
    if transitions:
        items.append(
            CostItem(
                "transitions",
                None,
                Money(transitions, cur),
                "each move is a write at the destination tier, per object",
            )
        )
    if copy:
        items.append(
            CostItem(
                "archive_copy",
                LogicalTier.ARCHIVE,
                Money(copy, cur),
                "Copy Blob into the archive account (lands in Hot)",
            )
        )
    if read_back:
        items.append(
            CostItem(
                "read_back",
                LogicalTier.ARCHIVE,
                Money(read_back, cur),
                "the copy is read back and checked (CRC64) before the original is deleted",
            )
        )
    if early:
        items.append(CostItem("early_deletion", None, Money(early, cur), "; ".join(early_notes)))

    recalls = _recall_per_tb(caps, pricer, layout, ops)
    frac = Decimal(str(a.recall_fraction_per_year))
    if frac and policy.archive_day < YEAR_DAYS:
        std = next((r for r in recalls if r.priority == RestoreSpeed.STANDARD.value), None)
        if std is not None:
            share = frac * Decimal(YEAR_DAYS - policy.archive_day) / YEAR_DAYS
            items.append(
                CostItem(
                    "recall",
                    LogicalTier.ARCHIVE,
                    Money(share * std.per_tb.amount, cur),
                    f"{a.recall_fraction_per_year:g} of the data a year, recalled once at Standard",
                )
            )

    low, high = _sum(items)
    keep_hot = keep_hot_per_tb(ctx, pricer)
    return CostEstimate(
        per_tb_first_year=Money.of(low, high, cur),
        keep_hot_per_tb_first_year=Money(keep_hot, cur),
        saving=Money.of(keep_hot - high, keep_hot - low, cur),
        breakdown=tuple(items),
        recall_per_tb=tuple(recalls),
        assumptions=a,
        prices=ctx.prices,
        missing=tuple(pricer.missing),
    )


def _pin_layout(current: PinFacts) -> str:
    return "two_account" if current.smart_layout else "single_account"


def recall_cost(
    caps: Capabilities, prices: PriceTable, layout: str, size: int, speed: RestoreSpeed
) -> Money | None:
    """What one recall of `size` bytes costs at `speed`; None if a price is missing."""
    pricer = _Pricer(prices)
    amount = _recall_price(caps, pricer, layout, speed, Decimal(size) / GiB, Decimal(1) / 10_000)
    return None if pricer.missing else Money(amount, prices.currency)


def early_deletion_fee(
    prices: PriceTable, native_class: str, account: str, size: int, days_short: Decimal
) -> Money | None:
    """Leaving `native_class` `days_short` days early; None if a price is missing."""
    pricer = _Pricer(prices)
    per_gb = early_deletion_per_gb(pricer, native_class, account, days_short)
    return None if pricer.missing else Money(per_gb * Decimal(size) / GiB, prices.currency)


def own_copy_read_fee(current: PinFacts, ctx: PreviewContext) -> Money | None:
    """What reading a file's blob once costs, when it leaves for a copy of its own
    (decision 66). Smart has no retrieval charge [V] and no row of its own in the price
    table. None if a price is missing."""
    pricer = _Pricer(ctx.prices)
    layout = _pin_layout(current)
    native = current.native_class or pricer.native(ctx.caps, current.logical_tier)
    if current.logical_tier is LogicalTier.SMART:
        return Money(Decimal(0), ctx.prices.currency)
    acct = account_for(layout, current.logical_tier)
    per_gb = pricer.get(native, acct, "retrieval_per_gb")
    if pricer.missing:
        return None
    return Money(per_gb * Decimal(current.size) / GiB, ctx.prices.currency)


def pin_cost_change(target: LogicalTier, current: PinFacts, ctx: PreviewContext) -> Money | None:
    """The monthly storage cost change of pinning one file to `target`.

    A warmer pin on an archived file recalls a copy to the edge and the
    archived blob stays, so its cloud cost doesn't change. A Smart file is
    priced as a range (Cold to Hot). A file sharing its blob pays for a whole new
    blob, because the shared one stays for the other file. None if a price is missing.
    """
    cur = ctx.prices.currency
    if current.archived:
        return Money(Decimal(0), cur)
    pricer = _Pricer(ctx.prices)
    layout = _pin_layout(current)
    gb = Decimal(current.size) / GiB
    new = pricer.get(pricer.native(ctx.caps, target), account_for(layout, target), "gb_month")
    if current.shares_blob and target is not current.logical_tier:
        return None if pricer.missing else Money(gb * new, cur)
    acct = account_for(layout, current.logical_tier)
    if current.logical_tier is LogicalTier.SMART:
        hot = pricer.get(pricer.native(ctx.caps, LogicalTier.HOT), acct, "gb_month")
        cold = pricer.get(pricer.native(ctx.caps, LogicalTier.COLD), acct, "gb_month")
        change = Money.of(gb * (new - hot), gb * (new - cold), cur)
    else:
        native = current.native_class or pricer.native(ctx.caps, current.logical_tier)
        change = Money(gb * (new - pricer.get(native, acct, "gb_month")), cur)
    return None if pricer.missing else change
