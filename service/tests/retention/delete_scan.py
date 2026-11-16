"""Static guards for the archive-copy check (test_archive_copy_check.py):
which functions copy an object and delete one, whether they use
`copy_mismatch`'s answer, and who calls a backend's delete.

Per module, a function's calls are its own plus those of the module's
functions it calls (one level), so a move split across helpers is seen."""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

#: Calls that copy an object server-side (Azure SDK, S3, our backends).
COPIES = frozenset({
    "_start_copy", "_copy_version", "copy_object", "start_copy_from_url", "copy_from_url",
    "stage_block_from_url", "upload_blob_from_url", "upload_part_copy", "copy_blob",
    "copy", "begin_copy",
})  # fmt: skip
#: Calls that delete an object.
DELETES = frozenset({"delete", "delete_blob", "_delete_version", "delete_object",
                     "delete_objects", "delete_blobs"})  # fmt: skip
CHECK = "copy_mismatch"


def _name(call: ast.Call) -> str:
    f = call.func
    return f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")


def _calls(fn: ast.AST) -> set[str]:
    return {_name(n) for n in ast.walk(fn) if isinstance(n, ast.Call)}


def _check_used(fn: ast.AST) -> bool:
    """`fn` calls copy_mismatch and acts on the answer: the call sits in a
    condition, or its result is assigned to a name that an `if` or `while`
    statement tests (a conditional expression doesn't count: it only picks a
    value, and may itself sit in a branch that never runs)."""
    parents = {c: p for p in ast.walk(fn) for c in ast.iter_child_nodes(p)}
    tested = {n.id for t in ast.walk(fn) if isinstance(t, ast.If | ast.While)
              for n in ast.walk(t.test) if isinstance(n, ast.Name)}  # fmt: skip
    for call in ast.walk(fn):
        if not (isinstance(call, ast.Call) and _name(call) == CHECK):
            continue
        node = call
        while node in parents and not isinstance(parents[node], ast.stmt):
            node = parents[node]
            if isinstance(node, ast.If | ast.While | ast.IfExp | ast.Compare | ast.BoolOp):
                return True
        stmt = parents.get(node)
        if isinstance(stmt, ast.If | ast.While | ast.Return | ast.Raise):
            return True
        if isinstance(stmt, ast.Assign | ast.AnnAssign):
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            if any(isinstance(t, ast.Name) and t.id in tested for t in targets):
                return True
    return False


@dataclass
class Fn:
    module: str
    name: str
    node: ast.AST
    calls: set[str]  # its own and, one level, its module callees'
    checked: bool  # it or a callee acts on copy_mismatch


def functions(path: Path, root: Path) -> Iterator[Fn]:
    tree = ast.parse(path.read_text())
    defs: dict[str, list[ast.AST]] = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef):
            defs.setdefault(n.name, []).append(n)
    module = str(path.relative_to(root))
    for name, nodes in defs.items():
        for node in nodes:
            own = _calls(node)
            callees = [c for callee in own - {name} for c in defs.get(callee, [])]
            calls = own.union(*(_calls(c) for c in callees))
            checked = _check_used(node) or any(_check_used(c) for c in callees)
            yield Fn(module, name, node, calls, checked)


def moves(root: Path) -> dict[tuple[str, str], bool]:
    """Every function that copies and deletes: (module, name) -> checked."""
    return {(f.module, f.name): f.checked
            for p in sorted(root.rglob("*.py")) for f in functions(p, root)
            if f.calls & COPIES and f.calls & DELETES}  # fmt: skip


def deleters(root: Path, *, skip: str = "backends/") -> set[tuple[str, str]]:
    """(module, function) of every `<anything>.delete(...)` call outside
    `skip`, whatever the receiver (a name, an attribute, a call)."""
    out = set()
    for p in sorted(root.rglob("*.py")):
        rel = str(p.relative_to(root))
        if skip and rel.startswith(skip):
            continue
        for f in functions(p, root):
            for n in ast.walk(f.node):
                if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                        and n.func.attr == "delete"):  # fmt: skip
                    out.add((rel, f.name))
    return out
