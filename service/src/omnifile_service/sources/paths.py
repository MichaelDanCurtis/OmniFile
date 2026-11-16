"""Object key -> catalog path (docs/design/catalog-seeding.md section 2)."""

from __future__ import annotations

from dataclasses import dataclass

NAME_MAX = 255  # bytes, as xfs and NFS allow


class Unmappable(ValueError):
    """The key can't be a POSIX path under the source."""


@dataclass(frozen=True)
class Placement:
    #: Folder names from the dataset root, the source's `path` included.
    folders: tuple[str, ...]
    #: The file's name, or None for a folder marker (`a/b/` of size 0).
    name: str | None


def split_path(path: str) -> tuple[str, ...]:
    """A source's `path` ('' or 'a/b') as names, each a valid POSIX name."""
    if path == "":
        return ()
    names = tuple(path.split("/"))
    for n in names:
        check_name(n)
    return names


def check_name(name: str) -> None:
    if name in ("", ".", ".."):
        raise Unmappable(f"{name!r} is not a file name")
    if "\x00" in name:
        raise Unmappable("a name can't contain NUL")
    if len(name.encode()) > NAME_MAX:
        raise Unmappable(f"a name is longer than {NAME_MAX} bytes")


def place(key: str, prefix: str, base: tuple[str, ...], size: int) -> Placement:
    """Where an object of the source goes. Raises `Unmappable`."""
    if not key.startswith(prefix):
        raise Unmappable(f"{key!r} is outside the source prefix {prefix!r}")
    rel = key[len(prefix) :]
    if rel == "":
        raise Unmappable("the prefix itself is not a file")
    if rel.endswith("/"):
        if size != 0:
            raise Unmappable("a key ending in '/' with data can't be a folder")
        names = rel[:-1].split("/")
        for n in names:
            check_name(n)
        return Placement(base + tuple(names), None)
    names = rel.split("/")
    for n in names:
        check_name(n)
    return Placement(base + tuple(names[:-1]), names[-1])
