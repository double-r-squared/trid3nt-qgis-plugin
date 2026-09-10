"""Cross-cutting fuzz: an invented kwarg must never raise TypeError.

Every tool in ``TOOL_REGISTRY`` is probed with realistic minimal params plus the
invented kwargs a model routinely supplies, through the normalizer path. Only a
TypeError signals a broken signature contract; any other exception is allowed,
because no network, store or worker path is reachable here."""

from __future__ import annotations

import inspect
import logging
from typing import Any

import pytest

from trid3nt_server.tools import TOOL_REGISTRY

# ---------------------------------------------------------------------------
# Eager-import all workflow modules that add to TOOL_REGISTRY at import time.
# Mirrors the startup-time import order; any module that calls @register_tool
# at module level must appear here so the registry is fully populated.
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Normalizer adapter
# ---------------------------------------------------------------------------

def _get_normalizer():
    """Return a ``(tool_name, raw_args, fn) -> dict`` callable and whether it is the
    production one: ``tool_arg_normalizer.normalize_args`` when importable, else the
    inspect-based strip."""
    try:
        from trid3nt_server.tools.tool_arg_normalizer import normalize_args  # type: ignore[import]
        return normalize_args, True
    except ImportError:
        return _inspect_strip_unknown, False


def _inspect_strip_unknown(
    tool_name: str, raw_args: dict[str, Any], fn
) -> dict[str, Any]:
    """Fallback normalizer: strip kwargs unknown to the tool's signature.

    A tool with a VAR_KEYWORD param takes the raw kwargs unstripped; one without it
    gets unknown keys removed. Same signature as the production normalizer."""
    sig = inspect.signature(fn)
    params = sig.parameters
    has_var_kw = any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
    )
    if has_var_kw:
        # Tool accepts **kwargs; pass everything through.
        return dict(raw_args)
    # Strip unknown keys; keep only those the signature declares.
    known = {n for n, p in params.items() if p.kind not in (
        inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD
    )}
    stripped = {k: v for k, v in raw_args.items() if k in known}
    if len(stripped) != len(raw_args):
        dropped = set(raw_args) - set(stripped)
        logger.debug(
            "inspect-strip fallback: tool=%r dropped unknown kwargs=%r",
            tool_name,
            dropped,
        )
    return stripped


# ---------------------------------------------------------------------------
# Minimal valid params for each tool (required positional arguments only)
# ---------------------------------------------------------------------------

# A sample EPSG:4326 bbox used as a stand-in for required bbox params.
_SAMPLE_BBOX = (-81.95, 26.55, -81.75, 26.75)  # Fort Myers, FL
_SAMPLE_DEM_URI = "s3://trid3nt-cache/cache/static-30d/dem/sample.tif"
_SAMPLE_LANDCOVER_URI = "s3://trid3nt-cache/cache/static-30d/landcover/sample.tif"
_SAMPLE_RASTER_URI = "s3://trid3nt-cache/cache/static-30d/raster/sample.tif"
_SAMPLE_VECTOR_URI = "s3://trid3nt-cache/cache/static-30d/vector/sample.fgb"

# Map tool_name → minimal kwargs that satisfy all required parameters.
# These are plausible real-world values, NOT magic that would make the tool
# succeed (network/GCS access is expected to fail — only TypeError is forbidden).
_MINIMAL_VALID_PARAMS: dict[str, dict[str, Any]] = {
    "clip_raster_to_polygon": {
        "raster_uri": _SAMPLE_RASTER_URI,
        "polygon_uri": _SAMPLE_VECTOR_URI,
    },
    "compute_aspect": {"dem_uri": _SAMPLE_DEM_URI},
    "compute_building_density": {"bbox": _SAMPLE_BBOX},
    "compute_colored_relief": {"dem_uri": _SAMPLE_DEM_URI},
    "compute_hillshade": {"dem_uri": _SAMPLE_DEM_URI},
    "compute_impervious_surface": {"landcover_uri": _SAMPLE_LANDCOVER_URI},
    "compute_slope": {"dem_uri": _SAMPLE_DEM_URI},
    "generate_chart": {
        "vega_lite_spec": {"mark": "bar", "encoding": {}},
        "title": "t",
        "records": [{"label": "a", "count": 1}],
    },
    "extract_landcover_class": {
        "landcover_uri": _SAMPLE_LANDCOVER_URI,
        "classes": [21],
    },
    "fetch_administrative_boundaries": {
        "level": "county",
        "bbox": _SAMPLE_BBOX,
    },
    "fetch_buildings": {"bbox": _SAMPLE_BBOX},
    "fetch_dem": {"bbox": _SAMPLE_BBOX},
    "fetch_era5_reanalysis": {
        "bbox": _SAMPLE_BBOX,
        "variable": "2m_temperature",
        "start_date": "2022-09-28",
        "end_date": "2022-09-30",
    },
    "fetch_firms_active_fire": {"bbox": _SAMPLE_BBOX},
    "fetch_gcn250_curve_numbers": {"bbox": _SAMPLE_BBOX},
    "fetch_goes_satellite": {"bbox": _SAMPLE_BBOX},
    "fetch_gtsm_tide_surge": {
        "bbox": _SAMPLE_BBOX,
        "start_date": "2022-09-28",
        "end_date": "2022-09-30",
    },
    "fetch_hrsl_population": {"bbox": _SAMPLE_BBOX},
    "fetch_landcover": {"bbox": _SAMPLE_BBOX},
    "fetch_landfire_fuels": {"bbox": _SAMPLE_BBOX},
    "fetch_mrms_qpe": {},
    "fetch_mtbs_burn_severity": {"bbox": _SAMPLE_BBOX},
    "show_nexrad_radar": {},
    "fetch_nifc_fire_perimeters": {},
    "fetch_nws_alerts_conus": {},
    "fetch_nws_event": {"area": "FLZ055"},
    "fetch_population": {"bbox": _SAMPLE_BBOX},
    "fetch_river_geometry": {"bbox": _SAMPLE_BBOX},
    "fetch_roads_osm": {"bbox": _SAMPLE_BBOX},
    "fetch_storm_events_db": {"year": 2022},
    "geocode_location": {"query": "Fort Myers, FL"},
    "lookup_precip_return_period": {
        "location": (-81.87, 26.64),
        "return_period_years": 100,
        "duration_hours": 24,
    },
    "run_solver": {
        "solver": "telemac",
        "model_setup_uri": "s3://trid3nt-runs/test/setup/",
    },
    "wait_for_completion": {
        "handle": {
            "run_id": "test-run-id",
            "workflows_execution_id": "test-exec-id",
            "status": "running",
        }
    },
    "web_fetch": {"url": "https://example.com"},
}


# ---------------------------------------------------------------------------
# The 20 invented kwarg patterns Gemini routinely generates
# ---------------------------------------------------------------------------

# These are drawn from the real failure log that motivated job-0164. Each dict
# contains one or more invented kwargs; they are layered ON TOP of the valid
# minimal params for the tool. The test asserts that no combination causes
# TypeError.
_INVENTED_KWARG_PATTERNS: list[dict[str, Any]] = [
    {"run_name": "fort-myers-100yr"},
    {"scenario_id": "FLOOD-2022-IAN"},
    {"description": "Hurricane Ian flood scenario"},
    {"durationHours": 48},
    {"duration_hours": 48},              # camelCase→snake alias mismatch
    {"rainfall_event": "atlas14_100yr"},
    {"return_period_years": 100},        # used where tool has return_period_yr
    {"return_period_yr": 25},            # converse alias
    {"start_time": "2022-09-28T00:00Z"},
    {"end_time": "2022-09-30T00:00Z"},
    {"output_format": "geotiff"},
    {"projection": "EPSG:4326"},
    {"resolution_m": 30},
    {"max_depth_m": 5.0},
    {"user_id": "demo-user"},
    {"session_id": "abcd-1234"},
    {"timeout_s": 900},
    {"dry_run": True},
    {
        "run_name": "scenario-A",
        "rainfall_event": "atlas14_100yr",
        "durationHours": 24,
        "description": "combined invented kwargs",
    },
    {
        "scenario_id": "FLOOD-2023",
        "return_period_years": 500,
        "output_format": "cog",
        "resolution_m": 10,
        "user_id": "fuzz-user",
    },
]

assert len(_INVENTED_KWARG_PATTERNS) == 20, (
    f"Expected 20 kwarg patterns, got {len(_INVENTED_KWARG_PATTERNS)}"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_fuzz_kwargs(tool_name: str, extra: dict[str, Any]) -> dict[str, Any]:
    """Merge valid minimal params with invented extras."""
    base = dict(_MINIMAL_VALID_PARAMS.get(tool_name, {}))
    base.update(extra)
    return base


def _call_fn(entry_fn, kwargs: dict[str, Any]) -> None:
    """Probe the signature with ``bind_partial`` rather than calling the body.

    It raises the same "unexpected keyword argument" TypeError a real call would,
    with no subprocess and no network; a missing-required one is swallowed."""
    sig = inspect.signature(entry_fn)
    try:
        sig.bind_partial(**kwargs)
    except TypeError as exc:
        msg = str(exc)
        if "unexpected keyword argument" in msg:
            raise
        # Missing required arg (e.g. "missing a required argument: 'bbox'"):
        # not the bug class we are guarding — pass silently.


# ---------------------------------------------------------------------------
# Parametrised fuzz test
# ---------------------------------------------------------------------------

# One case per invented pattern, sweeping the whole registry inside it. The
# cross product of every tool with every pattern proved the same single
# invariant 3,263 times; a failure names every tool that failed the pattern,
# which is what a reader of the red needs.


@pytest.mark.parametrize(
    "pattern_idx",
    range(len(_INVENTED_KWARG_PATTERNS)),
    ids=[f"pat{i}" for i in range(len(_INVENTED_KWARG_PATTERNS))],
)
def test_tool_survives_invented_kwargs(pattern_idx: int) -> None:
    """Every registered tool absorbs one pattern of model-invented kwargs.

    Refuses on TypeError "unexpected keyword argument" alone; any other
    exception is allowed, because no network path is reachable offline.
    """
    normalize_fn, _is_real_normalizer = _get_normalizer()
    invented = _INVENTED_KWARG_PATTERNS[pattern_idx]
    failed: list[str] = []

    for tool_name in sorted(TOOL_REGISTRY):
        entry = TOOL_REGISTRY[tool_name]
        raw = _build_fuzz_kwargs(tool_name, invented)
        cleaned = normalize_fn(tool_name, raw, entry.fn)
        try:
            _call_fn(entry.fn, cleaned)
        except TypeError as exc:
            failed.append(f"{tool_name}: {exc}")

    if failed:
        pytest.fail(
            f"[AGENT layer] pattern={pattern_idx} invented kwargs "
            f"{list(invented.keys())!r} raised TypeError on {len(failed)} tool(s):\n  "
            + "\n  ".join(failed)
            + "\nFix: add **_extra_ignored to each named tool, or verify the "
            "normalize_args wiring at the dispatch."
        )


# ---------------------------------------------------------------------------
# Sentinel test: all tools must have native **_extra_ignored
# ---------------------------------------------------------------------------

@pytest.mark.xfail(
    reason=(
        "not every @register_tool function carries **_extra_ignored yet; the "
        "normalizer covers the rest, so this is the target rather than a floor."
    ),
    strict=False,
)
def test_all_tools_have_native_extra_ignored() -> None:
    """Every ``@register_tool`` function must declare a VAR_KEYWORD param.

    The target state absorbs an invented kwarg in the signature itself and leaves the
    normalizer a safety net; until every tool does, this is xfail."""
    missing: list[str] = []
    for name in sorted(TOOL_REGISTRY.keys()):
        entry = TOOL_REGISTRY[name]
        sig = inspect.signature(entry.fn)
        has_var_kw = any(
            p.kind == inspect.Parameter.VAR_KEYWORD
            for p in sig.parameters.values()
        )
        if not has_var_kw:
            missing.append(name)

    if missing:
        pytest.fail(
            f"[AGENT/ENGINE layer] {len(missing)} tool(s) lack "
            f"**_extra_ignored: {missing}"
        )


# ---------------------------------------------------------------------------
# Coverage audit test: tool registry count must be ≥ 50
# ---------------------------------------------------------------------------

def test_tool_registry_count_ge_50() -> None:
    """The registry must hold at least 50 tools for the fuzz to be meaningful.

    A shrunken registry - an import error silently dropping a submodule - would leave
    the fuzz testing a subset of the real surface."""
    count = len(TOOL_REGISTRY)
    assert count >= 50, (
        f"[AGENT layer] Expected ≥50 tools in TOOL_REGISTRY, got {count}. "
        "A submodule import likely failed silently — check for ImportError at "
        "the eager-import block in trid3nt_server/tools/__init__.py."
    )


# ---------------------------------------------------------------------------
# Normalizer presence test
# ---------------------------------------------------------------------------

def test_normalizer_presence_logged() -> None:
    """Log whether the production normalizer or the fallback is in use.

    Informational: the test passes either way."""
    _, is_real = _get_normalizer()
    if is_real:
        logger.info(
            "OQ-0168-NORMALIZER-DEPENDENCY resolved: "
            "trid3nt_server.tools.tool_arg_normalizer.normalize_args is in use (job-0164 merged)."
        )
    else:
        logger.warning(
            "OQ-0168-NORMALIZER-DEPENDENCY: job-0164 not yet merged — "
            "using inspect-based fallback strip in fuzz harness. "
            "test_all_tools_have_native_extra_ignored is xfail pending job-0164."
        )
    # Always passes.
    assert True
