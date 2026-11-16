#!/usr/bin/env python3
"""Check SBOMs against the CycloneDX 1.6 JSON schema, with the official CycloneDX library's strict validator.

    release/validate_sbom.py FILE...

Re-runs itself under `uv run --no-project --with cyclonedx-python-lib[json-validation]==PIN` unless that library
is already importable, so it needs uv (and PyPI, or uv's cache, the first time). Exits 1 if any file is invalid.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

PIN = "11.12.0"  # cyclonedx-python-lib, Apache-2.0 (release/tools.toml)


def main(argv: list[str]) -> int:
    try:
        from cyclonedx.schema import SchemaVersion
        from cyclonedx.validation.json import JsonStrictValidator
    except ImportError:
        if os.environ.get("OMNIFILE_VALIDATE_REEXEC"):
            print("validate_sbom: cyclonedx-python-lib is not importable", file=sys.stderr)
            return 2
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import pinned

        cmd = [pinned.uv(), "run", "--quiet", "--no-project", "--isolated", "--with",
               f"cyclonedx-python-lib[json-validation]=={PIN}", "python", __file__, *argv]
        return subprocess.run(cmd, env={**os.environ, "OMNIFILE_VALIDATE_REEXEC": "1"}).returncode
    import importlib.metadata

    have = importlib.metadata.version("cyclonedx-python-lib")
    if have != PIN:
        print(f"validate_sbom: cyclonedx-python-lib {have}, not the pinned {PIN}", file=sys.stderr)
        return 2
    validator = JsonStrictValidator(SchemaVersion.V1_6)
    bad = 0
    for f in argv:
        text = open(f).read()
        if json.loads(text).get("specVersion") != "1.6":
            print(f"INVALID {f}: specVersion is not 1.6")
            bad += 1
            continue
        err = validator.validate_str(text)
        if err:
            print(f"INVALID {f}: {err}")
            bad += 1
        else:
            print(f"valid   {f} (CycloneDX 1.6)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
