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
