"""The customer guide's runnable steps: fenced blocks tagged `guide-step=N` (docs/guide/).

The fence's info string is `<lang> guide-step=N on=<where> [key=value ...]` (WP7's format):

- N: an integer, unique across the guide, strictly increasing through each file.
- on=workstation: bash from the root of the release folder, as an admin signed in with az
  (and aws); each block starts with `. ./omnifile.env`.
- on=edge-console: lang text, one answer per line, in the wizard's order; `<secret:NAME>`
  and `<value:NAME>` are values the harness supplies.
- on=edge-shell: bash on the edge, as an AD admin over ssh.
- on=portal: lang text, one action per line: open, click, fill, choose, expect.
- on=hyperv | windows-client (powershell), on=vsphere | kvm | linux-client (bash).
- file=PATH: write the block's content to PATH instead of running it.
- cloud=azure|aws|tenant: which run executes it (default azure).
- manual=yes: a person does it; the harness pauses for the owner instead of running it.
- input=NAME: the command reads that value on stdin (a password, an enrolment code).
- wait=N: the block checks something that takes time (a stub, an archive move, a recall):
  the harness reruns it every 60 s until it exits 0, for at most N minutes.
- gate=whatif: the guide's own what-if, whose `digest: sha256:<hex>` line is what the owner
  approves. `run.sh plan` runs it (and the plan=yes steps before it); `up` stops right after
  it if the digest it prints is not the approved one.
- plan=yes: a read-only step the gate needs first; `run.sh plan` runs it.
- <enter> in an edge-console block is an empty answer (press Enter).

The harness runs the blocks exactly as written: it never edits a command. It supplies
values only through omnifile.env and the <secret:>/<value:> placeholders.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .core import UsageError

ON_LANGS = {
    "workstation": {"bash", "sh", "toml", "json"},
    "edge-console": {"text"},
    "edge-shell": {"bash", "sh", "toml", "json"},
    "portal": {"text"},
    "hyperv": {"powershell", "pwsh"},
    "windows-client": {"powershell", "pwsh"},
    "vsphere": {"bash", "sh"},
    "kvm": {"bash", "sh", "xml"},
    "linux-client": {"bash", "sh"},
    # Hosts the harness has no stand-in for: recorded for the owner, never run.
    "ad-admin": {"powershell", "pwsh"},
    "dns-server": {"powershell", "pwsh"},
    "browser": {"text"},
    "exchange-admin": {"powershell", "pwsh"},
    # A Linux host on the edges' network (the Azure run's test VM).
    "edge-network": {"bash", "sh"},
}
OWNER_ONLY = {
    "hyperv",
    "vsphere",
    "windows-client",
    "ad-admin",
    "dns-server",
    "browser",
    "exchange-admin",
}
CLOUDS = {"azure", "aws", "tenant"}
PORTAL_VERBS = {"open", "click", "fill", "choose", "expect"}
PLACEHOLDER = re.compile(r"<(secret|value):([A-Za-z0-9_.-]+)>")
FENCE = re.compile(r"^(?P<indent> {0,3})(?P<fence>`{3,}|~{3,})(?P<info>.*)$")


@dataclass
class Step:
    n: int
    on: str
    lang: str
    body: str
    file: Path
    line: int
    attrs: dict[str, str] = field(default_factory=dict)

    @property
    def cloud(self) -> str:
        return self.attrs.get("cloud", "azure")

    @property
    def manual(self) -> bool:
        return self.attrs.get("manual", "no") == "yes"

    @property
    def gate(self) -> bool:
        return self.attrs.get("gate") == "whatif"

    @property
    def plan_step(self) -> bool:
        return self.attrs.get("plan", "no") == "yes" or self.gate

    @property
    def input_name(self) -> str | None:
        return self.attrs.get("input")

    @property
    def wait_minutes(self) -> int:
        return int(self.attrs.get("wait", "0"))

    @property
    def write_to(self) -> str | None:
        return self.attrs.get("file")

    @property
    def where(self) -> str:
        return f"{self.file.name}:{self.line}"

    def placeholders(self) -> list[tuple[str, str]]:
        return PLACEHOLDER.findall(self.body)


def _parse_info(info: str, where: str) -> tuple[str, dict[str, str]] | None:
    tokens = info.split()
    if not any(t.startswith("guide-step=") for t in tokens):
        return None
    lang = tokens[0] if tokens and "=" not in tokens[0] else ""
    attrs: dict[str, str] = {}
    for t in tokens[1:] if lang else tokens:
        if "=" not in t:
            raise UsageError(f"{where}: bad token {t!r} in a guide-step fence")
        k, v = t.split("=", 1)
        if k in attrs:
            raise UsageError(f"{where}: {k} given twice")
        attrs[k] = v
    return lang, attrs


def parse_file(path: Path) -> list[Step]:
    steps: list[Step] = []
    lines = path.read_text().splitlines()
    i = 0
    while i < len(lines):
        m = FENCE.match(lines[i])
        if not m:
            i += 1
            continue
        fence, info, start = m.group("fence"), m.group("info"), i + 1
        where = f"{path}:{start}"
        parsed = _parse_info(info, where)
        body: list[str] = []
        i += 1
        closed = False
        while i < len(lines):
            close = FENCE.match(lines[i])
            if (
                close
                and close.group("fence")[0] == fence[0]
                and len(close.group("fence")) >= len(fence)
                and not close.group("info").strip()
            ):
                closed = True
                break
            body.append(lines[i])
            i += 1
        i += 1
        if parsed is None:
            continue
        if not closed:
            raise UsageError(f"{where}: guide-step block is never closed")
        lang, attrs = parsed
        try:
            n = int(attrs.pop("guide-step"))
        except ValueError:
            raise UsageError(f"{where}: guide-step must be an integer") from None
        on = attrs.pop("on", None)
        if on not in ON_LANGS:
            raise UsageError(f"{where}: on= must be one of {', '.join(sorted(ON_LANGS))}")
        if lang not in ON_LANGS[on]:
            raise UsageError(f"{where}: a {on} step can't be {lang or 'untyped'}")
        if attrs.get("cloud", "azure") not in CLOUDS:
            raise UsageError(f"{where}: cloud= must be one of {', '.join(sorted(CLOUDS))}")
        if attrs.get("manual", "no") not in ("yes", "no"):
            raise UsageError(f"{where}: manual= is yes or no")
        known = {"cloud", "manual", "file", "input", "wait", "gate", "plan"}
        if set(attrs) - known:
            raise UsageError(f"{where}: unknown keys {sorted(set(attrs) - known)}")
        if attrs.get("gate", "whatif") != "whatif" or attrs.get("plan", "no") not in ("yes", "no"):
            raise UsageError(f"{where}: gate= is whatif, plan= is yes or no")
        if ("gate" in attrs or attrs.get("plan") == "yes") and on != "workstation":
            raise UsageError(f"{where}: a gate or plan step runs on the workstation")
        if "wait" in attrs and not attrs["wait"].isdigit():
            raise UsageError(f"{where}: wait= is a number of minutes")
        if "file" in attrs and lang not in ("toml", "json", "xml"):
            raise UsageError(f"{where}: file= is for toml, json or xml blocks")
        step = Step(n, on, lang, "\n".join(body) + "\n", path, start, attrs)
        if on == "portal":
            for ln in body:
                if ln.strip() and ln.split()[0] not in PORTAL_VERBS:
                    raise UsageError(
                        f"{where}: portal action {ln.split()[0]!r} is not one of "
                        f"{', '.join(sorted(PORTAL_VERBS))}"
                    )
        steps.append(step)
    return steps


def parse(guide: Path) -> list[Step]:
    """Every step in the guide's top-level .md files, in step order. Errors on duplicates,
    and on numbers that go down within a file."""
    if not guide.is_dir():
        raise UsageError(f"no guide at {guide}")
    steps: list[Step] = []
    seen: dict[int, Step] = {}
    for f in sorted(guide.glob("*.md")):  # the guide itself, not lint fixtures below it
        last = None
        for s in parse_file(f):
            if s.n in seen:
                raise UsageError(f"guide-step={s.n} at {s.where} and {seen[s.n].where}")
            if last is not None and s.n <= last:
                raise UsageError(f"{s.where}: guide-step={s.n} after {last}: numbers go up")
            seen[s.n] = s
            last = s.n
            steps.append(s)
    if not steps:
        raise UsageError(f"{guide} has no guide-step blocks")
    return sorted(steps, key=lambda s: s.n)


def fill(body: str, values: dict[str, str], secrets: dict[str, str]) -> str:
    """Substitute the guide's <secret:NAME> and <value:NAME> placeholders (edge-console and
    portal steps only). An unknown name is an error, never an empty answer."""

    def sub(m: re.Match) -> str:
        kind, name = m.group(1), m.group(2)
        src = secrets if kind == "secret" else values
        if name not in src:
            raise UsageError(f"the guide asks for <{kind}:{name}>, which the harness doesn't know")
        return src[name]

    return PLACEHOLDER.sub(sub, body).replace("<enter>", "")


def fill_known(body: str, values: dict[str, str], secrets: dict[str, str]) -> str:
    """For portal steps: as fill(), except that an unknown <value:NAME> on an `expect` line
    is left for the driver, which captures what the page shows there as NAME (an enrolment
    code, say) for later steps."""
    out = []
    for line in body.splitlines():
        if line.split()[:1] == ["expect"]:

            def sub(m: re.Match) -> str:
                kind, name = m.group(1), m.group(2)
                src = secrets if kind == "secret" else values
                return (
                    src[name]
                    if name in src
                    else m.group(0)
                    if kind == "value"
                    else fill(m.group(0), values, secrets)
                )

            out.append(PLACEHOLDER.sub(sub, line))
        else:
            out.append(fill(line, values, secrets))
    return "\n".join(out) + "\n"


def state_values(path: Path) -> dict[str, str]:
    """The values the guide's own steps recorded (omnifile.state.env, docs/guide/tools/state.sh):
    `[export] NAME=value` lines, the value shell-quoted or bare. <value:NAME> may name these."""
    import shlex

    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        m = re.match(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)=(.*)$", line)
        if m:
            words = shlex.split(m.group(2), comments=True) or [""]
            out[m.group(1)] = words[0]
    return out


def env_names(example: Path) -> list[str]:
    """The variable names an omnifile.env.example defines (`NAME=...` or `export NAME=...`)."""
    names = []
    for line in example.read_text().splitlines():
        m = re.match(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)=", line)
        if m:
            names.append(m.group(1))
    return names


def write_env(example: Path, dest: Path, values: dict[str, str], *, partial: bool = False) -> None:
    """omnifile.env from the example: every name it defines gets the harness's value. A name
    the harness doesn't know stops the run, so the guide and the harness stay in step. With
    partial (a run that executes only some sections, such as the AWS run's), such a name is
    left out instead, so a step that uses it fails on `set -u` rather than reading ''."""
    missing = [n for n in env_names(example) if n not in values]
    if missing and not partial:
        raise UsageError(
            f"{example} defines {', '.join(missing)}, which the harness has no "
            f"value for (harness/guidevalues.py)"
        )
    lines = ["# Written by tests/real/run.sh from " + example.name + ". Test values."]
    for n in env_names(example):
        if n not in values:
            continue
        v = values[n].replace("'", "'\"'\"'")
        lines.append(f"export {n}='{v}'")
    dest.write_text("\n".join(lines) + "\n")
