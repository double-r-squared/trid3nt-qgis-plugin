"""A package map names what is there, and everything it names is there.

A map README's tables are the assertion: every row's first cell must resolve to a
file or subfolder that exists, and every tracked top-level module and immediate
subfolder must appear in one of them.
"""

from __future__ import annotations

import re
import subprocess
from functools import lru_cache
from pathlib import Path

import pytest

from tests.hygiene import _source as source

REPO = source.REPO_ROOT

#: Trees a contributor navigates as PACKAGES. `docs/` is prose and frozen
#: evidence: a README there is a reader's map, not a package's, and
#: `docs/proof/` holds the sandbox scripts a run was proved with.
MAP_TREES = ("trid3nt_server", "plugin", "contracts", "scripts", "tests", "workers")
#: Directory entries that are never a package's own content.
IGNORED = frozenset({"__pycache__", ".pytest_cache"})
#: A first cell that NAMES a file or folder: a bare relative path, nothing else.
#: An angle bracket makes the cell a PATTERN (`<group>/<spec>/hooks.py`), which
#: names a shape rather than a member, and is left alone.
_PATH_CELL = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_.-]+)*/?")


@lru_cache(maxsize=1)
def _tracked() -> tuple[str, ...]:
    listing = subprocess.run(
        ["git", "ls-files", "-z", *MAP_TREES], cwd=REPO,
        capture_output=True, text=True, check=True).stdout
    return tuple(path for path in listing.split("\0") if path)


@lru_cache(maxsize=1)
def _maps() -> tuple[Path, ...]:
    """Every package map: a README beside at least one tracked module."""
    directories = {}
    for relative in _tracked():
        path = Path(relative)
        if path.parts[0] not in MAP_TREES:
            continue
        directories.setdefault(path.parent, set()).add(path.name)
    return tuple(sorted(
        REPO / directory / "README.md"
        for directory, names in directories.items()
        if "README.md" in names and any(n.endswith(".py") for n in names)))


def _table_cells(readme: Path) -> list[str]:
    """The first cell of every table BODY row, backticks stripped."""
    # A table's header row is the line before its separator, and a header names
    # the column ("file", "folder") rather than a member of the package.
    rows: list[str] = []
    for line in readme.read_text(encoding="utf-8").splitlines():
        if not line.lstrip().startswith("|"):
            rows.append("")
            continue
        first = line.strip().strip("|").split("|")[0].strip()
        if set(first) <= set("- :") and first:
            rows[-1] = ""
            continue
        rows.append(first.strip("`").strip())
    return [row for row in rows if row]


def _path_like(cell: str) -> bool:
    """Whether a first cell is naming a member of the package at all.
    A prose cell, a pytest node id or a markdown image is a row about something
    else, and a map is allowed to carry one."""
    return bool(_PATH_CELL.fullmatch(cell))


def _contents(directory: Path) -> tuple[set[str], set[str]]:
    """The tracked top-level modules and immediate subfolders of a package."""
    relative = directory.relative_to(REPO).as_posix()
    prefix = f"{relative}/" if relative != "." else ""
    modules, folders = set(), set()
    for tracked in _tracked():
        if not tracked.startswith(prefix):
            continue
        tail = tracked[len(prefix):]
        if "/" in tail:
            folders.add(tail.split("/", 1)[0])
        elif tail.endswith(".py"):
            modules.add(tail)
    return modules, folders - IGNORED


@pytest.mark.parametrize("readme", _maps(), ids=lambda p: str(p.relative_to(REPO)))
def test_a_map_names_only_what_exists(readme: Path) -> None:
    directory = readme.parent
    absent = [cell for cell in _table_cells(readme)
              if _path_like(cell) and not (directory / cell.rstrip("/")).exists()]
    assert not absent, (f"{readme.relative_to(REPO)} names entries that do not "
                        f"exist here: " + ", ".join(sorted(absent)))


@pytest.mark.parametrize("readme", _maps(), ids=lambda p: str(p.relative_to(REPO)))
def test_a_map_names_everything_that_exists(readme: Path) -> None:
    modules, folders = _contents(readme.parent)
    named = {cell.rstrip("/") for cell in _table_cells(readme)}
    missing = sorted((modules | folders) - named)
    assert not missing, (f"{readme.relative_to(REPO)} does not name: "
                        + ", ".join(missing))


#: The decisions index is a map of a folder rather than of a package, so the
#: table walk above does not reach it: its rows are markdown links, not cells.
DECISIONS = REPO / "docs" / "decisions"
_INDEX_ENTRY = re.compile(r"^- \[(\d{4}) - .+?\]\((\d{4}-[\w.-]+\.md)\)$", re.MULTILINE)


def test_the_decisions_index_is_the_folder() -> None:
    """Every numbered record is listed once, under its own number."""
    entries = _INDEX_ENTRY.findall((DECISIONS / "README.md").read_text(encoding="utf-8"))
    listed = [target for _, target in entries]
    records = sorted(path.name for path in DECISIONS.glob("[0-9][0-9][0-9][0-9]-*.md"))
    assert listed == records, (
        "the index and the folder disagree: "
        f"unlisted={sorted(set(records) - set(listed))} "
        f"absent={sorted(set(listed) - set(records))} "
        f"ordered={listed == sorted(listed)}")
    misnumbered = [target for number, target in entries if not target.startswith(number)]
    assert not misnumbered, "index rows whose number is not the record's: " + ", ".join(misnumbered)
