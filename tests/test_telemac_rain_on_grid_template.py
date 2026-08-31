"""Offline unit tests for the telemac_rain_on_grid engine template - migrated onto
the declarative skeleton (PARAMS + DATA + ``plan(ops)``; see
``docs/design/declarative-workflows.md``).

No solver / no network: registration shape, the declared plan sequence, the
wire-signature door contract, and the pure step/chart helpers only. Live
end-to-end (mesh acquisition + solve + depth COG) is proven on Coweeta Creek NC by
scripts/sandbox/telemac/rog_coweeta_live.py (docs/proof/templates/
telemac_rain_on_grid*.png); the worker RoG deck THROUGH the image by
scripts/sandbox/telemac/rog_offline_smoke.py.
"""

from __future__ import annotations

import asyncio

import pytest


def test_registered_on_the_model_surface():
    """The catchment front is LIVE: its outlet boundary is declared on the mesh
    ask and the hydrograph is measured server-side off the run's own result, so
    nothing is left for a park to state."""
    import trid3nt_server.main as _main
    from trid3nt_server.workflows.telemac.rain_on_grid.rain_on_grid import (
        telemac_rain_on_grid,
    )

    _main._import_tools_registry()
    from trid3nt_server.tools import TOOL_REGISTRY

    assert "telemac_rain_on_grid" in TOOL_REGISTRY
    assert telemac_rain_on_grid.parked is None

    md = telemac_rain_on_grid.workflow.metadata
    assert md.engine == "telemac"
    assert md.tier == "template"
    assert md.cacheable is False
    assert md.ttl_class == "live-no-cache"
    assert md.source_class == "workflow_dispatch"
    specs = {r.param for r in (md.resolution_specs or ())}
    assert "mesh_min_edge_m" in specs


def test_the_outlet_boundary_is_declared_on_the_mesh_ask():
    """The one liquid boundary a catchment has is DECLARED where every other
    boundary role is - on the mesh ask, at the delineation's snapped outlet -
    rather than resolved by a server step between the mesh and the deck."""
    from trid3nt_server.workflows.telemac.rain_on_grid.rain_on_grid import MESH

    roles = MESH.spec.fields["boundaries"]
    assert set(roles) == {"outflow"}
    assert roles["outflow"]["type"] == "Point"
    assert roles["outflow"]["coordinates"].path == "basin.snapped_pour_point"


def test_docstring_carries_the_godara_envelope():
    import trid3nt_server.workflows.telemac.rain_on_grid.rain_on_grid as rog_module
    from trid3nt_server.workflows.telemac.rain_on_grid.rain_on_grid import (
        telemac_rain_on_grid,
    )

    # the applicability envelope's citation lives on the module's own docstring.
    assert "Godara" in (rog_module.__doc__ or "")

    # and the applicability CLASS (single-storm, small steep catchments) rides the
    # model-facing rendered docstring, not just an internal comment.
    doc = telemac_rain_on_grid.__doc__ or ""
    assert "RAIN" in doc and "watershed" in doc.lower()
    assert "hydrograph" in doc.lower()
    assert "SCS" in doc or "curve-number" in doc.lower() or "curve number" in doc.lower()
    assert "SINGLE-STORM" in doc and "steep catchments" in doc


def test_corpus_yaml_present_and_routes():
    from pathlib import Path

    import yaml

    import trid3nt_server.workflows.telemac.rain_on_grid as pkg

    corpus = Path(pkg.__file__).parent / "corpus.yaml"
    assert corpus.exists()
    data = yaml.safe_load(corpus.read_text())
    assert "telemac_rain_on_grid" in data
    assert any("runoff" in q.lower() for q in data["telemac_rain_on_grid"])


# ===========================================================================
# The DECLARATION: the plan value and the wire-signature door contract.
# ===========================================================================
def test_the_declared_plan_is_the_rain_on_grid_sequence():
    """form -> draw -> aoi -> mesh -> infiltration -> deck -> solve ->
    flood_depth, and the plan VALIDATES against its own declared params/data."""
    from trid3nt_server.workflows.lib.validate import validate_plan
    from trid3nt_server.workflows.telemac.rain_on_grid.rain_on_grid import (
        telemac_rain_on_grid,
    )

    workflow = telemac_rain_on_grid.workflow
    plan = workflow.plan
    assert [step.label for step in plan.declared()] == [
        "form", "draw", "aoi", "mesh", "infiltration", "deck", "solve",
        "flood_depth"]
    validate_plan(plan, workflow.params, workflow.data)


def test_constant_door_params_off_wire_scenario_and_user_ones_present():
    """CONSTANT-door params (landcover_dataset, soil_spinup_days, mesh_grade,
    bed_dem_resolution_m, river_source, time_step_s, compute_class) never reach
    the model-facing schema; SCENARIO/USER ones (the storm, the granularity
    lever, the mesh slot) do."""
    import inspect

    from trid3nt_server.workflows.lib import doors
    from trid3nt_server.workflows.telemac.rain_on_grid.rain_on_grid import (
        telemac_rain_on_grid as fn,
    )

    constants = {p.name for p in fn.workflow.params if p.door == doors.CONSTANT}
    assert constants, "telemac_rain_on_grid declares no constants; the check is vacuous"
    wire = set(inspect.signature(fn).parameters)
    assert not (constants & wire), f"puts {constants & wire} on the model-facing wire"

    scenario_or_user = {p.name for p in fn.workflow.params
                        if p.door in (doors.SCENARIO, doors.USER)}
    assert scenario_or_user <= wire
    assert {"pour_point", "mesh_min_edge_m", "antecedent_moisture",
           "design_storm_mm_per_hr"} <= wire
    # No "mesh" slot: a supplied mesh reaches a run through the mesh ROUTER at
    # the build door, not through a template's own context slot - a second
    # resolver inside a model template is the silent-adoption defect D-9 forbids.
    assert "mesh" not in wire


@pytest.mark.asyncio
async def test_pour_point_is_never_invented_in_auto_mode():
    """pour_point is REQUIRED (door=USER, not optional) and its own DrawGate
    refuses typed in auto mode rather than falling back to a centroid nobody
    chose - unlike an OPTIONAL draw-gated param, whose absence just derives."""
    from trid3nt_server.workflows.lib import DrawGate
    from trid3nt_server.workflows.lib.errors import GateRefusedError
    from trid3nt_server.workflows.lib.interpreter import _run_draw_gate
    from trid3nt_server.workflows.lib.resolver import resolve_params
    from trid3nt_server.workflows.telemac.rain_on_grid.declarations import PARAMS

    pour_point_param = next(p for p in PARAMS if p.name == "pour_point")
    assert pour_point_param.optional is not True

    sheet = await resolve_params(PARAMS, {"bbox": [-83.47, 35.02, -83.36, 35.10]})
    gate = DrawGate(param="pour_point", geometry="point",
                    prompt="Click the catchment OUTLET the runoff drains to")
    with pytest.raises(GateRefusedError, match="never invented"):
        await _run_draw_gate(gate, sheet, PARAMS, input_mode=None,
                             tool_name="telemac_rain_on_grid")


# ===========================================================================
# amc_condition_for: the three SCS words, and a refused fourth.
# ===========================================================================
def test_amc_condition_for_maps_the_three_words():
    from trid3nt_server.workflows.telemac.rain_on_grid.cn_infiltration import (
        amc_condition_for,
    )

    assert amc_condition_for("dry") == 1 and amc_condition_for("i") == 1
    assert amc_condition_for("normal") == 2 and amc_condition_for("II") == 2
    assert amc_condition_for("wet") == 3 and amc_condition_for("3") == 3


def test_amc_condition_for_refuses_a_fourth_word():
    from trid3nt_server.workflows.telemac.rain_on_grid.cn_infiltration import (
        CNInfiltrationError, amc_condition_for,
    )

    with pytest.raises(CNInfiltrationError):
        amc_condition_for("saturated")


# ===========================================================================
# The outlet hydrograph chart.
# ===========================================================================
def test_hydrograph_chart_none_with_no_series():
    from types import SimpleNamespace

    from trid3nt_server.workflows.telemac.rain_on_grid.rain_on_grid import (
        build_hydrograph_chart,
    )

    empty = SimpleNamespace(outlet_hydrograph_t_s=None, outlet_hydrograph_q_m3s=None)
    assert build_hydrograph_chart(result=empty, params={}) is None


def test_hydrograph_chart_line_spec_with_a_series():
    from types import SimpleNamespace

    from trid3nt_server.workflows.telemac.rain_on_grid.rain_on_grid import (
        build_hydrograph_chart,
    )

    result = SimpleNamespace(
        outlet_hydrograph_t_s=[0.0, 3600.0], outlet_hydrograph_q_m3s=[-1.0, -5.0],
        peak_discharge_m3s=5.0, catchment_area_km2=2.0, runoff_coefficient=0.3,
        rain_intensity_mm_per_hr=25.0, name="watershed")
    payload = build_hydrograph_chart(result=result, params={"location": None})
    assert payload is not None
    assert payload["vega_lite_spec"]["mark"]["type"] == "line"
    values = payload["vega_lite_spec"]["data"]["values"]
    # discharge OUT of the basin reads positive: the outlet boundary's outward
    # normal makes an outflow arrive negative, and the sign is flipped once here.
    assert values == [{"t_h": 0.0, "q_m3s": 1.0}, {"t_h": 1.0, "q_m3s": 5.0}]
    assert "5" in payload["caption"] and "2 km2" in payload["caption"]


# ===========================================================================
# resolve_rain_event: the two rungs.
# ===========================================================================
def test_resolve_rain_event_design_storm_rung_no_window():
    from trid3nt_server.workflows.telemac.steps.rain_on_grid import (
        resolve_rain_event,
    )

    # no sim_duration_hr asked -> the storm's OWN duration stands.
    out = resolve_rain_event(window=None, intensity_mm_per_hr=25.0,
                             storm_duration_hr=6.0, sim_duration_hr=None)
    assert out["kind"] == "design_storm"
    assert out["blocks"] is None and out["series"] is None
    assert out["duration_s"] == 6.0 * 3600.0
    assert out["duration_basis"] == "storm"

    # sim_duration_hr asked -> it wins over the storm's own duration.
    out2 = resolve_rain_event(window=None, intensity_mm_per_hr=25.0,
                              storm_duration_hr=6.0, sim_duration_hr=10.0)
    assert out2["duration_s"] == 10.0 * 3600.0
    assert out2["duration_basis"] == "user"


def test_resolve_rain_event_malformed_window_refuses():
    from trid3nt_server.workflows.lib.domain import Domain, bind_domain, reset_domain
    from trid3nt_server.workflows.telemac.steps.rain_on_grid import (
        RainOnGridError, resolve_rain_event,
    )

    token = bind_domain(Domain(bbox=(-83.47, 35.02, -83.36, 35.10)))
    try:
        with pytest.raises(RainOnGridError) as ei:
            resolve_rain_event(window="no-separator", intensity_mm_per_hr=25.0,
                               storm_duration_hr=6.0, sim_duration_hr=None)
        assert ei.value.error_code == "TELEMAC_ROG_BAD_WINDOW"
    finally:
        reset_domain(token)


def test_resolve_rain_event_hyetograph_rung_builds_hourly_blocks(monkeypatch):
    """A real window fetches the hourly record and drives the run with it - the
    surviving equivalent of the deleted ``_fetch_hyetograph_blocks``."""
    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.workflows.lib.domain import Domain, bind_domain, reset_domain
    from trid3nt_server.workflows.telemac.steps.rain_on_grid import resolve_rain_event

    monkeypatch.setitem(
        TOOL_REGISTRY, "fetch_aorc_precip",
        type("S", (), {"fn": staticmethod(
            lambda **kw: {"precip_mm": [3.0, 12.5, 0.0]})})())

    token = bind_domain(Domain(bbox=(-83.47, 35.02, -83.42, 35.06)))
    try:
        out = resolve_rain_event(window="2015-12-23/2015-12-24",
                                 intensity_mm_per_hr=25.0, storm_duration_hr=6.0,
                                 sim_duration_hr=None)
    finally:
        reset_domain(token)
    assert out["kind"] == "hyetograph"
    assert out["series"] == [3.0, 12.5, 0.0]
    assert out["blocks"] == [[3600.0, 3.0], [7200.0, 12.5], [10800.0, 0.0]]
    assert out["duration_s"] == 3 * 3600.0   # hyetograph span dominates the no-ask


# ===========================================================================
# The soil-moisture store re-homed (surviving equivalent of the deleted module-
# level ``_spin_up_soil_v0`` / ``model_telemac_rain_on_grid`` soil-store guard).
# ===========================================================================
def test_soil_store_spin_up_fills_from_antecedent(monkeypatch):
    """A wetter antecedent record -> a higher V0, capped at the store capacity."""
    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.workflows.lib.domain import Domain, bind_domain, reset_domain
    from trid3nt_server.workflows.telemac.steps.rain_on_grid import (
        _soil_store_spin_up,
    )

    def _stub(precip):
        return type("S", (), {"fn": staticmethod(lambda **kw: {"precip_mm": precip})})()

    token = bind_domain(Domain(bbox=(-83.47, 35.02, -83.42, 35.06)))
    try:
        monkeypatch.setitem(TOOL_REGISTRY, "fetch_aorc_precip", _stub([5.0] * 240))
        v_wet = _soil_store_spin_up(window="2018-02-10/2018-02-11",
                                    capacity_mm=300.0, recovery_hr=120.0,
                                    antecedent_days=45)
        monkeypatch.setitem(TOOL_REGISTRY, "fetch_aorc_precip", _stub([1.0] * 240))
        v_dry = _soil_store_spin_up(window="2018-02-10/2018-02-11",
                                    capacity_mm=300.0, recovery_hr=120.0,
                                    antecedent_days=45)
    finally:
        reset_domain(token)
    assert v_wet > v_dry >= 0.0
    assert v_wet <= 300.0  # never over capacity


@pytest.mark.parametrize("rain,capacity,code", [
    ({"kind": "design_storm", "intensity_mm_per_hr": 25.0, "duration_s": 21600.0,
      "series": None}, 300.0, "TELEMAC_ROG_SOIL_STORE_NEEDS_WINDOW"),
    ({"kind": "hyetograph", "intensity_mm_per_hr": 25.0, "duration_s": 21600.0,
      "series": [1.0, 2.0]}, None, "TELEMAC_ROG_SOIL_STORE_NO_CAPACITY"),
    # And with both of its own preconditions met, the store still has no authored
    # form: it was the retired in-worker runoff model and the deck drives the
    # engine's own static SCS-CN.
    ({"kind": "hyetograph", "intensity_mm_per_hr": 25.0, "duration_s": 21600.0,
      "series": [1.0, 2.0]}, 300.0, "TELEMAC_ROG_SOIL_STORE_UNAUTHORED"),
])
def test_the_soil_store_refuses_typed_rather_than_reading_as_applied(
        rain, capacity, code):
    """soil_store refuses at deck-authoring time - a knob that reads as applied
    and is not is the one failure a labeled default cannot be read past."""
    from trid3nt_server.workflows.telemac.steps.rain_on_grid import (
        RainOnGridError, write_rain_on_grid_deck,
    )

    with pytest.raises(RainOnGridError) as ei:
        asyncio.run(write_rain_on_grid_deck(
            catchment={}, infiltration={"amc_condition": 2, "curve_number": None},
            rain=rain, time_step_s=3.0, soil_store=True,
            soil_store_capacity_mm=capacity, soil_recovery_hr=120.0,
            soil_spinup_days=45))
    assert ei.value.error_code == code


# ===========================================================================
# The pour-point-first AOI (the ADR 0196 live bug: a town bbox clipping the
# upstream basin) - the surviving equivalent of the deleted
# ``_aoi_from_pour_point`` / ``model_telemac_rain_on_grid`` dispatch tests.
# ===========================================================================
def test_aoi_from_pour_point_buffers_the_outlet():
    from trid3nt_server.workflows.telemac.steps import catchment_aoi
    from trid3nt_server.workflows.telemac.rain_on_grid.declarations import (
        POUR_POINT_BUFFER_DEG,
    )

    pp = (-83.40402, 35.05746)
    aoi = catchment_aoi(pp, POUR_POINT_BUFFER_DEG)
    # centered on the pour point.
    assert aoi[0] < pp[0] < aoi[2] and aoi[1] < pp[1] < aoi[3]
    # each side under the 0.3-deg D8 clamp.
    assert (aoi[2] - aoi[0]) <= 0.3 and (aoi[3] - aoi[1]) <= 0.3
    assert abs((aoi[2] - aoi[0]) - 2 * POUR_POINT_BUFFER_DEG) < 1e-9


@pytest.mark.asyncio
async def test_a_supplied_pour_point_derives_the_aoi_from_it_not_a_geocoded_bbox():
    """When a pour point is supplied the analysis AOI must come FROM the pour
    point, NOT a geocoded place bbox (the ADR 0196 live bug: 'Otto, NC' geocodes
    to a town box that does not contain the upstream Coweeta catchment).
    ``acquire_catchment`` (the catchment shape's own acquire step) never even
    reaches for a geocoder - the catchment shape's docstring names this: the
    basin's shape is the terrain's answer, never the geocoder's bbox."""
    from trid3nt_server.workflows.telemac.steps import catchment_aoi
    from trid3nt_server.workflows.telemac.steps.rain_on_grid import acquire_catchment

    pp = (-83.40402, 35.05746)
    out = await acquire_catchment(location="Otto, North Carolina", bbox=None,
                                  pour_point=pp, half_deg=0.14)
    assert out["bbox"] == catchment_aoi(pp, 0.14)
    assert out["pour_point"] == [pp[0], pp[1]]


@pytest.mark.asyncio
async def test_acquire_catchment_never_invents_a_pour_point():
    """Unreachable through the declared plan (the DrawGate refuses first); stated
    here anyway because a missing outlet must never fall back to a centroid."""
    from trid3nt_server.workflows.telemac.steps.rain_on_grid import (
        RainOnGridError, acquire_catchment,
    )

    with pytest.raises(RainOnGridError) as ei:
        await acquire_catchment(location="x", bbox=(-83.5, 35.0, -83.3, 35.1),
                                pour_point=None, half_deg=0.14)
    assert ei.value.error_code == "TELEMAC_ROG_PARAMS_INCOMPLETE"


# ===========================================================================
# The authored case: the steering file, the fields it names, and what solves it.
# ===========================================================================
def _accepted_catchment_mesh():
    """The mesh step's record for an accepted catchment, as the deck reads it."""
    from types import SimpleNamespace

    return {
        "artifact": SimpleNamespace(
            utm_epsg=32617, bbox=(-83.47, 35.02, -83.36, 35.10),
            name="coweeta creek",
            probes={"area_km2": 2.5, "edge_length_m": {"min": 40.0, "max": 300.0}}),
        "mesh_id": "M1", "slf_uri": "s3://cache/mesh/M1/mesh.slf",
        "cli_uri": "s3://cache/mesh/M1/mesh.cli",
        "topology_uri": "s3://cache/mesh/M1/mesh_topology.json",
        "display_uri": "s3://cache/mesh/M1/mesh.2dm",
        "node_count": 4, "element_count": 2, "min_edge_m": 40.0,
        "provenance": {"dem_source": "3dep 100%", "sizing_source": "nhdplus_hr",
                       "domain_source": "supplied polygon domain (1 part(s))"},
    }


@pytest.fixture()
def rog_deck(monkeypatch, tmp_path):
    """``write_rain_on_grid_deck`` with the accepted mesh's reads stood in for.

    The AUTHORING is real: the steering file and every field it names are written
    into a temp run directory by the author this step calls.
    """
    import numpy as np

    from trid3nt_server.workflows.mesh import topology as topo_mod
    from trid3nt_server.workflows.telemac.steps import rain_on_grid as rog_mod

    monkeypatch.setenv("TRID3NT_RUNS_DIR", str(tmp_path))
    monkeypatch.setattr(topo_mod, "read_topology", lambda _uri: {
        "roles": {"outflow": [1, 3]}, "liquid_boundary_order": ["outflow"]})
    monkeypatch.setattr(rog_mod, "mesh_nodes", lambda _mesh: (
        np.array([[0.0, 0.0], [20.0, 0.0], [0.0, 10.0], [20.0, 10.0]]),
        np.array([[0, 1, 2], [1, 3, 2]]), np.zeros(4),
        np.array([[-83.4, 35.0]] * 4)))

    async def _write(**kwargs):
        return await rog_mod.write_rain_on_grid_deck(
            catchment=_accepted_catchment_mesh(),
            infiltration={"amc_condition": 2, "curve_number": None,
                          "node_cn2": [70.0, 72.0, 74.0, 76.0],
                          "node_manning": [0.035, 0.035, 0.1, 0.06]},
            time_step_s=3.0, soil_recovery_hr=120.0, soil_spinup_days=45,
            **kwargs)

    return _write


_DESIGN_STORM = {"kind": "design_storm", "intensity_mm_per_hr": 25.0,
                 "duration_s": 21600.0, "rain_duration_s": 21600.0,
                 "series": None, "blocks": None, "note": "a CONSTANT design storm",
                 "duration_basis": "storm"}


def test_a_constant_storm_authors_a_case_and_stages_no_fortran(rog_deck, tmp_path):
    deck = asyncio.run(rog_deck(rain=_DESIGN_STORM))
    case = deck["case"]
    assert case["module"] == "telemac2d"
    assert case["steering"] == "t2d_rog.cas"
    assert case["results"] == ["r2d_rog.slf"]
    assert case["family"] == "rain_on_grid"
    # a constant-rain run compiles nothing, so the key is ABSENT: the worker's
    # strict gate reads a present key as a directory it must compile.
    assert "user_fortran" not in case
    assert case["echo"]["utm_epsg"] == 32617
    assert case["echo"]["npoin"] == 4 and case["echo"]["nelem"] == 2

    cas = (tmp_path / f"telemac-{deck['run_tag']}" / "t2d_rog.cas").read_text()
    assert "GEOMETRY FILE                   = rog.slf" in cas
    assert "BOUNDARY CONDITIONS FILE        = rog.cli" in cas
    assert "RESULTS FILE                    = r2d_rog.slf" in cas
    assert "FORMATTED DATA FILE 2           = rog_cn_map.dat" in cas
    assert "FORTRAN FILE" not in cas
    # every file the deck names was authored beside it
    assert set(deck["authored"]) == {"t2d_rog.cas", "rog_cn_map.dat",
                                     "rog_friction.tbl", "rog_zones.dat"}


def test_the_outputs_are_exactly_what_the_run_writes_and_was_handed(rog_deck):
    deck = asyncio.run(rog_deck(rain=_DESIGN_STORM))
    assert set(deck["outputs"]) == {
        "r2d_rog.slf", "rog.slf", "rog.cli", "full_listing.log",
        "telemac_metrics.json", "t2d_rog.cas", "rog_cn_map.dat",
        "rog_friction.tbl", "rog_zones.dat"}


def test_a_time_varying_storm_names_the_baked_fortran_on_both_channels(
        rog_deck, tmp_path):
    from trid3nt_server.workflows.telemac.steps.author import RAINDEF3_USER_FORTRAN

    deck = asyncio.run(rog_deck(rain={
        "kind": "hyetograph", "intensity_mm_per_hr": 25.0, "duration_s": 10800.0,
        "series": [3.0, 12.5, 0.0], "note": "the REAL hourly AORC hyetograph",
        "duration_basis": "hyetograph", "window": "2015-12-23/2015-12-24",
        "blocks": [[3600.0, 3.0], [7200.0, 12.5], [10800.0, 0.0]]}))
    assert deck["case"]["user_fortran"] == RAINDEF3_USER_FORTRAN
    cas = (tmp_path / f"telemac-{deck['run_tag']}" / "t2d_rog.cas").read_text()
    assert f"FORTRAN FILE                    = {RAINDEF3_USER_FORTRAN}" in cas
    assert "FORMATTED DATA FILE 1           = rog_hyeto.txt" in cas
    assert "rog_hyeto.txt" in deck["authored"]
    assert deck["hyetograph_total_mm"] == 15.5


def test_the_hydrograph_integrates_over_the_declared_outlet_nodes(rog_deck):
    """The nodes are the ones the DECLARED role landed on, read off the accepted
    topology rather than re-derived as a nearest-node set."""
    deck = asyncio.run(rog_deck(rain=_DESIGN_STORM))
    assert deck["outlet_nodes"] == [1, 3]


def test_a_mesh_whose_boundary_took_no_outlet_role_refuses(rog_deck, monkeypatch):
    from trid3nt_server.workflows.mesh import topology as topo_mod
    from trid3nt_server.workflows.telemac.steps.rain_on_grid import RainOnGridError

    monkeypatch.setattr(topo_mod, "read_topology", lambda _uri: {
        "roles": {"inflow": [0]}, "liquid_boundary_order": ["inflow"]})
    with pytest.raises(RainOnGridError) as ei:
        asyncio.run(rog_deck(rain=_DESIGN_STORM))
    assert ei.value.error_code == "TELEMAC_ROG_NO_OUTLET_NODES"


def test_the_rainfall_volume_is_the_gross_depth_over_the_meshed_area(rog_deck):
    from trid3nt_server.workflows.telemac.steps.rain_on_grid import (
        _rainfall_volume_m3,
    )

    deck = asyncio.run(rog_deck(rain=_DESIGN_STORM))
    # 25 mm/h over 6 h = 150 mm over 2.5 km2.
    assert _rainfall_volume_m3(deck) == pytest.approx(0.15 * 2.5e6)
