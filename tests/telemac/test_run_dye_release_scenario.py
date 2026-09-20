"""The dye-release template: its PARAMS, its slots, and the plan the workflow owns.

Exercised in ISOLATION - no network, no docker, no engine. Pinned: registration
and metadata, the wire-arg normalization and its refusals, the declared bounds,
the three engine-neutral slots the DATA body declares, and the stages the
workflow class builds from them.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

_PORTLAND = (-122.6735, 45.5175)  # the Willamette at USGS 14211720


def _workflow():
    from trid3nt_server.tools import TOOL_REGISTRY

    return TOOL_REGISTRY["telemac_dye_release"].fn.workflow


def _norm(**kw):
    base: dict[str, Any] = {
        "release": None, "compute_class": None, "input_mode": "auto",
    }
    base.update(kw)
    return asyncio.run(_workflow()._normalize(base))


def _resolve(**supplied):
    """The sheet this invocation resolves, over every row the template declares."""
    from trid3nt_server.workflows.runtime import resolve_params

    return asyncio.run(resolve_params(_workflow().params, dict(supplied)))


def _steps():
    return list(_workflow().plan.declared())


def _step(name: str):
    return next(s for s in _workflow().plan.steps if s.name == name)


def _template():
    from trid3nt_server.workflows.telemac.templates.dye_release import dye_release

    return dye_release


def test_registered_as_an_engine_template():
    from trid3nt_server.tools import TOOL_REGISTRY

    entry = TOOL_REGISTRY.get("telemac_dye_release")
    assert entry is not None
    assert entry.metadata.source_class == "workflow_dispatch"
    assert entry.metadata.engine == "telemac"
    assert entry.metadata.tier == "template"
    assert entry.metadata.cacheable is False
    assert entry.metadata.ttl_class == "live-no-cache"
    assert TOOL_REGISTRY.get("run_telemac") is None


def test_docstring_routing_view_fits_the_truncation_budget():
    from trid3nt_server.workflows.telemac.templates.dye_release.dye_release import (
        telemac_dye_release,
    )

    head = telemac_dye_release.routing_doc.split("\nReturns:")[0]
    assert len(head) <= 1000
    # The question class, not the body of water: nothing here says the answer is
    # only a river's.
    assert "river reach" not in head.lower().split("do not use")[0]


@pytest.mark.parametrize("value", [
    [-122.6735, 45.5175],
    "45.5175,-122.6735",
    {"coordinates": [-122.6735, 45.5175], "name": "outfall-a"},
])
def test_every_release_point_form_reaches_the_one_point_slot(value):
    from trid3nt_server.inputs import Point

    supplied, err = _norm(release=value)
    assert err is None
    got = supplied["release"]
    assert isinstance(got, Point) and (got.lon, got.lat) == _PORTLAND


def test_a_malformed_release_point_refuses_it_never_falls_back():
    from trid3nt_server.workflows.telemac.templates.dye_release.dye_release import (
        telemac_dye_release,
    )

    out = asyncio.run(telemac_dye_release(release=[200.0, 10.0]))
    assert out["error_code"] == "TELEMAC_PARAMS_INVALID"
    out = asyncio.run(telemac_dye_release(release={"name": "x"}))
    assert out["error_code"] == "TELEMAC_PARAMS_INVALID"


def test_an_invented_compute_class_refuses_at_the_ladder():
    """A rung the dispatcher cannot serve is REFUSED, not quietly re-seated."""
    supplied, err = _norm(compute_class="dye_spill")
    assert supplied == {}
    assert err["error_code"] == "COMPUTE_CLASS_UNKNOWN"
    assert "dye_spill" in err["error_message"]


def test_declared_bounds_keep_the_source_inside_the_domain():
    """spill_fraction=1.0 planted the source ON the outflow boundary and aborted
    the solve; the declared bound refuses it rather than moving it."""
    from trid3nt_server.workflows.runtime import GateRefusedError

    for outside in ({"spill_fraction": 1.0}, {"spill_fraction": 0.0},
                    {"spill_duration_s": 0.0}):
        with pytest.raises(GateRefusedError, match="outside the declared range"):
            _resolve(**outside)
    assert _resolve(spill_fraction=0.9).value_of("spill_fraction") == 0.9


def test_a_non_numeric_bounded_arg_refuses_it_is_never_defaulted():
    from trid3nt_server.workflows.runtime import GateRefusedError

    with pytest.raises(GateRefusedError):
        _resolve(spill_duration_s="a while")


def test_the_carrier_flow_is_the_inflow_runs_value_and_not_a_param():
    """The flow the inflow run carries is the observation ROW's - a reading, or
    the number stated on it - so no param of this question declares one, and the
    row states the CLASS it needs rather than the source that reports it."""
    from trid3nt_server.workflows.runtime import data_rows, param_rows
    from trid3nt_server.workflows.telemac.templates.dye_release.dye_release import DATA
    from trid3nt_server.workflows.telemac.templates.dye_release.declarations import (
        PARAMS,
    )

    assert "discharge_m3s" not in {p.name for p in param_rows(PARAMS)}
    carrier = next(d for d in data_rows(DATA) if d.name == "carrier")
    assert carrier.role == "discharge"
    assert carrier.coercion["measures"] == "a streamflow"
    assert carrier.producer is None
    assert carrier.data_class == "discharge series"
    assert carrier.coercion["to_units"] == "m3/s"


def test_the_params_are_the_questions_own_and_the_deck_states_the_keywords():
    """No keyword twin, no domain twin, no lever restated: the dictionary
    describes the clock, the roughness, the cadence, the wind and the rain, the
    slots describe the water, and the runtime declares the granularity, the
    moment and the box."""
    from trid3nt_server.workflows.runtime.levers import LEVER_NAMES
    from trid3nt_server.workflows.telemac.templates.dye_release.declarations import (
        PARAMS,
    )
    from trid3nt_server.workflows.telemac.templates.dye_release.dye_release import (
        STEERING,
    )
    from trid3nt_server.workflows.runtime import param_rows

    declared = {p.name for p in param_rows(PARAMS)}
    assert not declared & {"friction_law", "friction_coefficient",
                           "output_interval_min", "sim_duration_s",
                           "wind_speed_mps", "wind_direction_deg",
                           "rainfall_mm_per_day", "decay_half_life_hours",
                           "decay_rate_per_day"}
    assert not declared & {"location", "bbox", "river_geometry_uri",
                           "reach_length_km", "mesh_resolution_m"}
    # The window this question is asked over is the module's own keyword, in the
    # seconds it reads, and the settle is built off the same statement.
    assert STEERING.ASSERTED["DURATION"] == 3600.0
    # No lever is restated here, so all four are seated at the end, in the order
    # the runtime declares them.
    seated = [p.name for p in _workflow().params]
    assert seated[-len(LEVER_NAMES):] == list(LEVER_NAMES)


def test_the_data_body_is_the_slots_and_the_classes_they_need():
    """The domain the question prefers a producer for, and the bed, the carrier
    and the level as the CLASSES they need - no fetcher named on any of them."""
    from trid3nt_server.workflows.runtime import Ref, data_rows
    from trid3nt_server.workflows.telemac.templates.dye_release.dye_release import DATA

    rows = data_rows(DATA)
    assert [d.name for d in rows] == ["domain", "runs", "bed", "carrier",
                                      "stage"]
    by_name = {d.name: d for d in rows}
    assert by_name["domain"].role == "domain"
    assert by_name["domain"].geometry == "polygon"
    assert by_name["domain"].producer.runner == "fetch_river_reach"
    # THE BED is one class and the runtime owns the merge between the two.
    assert by_name["bed"].role == "bed"
    assert by_name["bed"].producer is None
    assert by_name["bed"].data_class == "bathymetry"
    # ONE READING, not the published grid: the step that opens the channel
    # refuses a record nobody chose a site from, so the flow arrives ingested.
    carrier = by_name["carrier"]
    assert carrier.role == "discharge"
    assert carrier.data_class == "discharge series"
    assert carrier.coercion["near"] == Ref("domain.centroid")
    # The level is matched too, and its absence is legal.
    assert by_name["stage"].data_class == "water level series"
    assert by_name["stage"].is_optional
    # No row here is superseded by a supplied artifact, and the DOMAIN is the
    # one ladder: the reach where a channel cuts, the waterbody the seed stands
    # in where none does.
    assert all(d.producer is None or d.producer.supplied_uri is None
               for d in rows)
    assert [d.name for d in rows
            if d.producer is not None and d.producer.ladder_rungs] == ["domain"]


def test_the_release_point_seeds_the_domain_producer():
    """A release named up front also names which stretch to model, and the wire
    carries no second spelling of the same point."""
    import inspect

    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.workflows.runtime import Ref, data_rows
    from trid3nt_server.workflows.telemac.templates.dye_release.dye_release import DATA

    domain = data_rows(DATA)[0]
    assert domain.producer.kwargs["seed_point"] == [Ref("release.lon"),
                                                    Ref("release.lat")]
    wire = set(inspect.signature(TOOL_REGISTRY["telemac_dye_release"].fn).parameters)
    assert "release" in wire
    assert not {"release_coords", "release_lat", "release_lon", "location",
                "bbox", "reach_length_km", "river_geometry_uri"} & wire


def test_the_domain_and_the_bed_reach_the_wire_as_the_slots_they_are():
    """What the user hands in supersedes the producer the template preferred, so
    a pond outline, a stated depth and a stated flow run this question with no
    fetch at all. The rows between - the survey and the terrain the bed is merged
    from - are the template's own working and stay off the wire."""
    import inspect

    from trid3nt_server.tools import TOOL_REGISTRY

    wire = set(inspect.signature(TOOL_REGISTRY["telemac_dye_release"].fn).parameters)
    assert {"domain", "bed", "carrier"} <= wire
    assert {"survey", "terrain"}.isdisjoint(wire)


def test_the_workflow_owns_the_stages_and_the_template_states_what_differs():
    from trid3nt_server.workflows.runtime import DataRef, validate_plan

    wf = _workflow()
    validate_plan(wf.plan, wf.params, wf.data)
    steps = _steps()
    # The channel is LISTED because this question declares a discharge; whether
    # a run of it carries one is the author's to settle off the carrier it is
    # handed, and an absent one opens on the level boundaries instead.
    assert [s.label for s in steps] == ["stated", "mesh", "channel", "source",
                                        "settled", "sheet", "solve", "outputs"]
    # The review is the door's VIEW of the sheet it just filled, so the run is
    # held on the fill itself rather than in front of a step that has not run.
    assert [s.label for s in steps if s.self_gating] == ["sheet"]
    assert steps[-2].consequential
    # The release reads the DOMAIN, not a centerline row of its own: an unplaced
    # point sits its fraction along the companion the producer wrote beside the
    # polygon, and a supplied one is snapped onto the same line.
    source = next(s for s in steps if s.label == "source")
    assert source.kwargs["domain"] == DataRef("domain")
    listed = steps[-1].kwargs["outputs"]
    assert [(p.kind, p.variable, p.publish) for p in listed] == [
        ("series", "T1", "chart")]
    assert steps[-1].kwargs["captions"] == {"T1": "dye concentration"}
    assert set(steps[-1].kwargs["answer"]) == {
        "dye_cmax_mgl", "dye_peak_time_s", "plume_reach_m", "active_frames",
        "mesh_size_m"}


def test_the_mesh_is_built_over_the_domain_slot_at_the_runtimes_own_lever():
    from trid3nt_server.workflows.mesh.tool import recipe_from_plan_value
    from trid3nt_server.workflows.runtime import DataRef

    recipe = recipe_from_plan_value(_step("mesh").kwargs["mesh"])
    assert recipe.mesher == "om2d" and recipe.kind == "unstructured_tri"
    assert recipe.extent == DataRef("domain")
    assert recipe.resolution_m.name == "mesh_resolution_m"
    bed = next(op for op in recipe.ops if op.fn == "set_bed")
    assert bed.kwargs == {"source": DataRef("bed")}
    # The RUNS slot: filled by the domain producer that cut the polygon between
    # two faces, by the user's own runs, or by what they draw on the canvas.
    runs = next(op for op in recipe.ops if op.fn == "set_boundary_roles")
    assert runs.kwargs == {"runs": DataRef("runs")}


def test_the_settle_step_reads_the_files_the_deck_itself_names():
    from trid3nt_server.workflows.runtime import Ref
    from trid3nt_server.workflows.telemac.workflow import stated

    settle = _step("settled")
    assert settle.runner.endswith("assembler.open_water")
    assert settle.kwargs["geometry"] == "domain.slf"
    assert settle.kwargs["boundary"] == "domain.cli"
    assert settle.kwargs["result"] == "r2d_domain.slf"
    # THE CLOCK IS THE RESOLVED FLOOR'S: the settle runs before the sheet
    # exists, so it reads the DURATION the deck will write.
    assert settle.kwargs["duration_s"] == Ref("stated.DURATION")
    assert stated(steering=_template().STEERING,
                  keywords={})["DURATION"] == 3600.0
    # The restart travels beside the result: a continuation reads it, and the
    # deck's own RESULTS statement cannot name it.
    assert list(_step("solve").kwargs["results"]) == ["r2d_domain.slf",
                                                      "restart_domain.slf"]


def test_no_step_names_a_template_module_as_a_tool():
    """The reach chain is dissolved: nothing this template runs is reached by
    module path into the templates tree."""
    assert not [s.runner for s in _steps()
                if ".templates." in s.runner]


def test_an_unknown_data_row_is_an_attribute_error_at_the_line_that_wrote_it():
    from trid3nt_server.workflows.telemac.templates.dye_release.dye_release import DATA

    with pytest.raises(AttributeError):
        DATA.centreline


def test_the_deck_states_the_four_source_keywords_and_a_sources_composite():
    """The release enters through the engine's own keywords, one element per
    source, and the composite shrinks to the sources file plus the window."""
    from trid3nt_server.workflows.runtime import ParamRef, Ref
    from trid3nt_server.workflows.telemac.modules.telemac2d import T2D as _T2D
    from trid3nt_server.workflows.telemac.templates.dye_release.dye_release import (
        STEERING,
    )

    assert STEERING.ASSERTED["ABSCISSAE_OF_SOURCES"] == [Ref("source.at.0")]
    assert STEERING.ASSERTED["ORDINATES_OF_SOURCES"] == [Ref("source.at.1")]
    assert STEERING.ASSERTED["WATER_DISCHARGE_OF_SOURCES"] == [8.0]
    assert STEERING.ASSERTED["VALUES_OF_THE_TRACERS_AT_THE_SOURCES"] == [100.0]
    for name in ("ABSCISSAE OF SOURCES", "ORDINATES OF SOURCES",
                 "WATER DISCHARGE OF SOURCES",
                 "VALUES OF THE TRACERS AT THE SOURCES"):
        assert _T2D.identify(name) is not None
    sources = STEERING.ASSERTED["sources"]
    window, until = sources["window_s"], sources["until_s"]
    assert isinstance(window, ParamRef) and window.name == "spill_duration_s"
    assert until == Ref("settled.until_s")
    assert sources["q"] == Ref("WATER_DISCHARGE_OF_SOURCES")
    assert sources["tracers"] == Ref("VALUES_OF_THE_TRACERS_AT_THE_SOURCES")


def test_the_sources_file_the_deck_writes_is_the_series_the_engine_reads():
    """The four keywords plus ``sources`` write the finite pulse: held at the
    stated discharge and concentration, then stepped to nothing so the slug
    advects and passes, with a tail past the last simulated instant."""
    from trid3nt_server.workflows.telemac.modules.telemac2d import (
        Sources,
        T2D as _T2D,
    )

    stated = Sources(at={"at": [0.0, 0.0]}, window_s=300.0, until_s=600.0)
    slots, files = _T2D.COMPOSITES["sources"].expand(
        {**stated, "q": [8.0], "tracers": [100.0]})
    assert dict(slots) == {"SOURCES_FILE": "river_sources.txt"}
    assert files["river_sources.txt"].splitlines() == [
        "#", "T Q(1) TR(1,1)", "s m3/s mg/l",
        "0.000 8 100", "300.000 8 100", "300.100 0 0", "700.000 0 0"]
