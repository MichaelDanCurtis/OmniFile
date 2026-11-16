"""Lint the customer guide against the repository and the compiled Bicep (docs/guide/check.sh).

    python3 docs/guide/lint/guide_lint.py --repo ROOT --main-json main.json [--pending FILE ...]
        [--build-params BICEP-CMD] [GUIDE]

Every name the guide gives a reader to type must exist, so that a run of the guide "as written" can't
fail on a typo or on something the repository no longer has:

- steps: every runnable block is fenced as ```<lang> guide-step=N on=<where> [...]```, with N unique
  and increasing, a known place, a language that suits it, and only known keys;
- values: every OMNI_ variable a block reads is in omnifile.env.example, recorded by an earlier step or
  a tool (tools/state.sh, tools/outputs.sh), or declared as an input; every example value is used;
  every workstation block that reads one loads omnifile.env first;
- Bicep: every parameter in customer.bicepparam, and every camelCase name in the guide's inline code,
  is a parameter or output of the compiled main.bicep (or a name listed as not Bicep); every output the
  tools read is one; customer.bicepparam builds;
- the repository: every repo path named exists; every script run exists and takes each flag given;
  every `python -m omnifile_service...` module and subcommand exists, as does every `omnifile-setup`
  subcommand; psql variables match the SQL file's;
- the edge: every OmniFile path on the filer (/etc/omnifile, /var/lib/omnifile, ...) is one the edge's
  or the appliance's sources name, and every omnifile-* or nfs-ganesha* unit has a unit file;
- the service: every OMNIFILE_ setting a step sets is one the service, relay or edge reads;
- scripts still to come from WP1: every flag a step gives is one WP1 declared (`flags` lines);
- commands inside quotes (`ssh host 'sudo omnifile-setup ...'`, `bash -c "..."`) get the same checks;
- the portal: every page opened is a route, and every label clicked, filled or chosen, and every text
  expected, is a portal string (a label built from a template needs enough fixed text of its own);
- setup answers: every key written to a .toml file is a field of the setup wizard's answers;
- content: the required sections are there, the Kerberos note gives its numbers, and no line promises a
  recall time for a file over 10 GB.

Names on a pending allowance (`--pending FILE`: `param`, `output`, `path`, `flags`, `subcommand`, `module`,
`moduleflags` and `feature` lines, each for work another package has not merged yet) are reported as
pending instead of failing, and an entry that already resolves is an error. None is in use now.

Exit 0 when nothing fails; 1 otherwise. Each problem is one line: `FAIL <code> line <n>: <what>`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PLACES = {
    "workstation": {"bash"},
    "ad-admin": {"powershell"},
    "dns-server": {"powershell"},
    "browser": {"text"},
    "portal": {"text"},
    "hyperv": {"powershell"},
    "vsphere": {"bash"},
    "kvm": {"bash"},
    "edge-console": {"text"},
    "edge-shell": {"bash"},
    "edge-network": {"bash"},
    "exchange-admin": {"powershell"},
    "linux-client": {"bash"},
    "windows-client": {"powershell"},
}
KEYS = {"guide-step", "on", "file", "manual", "input", "wait", "gate", "plan"}
TOP_DIRS = ("deploy", "docs", "appliance", "service", "edge", "relay", "portal", "tests", "fsal",
            "samba-vfs", "dev", "spikes", "ci")
SOURCE_LINE = ". ./omnifile.env && . ./omnifile.state.env"
REQUIRED_SECTIONS = (
    "before you start", "active directory", "images", "deploy the service: phase 1", "private endpoints",
    "database", "app registration", "edge ca", "phase 2", "portal", "hyper-v", "vsphere", "kvm",
    "setup wizard", "enrol the edge", "first dataset and share", "check that it works",
    "troubleshooting", "amazon s3",
)
# camelCase words the guide may show in inline code that are not Bicep names.
NOT_BICEP = {
    "uidNumber", "gidNumber", "loginShell", "unixHomeDirectory", "onPremisesSecurityIdentifier",
    "objectClass", "sAMAccountName", "idmapAd", "x509Subject", "EventBridgeConfiguration",
}
# Files the guide's own steps create inside the repository tree (gitignored or generated).
GENERATED = {
    "deploy/azure/relay-allowed-cidrs.json": "made by relay-ranges.sh (step 13)",
}
# Answers-file sections and the struct each maps to (edge/crates/omnifile-setup/src/answers.rs).
TOML_SECTIONS = {"network": "Network", "data": "Data", "ad": "Ad", "storage": "Storage",
                 "service": "Service", "updates": "Updates", "aws": "Aws"}

FENCE = re.compile(r"^(```+|~~~+)\s*(.*)$")
EDGE_PATH = re.compile(r"(?<![\w.-])(/(?:etc|var/lib|srv|run|usr/lib|usr/libexec|usr/share)/omnifile[\w./-]*)")
VAR = re.compile(r"\$\{?(OMNI_[A-Z0-9_]+)")
PLACEHOLDER = re.compile(r"<(value|secret):([A-Z0-9_]+)>")
CAMEL = re.compile(r"^[a-z][a-z0-9]*[A-Z][A-Za-z0-9]*$")
INLINE = re.compile(r"`([^`\n]+)`")
PATHLIKE = re.compile(r"(?<![\w./-])((?:%s)/[A-Za-z0-9_./@+-]*[A-Za-z0-9_/-])" % "|".join(TOP_DIRS))


@dataclass
class Block:
    line: int  # line of the opening fence
    lang: str
    attrs: dict[str, str]
    body: list[str]
    step: int | None = None

    @property
    def text(self) -> str:
        return "\n".join(self.body)


@dataclass
class Report:
    fails: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)

    def fail(self, code: str, line: int, what: str) -> None:
        self.fails.append(f"FAIL {code} line {line}: {what}")

    def wait(self, line: int, what: str, who: str = "WP1") -> None:
        self.pending.append(f"pending {who} line {line}: {what}")


class Pending:
    """The allowances: names other packages add that main doesn't have yet. `last` is the package whose
    entry the last lookup found (pending-wp8.txt: "WP8")."""

    def __init__(self, paths: list[Path] | Path | None):
        self.entries: dict[tuple[str, str], bool] = {}
        self.who: dict[tuple[str, str], str] = {}
        self.last = "WP1"
        if isinstance(paths, Path):
            paths = [paths]
        for path in paths or []:
            if not path.exists():
                continue
            m = re.search(r"pending-(\w+)\.txt$", path.name)
            who = m.group(1).upper() if m else path.name
            for raw in path.read_text().splitlines():
                raw = raw.strip()
                if raw and not raw.startswith("#"):
                    kind, _, name = raw.partition(" ")
                    self.entries[(kind, name.strip())] = False
                    self.who[(kind, name.strip())] = who

    def _hit(self, key: tuple[str, str]) -> None:
        self.entries[key] = True
        self.last = self.who.get(key, "WP1")

    def module_flags(self, module: str) -> set[str] | None:
        for kind, name in self.entries:
            if kind == "moduleflags" and name.split()[0] == module:
                self._hit((kind, name))
                return set(name.split()[1:])
        return None

    def flags(self, script: str) -> set[str] | None:
        """The flags WP1 declared for a script still to come (`flags <path> <flag> ...`), or None."""
        for kind, name in self.entries:
            if kind == "flags" and name.split()[0] == script:
                self._hit((kind, name))
                return set(name.split()[1:])
        return None

    def has(self, kind: str, name: str) -> bool:
        if (kind, name) in self.entries:
            self._hit((kind, name))
            return True
        return False

    def path_prefix(self, path: str) -> str | None:
        """A pending path that `path` is, or is inside."""
        for kind, name in self.entries:
            if kind == "path" and (path == name or path.startswith(name.rstrip("/") + "/")):
                self._hit((kind, name))
                return name
        return None

    def folder(self, folder: str) -> str | None:
        """A pending path inside `folder`: the folder comes with WP1."""
        for kind, name in self.entries:
            if kind == "path" and name.startswith(folder.rstrip("/") + "/"):
                return name
        return None


# ---------------------------------------------------------------------------------------------- parsing


def parse_blocks(lines: list[str]) -> list[Block]:
    blocks: list[Block] = []
    i = 0
    while i < len(lines):
        m = FENCE.match(lines[i])
        if not m:
            i += 1
            continue
        fence, info = m.group(1), m.group(2).strip()
        words = info.split()
        lang = words[0] if words and "=" not in words[0] else ""
        attrs = {}
        for w in words[1:] if lang else words:
            k, _, v = w.partition("=")
            attrs[k] = v
        start = i + 1
        body = []
        i += 1
        while i < len(lines) and not lines[i].startswith(fence[0] * 3):
            body.append(lines[i])
            i += 1
        blocks.append(Block(start, lang, attrs, body))
        i += 1
    return blocks


def commands(text: str) -> list[str]:
    """Shell text as logical lines: continuations joined, comments dropped."""
    out, cur = [], ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.lstrip().startswith("#"):
            continue
        if line.endswith("\\"):
            cur += line[:-1] + " "
            continue
        cur += line
        if cur.strip():
            out.append(cur)
        cur = ""
    if cur.strip():
        out.append(cur)
    return out


KEYWORDS = {"if", "then", "else", "elif", "fi", "do", "done", "while", "until", "for", "!", "{", "}"}


def segments(line: str) -> list[list[str]]:
    """A command line split into simple commands, each a token list. Quoted text stays one word, so a
    command inside quotes (`--command "python -m ..."`) is an argument, not a command."""
    lex = shlex.shlex(line, posix=True, punctuation_chars="();&|<>")
    lex.whitespace_split = True
    try:
        toks = list(lex)
    except ValueError:
        toks = line.split()
    out: list[list[str]] = []
    cur: list[str] = []
    for t in toks:
        if t and all(c in "();&|<>" for c in t):
            if cur:
                out.append(cur)
            cur = []
            continue
        if t.endswith("$"):  # `x=$` before `(`: a command substitution starts
            t = t[:-1]
        if not cur and (t in KEYWORDS or re.match(r"^[A-Za-z_]\w*=", t)):
            continue  # a keyword or an assignment before the command
        if t:
            cur.append(t)
    if cur:
        out.append(cur)
    return out


def bicep_literal(text: str):
    """A Bicep literal (a string, number, bool, or a flat object or array of them) as Python."""
    text = text.strip()
    if text.startswith("'") and text.endswith("'"):
        return text[1:-1]
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if text in ("true", "false"):
        return text == "true"
    if text.startswith("[") and text.endswith("]"):
        inner = [x.strip() for x in re.split(r"[\n,]", text[1:-1]) if x.strip()]
        return [bicep_literal(x) for x in inner]
    if text.startswith("{") and text.endswith("}"):
        out = {}
        for item in [x.strip() for x in re.split(r"[\n,]", text[1:-1]) if x.strip()]:
            k, _, v = item.partition(":")
            out[k.strip().strip("'")] = bicep_literal(v)
        return out
    raise ValueError(text)


def rust_struct_fields(src: str) -> dict[str, set[str]]:
    structs: dict[str, set[str]] = {}
    for m in re.finditer(r"pub struct (\w+) \{(.*?)\n\}", src, re.S):
        structs[m.group(1)] = set(re.findall(r"^\s*pub (\w+):", m.group(2), re.M))
    return structs


def rust_subcommands(src: str) -> set[str]:
    return set(re.findall(r'^\s*"([a-z][a-z-]*)"(?: \| "[^"]+")* =>', src, re.M))


# ---------------------------------------------------------------------------------------------- checks


class Lint:
    def __init__(self, repo: Path, guide: Path, main_json: dict, pending: Pending, report: Report):
        self.repo, self.guide, self.pending, self.r = repo, guide, pending, report
        self.params = set(main_json.get("parameters", {}))
        #: Parameters main.bicep declares nullable: null there means "the deployment size's value".
        self.nullable = {k for k, v in main_json.get("parameters", {}).items() if v.get("nullable")}
        self.param_defaults = {k: v["defaultValue"] for k, v in main_json.get("parameters", {}).items()
                               if "defaultValue" in v}
        self.outputs = set(main_json.get("outputs", {}))
        self.lines = guide.read_text().splitlines()
        self.blocks = parse_blocks(self.lines)
        self.steps = [b for b in self.blocks if "guide-step" in b.attrs]
        example = repo / "docs/guide/omnifile.env.example"
        ex = example.read_text() if example.exists() else ""
        self.example_vars = set(re.findall(r"^export (OMNI_[A-Z0-9_]+)=", ex, re.M))
        self.inputs = set(re.findall(r"^# input: (OMNI_[A-Z0-9_]+)", ex, re.M))
        self.recorded, self.recorded_prefixes = self._recorded()
        self.data_words = set()  # words the guide's own blocks create (file names), clickable as data
        self.edge_corpus = self._corpus(["edge/crates", "edge/config", "edge/systemd", "appliance/mkosi.extra",
                                         "appliance/build.sh", "fsal/systemd"])
        self.edge_paths = {x.rstrip(".") for x in re.findall(EDGE_PATH, self.edge_corpus)}
        self.service_corpus = self._corpus(["service/src", "relay/src", "edge/crates"])
        self.units = {f.stem for f in repo.rglob("*.service") if "node_modules" not in f.parts
                      and "target" not in f.parts}

    # -- helpers
    def _corpus(self, rels: list[str]) -> str:
        parts = []
        for rel in rels:
            base = self.repo / rel
            files = [base] if base.is_file() else sorted(base.rglob("*")) if base.exists() else []
            for f in files:
                if f.is_file() and "target" not in f.parts and f.suffix not in (".png", ".bin", ".gz", ".zst"):
                    try:
                        parts.append(f.read_text(errors="ignore"))
                    except OSError:
                        pass
        return "\n".join(parts)

    def exists(self, rel: str) -> bool:
        return (self.repo / rel).exists()

    def _recorded(self) -> tuple[set[str], set[str]]:
        names, prefixes = set(), set()
        texts = [b.text for b in self.blocks]
        for tool in sorted((self.repo / "docs/guide").rglob("*.sh")):
            texts.append(tool.read_text())
        for t in texts:
            for m in re.finditer(r"state\.sh\"? set(?:-file)? \"?(OMNI_[A-Z0-9_$]+)", t):
                n = m.group(1)
                if "$" in n:
                    prefixes.add(n.split("$")[0])
                else:
                    names.add(n)
            names.update(re.findall(r"^record \w+ (OMNI_[A-Z0-9_]+)$", t, re.M))
        return names, prefixes

    def known_var(self, name: str) -> bool:
        return (name in self.example_vars or name in self.recorded or name in self.inputs
                or any(name.startswith(p) for p in self.recorded_prefixes))

    def check_path(self, rel: str, line: int) -> None:
        rel = rel.rstrip(".,:;)")
        if rel.endswith("/"):
            rel = rel.rstrip("/")
        if self.exists(rel):
            return
        if rel in GENERATED:
            if not self.exists(str(Path(rel).parent)):
                self.r.fail("path", line, f"{rel}: its folder doesn't exist")
            return
        if "/out/" in rel or rel.endswith("/out"):
            # A script's output folder: the folder it lives in must exist (or come with WP1).
            base = rel.split("/out")[0]
            if self.exists(base):
                return
            if self.pending.folder(base):
                self.r.wait(line, f"{rel} (written by WP1's {base}/)", self.pending.last)
                return
        hit = self.pending.path_prefix(rel)
        if hit:
            self.r.wait(line, rel, self.pending.last)
            return
        self.r.fail("path", line, f"{rel} doesn't exist in the repository")

    # -- the checks
    def check_steps(self) -> None:
        last = 0
        seen: dict[int, int] = {}
        for b in self.steps:
            raw = b.attrs.get("guide-step", "")
            if not raw.isdigit():
                self.r.fail("step", b.line, f"guide-step={raw!r} is not a number")
                continue
            n = int(raw)
            b.step = n
            if n in seen:
                self.r.fail("step-dup", b.line, f"guide-step={n} is also used at line {seen[n]}")
            elif n <= last:
                self.r.fail("step-order", b.line, f"guide-step={n} comes after guide-step={last}")
            seen.setdefault(n, b.line)
            last = max(last, n)
            for k in b.attrs:
                if k not in KEYS:
                    self.r.fail("step-key", b.line, f"unknown key {k}=")
            on = b.attrs.get("on")
            if on not in PLACES:
                self.r.fail("step-on", b.line, f"on={on!r} is not one of {', '.join(sorted(PLACES))}")
            elif b.lang not in PLACES[on]:
                self.r.fail("step-lang", b.line, f"a {on} step is {'/'.join(sorted(PLACES[on]))}, not {b.lang!r}")
            if b.attrs.get("manual", "yes") != "yes":
                self.r.fail("step-key", b.line, "manual= takes only yes")
            if not b.attrs.get("wait", "1").isdigit():
                self.r.fail("step-key", b.line, "wait= takes minutes, a whole number")
            if b.attrs.get("gate", "whatif") != "whatif":
                self.r.fail("step-key", b.line, "gate= takes only whatif")
            if b.attrs.get("plan", "yes") != "yes":
                self.r.fail("step-key", b.line, "plan= takes only yes")
            inp = b.attrs.get("input")
            if inp is not None and inp not in self.inputs:
                self.r.fail("var", b.line, f"input={inp} is not declared (# input: in omnifile.env.example)")
        if not self.steps:
            self.r.fail("step", 1, "the guide has no guide-step blocks")
        # The plan a test run approves: read-only steps first, then the first preview that records the digest.
        # Each later deploy has its own preview: a gate=whatif step that records the digest the deploy approves,
        # after the previous deploy (WP5's harness stops at each gate for the owner's approval).
        gates = [b for b in self.steps if "gate" in b.attrs]
        for b in self.steps:
            if "plan" in b.attrs and (not gates or b.line > gates[0].line):
                self.r.fail("gate", b.line, "a plan=yes step comes before the gate=whatif step")
            if "plan" in b.attrs and b.attrs.get("on") != "workstation":
                self.r.fail("gate", b.line, "a plan=yes step runs on the workstation")
        for g in gates:
            if "OMNI_WHATIF_DIGEST" not in g.text:
                self.r.fail("gate", g.line, "the gate=whatif step records OMNI_WHATIF_DIGEST")
            if g.attrs.get("on") != "workstation":
                self.r.fail("gate", g.line, "a gate=whatif step runs on the workstation")
        gated = False
        for b in self.steps:
            if "gate" in b.attrs:
                gated = True
            if re.search(r"whatif\.sh\b[^\n]*\s--deploy\b", b.text.replace("\\\n", " ")):
                if not gated:
                    self.r.fail("gate", b.line, "a whatif.sh --deploy step with no gate=whatif step since the "
                                "previous deploy")
                gated = False

    def check_vars(self) -> None:
        used: set[str] = set()
        for b in self.steps:
            text = b.text
            names = set(VAR.findall(text))
            for kind, name in PLACEHOLDER.findall(text):
                names.add(name)
                if kind == "secret" and name not in self.inputs:
                    self.r.fail("var", b.line, f"<secret:{name}> is not declared (# input: in omnifile.env.example)")
            if b.attrs.get("on") == "edge-shell":
                first = next((ln.strip() for ln in b.body if ln.strip() and not ln.strip().startswith("#")), "")
                if first != "set -euo pipefail":
                    self.r.fail("strict", b.line, "a step on the filer starts with `set -euo pipefail`, so it stops at its first error")
            if b.attrs.get("on") == "workstation" and VAR.search(text):
                first = next((ln.strip() for ln in b.body if ln.strip() and not ln.strip().startswith("#")), "")
                if first != SOURCE_LINE:
                    self.r.fail("source", b.line, f"a workstation step that reads OMNI_ values starts with `{SOURCE_LINE}`")
            if b.attrs.get("on") != "workstation" and VAR.search(text):
                self.r.fail("var", b.line, f"only workstation steps read omnifile.env; use <value:NAME> on {b.attrs.get('on')}")
            for n in sorted(names):
                used.add(n)
                if not self.known_var(n):
                    self.r.fail("var", b.line, f"{n} is not in omnifile.env.example and no step records it")
        # Tools and the parameters file read values too.
        for extra in [self.repo / "docs/guide/customer.bicepparam", *sorted((self.repo / "docs/guide").rglob("*.sh"))]:
            if extra.exists():
                t = extra.read_text()
                used.update(VAR.findall(t))
                used.update(re.findall(r"readEnvironmentVariable\('(OMNI_[A-Z0-9_]+)'", t))
        for n in sorted(self.example_vars - used):
            self.r.fail("var-unused", 1, f"{n} is in omnifile.env.example but nothing uses it")

    def check_bicep(self) -> None:
        bp = self.repo / "docs/guide/customer.bicepparam"
        if bp.exists():
            for i, ln in enumerate(bp.read_text().splitlines(), 1):
                m = re.match(r"^param (\w+)\s*=", ln)
                if m:
                    self.param(m.group(1), i, f"customer.bicepparam line {i}")
            for m in re.finditer(r"readEnvironmentVariable\('(OMNI_[A-Z0-9_]+)'(, *'[^']*')?\)", bp.read_text()):
                name, default = m.group(1), m.group(2)
                if not self.known_var(name):
                    self.r.fail("var", 1, f"customer.bicepparam reads {name}, which nothing sets")
                if default is None and name not in self.example_vars and name in self.recorded:
                    pass  # recorded before the first deployment (step 9)
        # Inline code in the prose: camelCase names are Bicep names.
        in_block = self._block_lines()
        for i, ln in enumerate(self.lines, 1):
            if i in in_block:
                continue
            for tok in INLINE.findall(ln):
                tok = tok.strip()
                if CAMEL.match(tok) and tok not in NOT_BICEP:
                    if tok in self.params or tok in self.outputs:
                        continue
                    if self.pending.has("param", tok) or self.pending.has("output", tok):
                        self.r.wait(i, f"Bicep name {tok}", self.pending.last)
                        continue
                    self.r.fail("bicep", i, f"`{tok}` is neither a parameter nor an output of main.bicep")
        # Outputs the guide and its tools read.
        texts = [(b.line, b.text) for b in self.steps]
        for tool in sorted((self.repo / "docs/guide").rglob("*.sh")):
            texts.append((1, tool.read_text()))
        for line, t in texts:
            for name in re.findall(r"properties\.outputs\.(\w+)", t):
                self.output(name, line)
            for name in re.findall(r"^record (\w+) OMNI_", t, re.M):
                self.output(name, line)
            for name in re.findall(r"'\.(\w+)\.value'", t):
                self.output(name, line)

    def check_defaults(self) -> None:
        """`param X = empty(v) ? DEFAULT : ...` in customer.bicepparam: DEFAULT is what a client gets, so it
        must be main.bicep's default for X (a test-only value never changes a client's deployment)."""
        bp = self.repo / "docs/guide/customer.bicepparam"
        if not bp.exists():
            return
        text = bp.read_text()
        for m in re.finditer(r"^param (\w+) = empty\(\w+\) \? (.+?) : ", text, re.M | re.S):
            name, literal = m.group(1), m.group(2).strip()
            line = text[: m.start()].count("\n") + 1
            if name not in self.param_defaults:
                continue  # a parameter still to come (pending) or one without a default
            try:
                ours = bicep_literal(literal)
            except ValueError:
                self.r.fail("default", line, f"{name}: can't read the default {literal!r}")
                continue
            if ours != self.param_defaults[name]:
                self.r.fail("default", line, f"{name}: the client default here is {ours!r}, main.bicep's is "
                                             f"{self.param_defaults[name]!r}")

    def param(self, name: str, line: int, where: str) -> None:
        if name in self.params:
            return
        if self.pending.has("param", name):
            self.r.wait(line, f"parameter {name} ({where})", self.pending.last)
            return
        self.r.fail("param", line, f"{name} ({where}) is not a parameter of main.bicep")

    def output(self, name: str, line: int) -> None:
        if name in self.outputs:
            return
        if self.pending.has("output", name):
            self.r.wait(line, f"output {name}", self.pending.last)
            return
        self.r.fail("output", line, f"{name} is not an output of main.bicep")

    def _block_lines(self) -> set[int]:
        s: set[int] = set()
        for b in self.blocks:
            s.update(range(b.line - 1, b.line + len(b.body) + 1))
        return s

    def check_paths(self) -> None:
        in_block = self._block_lines()
        for i, ln in enumerate(self.lines, 1):
            if i in in_block:
                continue
            for tok in INLINE.findall(ln):
                for p in PATHLIKE.findall(tok):
                    self.check_path(p, i)
                if re.match(r"^/(etc|var/lib|srv|run|usr/lib|usr/libexec|usr/share)/omnifile", tok):
                    self._edge_paths(tok, i)
        for b in self.steps:
            for j, ln in enumerate(b.body):
                for p in PATHLIKE.findall(ln):
                    if "$" in p or "<" in p:
                        continue
                    self.check_path(p, b.line + 1 + j)

    def check_scripts(self) -> None:
        setup_main = self.repo / "edge/crates/omnifile-setup/src/main.rs"
        setup_src = setup_main.read_text() if setup_main.exists() else ""
        setup_subs = rust_subcommands(setup_src)
        for b in self.steps:
            if b.lang == "bash":
                for cmd in commands(b.text):
                    for toks in segments(cmd):
                        self._command(toks, b.line, setup_src, setup_subs, depth=0)
                self._settings(b.text, b.line)
                if b.attrs.get("on") == "edge-shell":
                    self._edge_paths(b.text, b.line)
            if b.lang == "powershell":
                for cmd in commands(b.text.replace("`\n", " ")):
                    m = re.match(r"\s*\.\\([\w.-]+\.ps1)(.*)", cmd)
                    if m:
                        self._ps1(m.group(1), m.group(2), b.line)

    def _command(self, toks: list[str], line: int, setup_src: str, setup_subs: set[str], depth: int) -> None:
        self._module(toks, line)
        self._script(toks, line)
        self._setup(toks, line, setup_src, setup_subs)
        self._psql(toks, line)
        self._unit(toks, line)
        # A command given as one quoted word (ssh host '...', bash -c "...", --command "..."): its own commands.
        if depth < 3:
            for tok in toks[1:]:
                if " " in tok:
                    for sub_toks in segments(tok):
                        self._command(sub_toks, line, setup_src, setup_subs, depth + 1)

    def _unit(self, toks: list[str], line: int) -> None:
        if not toks:
            return
        names: list[str] = []
        if toks[0] in ("systemctl", "sudo") and "systemctl" in toks:
            rest = toks[toks.index("systemctl") + 1:]
            if rest and not rest[0].startswith("-"):
                names = [t for t in rest[1:] if not t.startswith("-")]
        if "journalctl" in toks:
            j = toks[toks.index("journalctl") + 1:]
            names += [j[k + 1] for k, t in enumerate(j[:-1]) if t in ("-u", "--unit")]
        for n in names:
            unit = n.removesuffix(".service")
            if re.match(r"^(omnifile-|nfs-ganesha)", unit) and unit not in self.units:
                self.r.fail("unit", line, f"{n}: no unit file of that name in the repository")

    def _settings(self, text: str, line: int) -> None:
        for name in sorted(set(re.findall(r"\b(OMNIFILE_[A-Z0-9_]+)\b", text))):
            if name not in self.service_corpus:
                self.r.fail("setting", line, f"{name} is read by no service, relay or edge code")

    def _edge_paths(self, text: str, line: int) -> None:
        for path in sorted(set(re.findall(EDGE_PATH, text))):
            path = path.rstrip(".,:;/")
            known = self.edge_paths
            # A path the sources name; a folder holding one; a copy a step keeps beside one
            # (setup.toml.before-aws, setup.toml.XXXXXX).
            base = re.sub(r"\.[\w-]+$", "", path)
            folder = any(k.startswith(path + "/") for k in known)
            if path not in known and not folder and not (base != path and base in known):
                self.r.fail("edge-path", line, f"{path}: neither the edge's nor the appliance's sources name it")

    def _script(self, toks: list[str], line: int) -> None:
        start = 1 if toks[0] in ("bash", "python3", "sh", "sudo") else 0
        if start >= len(toks):
            return
        exe = toks[start]
        if not re.match(r"^(%s)/.*\.(sh|py)$" % "|".join(TOP_DIRS), exe):
            return
        flags = [t.split("=")[0] for t in toks[start + 1:] if re.match(r"^--?[A-Za-z]", t)]
        if not self.exists(exe):
            if self.pending.path_prefix(exe):
                self.r.wait(line, f"{exe} {' '.join(flags)}".strip(), self.pending.last)
                declared = self.pending.flags(exe)
                if declared is None:
                    self.r.fail("flag", line, f"{exe} is still to come from WP1, and pending-wp1.txt declares no flags for it")
                else:
                    for f in flags:
                        if f not in declared:
                            self.r.fail("flag", line, f"{exe} (still to come from WP1) declares no flag {f}")
            return  # check_paths reports a missing path
        src = (self.repo / exe).read_text()
        if not os.access(self.repo / exe, os.X_OK) and start == 0:
            self.r.fail("script", line, f"{exe} is not executable")
        for f in flags:
            if not re.search(r"(?<![\w-])%s(?![\w-])" % re.escape(f), src):
                self.r.fail("flag", line, f"{exe} has no flag {f}")

    def _setup(self, toks: list[str], line: int, src: str, subs: set[str]) -> None:
        if "omnifile-setup" not in toks:
            return
        i = toks.index("omnifile-setup")
        rest = toks[i + 1:]
        if not rest or rest[0].startswith("-"):
            return
        sub = rest[0].strip("'\"")
        if sub not in subs:
            self.r.fail("subcommand", line, f"omnifile-setup has no subcommand {sub}")
            return
        for f in [t for t in rest[1:] if t.startswith("--")]:
            if f'"{f}"' not in src:
                self.r.fail("flag", line, f"omnifile-setup {sub} has no flag {f}")

    def _psql(self, toks: list[str], line: int) -> None:
        if not toks or toks[0] != "psql" or "-f" not in toks:
            return
        sql = toks[toks.index("-f") + 1]
        given = {toks[k + 1].split("=")[0] for k, t in enumerate(toks[:-1]) if t == "-v"}
        if not self.exists(sql):
            return
        body = (self.repo / sql).read_text()
        body_no_comments = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("--"))
        wanted = set(re.findall(r":'(\w+)'", body_no_comments))
        for v in sorted(wanted - given):
            self.r.fail("psql", line, f"{sql} uses :'{v}', which the step doesn't give (-v {v}=...)")
        for v in sorted(given - wanted - {"ON_ERROR_STOP"}):
            self.r.fail("psql", line, f"the step gives -v {v}, which {sql} doesn't use")

    def _module(self, toks: list[str], line: int) -> None:
        """`python -m omnifile_service...`: its module, subcommand and flags, from the whole command
        (continuation lines joined, quoted words kept whole)."""
        for k in range(len(toks) - 1):
            if toks[k] != "-m" or not toks[k + 1].startswith("omnifile_service"):
                continue
            if k == 0 or not re.match(r"^python3?$", toks[k - 1]):
                continue
            mod, rest = toks[k + 1], toks[k + 2:]
            rel = mod.replace(".", "/")
            candidates = [f"service/src/{rel}/__main__.py", f"service/src/{rel}.py"]
            path = next((c for c in candidates if self.exists(c)), None)
            if path is None:
                if self.pending.has("module", mod):
                    self.r.wait(line, f"{mod} {' '.join(rest)}".strip(), self.pending.last)
                    declared = self.pending.module_flags(mod)
                    for f in [x for x in rest if x.startswith("--")]:
                        if declared is None or f not in declared:
                            self.r.fail("flag", line, f"{mod} (still to come) declares no flag {f}")
                    continue
                self.r.fail("module", line, f"{mod} is not a module of the service")
                continue
            src = (self.repo / path).read_text()
            if path.endswith("__main__.py"):  # the package's other files (its cli.py) hold the parser
                src = "\n".join(f.read_text() for f in sorted((self.repo / path).parent.glob("*.py")))
            rest = [x for x in rest if x]
            if rest and not rest[0].startswith("-") and not rest[0].startswith("/") and not rest[0].startswith("$"):
                sub = rest[0]
                if f'"{sub}"' not in src:
                    if self.pending.has("subcommand", f"{mod} {sub}"):
                        self.r.wait(line, f"{mod} {sub}", self.pending.last)
                        continue
                    self.r.fail("subcommand", line, f"{mod} has no subcommand {sub}")
                    continue
            for f in [t for t in rest if t.startswith("--")]:
                if f'"{f}"' not in src:
                    self.r.fail("flag", line, f"{mod} has no flag {f}")

    def _ps1(self, name: str, args: str, line: int) -> None:
        hits = sorted((self.repo / "docs").rglob(name))
        if not hits:
            # A script an earlier step writes (`>name.ps1`) is the guide's own.
            if any(re.search(r">\s*%s\b" % re.escape(name), b.text) for b in self.steps if b.line < line):
                return
            self.r.fail("path", line, f"{name} isn't in docs/, and no earlier step writes it")
            return
        src = hits[0].read_text()
        params = {p.lower() for p in re.findall(r"\[(?:string|int|switch|string\[\]|bool)\]\s*\$(\w+)", src, re.I)}
        for f in re.findall(r"(?<![\w-])-([A-Za-z]\w*)", args):
            if f.lower() not in params:
                self.r.fail("flag", line, f"{name} has no parameter -{f}")

    def check_portal(self) -> None:
        src_dir = self.repo / "portal/src"
        strings = ""
        if src_dir.exists():
            strings = "\n".join(p.read_text() for p in sorted(src_dir.rglob("*.ts*")) if ".test." not in p.name)
        routes_file = self.repo / "portal/src/app/routes.tsx"
        routes = set(re.findall(r'path: "([^"/:]+)', routes_file.read_text())) if routes_file.exists() else set()
        # Labels made from template literals (`${title} group`): each becomes a pattern.
        templates = []
        for lit in re.findall(r"`([^`\n]*\$\{[^`\n]*)`", strings):
            parts = re.split(r"\$\{[^}]*\}", lit)
            fixed = sum(len(p.replace(" ", "")) for p in parts)
            if fixed >= 5:
                templates.append((re.compile("^" + "(.+)".join(re.escape(p) for p in parts) + "$"), fixed))

        # The strings the portal's source quotes ("Writers", 'Archive'): what a template's variable can be.
        quoted = set(re.findall(r'"([^"\n]{1,80})"', strings)) | set(re.findall(r"'([^'\n]{1,80})'", strings))

        def from_template(label: str) -> bool:
            """A label a template makes: its fixed text, with each variable part a value the guide itself
            names (a file it made, a <value:NAME>) or a string the portal quotes, never free text."""
            for rx, _fixed in templates:
                m = rx.match(label)
                if m and all(PLACEHOLDER.fullmatch(g) or g in data or g in quoted for g in m.groups()):
                    return True
            return False
        data = set()
        for b in self.steps:
            if b.attrs.get("on") != "portal":
                data.update(re.findall(r"[\w.-]+\.\w+", b.text))
                continue
            for j, raw in enumerate(b.body):
                ln = raw.strip()
                if not ln:
                    continue
                verb, _, arg = ln.partition(" ")
                where = b.line + 1 + j
                if verb == "open":
                    seg = arg.strip("/").split("/")[0]
                    if seg and seg not in routes:
                        self.r.fail("portal-route", where, f"/{seg} is not a portal route")
                elif verb in ("click", "fill", "choose"):
                    label = arg.split(" = ")[0].strip() if verb != "click" else arg.strip()
                    if PLACEHOLDER.fullmatch(label) or label in data:
                        continue
                    if label not in strings and not from_template(label):
                        self.r.fail("portal-label", where, f"{verb} {label!r}: no such text in portal/src")
                elif verb == "expect":
                    text = arg.strip()
                    if PLACEHOLDER.fullmatch(text) or text in data or text in strings or from_template(text):
                        continue
                    self.r.fail("portal-expect", where, f"expect {text!r}: no such text in portal/src, and no step makes it")
                else:
                    self.r.fail("portal-verb", where, f"{verb!r}: a portal step line is open, click, fill, choose or expect")

    def check_toml(self) -> None:
        answers = self.repo / "edge/crates/omnifile-setup/src/answers.rs"
        if not answers.exists():
            return
        structs = rust_struct_fields(answers.read_text())
        for b in self.steps:
            if b.lang != "bash":
                continue
            for m in re.finditer(r"<<'?(\w+)'?\n(.*?)\n\1\b", b.text, re.S):
                head = b.text[: m.start()].splitlines()[-1] if b.text[: m.start()] else ""
                line_txt = (head + b.text[m.start(): m.start() + 80]).split("\n")[0]
                if not re.search(r">\s*\S+\.toml", line_txt):
                    continue
                body = re.sub(r"\$\{?\w+\}?", "x", m.group(2))
                try:
                    doc = tomllib.loads(body)
                except tomllib.TOMLDecodeError as e:
                    self.r.fail("toml", b.line, f"the setup answers written here are not TOML: {e}")
                    continue
                for k, v in doc.items():
                    if isinstance(v, dict):
                        fields = structs.get(TOML_SECTIONS.get(k, ""), None)
                        if fields is None:
                            self.r.fail("toml", b.line, f"[{k}] is not a section of the setup answers")
                            continue
                        for sub in v:
                            if sub not in fields:
                                self.r.fail("toml", b.line, f"[{k}] {sub} is not a setup answer")
                    elif k not in structs.get("Answers", set()):
                        self.r.fail("toml", b.line, f"{k} is not a setup answer")

    def check_markers(self) -> None:
        """`<!-- pending WPn: NAME -->` marks prose about another package's unmerged work. Each needs a
        `feature NAME FILE TEXT` entry in that package's pending file; the entry resolves (and so fails)
        once FILE holds TEXT on main."""
        for i, ln in enumerate(self.lines, 1):
            for who, name in re.findall(r"<!-- pending (WP\d+): ([\w-]+) -->", ln):
                entry = next((k for k in self.pending.entries if k[0] == "feature" and k[1].split()[0] == name), None)
                if entry is None:
                    self.r.fail("feature", i, f"text marked pending {who} ({name}), with no feature entry for it")
                    continue
                self.pending._hit(entry)
                self.r.wait(i, f"text about {name}", self.pending.last)

    def check_step_refs(self) -> None:
        numbers = {b.step: b for b in self.steps if b.step is not None}
        in_block = self._block_lines()
        verbs = r"(?:creates|records|makes|writes|prints|copies|sets)"
        for i, ln in enumerate(self.lines, 1):
            if i in in_block:
                continue
            for n in re.findall(r"\b[Ss]teps? (\d+)\b", ln):
                if int(n) not in numbers:
                    self.r.fail("step-ref", i, f"step {n}: the guide has no such step")
            for n, what in re.findall(r"\b[Ss]tep (\d+) %s `([^`]+)`" % verbs, ln):
                b = numbers.get(int(n))
                if b is not None and what not in b.text:
                    self.r.fail("step-ref", i, f"step {n} doesn't name `{what}`, which this line says it makes")

    def check_content(self) -> None:
        heads = [ln.lower() for ln in self.lines if ln.startswith("#")]
        for s in REQUIRED_SECTIONS:
            if not any(s in h for h in heads):
                self.r.fail("section", 1, f"no section about {s!r}")
        text = "\n".join(self.lines)
        if not ("53%" in text and "77%" in text and "krb5p" in text):
            self.r.fail("kerberos", 1, "the Kerberos note must give its cost: about 53% (krb5) and 77% (krb5p) fewer IOPS")
        for i, ln in enumerate(self.lines, 1):
            if not re.search(r"\b(recall|rehydrat)", ln, re.I):
                continue
            for size, unit in re.findall(r"\b(\d+(?:\.\d+)?)\s*(GB|GiB|TB|TiB)\b", ln):
                big = unit.startswith("T") or float(size) > 10
                if big and re.search(r"\b\d+\s*(hours?|minutes?|h)\b", ln) and "no time" not in ln:
                    self.r.fail("recall-promise", i, f"a recall time for a {size} {unit} file: never promise one above 10 GB")

    def check_pending_unused(self) -> None:
        for (kind, name), _used in self.pending.entries.items():
            resolved = (
                (kind == "param" and name in self.params)
                or (kind == "output" and name in self.outputs)
                or (kind == "path" and self.exists(name))
                or (kind == "flags" and self.exists(name.split()[0]))
                or (kind == "feature" and self._feature_landed(name))
            )
            if kind in ("module", "moduleflags"):
                rel = name.split()[0].replace(".", "/")
                resolved = any(self.exists(c) for c in (f"service/src/{rel}/__main__.py", f"service/src/{rel}.py"))
            if kind == "subcommand":
                mod, _, sub = name.partition(" ")
                rel = mod.replace(".", "/")
                for c in (f"service/src/{rel}/__main__.py", f"service/src/{rel}.py"):
                    if self.exists(c) and f'"{sub}"' in (self.repo / c).read_text():
                        resolved = True
            if resolved:
                self.r.fail("pending-resolved", 1, f"{kind} {name} resolves now: remove it from the pending list")

    def _feature_landed(self, entry: str) -> bool:
        parts = entry.split(" ", 2)
        if len(parts) < 3:
            return False
        f = self.repo / parts[1]
        return f.is_file() and parts[2] in f.read_text(errors="ignore")

    def run(self) -> None:
        self.check_steps()
        self.check_defaults()
        self.check_vars()
        self.check_bicep()
        self.check_paths()
        self.check_scripts()
        self.check_portal()
        self.check_toml()
        self.check_content()
        self.check_markers()
        self.check_step_refs()
        self.check_aws_names()
        self.check_product_names()
        self.check_pending_unused()

    # The AWS resources section 17 names: in a test run each ends in -OMNI_AWS_NAME_SUFFIX, so the harness can
    # find and tear them down; for a client the suffix is empty and every name is as it always was.
    AWS_NAMES = re.compile(r"omnifile-(?:edge-s3-read|edge-ca|edges|service|batch|events|s3|\$svc)(?![-\w])")

    # The owner's rule: what a client reads says "API client" or "the API", never the partner product's name.
    # Spelt in two halves so this file doesn't name it; the fixture's mention is the guide being linted.
    PRODUCT = re.compile("prizm" + "anyx", re.I)

    def check_product_names(self) -> None:
        for n, ln in enumerate(self.guide.read_text().splitlines(), start=1):
            if self.PRODUCT.search(ln):
                self.r.fail("product-name", n, "names a partner product: say 'API client' or 'the API'")
        root = self.repo / "docs/guide"
        if not root.is_dir():
            return
        for f in sorted(root.rglob("*")):
            if not f.is_file() or f == self.guide or "fixtures" in f.relative_to(root).parts:
                continue
            try:
                text = f.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for n, ln in enumerate(text.splitlines(), start=1):
                if self.PRODUCT.search(ln):
                    self.r.fail("product-name", 0, f"{f.relative_to(self.repo)} line {n} names a partner "
                                "product: say 'API client' or 'the API'")

    def check_aws_names(self) -> None:
        for b in self.steps:
            if b.attrs.get("on") != "workstation":
                continue
            if not re.search(r"(^|[\s(])aws |arn:aws:", b.text, re.M):
                continue
            for i, ln in enumerate(b.text.splitlines(), start=1):
                for m in self.AWS_NAMES.finditer(ln):
                    if not ln.startswith("$sfx", m.end()):
                        self.r.fail("aws-name", b.line + i, f"AWS name {m.group(0)} without $sfx "
                                    "(sfx=${OMNI_AWS_NAME_SUFFIX:+-$OMNI_AWS_NAME_SUFFIX})")
        for path, after in ((self.repo / "docs/guide/aws/service-role-policy.json", "NAME_SUFFIX"),
                            (self.repo / "docs/guide/aws/accounts-json.sh", "\\($sfx)")):
            if not path.exists():
                continue
            for n, ln in enumerate(path.read_text().splitlines(), start=1):
                for m in self.AWS_NAMES.finditer(ln):
                    if not ln.startswith(after, m.end()):
                        self.r.fail("aws-name", 0, f"{path.relative_to(self.repo)} line {n}: AWS name "
                                    f"{m.group(0)} without {after}")


# ---------------------------------------------------------------------------------- building the params


TEST_RUN = {
    "OMNI_TEST_RUN_ID": "run-20261103-a1", "OMNI_TEST_RUN_EXPIRES": "2026-11-04T18:00Z",
    "OMNI_TEST_PG_SKU_TIER": "Burstable", "OMNI_TEST_PG_SKU_NAME": "Standard_B2ms", "OMNI_TEST_PG_STORAGE_GB": "32",
    "OMNI_TEST_KV_PURGE_PROTECTION": "false", "OMNI_TEST_KV_SOFT_DELETE_DAYS": "7",
    "OMNI_TEST_API_MIN_REPLICAS": "1", "OMNI_TEST_API_MAX_REPLICAS": "2", "OMNI_TEST_WORKER_MAX_REPLICAS": "1",
    "OMNI_TEST_WORM_RETENTION_DAYS": "1", "OMNI_TEST_LOCK_WORM": "false", "OMNI_TEST_BLOB_SOFT_DELETE_DAYS": "1",
    "OMNI_TEST_ALERTS": "false", "OMNI_AWS_NAME_SUFFIX": "run-20261103-a1",
    # A test run deploys the small size, as tests/real/harness/guidevalues.py sets it.
    "OMNI_DEPLOYMENT_SIZE": "small",
}


def sample_env(repo: Path, phase2: bool, test_run: bool = False, small: bool = False) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OMNI_", "AZURE_", "AWS_"))}
    ex = (repo / "docs/guide/omnifile.env.example").read_text()
    for m in re.finditer(r"^export (OMNI_[A-Z0-9_]+)=(.*)$", ex, re.M):
        env[m.group(1)] = shlex.split(m.group(2))[0] if m.group(2).strip() else ""
    guid = "00000000-0000-0000-0000-000000000001"
    env.update({
        "OMNI_ADMIN_OBJECT_ID": guid,
        "OMNI_PG_ADMIN_GROUP_ID": guid,
        "OMNI_ACR_ID": "/subscriptions/x/resourceGroups/rg/providers/Microsoft.ContainerRegistry/registries/x",
    })
    if phase2:
        env.update({
            "OMNI_PHASE": "2",
            "OMNI_EDGE_CA_PEM": "-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----",
            "OMNI_ENROL_SERVER_CA_SHA256": "0" * 64,
            "OMNI_WEB_CLIENT_ID": guid,
            "OMNI_WEB_API_AUDIENCES": json.dumps([f"api://{guid}", guid]),
            "OMNI_TEAMS_APP_CATALOG_ID": guid,
            "OMNI_INTENT_KID": "intent-20261103",
            "OMNI_INTENT_PUBLIC_KEYS": '{"intent-20261103":"' + "A" * 43 + '"}',
            "OMNI_RELAY_CERT_SHA256": "1" * 64,
            "OMNI_AWS_ACCOUNTS_JSON": '{"123456789012":{"region":"eu-west-1"}}',
            "OMNI_AWS_AUDIENCE": f"api://{guid}",
        })
    if test_run:
        env.update(TEST_RUN)
    if small:
        env["OMNI_DEPLOYMENT_SIZE"] = "small"
    return env


def build_params(repo: Path, bicep: list[str], main_params: set[str], pending: Pending, r: Report,
                 nullable: frozenset[str] | set[str] = frozenset()) -> None:
    """Build customer.bicepparam offline, in a copy of the tree, for phase 1 and for phase 2."""
    src = repo / "docs/guide/customer.bicepparam"
    if not src.exists():
        r.fail("params", 1, "docs/guide/customer.bicepparam is missing")
        return
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        shutil.copytree(repo / "deploy/azure", t / "deploy/azure", ignore=shutil.ignore_patterns("relay-allowed-cidrs.json"))
        (t / "deploy/images").mkdir(parents=True)
        lock = repo / "deploy/images/images.lock.json"
        if lock.exists():
            shutil.copy(lock, t / "deploy/images/images.lock.json")
        else:
            pending.path_prefix("deploy/images/images.lock.json")
            r.wait(1, "deploy/images/images.lock.json: a stand-in lock in WP1's documented shape is used for the build")
            stub = {"schema": 1, "platform": "linux/amd64", "images": {
                n: {"repository": f"omnifile-{n}", "digest": "sha256:" + "0" * 64} for n in ("service", "web", "relay")},
                "apps": {"api": "service", "enrol": "service", "worker": "service", "verifier": "service",
                         "web": "web", "relay": "relay"}}
            (t / "deploy/images/images.lock.json").write_text(json.dumps(stub))
        subprocess.run([str(repo / "deploy/azure/relay-ranges.sh"), "--location", "westeurope", "--from-file",
                        str(repo / "deploy/azure/testdata/service-tags.json"), "--out",
                        str(t / "deploy/azure/relay-allowed-cidrs.json")], check=True, capture_output=True)
        (t / "docs/guide").mkdir(parents=True)
        kept, dropped = [], []
        for ln in src.read_text().splitlines():
            m = re.match(r"^param (\w+)\s*=", ln)
            if m and m.group(1) not in main_params and pending.has("param", m.group(1)):
                dropped.append(m.group(1))
                ln = "// pending WP1: " + ln
            kept.append(ln)
        # A dropped param may continue over several lines (an array): comment those out too.
        text = "\n".join(kept)
        text = re.sub(r"^// pending WP1: (param \w+ = .*\[)\n((?:  .*\n)*?)\]", lambda m: "// pending WP1: " + m.group(1)
                      + "\n" + "".join("// " + x + "\n" for x in m.group(2).splitlines()) + "// ]", text, flags=re.M)
        (t / "docs/guide/customer.bicepparam").write_text(text + "\n")
        if dropped:
            r.wait(1, f"customer.bicepparam built without {', '.join(dropped)}")
        for phase2, test_run, small in ((False, False, False), (True, False, False), (False, True, False),
                                        (False, False, True)):
            label = "a test run" if test_run else "phase 2" if phase2 else "a small site" if small else "phase 1"
            out = t / f"params-{label.replace(' ', '-')}.json"
            p = subprocess.run([*bicep, str(t / "docs/guide/customer.bicepparam"), "--outfile", str(out)],
                               capture_output=True, text=True, env=sample_env(repo, phase2, test_run, small))
            errors = [ln for ln in (p.stdout + p.stderr).splitlines() if " Error " in ln or ": Error" in ln]
            if p.returncode != 0 or errors:
                r.fail("params", 1, f"customer.bicepparam doesn't build ({label}):\n    "
                       + "\n    ".join(errors or (p.stdout + p.stderr).splitlines()[-5:]))
                continue
            built = json.loads(out.read_text())
            values = built.get("parameters", {})
            # A null is an unset value, unless main.bicep's parameter is nullable: then the size decides.
            nulls = [k for k, v in values.items() if v.get("value") is None and k not in nullable]
            if nulls:
                r.fail("params", 1, f"customer.bicepparam ({label}) leaves {', '.join(nulls)} empty")
            size = values.get("deploymentSize", {}).get("value")
            if size != ("small" if small or test_run else "standard"):
                r.fail("params", 1, f"customer.bicepparam ({label}) deploys the {size} size")
            if small:
                set_sized = sorted(k for k in nullable if values.get(k, {}).get("value") is not None)
                if set_sized:
                    r.fail("params", 1, f"customer.bicepparam (a small site) sets {', '.join(set_sized)}, "
                                        "so the small size can't")
            tags = values.get("tags", {}).get("value", {})
            if test_run and tags.get("omnifile-run") != TEST_RUN["OMNI_TEST_RUN_ID"]:
                r.fail("params", 1, "customer.bicepparam (a test run) doesn't tag the run")
            if not test_run and any(k.startswith("omnifile-") for k in tags):
                r.fail("params", 1, f"customer.bicepparam ({label}) tags a client's deployment as a test run")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("guide", nargs="?", default=None)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--main-json", required=True)
    ap.add_argument("--pending", action="append", default=[])
    ap.add_argument("--build-params", default=None,
                    help="the command that builds a .bicepparam file (e.g. 'bicep build-params')")
    a = ap.parse_args(argv)
    repo = Path(a.repo).resolve()
    guide = Path(a.guide) if a.guide else repo / "docs/guide/README.md"
    main_json = json.loads(Path(a.main_json).read_text())
    pending = Pending([Path(x) for x in a.pending])
    r = Report()
    lint = Lint(repo, guide, main_json, pending, r)
    lint.run()
    if a.build_params:
        build_params(repo, shlex.split(a.build_params), lint.params, pending, r, lint.nullable)
    for p in r.pending:
        print(p)
    for f in r.fails:
        print(f)
    n = len(lint.steps)
    if r.fails:
        print(f"guide lint: {len(r.fails)} problem(s) in {guide} ({n} steps)")
        return 1
    who = sorted({x.split()[1] for x in r.pending})
    print(f"guide lint: ok, {n} steps, {len(r.pending)} uses pending ({', '.join(who) or 'none'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
