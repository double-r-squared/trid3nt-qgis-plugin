"""Promotion parity tests (data-router fold, phase-2 wave-1 pilots + wave-2 ArcGIS family).

Migrated from the deleted twin test files (wave-1: test_fetch_gridmet /
_hifld_critical_infrastructure / _noaa_coops_tides / _esri_landcover_10m /
_census_acs; wave-2: test_fetch_nifc_fire_perimeters / _hifld_transmission_lines /
_mtbs_burn_severity / _cdc_svi / _nhd_waterbodies / _us_drought_monitor). Those
files unit-tested the twins' INTERNAL helpers (``_plan_tile_grid``, ``_VARIABLES``,
``_build_where_clause``, ``_normalize_props``, ``_ddate_to_iso`` ...) which no
longer exist -- deleted per the migration rule. The CONTRACT-level behavior that
survives the fold (each twin name is a registered tool with the twin's signature /
docstring / typed errors) is re-expressed HERE against the promoted router surface.
Deeper behavior parity (values, layer output, caveats, every error path) is covered
twin-vs-router by ``experiments/fetcher_fold_replication`` (wave-2 6/6 edge matrix)
and the router unit suites (``test_router_engine`` / ``test_router_executors`` /
``test_router_spec_loader``).
"""

from __future__ import annotations

import pytest

from trid3nt_server.agent.tools import TOOL_REGISTRY
from trid3nt_server.agent.adapters.adapter import build_tool_declarations
from trid3nt_server.agent.tools.fetchers._router.errors import (
    RouterInputError,
    bbox_error_suffix,
)
from trid3nt_server.agent.tools.fetchers._router.spec import compose_specs_from_tree

# The 5 promoted pilots: name -> (source_class, expected declaration inputSchema).
# The schema map is the ground truth captured from the hand-written twins BEFORE
# deletion (properties + required set) -- the fold must reproduce it byte-for-byte.
PROMOTED = {
    "fetch_gridmet": {
        "source_class": "gridmet",
        "properties": ["bbox", "end_date", "start_date", "variable"],
        "required": ["bbox", "end_date", "start_date", "variable"],
    },
    "fetch_hifld_critical_infrastructure": {
        "source_class": "hifld_critical_infrastructure",
        "properties": ["bbox", "facility_type"],
        "required": ["bbox", "facility_type"],
    },
    "fetch_noaa_coops_tides": {
        "source_class": "noaa_coops_tides",
        "properties": ["bbox", "end_date", "product", "start_date"],
        "required": ["bbox", "end_date", "start_date"],
    },
    "fetch_esri_landcover_10m": {
        "source_class": "esri_landcover_10m",
        "properties": ["bbox", "year"],
        "required": ["bbox"],
    },
    "fetch_census_acs": {
        "source_class": "census_acs",
        "properties": ["bbox", "variable", "year"],
        "required": ["bbox"],
    },
    # --- phase-2 wave-2: the ArcGIS FeatureServer/MapServer vector family ---
    "fetch_nifc_fire_perimeters": {
        "source_class": "nifc_perimeters",
        "properties": ["bbox", "status"],
        # A None-default param is required-in-schema per the adapter (twin-identical);
        # status carries a real "active" default so it stays optional.
        "required": ["bbox"],
    },
    "fetch_hifld_transmission_lines": {
        "source_class": "hifld_transmission_lines",
        "properties": ["bbox", "min_voltage_kv"],
        "required": ["bbox", "min_voltage_kv"],
    },
    "fetch_mtbs_burn_severity": {
        "source_class": "mtbs_burn_severity",
        "properties": ["bbox", "year_range"],
        "required": ["bbox", "year_range"],
    },
    "fetch_cdc_svi": {
        "source_class": "cdc_svi",
        "properties": ["bbox"],
        "required": ["bbox"],
    },
    "fetch_nhd_waterbodies": {
        "source_class": "nhd_waterbodies",
        "properties": ["bbox"],
        "required": ["bbox"],
    },
    "fetch_us_drought_monitor": {
        "source_class": "us_drought_monitor",
        "properties": ["bbox", "date"],
        "required": ["bbox", "date"],
    },
    # --- phase-2 wave-3: USGS water-data family (dataretrieval-delegated, ADR 0040) ---
    "fetch_usgs_water_quality": {
        "source_class": "usgs_water_quality",
        # bbox + characteristic both carry defaults in the twin (bbox=None,
        # characteristic="Nitrate") -> both optional-in-schema (required=[]);
        # the delegating executor's pre_validate raises WQP_INPUT_ERROR when
        # bbox is missing.
        "properties": ["bbox", "characteristic"],
        "required": [],
    },
    "fetch_nhdplus_nldi_navigate": {
        "source_class": "nhdplus_nldi",
        # all four params carry defaults in the twin -> required=[]; the executor
        # enforces the seed_point/comid mutual-exclusion + CONUS + comid gate.
        "properties": ["comid", "direction", "distance_km", "seed_point"],
        "required": [],
    },
    # --- phase-2 wave-4: station family (snapshot mode, ADR 0045) ---
    "fetch_noaa_coops_currents": {
        "source_class": "noaa_coops_currents",
        # twin sig: fetch_noaa_coops_currents(bbox, product="currents", **_extra) --
        # bbox required (no default), product optional (default="currents").
        "properties": ["bbox", "product"],
        "required": ["bbox"],
    },
}

_SPECS = compose_specs_from_tree()


@pytest.mark.parametrize("name", sorted(PROMOTED))
def test_pilot_registered_as_general_tool(name: str) -> None:
    """The twin name resolves to a promoted spec-driven tool in the default pool."""
    entry = TOOL_REGISTRY.get(name)
    assert entry is not None, f"{name} not registered (promotion did not fire)"
    # tier=general -> in every default-pool producer (none filter it out).
    assert getattr(entry.metadata, "tier", "general") == "general"
    assert entry.metadata.source_class == PROMOTED[name]["source_class"]
    assert entry.metadata.cacheable is True
    # The callable resolves through the router engine's synthetic module (so the
    # payload-warning seam finds the synthesized estimate_payload_mb).
    assert entry.module.endswith(f"_router._promoted.{name}")
    import importlib

    mod = importlib.import_module(entry.module)
    assert callable(getattr(mod, "estimate_payload_mb"))


@pytest.mark.parametrize("name", sorted(PROMOTED))
def test_pilot_declaration_schema_matches_twin(name: str) -> None:
    """FunctionDeclaration inputSchema (properties + required) == the twin's."""
    entry = TOOL_REGISTRY[name]
    decls = build_tool_declarations({name: entry})
    assert len(decls) == 1
    d = decls[0]
    assert d.name == name
    assert d.parameters is not None
    props = sorted((d.parameters.properties or {}).keys())
    required = sorted(d.parameters.required or [])
    assert props == PROMOTED[name]["properties"], f"{name} properties drifted"
    assert required == PROMOTED[name]["required"], f"{name} required set drifted"
    # A non-trivial description is carried (the twin docstring, indistinguishable).
    assert d.description and len(d.description) > 200


@pytest.mark.parametrize("name", sorted(PROMOTED))
def test_pilot_docstring_is_twin_verbatim(name: str) -> None:
    """The promoted tool carries the spec docstring (== twin, drives the index)."""
    entry = TOOL_REGISTRY[name]
    spec = _SPECS[name]
    assert spec.docstring, f"{name} source.yaml lost its docstring"
    # The promoted callable's __doc__ IS the spec docstring verbatim (the sole
    # source of the FunctionDeclaration description + the retrieval-index document).
    assert entry.fn.__doc__ == spec.docstring


@pytest.mark.parametrize("name", sorted(PROMOTED))
def test_pilot_degenerate_bbox_raises_twin_typed_error(name: str) -> None:
    """A degenerate bbox raises the twin-identical typed input error, pre-network.

    Validation runs before any endpoint call (offline), and the router stamps the
    twin's exact A.6 error_code (prefix from spec.error_code_prefix, suffix from
    the bbox param / spec-level input suffix)."""
    entry = TOOL_REGISTRY[name]
    spec = _SPECS[name]
    # NLDI has no bbox param (seed_point/comid selector); its degenerate-bbox case
    # is meaningless -- its selector edges are covered by the dedicated tests below.
    if not any(p.type == "bbox" for p in spec.params.values()):
        pytest.skip(f"{name} has no bbox param")
    expected_code = f"{spec.error_code_prefix}_{bbox_error_suffix(spec)}"
    # min_lon == max_lon -> degenerate (rejected by _validate_bbox before network).
    with pytest.raises(RouterInputError) as excinfo:
        entry.fn(bbox=(-100.0, 40.0, -100.0, 41.0))
    assert excinfo.value.error_code == expected_code
    assert excinfo.value.retryable is False


# ---------------------------------------------------------------------------
# Phase-2 wave-3: USGS water-data family input-validation (migrated from the
# deleted twin tests test_fetch_usgs_water_quality + the NLDI slice of
# test_pfdf_unlock_statsgo_nldi_3dep). All raise pre-network via the router's
# validate_params + the delegating executor's pre_validate (offline).
# ---------------------------------------------------------------------------


def _route(name: str, **params):
    from trid3nt_server.agent.tools.fetchers._router import router as _r
    return _r.route(_SPECS[name], params)


@pytest.mark.parametrize(
    "params, code",
    [
        (dict(characteristic="Nitrate"), "WQP_INPUT_ERROR"),                       # missing bbox
        (dict(bbox=(-100.0, 40.0, -100.0, 41.0), characteristic="Nitrate"), "WQP_INPUT_ERROR"),  # degenerate
        (dict(bbox=(-120.0, 30.0, -100.0, 45.0), characteristic="Nitrate"), "WQP_INPUT_ERROR"),  # > 100 deg^2
        (dict(bbox=(-93.3, 41.9, -93.1, 42.1), characteristic=""), "WQP_INPUT_ERROR"),           # empty characteristic
    ],
)
def test_wqp_input_validation(params: dict, code: str) -> None:
    with pytest.raises(RouterInputError) as exc:
        _route("fetch_usgs_water_quality", **params)
    assert exc.value.error_code == code
    assert exc.value.retryable is False


def test_wqp_characteristic_alias_resolves() -> None:
    """The friendly alias resolves to the canonical WQP name (LayerURI.units)."""
    spec = _SPECS["fetch_usgs_water_quality"]
    from trid3nt_server.agent.tools.fetchers._router.router import validate_params
    p = validate_params(spec, dict(bbox=[-93.3, 41.9, -93.1, 42.1], characteristic="do"))
    assert p["characteristic"] == "Dissolved oxygen (DO)"  # alias-mapped
    p2 = validate_params(spec, dict(bbox=[-93.3, 41.9, -93.1, 42.1], characteristic="Arsenic"))
    assert p2["characteristic"] == "Arsenic"  # canonical passes through verbatim


@pytest.mark.parametrize(
    "params",
    [
        dict(),                                              # neither seed nor comid
        dict(seed_point=(-81.85, 26.55), comid=15334434),    # both (mutual exclusion)
        dict(comid=0),                                       # comid <= 0
        dict(comid=-1),                                      # comid < 0
        dict(seed_point=(15.0, 35.0)),                       # Mediterranean -> outside CONUS
        dict(comid=123, direction="XX"),                     # unknown direction
        dict(comid=123, distance_km=-1.0),                   # distance below 0
        dict(comid=123, distance_km=99999.0),                # distance above 1000
    ],
)
def test_nldi_input_validation(params: dict) -> None:
    with pytest.raises(RouterInputError) as exc:
        _route("fetch_nhdplus_nldi_navigate", **params)
    assert exc.value.error_code == "NHDPLUS_NLDI_INPUT_INVALID"
    assert exc.value.retryable is False


# ---------------------------------------------------------------------------
# Phase-2 wave-4: CO-OPS currents snapshot (migrated from the deleted twin test
# test_fetch_noaa_coops_currents -- the internal _parse_observed / _parse_predictions
# helpers became the named `coops_currents` router transform). Input validation
# routes through validate_params (offline, pre-network); the snapshot selector is
# unit-tested directly with synthetic CO-OPS bodies.
# ---------------------------------------------------------------------------

import datetime as _dt  # noqa: E402

from trid3nt_server.agent.tools.fetchers._router.executors.station_timeseries import (  # noqa: E402
    coops_currents_select,
)

_CUR_NOW = _dt.datetime(2026, 6, 27, 23, 0, 0, tzinfo=_dt.timezone.utc)


@pytest.mark.parametrize(
    "params, code",
    [
        (dict(product="currents"), "COOPS_CURRENTS_INPUT_ERROR"),                        # missing bbox
        (dict(bbox=(-122.5, 37.4, -122.5, 38.2)), "COOPS_CURRENTS_INPUT_ERROR"),          # degenerate
        (dict(bbox=(-123.0, 37.4, -122.0, 38.2), product="water_level"), "COOPS_CURRENTS_INPUT_ERROR"),  # bad enum
    ],
)
def test_coops_currents_input_validation(params: dict, code: str) -> None:
    with pytest.raises(RouterInputError) as exc:
        _route("fetch_noaa_coops_currents", **params)
    assert exc.value.error_code == code
    assert exc.value.retryable is False


def test_coops_currents_select_observed_picks_latest() -> None:
    body = {"data": [
        {"t": "2026-06-27 22:00", "s": "0.30", "d": "120", "b": "4"},
        {"t": "2026-06-27 23:00", "s": "0.45", "d": "167", "b": "4"},  # latest
        {"t": "2026-06-27 21:00", "s": "0.20", "d": "90", "b": "4"},
    ]}
    snap = coops_currents_select(body, "currents", _CUR_NOW)
    assert snap is not None
    assert snap["speed_kn"] == pytest.approx(0.45)
    assert snap["direction_deg"] == pytest.approx(167.0)
    assert snap["datetime"] == "2026-06-27T23:00Z"
    assert snap["bin"] == 4
    assert snap["flow_state"] == ""


def test_coops_currents_select_observed_skips_missing() -> None:
    body = {"data": [
        {"t": "2026-06-27 22:00", "s": "", "d": "120", "b": "4"},
        {"t": "2026-06-27 23:00", "s": "0.45", "d": "", "b": "4"},
        {"t": "2026-06-27 21:00", "s": "0.20", "d": "90", "b": "4"},  # only valid
    ]}
    snap = coops_currents_select(body, "currents", _CUR_NOW)
    assert snap is not None and snap["speed_kn"] == pytest.approx(0.20)


def test_coops_currents_select_predictions_flood_dir() -> None:
    body = {"current_predictions": {"units": "knots", "cp": [
        {"Time": "2026-06-27 20:00", "Type": "ebb", "Velocity_Major": -0.8,
         "meanFloodDir": 356, "meanEbbDir": 170, "Bin": "8"},
        {"Time": "2026-06-27 23:10", "Type": "flood", "Velocity_Major": 1.2,
         "meanFloodDir": 356, "meanEbbDir": 170, "Bin": "8"},  # nearest now
        {"Time": "2026-06-28 03:00", "Type": "slack", "Velocity_Major": 0,
         "meanFloodDir": 356, "meanEbbDir": 170, "Bin": "8"},
    ]}}
    snap = coops_currents_select(body, "currents_predictions", _CUR_NOW)
    assert snap is not None
    assert snap["speed_kn"] == pytest.approx(1.2)
    assert snap["direction_deg"] == pytest.approx(356.0)  # flood direction
    assert snap["flow_state"] == "flood"
    assert snap["datetime"] == "2026-06-27T23:10Z"


def test_coops_currents_select_predictions_ebb_dir() -> None:
    body = {"current_predictions": {"cp": [
        {"Time": "2026-06-27 23:05", "Type": "ebb", "Velocity_Major": -0.9,
         "meanFloodDir": 356, "meanEbbDir": 170, "Bin": "8"},
    ]}}
    snap = coops_currents_select(body, "currents_predictions", _CUR_NOW)
    assert snap is not None
    assert snap["speed_kn"] == pytest.approx(0.9)  # abs of negative velocity
    assert snap["direction_deg"] == pytest.approx(170.0)  # ebb direction
    assert snap["flow_state"] == "ebb"


def test_coops_currents_select_empty() -> None:
    assert coops_currents_select({"data": []}, "currents", _CUR_NOW) is None
    assert coops_currents_select({"current_predictions": {"cp": []}}, "currents_predictions", _CUR_NOW) is None
