"""The release tools' pins (release/tools.toml), checked where a script runs them.

    uv()  the uv to run: $UV, else the first `uv` on PATH, refused unless `uv --version` is tools.toml's pin
          (a newer uv reads another cache format, so an offline dry run fails in ways that look like missing
          packages; and a lock is only reproducible with the uv that wrote it).
"""

from __future__ import annotations

import functools
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

TOOLS = Path(__file__).resolve().parent / "tools.toml"


def pin(name: str) -> str:
    return tomllib.loads(TOOLS.read_text())["tool"][name]["version"]


@functools.cache
def uv() -> str:
    exe = os.environ.get("UV") or shutil.which("uv")
    if not exe:
        raise SystemExit("no uv: install the pinned one (release/tools.toml) or set $UV")
    out = subprocess.run([exe, "--version"], capture_output=True, text=True).stdout
    m = re.match(r"uv (\S+)", out)
    want = pin("uv")
    if not m or m.group(1) != want:
        raise SystemExit(f"{exe} is uv {m.group(1) if m else '(unknown)'}, not the pinned {want} (release/tools.toml): "
                         "put the pinned uv first on PATH, or set $UV")
    return exe
