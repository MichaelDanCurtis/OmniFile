#!/usr/bin/env python3
"""Write a release's image digests into the Bicep parameters, and check the file compiles.

    release/bicep_params.py --lock deploy/images/images.lock.json --registry REGISTRY --version V \\
        --example deploy/azure/main.example.bicepparam --out omnifile-images-V.bicepparam [--compile deploy/azure]

The output is the example parameters file with the registry and the six image parameters (apiImage, enrolImage,
workerImage, verifierImage, webImage, relayImage) set to <registry>/<repository>@<digest>, by the lock's `apps`
map (api, enrol, worker and the verifier run the service image). Every other line is the example's, for the
client to fill in as the deployment guide says. A parameter the example lacks, or an image without a sha256
digest, is refused.

--compile DIR copies the Bicep sources (main.bicep, modules/, bicepconfig.json) to a scratch folder, makes the
relay's ranges from the recorded fixture (relay-ranges.sh --from-file testdata/service-tags.json, as
deploy/azure/check.sh does), and runs `bicep build-params` on the output there (or `az bicep build-params`): compile
only, no Azure call.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

IMAGE_PARAMS = {"apiImage": "api", "enrolImage": "enrol", "workerImage": "worker", "verifierImage": "verifier",
                "webImage": "web", "relayImage": "relay"}


def render(example: str, lock: dict, registry: str, version: str) -> str:
    if not re.fullmatch(r"[a-z0-9.-]+(:\d+)?", registry):
        raise SystemExit(f"bicep_params: {registry!r} is not a registry login server")
    images, apps = lock["images"], lock["apps"]
    out, seen = [], set()
    for line in example.splitlines():
        m = re.match(r"^param (\w+) = ", line)
        name = m.group(1) if m else ""
        if name in IMAGE_PARAMS:
            img = images[apps[IMAGE_PARAMS[name]]]
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", img.get("digest", "")):
                raise SystemExit(f"bicep_params: {apps[IMAGE_PARAMS[name]]} has no sha256 digest in the lock")
            line = f"param {name} = '{registry}/{img['repository']}@{img['digest']}'"
            seen.add(name)
        elif name == "registryServer":
            line = f"param registryServer = '{registry}'"
            seen.add(name)
        elif line.startswith("// Images."):
            line = (f"// Images of release {version}, by digest (deploy/images/images.lock.json, written by "
                    "release/runbook.sh). api, enrol, worker and the verifier run the same service image.")
        out.append(line)
    missing = sorted((set(IMAGE_PARAMS) | {"registryServer"}) - seen)
    if missing:
        raise SystemExit(f"bicep_params: the example has no {', '.join(missing)}")
    return "\n".join(out) + "\n"


def compile_check(params: Path, azure: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp)
        for n in ("main.bicep", "bicepconfig.json"):
            shutil.copy(azure / n, src / n)
        shutil.copytree(azure / "modules", src / "modules")
        subprocess.run([str(azure / "relay-ranges.sh"), "--location", "westeurope", "--from-file",
                        str(azure / "testdata" / "service-tags.json"), "--out", str(src / "relay-allowed-cidrs.json")],
                       check=True, capture_output=True)
        p = src / params.name
        shutil.copy(params, p)
        if shutil.which("bicep"):
            cmd = ["bicep", "build-params", str(p), "--outfile", str(src / "out.json")]
        elif shutil.which("az"):
            cmd = ["az", "bicep", "build-params", "--file", str(p), "--outfile", str(src / "out.json")]
        else:
            raise SystemExit("bicep_params: --compile needs the Bicep CLI (or az bicep)")
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0 or "Error " in r.stderr:
            raise SystemExit(f"bicep_params: {params.name} does not compile:\n{r.stderr.strip()[-1500:]}")
        built = json.loads((src / "out.json").read_text())
        got = {k: built["parameters"][k]["value"] for k in IMAGE_PARAMS}
        want = {k: v for k, v in (re.match(r"^param (\w+) = '(.*)'$", ln).groups()
                                  for ln in params.read_text().splitlines()
                                  if re.match(r"^param (\w+Image) = '", ln))}
        if got != want:
            raise SystemExit(f"bicep_params: the compiled parameters are not the written digests: {got}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--lock", type=Path, required=True)
    ap.add_argument("--registry", required=True)
    ap.add_argument("--version", required=True)
    ap.add_argument("--example", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--compile", type=Path, help="deploy/azure: also compile it there (bicep build-params)")
    args = ap.parse_args(argv)
    text = render(args.example.read_text(), json.loads(args.lock.read_text()), args.registry, args.version)
    args.out.write_text(text)
    if args.compile:
        compile_check(args.out, args.compile)
    print(f"bicep_params: {args.out}" + (" (compiles)" if args.compile else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
