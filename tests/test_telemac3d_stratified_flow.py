"""Offline unit tests for the telemac3d_stratified_flow engine template (ADR 0241).

No solver / no network: registration shape + arg-guard rejection paths + mode
classification only. The physics-through-the-image proof lives in the Dockerfile
build-time smoke + the live E2E; this is the offline-suite guard that the tool is
registered as an engine template and rejects ill-posed args before any dispatch.
"""
from __future__ import annotations

import asyncio

#: What the declared bed producer hands the run writer: the staged raster's URI.
#: A domain solved on real bathymetry refuses without one, because the worker
#: holds no fetcher of its own any more.
_STAGED_BED = {"uri": "s3://trid3nt-cache/cache/static-30d/ncei_dem_mosaic/test.tif",
               "source": "noaa_ncei_dem_all"}


def test_telemac3d_registered_as_engine_template():
    from trid3nt_server.tools import TOOL_REGISTRY
    entry = TOOL_REGISTRY.get("telemac3d_stratified_flow")
    assert entry is not None, "telemac3d_stratified_flow must be registered"
    m = entry.metadata
    assert m.engine == "telemac" and m.tier == "template"
    assert m.cacheable is False and m.ttl_class == "live-no-cache"
    specs = {r.param for r in (m.resolution_specs or ())}
    assert "target_resolution_m" in specs


def test_telemac3d_solver_registered():
    from trid3nt_server.workflows.solver.solver import (
        LOCAL_SOLVER_SPEC_REGISTRY,
        SOLVER_WORKFLOW_REGISTRY,
    )
    assert "telemac3d_strat" in SOLVER_WORKFLOW_REGISTRY
    assert "telemac3d_strat" in LOCAL_SOLVER_SPEC_REGISTRY


def test_tool_rejects_neither_location_nor_bbox_for_lake_modes():
    from trid3nt_server.workflows.telemac.stratified_flow.stratified_flow import (
        telemac3d_stratified_flow,
    )
    out = asyncio.run(telemac3d_stratified_flow(flow_mode="stratification"))
    assert isinstance(out, dict) and out["status"] == "error"
    assert out["error_code"] == "TELEMAC3D_PARAMS_INCOMPLETE"


def test_tool_rejects_invalid_bbox():
    from trid3nt_server.workflows.telemac.stratified_flow.stratified_flow import (
        telemac3d_stratified_flow,
    )
    out = asyncio.run(telemac3d_stratified_flow(bbox=[1.0, 2.0]))  # too few numbers
    assert isinstance(out, dict) and out["status"] == "error"
    assert out["error_code"] == "TELEMAC3D_PARAMS_INVALID"


def test_mode_classification_from_prompt():
    """The declared coercion reads the question class off the ask, or takes it."""
    from trid3nt_server.workflows.telemac.stratified_flow.flow_mode import flow_mode
    coerce = flow_mode()
    # NO signal in either field emits NOTHING: a value emitted here would resolve
    # through the USER door and stamp the declared stratification default as
    # user-supplied on every bare invocation.
    assert coerce({"location": "does this lake stratify and turn over"}) == {}
    assert coerce({"location": "wind-driven circulation and return flow"}
                  )["flow_mode"] == "wind_circulation"
    assert coerce({"location": "salt wedge intrusion in the estuary"}
                  )["flow_mode"] == "salt_wedge"
    # an explicit value wins over the phrasing, and an unknown one falls back to it
    assert coerce({"location": "anything", "flow_mode": "wind_circulation"}
                  )["flow_mode"] == "wind_circulation"
    assert coerce({"location": "the gyre", "flow_mode": "nonsense"}
                  )["flow_mode"] == "wind_circulation"

# ===========================================================================
# The DECLARATION: the plan value, the run, and the georeference.
# ===========================================================================
def _sheet(**overrides):
    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.workflows.runtime.resolver import resolve_params

    workflow = TOOL_REGISTRY["telemac3d_stratified_flow"].fn.workflow
    args = {"bbox": [-87.60, 46.70, -86.60, 47.20], "flow_mode": "stratification",
            "wind_speed_mps": 0.0, "warm_temp_c": 25.0, "cold_temp_c": 15.0,
            "nplan": 13, "target_resolution_m": 3000.0,
            "sim_duration_hours": 1.0, "bathy_source": "noaa_greatlakes",
            **overrides}
    supplied, err = workflow._normalize(args)
    assert err is None, err
    return workflow, asyncio.run(resolve_params(workflow.params, supplied))


def test_the_declared_plan_is_the_open_water_sequence():
    from trid3nt_server.workflows.runtime.validate import validate_plan

    workflow, _sheet_unused = _sheet()
    plan = workflow.plan
    assert [step.label for step in plan.declared()] == [
        "form", "aoi", "run", "solve", "column"]
    validate_plan(plan, workflow.params, workflow.data)


def test_the_3d_run_carries_both_layer_files():
    from trid3nt_server.workflows.telemac.authoring.stratified import write_stratified_case

    run = asyncio.run(write_stratified_case(bed=_STAGED_BED, 
        aoi={"slug": "aoi", "name": "aoi", "lon": -87.1, "lat": 46.95,
             "bbox": (-87.60, 46.70, -86.60, 47.20)},
        flow_mode="stratification", nplan=13, mesh_resolution_m=3000.0,
        sim_duration_hours=1.0, bathy_source="noaa_greatlakes"))
    assert run["section"] == "stratified" and run["prefix"] == "telemac3d"
    assert run["solver"] == "telemac3d_strat"
    assert run["result_basename"] == "t3d_surface.slf"
    assert run["bottom_basename"] == "t3d_bottom.slf"
    assert run["real_bathymetry"] is True
    assert run["config"]["nplan"] == 13


def test_a_salt_wedge_never_takes_the_real_bathymetry_path():
    """A real estuary would need a tidal liquid boundary; the wedge is the
    ANALYTIC lock-exchange V&V, and asking for real bathymetry cannot conjure one."""
    from trid3nt_server.workflows.telemac.authoring.stratified import write_stratified_case

    run = asyncio.run(write_stratified_case(bed=_STAGED_BED, 
        aoi={"slug": "aoi", "name": "aoi", "lon": -87.1, "lat": 46.95,
             "bbox": (-87.60, 46.70, -86.60, 47.20)},
        flow_mode="salt_wedge", bathy_source="noaa_greatlakes"))
    assert run["real_bathymetry"] is False
    assert "bbox" not in run["config"]
    assert "lock-exchange" in run["bathy_label"]


def test_the_3d_layers_are_georeferenced_from_the_aoi_corner():
    """The 3D build lays its mesh with node 0 at the AOI's SW corner.

    Reprojecting those local metres as ABSOLUTE UTM put BOTH COGs at the zone's
    false origin - measured on a Lake Superior run at lon -91.49, lat 0.0.
    """
    from trid3nt_server.workflows.telemac.products.postprocess_telemac import _local_mesh_origin

    x_org, y_org = _local_mesh_origin((-87.60, 46.70, -86.60, 47.20), 32616)
    assert 300_000 < x_org < 700_000 and 5_100_000 < y_org < 5_300_000


def test_the_profile_chart_shows_the_column_before_and_after():
    from trid3nt_contracts.telemac_contracts import (
        TELEMAC3D_STRATIFICATION_STYLE,
        Telemac3dLayerURI,
    )
    from trid3nt_server.workflows.telemac.stratified_flow.stratified_flow import (
        build_profile_chart,
    )

    layer = Telemac3dLayerURI(
        layer_id="x", name="Surface temperature (aoi)", layer_type="raster",
        uri="s3://b/k.tif", role="primary", stratification_metric=4.5347, stratification_dt=4.5347,
        variable_label="Surface temperature", variable_units="degC",
        profile_sigma=[0.0, 0.5, 1.0], profile_values=[15.0, 15.0, 19.535],
        profile_values_initial=[15.0, 15.0, 25.0])
    payload = build_profile_chart(result=layer, params={})
    values = payload["vega_lite_spec"]["data"]["values"]
    # BOTH lines: the prescribed initial column and the solved final one
    assert {row["state"] for row in values} == {"initial", "final"}
    assert [row["v"] for row in values if row["state"] == "final"] == \
        [15.0, 15.0, 19.535]
    assert "4.535 degC" in payload["caption"]
    bare = layer.model_copy(update={"profile_sigma": None, "profile_values": None})
    assert build_profile_chart(result=bare, params={}) is None
