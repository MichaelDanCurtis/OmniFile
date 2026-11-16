"""The Azure backend's POSIX metadata uses the edge's format (M1 open item).

The edge (`edge/crates/omnifile-blob/src/client.rs`, `PosixMeta`) writes
``file_owner`` / ``file_group`` as decimal ids and ``file_permissions`` as
4-digit octal (``0644``), and parses them back the same way. Whatever the
service writes must round-trip through that parser.
"""

from __future__ import annotations

import pytest

from omnifile_service.backends.azure import (
    from_azure_metadata,
    normalize_posix,
    posix_metadata,
    to_azure_metadata,
)
from omnifile_service.backends.base import BackendError

# The values the edge's own unit test produces for uid 41023, gid 100,
# mode 0o100644 (`posix_meta_names_are_identifiers_and_round_trip`).
EDGE_WIRE = {"file_owner": "41023", "file_group": "100", "file_permissions": "0644"}


def edge_parse(meta: dict[str, str]) -> tuple[int, int, int]:
    """`PosixMeta::from_metadata`, in Python: decimal ids, octal mode."""
    return (
        int(meta["file_owner"], 10),
        int(meta["file_group"], 10),
        int(meta["file_permissions"], 8),
    )


def test_posix_metadata_matches_the_edge_byte_for_byte():
    assert to_azure_metadata(posix_metadata(41023, 100, 0o100644)) == EDGE_WIRE


@pytest.mark.parametrize(
    ("mode", "wire"),
    [(0o644, "0644"), (0o4755, "4755"), (0o7, "0007"), (0o100600, "0600"), (0, "0000")],
)
def test_permissions_are_four_digit_octal(mode, wire):
    meta = to_azure_metadata(posix_metadata(1, 2, mode))
    assert meta["file_permissions"] == wire
    assert edge_parse(meta) == (1, 2, mode & 0o7777)


@pytest.mark.parametrize(
    ("given", "wire"), [("644", "0644"), ("0o640", "0640"), ("0640", "0640"), ("00644", "0644")]
)
def test_other_octal_spellings_are_normalised(given, wire):
    meta = to_azure_metadata({"file-owner": "0", "file-group": "0", "file-permissions": given})
    assert meta["file_permissions"] == wire


@pytest.mark.parametrize(
    "bad",
    [
        {"file-owner": "0x10"},
        {"file-owner": "-1"},
        {"file-group": "alice"},
        {"file-group": str(2**32)},
        {"file-permissions": "rw-r--r--"},
        {"file-permissions": "0989"},
        {"file-permissions": ""},
    ],
)
def test_values_the_edge_cannot_parse_are_refused(bad):
    with pytest.raises(BackendError):
        to_azure_metadata(bad)


def test_round_trip_and_other_metadata_untouched():
    meta = {**posix_metadata(20001, 10000, 0o640), "content_note": "x"}
    wire = to_azure_metadata(meta)
    assert wire["content_note"] == "x"
    assert from_azure_metadata(wire) == normalize_posix(meta)
    assert edge_parse(wire) == (20001, 10000, 0o640)
