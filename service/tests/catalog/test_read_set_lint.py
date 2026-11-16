"""Decision 63: every question of who may read a file goes through `catalog/read_set.py`.

An annexed file's captured ACL (`catalog.acl.read_sids`) isn't who may read it: a folder
above it can keep people out. A caller that reads the raw set, or calls `acl_can_read`
itself, would answer from the captured ACL alone and drift from the rest. This test
fails on any new one, so it can't.

What it looks at (the review of #83 made it harder to dodge):
- Python: the patterns in `RAW`, and in SQL strings (not docstrings) a bare
  `read_sids` or `deny_sids` outside an INSERT's column list;
- `api.acls.can_read` imported, and `deny_first_sets` called, outside `CALLERS`;
- migrations after 0052: `acl_can_read(` and the raw sets anywhere but in the
  definition of the predicate itself (`PREDICATE`).

Allowed, each for its reason:
- `catalog/read_set.py`, the helper;
- writers of the sets, which derive and store them from an ACL they were given
  (INSERT column lists, which this test doesn't count);
- `catalog/restricted.py`, decision 39's check of an OmniFile share's folders: which AD
  groups a folder admits, not whether someone may read a file;
- `annex/acl.py`, whose `literally_anyone_reads` takes a set it is handed.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "omnifile_service"
MIGRATIONS = SRC / "catalog" / "migrations"

#: SQL that answers from the captured ACL alone, or reads its sets.
RAW = [
    re.compile(r"acl_can_read\s*\("),
    re.compile(r"\b\w+\.(read_sids|deny_sids)\b"),
    re.compile(r"""\[\s*f?["'](acl_)?(read_sids|deny_sids)["']\s*\]"""),
    re.compile(r"""\.get\(\s*f?["'](acl_)?(read_sids|deny_sids)["']"""),
    re.compile(r"\bcan_read_sql\b"),
]
BARE = re.compile(r"\b(read_sids|deny_sids)\b")
INSERT_COLUMNS = re.compile(r"INSERT\s+INTO\s+[\w.]+\s*\([^)]*\)", re.I | re.S)
ALLOWED = {
    "catalog/read_set.py",
    "catalog/restricted.py",
}

#: Python's own checks of a set, and where each may be used.
PY_CHECKS = {
    # Who a set lets in, for a set that isn't a file's read set.
    re.compile(r"from\s+\.+api\.acls\s+import\s+[^\n]*\bcan_read\b|\bacls\.can_read\b"): {
        "catalog/read_set.py",
        "annex/returns.py",  # who may add a file to a share folder (write sets)
        "worker/share_placements.py",  # who may write a file (write sets)
    },
    # A captured or staged NT/NFSv4 ACL's sets, derived to be stored or compared.
    re.compile(r"\bdeny_first_sets\s*\("): {
        "annex/acl.py",
        "api/annex_placements.py",  # the staged ACL's sets, then Folders.allow
    },
}

#: The predicate's own functions, which a migration may define (and redefine).
PREDICATE = {"can_read_file", "passable_gates", "passable_dirs", "dir_passes"}
FIRST = 52


def _sql_strings(tree: ast.AST) -> list[tuple[int, str]]:
    """Each string literal and f-string's text, docstrings aside."""
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docs.add(id(body[0].value))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            text = "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in node.values)
            out.append((node.lineno, text))
        elif (
            isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs
        ):
            out.append((node.lineno, node.value))
    return out


def _hits() -> list[str]:
    out = []
    for p in sorted(SRC.rglob("*.py")):
        rel = p.relative_to(SRC).as_posix()
        text = p.read_text()
        for pat, where in PY_CHECKS.items():
            if rel not in where:
                for n, line in enumerate(text.splitlines(), 1):
                    if pat.search(line.split("#", 1)[0]):
                        out.append(f"{rel}:{n}: {line.strip()}")
        if rel in ALLOWED:
            continue
        seen = set()
        for n, line in enumerate(text.splitlines(), 1):
            code = line.split("#", 1)[0]
            if any(r.search(code) for r in RAW):
                seen.add(n)
                out.append(f"{rel}:{n}: {line.strip()}")
        for n, s in _sql_strings(ast.parse(text)):
            if BARE.search(INSERT_COLUMNS.sub("", s)) and n not in seen:
                seen.add(n)
                out.append(f"{rel}:{n}: a bare read_sids or deny_sids in SQL")
    for p in sorted(MIGRATIONS.glob("*.sql")):
        m = re.match(r"(\d+)_", p.name)
        if not m or int(m.group(1)) <= FIRST:
            continue
        sql = re.sub(r"--[^\n]*", "", p.read_text())
        # Blank out the predicate's own definitions.
        sql = re.sub(
            r"CREATE\s+(OR\s+REPLACE\s+)?FUNCTION\s+catalog\.(\w+)\s*\(.*?\$\$.*?\$\$",
            lambda f: "" if f.group(2) in PREDICATE else f.group(0),
            sql,
            flags=re.S | re.I,
        )
        sql = INSERT_COLUMNS.sub("", sql)
        for n, line in enumerate(sql.splitlines(), 1):
            if RAW[0].search(line) or RAW[1].search(line) or BARE.search(line):
                out.append(f"{p.name}:{n}: {line.strip()}")
    return out


def test_nothing_reads_a_files_read_set_but_the_helper() -> None:
    hits = _hits()
    assert not hits, (
        "decision 63: ask catalog/read_set.py (readable_sql, acl_sets and captured,"
        " Folders.allow) instead:\n" + "\n".join(hits)
    )


def test_the_lint_sees_each_kind_of_raw_read(tmp_path, monkeypatch) -> None:
    """It would pass on nothing at all, so each pattern is shown to catch."""
    import sys

    me = sys.modules[__name__]

    (tmp_path / "x.py").write_text(
        "a = 'SELECT catalog.acl_can_read(x, y, %s, %s)'\n"
        "b = 'SELECT a.read_sids FROM catalog.acl a'\n"
        "c = row['read_sids']\n"
        'd = row.get("deny_sids")\n'
        "e = can_read_sql()\n"
        "f = 'INSERT INTO catalog.acl (read_sids, deny_sids) VALUES (%s, %s)'\n"
        "g = 'SELECT read_sids FROM catalog.acl'\n"
        'h = f"SELECT {x} FROM catalog.acl WHERE deny_sids = {y}"\n'
        "from ..api.acls import access_sets, can_read\n"
        "j = deny_first_sets(owner, aces)\n"
        'def k():\n    """read_sids, in a docstring"""\n'
    )
    mig = tmp_path / "catalog" / "migrations"
    mig.mkdir(parents=True)
    (mig / "0052_x.sql").write_text("SELECT catalog.acl_can_read(a, b, c, d, e);\n")
    (mig / "0099_y.sql").write_text(
        "CREATE FUNCTION catalog.can_read_file(p int) RETURNS boolean LANGUAGE sql AS $$\n"
        "    SELECT catalog.acl_can_read(a.read_sids, b, c, d, e)\n$$;\n"
        "INSERT INTO catalog.acl (read_sids, deny_sids) VALUES ('{}', '{}');\n"
        "CREATE FUNCTION catalog.other() RETURNS boolean LANGUAGE sql AS $$\n"
        "    SELECT catalog.acl_can_read(x, y, z, w, v)\n$$;\n"
        "SELECT read_sids FROM catalog.acl;\n"
    )
    monkeypatch.setattr(me, "SRC", tmp_path)
    monkeypatch.setattr(me, "MIGRATIONS", mig)
    hits = me._hits()
    py = sorted(int(h.split(":")[1]) for h in hits if h.startswith("x.py"))
    assert py == [1, 2, 3, 4, 5, 7, 8, 9, 10], hits
    assert sorted(h.split(":")[0] for h in hits if not h.startswith("x.py")) == [
        "0099_y.sql",
        "0099_y.sql",
    ], hits
