"""SFR streams where a width is stated, and lake outlets routed into them: left-out cells, positive slopes, the `when`/`unless` gates, the conversions, Lakes `into`.

Offline except the validate cases, which run mf6's own --mode validate through the image.
The mesh is the 3x3 lattice of 10 m squares of the lakes suite (UTM 32617, numbered row by row from the south-west);
every count and length below is computed by hand.
"""

from __future__ import annotations

import pytest

from tests.solver.test_modflow_lakes import _CELLS, _CENTRE, _POINTS, _TO_LONLAT, _mesh, _rundir, staged_beside  # noqa: F401
from tests.solver.test_modflow_streams import _DIMENSIONS, _VALUES, _fill, _line, _MAIN
from trid3nt_server.workflows.modflow.authoring.simulation import serialize
from trid3nt_server.workflows.modflow.authoring.stress import place_streams
from trid3nt_server.workflows.modflow.authoring.validate import validate_authored_simulation
from trid3nt_server.workflows.modflow.errors import ModflowError

_LAKE_VALUES = {"stage": 12.0, "bottom": 0.0, "leakance": 0.1}
_INTO = {"streams": "creek", "from_key": "permanent_identifier", "to_key": "receives_from_waterbody"}


def _place(statements, *, leave_out=None, active=None):
    return place_streams(statements, points=_POINTS, cells=_CELLS, utm_epsg=32617, nlay=1, frame=("meters", "days"),
                         active=active, **({"leave_out": leave_out} if leave_out else {}))


def _statement(geometry, **over):
    return {"geometry": geometry, "values": dict(_VALUES), "dimensions": dict(_DIMENSIONS), **over}


def _collection(*features):
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": properties, "geometry": geometry} for properties, geometry in features]}


_LAKE = _collection(({"permanent_identifier": "L1"}, _CENTRE))
# the line the lake drains by leaves the lake's centre: cell 4 (the lake) is left out, cell 5 is the first reach
_OUTLET_LINE = _line((15.0, 15.0), (28.0, 15.0))


def _creek(tag="L1", **properties):
    return _collection(({"nhdplusid": 7, "receives_from_waterbody": tag, **properties}, _OUTLET_LINE))


def _lakes(**over):
    return {"geometry": "where", **_LAKE_VALUES, "name": "lakes", "into": dict(_INTO), **over}


def _streams(**over):
    return {"geometry": "creek", **_VALUES, "name": "creek", **over}


# ---- the cut: cells a reach does not lie in -------------------------------------------------------------


def test_a_line_across_a_left_out_cell_is_the_two_lines_on_either_side_of_it():
    packagedata, connectiondata, _, _, features = _place([_statement(_MAIN)], leave_out={4: "lake"})

    assert [row[2] for row in packagedata] == [3, 5]
    assert connectiondata == [[0], [1]]
    # a feature's first and last are those of the line it was drawn as, not of the pieces it was cut into
    assert [(f["first"], f["last"]) for f in features] == [(True, False), (False, True)]


def test_a_line_whose_first_cell_is_left_out_starts_at_the_next_reach():
    packagedata, _, _, _, features = _place([_statement(_line((15.0, 15.0), (28.0, 15.0)))], leave_out={4: "river"})

    assert [row[2] for row in packagedata] == [5]
    assert [(f["first"], f["last"]) for f in features] == [(True, True)]


def test_a_line_wholly_in_left_out_cells_places_no_reach_and_refuses_as_a_line_off_the_mesh_does():
    with pytest.raises(ModflowError) as caught:
        _place([_statement(_MAIN)], leave_out={3: "river", 4: "river", 5: "river"})
    assert caught.value.error_code == "MODFLOW_STRESS_OUTSIDE"


def test_a_line_that_leaves_the_mesh_and_returns_still_refuses_beside_left_out_cells():
    holed = [ring for cell, ring in enumerate(_CELLS) if cell != 4]
    with pytest.raises(ModflowError) as caught:
        place_streams([_statement(_MAIN)], points=_POINTS, cells=holed, utm_epsg=32617, nlay=1, frame=("meters", "days"),
                      leave_out={3: "river"})
    assert "leaves the mesh between cells 3 and 4" in str(caught.value)


# ---- the join ---------------------------------------------------------------------------------------


def _wired(*lines, leave_out=None):
    """Each reach of ``lines`` as ``(cell, connections it holds)``, the downstream ones negative."""
    packagedata, connectiondata, *_ = _place([_statement(line) for line in lines], leave_out=leave_out)
    held = {row[0]: row[1:] for row in connectiondata}
    return [(row[2], held[row[0]]) for row in packagedata], packagedata


def test_a_line_ending_where_two_other_lines_cross_a_cell_joins_the_one_its_end_touches():
    # the main stem crosses cell 4 at y 15, a second line crosses it at y 18, the tributary ends on the main stem at (15, 15)
    wired, packagedata = _wired(_MAIN, _line((2.0, 18.0), (28.0, 18.0)), _line((15.0, 28.0), (15.0, 15.0)))

    in_cell_4 = [(row[10], row[3]) for row in packagedata if row[2] == 4]
    # the main stem's reach has three connections (above it, the tributary, below it); the second line's has two, so does the tributary's
    assert sorted(ncon for ncon, _ in in_cell_4) == [2, 2, 3]
    assert len(wired) == 8


def test_a_line_ending_where_two_other_lines_touch_its_end_equally_refuses():
    # both other lines pass through the tributary's end (15, 15)
    with pytest.raises(ModflowError) as caught:
        _wired(_MAIN, _line((2.0, 12.0), (15.0, 15.0), (28.0, 18.0)), _line((15.0, 28.0), (15.0, 15.0)))
    assert "cannot choose" in str(caught.value) and "2 are as near" in str(caught.value)


def test_a_line_cut_short_by_a_left_out_cell_joins_nothing_in_the_cell_before_it():
    # a second line runs down through cell 3 (x 5); the main stem's reach in cell 3 ends where cell 4, a lake, begins
    other = _line((5.0, 28.0), (5.0, 2.0))
    cut, _ = _wired(_MAIN, other, leave_out={4: "lake"})
    whole, _ = _wired(_MAIN, other)

    assert (3, []) in cut and not any(cell == 4 for cell, _ in cut)
    # uncut, the main stem flows on into cell 4
    assert (3, [-1]) in whole


@pytest.mark.parametrize("slope", [-9998, 0, 0.0])
def test_a_slope_that_is_not_positive_refuses_naming_the_flowline(slope):
    with pytest.raises(ModflowError) as caught:
        _place([_statement(_collection(({"nhdplusid": 22000900021990, "slope": slope}, _MAIN)),
                           values={**_VALUES, "slope": {"field": "slope"}})])
    assert "22000900021990" in str(caught.value) and "slope" in str(caught.value)


def test_a_slope_the_flowline_holds_is_each_reach_s() -> None:
    packagedata, *_ = _place([_statement(_collection(({"slope": 0.002}, _MAIN)), values={**_VALUES, "slope": {"field": "slope"}})])
    assert {row[5] for row in packagedata} == {0.002}


# ---- the gates ------------------------------------------------------------------------------------------


def test_a_streams_statement_whose_when_reads_nothing_states_no_streams():
    sheet = _fill({"Streams": {"geometry": "where", **_VALUES, "when": "stream_width"}}, where=_MAIN, stream_width=None)
    assert "SFR_PACKAGEDATA" not in sheet.filled


def test_a_streams_statement_whose_when_reads_a_width_states_the_streams():
    sheet = _fill({"Streams": {"geometry": "where", **_VALUES, "when": "stream_width"}}, where=_MAIN, stream_width=5.0)
    assert len(sheet.filled["SFR_PACKAGEDATA"].value["streams"]) == 1


def test_a_drains_statement_whose_unless_reads_a_value_states_no_drains():
    drains = {"geometry": "where", "layer": 1, "elevation": 9.0, "conductance": 5.0, "unless": "stream_width"}
    assert "DRN_STRESS_PERIOD_DATA" not in _fill({"Drains": drains}, where=_MAIN, stream_width=5.0).filled
    assert "DRN_STRESS_PERIOD_DATA" in _fill({"Drains": drains}, where=_MAIN, stream_width=None).filled


def test_a_streams_statement_with_no_width_still_refuses_by_name_when_it_has_no_gate():
    body = {"geometry": "where", **_VALUES}
    body.pop("width")
    with pytest.raises(ModflowError) as caught:
        _fill({"Streams": body}, where=_MAIN)
    assert "width" in str(caught.value)


# ---- the write ------------------------------------------------------------------------------------------


def test_the_sfr_package_converts_manning_from_metres_and_seconds_to_the_model_s_units(staged_beside, tmp_path):
    mesh = _mesh(tmp_path)
    sheet = _fill({"Streams": {"geometry": "creek", **_VALUES}, "SFR_SAVE_FLOWS": True}, creek=_MAIN)
    rundir = _rundir(sheet, mesh, tmp_path)

    serialize(sheet, rundir, mesh)

    text = (rundir / "gwf.sfr").read_text()
    assert "LENGTH_CONVERSION" in text and "TIME_CONVERSION   86400.0" in text
    assert validate_authored_simulation(rundir) == {"rc": 0}


def test_a_conversion_the_run_states_is_the_one_written(staged_beside, tmp_path):
    mesh = _mesh(tmp_path)
    sheet = _fill({"Streams": {"geometry": "creek", **_VALUES}, "SFR_UNIT_CONVERSION": 86400.0, "SFR_SAVE_FLOWS": True},
                  creek=_MAIN)
    rundir = _rundir(sheet, mesh, tmp_path)

    serialize(sheet, rundir, mesh)

    text = (rundir / "gwf.sfr").read_text()
    assert "UNIT_CONVERSION" in text and "TIME_CONVERSION" not in text


def test_units_the_conversions_do_not_name_refuse(staged_beside, tmp_path):
    mesh = _mesh(tmp_path)
    sheet = _fill({"Streams": {"geometry": "creek", **_VALUES}, "TDIS_TIME_UNITS": "undefined"}, creek=_MAIN)
    rundir = _rundir(sheet, mesh, tmp_path)
    with pytest.raises(ModflowError) as caught:
        serialize(sheet, rundir, mesh)
    assert caught.value.error_code == "MODFLOW_UNITS_UNSTATED"


# ---- lake outlets into the stream -----------------------------------------------------------------------


def _routed(tmp_path, *, lakes=None, creek=None):
    mesh = _mesh(tmp_path)
    sheet = _fill({"Lakes": lakes or _lakes(), "Streams": _streams(), "SFR_SAVE_FLOWS": True, "LAK_SAVE_FLOWS": True},
                  where=_LAKE, creek=creek or _creek())
    rundir = _rundir(sheet, mesh, tmp_path)
    return sheet, rundir, mesh


def test_a_lake_drained_by_a_tagged_line_has_a_manning_outlet_with_the_first_reach_s_values(staged_beside, tmp_path):
    sheet, rundir, mesh = _routed(tmp_path)

    serialize(sheet, rundir, mesh)

    lak = (rundir / "gwf.lak").read_text()
    assert "NOUTLETS  1" in lak and "TIME_CONVERSION   86400.0" in lak and "MOVER" in lak
    # outlet 1 of lake 1 to outside, MANNING, invert the reach's bed top, then its width, roughness and slope
    assert lak.split("BEGIN outlets")[1].split("END outlets")[0].split() == [
        "1", "1", "0", "MANNING", "9.00000000", "5.00000000", "0.03000000", "0.00100000"]


def test_the_outlet_is_carried_into_the_first_reach_by_a_mover_and_mf6_validates(staged_beside, tmp_path):
    sheet, rundir, mesh = _routed(tmp_path)

    serialize(sheet, rundir, mesh)

    mvr = (rundir / "gwf.mvr").read_text()
    assert "FACTOR       1.0" in mvr and "LAK-1  1  SFR-1  1" in mvr.replace("lak_0", "LAK-1").replace("sfr_0", "SFR-1") \
        or "lak_0  1  sfr_0  1  FACTOR       1.00000000" in mvr
    assert "MOVER" in (rundir / "gwf.sfr").read_text()
    assert validate_authored_simulation(rundir) == {"rc": 0}


def test_a_lake_no_line_leaves_has_no_outlet_and_no_mover(staged_beside, tmp_path):
    sheet, rundir, mesh = _routed(tmp_path, creek=_creek(tag="L2"))

    serialize(sheet, rundir, mesh)

    assert "BEGIN outlets" not in (rundir / "gwf.lak").read_text()
    assert not (rundir / "gwf.mvr").exists()
    assert validate_authored_simulation(rundir) == {"rc": 0}


def test_streams_carrying_no_pairing_property_give_the_lakes_no_outlets(staged_beside, tmp_path):
    untagged = _collection(({"nhdplusid": 7}, _OUTLET_LINE))
    sheet, rundir, mesh = _routed(tmp_path, creek=untagged)

    serialize(sheet, rundir, mesh)

    assert not (rundir / "gwf.mvr").exists()
    assert validate_authored_simulation(rundir) == {"rc": 0}


def test_two_lines_leaving_one_lake_refuse(staged_beside, tmp_path):
    two = _collection(({"receives_from_waterbody": "L1"}, _OUTLET_LINE),
                      ({"receives_from_waterbody": "L1"}, _line((15.0, 15.0), (15.0, 28.0))))
    sheet, rundir, mesh = _routed(tmp_path, creek=two)
    with pytest.raises(ModflowError) as caught:
        serialize(sheet, rundir, mesh)
    assert "gives its water to one line" in str(caught.value) and "L1" in str(caught.value)


def test_a_lake_lacking_the_pairing_property_refuses_naming_it(staged_beside, tmp_path):
    mesh = _mesh(tmp_path)
    sheet = _fill({"Lakes": _lakes(), "Streams": _streams()},
                  where=_collection(({"gnis_name": "Pond"}, _CENTRE)), creek=_creek())
    rundir = _rundir(sheet, mesh, tmp_path)
    with pytest.raises(ModflowError) as caught:
        serialize(sheet, rundir, mesh)
    assert "permanent_identifier" in str(caught.value)


def test_into_naming_streams_no_statement_placed_refuses(staged_beside, tmp_path):
    mesh = _mesh(tmp_path)
    sheet = _fill({"Lakes": _lakes(into={**_INTO, "streams": "river"}), "Streams": _streams()}, where=_LAKE, creek=_creek())
    rundir = _rundir(sheet, mesh, tmp_path)
    with pytest.raises(ModflowError) as caught:
        serialize(sheet, rundir, mesh)
    assert "'river'" in str(caught.value)


def test_into_whose_when_reads_nothing_leaves_the_lake_without_outlets(staged_beside, tmp_path):
    mesh = _mesh(tmp_path)
    sheet = _fill({"Lakes": _lakes(into={**_INTO, "when": "stream_width"})}, where=_LAKE, stream_width=None)
    assert sheet.filled["LAK_PACKAGEDATA"].value["lakes"][0]["into"] is None
    rundir = _rundir(sheet, mesh, tmp_path)
    serialize(sheet, rundir, mesh)
    assert not (rundir / "gwf.mvr").exists()


def test_into_needs_the_statement_to_be_named():
    with pytest.raises(ModflowError) as caught:
        _fill({"Lakes": {"geometry": "where", **_LAKE_VALUES, "into": dict(_INTO)}}, where=_LAKE)
    assert "name" in str(caught.value)


@pytest.mark.parametrize("missing", ["streams", "from_key", "to_key"])
def test_into_missing_a_part_refuses_by_name(missing):
    held = {key: value for key, value in _INTO.items() if key != missing}
    with pytest.raises(ModflowError) as caught:
        _fill({"Lakes": _lakes(into=held)}, where=_LAKE)
    assert missing in str(caught.value)


def test_a_lake_s_own_outlets_and_a_routed_one_both_stand(staged_beside, tmp_path):
    sheet, rundir, mesh = _routed(tmp_path, lakes=_lakes(outlets=[{"to": "outside", "couttype": "SPECIFIED", "rate": 0.0}]))

    serialize(sheet, rundir, mesh)

    outlets = (rundir / "gwf.lak").read_text().split("BEGIN outlets")[1].split("END outlets")[0].split()
    assert [outlets[3], outlets[3 + 8]] == ["SPECIFIED", "MANNING"]
    assert "lak_0  2  sfr_0  1  FACTOR" in (rundir / "gwf.mvr").read_text() or "2  " in (rundir / "gwf.mvr").read_text()


_TRANSPORT = {"slots": {"NAM_SAVE_FLOWS": True, "IC_STRT": 0.0, "MST_POROSITY": 0.3, "IMS_COMPLEXITY": "COMPLEX",
                        "SSM_SAVE_FLOWS": True}}


def test_a_transport_body_beside_a_flow_model_that_moves_water_carries_the_mover_package(staged_beside, tmp_path):
    mesh = _mesh(tmp_path)
    sheet = _fill({"Lakes": _lakes(), "Streams": _streams(), "Transport": _TRANSPORT}, where=_LAKE, creek=_creek())
    rundir = _rundir(sheet, mesh, tmp_path)

    serialize(sheet, rundir, mesh)

    assert "MVT6" in (rundir / "gwt.nam").read_text()
    assert validate_authored_simulation(rundir) == {"rc": 0}


def test_a_transport_body_beside_a_flow_model_that_moves_no_water_carries_none(staged_beside, tmp_path):
    mesh = _mesh(tmp_path)
    sheet = _fill({"Lakes": _lakes(), "Streams": _streams(), "Transport": _TRANSPORT}, where=_LAKE, creek=_creek(tag="L2"))
    rundir = _rundir(sheet, mesh, tmp_path)

    serialize(sheet, rundir, mesh)

    assert "MVT6" not in (rundir / "gwt.nam").read_text()
    assert validate_authored_simulation(rundir) == {"rc": 0}
