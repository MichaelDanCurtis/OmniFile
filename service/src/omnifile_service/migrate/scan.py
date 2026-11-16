"""Scan a source before registering it (docs/design/cloud-connect.md section 4).

The worker reads the prefix with the seed's own readers (`sources.run.reader_for`:
the listing, or an S3 Inventory), so a scan lists and reads an inventory and
never writes anything, the source least of all (decision 40). One batch per
queue message; the running totals are written after each, so the portal shows
progress. It stops at `max_objects` or `max_seconds` (``capped``).

`Tally` adds a batch up; `result` turns the totals into what the wizard shows:
first-level prefixes, the storage-class mix, a restore estimate (Bulk, waves
of `WAVE_MAX`, AWS's stated hours per class, the 1,000 requests a second user
recalls share, and the S3 price table when an ArchiveAdmin set one), objects
over 10 GiB (no time is promised for them), and suggested waves.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from ..backends.base import LogicalTier, RestoreSpeed
from ..backends.sim.capabilities import S3_RESTORE_OPTIONS, S3_RESTORE_REQUESTS_PER_SECOND
from .waves import WAVE_CLASSES, WAVE_MAX, WAVE_SPEED

GiB = 1024**3
TiB = 1024 * GiB
#: The 10 GiB rule: above this no restore or copy time is promised (anti-pattern 5).
NO_PROMISE_BYTES = 10 * GiB
#: A suggested wave holds at most this many bytes to restore.
WAVE_BYTES = 50 * TiB
BATCH = 10_000
DEFAULT_MAX_OBJECTS = 2_000_000
DEFAULT_MAX_SECONDS = 300
#: Distinct first-level prefixes kept; the rest are summed under OTHER.
MAX_PREFIXES = 10_000
SHOWN_PREFIXES = 200
OTHER = "(other)"
#: S3's LIST price per 1,000 requests (USD; VERIFY RW-35), for the scan's own cost.
LIST_PER_1000 = Decimal("0.005")
LIST_PAGE = 1000


def needs_restore(native_class: str | None, logical_tier: Any) -> bool:
    if native_class in WAVE_CLASSES:
        return True
    return logical_tier == LogicalTier.ARCHIVE or getattr(logical_tier, "value", None) == "archive"


def first_level(key: str, prefix: str) -> str:
    rest = key[len(prefix) :] if key.startswith(prefix) else key
    head, sep, _ = rest.partition("/")
    return f"{prefix}{head}/" if sep else prefix or "(top level)"


@dataclass
class Tally:
    objects: int = 0
    bytes: int = 0
    #: prefix -> [objects, bytes, restore objects, restore bytes]
    prefixes: dict[str, list[int]] = field(default_factory=dict)
    #: class -> [objects, bytes]
    classes: dict[str, list[int]] = field(default_factory=dict)
    large: list[int] = field(default_factory=lambda: [0, 0])

    @classmethod
    def from_json(cls, d: Mapping[str, Any] | None) -> Tally:
        d = d or {}
        return cls(
            int(d.get("objects", 0)),
            int(d.get("bytes", 0)),
            {k: list(v) for k, v in (d.get("prefixes") or {}).items()},
            {k: list(v) for k, v in (d.get("classes") or {}).items()},
            list(d.get("large") or [0, 0]),
        )

    def to_json(self) -> dict[str, Any]:
        return {"objects": self.objects, "bytes": self.bytes, "prefixes": self.prefixes,
                "classes": self.classes, "large": self.large}  # fmt: skip

    def add(self, objects: Iterable[Any], prefix: str) -> None:
        for o in objects:
            size = int(o.size or 0)
            cls_ = o.native_class or "STANDARD"
            restore = needs_restore(o.native_class, o.logical_tier)
            self.objects += 1
            self.bytes += size
            p = first_level(o.key, prefix)
            if p not in self.prefixes and len(self.prefixes) >= MAX_PREFIXES:
                p = OTHER
            row = self.prefixes.setdefault(p, [0, 0, 0, 0])
            row[0] += 1
            row[1] += size
            if restore:
                row[2] += 1
                row[3] += size
            c = self.classes.setdefault(cls_, [0, 0])
            c[0] += 1
            c[1] += size
            if size > NO_PROMISE_BYTES:
                self.large[0] += 1
                self.large[1] += size


def _bulk_hours(native_class: str) -> float | None:
    for o in S3_RESTORE_OPTIONS:
        if o.speed is RestoreSpeed.BULK and native_class in o.from_classes:
            return o.typical_hours[1]
    return None


def _cost(classes: Mapping[str, list[int]], prices: Any) -> tuple[Decimal | None, str | None]:
    """Bulk retrieval per GB plus requests (`read_per_10k` stands for the restore
    requests' price) for each archived class. None when a price is missing."""
    if prices is None:
        return None, None
    total = Decimal(0)
    for name, (n, size) in classes.items():
        if name not in WAVE_CLASSES:
            continue
        p = prices.price(name)
        if p is None or p.retrieval_per_gb is None or p.read_per_10k is None:
            return None, prices.currency
        total += Decimal(size) / GiB * p.retrieval_per_gb + Decimal(n) / 10_000 * p.read_per_10k
    return total, prices.currency


def suggest_waves(prefixes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copy-only prefixes first, then the rest packed in order into waves of
    at most WAVE_MAX objects and WAVE_BYTES to restore."""
    waves: list[dict[str, Any]] = []
    copy_only = [p for p in prefixes if p["restore_objects"] == 0]
    if copy_only:
        waves.append(_wave(1, copy_only, True))
    cur: list[dict[str, Any]] = []
    n = b = 0
    for p in (p for p in prefixes if p["restore_objects"] > 0):
        if cur and (n + p["restore_objects"] > WAVE_MAX or b + p["restore_bytes"] > WAVE_BYTES):
            waves.append(_wave(len(waves) + 1, cur, False))
            cur, n, b = [], 0, 0
        cur.append(p)
        n += p["restore_objects"]
        b += p["restore_bytes"]
    if cur:
        waves.append(_wave(len(waves) + 1, cur, False))
    return waves


def _wave(i: int, ps: list[dict[str, Any]], copy_only: bool) -> dict[str, Any]:
    return {
        "wave": i,
        "prefixes": [p["prefix"] for p in ps],
        "objects": sum(p["objects"] for p in ps),
        "bytes": sum(p["bytes"] for p in ps),
        "restore_objects": sum(p["restore_objects"] for p in ps),
        "restore_bytes": sum(p["restore_bytes"] for p in ps),
        "copy_only": copy_only,
    }


def result(t: Tally, prices: Any = None) -> dict[str, Any]:
    rows = [
        {"prefix": k, "objects": v[0], "bytes": v[1], "restore_objects": v[2],
         "restore_bytes": v[3]}
        for k, v in t.prefixes.items()
    ]  # fmt: skip
    rows.sort(key=lambda r: (-r["bytes"], r["prefix"]))
    shown = rows[:SHOWN_PREFIXES]
    rest = rows[SHOWN_PREFIXES:]
    if rest:
        shown.append({
            "prefix": OTHER,
            **{k: sum(r[k] for r in rest) for k in ("objects", "bytes", "restore_objects",
                                                     "restore_bytes")},
        })  # fmt: skip
    classes = [
        {"storage_class": k, "objects": v[0], "bytes": v[1], "restore_needed": k in WAVE_CLASSES}
        for k, v in sorted(t.classes.items(), key=lambda kv: -kv[1][1])
    ]
    r_obj = sum(r["restore_objects"] for r in rows)
    r_bytes = sum(r["restore_bytes"] for r in rows)
    hours = [h for c in t.classes if c in WAVE_CLASSES and (h := _bulk_hours(c)) is not None]
    cost, currency = _cost(t.classes, prices)
    notes = [
        f"Restores are {WAVE_SPEED.value} (the cheapest), in waves of at most {WAVE_MAX:,}"
        " objects; each wave takes up to AWS's stated time for its slowest class.",
        f"Restore requests share {S3_RESTORE_REQUESTS_PER_SECOND:,} a second with users' recalls.",
    ]
    if t.large[0]:
        notes.append(f"{t.large[0]:,} objects are over 10 GiB: no time is promised for them.")
    if r_obj and cost is None:
        notes.append(
            "No S3 prices are set (Settings, Prices and alerts), so the restore cost isn't"
            " estimated."
        )
    return {
        "objects": t.objects,
        "bytes": t.bytes,
        "prefixes": shown,
        "classes": classes,
        "restore": {
            "objects": r_obj,
            "bytes": r_bytes,
            "waves": math.ceil(r_obj / WAVE_MAX) if r_obj else 0,
            "hours": max(hours) if hours else None,
            "request_seconds": round(r_obj / S3_RESTORE_REQUESTS_PER_SECOND, 1),
            "cost": None if cost is None else str(cost.quantize(Decimal("0.01"))),
            "currency": currency,
            "prices_set": cost is not None,
            "notes": notes,
        },
        "large_objects": {"objects": t.large[0], "bytes": t.large[1]},
        "waves": suggest_waves(rows),
    }


def caps_of(params: Mapping[str, Any]) -> dict[str, int]:
    return {
        "max_objects": int(params.get("max_objects") or DEFAULT_MAX_OBJECTS),
        "max_seconds": int(params.get("max_seconds") or DEFAULT_MAX_SECONDS),
    }


def list_cost(objects: int) -> Decimal:
    """What the scan's own LIST requests cost at S3's list price."""
    return Decimal(math.ceil(objects / LIST_PAGE)) / 1000 * LIST_PER_1000
