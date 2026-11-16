"""The harness's context: paths, run state, the tool environment, logging and steps.

Everything a phase does to a cloud goes through Context.run (a CLI) or a boto3 session from
the AWS module, so a dry run can swap both for fakes, and every step goes through
Context.step, so a failure can be injected at any of them (--inject-failure).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REAL = HERE.parent  # tests/real
REPO = REAL.parents[1]
FAKES = REAL / "fakes"
FAKE_BIN = FAKES / "bin"
#: deploy/teams/build.sh's pinned manifest schema (@microsoft/app-manifest 1.1.3, MIT): what
#: it would fetch with npm pack and check by sha512, kept so a dry run never fetches it.
TEAMS_SCHEMA_VERSION = "1.1.3"
TEAMS_SCHEMA = (
    FAKES / "fixtures" / "teams-schema" / TEAMS_SCHEMA_VERSION / "MicrosoftTeams.schema.json"
)
DEFAULT_ALLOW_LIST = REAL / "allow-list.local"

# Tag keys on every resource a run makes (Azure tags, AWS tags, Entra application tags).
TAG_RUN = "omnifile-run"
TAG_EXPIRES = "omnifile-expires"
TAG_HARNESS = "omnifile-harness"
HARNESS_TAG_VALUE = "tests-real"

# The tools a dry run must find as shims, never as the real thing.
#: Network tools a dry run has only as refusing shims (fakes/bin): nothing models them, and a
#: guide step that needs one must get a model first.
NO_NETWORK = ("nslookup", "dig", "host", "wget", "nc", "ssh-keyscan", "telnet")
SHIMMED = ("az", "aws", "gh", "ssh", "scp", "docker", "node", "psql", "curl", *NO_NETWORK)
# Other tools a step may need, linked into the dry run's PATH from the caller's PATH. az,
# aws and gh are never in this list: /opt/homebrew/bin is not on the dry run's PATH.
PASS_THROUGH = (
    "bicep",
    "jq",
    "openssl",
    "ssh-keygen",
    "python3",
    "uv",
    "git",
    "zstd",
    "sha256sum",
    "shasum",
    "base64",
    "tar",
    "gzip",
)
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"

DRY_RUN_ID = "rr-dry-0001"
DRY_NOW = "2026-11-03T00:00:00Z"


class HarnessError(Exception):
    code = 1


class UsageError(HarnessError):
    code = 2


class AwaitingApproval(HarnessError):
    code = 3


class Refused(HarnessError):
    code = 4


class InjectedFailure(HarnessError):
    code = 1


class StepFailed(HarnessError):
    code = 1


def iso(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> datetime:
    """UTC with seconds (the harness's tags) or without (2026-11-04T18:00Z, the guide's), or
    Graph's createdDateTime (fractional seconds)."""
    s = re.sub(r"(T\d\d:\d\d:\d\d)\.\d+Z$", r"\1Z", s or "")
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%MZ"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=UTC)
        except ValueError:
            pass
    raise ValueError(f"not a UTC time: {s!r}")


def current_time(dry_run: bool, fixed: datetime) -> datetime:
    """Now: the run's fixed clock in a dry run (the fakes share it), the wall clock otherwise
    (a phase can run for hours; its start time is not now)."""
    return fixed if dry_run else datetime.now(UTC)


def iso_minutes(s: str) -> str:
    """The same time as the guide's check-env.sh wants it: 2026-11-04T18:00Z."""
    return parse_iso(s).strftime("%Y-%m-%dT%H:%MZ")


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def digest_of(obj: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical(obj).encode()).hexdigest()


def norm_digest(d: str) -> str:
    d = d.strip().lower()
    if not d.startswith("sha256:"):
        d = "sha256:" + d
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", d):
        raise UsageError(f"not a digest: {d!r} (want sha256:<64 hex>)")
    return d


#: Every run id the harness accepts, from the command line or read back from a cloud's tags.
#: Strict, because run ids go into OData filters, tags, resource names and paths.
RUN_ID_RE = re.compile(r"rr-(?:[0-9]{12}-[0-9a-f]{4}|dry-[0-9]{4})")


def valid_run_id(run_id: str) -> bool:
    return bool(RUN_ID_RE.fullmatch(run_id or ""))


def require_run_id(run_id: str) -> str:
    if not valid_run_id(run_id):
        raise UsageError(f"not a run id: {run_id!r} (want rr-<yyyymmddhhmm>-<4 hex>)")
    return run_id


#: Real tools a dry run runs that can crash by themselves: the Bicep CLI (.NET) aborts now and
#: then (SIGABRT, an AccessViolationException in .NET's HTTP stack) on `build` and
#: `build-params`. It makes HTTPS calls to aka.ms on every build whatever bicepconfig says, and
#: the crash comes from the attempt: here 4 builds in 80 crashed behind the dead proxy, and 2 in
#: 80 behind a local proxy that answers 403, so neither proxy cures it. The proxy keeps the
#: calls off the network; the cure is to run a crashed build again. Up to CRASH_TRIES runs in
#: all (three in a row were seen once), a second apart; an error is not retried.
RETRY_ON_CRASH = {"bicep"}
CRASH_TRIES = 5


#: A proxy nothing listens on (the discard port): a dry run's Bicep CLI reaches no network.
#: Left to itself it calls aka.ms:443 on every build; through this it fails fast and builds
#: offline, which is also what made its crashes (an HTTPS call aborting) go away.
DEAD_PROXY = "http://127.0.0.1:9"
OFFLINE_ENV = {
    "HTTPS_PROXY": DEAD_PROXY,
    "HTTP_PROXY": DEAD_PROXY,
    "https_proxy": DEAD_PROXY,
    "http_proxy": DEAD_PROXY,
    "NO_PROXY": "",
    "no_proxy": "",
}


def retry_wrapper(real: str) -> str:
    q = "'" + real.replace("'", "'\\''") + "'"
    exports = "".join(f"export {k}='{v}'\n" for k, v in OFFLINE_ENV.items())
    return (
        "#!/bin/sh\n"
        "# tests/real: the real tool, offline (core.OFFLINE_ENV), run again if it crashed\n"
        "# (core.RETRY_ON_CRASH), up to core.CRASH_TRIES runs in all.\n"
        f"{exports}"
        "n=1\n"
        f'{q} "$@"; c=$?\n'
        f'while [ "$c" -ge 128 ] && [ "$n" -lt {CRASH_TRIES} ]; do\n'
        f'  echo "{Path(real).name} crashed (exit $c); running it again" >&2\n'
        f'  sleep 1; n=$((n + 1)); {q} "$@"; c=$?\n'
        "done\n"
        'exit "$c"\n'
    )


#: Real tools a dry run runs whose network uses go to the fakes instead (a test in the shell,
#: the fake's tool name). Default deny: the fake models s_client (the guide's tls-issuer.sh)
#: and the service's database commands the guide runs through uv (migrate, admin), and
#: refuses every other openssl subcommand that takes a host or URL, and git's remote ones.
INTERCEPT = {
    "openssl": (
        'case "${1:-}" in s_client|s_time|s_server|ocsp|cmp|ech) true;; *) false;; esac',
        "openssl",
    ),
    "git": (
        'case "${1:-}" in fetch|clone|ls-remote|pull|push|remote|submodule) true;; *) false;; esac',
        "git",
    ),
    "uv": (
        'case " $* " in *" omnifile_service.catalog.migrate "*|*" omnifile_service.admin "*)'
        " true;; *) false;; esac",
        "uv-service",
    ),
}


#: Tools the guide needs at a version: the first one on PATH that has it, else Homebrew's,
#: else the first on PATH (and the guide's own check says what is wrong).
WANT_VERSION = {
    # OpenSSL 3 or later, never LibreSSL (the guide's step 6 and tools/tls-issuer.sh)
    "openssl": (
        re.compile(r"^OpenSSL ([3-9]|[1-9][0-9]+)\."),
        ("/opt/homebrew/bin", "/opt/homebrew/opt/openssl@3/bin", "/usr/local/opt/openssl@3/bin"),
    )
}


def find_tool(tool: str, path: str) -> str | None:
    first = shutil.which(tool, path=path)
    if tool not in WANT_VERSION:
        return first
    want, kegs = WANT_VERSION[tool]
    for d in [*path.split(os.pathsep), *kegs]:
        cand = Path(d) / tool
        if cand.is_file() and os.access(cand, os.X_OK):
            try:
                v = subprocess.run(
                    [str(cand), "version"], capture_output=True, text=True, timeout=10
                )
            except (OSError, subprocess.SubprocessError):
                continue
            if want.match(v.stdout):
                return str(cand)
    return first


def intercept_wrapper(real: str, test: str, fake: str) -> str:
    q = "'" + real.replace("'", "'\\''") + "'"
    fakecli = "'" + str(FAKES / "fakecli.py").replace("'", "'\\''") + "'"
    return (
        "#!/bin/sh\n"
        "# tests/real: the real tool, except what reaches a network (core.INTERCEPT).\n"
        f'if {test}; then exec "$REAL_FAKE_PY" {fakecli} {fake} "$@"; fi\n'
        f'exec {q} "$@"\n'
    )


def new_run_id(now: datetime) -> str:
    return f"rr-{now.strftime('%Y%m%d%H%M')}-{secrets.token_hex(2)}"


def prefix_for(run_id: str) -> str:
    """The deployment's namePrefix: 2-8 lowercase characters (WP1's limit), so "r" and 7 hex
    of the run id's hash. Two runs can still share one (1 in 2^28): teardown never deletes by
    this name alone (see ownership.py), and names the harness picks itself carry the whole
    run id instead."""
    return "r" + hashlib.sha256(run_id.encode()).hexdigest()[:7]


@dataclass
class Context:
    phase: str
    cloud: str
    dry_run: bool
    out: Path
    run_id: str
    now: datetime
    approve: str | None = None
    #: Every --approve given: the plan's digest, then each later gate's (an owner approves
    #: each deploy's own what-if), or "auto" in a dry run.
    approvals: list[str] = field(default_factory=list)
    guide: Path | None = None
    appliance: Path | None = None
    azure_run: str | None = None
    ttl_hours: int = 12
    #: whether --ttl-hours was given (the cheap pass has its own shorter default)
    ttl_given: bool = False
    #: --scope as given (None: what plan fixed, else full)
    scope: str | None = None
    inject: str | None = None
    expired: bool = False
    allow_list_path: Path = DEFAULT_ALLOW_LIST
    state: dict[str, Any] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    steps_seen: list[str] = field(default_factory=list)
    secrets: dict[str, str] = field(default_factory=dict)

    # ------------------------------------------------------------------ files

    @property
    def state_file(self) -> Path:
        return self.out / "state.json"

    @property
    def log_file(self) -> Path:
        return self.out / "log.txt"

    @property
    def evidence(self) -> Path:
        p = self.out / "evidence"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def load_state(self) -> None:
        if self.state_file.exists():
            self.state = json.loads(self.state_file.read_text())
            if self.state.get("run_id") != self.run_id:
                raise UsageError(f"{self.state_file} is for run {self.state.get('run_id')}")
            if self.state.get("dry_run") != self.dry_run:
                raise UsageError("a dry run's state can't be used by a real run, or the reverse")
        sf = self.out / "secrets.json"
        if sf.exists():
            self.secrets = json.loads(sf.read_text())

    def save_state(self) -> None:
        self.out.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2, sort_keys=True) + "\n")
        tmp.replace(self.state_file)

    def secret(self, name: str, nbytes: int = 18) -> str:
        """A generated test secret (AD user passwords and the like), kept in OUT/secrets.json
        (0600, never committed, never printed)."""
        if name not in self.secrets:
            self.secrets[name] = "Of1-" + secrets.token_urlsafe(nbytes)
            self._save_secrets()
        return self.secrets[name]

    def add_secret(self, name: str, value: str) -> None:
        """A value the run learned that must not be logged (an enrolment code)."""
        if value and self.secrets.get(name) != value:
            self.secrets[name] = value
            self._save_secrets()

    def _save_secrets(self) -> None:
        self.out.mkdir(parents=True, exist_ok=True)
        sf = self.out / "secrets.json"
        # Created 0600 before anything is written to it.
        fd = os.open(sf, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(self.secrets, indent=2, sort_keys=True))

    def mask(self, text: str) -> str:
        for s in sorted(self.secrets.values(), key=len, reverse=True):
            if s and len(s) >= 6:
                text = text.replace(s, "<secret>")
        return text

    # ---------------------------------------------------------------- logging

    def log(self, msg: str) -> None:
        self.out.mkdir(parents=True, exist_ok=True)
        line = self.mask(f"[{self.cloud}:{self.phase}] {msg}")
        print(line, flush=True)
        with self.log_file.open("a") as f:
            f.write(line + "\n")

    # ------------------------------------------------------------------ steps

    @contextmanager
    def step(self, name: str):
        """One named step. --inject-failure NAME fails here, before the step does anything,
        so a test can prove what teardown does after a failure at that point."""
        full = f"{self.cloud}.{name}"
        self.steps_seen.append(full)
        self.log(f"step {full}")
        if self.inject and self.inject in (name, full):
            raise InjectedFailure(f"failure injected at {full}")
        yield

    # ------------------------------------------------------------------ tags

    @property
    def expires(self) -> str:
        return self.state["expires"]

    def tags(self) -> dict[str, str]:
        return {TAG_RUN: self.run_id, TAG_EXPIRES: self.expires, TAG_HARNESS: HARNESS_TAG_VALUE}

    def az_tags(self) -> list[str]:
        return [f"{k}={v}" for k, v in self.tags().items()]

    # ---------------------------------------------------------- environment

    def build_env(self, moto_url: str | None = None) -> None:
        """The environment every tool runs in. A dry run strips cloud credentials, points the
        CLIs' config at empty directories, and puts the shims first on a PATH that has no
        real az, aws or gh; it then refuses to go on unless each shimmed tool resolves to
        its shim."""
        if not self.dry_run:
            self.env = dict(os.environ)
            return
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(
                (
                    "AZURE_",
                    "AWS_",
                    "ARM_",
                    "GH_",
                    "GITHUB_",
                    "MSI_",
                    "IDENTITY_",
                    "OMNIFILE_",
                    "REAL_FAKE_",
                    "BOTO",
                    "REAL_AWS_RUN",
                )
            )
        }
        dry = self.out / "dry"
        link = dry / "bin"
        link.mkdir(parents=True, exist_ok=True)
        orig_path = os.environ.get("PATH", "")
        for tool in PASS_THROUGH:
            found = find_tool(tool, orig_path)
            dst = link / tool
            if found and tool in INTERCEPT and not dst.exists():
                dst.write_text(intercept_wrapper(found, *INTERCEPT[tool]))
                dst.chmod(0o755)
                continue
            if found and tool in RETRY_ON_CRASH and not dst.exists():
                dst.write_text(retry_wrapper(found))
                dst.chmod(0o755)
                continue
            if found and not dst.exists():
                dst.symlink_to(found)
        # The harness's own Python (its venv, with boto3 and moto) runs the shims. Not a
        # symlink elsewhere: Python finds its venv from the path it was started by.
        py = sys.executable
        for d in ("azure", "gh", "aws", "home", "cache"):
            (dry / d).mkdir(exist_ok=True)
        # A home and caches of the run's own: nothing a step or a tool reads comes from the
        # owner's (deploy/teams/build.sh looks for its schema under $XDG_CACHE_HOME).
        seeded = dry / "cache" / "omnifile" / "teams-schema" / TEAMS_SCHEMA_VERSION
        seeded.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(TEAMS_SCHEMA, seeded / TEAMS_SCHEMA.name)
        (dry / "aws" / "config").write_text("[default]\nregion = us-west-2\n")
        (dry / "aws" / "credentials").write_text("")
        env.update(
            {
                "PATH": f"{FAKE_BIN}:{link}:{SYSTEM_PATH}",
                "HOME": str(dry / "home"),
                "XDG_CACHE_HOME": str(dry / "cache"),
                "XDG_CONFIG_HOME": str(dry / "home" / ".config"),
                # uv (deploy/teams/build.sh runs `uv run` in service/) never downloads: it uses
                # service/.venv as synced, and a cache of the run's own unless one is given.
                "UV_CACHE_DIR": os.environ.get("UV_CACHE_DIR") or str(dry / "cache" / "uv"),
                "UV_OFFLINE": "1",
                # Nothing that honours a proxy reaches the network (bicep's wrapper empties
                # NO_PROXY too); the local moto server and listeners stay reachable.
                **OFFLINE_ENV,
                "NO_PROXY": "127.0.0.1,localhost,::1",
                "no_proxy": "127.0.0.1,localhost,::1",
                "AZURE_CONFIG_DIR": str(dry / "azure"),
                "GH_CONFIG_DIR": str(dry / "gh"),
                "AWS_CONFIG_FILE": str(dry / "aws" / "config"),
                "AWS_SHARED_CREDENTIALS_FILE": str(dry / "aws" / "credentials"),
                "AWS_ACCESS_KEY_ID": "fake-access-key",
                "AWS_SECRET_ACCESS_KEY": "fake-secret-key",
                "AWS_DEFAULT_REGION": "us-west-2",
                "AWS_EC2_METADATA_DISABLED": "true",
                "REAL_FAKE_PY": str(py),
                "REAL_FAKE_STATE": str(self.out / "fake-state.json"),
                "REAL_FAKE_TRANSCRIPT": str(self.out / "transcript.txt"),
                "REAL_FAKE_REPO": str(REPO),
                "REAL_FAKE_OUT": str(self.out),
                "REAL_NOW": iso(self.now),
            }
        )
        if moto_url:
            env["AWS_ENDPOINT_URL"] = moto_url
            env["REAL_FAKE_MOTO"] = moto_url
        for knob in (
            "REAL_FAKE_FAIL",
            "REAL_FAKE_PURGE_PROTECTION",
            "REAL_FAKE_403",
            "REAL_FAKE_FAIL_BOTO",
            "REAL_FAKE_SWITCH_AFTER",
            "REAL_FAKE_OWNER_AZ",
        ):  # tests' fault knobs
            if os.environ.get(knob):
                env[knob] = os.environ[knob]
        self.env = env
        for tool in SHIMMED:
            where = shutil.which(tool, path=env["PATH"])
            if where is None or Path(where).resolve().parent != FAKE_BIN.resolve():
                raise Refused(f"dry run: {tool} resolves to {where}, not the shim in {FAKE_BIN}")

    # ------------------------------------------------------------------- run

    def run(
        self,
        argv: list[str],
        *,
        check: bool = True,
        input: str | None = None,
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
        capture: bool = True,
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess:
        """Run a tool. Output is captured; secrets known to the run are never logged."""
        full_env = dict(self.env)
        if env:
            full_env.update(env)
        shown = self.mask(" ".join(argv))
        with (self.out / "commands.txt").open("a") as f:
            f.write(shown + "\n")
        # No input: stdin is /dev/null, never the harness's own (a tool reading it would hang).
        io = {"input": input} if input is not None else {"stdin": subprocess.DEVNULL}
        p = subprocess.run(
            argv,
            env=full_env,
            cwd=str(cwd or REPO),
            capture_output=capture,
            text=True,
            timeout=timeout,
            **io,
        )
        if check and p.returncode != 0:
            err = (p.stderr or "").strip().splitlines()[-5:]
            raise StepFailed(
                f"{argv[0]} {argv[1] if len(argv) > 1 else ''} failed "
                f"({p.returncode}): " + " | ".join(err)
            )
        return p

    def az(self, *args: str, check: bool = True, input: str | None = None) -> Any:
        """az ARGS -o json, parsed. An empty answer is None."""
        p = self.run(["az", *args, "-o", "json"], check=check, input=input)
        if p.returncode != 0:
            return None
        out = p.stdout.strip()
        return json.loads(out) if out else None

    def wait(self, what: str, fn, *, timeout_s: int, every_s: int = 15):
        """Poll fn until it returns something true. A dry run's fakes answer at once, so it
        never sleeps there."""
        deadline = time.monotonic() + timeout_s
        while True:
            v = fn()
            if v:
                return v
            if self.dry_run or time.monotonic() > deadline:
                raise StepFailed(f"timed out waiting for {what}")
            time.sleep(every_s)

    # ------------------------------------------------------------ expiry

    def fix_expiry(self, max_ttl: int) -> None:
        if "expires" in self.state:
            return
        ttl = min(self.ttl_hours, max_ttl)
        if ttl <= 0:
            raise UsageError("--ttl-hours must be positive")
        self.state["expires"] = iso(self.now + timedelta(hours=ttl))
