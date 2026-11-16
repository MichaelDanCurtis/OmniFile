"""The owner-written allow-list: the only accounts a run may touch.

tests/real/allow-list.local is git-ignored and written by the owner, never by the harness.
One entry per line, `key value`; `#` starts a comment; a key may repeat:

    azure-subscription  <subscription id>
    azure-tenant        <tenant id of that subscription>
    azure-location      <region>
    aws-account         <12 digits>
    aws-region          <region>
    aws-profile         <profile name the owner signed in with>
    m365-tenant         <the Teams tenant id>
    m365-test-user      <UPN of the AD-synced test user>
    ssh-from            <CIDR the owner's workstation reaches the test VM from>
    max-ttl-hours       <hours; the longest expiry a run may set (default 24)>

Every phase checks the signed-in identity against it before anything else, and refuses
(exit 4) if the subscription, tenant or account is not listed. A real run reads only the
default path; --allow-list (another file) is for dry runs, so tests can use a temporary one.
"""

from __future__ import annotations

import ipaddress
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .core import DEFAULT_ALLOW_LIST, REPO, Refused, UsageError

KEYS = {
    "azure-subscription": re.compile(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    ),
    "azure-tenant": re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"),
    "azure-location": re.compile(r"[a-z0-9]+"),
    "aws-account": re.compile(r"[0-9]{12}"),
    "aws-region": re.compile(r"[a-z]{2}(-[a-z]+)+-[0-9]"),
    "aws-profile": re.compile(r"[A-Za-z0-9_.-]+"),
    "m365-tenant": re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"),
    "m365-test-user": re.compile(r"[^@\s]+@[^@\s]+"),
    "ssh-from": None,  # a CIDR, checked below
    "max-ttl-hours": re.compile(r"[0-9]{1,3}"),
}


@dataclass
class AllowList:
    path: Path
    entries: dict[str, list[str]] = field(default_factory=dict)

    def values(self, key: str) -> list[str]:
        return self.entries.get(key, [])

    def one(self, key: str) -> str:
        v = self.values(key)
        if len(v) != 1:
            raise Refused(f"the allow-list needs exactly one {key} line ({self.path})")
        return v[0]

    @property
    def max_ttl(self) -> int:
        v = self.values("max-ttl-hours")
        return int(v[0]) if v else 24

    def require(self, key: str, value: str, what: str) -> None:
        if value.lower() not in {v.lower() for v in self.values(key)}:
            raise Refused(
                f"refused: {what} {value} is not on the allow-list ({self.path}); "
                f"add a `{key} {value}` line yourself if this run may touch it"
            )


def load(path: Path, *, dry_run: bool) -> AllowList:
    if not dry_run and path.resolve() != DEFAULT_ALLOW_LIST.resolve():
        raise Refused(
            "a real run reads only tests/real/allow-list.local; --allow-list is " "for dry runs"
        )
    if not path.is_file():
        raise Refused(
            f"no allow-list at {path}: the owner writes it (see "
            f"tests/real/allow-list.example); the harness never does"
        )
    if not dry_run:
        # A committed allow-list would let anyone's checkout touch the owner's accounts.
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", str(path)], cwd=REPO, capture_output=True
        )
        if tracked.returncode == 0:
            raise Refused(f"{path} is tracked by git; it must stay local")
    al = AllowList(path)
    for n, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 2:
            raise UsageError(f"{path}:{n}: want `key value`, got {raw!r}")
        key, value = parts
        if key not in KEYS:
            raise UsageError(f"{path}:{n}: unknown key {key!r} (known: {', '.join(KEYS)})")
        pat = KEYS[key]
        if pat is None:
            try:
                ipaddress.ip_network(value, strict=True)
            except ValueError as e:
                raise UsageError(f"{path}:{n}: {key} {value}: {e}") from None
            if ipaddress.ip_network(value).num_addresses > 256:
                raise UsageError(f"{path}:{n}: {key} {value} is wider than a /24")
        elif not pat.fullmatch(value):
            raise UsageError(f"{path}:{n}: {key} {value!r} is malformed")
        al.entries.setdefault(key, []).append(value)
    if not al.entries:
        raise Refused(f"{path} lists nothing")
    return al
