"""Unit tests for ``retrieve_visible_tools`` (the built-in surfacing path).

Asserts the invariants: CORE-FLOOR (``CORE_FLOOR`` always a subset),
NEVER-HIDE-MID-TASK (result always contains the Case's accrued visible set),
DETERMINISTIC, k-clamp, and FAIL-OPEN (error / cold index / empty ranking -> full
registry; empty query -> floor only). Plus a recall fixture over covered tools.

ASCII only.
"""

from __future__ import annotations

import pytest

import trid3nt_server.tools.search.search_tools.search_tools as dd
from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.search import tool_retrieval as trmod
from trid3nt_server.tools.search.tool_retrieval import (
    CORE_FLOOR,
    DEFAULT_K,
    MAX_K,
    retrieve_visible_tools,
)


@pytest.fixture(scope="module")
def warm_index():
    """Warm the discover index once (cheap: hashed backend, no model load)."""
    dd._get_index()
    yield


# ---------------------------------------------------------------------------
# The core floor covers the render + layer-analysis slots.
# ---------------------------------------------------------------------------
def test_core_floor_covers_render_and_analysis_slots():
    for name in ("generate_chart", "spatial_query"):
        assert name in CORE_FLOOR, f"{name} must be in CORE_FLOOR"
    # publish_layer used to hold the render slot. ADR 0313 deleted the tool:
    # emission is automatic, so there is no "display this" intent to keep
    # always-visible, and a floor entry naming a tool that does not exist would
    # be a dead name the retrieval pool hands the model every turn.
    assert "publish_layer" not in CORE_FLOOR


# ---------------------------------------------------------------------------
# CORE-FLOOR: CORE_FLOOR is ALWAYS a subset of the result.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("query", ["model the flood", "", "show me lightning", "asdfqwer", "   "])
@pytest.mark.parametrize("accrued", [None, "fresh", "seeded"])
def test_core_floor_always_subset(warm_index, query, accrued):
    a = None if accrued is None else (
        set() if accrued == "fresh" else {"fetch_usgs_nwis_gauges"}
    )
    res = retrieve_visible_tools(query, a, DEFAULT_K)
    assert CORE_FLOOR <= res


# ---------------------------------------------------------------------------
# NEVER-HIDE-MID-TASK: the result always contains the Case's accrued set.
# ---------------------------------------------------------------------------
def test_never_hide_mid_task(warm_index):
    accrued = {"coastal_tidal_surge", "compute_contours", "fetch_usgs_nwis_gauges"}
    # a query about something UNRELATED to the accrued tools.
    res = retrieve_visible_tools("show me the lightning over the storm", accrued, DEFAULT_K)
    assert accrued <= res
    assert "coastal_tidal_surge" in res  # dispatched stays
    assert "compute_contours" in res  # explicit stays


def test_monotonic_growth_only_adds(warm_index):
    accrued: set[str] = set()
    r1 = retrieve_visible_tools("fetch the elevation DEM", accrued, DEFAULT_K)
    # the Case grows: a tool dispatched this session.
    accrued.add("fetch_dem")
    accrued.add("fetch_usgs_nwis_gauges")
    r2 = retrieve_visible_tools("fetch the elevation DEM", accrued, DEFAULT_K)
    # everything the Case accrued is visible; nothing accrued left the set.
    assert accrued <= r2
    assert "fetch_dem" in r1 and "fetch_dem" in r2


# ---------------------------------------------------------------------------
# DETERMINISTIC.
# ---------------------------------------------------------------------------
def test_deterministic(warm_index):
    accrued = {"fetch_dem"}
    r1 = retrieve_visible_tools("show me the lightning over the storm", accrued, DEFAULT_K)
    r2 = retrieve_visible_tools("show me the lightning over the storm", accrued, DEFAULT_K)
    assert r1 == r2


# ---------------------------------------------------------------------------
# k clamp [1, MAX_K].
# ---------------------------------------------------------------------------
def test_k_clamps_high(warm_index):
    res = retrieve_visible_tools("fetch radar reflectivity precipitation", None, 1000)
    discovered = res - set(CORE_FLOOR)
    assert len(discovered) <= MAX_K


def test_k_clamps_low_and_bad(warm_index):
    assert CORE_FLOOR <= retrieve_visible_tools("fetch radar", None, 0)
    assert CORE_FLOOR <= retrieve_visible_tools("fetch radar", None, -5)
    assert CORE_FLOOR <= retrieve_visible_tools("fetch radar", None, "garbage")  # type: ignore[arg-type]


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


def _pool_hidden_names() -> set[str]:
    """Registered pool-HIDDEN names: tier=internal only.

    Engine templates (tier=template) are ordinary retrieval-pool members -- they
    belong in retrieve_visible_tools, the fail-open dump, and the corpus. Only
    tier=internal (an absorbed in-process seam, fetch_copernicus_dem) stays
    pool-hidden -- never model-facing, carries no corpus, and must NOT appear in
    the fail-open dump."""
    import trid3nt_server.main as _m

    _m._import_tools_registry()
    from trid3nt_server.tools import TOOL_REGISTRY as _full

    return {
        n for n, e in _full.items()
        if getattr(e.metadata, "tier", "general") in ("internal",)
    }


def _assert_full_failopen(res):
    # The fail-open floor filters ONLY tier=internal (a cold index must not leak
    # the internal seam); engine templates are pool members and MUST appear.
    # Expect the full registry MINUS the internal seam.
    full = _full_registry_names() - _pool_hidden_names()
    assert full <= res, f"fail-open dropped: {sorted(full - res)}"
    assert _STARTUP_ONLY <= res, "fail-open omitted the startup-only tools"
    assert not (_pool_hidden_names() & res), (
        f"fail-open leaked pool-hidden internal tools: {sorted(_pool_hidden_names() & res)}"
    )


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
    assert retrieve_visible_tools("   ", None, DEFAULT_K) == set(CORE_FLOOR)
    accrued = {"fetch_dem"}
    res = retrieve_visible_tools("", accrued, DEFAULT_K)
    assert res == accrued | set(CORE_FLOOR)
    assert set(TOOL_REGISTRY) - res  # full registry NOT dumped


# ---------------------------------------------------------------------------
# RECALL on covered fixtures (the result must surface the expected tool top-k).
# ---------------------------------------------------------------------------
_RECALL_FIXTURE = [
    ("show me the lightning over this storm from GOES", "fetch_glm_lightning"),
    ("detect the active fire hot pixels from GOES", "fetch_goes_active_fire"),
    ("get the elevation DEM for this area", "fetch_dem"),
    ("geocode this city to a bounding box", "geocode_location"),
    ("show NEXRAD radar reflectivity on the map", "show_nexrad_radar"),
    ("how far does the storm surge flood inland along this coast", "coastal_tidal_surge"),
    ("fetch high resolution aerial imagery for this area", "fetch_naip"),
    ("how much does incoming swell amplify inside this harbour basin",
     "artemis_harbor_agitation"),
    ("how big do the waves get on this lake in a westerly gale", "tomawac_wave_field"),
    ("draw the topographic contour lines from the elevation", "compute_contours"),
]


@pytest.mark.parametrize("query,want", _RECALL_FIXTURE)
def test_recall_surfaces_expected_tool(warm_index, query, want):
    res = retrieve_visible_tools(query, None, DEFAULT_K)
    assert want in res, f"recall miss: {want!r} not surfaced for {query!r}"


# ---------------------------------------------------------------------------
# Corpus coverage: every registered tool has routing queries; no dead keys.
# ---------------------------------------------------------------------------
def _load_corpus():
    # Compose through the module's own loader (per-tool corpus.yaml tree +
    # residual) so the test never hardcodes the package depth or the split.
    return dd._load_corpus()


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
    # Engine templates ARE required to have corpus queries -- their co-located
    # workflows/<engine>/<template>/corpus.yaml is walked into the composed
    # corpus. Only tier=internal (never model-facing) carries no corpus.
    missing = sorted(_full_registry_names() - _pool_hidden_names() - set(corpus))
    assert not missing, (
        "these registered tools have NO tool_query_corpus.yaml entry -- add 5-8 "
        f"routing queries each so retrieve_visible_tools can recall them: {missing}"
    )


def test_no_dead_corpus_keys():
    """A corpus key for a tool nothing registers is dead weight in the index --
    EXCEPT a DECLARED PARKED template, whose corpus is part of the declaration and
    comes back with it in one keyword. The retrieval visible set is derived from
    the registry, so a parked template's phrasings never reach the model."""
    from tests.test_door_dissolution import PARKED_TEMPLATES

    corpus = _load_corpus()
    dead = sorted(set(corpus) - _full_registry_names() - set(PARKED_TEMPLATES))
    assert not dead, (
        f"tool_query_corpus.yaml has keys for non-registered tools (prune them): {dead}"
    )


def test_every_corpus_entry_meets_query_floor():
    corpus = _load_corpus()
    thin = {t: len(q) for t, q in corpus.items() if len(q) < 5}
    assert not thin, f"corpus entries below the 5-query recall floor: {thin}"
