"""Nightly reconcile of backends against the catalog (M3).

Blob Inventory reports (`blob_inventory.py`, CSV or Parquet) on non-WORM
accounts, or a listing when none is configured or usable; the change feed
plus listings on WORM accounts, where immutability blocks Inventory [W].
Differences become `catalog.reconcile_drift` rows; nothing is repaired
automatically.
"""

from .drift import DRIFT_KINDS, open_drift, record_drift
from .inventory import (
    InventoryItem,
    InventorySource,
    ListingInventory,
    PrefixedInventory,
    choose_inventory,
    inventory_for,
)
from .run import (
    ReconcileReport,
    SourcesSeeding,
    known_containers,
    reconcile_container,
    reconcile_due,
    register_known_containers,
    source_prefixes,
)

__all__ = [
    "DRIFT_KINDS",
    "InventoryItem",
    "InventorySource",
    "ListingInventory",
    "PrefixedInventory",
    "ReconcileReport",
    "SourcesSeeding",
    "choose_inventory",
    "inventory_for",
    "known_containers",
    "open_drift",
    "reconcile_container",
    "reconcile_due",
    "record_drift",
    "register_known_containers",
    "source_prefixes",
]
