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


def _duplicate_assignments(path: Path) -> dict[str, list[int]]:
    """Module-level names assigned more than once, mapped to their line numbers."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    lines: dict[str, list[int]] = {}
    for node in tree.body:  # module level only; a name rebound in a function is fine
        targets = []
        if isinstance(node, ast.Assign):
            targets = [t for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target] if node.value is not None else []
        for target in targets:
            lines.setdefault(target.id, []).append(node.lineno)
    return {name: at for name, at in lines.items() if len(at) > 1}


@pytest.mark.parametrize("path", _modules(), ids=lambda p: str(p.relative_to(SRC)))
def test_no_module_level_name_is_assigned_twice(path: Path) -> None:
    duplicates = _duplicate_assignments(path)
    assert not duplicates, (
        f"{path.relative_to(REPO_ROOT)} binds these module-level names more than once, "
        f"so the earlier definition is dead and any function reading the global gets "
        f"the later one: {duplicates}"
    )


def test_the_guard_catches_a_shadowed_constant(tmp_path: Path) -> None:
    """The guard itself, proved rather than assumed."""
    shadowed = tmp_path / "shadowed.py"
    shadowed.write_text(
        'import re\n_CLAIM_RE = re.compile("a")\n_OTHER = 1\n_CLAIM_RE = re.compile("b")\n',
        encoding="utf-8",
    )
    assert _duplicate_assignments(shadowed) == {"_CLAIM_RE": [2, 4]}


def test_a_name_rebound_inside_a_function_is_not_flagged(tmp_path: Path) -> None:
    """Local rebinding is ordinary code and must not trip this."""
    local = tmp_path / "local.py"
    local.write_text("def f():\n    x = 1\n    x = 2\n    return x\n", encoding="utf-8")
    assert _duplicate_assignments(local) == {}
