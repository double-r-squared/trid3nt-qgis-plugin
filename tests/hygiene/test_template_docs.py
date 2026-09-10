"""The template pages are generated, and their figures are not stale.

A page regenerated from the declaration must equal the page on disk, and a figure
whose stamped commit predates its template's last change is a red test rather
than something a reader is expected to remember.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

import pytest

from tests.hygiene import _source as source

REPO = source.REPO_ROOT
DOC_ROOT = REPO / "docs" / "templates"
GENERATOR = REPO / "scripts" / "instruments" / "gen_template_docs.py"
RENDERER = REPO / "scripts" / "packet" / "doc_renders.py"

#: The doc-size budget, as bytes. The ruling is "a composite near 300 KB, an
#: animation near 1-2 MB"; these are the ceilings that keep a page a page.
COMPOSITE_MAX_BYTES = 600_000
ANIMATION_MAX_BYTES = 3_000_000

pytest.importorskip("PIL")


@lru_cache(maxsize=1)
def _generator():
    """The generator, imported by path - ``scripts/`` is not a package."""
    spec = importlib.util.spec_from_file_location("gen_template_docs", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("gen_template_docs", module)
    spec.loader.exec_module(module)
    return module


@lru_cache(maxsize=1)
def _templates() -> dict:
    return _generator()._templates()


@lru_cache(maxsize=1)
def _pages() -> dict:
    return _generator().generate()


def _package_dir(name: str) -> Path:
    """The template package the page is generated from."""
    # The registered function is synthesized by the runtime factory and carries
    # ITS module, not the template's; the STEERING body is declared in the
    # recipe file, so it is what names the package.
    steering = _templates()[name].plan_decl.steering
    return Path(sys.modules[steering.__module__].__file__).resolve().parent


def _last_change(path: Path) -> str:
    """The commit that last touched a path, or "" when git knows of none."""
    out = subprocess.run(
        ["git", "-C", str(REPO), "log", "-1", "--format=%H", "--", str(path)],
        capture_output=True, text=True, check=True).stdout.strip()
    return out


def _is_ancestor(older: str, newer: str) -> bool:
    return subprocess.run(
        ["git", "-C", str(REPO), "merge-base", "--is-ancestor", older, newer],
        capture_output=True).returncode == 0


def _stamped_commit(path: Path) -> str | None:
    """The commit a figure carries, with a dirty-tree marker taken off."""
    spec = importlib.util.spec_from_file_location(
        "doc_size", REPO / "scripts" / "packet" / "doc_size.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stamped = module.read_commit_stamp(path)
    return stamped.removesuffix("-dirty") if stamped else None


def test_every_registered_template_has_a_page() -> None:
    missing = sorted(name for name in _templates()
                     if not (DOC_ROOT / f"{name}.md").is_file())
    assert not missing, ("registered templates with no page (run "
                         f"{GENERATOR.relative_to(REPO)}): " + ", ".join(missing))


def test_every_page_is_byte_current() -> None:
    stale = sorted(relative for relative, content in _pages().items()
                   if not (REPO / relative).is_file()
                   or (REPO / relative).read_text(encoding="utf-8") != content)
    assert not stale, ("these pages are not what the declaration generates; run "
                       f"{GENERATOR.relative_to(REPO)}: " + ", ".join(stale))


def test_every_template_has_doc_figures() -> None:
    bare = sorted(name for name in _templates()
                  if not (DOC_ROOT / name / "run.json").is_file()
                  or not (DOC_ROOT / name / f"{name}.png").is_file())
    assert not bare, ("templates with no doc renders (run "
                      f"{RENDERER.relative_to(REPO)} --template <name>): "
                      + ", ".join(bare))


@pytest.mark.parametrize("name", sorted(_templates()))
def test_doc_figures_are_not_older_than_their_template(name: str) -> None:
    changed = _last_change(_package_dir(name))
    if not changed:
        pytest.skip(f"git knows no commit touching the {name} package")
    stale = []
    for figure in _generator()._figures(name):
        stamped = _stamped_commit(figure)
        if stamped is None:
            stale.append(f"{figure.name}: carries no commit stamp")
        elif not _is_ancestor(changed, stamped):
            stale.append(f"{figure.name}: stamped {stamped[:12]}, which does not "
                         f"contain the template's last change {changed[:12]}")
    assert not stale, (f"{name} doc figures predate the declaration they show; "
                       f"re-render with {RENDERER.relative_to(REPO)} --template "
                       f"{name}:\n  " + "\n  ".join(stale))


@pytest.mark.parametrize("name", sorted(_templates()))
def test_doc_figures_stay_inside_the_doc_size(name: str) -> None:
    over = []
    for figure in _generator()._figures(name):
        cap = (ANIMATION_MAX_BYTES if figure.suffix.lower() == ".gif"
               else COMPOSITE_MAX_BYTES)
        size = figure.stat().st_size
        if size > cap:
            over.append(f"{figure.name}: {size:,} bytes over the {cap:,} ceiling")
    assert not over, (f"{name} doc figures are heavier than a page carries:\n  "
                      + "\n  ".join(over))
