"""Door dissolution (ADR 0094): the engine-door concierge tools are DELETED and
each engine template stands alone in the retrieval pool.

Two guarantees are pinned here:

1. CALLABILITY -- every engine template is registered (tier=template,
   source_class=workflow_dispatch) and directly callable; NO tier=door tool
   survives; the 10 deleted door names are gone with no alias.
2. RETRIEVAL -- with templates walked into the index, EACH engine template is
   surfaced in the model-free ``retrieve_visible_tools(query, None, 8)`` top-8
   by at least one of its natural-language corpus queries (the retrieval-corpus-
   first rule -- the doors can die only because discovery works without them).

ASCII only.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import trid3nt_server.main as _main
from trid3nt_server.tools.search.search_tools import search_tools as dd
from trid3nt_server.tools.search.tool_retrieval import retrieve_visible_tools

#: The engine templates the tree registers. Pinned as a SET, not derived: a
#: template that stops registering is a capability that silently left, and the
#: retrieval matrix below is only a guarantee if the roster it walks is fixed.
EXPECTED_TEMPLATES = {
    "telemac_river_dye",
    "telemac_do_sag",
    "telemac_rain_on_grid",
    "telemac3d_stratified_flow",
    "artemis_harbor_agitation",
}

#: Templates that are DECLARED but off the model surface, name -> the module
#: attribute that carries the declaration. Pinned for the same reason the
#: registered set is: parking is a stated condition with a reason, and a template
#: that drifts INTO or OUT OF it silently is the drift this file exists to catch.
PARKED_TEMPLATES = {
    "tomawac_wave_field":
        "trid3nt_server.workflows.telemac.wave_field.wave_field",
    "coastal_tidal_surge":
        "trid3nt_server.workflows.telemac.coastal_tidal_surge.coastal_tidal_surge",
}

# The 10 deleted engine-door concierge tools.
DELETED_DOORS = {
    "run_sfincs",
    "run_swmm",
    "run_modflow",
    "run_telemac",
    "run_swan",
    "run_elmfire",
    "run_geoclaw",
    "run_landlab",
    "run_openquake",
    "run_pelicun",
}


def _full_registry():
    _main._import_tools_registry()
    from trid3nt_server.tools import TOOL_REGISTRY

    return TOOL_REGISTRY


# ---------------------------------------------------------------------------
# (1) Callability without doors.
# ---------------------------------------------------------------------------
def test_no_engine_door_survives():
    """No tool carries tier=door, and none of the 10 door names is registered."""
    reg = _full_registry()
    doors = [n for n, e in reg.items() if getattr(e.metadata, "tier", "general") == "door"]
    assert doors == [], f"engine doors must be dissolved; still registered: {doors}"
    still = DELETED_DOORS & set(reg)
    assert still == set(), f"deleted door names must be gone (no alias): {sorted(still)}"


def test_all_templates_registered_and_callable():
    """Every engine template is registered tier=template, workflow_dispatch, and
    directly callable (no door, no gate expansion)."""
    reg = _full_registry()
    registered_templates = {
        n for n, e in reg.items() if getattr(e.metadata, "tier", "general") == "template"
    }
    assert registered_templates == EXPECTED_TEMPLATES, (
        "registered tier=template set drifted from the expected set: "
        f"missing={sorted(EXPECTED_TEMPLATES - registered_templates)} "
        f"unexpected={sorted(registered_templates - EXPECTED_TEMPLATES)}"
    )
    for name in EXPECTED_TEMPLATES:
        entry = reg[name]
        assert callable(entry.fn), f"{name} is not callable"
        assert getattr(entry.metadata, "engine", None), f"{name} missing engine tag"


def test_parked_templates_are_declared_off_the_surface_with_a_reason():
    """A parked template is READ from its declaration, never inferred from what a
    session happened to import: the module is in the tree's import list, the
    declaration validates, ``parked`` names the reason, and the tool is absent."""
    import importlib

    reg = _full_registry()
    for name, module_path in PARKED_TEMPLATES.items():
        fn = getattr(importlib.import_module(module_path), name)
        assert fn.parked, f"{name} is pinned parked but declares no reason"
        assert fn.workflow.plan.name == name
        assert name not in reg, f"{name} is pinned parked but registers a tool"


# ---------------------------------------------------------------------------
# (2) Retrieval matrix -- every template surfaces top-8 for >=1 natural query.
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def warm_index():
    dd._get_index()  # hashed backend, no network model load
    yield


def _template_corpus() -> dict[str, list[str]]:
    """Load each template's co-located workflows/**/corpus.yaml queries."""
    import trid3nt_server.tools as t

    workflows = Path(t.__file__).resolve().parents[1] / "workflows"
    out: dict[str, list[str]] = {}
    for cp in workflows.rglob("corpus.yaml"):
        data = yaml.safe_load(cp.read_text()) or {}
        for k, v in data.items():
            out.setdefault(k, []).extend(q for q in (v or []) if isinstance(q, str))
    return out


def test_every_template_surfaces_in_top8(warm_index):
    """Model-free retrieve_visible_tools(query, None, 8): for EACH engine
    template, at least one of its natural corpus queries surfaces it in the
    top-8. This is the discovery guarantee that lets the doors die."""
    corpus = _template_corpus()
    misses: dict[str, list[str]] = {}
    for tmpl in sorted(EXPECTED_TEMPLATES):
        queries = corpus.get(tmpl, [])
        assert queries, f"{tmpl} has NO corpus queries (retrieval-corpus-first rule)"
        surfaced = any(tmpl in retrieve_visible_tools(q, None, 8) for q in queries)
        if not surfaced:
            misses[tmpl] = queries
    assert not misses, (
        "these templates surface in NO top-8 for any corpus query "
        f"(discovery is broken -- doors cannot die): {sorted(misses)}"
    )
