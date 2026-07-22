"""Validation + round-trip tests for SWMM quasi-2D urban-flood contracts
(sprint-16 P1, PySWMM engine).

Covers:
- ``SWMMRunArgs`` validation bounds (bbox EPSG:4326 ordering, positive
  durations/intervals/resolutions, mass-balance tolerance in (0,100],
  building-representation + infiltration-method enums) and the demo defaults.
- The ``barriers`` GeoJSON FeatureCollection structural validator (tagged
  LineStrings only; barrier_type ∈ {wall, flap_gate}).
- ``SWMMDepthLayerURI`` round-trip JSON serialization and inheritance from
  ``LayerURI`` (it still maps onto map-command load-layer; the three narration
  scalars are present and bounded; the barrier geometry round-trips).
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from grace2_contracts import SWMMDepthLayerURI, SWMMRunArgs
from grace2_contracts.execution import LayerURI
from grace2_contracts.envelope import TemporalConfig
from grace2_contracts.swmm_contracts import (
    DEFAULT_MANNING_OVERLAND,
    DEFAULT_RAIN_INTERVAL_MIN,
    DEFAULT_RETURN_PERIOD_YR,
    DEFAULT_STORM_DURATION_HR,
    DEFAULT_TARGET_RESOLUTION_M,
)


# A small valid AOI bbox (lon-first EPSG:4326): Chattanooga-ish.
BBOX = (-85.32, 35.02, -85.28, 35.06)


def _barrier_fc() -> dict:
    """A minimal valid barrier FeatureCollection: one wall, one flap gate."""
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"barrier_type": "wall"},
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[-85.31, 35.03], [-85.31, 35.05]],
                },
            },
            {
                "type": "Feature",
                "properties": {"barrier_type": "flap_gate"},
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[-85.30, 35.04], [-85.295, 35.04]],
                },
            },
        ],
    }


# --------------------------------------------------------------------------- #
# SWMMRunArgs — defaults
# --------------------------------------------------------------------------- #


def test_swmm_run_args_minimal_applies_demo_defaults() -> None:
    args = SWMMRunArgs(bbox=BBOX)
    assert args.return_period_yr == DEFAULT_RETURN_PERIOD_YR == 100
    assert args.storm_duration_hr == DEFAULT_STORM_DURATION_HR == 6.0
    assert args.rain_interval_min == DEFAULT_RAIN_INTERVAL_MIN == 5
    assert args.target_resolution_m == DEFAULT_TARGET_RESOLUTION_M == 10.0
    assert args.manning_overland == DEFAULT_MANNING_OVERLAND == 0.03
    assert args.mass_balance_tolerance_pct == 5.0
    assert args.schema_version == "v1"
    # building representation defaults to "drop" (matches the screenshot),
    # never silently anything else.
    assert args.building_representation == "drop"
    assert args.infiltration_method == "none"
    assert args.total_rain_depth_mm is None
    assert args.barriers is None


def test_swmm_run_args_explicit_overrides() -> None:
    args = SWMMRunArgs(
        bbox=BBOX,
        return_period_yr=25,
        total_rain_depth_mm=120.0,
        storm_duration_hr=3.0,
        rain_interval_min=10,
        building_representation="raise",
        infiltration_method="green_ampt",
        target_resolution_m=5.0,
        manning_overland=0.04,
        mass_balance_tolerance_pct=10.0,
    )
    assert args.total_rain_depth_mm == 120.0
    assert args.building_representation == "raise"
    assert args.infiltration_method == "green_ampt"
    assert args.target_resolution_m == 5.0
    assert args.mass_balance_tolerance_pct == 10.0


# --------------------------------------------------------------------------- #
# SWMMRunArgs — validation bounds
# --------------------------------------------------------------------------- #


def test_bbox_ordering_enforced() -> None:
    # minLon > maxLon -> rejected by the shared BBox validator.
    with pytest.raises(ValidationError):
        SWMMRunArgs(bbox=(-85.28, 35.02, -85.32, 35.06))


@pytest.mark.parametrize("dur", [0.0, -1.0])
def test_storm_duration_must_be_positive(dur: float) -> None:
    with pytest.raises(ValidationError):
        SWMMRunArgs(bbox=BBOX, storm_duration_hr=dur)


@pytest.mark.parametrize("interval", [0, -5])
def test_rain_interval_must_be_positive(interval: int) -> None:
    with pytest.raises(ValidationError):
        SWMMRunArgs(bbox=BBOX, rain_interval_min=interval)


@pytest.mark.parametrize("depth", [0.0, -10.0])
def test_total_rain_depth_must_be_positive_when_set(depth: float) -> None:
    with pytest.raises(ValidationError):
        SWMMRunArgs(bbox=BBOX, total_rain_depth_mm=depth)


@pytest.mark.parametrize("res", [0.0, -10.0])
def test_target_resolution_must_be_positive(res: float) -> None:
    with pytest.raises(ValidationError):
        SWMMRunArgs(bbox=BBOX, target_resolution_m=res)


@pytest.mark.parametrize("n", [0.0, -0.03])
def test_manning_must_be_positive(n: float) -> None:
    with pytest.raises(ValidationError):
        SWMMRunArgs(bbox=BBOX, manning_overland=n)


@pytest.mark.parametrize("tol", [0.0, -1.0, 100.01, 200.0])
def test_mass_balance_tolerance_in_0_100(tol: float) -> None:
    """The honesty gate tolerance is a percent in (0, 100]."""
    with pytest.raises(ValidationError):
        SWMMRunArgs(bbox=BBOX, mass_balance_tolerance_pct=tol)


def test_mass_balance_tolerance_100_allowed() -> None:
    args = SWMMRunArgs(bbox=BBOX, mass_balance_tolerance_pct=100.0)
    assert args.mass_balance_tolerance_pct == 100.0


def test_building_representation_enum_rejects_unknown() -> None:
    with pytest.raises(ValidationError):
        SWMMRunArgs(bbox=BBOX, building_representation="explode")  # type: ignore[arg-type]


def test_building_representation_default_unset_is_drop() -> None:
    """Leaving building_representation UNSET yields the canonical default 'drop'
    (the param is NOT required)."""
    args = SWMMRunArgs(bbox=BBOX)
    assert args.building_representation == "drop"


@pytest.mark.parametrize(
    ("alias", "canonical"),
    [
        # The LLM-invented "BUILDING OBSTRUCTIONS" synonyms -> "drop".
        ("obstacles", "drop"),
        ("obstacle", "drop"),
        ("obstruction", "drop"),
        ("obstructions", "drop"),
        ("holes", "drop"),
        ("remove", "drop"),
        # case/whitespace insensitivity + the canonical value itself.
        ("  Obstacles ", "drop"),
        ("DROP", "drop"),
        # dam/wall/block -> "raise".
        ("block", "raise"),
        ("dam", "raise"),
        ("wall", "raise"),
        # friction/manning -> "roughness".
        ("friction", "roughness"),
        ("manning", "roughness"),
    ],
)
def test_building_representation_aliases_normalize(alias: str, canonical: str) -> None:
    """A common synonym (e.g. the LLM-invented 'obstacles') normalizes to the
    canonical value on the FIRST attempt — no self-correcting retry loop."""
    args = SWMMRunArgs(bbox=BBOX, building_representation=alias)  # type: ignore[arg-type]
    assert args.building_representation == canonical


def test_building_representation_unknown_string_still_raises() -> None:
    """A genuinely-bogus value passes through the alias map unchanged and still
    raises the honest Literal error (no silent coercion to a default)."""
    with pytest.raises(ValidationError):
        SWMMRunArgs(bbox=BBOX, building_representation="bananas")  # type: ignore[arg-type]


def test_infiltration_method_enum_rejects_unknown() -> None:
    with pytest.raises(ValidationError):
        SWMMRunArgs(bbox=BBOX, infiltration_method="magic")  # type: ignore[arg-type]


def test_swmm_run_args_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        SWMMRunArgs(bbox=BBOX, dispersivity_m=10.0)  # type: ignore[call-arg]


# --------------------------------------------------------------------------- #
# SWMMRunArgs.barriers — GeoJSON structural validator
# --------------------------------------------------------------------------- #


def test_barriers_valid_feature_collection_accepted() -> None:
    args = SWMMRunArgs(bbox=BBOX, barriers=_barrier_fc())
    assert args.barriers is not None
    assert len(args.barriers["features"]) == 2
    tags = {f["properties"]["barrier_type"] for f in args.barriers["features"]}
    assert tags == {"wall", "flap_gate"}


def test_barriers_must_be_feature_collection() -> None:
    bad = {"type": "Feature", "geometry": {"type": "LineString", "coordinates": []}}
    with pytest.raises(ValidationError):
        SWMMRunArgs(bbox=BBOX, barriers=bad)


def test_barriers_features_must_be_linestrings() -> None:
    fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"barrier_type": "wall"},
                "geometry": {"type": "Point", "coordinates": [-85.31, 35.03]},
            }
        ],
    }
    with pytest.raises(ValidationError):
        SWMMRunArgs(bbox=BBOX, barriers=fc)


def test_barriers_linestring_needs_two_positions() -> None:
    fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"barrier_type": "wall"},
                "geometry": {"type": "LineString", "coordinates": [[-85.31, 35.03]]},
            }
        ],
    }
    with pytest.raises(ValidationError):
        SWMMRunArgs(bbox=BBOX, barriers=fc)


def test_barriers_barrier_type_must_be_tagged() -> None:
    fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"barrier_type": "fence"},  # not a valid tag
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[-85.31, 35.03], [-85.31, 35.05]],
                },
            }
        ],
    }
    with pytest.raises(ValidationError):
        SWMMRunArgs(bbox=BBOX, barriers=fc)


def test_barriers_missing_barrier_type_rejected() -> None:
    fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {},  # no barrier_type
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[-85.31, 35.03], [-85.31, 35.05]],
                },
            }
        ],
    }
    with pytest.raises(ValidationError):
        SWMMRunArgs(bbox=BBOX, barriers=fc)


def test_barriers_empty_collection_allowed() -> None:
    """A FeatureCollection with zero features is structurally valid (no walls)."""
    args = SWMMRunArgs(bbox=BBOX, barriers={"type": "FeatureCollection", "features": []})
    assert args.barriers == {"type": "FeatureCollection", "features": []}


# --------------------------------------------------------------------------- #
# SWMMRunArgs — round-trip
# --------------------------------------------------------------------------- #


def test_swmm_run_args_roundtrip() -> None:
    args = SWMMRunArgs(
        bbox=BBOX,
        return_period_yr=50,
        storm_duration_hr=6.0,
        rain_interval_min=5,
        building_representation="drop",
        infiltration_method="scs_cn",
        barriers=_barrier_fc(),
    )
    a = args.model_dump(mode="json")
    text_a = json.dumps(a, sort_keys=True)
    b = SWMMRunArgs.model_validate(json.loads(text_a)).model_dump(mode="json")
    assert text_a == json.dumps(b, sort_keys=True)
    # bbox tuple round-trips through a JSON list back to a tuple
    assert a["bbox"] == list(BBOX)
    assert SWMMRunArgs.model_validate(json.loads(text_a)).bbox == BBOX
    # barriers survive the round-trip intact
    assert b["barriers"]["features"][0]["properties"]["barrier_type"] == "wall"


# --------------------------------------------------------------------------- #
# SWMMDepthLayerURI — inheritance + scalars + barriers + round-trip
# --------------------------------------------------------------------------- #


def _depth_layer(**overrides: object) -> SWMMDepthLayerURI:
    base = dict(
        layer_id="run-01HX-depth",
        name="Urban flood depth (m)",
        layer_type="raster",
        uri="gs://grace-2/runs/01HX/depth.cog.tif",
        style_preset="flood_depth",
        max_depth_m=1.85,
        flooded_area_km2=0.42,
        n_buildings_affected=37,
    )
    base.update(overrides)
    return SWMMDepthLayerURI(**base)  # type: ignore[arg-type]


def test_depth_layer_is_a_layer_uri() -> None:
    layer = _depth_layer()
    assert isinstance(layer, LayerURI)
    assert layer.layer_id == "run-01HX-depth"
    assert layer.layer_type == "raster"
    assert layer.role == "primary"  # inherited default
    assert layer.temporal is None  # inherited default
    assert layer.barriers is None


def test_depth_layer_narration_scalars_present_and_added() -> None:
    layer = _depth_layer()
    dumped = layer.model_dump(mode="json")
    assert dumped["max_depth_m"] == 1.85
    assert dumped["flooded_area_km2"] == 0.42
    assert dumped["n_buildings_affected"] == 37
    # The three scalars are added by the subclass, not on the base LayerURI.
    for f in ("max_depth_m", "flooded_area_km2", "n_buildings_affected"):
        assert f not in LayerURI.model_fields
        assert f in SWMMDepthLayerURI.model_fields


@pytest.mark.parametrize("depth", [-0.1, -1.0])
def test_max_depth_must_be_non_negative(depth: float) -> None:
    with pytest.raises(ValidationError):
        _depth_layer(max_depth_m=depth)


@pytest.mark.parametrize("area", [-0.1, -5.0])
def test_flooded_area_must_be_non_negative(area: float) -> None:
    with pytest.raises(ValidationError):
        _depth_layer(flooded_area_km2=area)


@pytest.mark.parametrize("n", [-1, -10])
def test_n_buildings_must_be_non_negative(n: int) -> None:
    with pytest.raises(ValidationError):
        _depth_layer(n_buildings_affected=n)


def test_depth_layer_zero_scalars_allowed() -> None:
    layer = _depth_layer(max_depth_m=0.0, flooded_area_km2=0.0, n_buildings_affected=0)
    assert layer.max_depth_m == 0.0
    assert layer.n_buildings_affected == 0


def test_depth_layer_requires_the_added_scalars() -> None:
    with pytest.raises(ValidationError):
        SWMMDepthLayerURI(
            layer_id="run-01HX-depth",
            name="depth",
            layer_type="raster",
            uri="gs://grace-2/runs/01HX/depth.cog.tif",
            style_preset="flood_depth",
            # missing the three scalars
        )


def test_depth_layer_carries_and_validates_barriers() -> None:
    layer = _depth_layer(barriers=_barrier_fc())
    assert layer.barriers is not None
    assert len(layer.barriers["features"]) == 2
    # invalid barrier geometry is rejected on the layer too
    with pytest.raises(ValidationError):
        _depth_layer(barriers={"type": "Feature"})


def test_depth_layer_inherits_temporal_for_animation() -> None:
    """The time-stepped depth animation rides the inherited ``temporal`` field."""
    layer = _depth_layer(
        temporal=TemporalConfig(
            start="2026-06-01T00:00:00Z",
            end="2026-06-01T06:00:00Z",
            step_seconds=300,
        ),
        units="m",
    )
    assert layer.temporal is not None
    assert layer.temporal.step_seconds == 300
    assert layer.units == "m"


def test_depth_layer_roundtrip() -> None:
    layer = _depth_layer(
        temporal=TemporalConfig(
            start="2026-06-01T00:00:00Z",
            end="2026-06-01T06:00:00Z",
            step_seconds=300,
        ),
        bbox=BBOX,
        units="m",
        barriers=_barrier_fc(),
    )
    a = layer.model_dump(mode="json")
    text_a = json.dumps(a, sort_keys=True)
    b = SWMMDepthLayerURI.model_validate(json.loads(text_a)).model_dump(mode="json")
    assert text_a == json.dumps(b, sort_keys=True)
    assert b["max_depth_m"] == 1.85
    assert b["barriers"]["features"][1]["properties"]["barrier_type"] == "flap_gate"


def test_depth_layer_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        _depth_layer(some_unknown_field=1.0)
