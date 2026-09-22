"""No module may bind a matcher name twice (#192 review finding).

`refund_policy.py` defined `_OUTAGE_CLAIM_RE` at two different module-level
lines. Python binds the name to the last one, and `_claims_outage` reads that
global at call time, so the release-blocking `ticket_outage_claim_unsupported`
check silently stopped recognizing the word "disruption". Every test, the repo
check and both CI gates stayed green, because no committed fixture uses it.

A shadowed module-level constant is invisible to ruff, to pytest and to review.
This walks the AST of every module in the package and fails naming the file,
the symbol and both line numbers.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from conftest import REPO_ROOT

SRC = REPO_ROOT / "src" / "trace_harness"


def _modules() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


#: Scopes that introduce their own namespace. A name rebound inside one of
#: these is ordinary code and must not be flagged.
_OWN_SCOPE = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)

#: Branching statements whose arms are alternatives rather than sequence. A
#: name assigned in both an if and its else, or a try and its except, is one
#: binding written twice on purpose, which is how an import fallback is
#: spelled. Sibling arms are merged rather than summed.
_SIBLING_ARMS = {
    ast.If: ("body", "orelse"),
    ast.Try: ("body", "handlers", "orelse", "finalbody"),
}


def _names_bound(node: ast.AST) -> list[tuple[str, int]]:
    """Names this single statement binds, with the line it binds them on."""
    bound: list[tuple[str, int]] = []
    targets: list[ast.AST] = []
    if isinstance(node, ast.Assign):
        targets = list(node.targets)
    elif isinstance(node, ast.AnnAssign) and node.value is not None:
        targets = [node.target]
    elif isinstance(node, ast.AugAssign):
        targets = [node.target]
    elif isinstance(node, _OWN_SCOPE) and hasattr(node, "name"):
        return [(node.name, node.lineno)]

    stack = list(targets)
    while stack:
        target = stack.pop()
        if isinstance(target, ast.Name):
            bound.append((target.id, node.lineno))
        elif isinstance(target, (ast.Tuple, ast.List)):
            stack.extend(target.elts)
        elif isinstance(target, ast.Starred):
            stack.append(target.value)
    return bound


def _walk_bindings(body: list[ast.stmt]) -> dict[str, list[int]]:
    """Every name bound in ``body``, descending into control flow but not scopes.

    Descending into `if`/`try`/`for`/`while`/`with` matters because the original
    bug indented by two spaces is invisible to a top-level-only walk. Not
    descending into functions and classes matters because rebinding a local is
    ordinary. Sibling branches of the same statement are merged, so an
    `if`/`else` pair assigning one name counts once.
    """
    found: dict[str, list[int]] = {}

    def merge(into: dict[str, list[int]], other: dict[str, list[int]]) -> None:
        for name, at in other.items():
            into.setdefault(name, []).extend(at)

    for node in body:
        for name, line in _names_bound(node):
            found.setdefault(name, []).append(line)

        arms = _SIBLING_ARMS.get(type(node))
        if arms is not None:
            # Each arm is walked on its own, then the arms are collapsed to one
            # binding per name. Two arms assigning the same name are one
            # binding written twice on purpose, so they contribute a single
            # line rather than both. A name bound twice *within* one arm still
            # carries both lines and is still caught.
            per_arm: list[dict[str, list[int]]] = []
            for arm in arms:
                statements = getattr(node, arm, []) or []
                if arm == "handlers":
                    per_arm.extend(_walk_bindings(h.body) for h in statements)
                else:
                    per_arm.append(_walk_bindings(statements))
            collapsed: dict[str, list[int]] = {}
            for bindings in per_arm:
                for name, at in bindings.items():
                    if len(at) > 1:  # a real duplicate inside one arm
                        collapsed.setdefault(name, []).extend(at)
                    elif name not in collapsed:
                        collapsed[name] = list(at)
            merge(found, collapsed)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith)):
            merge(found, _walk_bindings(node.body))
            merge(found, _walk_bindings(getattr(node, "orelse", []) or []))
    return found


def _duplicate_assignments(path: Path) -> dict[str, list[int]]:
    """Module-level names bound more than once, mapped to their line numbers."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    lines = _walk_bindings(tree.body)
    return {name: sorted(set(at)) for name, at in lines.items() if len(set(at)) > 1}


@pytest.mark.parametrize("path", _modules(), ids=lambda p: str(p.relative_to(SRC)))
def test_no_module_level_name_is_assigned_twice(path: Path) -> None:
    duplicates = _duplicate_assignments(path)
    assert not duplicates, (
        f"{path.relative_to(REPO_ROOT)} binds these module-level names more than once, "
        f"so the earlier definition is dead and any function reading the global gets "
        f"the later one: {duplicates}"
    )


def test_the_guard_actually_covers_the_package(tmp_path: Path) -> None:
    """A guard that silently covers nothing passes forever.

    ``_modules()`` runs at collection time. If the source layout moves, an
    empty list makes pytest report one skipped test and exit 0 while both
    self-proofs stay green, so the count and a known file are asserted here.
    """
    modules = _modules()
    assert len(modules) > 20, f"only {len(modules)} modules found under {SRC}"
    names = {str(m.relative_to(SRC)) for m in modules}
    assert "verifiers/refund_policy.py" in names


def test_the_guard_catches_the_bug_that_shipped(tmp_path: Path) -> None:
    """The real pre-fix file, not a hand-written stand-in.

    #192 defined ``_OUTAGE_CLAIM_RE`` at lines 157 and 223. A self-proof built
    from a four-line toy shares the blind spots of the thing it proves.
    """
    import subprocess

    before = subprocess.run(
        ["git", "show", "b830c99:src/trace_harness/verifiers/refund_policy.py"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    if before.returncode != 0:  # shallow clone or rewritten history
        pytest.skip("commit b830c99 not available in this checkout")
    tmp = tmp_path / "refund_policy_before.py"
    tmp.write_text(before.stdout, encoding="utf-8")
    try:
        assert _duplicate_assignments(tmp) == {"_OUTAGE_CLAIM_RE": [157, 223]}
    finally:
        tmp.unlink(missing_ok=True)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('import re\n_R = re.compile("a")\n_O = 1\n_R = re.compile("b")\n', {"_R": [2, 4]}),
        # The original bug indented by two spaces. A top-level-only walk misses it.
        ("import os\nif os.name:\n    _R = 1\n_R = 2\n_R = 3\n", {"_R": [3, 4, 5]}),
        # A def replacing a constant. ruff F811 does not report this either.
        ("_R = 1\ndef _R():\n    return None\n", {"_R": [1, 2]}),
        # Tuple and starred targets were dropped entirely before.
        ("_A, _B = 1, 2\n_A = 3\n", {"_A": [1, 2]}),
        ("_H, *_T = [1, 2]\n_H = 9\n", {"_H": [1, 2]}),
        ("_C = 1\n_C += 1\n", {"_C": [1, 2]}),
    ],
    ids=["plain", "nested-in-if", "def-shadows-const", "tuple", "starred", "augassign"],
)
def test_the_guard_catches_each_shadowing_form(
    tmp_path: Path, source: str, expected: dict[str, list[int]]
) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text(source, encoding="utf-8")
    assert _duplicate_assignments(probe) == expected


@pytest.mark.parametrize(
    ("source", "why"),
    [
        ("def f():\n    x = 1\n    x = 2\n    return x\n", "function local"),
        ("class C:\n    x = 1\n    x = 2\n", "class body"),
        ("_X = [i for i in range(3)]\n_Y = [i for i in range(3)]\n", "comprehension target"),
        # Import fallback. Both arms bind one name on purpose.
        (
            "try:\n    import ujson as _J\n    _M = 1\nexcept ImportError:\n    _M = 2\n",
            "try/except arms",
        ),
        ("import os\nif os.name:\n    _M = 1\nelse:\n    _M = 2\n", "if/else arms"),
    ],
    ids=["function", "class", "comprehension", "try-except", "if-else"],
)
def test_legitimate_rebinding_is_not_flagged(tmp_path: Path, source: str, why: str) -> None:
    """Sibling branches are alternatives, not a sequence of two bindings."""
    probe = tmp_path / "probe.py"
    probe.write_text(source, encoding="utf-8")
    assert _duplicate_assignments(probe) == {}, why
