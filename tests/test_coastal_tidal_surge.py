"""Offline unit tests for the coastal_tidal_surge engine template (ADR 0259).

No solver / no network: registration shape + arg-guard rejection paths + the
series-type classifier only. The physics-through-the-image proof lives in the
substrate build-time smoke + the live E2E; this is the offline-suite guard that
the tool is registered as an engine template and rejects ill-posed args before any
dispatch.
"""
from __future__ import annotations

import asyncio


def test_coastal_tidal_surge_registered_as_engine_template():
    from trid3nt_server.tools import TOOL_REGISTRY
    entry = TOOL_REGISTRY.get("coastal_tidal_surge")
    assert entry is not None, "coastal_tidal_surge must be registered"
    m = entry.metadata
    assert m.engine == "telemac" and m.tier == "template"
    assert m.cacheable is False and m.ttl_class == "live-no-cache"
    assert m.source_class == "workflow_dispatch"
    specs = {r.param for r in (m.resolution_specs or ())}
    assert "target_resolution_m" in specs


def test_coastal_solver_registered():
    from trid3nt_server.workflows.solver.solver import (
        LOCAL_SOLVER_SPEC_REGISTRY,
        SOLVER_WORKFLOW_REGISTRY,
    )
    assert "telemac_coastal" in SOLVER_WORKFLOW_REGISTRY
    assert "telemac_coastal" in LOCAL_SOLVER_SPEC_REGISTRY


def test_tool_rejects_invalid_bbox():
    from trid3nt_server.workflows.telemac.coastal_tidal_surge.coastal_tidal_surge import (
        coastal_tidal_surge,
    )
    out = asyncio.run(coastal_tidal_surge(bbox=[1.0, 2.0]))  # too few numbers
    assert isinstance(out, dict) and out["status"] == "error"
    assert out["error_code"] == "COASTAL_PARAMS_INVALID"


def test_series_type_classification_from_prompt():
    """The declared coercion reads the question class off the ask, or takes it."""
    from trid3nt_server.workflows.telemac.coastal_tidal_surge.series_type import (
        series_type,
    )
    coerce = series_type()
    assert coerce({"location": "the astronomical tide prediction"})["series_type"] \
        == "prediction"
    # Prediction wording is the only positive signal; the observed record is the
    # else-branch, so no wording emits NOTHING. A value emitted here would resolve
    # through the USER door and stamp the declared default as user-supplied.
    assert coerce({"location": "observed hurricane surge record"}) == {}
    assert coerce({"location": "map the storm surge inland"}) == {}
    # an explicit value wins over the phrasing, and an unknown one falls back to it
    assert coerce({"location": "anything", "series_type": "prediction"})["series_type"] \
        == "prediction"
    assert coerce({"location": "calm tide", "series_type": "nonsense"})["series_type"] \
        == "prediction"


def test_coastal_layer_contract_carries_typed_scalars():
    from trid3nt_contracts.telemac_contracts import (
        TELEMAC_COASTAL_DEPTH_STYLE_PRESET,
        TelemacCoastalLayerURI,
    )
    layer = TelemacCoastalLayerURI(
        layer_id="telemac-coastal-depth-x", name="Peak inundation depth (coast)",
        layer_type="raster", uri="s3://bucket/coastal_depth_max.tif",
        style_preset=TELEMAC_COASTAL_DEPTH_STYLE_PRESET, role="primary", units="m",
        peak_depth_m=9.33, flooded_land_km2=14.51, series_type="observed",
        sl_peak_m=2.645)
    assert layer.peak_depth_m == 9.33 and layer.flooded_land_km2 == 14.51
    assert layer.style_preset == "continuous_coastal_inundation_depth"


# ===========================================================================
# The DECLARATION: the plan value, the deck, and the facade routing.
# ===========================================================================
def _sheet(**overrides):
    """The resolved sheet for a canary-shaped invocation of the template."""
    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.workflows.lib.resolver import resolve_params

    workflow = TOOL_REGISTRY["coastal_tidal_surge"].fn.workflow
    args = {"bbox": [-85.02, 29.69, -84.90, 29.80], "series_type": "observed",
            "station": "8728690", "start_date": "2018-10-09",
            "end_date": "2018-10-11", "target_resolution_m": 250.0,
            "duration_hours": 6.0, **overrides}
    supplied, err = workflow._normalize(args)
    assert err is None, err
    return workflow, asyncio.run(resolve_params(workflow.params, supplied))


def test_the_declared_plan_is_the_open_water_sequence():
    """Review, acquire the AOI, author, solve, publish - and the plan VALIDATES."""
    from trid3nt_server.workflows.lib.validate import validate_plan

    workflow, sheet = _sheet()
    plan = workflow.build_plan(sheet)
    assert [step.label for step in plan.flat()] == [
        "form", "aoi", "deck", "solve", "inundation"]
    assert [step.stage for step in plan.flat()][1:] == [
        "acquire", "author", "solve", "publish"]
    # the AOI step is what rebinds the domain, so every producer after it reads it
    assert plan.flat()[1].rebinds_domain
    # the chart is a FUNCTION colocated in the template file
    from trid3nt_server.workflows.telemac.coastal_tidal_surge.coastal_tidal_surge import (
        build_stage_chart,
    )
    assert plan.flat()[-1].charts[0].builder is build_stage_chart
    validate_plan(plan, workflow.params, workflow.data, sheet=sheet)


def test_the_open_water_domain_acquires_the_aoi_and_nothing_else():
    """No flowline, no seed, no carrier: a coastal strip is bounded by the ask."""
    from trid3nt_server.workflows.telemac.workflow import TelemacWorkflow

    steps = TelemacWorkflow.acquire_domain(
        TelemacWorkflow, location=None, bbox=[-85.0, 29.7, -84.9, 29.8],
        shape="open_water", aoi_name="coast")
    assert len(steps) == 1 and steps[0].name == "aoi"
    assert steps[0].runner.endswith("shared.aoi.acquire_aoi")


def test_an_unknown_domain_shape_refuses_at_plan_construction():
    from trid3nt_server.workflows.lib import PlanValidationError
    from trid3nt_server.workflows.telemac.workflow import TelemacWorkflow

    import pytest
    with pytest.raises(PlanValidationError) as excinfo:
        TelemacWorkflow.acquire_domain(TelemacWorkflow, location="x", bbox=None,
                                       shape="estuary")
    assert "estuary" in str(excinfo.value)


def test_an_unknown_physics_process_refuses_before_anything_runs():
    from trid3nt_server.workflows.lib import Forcing, MeshPolicy, Physics
    from trid3nt_server.workflows.lib import PlanValidationError
    from trid3nt_server.workflows.telemac.workflow import TelemacWorkflow

    import pytest
    facade = TelemacWorkflow.__new__(TelemacWorkflow)
    mesh = facade.build_mesh("aoi", MeshPolicy(resolution=None, target_edge_m=250.0))
    with pytest.raises(PlanValidationError) as excinfo:
        facade.author(mesh=mesh, physics=Physics("tsunami"), forcing=Forcing())
    assert "tsunami" in str(excinfo.value)


def test_a_grid_domain_declares_no_corridor_fields():
    """An undeclared corridor member is ABSENT, not None.

    Passing None would null out the writer's own default - which is how a grid
    domain would silently hand a corridor writer a reach with no length.
    """
    from trid3nt_server.workflows.lib import MeshPolicy
    from trid3nt_server.workflows.telemac.workflow import TelemacWorkflow

    facade = TelemacWorkflow.__new__(TelemacWorkflow)
    fields = facade.build_mesh(
        "aoi", MeshPolicy(resolution=None, target_edge_m=250.0)).deck_fields()
    assert fields == {"mesh_resolution_m": 250.0}


def test_the_coastal_deck_carries_what_solves_it():
    from trid3nt_server.workflows.telemac.steps.coastal import write_coastal_deck

    deck = asyncio.run(write_coastal_deck(
        aoi={"slug": "coast", "name": "coast", "bbox": (-85.02, 29.69, -84.90, 29.80)},
        water_level={"series": [[0.0, 0.5], [360.0, 1.4]], "series_datum": "MLLW",
                     "series_type": "observed", "station_id": "8728690"},
        mesh_resolution_m=250.0, duration_hours=6.0))
    assert deck["solver"] == "telemac_coastal" and deck["section"] == "coastal"
    assert deck["result_basename"] == "res_coastal.slf"
    assert deck["config"]["duration_s"] == 21600.0
    assert deck["config"]["water_level_series"] == [[0.0, 0.5], [360.0, 1.4]]
    # unasked cadence stays ABSENT so the worker's own default stands
    assert "output_interval_min" not in deck["config"]


def test_a_coastal_deck_with_no_series_refuses_typed():
    """A seaward boundary with nothing to drive it is a refusal, not a flat tide."""
    from trid3nt_server.workflows.telemac.steps.open_water import OpenWaterError

    from trid3nt_server.workflows.telemac.steps.coastal import write_coastal_deck

    import pytest
    with pytest.raises(OpenWaterError) as excinfo:
        asyncio.run(write_coastal_deck(
            aoi={"slug": "coast", "name": "coast", "bbox": (-85.0, 29.7, -84.9, 29.8)},
            water_level=None))
    assert excinfo.value.error_code == "COASTAL_TIDE_EMPTY"


def test_the_stage_chart_reads_the_layer_and_refuses_to_invent_one():
    from trid3nt_contracts.telemac_contracts import TelemacCoastalLayerURI
    from trid3nt_server.workflows.telemac.coastal_tidal_surge.coastal_tidal_surge import (
        build_stage_chart,
    )

    from trid3nt_contracts.telemac_contracts import TELEMAC_COASTAL_DEPTH_STYLE_PRESET

    layer = TelemacCoastalLayerURI(
        layer_id="x", name="Peak inundation depth (coast)", layer_type="raster",
        uri="s3://b/k.tif", style_preset=TELEMAC_COASTAL_DEPTH_STYLE_PRESET,
        role="primary", peak_depth_m=6.8779,
        flooded_land_km2=0.0921, peak_wl_m=5.3869, sl_peak_m=2.845,
        series_type="observed", series_datum="MLLW")
    payload = build_stage_chart(result=layer, params={})
    values = payload["vega_lite_spec"]["data"]["values"]
    assert [row["m"] for row in values] == [2.845, 5.3869, 6.8779]
    assert "0.0921 km2" in payload["caption"]
    # a run that measured no boundary stage has no chart to draw, and says so
    assert build_stage_chart(result=object(), params={}) is None
