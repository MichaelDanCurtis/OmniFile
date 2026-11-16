"""The one rule for deleting an original after a copy replaced it.

Every delete of an archive move's original goes through this check: the
tiering worker's (`retention.worm_sync.delete_blob`, against the catalog) and
a backend's own move (`set_class(ARCHIVE)` on Azure's two-account live
account, and sim's, against the original's properties). The copy is in
Archive by then and can't be read, so only its properties can be compared:

- its length must match;
- its CRC64-NVME must match where the provider keeps a whole-object one
  (S3 does; Azure's Get Blob Properties has none [V]);
- its Content-MD5 must match where both sides have one;
- a copy whose properties carry neither checksum must have been read back in
  the provider before it went to Archive (`read_back`).

`tests/retention/test_archive_copy_check.py` checks that no path deletes an
original without it.
"""

from __future__ import annotations

from .base import ObjectHead


def copy_mismatch(
    *,
    size: int | None,
    crc64nvme: bytes | None,
    md5: bytes | None,
    copy: ObjectHead,
    read_back: bool,
) -> str | None:
    """Why `copy` can't replace an original of `size`, `crc64nvme` and `md5`
    (what is known of it; None where unknown): ``size``, ``crc64``, ``md5``
    or ``unverified``. None when it can."""
    if size is not None and copy.size != size:
        return "size"
    checked = False
    if copy.crc64nvme is not None and crc64nvme is not None:
        if bytes(copy.crc64nvme) != bytes(crc64nvme):
            return "crc64"
        checked = True
    if copy.md5 is not None and md5 is not None:
        if bytes(copy.md5) != bytes(md5):
            return "md5"
        checked = True
    if not checked and not read_back:
        return "unverified"
    return None


__all__ = ["copy_mismatch"]
