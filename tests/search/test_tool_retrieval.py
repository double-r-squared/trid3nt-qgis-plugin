"""Unit tests for ``retrieve_visible_tools``, the built-in surfacing path.

The invariants: ``CORE_FLOOR`` is always a subset, the Case's accrued visible set
is never hidden mid-task, the result is deterministic, k is clamped, and an
error, a cold index or an empty ranking FAILS OPEN to the full registry while an
empty query returns the floor alone. Plus a recall fixture. ASCII only."""

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


def test_core_floor_covers_render_and_analysis_slots():
    for name in ("generate_chart", "spatial_query"):
        assert name in CORE_FLOOR, f"{name} must be in CORE_FLOOR"
    # There is no publish_layer in the floor and no publish_layer tool:
    # emission is automatic, so there is no "display this" intent to keep
    # always-visible, and a floor entry naming a tool that does not exist would
    # be a dead name the retrieval pool hands the model every turn.
    assert "publish_layer" not in CORE_FLOOR


@pytest.mark.parametrize("query", ["model the flood", "", "show me lightning", "asdfqwer", "   "])
@pytest.mark.parametrize("accrued", [None, "fresh", "seeded"])
def test_core_floor_always_subset(warm_index, query, accrued):
    a = None if accrued is None else (
        set() if accrued == "fresh" else {"fetch_usgs_nwis_gauges"}
    )
    res = retrieve_visible_tools(query, a, DEFAULT_K)
    assert CORE_FLOOR <= res


def test_never_hide_mid_task(warm_index):
    accrued = {"telemac_river_dye", "compute_contours", "fetch_usgs_nwis_gauges"}
    # a query about something UNRELATED to the accrued tools.
    res = retrieve_visible_tools("show me the lightning over the storm", accrued, DEFAULT_K)
    assert accrued <= res
    assert "telemac_river_dye" in res  # dispatched stays
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


def test_deterministic(warm_index):
    accrued = {"fetch_dem"}
    r1 = retrieve_visible_tools("show me the lightning over the storm", accrued, DEFAULT_K)
    r2 = retrieve_visible_tools("show me the lightning over the storm", accrued, DEFAULT_K)
    assert r1 == r2


def test_k_clamps_high(warm_index):
    res = retrieve_visible_tools("fetch radar reflectivity precipitation", None, 1000)
    discovered = res - set(CORE_FLOOR)
    assert len(discovered) <= MAX_K


def test_k_clamps_low_and_bad(warm_index):
    assert CORE_FLOOR <= retrieve_visible_tools("fetch radar", None, 0)
    assert CORE_FLOOR <= retrieve_visible_tools("fetch radar", None, -5)
    assert CORE_FLOOR <= retrieve_visible_tools("fetch radar", None, "garbage")  # type: ignore[arg-type]


def _pool_hidden_names() -> set[str]:
    """Registered pool-HIDDEN names: ``tier=internal`` only.

    An internal tool is never model-facing, carries no corpus and must NOT appear in
    the fail-open dump; engine templates are ordinary retrieval-pool members."""
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


def test_empty_query_returns_floor_only(warm_index):
    assert retrieve_visible_tools("   ", None, DEFAULT_K) == set(CORE_FLOOR)
    accrued = {"fetch_dem"}
    res = retrieve_visible_tools("", accrued, DEFAULT_K)
    assert res == accrued | set(CORE_FLOOR)
    assert set(TOOL_REGISTRY) - res  # full registry NOT dumped


_RECALL_FIXTURE = [
    ("show me the lightning over this storm from GOES", "fetch_glm_lightning"),
    ("detect the active fire hot pixels from GOES", "fetch_goes_active_fire"),
    ("get the elevation DEM for this area", "fetch_dem"),
    ("geocode this city to a bounding box", "geocode_location"),
    ("fetch high resolution aerial imagery for this area", "fetch_naip"),
    ("how much does incoming swell amplify inside this harbour basin",
     "artemis_harbor_agitation"),
    ("how far downstream does a dye spill travel in this river",
     "telemac_river_dye"),
    ("draw the topographic contour lines from the elevation", "compute_contours"),
    ("what telemac keyword controls the bottom friction law", "describe_keywords"),
    ("read every raster on this case at this spot", "probe_point"),
]


@pytest.mark.parametrize("query,want", _RECALL_FIXTURE)
def test_recall_surfaces_expected_tool(warm_index, query, want):
    res = retrieve_visible_tools(query, None, DEFAULT_K)
    assert want in res, f"recall miss: {want!r} not surfaced for {query!r}"


def _load_corpus():
    # Compose through the module's own loader (per-tool corpus.yaml tree +
    # residual) so the test never hardcodes the package depth or the split.
    return dd._load_corpus()


def _full_registry_names() -> set[str]:
    """The FULL registry after the startup import path has run, so the coverage
    check is deterministic regardless of test order."""
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
    """A corpus key for a tool nothing registers is dead weight in the index.

    The exception is a DECLARED PARKED template, whose corpus travels with the
    declaration; the visible set is derived from the registry, so it never surfaces."""
    from tests.search.test_door_dissolution import PARKED_TEMPLATES

    corpus = _load_corpus()
    dead = sorted(set(corpus) - _full_registry_names() - set(PARKED_TEMPLATES))
    assert not dead, (
        f"tool_query_corpus.yaml has keys for non-registered tools (prune them): {dead}"
    )


def test_every_corpus_entry_meets_query_floor():
    corpus = _load_corpus()
    thin = {t: len(q) for t, q in corpus.items() if len(q) < 5}
    assert not thin, f"corpus entries below the 5-query recall floor: {thin}"
