"""The primitive set: what each reads off a solved run, and what the door does
with a template's outputs list.

Offline: the result a read opens is stated through the ``telemac_result``
fixture, the store and the publisher are stood in for, and what is proved is
which numbers reach which answer."""

from __future__ import annotations

import asyncio
from typing import Any

import numpy as np
import pytest

from trid3nt_server.workflows.publishing import Field, Frames, Published, Series
from trid3nt_server.workflows.telemac.modules import (
    T2D,
    Measure,
    extent,
    field,
    mass_balance,
    max_over_time,
    mesh,
    series,
)
from trid3nt_server.workflows.telemac.modules.outputs import OutputEmpty, Solved


def _reach(telemac_result, *, tracer_name: str = "DYE") -> dict[str, Any]:
    """Five nodes in UTM zone 10, four frames: the tracer arrives, peaks, drains."""
    x = [500000.0, 500120.0, 500000.0, 500120.0, 500060.0]
    y = [4400000.0, 4400000.0, 4400110.0, 4400110.0, 4400055.0]
    ikle = [[0, 1, 4], [1, 3, 4], [2, 3, 4], [0, 2, 4]]
    dye = [[0.0, 0.0, 0.0, 0.0, 0.0],
           [10.0, 80.0, 5.0, 40.0, 60.0],
           [2.0, 30.0, 1.0, 50.0, 20.0],
           [0.0, 0.0, 0.0, 0.0, 0.0]]
    depth = [[2.0] * 5] * 4
    return telemac_result(varnames=["WATER DEPTH", tracer_name], x=x, y=y,
                          ikle=ikle, times=[0.0, 60.0, 120.0, 180.0],
                          data={"WATER DEPTH": depth, tracer_name: dye})


def _solved(run: dict[str, Any] | None = None, body: Any = T2D) -> Solved:
    return Solved({"run_id": "RID", "utm_epsg": 32610, "result_basename": "r2d.slf",
                   "tracer_names": ["DYE             MG/L"], "name": "reach",
                   "mesh_size_m": 7.5, "mesh_resolution_label": "7.5 m measured",
                   "started_at": "2026-01-01T00:00:00+00:00", **(run or {})},
                  body)


@pytest.fixture()
def solved(monkeypatch, telemac_result):
    _reach(telemac_result)
    monkeypatch.setattr(
        "trid3nt_server.workflows.telemac.solving.solve.download_result",
        lambda run_id, basename, error_code=None: "/tmp/does-not-matter.slf")
    return _solved()


def test_a_tracer_token_resolves_to_the_declared_tracer_name_and_its_unit(solved):
    assert solved.variable("T1") == ("DYE", "mg/L")
    assert solved.variable("H") == ("WATER DEPTH", "m")


def test_a_named_release_renames_the_tracer_the_reads_find(solved, telemac_result):
    _reach(telemac_result, tracer_name="OUTFALL-A")
    renamed = _solved({"tracer_names": ["outfall-a       MG/L"]})
    assert renamed.variable("T1") == ("OUTFALL-A", "mg/L")


def test_a_token_outside_the_vocabulary_refuses_by_name(solved):
    with pytest.raises(OutputEmpty, match="not in the telemac2d variable"):
        solved.variable("XX")
    with pytest.raises(OutputEmpty, match="declares 1"):
        solved.variable("T2")


def test_a_variable_the_result_does_not_carry_refuses(solved):
    with pytest.raises(OutputEmpty, match="not among the variables"):
        solved.variable("S")


def test_the_envelope_is_the_peak_every_node_reached_and_when(solved):
    read = T2D.OUTPUTS["max_over_time"].read(max_over_time("T1"), solved)
    assert isinstance(read, Field)
    assert list(read.values) == [10.0, 80.0, 5.0, 50.0, 60.0]
    assert read.measures["max"] == 80.0
    assert read.measures["t_max"] == 60.0
    # the tracer's visible edge: five percent of its own peak
    assert read.floor == pytest.approx(4.0)
    assert read.measures["active_frames"] == 2
    assert read.units == "mg/L" and read.name == "DYE"
    # the nodes are handed over in lon/lat, with the element table beside them
    assert -124.0 < float(read.lon[0]) < -122.0
    assert np.asarray(read.ikle).shape == (4, 3)


def test_the_series_is_the_domain_maximum_at_each_instant(solved):
    read = T2D.OUTPUTS["series"].read(series("T1"), solved)
    assert isinstance(read, Series)
    assert list(read.times) == [0.0, 60.0, 120.0, 180.0]
    assert list(read.values) == [0.0, 80.0, 50.0, 0.0]
    assert read.at == "the domain maximum"


def test_a_series_at_a_point_reads_the_nearest_node(solved):
    from trid3nt_server.workflows.inputs import Point

    lon, lat = solved.lonlat
    at = Point(float(lon[1]), float(lat[1]), "outfall-a")
    read = T2D.OUTPUTS["series"].read(series("T1", at=at), solved)
    assert list(read.values) == [0.0, 80.0, 30.0, 0.0]
    assert read.at == "at outfall-a"


def test_the_field_over_every_instant_is_the_frames_an_animation_plays(solved):
    read = T2D.OUTPUTS["field"].read(field("T1", t="every"), solved)
    assert isinstance(read, Frames)
    assert (read.file, read.group, read.epsg, read.frames) == (
        "r2d.slf", "DYE", 32610, 4)
    assert read.reference_time == "2026-01-01T00:00:00+00:00"
    # how far the plume's centroid moved from where it first appeared
    assert read.measures["travel_m"] > 0.0
    assert read.measures["active_frames"] == 2


def test_a_field_at_an_instant_is_that_frame(solved):
    last = T2D.OUTPUTS["field"].read(field("T1"), solved)
    assert last.t == 180.0 and list(last.values) == [0.0] * 5
    second = T2D.OUTPUTS["field"].read(field("T1", t=1), solved)
    assert second.t == 60.0 and list(second.values) == [10.0, 80.0, 5.0, 40.0, 60.0]
    nearest = T2D.OUTPUTS["field"].read(field("T1", t=130.0), solved)
    assert nearest.t == 120.0


def test_a_tracer_that_never_rose_above_its_floor_refuses(monkeypatch,
                                                            telemac_result):
    telemac_result(varnames=["DYE"], x=[0.0, 1.0, 0.0], y=[0.0, 0.0, 1.0],
                   ikle=[[0, 1, 2]], times=[0.0, 1.0],
                   data={"DYE": [[0.0, 0.0, 0.0], [1e-5, 0.0, 0.0]]})
    monkeypatch.setattr(
        "trid3nt_server.workflows.telemac.solving.solve.download_result",
        lambda run_id, basename, error_code=None: "/tmp/does-not-matter.slf")
    with pytest.raises(OutputEmpty, match="never exceeded its floor"):
        T2D.OUTPUTS["max_over_time"].read(max_over_time("T1"), _solved())


def test_the_extent_and_the_mesh_are_measures_off_the_result_and_the_run(solved):
    box = T2D.OUTPUTS["extent"].read(extent(), solved).measures
    assert box["bbox"][0] < box["bbox"][2] and box["bbox"][1] < box["bbox"][3]
    assert box["wetted_fraction"] == pytest.approx(1.0)
    facts = T2D.OUTPUTS["mesh"].read(mesh(), solved).measures
    assert facts["nodes"] == 5 and facts["elements"] == 4 and facts["planes"] == 1
    assert facts["size_m"] == 7.5


def test_the_mass_balance_is_the_engine_s_own_closure(monkeypatch, solved):
    monkeypatch.setattr(
        "trid3nt_server.workflows.telemac.solving.solve.download_result",
        lambda run_id, basename, error_code=None: _listing(monkeypatch))
    read = T2D.OUTPUTS["mass_balance"].read(mass_balance(), solved)
    assert read.measures["continuity_rel_error"] == pytest.approx(-1.2e-7)


def _listing(monkeypatch) -> str:
    import tempfile
    from pathlib import Path

    path = Path(tempfile.mkdtemp()) / "full_listing.log"
    path.write_text("BALANCE OF WATER VOLUME\n FLUX BOUNDARY 1 : 3.0\n"
                    " RELATIVE ERROR IN VOLUME AT T = 600.0 S : -1.2E-07\n")
    return str(path)


def test_a_primitive_is_a_value_and_its_measure_names_the_read_it_comes_from():
    listed = max_over_time("T1").layer(style={"kind": "continuous"})
    assert listed.publish == "layer" and listed.key == max_over_time("T1")
    assert field("T1", t="every").animate().key == field("T1", t="every")
    measure = max_over_time("T1").measure("max")
    assert measure == Measure(max_over_time("T1"), "max")
    assert max_over_time("T1").layer().measure("max") == measure


# -- the door: the outputs list, read and published -------------------------- #

def test_the_door_refuses_a_published_variable_with_no_caption():
    from trid3nt_server.workflows.runtime import PlanValidationError
    from trid3nt_server.workflows.telemac.templates.river_dye.river_dye import (
        telemac_river_dye,
    )
    from trid3nt_server.workflows.telemac.workflow import Door

    door = telemac_river_dye.workflow.plan_decl
    with pytest.raises(PlanValidationError, match="no caption"):
        Door(**{**_fields(door), "captions": {}})(telemac_river_dye.workflow)
    with pytest.raises(PlanValidationError, match="no .layer"):
        Door(**{**_fields(door), "outputs": [max_over_time("T1")]})(
            telemac_river_dye.workflow)


def _fields(door: Any) -> dict[str, Any]:
    import dataclasses

    return {f.name: getattr(door, f.name) for f in dataclasses.fields(door)}


def test_publish_outputs_reads_once_publishes_each_and_answers(monkeypatch, solved):
    """One read per primitive, every deliverable handed to the publisher in the
    listed order, and the answer read off the same reads."""
    from trid3nt_contracts.execution import LayerURI

    from trid3nt_server.workflows.telemac import workflow as door

    seen: dict[str, Any] = {}

    async def _publish(**kwargs):
        seen.update(kwargs)
        return Published(primary=LayerURI(
            layer_id="L", name="Peak dye concentration (reach)",
            layer_type="raster", uri="s3://runs/RID/dye_concentration.tif",
            quantity="dye_concentration"))

    monkeypatch.setattr(door, "publish", _publish)
    run = {**solved.run, "module": "telemac2d"}
    result = asyncio.run(door.publish_outputs(
        run=run,
        outputs=[field("T1", t="every").animate(),
                 max_over_time("T1").layer(style={"kind": "continuous"}),
                 series("T1").chart()],
        captions={"T1": "dye concentration"},
        answer={"cmax": max_over_time("T1").measure("max"),
                "t_peak": series("T1").measure("t_max"),
                "reach_m": field("T1", t="every").measure("travel_m"),
                "edge_m": mesh().measure("size_m")},
        params={"location": "the Wabash"}))
    assert [(d.mode, type(d.read).__name__, d.caption) for d in seen["items"]] == [
        ("animate", "Frames", "dye concentration"),
        ("layer", "Field", "dye concentration"),
        ("chart", "Series", "dye concentration")]
    assert seen["items"][1].style == {"kind": "continuous"}
    assert (seen["run_id"], seen["engine"], seen["name"], seen["where"]) == (
        "RID", "telemac", "reach", "the Wabash")
    assert result.answer == {"cmax": 80.0, "t_peak": 60.0,
                             "reach_m": pytest.approx(result.answer["reach_m"]),
                             "edge_m": 7.5}
    assert result.answer["reach_m"] > 0.0
    assert result.layer_id == "L" and result.quantity == "dye_concentration"


def test_the_answer_rides_the_layer_and_the_skeleton_reads_it_there():
    """The workflow's answer and the sensitivity note read a scalar off the
    answer map the layer carries, the same way a product layer's own field."""
    from trid3nt_contracts.execution import AnswerLayerURI

    from trid3nt_server.tools import TOOL_REGISTRY

    workflow = TOOL_REGISTRY["telemac_river_dye"].fn.workflow
    layer = AnswerLayerURI(layer_id="L", name="n", layer_type="raster", uri="s3://x",
                           answer={"dye_cmax_mgl": 4.5, "plume_reach_m": 120.0,
                                   "mesh_size_m": 9.0})
    metrics = workflow.answer(layer)
    assert (metrics["dye_cmax_mgl"], metrics["plume_reach_m"],
            metrics["mesh_size_m"]) == (4.5, 120.0, 9.0)
    assert metrics["dye_peak_time_s"] is None
    notes = workflow.checks(layer, _run_of(workflow))
    assert any("dye_cmax_mgl" in note and "9 m" in note for note in notes)


def _run_of(workflow: Any) -> Any:
    from types import SimpleNamespace

    from trid3nt_server.workflows.runtime import resolve_params

    params = asyncio.run(resolve_params(workflow.params,
                                        {"location": "X", "mesh_resolution_m": 9.0}))
    return SimpleNamespace(params=params, results={}, notes=[], entries=[])


# -- the primitives past the dye: appended tracers, instants, profiles, tracks - #

def _coupled_reach(telemac_result) -> dict[str, Any]:
    """Five nodes down a straight channel, two frames: the carrier's own tracer,
    then two the coupled process appended behind it, each with the unit the
    record stores; the flow runs +x at the last instant."""
    x = [500000.0, 500100.0, 500200.0, 500300.0, 500400.0]
    y = [4400000.0, 4400010.0, 4400000.0, 4400010.0, 4400000.0]
    ikle = [[0, 1, 2], [1, 2, 3], [2, 3, 4]]
    return telemac_result(
        varnames=["VELOCITY U", "VELOCITY V", "WATER DEPTH", "DYE", "DISSOLVED O2",
                  "ORGANIC LOAD"],
        varunits=["M/S", "M/S", "M", "MG/L", "MGO2/L", "MG/L"],
        x=x, y=y, ikle=ikle, times=[0.0, 600.0],
        data={"VELOCITY U": [[0.5] * 5] * 2, "VELOCITY V": [[0.0] * 5] * 2,
              "WATER DEPTH": [[2.0] * 5, [2.0, 2.0, 2.0, 2.0, 0.0]],
              "DYE": [[0.0] * 5, [1.0, 2.0, 3.0, 4.0, 5.0]],
              "DISSOLVED O2": [[9.0] * 5, [9.0, 8.0, 6.0, 7.0, 8.5]],
              "ORGANIC LOAD": [[0.0] * 5, [0.0, 20.0, 15.0, 10.0, 5.0]]})


@pytest.fixture()
def coupled(monkeypatch, telemac_result):
    _coupled_reach(telemac_result)
    monkeypatch.setattr(
        "trid3nt_server.workflows.telemac.solving.solve.download_result",
        lambda run_id, basename, error_code=None: "/tmp/does-not-matter.slf")
    return _solved()


def test_a_tracer_a_coupled_process_appended_is_read_by_position_with_its_unit(
        coupled):
    """The deck declares one tracer; the process appends its own behind it, in
    order, and the record's own unit is what the read carries."""
    assert coupled.variable("T1") == ("DYE", "mg/L")
    assert coupled.variable("T2") == ("DISSOLVED O2", "mgO2/L")
    assert coupled.variable("T3") == ("ORGANIC LOAD", "mg/L")
    with pytest.raises(OutputEmpty, match="declares 1 and the result carries"):
        coupled.variable("T4")


def test_a_field_at_an_instant_measures_that_frame_s_own_extremes(coupled):
    last = T2D.OUTPUTS["field"].read(field("T2", t=-1), coupled)
    assert (last.measures["max"], last.measures["min"], last.measures["t"]) == (
        9.0, 6.0, 600.0)
    # a tracer's floor still comes off the envelope, so the edge is the run's
    assert last.floor == pytest.approx(0.45)


def _line(coupled, *, reverse: bool = False):
    """The channel's own centerline in lon/lat, as a geometry document."""
    import json
    import tempfile
    from pathlib import Path

    lon, lat = coupled.lonlat
    coords = [[float(lon[0]), float(lat[0])], [float(lon[-1]), float(lat[-1])]]
    if reverse:
        coords.reverse()
    path = Path(tempfile.mkdtemp()) / "centerline.geojson"
    path.write_text(json.dumps({"type": "Feature", "properties": {},
                                "geometry": {"type": "LineString",
                                             "coordinates": coords}}))
    return str(path)


def test_a_profile_is_the_variable_per_station_down_the_line_at_the_instant(
        coupled):
    """Depth-weighted per station, the dry node dropped, the minimum found and
    placed, and the along-line speed the flow travelled at."""
    from trid3nt_server.workflows.publishing import Profile
    from trid3nt_server.workflows.telemac.modules.outputs import profile

    read = T2D.OUTPUTS["profile"].read(profile("T2", along=_line(coupled)), coupled)
    assert isinstance(read, Profile)
    assert read.along == "downstream distance" and read.units == "mgO2/L"
    # four wet nodes, four stations; the last node was dry at the instant
    assert read.measures["stations"] == 4
    assert list(read.values) == [9.0, 8.0, 6.0, 7.0]
    assert read.measures["min"] == 6.0
    assert read.measures["x_min_m"] == pytest.approx(200.0, abs=10.0)
    assert read.measures["velocity_mps"] == pytest.approx(0.5)


def test_a_profile_runs_the_way_the_solved_flow_goes(coupled):
    """A line drawn against the flow is read downstream regardless: the flow
    orients the chainage, so the mix point stays where the water carries it."""
    from trid3nt_server.workflows.telemac.modules.outputs import profile

    forward = T2D.OUTPUTS["profile"].read(profile("T3", along=_line(coupled)),
                                          coupled)
    against = T2D.OUTPUTS["profile"].read(
        profile("T3", along=_line(coupled, reverse=True)), coupled)
    assert list(forward.values) == list(against.values) == [0.0, 20.0, 15.0, 10.0]
    assert forward.measures["x_max_m"] == pytest.approx(against.measures["x_max_m"])


def test_the_drogues_are_the_track_at_three_written_instants(monkeypatch, coupled):
    """The release, the middle and the end of the track the module wrote, in
    lon/lat, with how many floats were released, remain, and how far they went."""
    import tempfile
    from pathlib import Path

    from trid3nt_server.workflows.publishing import Track

    track = Path(tempfile.mkdtemp()) / "drogues.txt"
    track.write_text(
        "TITLE = drogues\nVARIABLES = ID, X, Y\n"
        "ZONE T=\"t\", SOLUTIONTIME= 0.0\n1, 500000.0, 4400000.0\n2, 500000.0, 4400010.0\n"
        "ZONE T=\"t\", SOLUTIONTIME= 60.0\n1, 500100.0, 4400000.0\n2, 500100.0, 4400010.0\n"
        "ZONE T=\"t\", SOLUTIONTIME= 120.0\n1, 500200.0, 4400000.0\n"
        "ZONE T=\"t\", SOLUTIONTIME= 180.0\n1, 500300.0, 4400000.0\n")
    monkeypatch.setattr(
        "trid3nt_server.workflows.telemac.solving.solve.download_result",
        lambda run_id, basename, error_code=None: str(track))
    from trid3nt_server.workflows.telemac.modules.outputs import drogues

    read = T2D.OUTPUTS["drogues"].read(drogues(), coupled)
    assert isinstance(read, Track)
    assert [f["properties"]["t_s"] for f in read.features["features"]] == [
        0.0, 120.0, 180.0]
    assert read.measures == {"released": 2, "remaining": 1, "exited": 1,
                             "instants": 4, "drift_m": 300.0}


def test_a_primitive_names_the_coupled_module_whose_result_it_reads():
    """A module's own file is read through its own wrapper, keyed by the module."""
    from trid3nt_server.workflows.telemac.modules import GAIA

    bed = field("E", t=-1, module="gaia")
    assert bed.module == "gaia" and bed.key.module == "gaia"
    assert field("E", t=-1).key != bed.key and hash(bed.key) != hash(field("E").key)
    assert GAIA.OUTPUTS["field"].read is T2D.OUTPUTS["field"].read


def test_the_door_carries_a_primitive_s_point_and_line_beside_the_list(monkeypatch):
    """A read's point or line is a declared read the plan binds; it rides beside
    the list as anchors and the publish step rejoins it to its primitive."""
    from trid3nt_server.workflows.runtime import DataRef
    from trid3nt_server.workflows.telemac.templates.do_sag.do_sag import (
        telemac_do_sag,
    )

    step = next(s for s in telemac_do_sag.workflow.plan.declared()
                if s.label == "outputs")
    listed = step.kwargs["outputs"]
    assert all(p.along is None and p.at is None for p in listed)
    anchors = step.kwargs["anchors"]
    assert len(anchors) == len(listed) + len(step.kwargs["answer"])
    assert anchors[2]["along"] == DataRef("centerline")


def test_publish_outputs_rejoins_the_anchors_and_draws_the_reference_lines(
        monkeypatch, coupled):
    from trid3nt_contracts.execution import LayerURI

    from trid3nt_server.workflows.publishing import Line
    from trid3nt_server.workflows.telemac import workflow as door
    from trid3nt_server.workflows.telemac.modules.outputs import profile

    seen: dict[str, Any] = {}

    async def _publish(**kwargs):
        seen.update(kwargs)
        return Published(primary=LayerURI(
            layer_id="L", name="Dissolved oxygen (reach)", layer_type="raster",
            uri="s3://runs/RID/dissolved_oxygen.tif", quantity="dissolved_oxygen"))

    def _reference(read, reads, params):
        assert params["do_standard_mgl"] == 5.0
        return [Line(label="standard", x=[0.0, 1.0], values=[5.0, 5.0])]

    monkeypatch.setattr(door, "publish", _publish)
    run = {**coupled.run, "module": "telemac2d"}
    result = asyncio.run(door.publish_outputs(
        run=run,
        outputs=[field("T2", t=-1).layer(style={"kind": "continuous"}),
                 profile("T2", along=None).chart(reference=_reference)],
        captions={"T2": "dissolved oxygen"},
        answer={"low": profile("T2", along=None).measure("min")},
        anchors=[{"at": None, "along": None}, {"at": None, "along": _line(coupled)},
                 {"at": None, "along": _line(coupled)}],
        params={"location": "the Eel", "do_standard_mgl": 5.0}))
    chart = seen["items"][1].read
    assert [line.label for line in chart.lines] == ["standard"]
    assert result.answer == {"low": 6.0}


def _catchment_listing(monkeypatch) -> str:
    import tempfile
    from pathlib import Path

    path = Path(tempfile.mkdtemp()) / "full_listing.log"
    path.write_text(
        "      RUNOFF_SCS_CN : ACCUMULATED RAINFALL :    0.1000000     M\n"
        "BALANCE OF WATER VOLUME\n FLUX BOUNDARY 1 : -2.0\n FLUX BOUNDARY 2 : 8.0\n"
        " RELATIVE ERROR IN VOLUME AT T = 600.0 S : -1.0E-07\n"
        "BALANCE OF WATER VOLUME\n FLUX BOUNDARY 1 : -6.0\n FLUX BOUNDARY 2 : 1.0\n"
        " RELATIVE ERROR IN VOLUME AT T = 1200.0 S : -1.2E-07\n"
        "                   FINAL BALANCE OF WATER VOLUME  \n"
        "     INITIAL VOLUME              :     0.000000     M3\n"
        "     FINAL VOLUME                :     100.     M3\n"
        "     VOLUME THAT ENTERED THE DOMAIN:    -1200.     M3  ( IF <0 EXIT )\n"
        "     VOLUME ADDED BY SOURCE TERM   :     1300.     M3\n"
        "     TOTAL VOLUME LOST             :   -0.1E-07 M3\n")
    return str(path)


@pytest.fixture()
def catchment(monkeypatch, solved):
    """The reach's result read as a catchment's: two liquid boundaries the settle
    placed, the outlet at the east face, and the listing the engine printed."""
    monkeypatch.setattr(
        "trid3nt_server.workflows.telemac.solving.solve.download_result",
        lambda run_id, basename, error_code=None: _catchment_listing(monkeypatch))
    return _solved({"liquid_boundaries": [
        {"number": 1, "role": "rating_curve", "x": 500120.0, "y": 4400055.0},
        {"number": 2, "role": "inflow", "x": 500000.0, "y": 4400055.0}]})


def test_a_series_of_the_printed_flux_reads_the_boundary_nearest_the_point(catchment):
    """FLUX is the token the module prints rather than writes: the series is the
    listing's own discharge across the liquid boundary the Point lies on,
    outflow-positive, and the station sits at that boundary."""
    from trid3nt_server.workflows.inputs import Point

    lon, lat = catchment.lonlat
    outlet = Point(float(lon[1]), float(lat[1]), "outlet")
    read = T2D.OUTPUTS["series"].read(series("FLUX", at=outlet), catchment)
    assert isinstance(read, Series)
    assert list(read.times) == [600.0, 1200.0] and list(read.values) == [2.0, 6.0]
    assert (read.name, read.units, read.at) == ("FLUX BOUNDARY", "m3/s", "at outlet")
    assert read.lon == pytest.approx(float(lon[1]), abs=1e-6)
    assert read.measures["max"] == 6.0 and read.measures["t_max"] == 1200.0
    assert read.measures["truncated"] is True
    assert read.measures["integral"] == pytest.approx(2400.0)
    # the other face, by a pair rather than a Point, named by its number
    other = T2D.OUTPUTS["series"].read(
        series("FLUX", at=(float(lon[0]), float(lat[0]))), catchment)
    assert list(other.values) == [-8.0, -1.0]
    assert other.at == "at liquid boundary 2"


def test_a_printed_token_needs_a_point_and_a_run_that_placed_its_boundaries(
        catchment, solved):
    with pytest.raises(OutputEmpty, match="needs the Point"):
        T2D.OUTPUTS["series"].read(series("FLUX"), catchment)
    with pytest.raises(OutputEmpty, match="records no liquid boundary"):
        T2D.OUTPUTS["series"].read(series("FLUX", at=(0.0, 0.0)), solved)


def test_the_water_balance_carries_the_final_block_and_the_rain_that_fell(catchment):
    """The volumes are the engine's own, outflow-positive across the boundaries
    like the flux series; the rain volume is the accumulated depth the runoff
    routine printed over the meshed area, and the coefficient their ratio."""
    from trid3nt_server.workflows.telemac.products.run_reads import mesh_area_m2

    read = T2D.OUTPUTS["mass_balance"].read(mass_balance(), catchment)
    area = mesh_area_m2(catchment.result)
    assert read.measures["continuity_rel_error"] == pytest.approx(-1.2e-7)
    assert read.measures["outflow_volume_m3"] == 1200.0
    assert read.measures["source_volume_m3"] == 1300.0
    assert read.measures["rain_depth_m"] == pytest.approx(0.1)
    assert read.measures["rain_volume_m3"] == pytest.approx(0.1 * area, abs=1e-3)
    assert read.measures["runoff_coefficient"] == pytest.approx(1200.0 / (0.1 * area),
                                                                 abs=1e-6)
    assert "boundary_volume_m3" not in read.measures


def test_the_envelope_carries_its_p99_beside_its_maximum_and_the_extent_its_area(
        solved):
    """One pit can set the maximum while the field sits far below it, so the
    99th percentile of the envelope rides beside it."""
    from trid3nt_server.workflows.telemac.products.run_reads import mesh_area_m2

    read = T2D.OUTPUTS["max_over_time"].read(max_over_time("T1"), solved)
    assert read.measures["max"] == 80.0
    assert read.measures["p99"] == pytest.approx(np.percentile([10, 80, 5, 50, 60], 99))
    assert read.measures["truncated"] is False
    box = T2D.OUTPUTS["extent"].read(extent(), solved).measures
    assert box["area_km2"] == pytest.approx(mesh_area_m2(solved.result) / 1.0e6)


def test_a_series_at_a_point_carries_the_station_it_was_read_at(solved):
    from trid3nt_server.workflows.inputs import Point

    lon, lat = solved.lonlat
    read = T2D.OUTPUTS["series"].read(
        series("T1", at=Point(float(lon[1]), float(lat[1]))), solved)
    assert (read.lon, read.lat) == (pytest.approx(float(lon[1])),
                                    pytest.approx(float(lat[1])))
    assert T2D.OUTPUTS["series"].read(series("T1"), solved).lon is None
