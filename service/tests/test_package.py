import re
from pathlib import Path

import omnifile_service

VERSION = Path(__file__).resolve().parents[2] / "VERSION"


def pep440(semver: str) -> str:
    """The repository's SemVer VERSION as Python spells it (release/stamp.py):
    1.2.0-rc.1 is 1.2.0rc1."""
    m = re.fullmatch(r"(\d+\.\d+\.\d+)(?:-(alpha|beta|rc)\.(\d+))?", semver.strip())
    assert m, f"VERSION {semver!r} is not a release version"
    pre = {"alpha": "a", "beta": "b", "rc": "rc"}
    return m.group(1) + (pre[m.group(2)] + m.group(3) if m.group(2) else "")


def test_version_is_the_repositorys() -> None:
    assert omnifile_service.__version__ == pep440(VERSION.read_text())
