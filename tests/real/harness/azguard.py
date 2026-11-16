"""The `az` every guide step runs, first on the step's PATH in real runs and dry runs alike: the
real az, except a deployment that no approved what-if covers.

It is a safety net against accidental deploys, not a security boundary. A step runs as the
same user as the harness, so a step that sets out to can always reach the real az (by its
path, or by a PATH of its own) or the ARM API directly. What it stops is the guide, or a
change to it, deploying something the owner did not approve by mistake.

Refused, however the shell spelled it (quotes, variables, eval, env, bash -c):
- `az deployment [group|sub|mg|tenant] create`, `az group deployment create`;
- `az stack group|sub|mg create|set`;
- `az resource ...` on a deployment or deployment stack, by id, by URL or by
  `--namespace Microsoft.Resources --resource-type deployments`;
- `az rest` with PUT, PATCH or POST to a URL with /deployments/ or /deploymentStacks/, also
  when the URL hides them with %-escapes (decoded until nothing changes).

The one exception is `az deployment group create` from deploy/azure/whatif.sh after its own
digest check. The harness gives the deploy step a one-shot grant (OMNIFILE_DEPLOY_GRANT) for
the digest that step's own gate previewed and the owner approved, the run's resource group,
and the compiled template and parameters that gate previewed; whatif.sh says which digest it
checked (OMNIFILE_DEPLOY_APPROVED_DIGEST). All must match, and the grant is spent under an
exclusive lock, so it works once.

Where the grants are, where the real az is and where to log are fixed by the wrapper the
harness writes for each run (`write_wrapper`): they are this script's own arguments, never
read from the environment, so a step can't point the guard at a grants file of its own, and
no step is handed the real az's path. Standard library only.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shlex
import sys
import time
from pathlib import Path
from urllib.parse import unquote

#: az's global options that take a value (so it isn't read as part of the command).
VALUED = {"--output", "-o", "--query", "--subscription"}
DEPLOY_PATHS = ("/deployments/", "/deploymentstacks/")
DEPLOY_TYPES = {"deployments", "deploymentstacks"}
#: The first comment line of every wrapper (so the real-az search never picks a guard).
GUARD_MARK = "OmniFile real-cloud harness az guard"


def decoded(s: str) -> str:
    """Lower case, %-escapes decoded until nothing changes (a URL can hide /deployments/)."""
    s = s.lower()
    for _ in range(8):
        d = unquote(s).lower()
        if d == s:
            break
        s = d
    return s


def words(argv: list[str]) -> list[str]:
    """The command's words, lower case, options and their values left out."""
    out, skip = [], False
    for a in argv:
        if skip:
            skip = False
            continue
        if a.startswith("-"):
            if a in VALUED:
                skip = True
            continue
        out.append(a.lower())
    return out


def opts(argv: list[str], *names: str) -> list[str]:
    """Every value given to any of NAMES (`--x v` or `--x=v`)."""
    got = []
    for i, a in enumerate(argv):
        for n in names:
            if a == n and i + 1 < len(argv):
                got.append(argv[i + 1])
            elif a.startswith(n + "="):
                got.append(a.split("=", 1)[1])
    return got


def opt(argv: list[str], *names: str) -> str | None:
    got = opts(argv, *names)
    return got[0] if got else None


def kind(argv: list[str]) -> str | None:
    """What a refused command is, or None for one that passes through. Strict on purpose: a
    `create` anywhere in a deployment command is refused, even as an option's value (a false
    refusal stops the run; a false pass would deploy)."""
    w = words(argv)
    if w[:1] == ["deployment"] and "create" in w:
        return "deployment create"
    if w[:2] == ["group", "deployment"] and "create" in w:
        return "group deployment create"
    if w[:1] == ["stack"] and {"create", "set"} & set(w):
        return "stack create/set"
    if w[:1] == ["resource"]:
        # by id or URL, by type, or by --namespace Microsoft.Resources --resource-type
        # deployments: any argument that names a deployment, however it is given
        args = [decoded(a).split("=", 1)[-1] for a in argv]
        if any(p in a for a in args for p in DEPLOY_PATHS) or any(
            a.rstrip("/") in DEPLOY_TYPES
            or a.rstrip("/").endswith(("/deployments", "/deploymentstacks"))
            for a in args
        ):
            return "resource call on a deployment"
    if w[:1] == ["rest"]:
        # no --method but a body: count it as a write (strict; a GET carries no body)
        body = opts(argv, "--body", "-b")
        method = (opt(argv, "--method", "-m") or ("post" if body else "get")).lower()
        urls = [decoded(u) for u in opts(argv, "--url", "--uri", "-u")]
        if method in {"put", "patch", "post"} and any(
            p in u or u.rstrip("/").endswith(p.rstrip("/")) for u in urls for p in DEPLOY_PATHS
        ):
            return f"rest {method} to a deployment"
    return None


def file_sha(path: str) -> str:
    """As whatif.sh's digest counts a compiled file: SHA-256 of its JSON, keys sorted."""
    with open(path) as f:
        return (
            "sha256:"
            + hashlib.sha256(json.dumps(json.load(f), sort_keys=True).encode()).hexdigest()
        )


#: The options whatif.sh's create passes, each with one value: in a granted create, nothing
#: else. az accepts any unambiguous abbreviation (`--param`, `--parameter`) and several values
#: after --parameters, so options are matched exactly and anything unlisted is refused.
GRANTED_OPTIONS = {
    "--resource-group": "rg",
    "-g": "rg",
    "--name": "name",
    "-n": "name",
    "--template-file": "template",
    "-f": "template",
    "--parameters": "parameters",
    "-p": "parameters",
    "--output": "output",
    "-o": "output",
}


def _inputs(argv: list[str]) -> tuple[dict[str, str] | None, str | None]:
    """The template and parameters this create deploys, by hash. The command must be exactly
    whatif.sh's: `deployment group create` and then only GRANTED_OPTIONS, each once with one
    value (`--parameters` an @file); any other option, abbreviation or extra value is refused."""
    if argv[:3] != ["deployment", "group", "create"]:
        return None, "a granted create is `az deployment group create` and its options only"
    seen: dict[str, str] = {}
    rest = argv[3:]
    while rest:
        flag = rest[0]
        key = GRANTED_OPTIONS.get(flag)
        if key is None:
            return None, f"{flag!r} is not an option whatif.sh passes (only the exact ones are)"
        if len(rest) < 2 or rest[1].startswith("-"):
            return None, f"{flag} needs one value"
        if key in seen:
            return None, f"{flag} given twice"
        seen[key], rest = rest[1], rest[2:]
    if not {"rg", "name", "template", "parameters"} <= set(seen):
        return None, "the grant covers -g, -n, one --template-file and one --parameters @file"
    if not seen["parameters"].startswith("@"):
        return None, "--parameters must be the one @file the gate hashed"
    try:
        files = {
            "template": file_sha(seen["template"]),
            "parameters": file_sha(seen["parameters"][1:]),
        }
        return files, None
    except (OSError, ValueError) as e:
        return None, f"can't read what it deploys ({e})"


def granted(argv: list[str], grants_path: Path) -> str | None:
    """None when this create is whatif.sh's, with an unspent grant for its digest, group,
    template and parameters; else why not. The grant is spent here, under an exclusive lock."""
    w = words(argv)
    if w[:3] != ["deployment", "group", "create"]:
        return "only whatif.sh's `az deployment group create` can be granted"
    token = os.environ.get("OMNIFILE_DEPLOY_GRANT", "")
    digest = os.environ.get("OMNIFILE_DEPLOY_APPROVED_DIGEST", "")
    if not token or not digest:
        return "no grant: this step is not an approved deploy, or whatif.sh did not check it"
    inputs, why = _inputs(argv)
    if inputs is None:
        return why
    lock = grants_path.with_name(grants_path.name + ".lock")
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            grants = json.loads(grants_path.read_text())
        except (OSError, json.JSONDecodeError):
            return "no grants file"
        g = grants.get(token)
        if not g:
            return "an unknown grant"
        if g.get("used"):
            return f"the grant for step {g.get('step')} is spent"
        if g.get("digest") != digest:
            return f"whatif.sh checked {digest}, the grant is for {g.get('digest')}"
        rg = opt(argv, "--resource-group", "-g")
        if rg != g.get("rg"):
            return f"the grant is for resource group {g.get('rg')}, not {rg}"
        for k in ("template", "parameters"):
            if not g.get(k) or g[k] != inputs[k]:
                return f"the grant is for {k} {g.get(k)}, this deploys {inputs[k]}"
        g["used"] = True
        g["used_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        tmp = grants_path.with_name(grants_path.name + ".tmp")
        tmp.write_text(json.dumps(grants, indent=2, sort_keys=True))
        os.chmod(tmp, 0o600)
        tmp.replace(grants_path)
        return None
    finally:
        os.close(fd)  # releases the lock


def write_wrapper(d: Path, *, python: str, real: str, grants: Path, log: Path) -> Path:
    """The run's `az`: this script with the run's fixed paths as its own arguments."""
    d.mkdir(parents=True, exist_ok=True)
    w = d / "az"
    q = shlex.quote
    w.write_text(
        "#!/bin/sh\n"
        f"# {GUARD_MARK} for this run (tests/real/harness/azguard.py).\n"
        f"exec {q(python)} {q(str(Path(__file__).resolve()))} --real {q(real)} "
        f'--grants {q(str(grants))} --log {q(str(log))} -- "$@"\n'
    )
    w.chmod(0o755)
    return w


def main(argv: list[str]) -> int:
    conf: dict[str, str] = {}
    while argv and argv[0] != "--":
        if len(argv) < 2 or argv[0] not in ("--real", "--grants", "--log"):
            print("az guard: run it through the harness's wrapper: refused", file=sys.stderr)
            return 2
        conf[argv[0][2:]], argv = argv[1], argv[2:]
    argv = argv[1:]
    real, grants, logf = conf.get("real", ""), conf.get("grants", ""), conf.get("log", "")
    if not real or not os.path.isabs(real) or not grants or not os.path.isabs(grants):
        print("az guard: no real az or grants file recorded: refused", file=sys.stderr)
        return 2

    def log(line: str) -> None:
        if logf:
            with open(logf, "a") as f:
                f.write(line + "\n")

    k = kind(argv)
    if k is not None:
        why = granted(argv, Path(grants)) if k == "deployment create" else "never granted"
        if why is not None:
            log(f"refused {k}: {why}: az {' '.join(argv)}")
            print(
                f"az guard: refused {k} ({why}). Only deploy/azure/whatif.sh --deploy may deploy, "
                "in a deploy step whose own gate's digest the owner approved",
                file=sys.stderr,
            )
            return 2
        log(f"granted {k}: az {' '.join(argv)}")
    os.execv(real, [real, *argv])
    return 0  # not reached


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
