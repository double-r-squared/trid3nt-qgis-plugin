"""Round-trip + negative tests for AssessmentEnvelope (Appendix B)."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from grace2_contracts.common import new_ulid
from grace2_contracts.envelope import (
    AssessmentEnvelope,
    BaseMetrics,
    CriticalFacility,
    DataSource,
    FloodMetrics,
    FloodPayload,
    ForcingSummary,
    Provenance,
    ResultLayer,
    TemporalConfig,
)


def _modeled_flood_envelope() -> AssessmentEnvelope:
    return AssessmentEnvelope(
        envelope_id=new_ulid(),
        project_id=new_ulid(),
        session_id=new_ulid(),
        envelope_type="modeled",
        hazard_type="flood",
        workflow_name="run_storm_surge_flood",
        bbox=(-82.5, 26.4, -81.7, 26.9),
        time_range={"start": "2022-09-28T00:00:00Z", "end": "2022-09-30T00:00:00Z"},
        forcing=ForcingSummary(
            forcing_type="storm_surge",
            source="NHC ATCF, Hurricane Ian",
            parameters={"storm_id": "AL092022", "intensity_scaling": 1.0},
            inputs_uri="gs://trid3nt/forcing/ian_atcf.csv",
        ),
        catalog_entries=None,
        layers=[
            ResultLayer(
                layer_id="run-01HX-flood-depth",
                name="Flood depth (m)",
                layer_type="raster",
                uri="gs://trid3nt/runs/01HX/depth.cog.tif",
                style_preset="flood_depth_blue",
                temporal=TemporalConfig(
                    start="2022-09-28T00:00:00Z",
                    end="2022-09-30T00:00:00Z",
                    step_seconds=3600,
                ),
                role="primary",
                units="meters",
            )
        ],
        metrics=BaseMetrics(),
        provenance=Provenance(
            data_sources=[
                DataSource(
                    name="USGS 3DEP",
                    uri="https://elevation.example.com/dem.tif",
                    accessed_at="2026-06-05T12:00:00Z",
                )
            ],
        ),
        created_at="2026-06-05T12:00:00Z",
        completed_at="2026-06-05T12:30:00Z",
        flood=FloodPayload(
            metrics=FloodMetrics(
                flooded_area_km2=18.4,
                max_depth_m=3.2,
                mean_depth_m=0.8,
                p95_depth_m=2.1,
                max_velocity_m_s=1.4,
                affected_buildings_count=847,
                affected_buildings_by_depth={"0-0.5m": 412, "0.5-1m": 251, "1-2m": 132, "2m+": 52},
                affected_critical_facilities=[
                    CriticalFacility(
                        name="Lee Memorial Hospital",
                        category="hospital",
                        coordinates=(-81.87, 26.65),
                        max_depth_m=0.6,
                    )
                ],
                population_exposed=12_400,
                solver_version="sfincs-v2.0.4",
                grid_resolution_m=10.0,
                simulation_duration_hours=48,
            )
        ),
    )


def test_modeled_flood_envelope_roundtrip_idempotent() -> None:
    env = _modeled_flood_envelope()
    dumped_a = env.model_dump(mode="json")
    text_a = json.dumps(dumped_a, sort_keys=True)
    env_b = AssessmentEnvelope.model_validate(json.loads(text_a))
    dumped_b = env_b.model_dump(mode="json")
    text_b = json.dumps(dumped_b, sort_keys=True)
    assert text_a == text_b


def test_envelope_type_is_a_discriminator() -> None:
    env = _modeled_flood_envelope()
    assert env.envelope_type == "modeled"
    assert env.hazard_type == "flood"


def test_wrong_subtype_for_hazard_rejected() -> None:
    """Invariant 3: exactly one subtype matching hazard_type is populated.

    A flood-typed envelope with the flood subtype absent (and wildfire populated
    instead) must fail validation.
    """
    base = _modeled_flood_envelope()
    data = base.model_dump(mode="json")
    data["flood"] = None
    data["wildfire"] = {"placeholder": True}
    with pytest.raises(ValidationError):
        AssessmentEnvelope.model_validate(data)


def test_two_subtypes_populated_rejected() -> None:
    base = _modeled_flood_envelope()
    data = base.model_dump(mode="json")
    data["groundwater"] = {"placeholder": True}
    with pytest.raises(ValidationError):
        AssessmentEnvelope.model_validate(data)


def test_no_subtype_populated_rejected() -> None:
    base = _modeled_flood_envelope()
    data = base.model_dump(mode="json")
    data["flood"] = None
    with pytest.raises(ValidationError):
        AssessmentEnvelope.model_validate(data)


def test_flood_metrics_has_no_cost_field() -> None:
    """Invariant 9 (no cost theater) and invariant 3 (hazard fields stay in subtype)."""
    fm = FloodMetrics(
        flooded_area_km2=1.0,
        max_depth_m=0.5,
        mean_depth_m=0.2,
        p95_depth_m=0.4,
        solver_version="sfincs-v2.0.4",
        grid_resolution_m=10.0,
        simulation_duration_hours=24,
    )
    dumped = fm.model_dump(mode="json")
    assert not any("cost" in k.lower() for k in dumped.keys())


def test_base_metrics_stays_hazard_agnostic_extra_forbidden() -> None:
    """A flood-specific field cannot sneak into BaseMetrics."""
    with pytest.raises(ValidationError):
        BaseMetrics.model_validate({"flooded_area_km2": 1.0})


def test_flooded_area_negative_rejected() -> None:
    with pytest.raises(ValidationError):
        FloodMetrics(
            flooded_area_km2=-1.0,
            max_depth_m=0.5,
            mean_depth_m=0.2,
            p95_depth_m=0.4,
            solver_version="sfincs-v2.0.4",
            grid_resolution_m=10.0,
            simulation_duration_hours=24,
        )


def test_grid_resolution_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        FloodMetrics(
            flooded_area_km2=1.0,
            max_depth_m=0.5,
            mean_depth_m=0.2,
            p95_depth_m=0.4,
            solver_version="sfincs-v2.0.4",
            grid_resolution_m=0.0,
            simulation_duration_hours=24,
        )


def test_result_layer_aligns_with_load_layer_args() -> None:
    """The visualization seam: ResultLayer fields map onto map-command load-layer
    args without translation (layer_id, style_preset, optional temporal)."""
    from grace2_contracts.ws import LoadLayerArgs

    rl = ResultLayer(
        layer_id="run-01HX-flood-depth",
        name="Flood depth (m)",
        layer_type="raster",
        uri="gs://trid3nt/runs/01HX/depth.cog.tif",
        style_preset="flood_depth_blue",
        temporal=TemporalConfig(
            start="2022-09-28T00:00:00Z",
            end="2022-09-30T00:00:00Z",
            step_seconds=3600,
        ),
        role="primary",
    )
    args = LoadLayerArgs(
        layer_id=rl.layer_id,
        wms_url="https://qgis.example.com/wms?MAP=01HX.qgs",
        style_preset=rl.style_preset,
        temporal={
            "start": rl.temporal.model_dump(mode="json")["start"],
            "end": rl.temporal.model_dump(mode="json")["end"],
            "step_seconds": rl.temporal.step_seconds,
        },
    )
    # No transformations required beyond plumbing the WMS URL — the visualization
    # seam holds.
    assert args.layer_id == rl.layer_id and args.style_preset == rl.style_preset
