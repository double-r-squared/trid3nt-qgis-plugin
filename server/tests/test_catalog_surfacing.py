"""Catalog-surfacing experiment mechanisms (experiments/catalog_surfacing/DESIGN.md).

Covers the two arm prerequisites + the identity gate:

- DEFAULT config (no arm flag): the 27 spec-served sources stay tier="general",
  ambient-declarable; registry == 190; fetch_from_catalog keeps its exact
  entry_id-only signature; search_data_catalog returns YAML catalog entries.
- Arm flag ON (own process): the 27 leave the default declarable pool (tier="catalog")
  but stay in the search index; Arm 1 exposes a fetch_from_catalog(source=...) branch
  + card projection; Arm 2 keeps them discoverable + gate-expandable.
- Card content fidelity + the fetch-via-spec validation locus.

The flag is read at import (each arm runs in its OWN process per the design), so the
full flag-on registration behaviour is asserted via subprocess; the flag-independent
projection + validation pieces are asserted in-process.

ASCII only.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

# --- Shared subprocess driver: run a snippet under a chosen arm env, parse the
#     trailing JSON line it prints. Mirrors the "each arm in its own process"
#     isolation guarantee (registration tier + the fetch_from_catalog signature
#     are import-time-frozen per process). ---

_DRIVER = r"""
import json, inspect, os
import trid3nt_server.main as m
m._import_tools_registry()
import trid3nt_server.agent.categories  # noqa: F401
import trid3nt_server.server as srv
from trid3nt_server.agent.tools import TOOL_REGISTRY
from trid3nt_server.agent.tools.fetchers._router import registration as reg
from trid3nt_server.agent.tools.search.search_tools import search_tools as st
from trid3nt_server.agent.tools.search.tool_retrieval import retrieve_ranked_tools

specs = sorted(reg.registered_spec_names())
dd = srv._default_declarable_registry()
st._reset_index_for_tests(); idx = st._get_index()
ranked = [n for n, _ in retrieve_ranked_tools("fuel moisture fire danger for this area", 25)]
ffc = TOOL_REGISTRY["fetch_from_catalog"].fn
out = {
    "arm": reg.catalog_arm(),
    "registry_size": len(TOOL_REGISTRY),
    "n_specs": len(specs),
    "gridmet_tier": getattr(TOOL_REGISTRY["fetch_gridmet"].metadata, "tier", "?"),
    "declarable_size": len(dd),
    "any_spec_in_declarable": any(s in dd for s in specs),
    "gridmet_in_index": "fetch_gridmet" in idx.tool_names,
    "gridmet_ranked_top25": "fetch_gridmet" in ranked,
    "ffc_params": list(inspect.signature(ffc).parameters.keys()),
    "ffc_doc_head": (ffc.__doc__ or "")[:48],
}
print("RESULT_JSON=" + json.dumps(out))
"""


def _run_arm(arm: str | None) -> dict:
    env = {k: v for k, v in _os_environ().items() if k != "TRID3NT_CATALOG_ARM"}
    if arm is not None:
        env["TRID3NT_CATALOG_ARM"] = arm
    proc = subprocess.run(
        [sys.executable, "-c", _DRIVER],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )
    assert proc.returncode == 0, f"driver failed (arm={arm}):\n{proc.stderr[-2000:]}"
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT_JSON=")]
    assert line, f"no RESULT_JSON in stdout:\n{proc.stdout[-1000:]}"
    return json.loads(line[-1][len("RESULT_JSON="):])


def _os_environ() -> dict:
    import os

    return dict(os.environ)


# --------------------------------------------------------------------------- #
# Identity gate: DEFAULT config unchanged.
# --------------------------------------------------------------------------- #


def test_default_config_identity():
    r = _run_arm(None)
    assert r["arm"] is None
    assert r["registry_size"] == 190
    assert r["n_specs"] == 64
    # They stay ambient (tier=general) and IN the declarable pool.
    assert r["gridmet_tier"] == "general"
    assert r["any_spec_in_declarable"] is True
    # fetch_from_catalog keeps its exact entry_id-only signature (no source param).
    assert r["ffc_params"] == ["entry_id", "params", "_extra_ignored"]
    assert r["ffc_doc_head"].startswith("Fetch bytes for a vetted catalog entry")


# --------------------------------------------------------------------------- #
# Arm 2 (discovery-expands-declaration) prerequisite.
# --------------------------------------------------------------------------- #


def test_arm2_specs_leave_pool_but_stay_indexed():
    r = _run_arm("2")
    assert r["arm"] == "2"
    assert r["registry_size"] == 190  # registry does NOT shrink; only the pool does
    assert r["gridmet_tier"] == "catalog"
    assert r["any_spec_in_declarable"] is False  # every spec leaves the ambient pool
    # -57, not -58: fetch_copernicus_dem is tier="internal" (wave-11 absorption into
    # fetch_dem), so it is ALREADY out of the ambient pool in the None baseline; the
    # arm moves the remaining general->catalog (incl. the 4 chained-resolution folds
    # ADR 0063, the 2 ADR 0064 folds openfema / storm_events, the 5 ADR 0065
    # station-sibling folds, the 2 ADR 0068 SLR-raster mapserver_export folds, the
    # 2 ADR 0070 Overpass folds roads / pois, the 5 ADR 0071 keyed/misc folds
    # mobi / climate_normals / ebird / iucn / usgs_groundwater_levels, and the ADR
    # 0073 envelope fold high_water_marks; + ADR 0074 river fold; + ADR 0075 3dep fold;
    # + ADR 0076 wfigs record fold; + ADR 0077 movebank keyed-CSV fold).
    assert r["declarable_size"] == _run_arm(None)["declarable_size"] - 63
    # Still searchable + rankable so a search hit can gate-expand it.
    assert r["gridmet_in_index"] is True
    assert r["gridmet_ranked_top25"] is True
    # Arm 2 keeps the provider FunctionDeclaration: fetch_from_catalog is unchanged.
    assert r["ffc_params"] == ["entry_id", "params", "_extra_ignored"]


# --------------------------------------------------------------------------- #
# Arm 1 (card-carried) prerequisite.
# --------------------------------------------------------------------------- #


def test_arm1_signature_and_pool():
    r = _run_arm("1")
    assert r["arm"] == "1"
    assert r["registry_size"] == 190
    assert r["gridmet_tier"] == "catalog"
    assert r["any_spec_in_declarable"] is False
    assert r["gridmet_in_index"] is True
    # fetch_from_catalog exposes the source branch ONLY under Arm 1.
    assert r["ffc_params"] == ["entry_id", "params", "source", "_extra_ignored"]


# --------------------------------------------------------------------------- #
# In-process: card projection + fetch-via-spec validation locus.
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def _registry_loaded():
    import trid3nt_server.main as m

    m._import_tools_registry()
    import trid3nt_server.agent.categories  # noqa: F401


def test_spec_card_content_fidelity(_registry_loaded):
    from trid3nt_server.agent.tools.fetchers._router import registration as reg

    spec = reg._SPEC_REGISTRY["fetch_gridmet"]
    card = reg.spec_card(spec, relevance_score=1.23)
    assert card["name"] == "fetch_gridmet"
    # FULL docstring (not truncated at the provider ~1000-char limit).
    assert card["docstring"] == (spec.docstring or reg._synthesize_doc(spec))
    assert len(card["docstring"]) == len(spec.docstring or reg._synthesize_doc(spec))
    # Typed param schema per spec param.
    for pname, pspec in spec.params.items():
        assert pname in card["params"]
        assert card["params"][pname]["type"] == pspec.type
        assert card["params"][pname]["required"] == bool(pspec.required)
    # Honesty context + score present.
    for key in ("gates", "caveats", "fallback"):
        assert key in card
    assert card["relevance_score"] == pytest.approx(1.23)


def test_search_spec_cards_ranks_expected_source(_registry_loaded):
    from trid3nt_server.agent.tools.search.search_tools import search_tools as st
    from trid3nt_server.agent.tools.fetchers._router import registration as reg

    st._reset_index_for_tests()
    st._get_index()  # warm
    cards = reg.search_spec_cards("fuel moisture fire danger weather", k=10)
    names = [c["name"] for c in cards]
    assert names, "no cards ranked (cold index?)"
    assert "fetch_gridmet" in names
    # Only spec-served sources are projected as cards.
    assert set(names) <= reg.registered_spec_names()


def test_fetch_via_spec_bad_args_raise_router_input_error(_registry_loaded):
    from trid3nt_server.agent.tools.fetchers._router.errors import RouterInputError
    from trid3nt_server.agent.tools.search.fetch_from_catalog.fetch_from_catalog import (
        _fetch_from_catalog_via_spec,
    )

    # Missing the required bbox (no network reached: validate_params raises first).
    with pytest.raises(RouterInputError):
        _fetch_from_catalog_via_spec("fetch_gridmet", {"variable": "not_a_real_var"})


def test_fetch_via_spec_unknown_source_raises(_registry_loaded):
    from trid3nt_server.agent.tools.search.catalog_common import CatalogNotFoundError
    from trid3nt_server.agent.tools.search.fetch_from_catalog.fetch_from_catalog import (
        _fetch_from_catalog_via_spec,
    )

    with pytest.raises(CatalogNotFoundError):
        _fetch_from_catalog_via_spec("fetch_not_a_source", {"bbox": [-83, 27, -82, 28]})


# --------------------------------------------------------------------------- #
# Arm 3 (stratified-pool composed declaration) prerequisite + mechanisms.
# --------------------------------------------------------------------------- #


def test_arm3_specs_leave_pool_and_source_param():
    """Arm 3 = the same pool exclusion as arms 1/2 (tier=catalog, -17 ambient,
    still indexed) PLUS the fetch_from_catalog source-passthrough branch (the
    composed fetcher's real dispatch path)."""
    r = _run_arm("3")
    assert r["arm"] == "3"
    assert r["registry_size"] == 190  # registry does NOT shrink; only the pool does
    assert r["gridmet_tier"] == "catalog"
    assert r["any_spec_in_declarable"] is False  # every spec leaves the ambient pool
    # -63, not -64: fetch_copernicus_dem is tier="internal" (already out of the pool).
    assert r["declarable_size"] == _run_arm(None)["declarable_size"] - 63
    assert r["gridmet_in_index"] is True
    # fetch_from_catalog exposes the source branch under Arm 3 (like Arm 1).
    assert r["ffc_params"] == ["entry_id", "params", "source", "_extra_ignored"]


@pytest.fixture()
def _stratum(_registry_loaded):
    from trid3nt_server.agent.tools.fetchers._router import stratified as strat
    from trid3nt_server.agent.tools.search.search_tools import search_tools as st

    st._reset_index_for_tests()
    strat.reset_source_stratum_index_for_tests()
    return strat


def test_stratum_index_is_source_scoped(_stratum):
    """Stratum split: the pool index ranks over the spec-served sources, MINUS any
    tier="internal" seam (fetch_copernicus_dem is absorbed into fetch_dem and never
    faces the model, so the search index -- and thus the stratum -- excludes it)."""
    from trid3nt_server.agent.tools.fetchers._router import registration as reg
    from trid3nt_server.agent.tools import TOOL_REGISTRY

    idx = _stratum.source_stratum_index()
    model_facing = {
        n for n in reg.registered_spec_names()
        if getattr(TOOL_REGISTRY[n].metadata, "tier", "general") != "internal"
    }
    assert set(idx.tool_names) == model_facing
    assert len(idx.tool_names) == 63  # 64 specs minus the internal copernicus seam


def test_stratum_activates_on_data_ask_enum_rank_order(_stratum):
    """A data ask activates; the enum is the matched sources IN RANK ORDER (k<=5),
    the target leads, and full cards accompany it."""
    plan = _stratum.stratum_declaration_plan("gridMET daily weather fuel moisture burning index")
    assert plan["activated"] is True
    assert plan["sources"][0] == "fetch_gridmet"
    assert 1 <= len(plan["sources"]) <= _stratum.SOURCE_ENUM_K
    # cards parallel the enum, in the same order, carrying the FULL docstring.
    assert [c["name"] for c in plan["cards"]] == plan["sources"]
    from trid3nt_server.agent.tools.fetchers._router import registration as reg

    spec = reg._SPEC_REGISTRY["fetch_gridmet"]
    top_card = plan["cards"][0]
    assert top_card["docstring"] == (spec.docstring or reg._synthesize_doc(spec))


def test_stratum_declines_clearly_non_data_ask(_stratum):
    """Trigger is a threshold, not always-on: an ask with no pool relevance does
    NOT declare the composed fetcher (core surface only)."""
    plan = _stratum.stratum_declaration_plan("please greet the user warmly")
    assert plan["activated"] is False
    assert plan["sources"] == []
    assert _stratum.compose_fetcher_declaration(plan) is None
    assert _stratum.render_cards_context(plan) == ""


def test_composed_declaration_enum_matches_plan(_stratum):
    """The composed generic fetcher carries the source enum in rank order + a
    free-form params object; no per-source virtual tool is declared."""
    plan = _stratum.stratum_declaration_plan("census demographics median household income")
    assert plan["activated"] is True
    decl = _stratum.compose_fetcher_declaration(plan)
    assert decl.name == _stratum.COMPOSED_FETCHER_NAME == "fetch_from_catalog"
    props = decl.parameters.properties
    assert list(props["source"].enum) == plan["sources"]  # rank order preserved
    assert set(decl.parameters.required) == {"source", "params"}


def test_render_cards_context_carries_full_detail(_stratum):
    plan = _stratum.stratum_declaration_plan("gridMET daily weather fuel moisture")
    ctx = _stratum.render_cards_context(plan)
    assert "fetch_gridmet" in ctx
    assert "params:" in ctx
    # the block names every enum source (the model's per-source view).
    for name in plan["sources"]:
        assert name in ctx


def test_default_declarable_excludes_catalog_tier(_registry_loaded):
    """The pool filter drops tier in {template, catalog} (a search hit re-adds
    the specific expanded name); a gate-expander result naming a catalog source
    resolves to a real, registered, pool-excluded tool -> declarable-on-expansion."""
    import trid3nt_server.server as srv

    fake_search_result = {
        "results": [{"tool_name": "fetch_gridmet"}, {"tool_name": "fetch_census_acs"}]
    }
    names = srv._tool_names_from_search_result(fake_search_result)
    assert names == ["fetch_gridmet", "fetch_census_acs"]
    from trid3nt_server.agent.tools import TOOL_REGISTRY

    for n in names:
        assert n in TOOL_REGISTRY  # real + registered -> expander can declare it
