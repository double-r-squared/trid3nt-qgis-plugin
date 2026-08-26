"""Offline unit tests for the tomawac_wave_field engine template (ADR 0236).

No solver / no network: registration shape + arg-guard rejection paths only.
The physics-through-the-image proof lives in the Dockerfile build-time smoke +
the live E2E; this is the offline-suite guard that the tool is registered as an
engine template and rejects ill-posed args before any dispatch.
"""
from __future__ import annotations

import asyncio

#: What the declared bed producer hands the deck writer: the staged raster's URI.
#: A domain solved on real bathymetry refuses without one, because the worker
#: holds no fetcher of its own any more.
_STAGED_BED = {"uri": "s3://trid3nt-cache/cache/static-30d/ncei_dem_mosaic/test.tif",
               "source": "noaa_ncei_dem_all"}


def test_tomawac_wave_field_registered_as_engine_template():
    from trid3nt_server.tools import TOOL_REGISTRY
    entry = TOOL_REGISTRY.get("tomawac_wave_field")
    assert entry is not None, "tomawac_wave_field must be registered"
    m = entry.metadata
    assert m.engine == "telemac" and m.tier == "template"
    assert m.cacheable is False and m.ttl_class == "live-no-cache"
    specs = {r.param for r in (m.resolution_specs or ())}
    assert "target_resolution_m" in specs


def test_tomawac_solver_registered():
    from trid3nt_server.workflows.solver.solver import (
        LOCAL_SOLVER_SPEC_REGISTRY,
        SOLVER_WORKFLOW_REGISTRY,
    )
    assert "tomawac_wave" in SOLVER_WORKFLOW_REGISTRY
    assert "tomawac_wave" in LOCAL_SOLVER_SPEC_REGISTRY


def test_tool_rejects_neither_location_nor_bbox():
    from trid3nt_server.workflows.telemac.wave_field.wave_field import tomawac_wave_field
    out = asyncio.run(tomawac_wave_field())
    assert isinstance(out, dict) and out["status"] == "error"
    assert out["error_code"] == "TOMAWAC_PARAMS_INCOMPLETE"


def test_tool_rejects_invalid_bbox():
    from trid3nt_server.workflows.telemac.wave_field.wave_field import tomawac_wave_field
    out = asyncio.run(tomawac_wave_field(bbox=[1.0, 2.0]))  # too few numbers
    assert isinstance(out, dict) and out["status"] == "error"
    assert out["error_code"] == "TOMAWAC_PARAMS_INVALID"


def test_mode_classification_from_prompt():
    """The declared coercion reads the question class off the ask, or takes it."""
    from trid3nt_server.workflows.telemac.wave_field.wave_mode import wave_mode
    coerce = wave_mode()
    assert coerce({"location": "swell shoaling at the beach"})["wave_mode"] == "shoaling"
    assert coerce({"location": "opposing current at the inlet"})["wave_mode"] \
        == "wave_current"
    assert coerce({"location": "bottom friction on the shelf"})["wave_mode"] \
        == "bottom_friction"
    # NO signal in either field emits NOTHING: a value emitted here would resolve
    # through the USER door and stamp the declared fetch_growth default as
    # user-supplied on every bare invocation.
    assert coerce({"location": "how big do the waves get"}) == {}
    # an explicit value wins over the phrasing, and an unknown one falls back to it
    assert coerce({"location": "anything", "wave_mode": "wave_current"})["wave_mode"] \
        == "wave_current"
    assert coerce({"location": "at the beach", "wave_mode": "nonsense"})["wave_mode"] \
        == "shoaling"


# ===========================================================================
# The DECLARATION: the plan value, the deck, and the georeference.
# ===========================================================================
def _sheet(**overrides):
    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.workflows.lib.resolver import resolve_params

    workflow = TOOL_REGISTRY["tomawac_wave_field"].fn.workflow
    args = {"bbox": [-87.60, 46.70, -86.60, 47.20], "wave_mode": "fetch_growth",
            "target_resolution_m": 3000.0, "sim_duration_hours": 1.0,
            "bathy_source": "noaa_greatlakes", **overrides}
    supplied, err = workflow._normalize(args)
    assert err is None, err
    return workflow, asyncio.run(resolve_params(workflow.params, supplied))


def test_the_declared_plan_is_the_open_water_sequence():
    from trid3nt_server.workflows.lib.validate import validate_plan

    workflow, _sheet_unused = _sheet()
    plan = workflow.plan
    assert [step.label for step in plan.declared()] == [
        "form", "aoi", "deck", "solve", "wave_field"]
    validate_plan(plan, workflow.params, workflow.data)


def test_the_wave_deck_stages_under_tomawac_but_keys_itself_wave():
    """The manifest KEY and the cache PREFIX are different words, on purpose.

    Collapsing them puts the document where the worker looks with a key it does
    not read - which is a silent fall-through to the reach pipeline, not an error.
    """
    from trid3nt_server.workflows.telemac.steps.wave import write_wave_deck

    deck = asyncio.run(write_wave_deck(bed=_STAGED_BED, 
        aoi={"slug": "aoi", "name": "aoi", "lon": -87.1, "lat": 46.95,
             "bbox": (-87.60, 46.70, -86.60, 47.20)},
        wave_mode="fetch_growth", mesh_resolution_m=3000.0, sim_duration_hours=1.0,
        bathy_source="noaa_greatlakes"))
    assert deck["section"] == "wave" and deck["prefix"] == "tomawac"
    assert deck["solver"] == "tomawac_wave"
    assert deck["real_bathymetry"] is True and deck["lake"] == "superior"
    assert deck["config"]["bbox"] == [-87.6, 46.7, -86.6, 47.2]
    # friction arms itself for the class that is ABOUT friction, and only that one
    assert deck["config"]["bottom_friction"] is False


def test_the_idealized_basin_carries_no_bbox():
    """A geography-free basin has no AOI to sample, so it claims none."""
    from trid3nt_server.workflows.telemac.steps.wave import write_wave_deck

    deck = asyncio.run(write_wave_deck(bed=_STAGED_BED, 
        aoi={"slug": "aoi", "name": "aoi", "lon": -120.0, "lat": 38.0,
             "bbox": (-120.7, 37.6, -119.3, 38.4)},
        bathy_source="auto"))
    assert deck["real_bathymetry"] is False
    assert "bbox" not in deck["config"]
    assert deck["config"]["target_resolution_m"] == 1500.0


def test_a_local_coordinate_mesh_is_georeferenced_from_the_aoi_corner():
    """The wave grid is built with node 0 at the AOI's SW corner.

    Reprojecting those local metres as ABSOLUTE UTM is what put the Hs COG at the
    zone's false origin - thousands of km from the lake - while the bed COG beside
    it sat correctly on the water. A basin with no AOI has no corner to add; a
    MALFORMED corner is a different fact and refuses, because reading it as absent
    is what sends a real domain to the false origin.
    """
    import pytest

    from trid3nt_server.workflows.telemac.postprocess_telemac import (
        PostprocessTelemacError,
        _local_mesh_origin,
    )

    x_org, y_org = _local_mesh_origin((-87.60, 46.70, -86.60, 47.20), 32616)
    assert 300_000 < x_org < 700_000 and 5_100_000 < y_org < 5_300_000
    assert _local_mesh_origin(None, 32616) == (0.0, 0.0)
    with pytest.raises(PostprocessTelemacError):
        _local_mesh_origin((1.0, 2.0), 32616)


def test_the_fetch_chart_plots_the_workers_own_curve():
    from trid3nt_contracts.telemac_contracts import (
        TELEMAC_WAVE_STYLE_PRESET,
        TelemacWaveLayerURI,
    )
    from trid3nt_server.workflows.telemac.wave_field.wave_field import build_fetch_chart

    layer = TelemacWaveLayerURI(
        layer_id="x", name="Significant wave height (aoi)", layer_type="raster",
        uri="s3://b/k.tif", style_preset=TELEMAC_WAVE_STYLE_PRESET, role="primary",
        hs_max_m=0.7164, hs_upwind_m=0.3787, hs_downwind_m=0.7164,
        wind_speed_mps=20.0, fetch_curve_km=[0.0, 3.047, 6.093],
        fetch_curve_hs_m=[0.09, 0.3787, 0.5169])
    payload = build_fetch_chart(result=layer, params={})
    values = payload["vega_lite_spec"]["data"]["values"]
    assert [row["hs_m"] for row in values] == [0.09, 0.3787, 0.5169]
    assert "0.379 m at the upwind shore" in payload["caption"]
    # a run with no fetch axis (shoaling, wave_current) has no curve to draw
    bare = layer.model_copy(update={"fetch_curve_km": None, "fetch_curve_hs_m": None})
    assert build_fetch_chart(result=bare, params={}) is None
