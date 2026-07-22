"""Validation + round-trip tests for GeoClaw (Clawpack) shallow-water
inundation contracts (sprint-17, clawpack.geoclaw engine).

Mirrors ``test_swmm_contracts.py``. Focused on the ADDITIVE field bundle
landed for the tsunami/surge engine (all defaults preserve prior behaviour;
``schema_version`` stays "v1"):

- ``GeoClawDepthLayerURI.arrival_time_s`` (fgmax wave-arrival-on-land time,
  ``None`` when fgmax was not run) round-trips, including the None default.
- ``GeoClawRunArgs`` user-gated Okada fault geometry
  (``fault_strike_deg`` / ``fault_dip_deg`` / ``fault_rake_deg`` /
  ``fault_depth_km``), the fine-coastal-DEM list ``extra_topo_uris`` (default
  ``[]``), ``fgmax_arrival_tol_m`` (default 0.01), and the optional
  ``coastal_gauge_lonlat`` all round-trip.
- Additive safety: ``GeoClawRunArgs`` built with NONE of the new fields still
  validates and keeps ``schema_version == "v1"``.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from grace2_contracts.geoclaw_contracts import (
    GEOCLAW_DEFAULT_FGMAX_ARRIVAL_TOL_M,
    GeoClawDepthLayerURI,
    GeoClawRunArgs,
)
from grace2_contracts.execution import LayerURI


# A small valid AOI bbox (lon-first EPSG:4326): Chattanooga-ish.
BBOX = (-85.32, 35.02, -85.28, 35.06)


def _depth_layer(**overrides: object) -> GeoClawDepthLayerURI:
    """A minimal valid GeoClaw depth layer (the three narration scalars set)."""
    base = dict(
        layer_id="run-01HX-depth",
        name="GeoClaw inundation depth (m)",
        layer_type="raster",
        uri="s3://trid3nt/runs/01HX/depth.cog.tif",
        style_preset="continuous_flood_depth",
        max_depth_m=2.4,
        flooded_area_km2=0.61,
        max_inundation_m=1.3,
    )
    base.update(overrides)
    return GeoClawDepthLayerURI(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Module constant
# --------------------------------------------------------------------------- #


def test_default_fgmax_arrival_tol_constant() -> None:
    assert GEOCLAW_DEFAULT_FGMAX_ARRIVAL_TOL_M == 0.01


# --------------------------------------------------------------------------- #
# GeoClawRunArgs - additive-safety + new-field defaults
# --------------------------------------------------------------------------- #


def test_run_args_minimal_validates_with_none_of_the_new_fields() -> None:
    """Built with NONE of the new fields the model still validates (additive
    safety) and keeps schema_version == 'v1'."""
    args = GeoClawRunArgs(bbox=BBOX)
    assert args.schema_version == "v1"
    # the four user-gated fault fields default to None (never fabricated)
    assert args.fault_strike_deg is None
    assert args.fault_dip_deg is None
    assert args.fault_rake_deg is None
    assert args.fault_depth_km is None
    # extra coastal DEMs default to an empty list (primary topo only)
    assert args.extra_topo_uris == []
    # fgmax arrival tolerance defaults to the module constant (0.01)
    assert args.fgmax_arrival_tol_m == GEOCLAW_DEFAULT_FGMAX_ARRIVAL_TOL_M == 0.01
    # no coastal gauge placed by default
    assert args.coastal_gauge_lonlat is None


def test_run_args_new_fields_are_added_not_on_base_run_args() -> None:
    """The new fields exist on GeoClawRunArgs (additive growth)."""
    for f in (
        "fault_strike_deg",
        "fault_dip_deg",
        "fault_rake_deg",
        "fault_depth_km",
        "extra_topo_uris",
        "fgmax_arrival_tol_m",
        "coastal_gauge_lonlat",
    ):
        assert f in GeoClawRunArgs.model_fields


def test_run_args_explicit_new_field_overrides() -> None:
    args = GeoClawRunArgs(
        bbox=BBOX,
        scenario="tsunami",
        fault_strike_deg=198.0,
        fault_dip_deg=22.0,
        fault_rake_deg=90.0,
        fault_depth_km=12.5,
        extra_topo_uris=["s3://b/coarse.tif", "s3://b/fine.tif"],
        fgmax_arrival_tol_m=0.05,
        coastal_gauge_lonlat=(-85.30, 35.04),
    )
    assert args.fault_strike_deg == 198.0
    assert args.fault_dip_deg == 22.0
    assert args.fault_rake_deg == 90.0
    assert args.fault_depth_km == 12.5
    assert args.extra_topo_uris == ["s3://b/coarse.tif", "s3://b/fine.tif"]
    assert args.fgmax_arrival_tol_m == 0.05
    assert args.coastal_gauge_lonlat == (-85.30, 35.04)


# --------------------------------------------------------------------------- #
# GeoClawRunArgs - new-field validation bounds
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("strike", [-0.1, 360.1])
def test_fault_strike_in_0_360(strike: float) -> None:
    with pytest.raises(ValidationError):
        GeoClawRunArgs(bbox=BBOX, fault_strike_deg=strike)


@pytest.mark.parametrize("dip", [0.0, -1.0, 90.1])
def test_fault_dip_in_0_excl_90(dip: float) -> None:
    with pytest.raises(ValidationError):
        GeoClawRunArgs(bbox=BBOX, fault_dip_deg=dip)


@pytest.mark.parametrize("rake", [-180.1, 180.1])
def test_fault_rake_in_neg180_180(rake: float) -> None:
    with pytest.raises(ValidationError):
        GeoClawRunArgs(bbox=BBOX, fault_rake_deg=rake)


@pytest.mark.parametrize("depth", [0.0, -1.0])
def test_fault_depth_must_be_positive(depth: float) -> None:
    with pytest.raises(ValidationError):
        GeoClawRunArgs(bbox=BBOX, fault_depth_km=depth)


@pytest.mark.parametrize("tol", [0.0, -0.01])
def test_fgmax_arrival_tol_must_be_positive(tol: float) -> None:
    with pytest.raises(ValidationError):
        GeoClawRunArgs(bbox=BBOX, fgmax_arrival_tol_m=tol)


def test_fault_bounds_inclusive_endpoints_accepted() -> None:
    args = GeoClawRunArgs(
        bbox=BBOX,
        fault_strike_deg=0.0,
        fault_dip_deg=90.0,
        fault_rake_deg=180.0,
    )
    assert args.fault_strike_deg == 0.0
    assert args.fault_dip_deg == 90.0
    assert args.fault_rake_deg == 180.0
    args2 = GeoClawRunArgs(bbox=BBOX, fault_strike_deg=360.0, fault_rake_deg=-180.0)
    assert args2.fault_strike_deg == 360.0
    assert args2.fault_rake_deg == -180.0


# --------------------------------------------------------------------------- #
# GeoClawRunArgs - round-trip (model_dump -> reload equality)
# --------------------------------------------------------------------------- #


def test_run_args_roundtrip_with_new_fields() -> None:
    args = GeoClawRunArgs(
        bbox=BBOX,
        scenario="tsunami",
        fault_strike_deg=198.0,
        fault_dip_deg=22.0,
        fault_rake_deg=90.0,
        fault_depth_km=12.5,
        extra_topo_uris=["s3://b/coarse.tif", "s3://b/fine.tif"],
        fgmax_arrival_tol_m=0.05,
        coastal_gauge_lonlat=(-85.30, 35.04),
    )
    a = args.model_dump(mode="json")
    text_a = json.dumps(a, sort_keys=True)
    reloaded = GeoClawRunArgs.model_validate(json.loads(text_a))
    b = reloaded.model_dump(mode="json")
    assert text_a == json.dumps(b, sort_keys=True)
    # the new fields survive the round-trip intact
    assert reloaded.fault_strike_deg == 198.0
    assert reloaded.fault_dip_deg == 22.0
    assert reloaded.fault_rake_deg == 90.0
    assert reloaded.fault_depth_km == 12.5
    assert reloaded.extra_topo_uris == ["s3://b/coarse.tif", "s3://b/fine.tif"]
    assert reloaded.fgmax_arrival_tol_m == 0.05
    # tuple round-trips through a JSON list back to a tuple
    assert reloaded.coastal_gauge_lonlat == (-85.30, 35.04)
    assert reloaded.schema_version == "v1"


def test_run_args_roundtrip_defaults_only() -> None:
    """The default-only model (none of the new fields set) round-trips with the
    None / [] / 0.01 defaults preserved and schema_version unchanged."""
    args = GeoClawRunArgs(bbox=BBOX)
    a = args.model_dump(mode="json")
    text_a = json.dumps(a, sort_keys=True)
    reloaded = GeoClawRunArgs.model_validate(json.loads(text_a))
    b = reloaded.model_dump(mode="json")
    assert text_a == json.dumps(b, sort_keys=True)
    assert reloaded.fault_strike_deg is None
    assert reloaded.fault_dip_deg is None
    assert reloaded.fault_rake_deg is None
    assert reloaded.fault_depth_km is None
    assert reloaded.extra_topo_uris == []
    assert reloaded.fgmax_arrival_tol_m == 0.01
    assert reloaded.coastal_gauge_lonlat is None
    assert reloaded.schema_version == "v1"


# --------------------------------------------------------------------------- #
# GeoClawDepthLayerURI.arrival_time_s - added scalar + bounds + round-trip
# --------------------------------------------------------------------------- #


def test_depth_layer_is_a_layer_uri() -> None:
    layer = _depth_layer()
    assert isinstance(layer, LayerURI)
    assert layer.layer_type == "raster"


def test_arrival_time_default_is_none_and_is_added() -> None:
    """arrival_time_s defaults to None (fgmax not run) and is a subclass field
    not on the base LayerURI."""
    layer = _depth_layer()
    assert layer.arrival_time_s is None
    assert "arrival_time_s" not in LayerURI.model_fields
    assert "arrival_time_s" in GeoClawDepthLayerURI.model_fields


def test_arrival_time_set_value_accepted() -> None:
    layer = _depth_layer(arrival_time_s=742.0)
    assert layer.arrival_time_s == 742.0


def test_arrival_time_zero_allowed() -> None:
    layer = _depth_layer(arrival_time_s=0.0)
    assert layer.arrival_time_s == 0.0


@pytest.mark.parametrize("t", [-0.1, -1.0])
def test_arrival_time_must_be_non_negative(t: float) -> None:
    with pytest.raises(ValidationError):
        _depth_layer(arrival_time_s=t)


def test_depth_layer_roundtrip_with_arrival_time() -> None:
    layer = _depth_layer(arrival_time_s=742.0, scenario="tsunami", units="m")
    a = layer.model_dump(mode="json")
    text_a = json.dumps(a, sort_keys=True)
    reloaded = GeoClawDepthLayerURI.model_validate(json.loads(text_a))
    b = reloaded.model_dump(mode="json")
    assert text_a == json.dumps(b, sort_keys=True)
    assert reloaded.arrival_time_s == 742.0
    assert reloaded.max_depth_m == 2.4
    assert reloaded.scenario == "tsunami"


def test_depth_layer_roundtrip_arrival_time_none_default() -> None:
    """Round-trip of the None default for arrival_time_s (fgmax not run)."""
    layer = _depth_layer()
    a = layer.model_dump(mode="json")
    text_a = json.dumps(a, sort_keys=True)
    reloaded = GeoClawDepthLayerURI.model_validate(json.loads(text_a))
    b = reloaded.model_dump(mode="json")
    assert text_a == json.dumps(b, sort_keys=True)
    assert reloaded.arrival_time_s is None


# --------------------------------------------------------------------------- #
# schema_version unchanged
# --------------------------------------------------------------------------- #


def test_schema_version_is_v1() -> None:
    assert GeoClawRunArgs(bbox=BBOX).schema_version == "v1"
    assert GeoClawRunArgs.model_fields["schema_version"].default == "v1"
