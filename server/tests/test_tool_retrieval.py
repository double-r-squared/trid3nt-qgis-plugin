"""Unit tests for ``retrieve_visible_tools`` (tool-retrieval STEP 0 + STEP 1).

Asserts the kickoff's invariants: CORE-FLOOR (HOT_SET always a subset),
NEVER-HIDE-MID-TASK (result always contains the Case's accumulated AllowedToolSet),
DETERMINISTIC, k-clamp, and FAIL-OPEN (error / cold index / empty ranking -> full
registry; empty query -> floor only). Plus a recall fixture over covered tools.

ASCII only.
"""

from __future__ import annotations

import pytest

import trid3nt_server.tools.discovery.search_tools as dd
from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.discovery import tool_retrieval as trmod
from trid3nt_server.tools.discovery.tool_retrieval import (
    DEFAULT_K,
    MAX_K,
    retrieve_visible_tools,
)
from trid3nt_server.categories import (
    HOT_SET_TOOLS,
    AllowedToolSet,
    tools_for_category,
)


@pytest.fixture(scope="module")
def warm_index():
    """Warm the discover index once (cheap: hashed backend, no model load)."""
    dd._get_index()
    yield


def _allowed(opened=None, dispatched=None, explicit=None) -> AllowedToolSet:
    a = AllowedToolSet()
    for c in opened or ():
        a.open_category(c)
    for t in dispatched or ():
        a.record_dispatch(t)
    if explicit:
        a.add_tools(explicit)
    return a


# ---------------------------------------------------------------------------
# STEP 0 -- the extended HOT_SET floor.
# ---------------------------------------------------------------------------
def test_step0_hot_set_floor_extended():
    for name in (
        "publish_layer",
        "compute_zonal_statistics",
        "generate_histogram",
        "generate_time_series",
        # DuckDB spatial-query fold (Phase B): spatial_query holds the
        # layer-analysis floor slot summarize_layer_statistics held.
        "spatial_query",
    ):
        assert name in HOT_SET_TOOLS, f"{name} must be in the STEP-0 HOT_SET floor"


# ---------------------------------------------------------------------------
# CORE-FLOOR: HOT_SET_TOOLS is ALWAYS a subset of the result.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("query", ["model the flood", "", "show me lightning", "asdfqwer", "   "])
@pytest.mark.parametrize("allowed", [None, "fresh", "opened"])
def test_core_floor_always_subset(warm_index, query, allowed):
    a = None if allowed is None else (
        AllowedToolSet() if allowed == "fresh" else _allowed(opened={"hydrology"})
    )
    res = retrieve_visible_tools(query, a, DEFAULT_K)
    assert HOT_SET_TOOLS <= res


# ---------------------------------------------------------------------------
# NEVER-HIDE-MID-TASK: the result always contains the Case's accumulated set.
# ---------------------------------------------------------------------------
def test_never_hide_mid_task(warm_index):
    a = _allowed(
        opened={"hydrology"},
        dispatched={"run_swmm_urban_flood"},
        explicit={"compute_contours"},
    )
    # a query about something UNRELATED to the accumulated tools.
    res = retrieve_visible_tools("show me the lightning over the storm", a, DEFAULT_K)
    assert set(a.as_frozenset()) <= res
    assert "run_swmm_urban_flood" in res  # dispatched stays
    assert "compute_contours" in res      # explicit stays
    assert set(tools_for_category("hydrology")) <= res  # opened-category tools stay


def test_monotonic_growth_only_adds(warm_index):
    a = AllowedToolSet()
    r1 = retrieve_visible_tools("fetch the elevation DEM", a, DEFAULT_K)
    # the Case grows: a tool dispatched + a category opened.
    a.record_dispatch("fetch_dem")
    a.open_category("hydrology")
    r2 = retrieve_visible_tools("fetch the elevation DEM", a, DEFAULT_K)
    # everything in the (grown) allowed set is visible; nothing the Case accrued left.
    assert set(a.as_frozenset()) <= r2
    assert set(tools_for_category("hydrology")) <= r2
    assert "fetch_dem" in r1 and "fetch_dem" in r2


def test_never_hide_survives_invalid_opened_category():
    """Registry skew: a Case holding a now-unknown category id open must NOT drop
    its OTHER accrued tools (the per-category-guarded snapshot, 2026-06-23)."""
    a = AllowedToolSet()
    a.open_category("hydrology")          # valid
    a.open_category("no_such_category")   # invalid (e.g. removed/renamed across a deploy)
    a.record_dispatch("run_swmm_urban_flood")
    a.add_tools({"compute_contours"})
    for query in ("", "fetch radar reflectivity nexrad"):
        res = retrieve_visible_tools(query, a, DEFAULT_K)
        assert "run_swmm_urban_flood" in res, query  # dispatched stays
        assert "compute_contours" in res, query      # explicit stays
        assert set(tools_for_category("hydrology")) <= res, query  # valid cat stays
        assert HOT_SET_TOOLS <= res


# ---------------------------------------------------------------------------
# DETERMINISTIC.
# ---------------------------------------------------------------------------
def test_deterministic(warm_index):
    a = _allowed(dispatched={"fetch_dem"})
    r1 = retrieve_visible_tools("show me the lightning over the storm", a, DEFAULT_K)
    r2 = retrieve_visible_tools("show me the lightning over the storm", a, DEFAULT_K)
    assert r1 == r2


# ---------------------------------------------------------------------------
# k clamp [1, MAX_K].
# ---------------------------------------------------------------------------
def test_k_clamps_high(warm_index):
    res = retrieve_visible_tools("fetch radar reflectivity precipitation", None, 1000)
    discovered = res - set(HOT_SET_TOOLS)
    assert len(discovered) <= MAX_K


def test_k_clamps_low_and_bad(warm_index):
    assert HOT_SET_TOOLS <= retrieve_visible_tools("fetch radar", None, 0)
    assert HOT_SET_TOOLS <= retrieve_visible_tools("fetch radar", None, -5)
    assert HOT_SET_TOOLS <= retrieve_visible_tools("fetch radar", None, "garbage")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# FAIL-OPEN: error / cold index / empty ranking -> FULL registry.
# ---------------------------------------------------------------------------
# the 4 tools that register ONLY via the full startup path -- the fail-open
# full-registry snapshot must include them even in a cold process.
_STARTUP_ONLY = {
    "search_data_catalog",
    "fetch_from_catalog",
    "list_qgis_algorithms",
    "describe_qgis_algorithm",
}


def _assert_full_failopen(res):
    full = _full_registry_names()
    assert full <= res, f"fail-open dropped: {sorted(full - res)}"
    assert _STARTUP_ONLY <= res, "fail-open omitted the startup-only tools"


def test_fail_open_on_discovery_error(warm_index, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("synthetic discovery fault")

    monkeypatch.setattr(trmod, "_discover_topk", _boom)
    _assert_full_failopen(retrieve_visible_tools("show me the lightning", None, DEFAULT_K))


def test_fail_open_on_cold_index(monkeypatch):
    monkeypatch.setattr(dd, "_INDEX", None)  # simulate not-yet-warmed
    _assert_full_failopen(retrieve_visible_tools("show me the lightning", None, DEFAULT_K))


def test_fail_open_on_empty_ranking(warm_index, monkeypatch):
    monkeypatch.setattr(trmod, "_discover_topk", lambda *a, **k: set())
    _assert_full_failopen(retrieve_visible_tools("zzqqxx-nomatch", None, DEFAULT_K))


def test_fail_open_on_snapshot_error_returns_full_registry(warm_index, monkeypatch):
    """If allowed_set.as_frozenset() raises, fail-open to the FULL registry (never
    HOT_SET-only) so a once-visible accrued tool is not silently hidden."""

    class _Boom:
        def as_frozenset(self):
            raise RuntimeError("synthetic snapshot fault")

    _assert_full_failopen(retrieve_visible_tools("model the flood", _Boom(), DEFAULT_K))


def test_cold_index_never_builds_on_hot_path(monkeypatch):
    """The hot path must NOT trigger a cold index build (which blocks on a model
    load); a cold _INDEX must fail-open without calling _get_index/_build_index."""
    monkeypatch.setattr(dd, "_INDEX", None)
    called = {"build": 0}
    monkeypatch.setattr(dd, "_get_index", lambda *a, **k: called.__setitem__("build", called["build"] + 1))
    monkeypatch.setattr(dd, "_build_index", lambda *a, **k: called.__setitem__("build", called["build"] + 1))
    res = retrieve_visible_tools("show me the lightning", None, DEFAULT_K)
    assert called["build"] == 0  # never built on the hot path
    _assert_full_failopen(res)


# ---------------------------------------------------------------------------
# Empty query -> floor only (does NOT dump the full catalog).
# ---------------------------------------------------------------------------
def test_empty_query_returns_floor_only(warm_index):
    assert retrieve_visible_tools("   ", None, DEFAULT_K) == set(HOT_SET_TOOLS)
    a = _allowed(dispatched={"fetch_dem"})
    res = retrieve_visible_tools("", a, DEFAULT_K)
    assert res == set(a.as_frozenset()) | set(HOT_SET_TOOLS)
    assert set(TOOL_REGISTRY) - res  # full registry NOT dumped


# ---------------------------------------------------------------------------
# RECALL on covered fixtures (the result must surface the expected tool top-k).
# ---------------------------------------------------------------------------
_RECALL_FIXTURE = [
    ("show me the lightning over this storm from GOES", "fetch_glm_lightning"),
    ("detect the active fire hot pixels from GOES", "fetch_goes_active_fire"),
    ("get the elevation DEM for this area", "fetch_dem"),
    ("geocode this city to a bounding box", "geocode_location"),
    ("fetch NEXRAD radar reflectivity", "fetch_nexrad_reflectivity"),
    # newly-backfilled (STEP 7) tools -- prove the backfill lifts recall.
    ("how deep will the water get from this hurricane flood", "run_model_flood_scenario"),
    ("simulate urban street flooding from heavy rain in this city", "run_swmm_urban_flood"),
    ("fetch high resolution aerial imagery for this area", "fetch_naip"),
    ("model the groundwater contamination plume from this chemical spill", "run_model_groundwater_contamination_scenario"),
    ("run a probabilistic seismic hazard calculation for this region", "run_seismic_hazard_psha"),
    ("draw the topographic contour lines from the elevation", "compute_contours"),
]


@pytest.mark.parametrize("query,want", _RECALL_FIXTURE)
def test_recall_surfaces_expected_tool(warm_index, query, want):
    res = retrieve_visible_tools(query, None, DEFAULT_K)
    assert want in res, f"recall miss: {want!r} not surfaced for {query!r}"


# ---------------------------------------------------------------------------
# STEP 7 -- corpus coverage: every registered tool has routing queries; no dead keys.
# ---------------------------------------------------------------------------
def _load_corpus():
    import pathlib

    import yaml

    # Resolve through the module's own seam so the test never hardcodes the
    # package depth (tools/discovery/ post-reorg).
    path = pathlib.Path(dd._default_corpus_path())
    return yaml.safe_load(path.read_text())


def _full_registry_names() -> set[str]:
    """The FULL registry -- includes the workflow/solver/catalog/qgis tools that
    register only via the startup import path (NOT the plain `from . import
    TOOL_REGISTRY` snapshot), so the coverage check is deterministic regardless
    of test order."""
    import trid3nt_server.main as _m

    _m._import_tools_registry()
    from trid3nt_server.tools import TOOL_REGISTRY as _full

    return set(_full)


def test_every_registered_tool_has_corpus_queries():
    corpus = _load_corpus()
    missing = sorted(_full_registry_names() - set(corpus))
    assert not missing, (
        "these registered tools have NO tool_query_corpus.yaml entry -- add 5-8 "
        f"routing queries each so retrieve_visible_tools can recall them: {missing}"
    )


def test_no_dead_corpus_keys():
    corpus = _load_corpus()
    dead = sorted(set(corpus) - _full_registry_names())
    assert not dead, (
        f"tool_query_corpus.yaml has keys for non-registered tools (prune them): {dead}"
    )


def test_every_corpus_entry_meets_query_floor():
    corpus = _load_corpus()
    thin = {t: len(q) for t, q in corpus.items() if len(q) < 5}
    assert not thin, f"corpus entries below the 5-query recall floor: {thin}"
