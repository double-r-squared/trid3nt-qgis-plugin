"""The MODEL-FACING description surface names only tools that exist.

A tool docstring, a spec ``docstring``/``caveats`` line and a retrieval corpus
query are all indexed as the model's routing signal. A dead engine name there
does what a dead name in the system prompt does one layer up: it advertises a
capability the product does not have, and it pulls a query toward a tool that
cannot answer it. ``tests/test_system_prompt.py`` pins the prompt; this pins
everything below it.

The retired-family list is the ONE lock. The prompt test's prefix-sharing lock
does not transfer here: a docstring is full of ordinary identifiers
(``model_setup_uri``, ``run_id``, ``list_of``) that share a first segment with a
registered tool without being one.

ASCII only.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import trid3nt_server.main as _main  # noqa: F401 -- registers the full tree
from trid3nt_server.tools import TOOL_REGISTRY

#: Engine families purged from the registry, lowercase. A name returns here one
#: line at a time as its engine lands again. Kept verbatim in step with
#: ``tests/test_system_prompt._RETIRED_ENGINE_NAMES`` -- the two surfaces are the
#: same class, so a name may not leave one list while it holds in the other.
RETIRED_ENGINE_NAMES = (
    "sfincs",
    "swmm",
    "geoclaw",
    "swan_wave",
    "schism",
    "modflow",
    "openquake",
    "landlab",
    "elmfire",
    "pelicun",
    "hydromt",
    "publish_layer",
    "hec-ras",
    "hecras",
)


def _offenders(text: str) -> list[str]:
    flat = text.lower()
    return [name for name in RETIRED_ENGINE_NAMES if name in flat]


def test_no_tool_docstring_names_a_retired_engine() -> None:
    """Every registered tool's docstring is the description the model routes on."""
    import inspect

    bad: dict[str, list[str]] = {}
    for name, entry in sorted(TOOL_REGISTRY.items()):
        hits = _offenders(inspect.getdoc(entry.fn) or "")
        if hits:
            bad[name] = hits
    assert not bad, f"retired engine names in tool docstrings: {bad}"


def _spec_yaml_paths() -> list[Path]:
    import trid3nt_server.tools as tools_pkg

    root = Path(tools_pkg.__file__).resolve().parent / "fetchers"
    return sorted(root.rglob("source.yaml"))


@pytest.mark.parametrize("path", _spec_yaml_paths(), ids=lambda p: p.parent.name)
def test_no_spec_description_names_a_retired_engine(path: Path) -> None:
    """``docstring`` is the declaration description; ``caveats`` ride the spec
    card, which is the model's only honesty view of a source."""
    spec = yaml.safe_load(path.read_text()) or {}
    surface = "\n".join(
        [str(spec.get("docstring") or "")] + [str(c) for c in (spec.get("caveats") or [])]
    )
    hits = _offenders(surface)
    assert not hits, f"retired engine names in {path.name} description: {hits}"


def _corpus_paths() -> list[Path]:
    import trid3nt_server.tools as tools_pkg

    root = Path(tools_pkg.__file__).resolve().parents[1]
    paths = sorted(root.rglob("corpus.yaml"))
    residual = root / "tools" / "tool_query_corpus.yaml"
    if residual.exists():
        paths.append(residual)
    return paths


@pytest.mark.parametrize("path", _corpus_paths(), ids=lambda p: p.parent.name)
def test_no_corpus_query_names_a_retired_engine(path: Path) -> None:
    """A corpus query is indexed AS the tool's document text, so a dead name in
    one is a retrieval magnet for a question nothing here can answer."""
    data = yaml.safe_load(path.read_text()) or {}
    queries = [q for v in data.values() for q in (v or []) if isinstance(q, str)]
    bad = {q: _offenders(q) for q in queries if _offenders(q)}
    assert not bad, f"retired engine names in {path} corpus queries: {bad}"
