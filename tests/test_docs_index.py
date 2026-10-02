"""Every doc has to be reachable from the index (#186).

`docs/README.md` linked sixteen of thirty-three docs. The other seventeen were
findable only by knowing they existed, which is the same as not existing for
anyone who just joined.
"""

from __future__ import annotations

import posixpath
import re
from pathlib import Path

from conftest import REPO_ROOT

DOCS_DIR = REPO_ROOT / "docs"
INDEX = DOCS_DIR / "README.md"

# A Markdown link to a .md file, with an optional #anchor that is not part of
# the path. Links to directories, URLs and non-Markdown files are not docs.
_LINK_RE = re.compile(r"\]\(([^)#\s]+\.md)(?:#[^)\s]*)?\)")


def linked_docs(index_text: str) -> set[str]:
    """Every .md path the index links, normalized relative to ``docs/``.

    ``normpath`` resolves ``./a.md`` and ``sub/../a.md`` to ``a.md`` while
    keeping ``../a.md`` outside ``docs/``, so a link to the repo root README
    never counts as indexing a doc of the same name.
    """
    return {posixpath.normpath(link) for link in _LINK_RE.findall(index_text)}


def unindexed_docs(index_text: str, docs: set[str]) -> list[str]:
    return sorted(docs - linked_docs(index_text))


def dangling_links(index_text: str, docs_dir: Path) -> list[str]:
    return sorted(link for link in linked_docs(index_text) if not (docs_dir / link).is_file())


def all_docs(docs_dir: Path) -> set[str]:
    index = docs_dir / "README.md"
    return {
        path.relative_to(docs_dir).as_posix() for path in docs_dir.rglob("*.md") if path != index
    }


def test_the_index_links_every_doc() -> None:
    missing = unindexed_docs(INDEX.read_text(encoding="utf-8"), all_docs(DOCS_DIR))
    assert not missing, "docs with no link in docs/README.md:\n" + "\n".join(missing)


def test_the_index_links_nothing_that_is_gone() -> None:
    """A link to a deleted doc reads as coverage and gives a 404."""
    dangling = dangling_links(INDEX.read_text(encoding="utf-8"), DOCS_DIR)
    assert not dangling, f"docs/README.md links files that do not exist: {dangling}"


def test_a_new_doc_would_fail_this() -> None:
    """The guard itself, proved on a synthetic index rather than assumed."""
    index = "[a](a.md) and [b](./sub/b.md#part) and [c](sub/../c.md)"
    assert unindexed_docs(index, {"a.md", "sub/b.md", "c.md", "nobody_indexed.md"}) == [
        "nobody_indexed.md"
    ]


def test_an_anchor_does_not_hide_a_link() -> None:
    assert linked_docs("[x](x.md#a-section)") == {"x.md"}


def test_a_link_outside_docs_does_not_index_a_doc_of_the_same_name() -> None:
    """``../README.md`` is the repo root README, never ``docs/README.md``'s sibling."""
    assert unindexed_docs("[root](../foo.md)", {"foo.md"}) == ["foo.md"]


def test_a_link_to_a_missing_file_is_reported(tmp_path: Path) -> None:
    (tmp_path / "here.md").write_text("# here\n", encoding="utf-8")
    assert dangling_links("[here](here.md) [gone](gone.md#top)", tmp_path) == ["gone.md"]
