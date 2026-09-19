"""Every doc has to be reachable from the index (#186).

`docs/README.md` linked sixteen of thirty-two docs. The other sixteen were
findable only by knowing they existed, which is the same as not existing for
anyone who just joined.
"""

from __future__ import annotations

import re

from conftest import REPO_ROOT

DOCS_DIR = REPO_ROOT / "docs"
INDEX = DOCS_DIR / "README.md"

_LINK_RE = re.compile(r"\]\(([^)]+\.md)\)")


def _linked_paths() -> set[str]:
    text = INDEX.read_text(encoding="utf-8")
    return {match.split("#")[0].lstrip("./") for match in _LINK_RE.findall(text)}


def _all_docs() -> set[str]:
    return {str(path.relative_to(DOCS_DIR)) for path in DOCS_DIR.rglob("*.md") if path != INDEX}


def test_the_index_links_every_doc() -> None:
    missing = sorted(_all_docs() - _linked_paths())
    assert not missing, "docs with no link in docs/README.md:\n" + "\n".join(missing)


def test_the_index_links_nothing_that_is_gone() -> None:
    """A link to a deleted doc reads as coverage and gives a 404."""
    dangling = sorted(link for link in _linked_paths() if not (DOCS_DIR / link).is_file())
    assert not dangling, f"docs/README.md links files that do not exist: {dangling}"


def test_a_new_doc_would_fail_this() -> None:
    """The guard itself, proved rather than assumed."""
    invented = _all_docs() | {"a_doc_nobody_indexed.md"}
    assert sorted(invented - _linked_paths()) == ["a_doc_nobody_indexed.md"]
