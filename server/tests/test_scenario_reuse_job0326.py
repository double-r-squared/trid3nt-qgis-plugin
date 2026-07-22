"""job-0326: deterministic expensive-simulation reuse guard.

PROBLEM (live, NATE 2026-06-16): the agent re-ran ~10-20-minute SFINCS / MODFLOW
solves whose output layer was ALREADY on the map, and re-fetched / re-computed
layers that already existed — the F54 soft prompt steer was being ignored. These
tests cover the deterministic backstop:

  1. ``scenario_signature`` distills an expensive-scenario call into a comparable
     identity (scenario family + AOI + key physics params), and is conservative
     (no AOI we can match without geocoding -> no signature -> RUN).
  2. ``ScenarioResultIndex.find_reuse`` matches a repeat request to an existing
     result on a CLEAR match (same family + same AOI + same key params), and
     refuses on any change (AOI / return period / contaminant / etc.).
  3. The enriched ``build_layers_present_note`` carries the richer identity
     (RESULT vs INPUT, scenario family, handle, bbox) so the model can SEE that
     an existing result already answers the request.
"""

from __future__ import annotations

from trid3nt_server.adapter import build_layers_present_note
from trid3nt_server.scenario_reuse import (
    ScenarioResultIndex,
    layer_id_scenario_type,
    scenario_signature,
    scenario_type_for_tool,
)

FORT_MYERS_BBOX = [-82.0, 26.0, -81.0, 27.0]
NEW_ORLEANS_BBOX = [-90.2, 29.8, -89.9, 30.1]


# --------------------------------------------------------------------------- #
# scenario_signature — identity distillation + conservatism
# --------------------------------------------------------------------------- #


def test_flood_signature_keys_on_bbox_and_physics_params() -> None:
    sig = scenario_signature(
        "run_model_flood_scenario",
        {"bbox": FORT_MYERS_BBOX, "return_period_yr": 100, "duration_hr": 24},
    )
    assert sig is not None
    assert sig.scenario_type == "flood-depth"
    assert sig.bbox_q is not None
    assert ("return_period_yr", 100.0) in sig.key_params
    assert ("duration_hr", 24.0) in sig.key_params


def test_flood_signature_accepts_years_hours_aliases() -> None:
    # Defensive: even if normalize_args hasn't canonicalized, both alias spellings
    # land on the same key.
    a = scenario_signature(
        "run_model_flood_scenario",
        {"bbox": FORT_MYERS_BBOX, "return_period_years": 100, "duration_hours": 24},
    )
    b = scenario_signature(
        "run_model_flood_scenario",
        {"bbox": FORT_MYERS_BBOX, "return_period_yr": 100, "duration_hr": 24},
    )
    assert a is not None and b is not None
    assert a.key_params == b.key_params


def test_flood_signature_none_without_aoi() -> None:
    # No bbox AND no location_query -> nothing to match on without geocoding ->
    # conservative: no signature -> caller RUNS.
    assert (
        scenario_signature(
            "run_model_flood_scenario", {"return_period_yr": 100}
        )
        is None
    )


def test_non_scenario_tool_has_no_signature() -> None:
    assert scenario_type_for_tool("fetch_dem") is None
    assert scenario_signature("fetch_dem", {"bbox": FORT_MYERS_BBOX}) is None


def test_plume_signature_keys_on_spill_point_and_params() -> None:
    sig = scenario_signature(
        "run_modflow_job",
        {
            "spill_location_latlon": [40.81, -96.71],
            "contaminant": "benzene",
            "release_rate_kg_s": 0.5,
            "duration_days": 30,
        },
    )
    assert sig is not None
    assert sig.scenario_type == "plume"
    assert ("contaminant", "benzene") in sig.key_params
    assert ("release_rate_kg_s", 0.5) in sig.key_params
    assert ("duration_days", 30.0) in sig.key_params


def test_plume_signature_none_without_spill_point() -> None:
    assert (
        scenario_signature(
            "run_modflow_job", {"contaminant": "benzene"}
        )
        is None
    )


# --------------------------------------------------------------------------- #
# find_reuse — repeat reuses; new/changed runs
# --------------------------------------------------------------------------- #


def _record_flood(idx: ScenarioResultIndex, *, bbox, rp=100, dur=24, layer_id="flood-depth-peak-r1"):
    sig = scenario_signature(
        "run_model_flood_scenario",
        {"bbox": bbox, "return_period_yr": rp, "duration_hr": dur},
    )
    idx.record_result(
        sig,
        layer_id=layer_id,
        name="Flood Depth (peak)",
        layer_type="raster",
        uri=f"gs://runs/{layer_id}/flood.tif",
        bbox=bbox,
    )
    return sig


def test_repeat_flood_request_reuses_existing_result() -> None:
    idx = ScenarioResultIndex(session_id="s1")
    _record_flood(idx, bbox=FORT_MYERS_BBOX)
    repeat = scenario_signature(
        "run_model_flood_scenario",
        {"bbox": FORT_MYERS_BBOX, "return_period_yr": 100, "duration_hr": 24},
    )
    reuse = idx.find_reuse(repeat)
    assert reuse is not None
    assert reuse.layer_id == "flood-depth-peak-r1"
    assert reuse.scenario_type == "flood-depth"


def test_near_equal_bbox_still_reuses_within_tolerance() -> None:
    # Geocoder jitter on the same place name nudges the bbox a hair — still the
    # same AOI for reuse purposes (quantization tolerance).
    idx = ScenarioResultIndex(session_id="s1")
    _record_flood(idx, bbox=FORT_MYERS_BBOX)
    jittered = [-82.001, 26.001, -80.999, 27.001]
    repeat = scenario_signature(
        "run_model_flood_scenario",
        {"bbox": jittered, "return_period_yr": 100, "duration_hr": 24},
    )
    assert idx.find_reuse(repeat) is not None


def test_changed_return_period_runs_again() -> None:
    idx = ScenarioResultIndex(session_id="s1")
    _record_flood(idx, bbox=FORT_MYERS_BBOX, rp=100)
    changed = scenario_signature(
        "run_model_flood_scenario",
        {"bbox": FORT_MYERS_BBOX, "return_period_yr": 500, "duration_hr": 24},
    )
    assert idx.find_reuse(changed) is None


def test_different_aoi_runs_again() -> None:
    idx = ScenarioResultIndex(session_id="s1")
    _record_flood(idx, bbox=FORT_MYERS_BBOX)
    other = scenario_signature(
        "run_model_flood_scenario",
        {"bbox": NEW_ORLEANS_BBOX, "return_period_yr": 100, "duration_hr": 24},
    )
    assert idx.find_reuse(other) is None


def test_plume_repeat_reuses_and_change_runs() -> None:
    idx = ScenarioResultIndex(session_id="s1")
    sig = scenario_signature(
        "run_modflow_job",
        {
            "spill_location_latlon": [40.81, -96.71],
            "contaminant": "benzene",
            "release_rate_kg_s": 0.5,
            "duration_days": 30,
        },
    )
    idx.record_result(
        sig,
        layer_id="plume-r9",
        name="Contaminant Plume",
        layer_type="raster",
        uri="gs://runs/r9/plume.tif",
        bbox=[-96.8, 40.7, -96.6, 40.9],  # plume FOOTPRINT, not the spill point
    )
    repeat = scenario_signature(
        "run_modflow_job",
        {
            "spill_location_latlon": [40.81, -96.71],
            "contaminant": "benzene",
            "release_rate_kg_s": 0.5,
            "duration_days": 30,
        },
    )
    assert idx.find_reuse(repeat) is not None
    diff_contaminant = scenario_signature(
        "run_modflow_job",
        {
            "spill_location_latlon": [40.81, -96.71],
            "contaminant": "TCE",
            "release_rate_kg_s": 0.5,
            "duration_days": 30,
        },
    )
    assert idx.find_reuse(diff_contaminant) is None


def test_seed_from_loaded_layers_enables_bare_rerun_short_circuit() -> None:
    # A persisted flood RESULT (no signature) is reused only when the request is
    # bbox-keyed, carries NO key params (a bare "model the flood here"), the
    # request bbox matches the Case AOI, AND it is the only result of its family.
    idx = ScenarioResultIndex(session_id="s1")
    idx.seed_from_loaded_layers(
        [
            {
                "layer_id": "flood-depth-peak-old",
                "name": "Flood Depth (peak)",
                "layer_type": "raster",
                "uri": "gs://runs/old/flood.tif",
                "role": "primary",
            }
        ]
    )
    bare = scenario_signature(
        "run_model_flood_scenario", {"bbox": FORT_MYERS_BBOX}
    )
    reuse = idx.find_reuse(bare, case_bbox=FORT_MYERS_BBOX)
    assert reuse is not None
    assert reuse.layer_id == "flood-depth-peak-old"
    # A bbox-keyed request that does NOT match the Case AOI does not reuse the
    # bbox-less persisted result.
    bare_other = scenario_signature(
        "run_model_flood_scenario", {"bbox": NEW_ORLEANS_BBOX}
    )
    assert idx.find_reuse(bare_other, case_bbox=FORT_MYERS_BBOX) is None


# --------------------------------------------------------------------------- #
# layer_id classification
# --------------------------------------------------------------------------- #


def test_layer_id_scenario_type_classifies_results_vs_inputs() -> None:
    assert layer_id_scenario_type("flood-depth-peak-abc") == "flood-depth"
    assert layer_id_scenario_type("plume-r9", "Contaminant Plume") == "plume"
    assert layer_id_scenario_type("nlcd-landcover-xyz", "NLCD Landcover") is None
    assert layer_id_scenario_type("usgs-dem-123", "DEM") is None


# --------------------------------------------------------------------------- #
# enriched layers-present note (Task 2)
# --------------------------------------------------------------------------- #


def test_layers_present_note_labels_result_and_input() -> None:
    note = build_layers_present_note(
        [
            {
                "layer_id": "flood-depth-peak-abc123",
                "name": "Flood Depth (peak)",
                "layer_type": "raster",
                "uri": "gs://runs/abc/flood.tif",
                "role": "primary",
            },
            {
                "layer_id": "nlcd-landcover-xyz",
                "name": "NLCD Landcover",
                "layer_type": "raster",
                "uri": "gs://cache/nlcd.tif",
                "role": "context",
            },
        ],
        case_bbox=FORT_MYERS_BBOX,
    )
    assert note is not None
    # The flood-depth layer is tagged a RESULT of the flood-depth family...
    assert "RESULT[flood-depth]" in note
    # ...the landcover is an INPUT...
    assert "INPUT" in note
    # ...the reusable handle and uri are surfaced for both...
    assert "handle=flood-depth-peak-abc123" in note
    assert "uri=gs://cache/nlcd.tif" in note
    # ...and the forbidden-rerun directive is present.
    assert "FORBIDDEN" in note


def test_layers_present_note_includes_layer_bbox_when_present() -> None:
    note = build_layers_present_note(
        [
            {
                "layer_id": "flood-depth-peak-1",
                "name": "Flood Depth (peak)",
                "layer_type": "raster",
                "uri": "gs://runs/1/flood.tif",
                "role": "primary",
                "bbox": FORT_MYERS_BBOX,
            }
        ],
        case_bbox=None,
    )
    assert note is not None
    assert "bbox=[-82.0, 26.0, -81.0, 27.0]" in note
