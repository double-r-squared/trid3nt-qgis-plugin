"""Validation + round-trip tests for MODFLOW groundwater contracts (sprint-13
Stage 1, §2.3 MODFLOW integration / OQ-9 mf6-gwt).

Covers:
- ``MODFLOWRunArgs`` validation bounds (positive rates/durations, porosity
  0-1, lat/lon ranges, contaminant non-empty) and TENTATIVE OQ-3 defaults.
- ``PlumeLayerURI`` round-trip JSON serialization and inheritance from
  ``LayerURI`` (it still maps onto map-command load-layer; the two plume
  scalars are present and bounded >= 0).
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from trid3nt_contracts import (
    ASRLayerURI,
    BudgetPartitionLayerURI,
    CaptureZoneLayerURI,
    DewaterLayerURI,
    DrawdownLayerURI,
    HydroperiodLayerURI,
    MODFLOWRunArgs,
    MoundingLayerURI,
    MultiSpeciesPlumeResult,
    PlumeLayerURI,
    SaltwaterWedgeLayerURI,
    SpeciesSpec,
)
from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.envelope import TemporalConfig
from trid3nt_contracts.modflow_contracts import (
    DEFAULT_AQUIFER_K_MS,
    DEFAULT_AQUIFER_SS,
    DEFAULT_AQUIFER_SY,
    DEFAULT_POROSITY,
    DEFAULT_WETLAND_SY,
)


# --------------------------------------------------------------------------- #
# MODFLOWRunArgs - defaults (OQ-3 TENTATIVE demo parameterization)
# --------------------------------------------------------------------------- #


def test_modflow_run_args_minimal_applies_oq3_defaults() -> None:
    """K and porosity default to the TENTATIVE OQ-3 demo values."""
    args = MODFLOWRunArgs(
        spill_location_latlon=(26.6, -81.9),  # Fort-Myers-ish (lat, lon)
        contaminant="benzene",
        release_rate_kg_s=0.5,
        duration_days=3.0,
    )
    assert args.aquifer_k_ms == DEFAULT_AQUIFER_K_MS == 1e-4
    assert args.porosity == DEFAULT_POROSITY == 0.3
    assert args.schema_version == "v2"
    # River-coupling fields default off -> the deck stays the pure-spill deck.
    assert args.river_geometry_uri is None
    assert args.along_river_source is False


def test_modflow_run_args_explicit_overrides_defaults() -> None:
    args = MODFLOWRunArgs(
        spill_location_latlon=(40.0, -100.0),
        contaminant="TCE",
        release_rate_kg_s=1.0,
        duration_days=10.0,
        aquifer_k_ms=5e-5,
        porosity=0.25,
    )
    assert args.aquifer_k_ms == 5e-5
    assert args.porosity == 0.25


# --------------------------------------------------------------------------- #
# MODFLOWRunArgs - validation bounds
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("rate", [0.0, -1.0, -0.001])
def test_release_rate_must_be_positive(rate: float) -> None:
    with pytest.raises(ValidationError):
        MODFLOWRunArgs(
            spill_location_latlon=(26.6, -81.9),
            contaminant="benzene",
            release_rate_kg_s=rate,
            duration_days=3.0,
        )


@pytest.mark.parametrize("duration", [0.0, -1.0])
def test_duration_must_be_positive(duration: float) -> None:
    with pytest.raises(ValidationError):
        MODFLOWRunArgs(
            spill_location_latlon=(26.6, -81.9),
            contaminant="benzene",
            release_rate_kg_s=0.5,
            duration_days=duration,
        )


@pytest.mark.parametrize("k", [0.0, -1e-4])
def test_aquifer_k_must_be_positive(k: float) -> None:
    with pytest.raises(ValidationError):
        MODFLOWRunArgs(
            spill_location_latlon=(26.6, -81.9),
            contaminant="benzene",
            release_rate_kg_s=0.5,
            duration_days=3.0,
            aquifer_k_ms=k,
        )


@pytest.mark.parametrize("porosity", [0.0, -0.1, 1.01, 2.0])
def test_porosity_must_be_in_0_1_interval(porosity: float) -> None:
    """Porosity is dimensionless in (0, 1]; 0 and >1 are rejected."""
    with pytest.raises(ValidationError):
        MODFLOWRunArgs(
            spill_location_latlon=(26.6, -81.9),
            contaminant="benzene",
            release_rate_kg_s=0.5,
            duration_days=3.0,
            porosity=porosity,
        )


def test_porosity_boundary_one_is_allowed() -> None:
    """porosity == 1.0 is valid (le bound); 0.0 is not (gt bound)."""
    args = MODFLOWRunArgs(
        spill_location_latlon=(26.6, -81.9),
        contaminant="benzene",
        release_rate_kg_s=0.5,
        duration_days=3.0,
        porosity=1.0,
    )
    assert args.porosity == 1.0


def test_contaminant_must_be_non_empty() -> None:
    with pytest.raises(ValidationError):
        MODFLOWRunArgs(
            spill_location_latlon=(26.6, -81.9),
            contaminant="",
            release_rate_kg_s=0.5,
            duration_days=3.0,
        )


@pytest.mark.parametrize(
    "latlon",
    [
        (91.0, -81.9),  # lat > 90
        (-91.0, -81.9),  # lat < -90
        (26.6, 181.0),  # lon > 180
        (26.6, -181.0),  # lon < -180
    ],
)
def test_spill_location_latlon_range_validated(latlon: tuple[float, float]) -> None:
    with pytest.raises(ValidationError):
        MODFLOWRunArgs(
            spill_location_latlon=latlon,
            contaminant="benzene",
            release_rate_kg_s=0.5,
            duration_days=3.0,
        )


def test_spill_location_latlon_order_is_lat_then_lon() -> None:
    """A swapped (lon, lat) pair like (-81.9, 26.6) is fine numerically here
    (both in range), but a clearly-lon-first value like (-81.9, 200.0) is
    rejected because the second slot is the longitude and 200 is out of range.

    This documents the (lat, lon) contract: the FIRST slot is bounded [-90, 90].
    """
    # Valid (lat, lon)
    ok = MODFLOWRunArgs(
        spill_location_latlon=(26.6, -81.9),
        contaminant="benzene",
        release_rate_kg_s=0.5,
        duration_days=3.0,
    )
    assert ok.spill_location_latlon == (26.6, -81.9)
    # First slot (lat) bounded to [-90, 90]: 100 is invalid as a latitude
    with pytest.raises(ValidationError):
        MODFLOWRunArgs(
            spill_location_latlon=(100.0, -81.9),
            contaminant="benzene",
            release_rate_kg_s=0.5,
            duration_days=3.0,
        )


def test_modflow_run_args_forbids_extra_fields() -> None:
    """GraceModel extra='forbid' - an unknown field is a defect."""
    with pytest.raises(ValidationError):
        MODFLOWRunArgs(
            spill_location_latlon=(26.6, -81.9),
            contaminant="benzene",
            release_rate_kg_s=0.5,
            duration_days=3.0,
            dispersivity_m=10.0,  # not a field
        )


def test_modflow_run_args_roundtrip() -> None:
    args = MODFLOWRunArgs(
        spill_location_latlon=(26.6, -81.9),
        contaminant="benzene",
        release_rate_kg_s=0.5,
        duration_days=3.0,
        aquifer_k_ms=2e-4,
        porosity=0.35,
    )
    a = args.model_dump(mode="json")
    text_a = json.dumps(a, sort_keys=True)
    b = MODFLOWRunArgs.model_validate(json.loads(text_a)).model_dump(mode="json")
    assert text_a == json.dumps(b, sort_keys=True)
    # tuple serializes to a JSON list and round-trips back to a tuple
    assert a["spill_location_latlon"] == [26.6, -81.9]
    assert (
        MODFLOWRunArgs.model_validate(json.loads(text_a)).spill_location_latlon
        == (26.6, -81.9)
    )


# --------------------------------------------------------------------------- #
# PlumeLayerURI - inheritance + round-trip
# --------------------------------------------------------------------------- #


def _plume(**overrides: object) -> PlumeLayerURI:
    base = dict(
        layer_id="run-01HX-plume",
        name="Benzene plume (mg/L)",
        layer_type="raster",
        uri="gs://trid3nt/runs/01HX/plume.cog.tif",
        style_preset="plume_concentration",
        max_concentration_mgl=12.5,
        plume_area_km2=3.2,
    )
    base.update(overrides)
    return PlumeLayerURI(**base)  # type: ignore[arg-type]


def test_plume_layer_uri_is_a_layer_uri() -> None:
    """PlumeLayerURI extends LayerURI - it is substitutable as a LayerURI."""
    plume = _plume()
    assert isinstance(plume, LayerURI)
    # Inherited base fields are present and behave identically.
    assert plume.layer_id == "run-01HX-plume"
    assert plume.layer_type == "raster"
    assert plume.role == "primary"  # inherited default
    assert plume.temporal is None  # inherited default


def test_plume_layer_uri_inherits_temporal_and_bbox() -> None:
    plume = _plume(
        temporal=TemporalConfig(
            start="2026-06-01T00:00:00Z",
            end="2026-06-04T00:00:00Z",
            step_seconds=86400,
        ),
        bbox=(-82.0, 26.4, -81.7, 26.8),
        units="mg/L",
    )
    assert plume.temporal is not None
    assert plume.temporal.step_seconds == 86400
    assert plume.bbox == (-82.0, 26.4, -81.7, 26.8)
    assert plume.units == "mg/L"


def test_plume_scalars_present_in_dump_and_are_added_fields() -> None:
    plume = _plume()
    dumped = plume.model_dump(mode="json")
    assert dumped["max_concentration_mgl"] == 12.5
    assert dumped["plume_area_km2"] == 3.2
    # Confirm the two scalars are NOT on the base LayerURI (added by subclass).
    assert "max_concentration_mgl" not in LayerURI.model_fields
    assert "plume_area_km2" not in LayerURI.model_fields
    assert "max_concentration_mgl" in PlumeLayerURI.model_fields
    assert "plume_area_km2" in PlumeLayerURI.model_fields


@pytest.mark.parametrize("conc", [-0.1, -1.0])
def test_max_concentration_must_be_non_negative(conc: float) -> None:
    with pytest.raises(ValidationError):
        _plume(max_concentration_mgl=conc)


@pytest.mark.parametrize("area", [-0.1, -5.0])
def test_plume_area_must_be_non_negative(area: float) -> None:
    with pytest.raises(ValidationError):
        _plume(plume_area_km2=area)


def test_plume_zero_scalars_allowed() -> None:
    """A plume with zero concentration/area (e.g. below detection) is valid."""
    plume = _plume(max_concentration_mgl=0.0, plume_area_km2=0.0)
    assert plume.max_concentration_mgl == 0.0
    assert plume.plume_area_km2 == 0.0


def test_plume_layer_uri_roundtrip() -> None:
    plume = _plume(
        temporal=TemporalConfig(
            start="2026-06-01T00:00:00Z",
            end="2026-06-04T00:00:00Z",
            step_seconds=86400,
        ),
        bbox=(-82.0, 26.4, -81.7, 26.8),
        units="mg/L",
        role="primary",
    )
    a = plume.model_dump(mode="json")
    text_a = json.dumps(a, sort_keys=True)
    b = PlumeLayerURI.model_validate(json.loads(text_a)).model_dump(mode="json")
    assert text_a == json.dumps(b, sort_keys=True)


def test_plume_layer_uri_requires_the_added_scalars() -> None:
    """The two plume scalars are required (no defaults) - a plume without them
    is incomplete."""
    with pytest.raises(ValidationError):
        PlumeLayerURI(
            layer_id="run-01HX-plume",
            name="Plume",
            layer_type="raster",
            uri="gs://trid3nt/runs/01HX/plume.cog.tif",
            style_preset="plume_concentration",
            # missing max_concentration_mgl + plume_area_km2
        )


def test_plume_layer_uri_forbids_extra_fields() -> None:
    """Inherited GraceModel extra='forbid' still applies on the subclass."""
    with pytest.raises(ValidationError):
        _plume(some_unknown_field=1.0)


# --------------------------------------------------------------------------- #
# sprint-18 Wave-1: archetype run-args fields + new LayerURI subclasses
# (ADDITIVE / DEFAULTED - the existing spill/seepage path stays byte-identical)
# --------------------------------------------------------------------------- #


def _spill_args(**overrides: object) -> MODFLOWRunArgs:
    """The minimal EXISTING spill run-args (no archetype) as the additive base."""
    base: dict[str, object] = dict(
        spill_location_latlon=(26.6, -81.9),
        contaminant="benzene",
        release_rate_kg_s=0.5,
        duration_days=3.0,
    )
    base.update(overrides)
    return MODFLOWRunArgs(**base)  # type: ignore[arg-type]


def test_additive_safety_no_new_fields_still_validates() -> None:
    """A run-args with NONE of the sprint-18 archetype fields validates, all the
    new fields default off, and schema_version is unchanged (additive growth)."""
    args = _spill_args()
    # archetype selector defaults to None -> existing spill/seepage path.
    assert args.archetype is None
    # sustainable_yield fields default off (storage uses the demo SY/SS defaults).
    assert args.well_location_latlon is None
    assert args.pumping_rate_m3_day is None
    assert args.aquifer_sy == DEFAULT_AQUIFER_SY == 0.2
    assert args.aquifer_ss == DEFAULT_AQUIFER_SS == 1e-5
    assert args.sim_years is None
    assert args.n_periods is None
    # mine_dewatering fields default off.
    assert args.pit_footprint_lonlat is None
    assert args.drain_elevation_m is None
    assert args.drain_conductance_m2_day is None
    assert args.well_pumping_rate_m3_day is None
    # regional_water_budget field defaults off.
    assert args.zone_partition is None
    # --- sprint-18 Wave-2 archetype fields all default off ---
    # MAR (managed aquifer recharge) fields.
    assert args.basin_footprint_lonlat is None
    assert args.infiltration_rate_m_day is None
    assert args.recharge_months is None
    # ASR (aquifer storage & recovery) fields.
    assert args.injection_rate_m3_day is None
    assert args.recovery_rate_m3_day is None
    assert args.injection_months is None
    assert args.recovery_months is None
    assert args.n_cycles is None
    # wetland_hydroperiod fields (specific_yield uses the demo default).
    assert args.wetland_footprint_lonlat is None
    assert args.recharge_schedule_m_day is None
    assert args.et_surface_m is None
    assert args.et_max_rate_m_day is None
    assert args.et_extinction_depth_m is None
    assert args.specific_yield == DEFAULT_WETLAND_SY == 0.2
    # schema_version UNCHANGED by the additive growth.
    assert args.schema_version == "v2"


def test_schema_version_unchanged_after_additive_fields() -> None:
    """The contract version pin stays v2 (no schema_version bump for additive)."""
    assert MODFLOWRunArgs.model_fields["schema_version"].default == "v2"


def test_sustainable_yield_archetype_roundtrip() -> None:
    args = _spill_args(
        archetype="sustainable_yield",
        well_location_latlon=(40.0, -100.0),
        pumping_rate_m3_day=-2000.0,  # extraction (WEL negative)
        aquifer_sy=0.15,
        aquifer_ss=2e-5,
        sim_years=10.0,
        n_periods=12,
    )
    assert args.archetype == "sustainable_yield"
    assert args.well_location_latlon == (40.0, -100.0)
    assert args.pumping_rate_m3_day == -2000.0
    assert args.aquifer_sy == 0.15
    assert args.aquifer_ss == 2e-5
    assert args.sim_years == 10.0
    assert args.n_periods == 12
    a = args.model_dump(mode="json")
    text_a = json.dumps(a, sort_keys=True)
    b = MODFLOWRunArgs.model_validate(json.loads(text_a)).model_dump(mode="json")
    assert text_a == json.dumps(b, sort_keys=True)
    # the well-location tuple round-trips through a JSON list back to a tuple.
    assert a["well_location_latlon"] == [40.0, -100.0]
    assert (
        MODFLOWRunArgs.model_validate(json.loads(text_a)).well_location_latlon
        == (40.0, -100.0)
    )


def test_well_location_latlon_range_validated() -> None:
    """The pumping-well location honors the (lat, lon) range contract."""
    with pytest.raises(ValidationError):
        _spill_args(archetype="sustainable_yield", well_location_latlon=(100.0, -100.0))
    with pytest.raises(ValidationError):
        _spill_args(archetype="sustainable_yield", well_location_latlon=(40.0, 200.0))


@pytest.mark.parametrize("sy", [0.0, -0.1, 1.5])
def test_aquifer_sy_bounds(sy: float) -> None:
    """Specific yield is in (0, 1]; 0 and >1 are rejected."""
    with pytest.raises(ValidationError):
        _spill_args(aquifer_sy=sy)


@pytest.mark.parametrize("ss", [0.0, -1e-6])
def test_aquifer_ss_must_be_positive(ss: float) -> None:
    with pytest.raises(ValidationError):
        _spill_args(aquifer_ss=ss)


@pytest.mark.parametrize("n", [0, -1])
def test_n_periods_must_be_at_least_one(n: int) -> None:
    with pytest.raises(ValidationError):
        _spill_args(n_periods=n)


def test_mine_dewatering_archetype_roundtrip() -> None:
    args = _spill_args(
        archetype="mine_dewatering",
        pit_footprint_lonlat=[(-100.0, 40.0), (-100.0, 40.1), (-99.9, 40.1)],
        drain_elevation_m=12.5,
        drain_conductance_m2_day=500.0,
        well_pumping_rate_m3_day=-300.0,
    )
    assert args.archetype == "mine_dewatering"
    assert args.pit_footprint_lonlat == [
        (-100.0, 40.0),
        (-100.0, 40.1),
        (-99.9, 40.1),
    ]
    assert args.drain_elevation_m == 12.5
    assert args.drain_conductance_m2_day == 500.0
    assert args.well_pumping_rate_m3_day == -300.0
    a = args.model_dump(mode="json")
    text_a = json.dumps(a, sort_keys=True)
    b = MODFLOWRunArgs.model_validate(json.loads(text_a)).model_dump(mode="json")
    assert text_a == json.dumps(b, sort_keys=True)
    # list-of-tuples round-trips to list-of-lists in JSON and back to tuples.
    assert a["pit_footprint_lonlat"] == [[-100.0, 40.0], [-100.0, 40.1], [-99.9, 40.1]]
    assert MODFLOWRunArgs.model_validate(json.loads(text_a)).pit_footprint_lonlat == [
        (-100.0, 40.0),
        (-100.0, 40.1),
        (-99.9, 40.1),
    ]


def test_drain_conductance_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        _spill_args(archetype="mine_dewatering", drain_conductance_m2_day=0.0)


def test_regional_water_budget_archetype_roundtrip() -> None:
    args = _spill_args(
        archetype="regional_water_budget",
        zone_partition="upgradient_downgradient",
    )
    assert args.archetype == "regional_water_budget"
    assert args.zone_partition == "upgradient_downgradient"
    a = args.model_dump(mode="json")
    text_a = json.dumps(a, sort_keys=True)
    b = MODFLOWRunArgs.model_validate(json.loads(text_a)).model_dump(mode="json")
    assert text_a == json.dumps(b, sort_keys=True)


def test_unknown_archetype_rejected_by_literal() -> None:
    with pytest.raises(ValidationError):
        _spill_args(archetype="not_an_archetype")


# --------------------------------------------------------------------------- #
# sprint-18 Wave-2: MAR / ASR / wetland_hydroperiod run-args fields
# (ADDITIVE / DEFAULTED - Wave-1 + spill/seepage paths stay byte-identical)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("archetype", ["MAR", "ASR", "wetland_hydroperiod"])
def test_wave2_archetypes_accepted_by_literal(archetype: str) -> None:
    """The three Wave-2 archetype literals validate (additive on the Wave-1 set)."""
    args = _spill_args(archetype=archetype)
    assert args.archetype == archetype


def test_mar_archetype_roundtrip() -> None:
    args = _spill_args(
        archetype="MAR",
        basin_footprint_lonlat=[(-100.0, 40.0), (-100.0, 40.1), (-99.9, 40.1)],
        infiltration_rate_m_day=0.5,
        recharge_months=6,
        n_periods=6,
    )
    assert args.archetype == "MAR"
    assert args.basin_footprint_lonlat == [
        (-100.0, 40.0),
        (-100.0, 40.1),
        (-99.9, 40.1),
    ]
    assert args.infiltration_rate_m_day == 0.5
    assert args.recharge_months == 6
    assert args.n_periods == 6
    a = args.model_dump(mode="json")
    text_a = json.dumps(a, sort_keys=True)
    b = MODFLOWRunArgs.model_validate(json.loads(text_a)).model_dump(mode="json")
    assert text_a == json.dumps(b, sort_keys=True)
    # list-of-tuples round-trips to list-of-lists in JSON and back to tuples.
    assert a["basin_footprint_lonlat"] == [[-100.0, 40.0], [-100.0, 40.1], [-99.9, 40.1]]
    assert MODFLOWRunArgs.model_validate(json.loads(text_a)).basin_footprint_lonlat == [
        (-100.0, 40.0),
        (-100.0, 40.1),
        (-99.9, 40.1),
    ]


def test_mar_infiltration_rate_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        _spill_args(archetype="MAR", infiltration_rate_m_day=0.0)


def test_mar_recharge_months_must_be_at_least_one() -> None:
    with pytest.raises(ValidationError):
        _spill_args(archetype="MAR", recharge_months=0)


def test_asr_archetype_roundtrip() -> None:
    args = _spill_args(
        archetype="ASR",
        well_location_latlon=(40.0, -100.0),  # reused from sustainable_yield
        injection_rate_m3_day=1500.0,
        recovery_rate_m3_day=1200.0,
        injection_months=6,
        recovery_months=4,
        n_cycles=3,
    )
    assert args.archetype == "ASR"
    assert args.well_location_latlon == (40.0, -100.0)
    assert args.injection_rate_m3_day == 1500.0
    assert args.recovery_rate_m3_day == 1200.0
    assert args.injection_months == 6
    assert args.recovery_months == 4
    assert args.n_cycles == 3
    a = args.model_dump(mode="json")
    text_a = json.dumps(a, sort_keys=True)
    b = MODFLOWRunArgs.model_validate(json.loads(text_a)).model_dump(mode="json")
    assert text_a == json.dumps(b, sort_keys=True)
    # the reused well-location tuple round-trips through a JSON list back to a tuple.
    assert a["well_location_latlon"] == [40.0, -100.0]
    assert (
        MODFLOWRunArgs.model_validate(json.loads(text_a)).well_location_latlon
        == (40.0, -100.0)
    )


@pytest.mark.parametrize("rate", [0.0, -1.0])
def test_asr_injection_rate_must_be_positive(rate: float) -> None:
    """ASR injection rate is a POSITIVE magnitude (the adapter applies the sign)."""
    with pytest.raises(ValidationError):
        _spill_args(archetype="ASR", injection_rate_m3_day=rate)


@pytest.mark.parametrize("rate", [0.0, -1.0])
def test_asr_recovery_rate_must_be_positive(rate: float) -> None:
    with pytest.raises(ValidationError):
        _spill_args(archetype="ASR", recovery_rate_m3_day=rate)


@pytest.mark.parametrize("n", [0, -1])
def test_asr_n_cycles_must_be_at_least_one(n: int) -> None:
    with pytest.raises(ValidationError):
        _spill_args(archetype="ASR", n_cycles=n)


def test_wetland_hydroperiod_archetype_roundtrip() -> None:
    args = _spill_args(
        archetype="wetland_hydroperiod",
        wetland_footprint_lonlat=[(-81.0, 26.0), (-81.0, 26.1), (-80.9, 26.1)],
        recharge_schedule_m_day=[0.01, 0.005, 0.0, 0.002],
        et_surface_m=2.0,
        et_max_rate_m_day=0.004,
        et_extinction_depth_m=1.5,
        specific_yield=0.18,
    )
    assert args.archetype == "wetland_hydroperiod"
    assert args.wetland_footprint_lonlat == [
        (-81.0, 26.0),
        (-81.0, 26.1),
        (-80.9, 26.1),
    ]
    assert args.recharge_schedule_m_day == [0.01, 0.005, 0.0, 0.002]
    assert args.et_surface_m == 2.0
    assert args.et_max_rate_m_day == 0.004
    assert args.et_extinction_depth_m == 1.5
    assert args.specific_yield == 0.18
    a = args.model_dump(mode="json")
    text_a = json.dumps(a, sort_keys=True)
    b = MODFLOWRunArgs.model_validate(json.loads(text_a)).model_dump(mode="json")
    assert text_a == json.dumps(b, sort_keys=True)
    assert a["recharge_schedule_m_day"] == [0.01, 0.005, 0.0, 0.002]


@pytest.mark.parametrize("sy", [0.0, -0.1, 1.5])
def test_wetland_specific_yield_bounds(sy: float) -> None:
    """Wetland specific yield is in (0, 1]; 0 and >1 are rejected."""
    with pytest.raises(ValidationError):
        _spill_args(archetype="wetland_hydroperiod", specific_yield=sy)


def test_wetland_specific_yield_defaults_to_demo_value() -> None:
    args = _spill_args(archetype="wetland_hydroperiod")
    assert args.specific_yield == DEFAULT_WETLAND_SY == 0.2


@pytest.mark.parametrize(
    "field,value",
    [
        ("et_max_rate_m_day", 0.0),
        ("et_max_rate_m_day", -0.1),
        ("et_extinction_depth_m", 0.0),
        ("et_extinction_depth_m", -1.0),
    ],
)
def test_wetland_et_params_must_be_positive(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        _spill_args(archetype="wetland_hydroperiod", **{field: value})


# --------------------------------------------------------------------------- #
# DrawdownLayerURI / DewaterLayerURI / BudgetPartitionLayerURI
# --------------------------------------------------------------------------- #


def _drawdown(**overrides: object) -> DrawdownLayerURI:
    base: dict[str, object] = dict(
        layer_id="run-01HX-drawdown",
        name="Pumping drawdown (m)",
        layer_type="raster",
        uri="s3://trid3nt/runs/01HX/drawdown.cog.tif",
        style_preset="continuous_drawdown_m",
        max_drawdown_m=4.2,
    )
    base.update(overrides)
    return DrawdownLayerURI(**base)  # type: ignore[arg-type]


def test_drawdown_layer_uri_is_a_layer_uri_and_roundtrips() -> None:
    layer = _drawdown(
        head_decline_timeseries=[0.0, 1.1, 2.4, 3.7, 4.2],
        units="meters",
        bbox=(-100.2, 39.9, -99.8, 40.3),
    )
    assert isinstance(layer, LayerURI)
    assert layer.max_drawdown_m == 4.2
    assert layer.head_decline_timeseries == [0.0, 1.1, 2.4, 3.7, 4.2]
    a = layer.model_dump(mode="json")
    text_a = json.dumps(a, sort_keys=True)
    b = DrawdownLayerURI.model_validate(json.loads(text_a)).model_dump(mode="json")
    assert text_a == json.dumps(b, sort_keys=True)


def test_drawdown_timeseries_optional_and_scalar_added() -> None:
    layer = _drawdown()
    assert layer.head_decline_timeseries is None  # optional, defaults None
    assert "max_drawdown_m" not in LayerURI.model_fields
    assert "max_drawdown_m" in DrawdownLayerURI.model_fields


@pytest.mark.parametrize("dd", [-0.1, -5.0])
def test_max_drawdown_must_be_non_negative(dd: float) -> None:
    with pytest.raises(ValidationError):
        _drawdown(max_drawdown_m=dd)


def _dewater(**overrides: object) -> DewaterLayerURI:
    base: dict[str, object] = dict(
        layer_id="run-01HX-dewater",
        name="Mine dewatering rate (m^3/day)",
        layer_type="raster",
        uri="s3://trid3nt/runs/01HX/dewater.cog.tif",
        style_preset="continuous_dewatering_rate",
        dewatering_rate_m3_day=18500.0,
        drain_cell_count=42,
    )
    base.update(overrides)
    return DewaterLayerURI(**base)  # type: ignore[arg-type]


def test_dewater_layer_uri_is_a_layer_uri_and_roundtrips() -> None:
    layer = _dewater(units="m^3/day")
    assert isinstance(layer, LayerURI)
    assert layer.dewatering_rate_m3_day == 18500.0
    assert layer.drain_cell_count == 42
    a = layer.model_dump(mode="json")
    text_a = json.dumps(a, sort_keys=True)
    b = DewaterLayerURI.model_validate(json.loads(text_a)).model_dump(mode="json")
    assert text_a == json.dumps(b, sort_keys=True)
    assert "dewatering_rate_m3_day" not in LayerURI.model_fields
    assert "dewatering_rate_m3_day" in DewaterLayerURI.model_fields


@pytest.mark.parametrize("rate", [-0.1, -100.0])
def test_dewatering_rate_must_be_non_negative(rate: float) -> None:
    with pytest.raises(ValidationError):
        _dewater(dewatering_rate_m3_day=rate)


def test_drain_cell_count_must_be_non_negative() -> None:
    with pytest.raises(ValidationError):
        _dewater(drain_cell_count=-1)


def _budget(**overrides: object) -> BudgetPartitionLayerURI:
    base: dict[str, object] = dict(
        layer_id="run-01HX-budget",
        name="Regional water budget partition",
        layer_type="vector",
        uri="s3://trid3nt/runs/01HX/budget.fgb",
        style_preset="continuous_head_m",
        budget_partition_m3_day={
            "upgradient_chd_in": 1200.0,
            "downgradient_chd_out": -1180.0,
            "storage": -20.0,
        },
    )
    base.update(overrides)
    return BudgetPartitionLayerURI(**base)  # type: ignore[arg-type]


def test_budget_partition_layer_uri_is_a_layer_uri_and_roundtrips() -> None:
    layer = _budget(units="m^3/day")
    assert isinstance(layer, LayerURI)
    assert layer.budget_partition_m3_day["upgradient_chd_in"] == 1200.0
    assert layer.budget_partition_m3_day["downgradient_chd_out"] == -1180.0
    a = layer.model_dump(mode="json")
    text_a = json.dumps(a, sort_keys=True)
    b = BudgetPartitionLayerURI.model_validate(json.loads(text_a)).model_dump(
        mode="json"
    )
    assert text_a == json.dumps(b, sort_keys=True)
    assert "budget_partition_m3_day" not in LayerURI.model_fields
    assert "budget_partition_m3_day" in BudgetPartitionLayerURI.model_fields


def test_budget_partition_required_and_extra_forbidden() -> None:
    # the partition dict is required (no default).
    with pytest.raises(ValidationError):
        BudgetPartitionLayerURI(
            layer_id="run-01HX-budget",
            name="Budget",
            layer_type="vector",
            uri="s3://trid3nt/runs/01HX/budget.fgb",
            style_preset="continuous_head_m",
            # missing budget_partition_m3_day
        )
    # inherited GraceModel extra='forbid' still applies.
    with pytest.raises(ValidationError):
        _budget(some_unknown_field=1.0)


# --------------------------------------------------------------------------- #
# Output-quantity registry: the three new modflow quantities are registered.
# --------------------------------------------------------------------------- #


def test_new_modflow_output_quantities_registered() -> None:
    """drawdown / dewatering-rate / budget-partition are registered + default-on."""
    from trid3nt_contracts.output_quantities import get_output_registry

    registry = get_output_registry("modflow")
    by_id = {spec.quantity_id: spec for spec in registry}
    for qid in ("drawdown", "dewatering-rate", "budget-partition"):
        assert qid in by_id, f"missing modflow output quantity {qid!r}"
        assert by_id[qid].default_on is True
    # the new quantities are ADDITIVE: the existing headline quantities still exist.
    assert "plume-concentration" in by_id
    assert "river-seepage" in by_id


# --------------------------------------------------------------------------- #
# sprint-18 Wave-2: MoundingLayerURI / ASRLayerURI / HydroperiodLayerURI
# --------------------------------------------------------------------------- #


def _mounding(**overrides: object) -> MoundingLayerURI:
    base: dict[str, object] = dict(
        layer_id="run-01HX-mounding",
        name="Recharge mounding (m)",
        layer_type="raster",
        uri="s3://trid3nt/runs/01HX/mounding.cog.tif",
        style_preset="continuous_mounding_m",
        max_mounding_m=3.4,
    )
    base.update(overrides)
    return MoundingLayerURI(**base)  # type: ignore[arg-type]


def test_mounding_layer_uri_is_a_layer_uri_and_roundtrips() -> None:
    layer = _mounding(recharged_volume_m3=125000.0, units="meters")
    assert isinstance(layer, LayerURI)
    assert layer.max_mounding_m == 3.4
    assert layer.recharged_volume_m3 == 125000.0
    a = layer.model_dump(mode="json")
    text_a = json.dumps(a, sort_keys=True)
    b = MoundingLayerURI.model_validate(json.loads(text_a)).model_dump(mode="json")
    assert text_a == json.dumps(b, sort_keys=True)
    assert "max_mounding_m" not in LayerURI.model_fields
    assert "max_mounding_m" in MoundingLayerURI.model_fields


def test_mounding_recharged_volume_optional_defaults_none() -> None:
    layer = _mounding()
    assert layer.recharged_volume_m3 is None  # optional, defaults None


@pytest.mark.parametrize("m", [-0.1, -5.0])
def test_max_mounding_must_be_non_negative(m: float) -> None:
    with pytest.raises(ValidationError):
        _mounding(max_mounding_m=m)


def test_recharged_volume_must_be_non_negative() -> None:
    with pytest.raises(ValidationError):
        _mounding(recharged_volume_m3=-1.0)


def _asr(**overrides: object) -> ASRLayerURI:
    base: dict[str, object] = dict(
        layer_id="run-01HX-asr",
        name="ASR well head (m)",
        layer_type="raster",
        uri="s3://trid3nt/runs/01HX/asr.cog.tif",
        style_preset="continuous_head_m",
    )
    base.update(overrides)
    return ASRLayerURI(**base)  # type: ignore[arg-type]


def test_asr_layer_uri_is_a_layer_uri_and_roundtrips() -> None:
    layer = _asr(
        recovery_efficiency=0.82,
        head_timeseries=[10.0, 14.0, 11.0, 15.0, 12.0],
        units="meters",
    )
    assert isinstance(layer, LayerURI)
    assert layer.recovery_efficiency == 0.82
    assert layer.head_timeseries == [10.0, 14.0, 11.0, 15.0, 12.0]
    a = layer.model_dump(mode="json")
    text_a = json.dumps(a, sort_keys=True)
    b = ASRLayerURI.model_validate(json.loads(text_a)).model_dump(mode="json")
    assert text_a == json.dumps(b, sort_keys=True)
    assert "recovery_efficiency" not in LayerURI.model_fields
    assert "recovery_efficiency" in ASRLayerURI.model_fields


def test_asr_scalars_optional_default_none() -> None:
    layer = _asr()
    assert layer.recovery_efficiency is None
    assert layer.head_timeseries is None


@pytest.mark.parametrize("eff", [-0.1, 1.5])
def test_asr_recovery_efficiency_bounds(eff: float) -> None:
    """Recovery efficiency is a fraction in [0, 1]; <0 and >1 are rejected."""
    with pytest.raises(ValidationError):
        _asr(recovery_efficiency=eff)


def test_asr_recovery_efficiency_boundaries_allowed() -> None:
    assert _asr(recovery_efficiency=0.0).recovery_efficiency == 0.0
    assert _asr(recovery_efficiency=1.0).recovery_efficiency == 1.0


def _hydroperiod(**overrides: object) -> HydroperiodLayerURI:
    base: dict[str, object] = dict(
        layer_id="run-01HX-hydroperiod",
        name="Wetland hydroperiod (m)",
        layer_type="raster",
        uri="s3://trid3nt/runs/01HX/hydroperiod.cog.tif",
        style_preset="continuous_hydroperiod_m",
        seasonal_head_range_m=1.2,
    )
    base.update(overrides)
    return HydroperiodLayerURI(**base)  # type: ignore[arg-type]


def test_hydroperiod_layer_uri_is_a_layer_uri_and_roundtrips() -> None:
    layer = _hydroperiod(
        head_timeseries=[1.0, 1.6, 2.2, 1.4, 1.0],
        units="meters",
    )
    assert isinstance(layer, LayerURI)
    assert layer.seasonal_head_range_m == 1.2
    assert layer.head_timeseries == [1.0, 1.6, 2.2, 1.4, 1.0]
    a = layer.model_dump(mode="json")
    text_a = json.dumps(a, sort_keys=True)
    b = HydroperiodLayerURI.model_validate(json.loads(text_a)).model_dump(mode="json")
    assert text_a == json.dumps(b, sort_keys=True)
    assert "seasonal_head_range_m" not in LayerURI.model_fields
    assert "seasonal_head_range_m" in HydroperiodLayerURI.model_fields


def test_hydroperiod_timeseries_optional_defaults_none() -> None:
    layer = _hydroperiod()
    assert layer.head_timeseries is None


@pytest.mark.parametrize("r", [-0.1, -2.0])
def test_seasonal_head_range_must_be_non_negative(r: float) -> None:
    with pytest.raises(ValidationError):
        _hydroperiod(seasonal_head_range_m=r)


def test_wave2_layer_uris_require_their_added_scalar_and_forbid_extra() -> None:
    """The required Wave-2 scalars (max_mounding_m / seasonal_head_range_m) have no
    default; inherited GraceModel extra='forbid' still applies on every subclass."""
    with pytest.raises(ValidationError):
        MoundingLayerURI(
            layer_id="run-01HX-mounding",
            name="Mounding",
            layer_type="raster",
            uri="s3://trid3nt/runs/01HX/mounding.cog.tif",
            style_preset="continuous_mounding_m",
            # missing max_mounding_m
        )
    with pytest.raises(ValidationError):
        HydroperiodLayerURI(
            layer_id="run-01HX-hydroperiod",
            name="Hydroperiod",
            layer_type="raster",
            uri="s3://trid3nt/runs/01HX/hydroperiod.cog.tif",
            style_preset="continuous_hydroperiod_m",
            # missing seasonal_head_range_m
        )
    with pytest.raises(ValidationError):
        _mounding(some_unknown_field=1.0)
    with pytest.raises(ValidationError):
        _asr(some_unknown_field=1.0)
    with pytest.raises(ValidationError):
        _hydroperiod(some_unknown_field=1.0)


def test_wave2_modflow_output_quantities_registered() -> None:
    """mounding / recovery-efficiency / hydroperiod are registered + default-on,
    additive on top of the Wave-1 + headline quantities."""
    from trid3nt_contracts.output_quantities import get_output_registry

    registry = get_output_registry("modflow")
    by_id = {spec.quantity_id: spec for spec in registry}
    for qid in ("mounding", "recovery-efficiency", "hydroperiod"):
        assert qid in by_id, f"missing modflow output quantity {qid!r}"
        assert by_id[qid].default_on is True
    # ADDITIVE: the Wave-1 + headline quantities still exist.
    assert "drawdown" in by_id
    assert "dewatering-rate" in by_id
    assert "budget-partition" in by_id
    assert "plume-concentration" in by_id
    assert "river-seepage" in by_id


# --------------------------------------------------------------------------- #
# sprint-18 Wave-3: multi_species transport - SpeciesSpec + species list +
# MultiSpeciesPlumeResult (ADDITIVE / DEFAULTED - the single-contaminant spill
# path is byte-identical when ``species is None``).
# --------------------------------------------------------------------------- #


def test_multi_species_archetype_accepted_by_literal() -> None:
    """The ``multi_species`` archetype literal validates (additive on Wave-1/2)."""
    args = _spill_args(
        archetype="multi_species",
        species=[SpeciesSpec(name="TCE", release_rate_kg_s=0.5)],
    )
    assert args.archetype == "multi_species"


def test_species_spec_minimal_optional_fields_default_none() -> None:
    """A SpeciesSpec needs only name + release_rate; physics fields default None."""
    sp = SpeciesSpec(name="TCE", release_rate_kg_s=0.5)
    assert sp.name == "TCE"
    assert sp.release_rate_kg_s == 0.5
    assert sp.sorption_kd is None
    assert sp.decay_per_day is None
    assert sp.parent is None


def test_species_spec_full_decay_chain_fields() -> None:
    """A daughter species carries sorption Kd + decay + a parent reference."""
    sp = SpeciesSpec(
        name="cis-DCE",
        release_rate_kg_s=0.0,  # pure daughter (produced only by decay)
        sorption_kd=0.0002,
        decay_per_day=0.01,
        parent="TCE",
    )
    assert sp.parent == "TCE"
    assert sp.sorption_kd == 0.0002
    assert sp.decay_per_day == 0.01


def test_species_list_roundtrip_on_run_args() -> None:
    """A TCE -> cis-DCE -> VC decay chain round-trips through JSON on the run-args."""
    args = _spill_args(
        archetype="multi_species",
        species=[
            SpeciesSpec(name="TCE", release_rate_kg_s=0.5, decay_per_day=0.02),
            SpeciesSpec(
                name="cis-DCE",
                release_rate_kg_s=0.0,
                decay_per_day=0.01,
                parent="TCE",
            ),
            SpeciesSpec(
                name="VC",
                release_rate_kg_s=0.0,
                sorption_kd=0.0001,
                parent="cis-DCE",
            ),
        ],
    )
    assert args.species is not None
    assert [s.name for s in args.species] == ["TCE", "cis-DCE", "VC"]
    assert args.species[1].parent == "TCE"
    assert args.species[2].parent == "cis-DCE"
    a = args.model_dump(mode="json")
    text_a = json.dumps(a, sort_keys=True)
    b = MODFLOWRunArgs.model_validate(json.loads(text_a)).model_dump(mode="json")
    assert text_a == json.dumps(b, sort_keys=True)
    # the species list survives the JSON round-trip back into SpeciesSpec models.
    rehydrated = MODFLOWRunArgs.model_validate(json.loads(text_a))
    assert rehydrated.species is not None
    assert isinstance(rehydrated.species[0], SpeciesSpec)
    assert rehydrated.species[1].parent == "TCE"
    assert rehydrated.species[2].sorption_kd == 0.0001


def test_additive_safety_no_species_is_valid_single_contaminant() -> None:
    """No species (archetype None) is the EXISTING single-contaminant path: the
    top-level contaminant/release_rate are used and ``species`` defaults None."""
    args = _spill_args()
    assert args.species is None
    assert args.archetype is None
    # the single-contaminant scalars are untouched.
    assert args.contaminant == "benzene"
    assert args.release_rate_kg_s == 0.5
    # schema_version UNCHANGED by the additive growth.
    assert args.schema_version == "v2"
    # species defaults absent from a minimal dump path only via None (additive).
    assert MODFLOWRunArgs.model_fields["species"].default is None


@pytest.mark.parametrize("rate", [-0.1, -1.0])
def test_species_release_rate_must_be_non_negative(rate: float) -> None:
    """A species release rate is >= 0 (0 allowed for a pure daughter product)."""
    with pytest.raises(ValidationError):
        SpeciesSpec(name="TCE", release_rate_kg_s=rate)


def test_species_release_rate_zero_allowed_for_daughter() -> None:
    """release_rate 0.0 is valid (a daughter produced only by parent decay)."""
    sp = SpeciesSpec(name="VC", release_rate_kg_s=0.0, parent="cis-DCE")
    assert sp.release_rate_kg_s == 0.0


@pytest.mark.parametrize("kd", [-0.1, -1.0])
def test_species_sorption_kd_must_be_non_negative(kd: float) -> None:
    with pytest.raises(ValidationError):
        SpeciesSpec(name="TCE", release_rate_kg_s=0.5, sorption_kd=kd)


@pytest.mark.parametrize("decay", [-0.1, -1.0])
def test_species_decay_must_be_non_negative(decay: float) -> None:
    with pytest.raises(ValidationError):
        SpeciesSpec(name="TCE", release_rate_kg_s=0.5, decay_per_day=decay)


def test_species_name_must_be_non_empty() -> None:
    with pytest.raises(ValidationError):
        SpeciesSpec(name="", release_rate_kg_s=0.5)


def test_species_spec_forbids_unknown_fields() -> None:
    """SpeciesSpec is a GraceModel (extra='forbid')."""
    with pytest.raises(ValidationError):
        SpeciesSpec(name="TCE", release_rate_kg_s=0.5, some_unknown_field=1.0)


def test_multi_species_plume_result_reuses_plume_layer_uri() -> None:
    """MultiSpeciesPlumeResult carries N PlumeLayerURI (one per species), no new
    LayerURI type, and round-trips through JSON."""
    result = MultiSpeciesPlumeResult(
        plumes=[
            _plume(layer_id="run-01HX-plume-TCE", name="TCE plume (mg/L)"),
            _plume(
                layer_id="run-01HX-plume-DCE",
                name="cis-DCE plume (mg/L)",
                max_concentration_mgl=3.1,
                plume_area_km2=1.0,
            ),
        ]
    )
    assert len(result.plumes) == 2
    assert all(isinstance(p, PlumeLayerURI) for p in result.plumes)
    assert all(isinstance(p, LayerURI) for p in result.plumes)  # still a LayerURI
    a = result.model_dump(mode="json")
    text_a = json.dumps(a, sort_keys=True)
    b = MultiSpeciesPlumeResult.model_validate(
        json.loads(text_a)
    ).model_dump(mode="json")
    assert text_a == json.dumps(b, sort_keys=True)
    # the per-species plume narration scalars survive.
    assert a["plumes"][1]["max_concentration_mgl"] == 3.1
    assert a["plumes"][1]["plume_area_km2"] == 1.0


def test_multi_species_plume_result_requires_at_least_one_plume() -> None:
    """An empty plume list is rejected (a multi_species run has >= 1 plume)."""
    with pytest.raises(ValidationError):
        MultiSpeciesPlumeResult(plumes=[])


def test_unknown_archetype_still_rejected_with_multi_species_added() -> None:
    """Adding multi_species does not open the literal to arbitrary values."""
    with pytest.raises(ValidationError):
        _spill_args(archetype="not_an_archetype")


# --------------------------------------------------------------------------- #
# sprint-18 Wave-4: capture_zone + wellhead_protection via MF6 PRT backward
# particle tracking (ADDITIVE / DEFAULTED - all prior paths byte-identical)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("archetype", ["capture_zone", "wellhead_protection"])
def test_wave4_archetypes_accepted_by_literal(archetype: str) -> None:
    """The two Wave-4 PRT archetype literals validate (additive on Wave-1/2/3)."""
    args = _spill_args(archetype=archetype)
    assert args.archetype == archetype


def test_prt_fields_default_off_on_spill_path() -> None:
    """All Wave-4 PRT fields default off; the existing spill/seepage path is
    byte-identical when none of them are set (additive growth guarantee)."""
    args = _spill_args()
    assert args.capture_zone_travel_time_years is None
    assert args.n_particles == 16
    assert args.prt_max_tracking_years is None


def test_capture_zone_archetype_roundtrip() -> None:
    """capture_zone with explicit PRT fields round-trips through JSON."""
    args = _spill_args(
        archetype="capture_zone",
        well_location_latlon=(40.0, -100.0),
        capture_zone_travel_time_years=[1.0, 5.0, 10.0],
        n_particles=32,
        prt_max_tracking_years=15.0,
    )
    assert args.archetype == "capture_zone"
    assert args.well_location_latlon == (40.0, -100.0)
    assert args.capture_zone_travel_time_years == [1.0, 5.0, 10.0]
    assert args.n_particles == 32
    assert args.prt_max_tracking_years == 15.0
    a = args.model_dump(mode="json")
    text_a = json.dumps(a, sort_keys=True)
    b = MODFLOWRunArgs.model_validate(json.loads(text_a)).model_dump(mode="json")
    assert text_a == json.dumps(b, sort_keys=True)
    assert a["capture_zone_travel_time_years"] == [1.0, 5.0, 10.0]


def test_wellhead_protection_archetype_roundtrip() -> None:
    """wellhead_protection with EPA-style tiers round-trips through JSON."""
    args = _spill_args(
        archetype="wellhead_protection",
        well_location_latlon=(26.5, -81.7),
        capture_zone_travel_time_years=[2.0, 5.0, 10.0],
        n_particles=16,
    )
    assert args.archetype == "wellhead_protection"
    assert args.capture_zone_travel_time_years == [2.0, 5.0, 10.0]
    a = args.model_dump(mode="json")
    text_a = json.dumps(a, sort_keys=True)
    b = MODFLOWRunArgs.model_validate(json.loads(text_a)).model_dump(mode="json")
    assert text_a == json.dumps(b, sort_keys=True)


@pytest.mark.parametrize("n", [3, 0, -1])
def test_n_particles_below_minimum_rejected(n: int) -> None:
    """n_particles must be >= 4; values below the bound are rejected."""
    with pytest.raises(ValidationError):
        _spill_args(archetype="capture_zone", n_particles=n)


def test_n_particles_above_maximum_rejected() -> None:
    """n_particles must be <= 256; values above the bound are rejected."""
    with pytest.raises(ValidationError):
        _spill_args(archetype="capture_zone", n_particles=257)


def test_n_particles_boundary_values_allowed() -> None:
    """n_particles boundary values 4 and 256 are valid."""
    assert _spill_args(archetype="capture_zone", n_particles=4).n_particles == 4
    assert _spill_args(archetype="capture_zone", n_particles=256).n_particles == 256


def test_prt_max_tracking_years_must_be_positive() -> None:
    """prt_max_tracking_years must be > 0 when supplied."""
    with pytest.raises(ValidationError):
        _spill_args(archetype="capture_zone", prt_max_tracking_years=0.0)
    with pytest.raises(ValidationError):
        _spill_args(archetype="capture_zone", prt_max_tracking_years=-1.0)


def test_wave4_unknown_archetype_still_rejected() -> None:
    """Adding capture_zone/wellhead_protection does not open the literal."""
    with pytest.raises(ValidationError):
        _spill_args(archetype="zone_of_contribution")


# --------------------------------------------------------------------------- #
# CaptureZoneLayerURI - the first vector MODFLOW LayerURI (Wave-4)
# --------------------------------------------------------------------------- #


def _capture_zone(**overrides: object) -> CaptureZoneLayerURI:
    base: dict[str, object] = dict(
        layer_id="run-01HX-capture-zone",
        name="Capture zone - 1/5/10-year isochrones",
        layer_type="vector",
        uri="s3://trid3nt/runs/01HX/capture_zone.fgb",
        style_preset="capture_zone",
        capture_zone_area_km2=1.4,
        travel_time_years=[1.0, 5.0, 10.0],
        isochrone_areas_km2={"1": 0.05, "5": 0.35, "10": 1.4},
        particle_count=16,
    )
    base.update(overrides)
    return CaptureZoneLayerURI(**base)  # type: ignore[arg-type]


def test_capture_zone_layer_uri_is_a_layer_uri() -> None:
    """CaptureZoneLayerURI extends LayerURI - substitutable as a LayerURI."""
    layer = _capture_zone()
    assert isinstance(layer, LayerURI)
    assert layer.layer_id == "run-01HX-capture-zone"
    assert layer.layer_type == "vector"
    assert layer.role == "primary"  # inherited default
    assert layer.temporal is None  # inherited default


def test_capture_zone_layer_type_defaults_to_vector() -> None:
    """layer_type defaults to 'vector' (NOT raster - the first vector MODFLOW layer)."""
    # Explicitly set to vector (consistent with default).
    layer = _capture_zone()
    assert layer.layer_type == "vector"
    # Also verify the class-level default field is 'vector'.
    assert CaptureZoneLayerURI.model_fields["layer_type"].default == "vector"


def test_capture_zone_layer_uri_roundtrips() -> None:
    """CaptureZoneLayerURI round-trips through JSON serialization."""
    layer = _capture_zone(
        bbox=(-100.2, 39.8, -99.7, 40.3),
        units="km^2",
    )
    a = layer.model_dump(mode="json")
    text_a = json.dumps(a, sort_keys=True)
    b = CaptureZoneLayerURI.model_validate(json.loads(text_a)).model_dump(mode="json")
    assert text_a == json.dumps(b, sort_keys=True)
    # The isochrone dict survives the round-trip.
    assert a["isochrone_areas_km2"] == {"1": 0.05, "5": 0.35, "10": 1.4}
    assert a["travel_time_years"] == [1.0, 5.0, 10.0]
    assert a["particle_count"] == 16


def test_capture_zone_added_fields_not_on_base_layer_uri() -> None:
    """The four added scalars are on CaptureZoneLayerURI, not on the base LayerURI."""
    for field in (
        "capture_zone_area_km2",
        "travel_time_years",
        "isochrone_areas_km2",
        "particle_count",
    ):
        assert field not in LayerURI.model_fields, f"{field!r} should not be on LayerURI"
        assert field in CaptureZoneLayerURI.model_fields, f"{field!r} missing from CaptureZoneLayerURI"


def test_capture_zone_area_must_be_non_negative() -> None:
    """capture_zone_area_km2 is >= 0; negative values are rejected."""
    with pytest.raises(ValidationError):
        _capture_zone(capture_zone_area_km2=-0.1)


def test_capture_zone_particle_count_must_be_non_negative() -> None:
    """particle_count is >= 0; a negative count is rejected."""
    with pytest.raises(ValidationError):
        _capture_zone(particle_count=-1)


def test_capture_zone_travel_time_years_requires_at_least_one_tier() -> None:
    """travel_time_years must have at least one tier (min_length=1)."""
    with pytest.raises(ValidationError):
        _capture_zone(travel_time_years=[])


def test_capture_zone_isochrone_areas_km2_dict_structure() -> None:
    """isochrone_areas_km2 accepts an arbitrary string-keyed float dict."""
    layer = _capture_zone(
        isochrone_areas_km2={"2": 0.12, "5": 0.44, "10": 1.8},
        travel_time_years=[2.0, 5.0, 10.0],
    )
    assert layer.isochrone_areas_km2["5"] == 0.44
    a = layer.model_dump(mode="json")
    assert a["isochrone_areas_km2"] == {"2": 0.12, "5": 0.44, "10": 1.8}


def test_capture_zone_requires_all_added_scalars() -> None:
    """All four added scalars are required (no defaults for the key ones)."""
    # Missing capture_zone_area_km2.
    with pytest.raises(ValidationError):
        CaptureZoneLayerURI(
            layer_id="run-01HX-capture-zone",
            name="Capture zone",
            layer_type="vector",
            uri="s3://trid3nt/runs/01HX/capture_zone.fgb",
            style_preset="capture_zone",
            travel_time_years=[1.0, 5.0],
            isochrone_areas_km2={"1": 0.05, "5": 0.35},
            particle_count=16,
            # missing capture_zone_area_km2
        )
    # Missing travel_time_years.
    with pytest.raises(ValidationError):
        CaptureZoneLayerURI(
            layer_id="run-01HX-capture-zone",
            name="Capture zone",
            layer_type="vector",
            uri="s3://trid3nt/runs/01HX/capture_zone.fgb",
            style_preset="capture_zone",
            capture_zone_area_km2=1.4,
            isochrone_areas_km2={"1": 0.05, "5": 0.35},
            particle_count=16,
            # missing travel_time_years
        )


def test_capture_zone_forbids_extra_fields() -> None:
    """Inherited GraceModel extra='forbid' still applies on CaptureZoneLayerURI."""
    with pytest.raises(ValidationError):
        _capture_zone(some_unknown_field=1.0)


def test_capture_zone_exported_from_package_top_level() -> None:
    """CaptureZoneLayerURI is importable from the trid3nt_contracts top level."""
    from trid3nt_contracts import CaptureZoneLayerURI as CZL  # noqa: F401

    assert CZL is CaptureZoneLayerURI


# --------------------------------------------------------------------------- #
# sprint-18 Wave-5: saltwater_intrusion - MODFLOWRunArgs fields +
# SaltwaterWedgeLayerURI (ADDITIVE / DEFAULTED - all prior paths byte-identical)
# --------------------------------------------------------------------------- #


def test_wave5_saltwater_intrusion_archetype_accepted_by_literal() -> None:
    """The saltwater_intrusion archetype literal validates (additive on Wave-1/2/3/4)."""
    args = _spill_args(archetype="saltwater_intrusion")
    assert args.archetype == "saltwater_intrusion"


def test_saltwater_intrusion_fields_default_off_on_spill_path() -> None:
    """All Wave-5 saltwater_intrusion fields default off; the existing spill/seepage
    path is byte-identical when none of them are set (additive growth guarantee)."""
    args = _spill_args()
    assert args.coastal_transect_latlon is None
    assert args.seawater_salinity_ppt == 35.0
    assert args.n_vertical_layers == 20
    assert args.freshwater_inflow_m3_day is None


def test_saltwater_intrusion_archetype_roundtrip() -> None:
    """saltwater_intrusion with explicit fields round-trips through JSON."""
    args = _spill_args(
        archetype="saltwater_intrusion",
        coastal_transect_latlon=((25.7, -80.2), (25.7, -80.1)),
        seawater_salinity_ppt=35.0,
        n_vertical_layers=20,
        freshwater_inflow_m3_day=500.0,
    )
    assert args.archetype == "saltwater_intrusion"
    assert args.coastal_transect_latlon == ((25.7, -80.2), (25.7, -80.1))
    assert args.seawater_salinity_ppt == 35.0
    assert args.n_vertical_layers == 20
    assert args.freshwater_inflow_m3_day == 500.0
    a = args.model_dump(mode="json")
    text_a = json.dumps(a, sort_keys=True)
    b = MODFLOWRunArgs.model_validate(json.loads(text_a)).model_dump(mode="json")
    assert text_a == json.dumps(b, sort_keys=True)
    # nested tuple of tuples round-trips to nested list-of-lists in JSON and back
    assert a["coastal_transect_latlon"] == [[25.7, -80.2], [25.7, -80.1]]
    rehydrated = MODFLOWRunArgs.model_validate(json.loads(text_a))
    assert rehydrated.coastal_transect_latlon == ((25.7, -80.2), (25.7, -80.1))


def test_seawater_salinity_ppt_must_be_positive() -> None:
    """seawater_salinity_ppt must be > 0; 0 and negative values are rejected."""
    with pytest.raises(ValidationError):
        _spill_args(archetype="saltwater_intrusion", seawater_salinity_ppt=0.0)
    with pytest.raises(ValidationError):
        _spill_args(archetype="saltwater_intrusion", seawater_salinity_ppt=-5.0)


def test_n_vertical_layers_bounds() -> None:
    """n_vertical_layers must be in [4, 80]; values outside that range are rejected."""
    with pytest.raises(ValidationError):
        _spill_args(archetype="saltwater_intrusion", n_vertical_layers=3)
    with pytest.raises(ValidationError):
        _spill_args(archetype="saltwater_intrusion", n_vertical_layers=81)


def test_n_vertical_layers_boundary_values_allowed() -> None:
    """Boundary values 4 and 80 are valid for n_vertical_layers."""
    assert _spill_args(archetype="saltwater_intrusion", n_vertical_layers=4).n_vertical_layers == 4
    assert _spill_args(archetype="saltwater_intrusion", n_vertical_layers=80).n_vertical_layers == 80


def test_freshwater_inflow_m3_day_must_be_positive_when_supplied() -> None:
    """freshwater_inflow_m3_day must be > 0 when supplied; 0 is rejected."""
    with pytest.raises(ValidationError):
        _spill_args(archetype="saltwater_intrusion", freshwater_inflow_m3_day=0.0)
    with pytest.raises(ValidationError):
        _spill_args(archetype="saltwater_intrusion", freshwater_inflow_m3_day=-100.0)


def test_freshwater_inflow_m3_day_none_is_valid() -> None:
    """freshwater_inflow_m3_day=None is valid (adapter auto-derives the flux)."""
    args = _spill_args(archetype="saltwater_intrusion", freshwater_inflow_m3_day=None)
    assert args.freshwater_inflow_m3_day is None


def test_wave5_unknown_archetype_still_rejected() -> None:
    """Adding saltwater_intrusion does not open the Literal to arbitrary values."""
    with pytest.raises(ValidationError):
        _spill_args(archetype="henry_problem")


# --------------------------------------------------------------------------- #
# SaltwaterWedgeLayerURI - Wave-5 vector LayerURI
# --------------------------------------------------------------------------- #


def _saltwater_wedge(**overrides: object) -> SaltwaterWedgeLayerURI:
    base: dict[str, object] = dict(
        layer_id="run-01HX-saltwater-wedge",
        name="Saltwater wedge transect (Henry demo)",
        layer_type="vector",
        uri="s3://trid3nt/runs/01HX/saltwater_wedge.fgb",
        style_preset="saltwater_intrusion",
        intrusion_length_m=850.0,
        toe_distance_m=850.0,
        seaward_salinity_ppt=35.0,
        transect_endpoints=((25.7, -80.2), (25.7, -80.1)),
    )
    base.update(overrides)
    return SaltwaterWedgeLayerURI(**base)  # type: ignore[arg-type]


def test_saltwater_wedge_layer_uri_is_a_layer_uri() -> None:
    """SaltwaterWedgeLayerURI extends LayerURI - substitutable as a LayerURI."""
    layer = _saltwater_wedge()
    assert isinstance(layer, LayerURI)
    assert layer.layer_id == "run-01HX-saltwater-wedge"
    assert layer.layer_type == "vector"
    assert layer.role == "primary"  # inherited default
    assert layer.temporal is None  # inherited default


def test_saltwater_wedge_layer_type_defaults_to_vector() -> None:
    """layer_type defaults to 'vector' (the transect line + toe point are a vector)."""
    layer = _saltwater_wedge()
    assert layer.layer_type == "vector"
    assert SaltwaterWedgeLayerURI.model_fields["layer_type"].default == "vector"


def test_saltwater_wedge_layer_uri_roundtrips() -> None:
    """SaltwaterWedgeLayerURI round-trips through JSON serialization."""
    layer = _saltwater_wedge(
        bbox=(-80.21, 25.69, -80.09, 25.71),
        units="m",
    )
    a = layer.model_dump(mode="json")
    text_a = json.dumps(a, sort_keys=True)
    b = SaltwaterWedgeLayerURI.model_validate(json.loads(text_a)).model_dump(mode="json")
    assert text_a == json.dumps(b, sort_keys=True)
    # Nested tuple-of-tuples serializes to list-of-lists in JSON.
    assert a["transect_endpoints"] == [[25.7, -80.2], [25.7, -80.1]]
    assert a["intrusion_length_m"] == 850.0
    assert a["seaward_salinity_ppt"] == 35.0


def test_saltwater_wedge_transect_endpoints_roundtrip() -> None:
    """transect_endpoints nested tuple survives a JSON round-trip back to tuples."""
    layer = _saltwater_wedge()
    text = json.dumps(layer.model_dump(mode="json"), sort_keys=True)
    rehydrated = SaltwaterWedgeLayerURI.model_validate(json.loads(text))
    assert rehydrated.transect_endpoints == ((25.7, -80.2), (25.7, -80.1))


def test_saltwater_wedge_added_fields_not_on_base_layer_uri() -> None:
    """The four added fields are on SaltwaterWedgeLayerURI, not on the base LayerURI."""
    for field in (
        "intrusion_length_m",
        "toe_distance_m",
        "seaward_salinity_ppt",
        "transect_endpoints",
    ):
        assert field not in LayerURI.model_fields, f"{field!r} should not be on LayerURI"
        assert field in SaltwaterWedgeLayerURI.model_fields, (
            f"{field!r} missing from SaltwaterWedgeLayerURI"
        )


@pytest.mark.parametrize("length", [-0.1, -500.0])
def test_intrusion_length_must_be_non_negative(length: float) -> None:
    """intrusion_length_m is >= 0; negative values are rejected."""
    with pytest.raises(ValidationError):
        _saltwater_wedge(intrusion_length_m=length)


@pytest.mark.parametrize("dist", [-0.1, -1.0])
def test_toe_distance_must_be_non_negative(dist: float) -> None:
    """toe_distance_m is >= 0; negative values are rejected."""
    with pytest.raises(ValidationError):
        _saltwater_wedge(toe_distance_m=dist)


def test_saltwater_wedge_zero_intrusion_allowed() -> None:
    """intrusion_length_m == 0 is valid (no wedge penetration yet)."""
    layer = _saltwater_wedge(intrusion_length_m=0.0, toe_distance_m=0.0)
    assert layer.intrusion_length_m == 0.0
    assert layer.toe_distance_m == 0.0


def test_saltwater_wedge_requires_all_added_fields() -> None:
    """All four added fields are required (no defaults for the key scalars)."""
    # Missing intrusion_length_m.
    with pytest.raises(ValidationError):
        SaltwaterWedgeLayerURI(
            layer_id="run-01HX-saltwater-wedge",
            name="Saltwater wedge",
            layer_type="vector",
            uri="s3://trid3nt/runs/01HX/saltwater_wedge.fgb",
            style_preset="saltwater_intrusion",
            toe_distance_m=850.0,
            seaward_salinity_ppt=35.0,
            transect_endpoints=((25.7, -80.2), (25.7, -80.1)),
            # missing intrusion_length_m
        )
    # Missing transect_endpoints.
    with pytest.raises(ValidationError):
        SaltwaterWedgeLayerURI(
            layer_id="run-01HX-saltwater-wedge",
            name="Saltwater wedge",
            layer_type="vector",
            uri="s3://trid3nt/runs/01HX/saltwater_wedge.fgb",
            style_preset="saltwater_intrusion",
            intrusion_length_m=850.0,
            toe_distance_m=850.0,
            seaward_salinity_ppt=35.0,
            # missing transect_endpoints
        )


def test_saltwater_wedge_forbids_extra_fields() -> None:
    """Inherited GraceModel extra='forbid' still applies on SaltwaterWedgeLayerURI."""
    with pytest.raises(ValidationError):
        _saltwater_wedge(some_unknown_field=1.0)


def test_saltwater_wedge_exported_from_package_top_level() -> None:
    """SaltwaterWedgeLayerURI is importable from the trid3nt_contracts top level."""
    from trid3nt_contracts import SaltwaterWedgeLayerURI as SWL  # noqa: F401

    assert SWL is SaltwaterWedgeLayerURI
