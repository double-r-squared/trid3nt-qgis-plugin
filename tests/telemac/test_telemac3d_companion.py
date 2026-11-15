"""TELEMAC-3D publishes what its 2D companion file writes.

The companion's variable list is the one a real run's res2d_basin.slf carried
(the stratified_flow canary on the Hagg week): VELOCITY U, VELOCITY V, WATER
DEPTH, BOTTOM. The reads are stated through ``telemac_result``; the publisher is
stood in for, so what is proved is which file each row is read from."""

from __future__ import annotations

import asyncio

import pytest

from trid3nt_contracts.execution import LayerURI

from trid3nt_server.render.formats import Published
from trid3nt_server.workflows.telemac.modules import T3D, fill

_TRACER = "TEMPERATURE     DEGC"
_X = [500000.0, 500120.0, 500000.0, 500120.0, 500060.0]
_Y = [4400000.0, 4400000.0, 4400110.0, 4400110.0, 4400055.0]
_IKLE = [[0, 1, 4], [1, 3, 4], [2, 3, 4], [0, 2, 4]]


def _sheet(**stated):
    return fill(T3D, NUMBER_OF_TRACERS=1, NAMES_OF_TRACERS=[_TRACER],
                RD_RESULT_FILE="res3d_basin.slf", **stated)


def test_the_companion_rows_exist_where_the_deck_names_its_2d_file():
    named = _sheet(ED_RESULT_FILE="res2d_basin.slf")
    bare = _sheet()
    rows = {token: (module, row.file) for token, module, row in named.published()}
    assert rows["H"] == ("telemac3d", "ED_RESULT_FILE")
    assert rows["B"] == ("telemac3d", "ED_RESULT_FILE")
    assert "H" not in {token for token, _, _ in bare.published()}


def test_the_companion_rows_are_no_3d_printout():
    """H and B are mnemonics of the 2D keyword; written into the 3D one the engine stops."""
    named = _sheet(ED_RESULT_FILE="res2d_basin.slf")
    assert named.printouts() == {
        "VARIABLES FOR 3D GRAPHIC PRINTOUTS": "Z,U,V,W,TA1"}


@pytest.fixture()
def published(monkeypatch, fake_s3, telemac_result):
    seen = []

    async def _publish(*, run_id, engine, name, items):
        seen.extend(items)
        return Published(layers=(LayerURI(
            layer_id="L", name="Water depth", layer_type="mesh",
            uri="s3://runs/RID/res2d_basin.slf", quantity="water_depth"),))

    monkeypatch.setattr("trid3nt_server.workflows.runtime.primitives.publish",
                        _publish)
    three = telemac_result(
        varnames=["ELEVATION Z", "VELOCITY U", "VELOCITY V", "VELOCITY W",
                  "TEMPERATURE"],
        varunits=["M", "M/S", "M/S", "M/S", "DEGC"], nplan=2,
        x=_X, y=_Y, ikle=_IKLE, times=[0.0, 360.0],
        data={name: [[1.0] * 10, [2.0] * 10] for name in
              ["ELEVATION Z", "VELOCITY U", "VELOCITY V", "VELOCITY W",
               "TEMPERATURE"]})
    two = telemac_result(
        varnames=["VELOCITY U", "VELOCITY V", "WATER DEPTH", "BOTTOM"],
        varunits=["M/S", "M/S", "M", "M"],
        x=_X, y=_Y, ikle=_IKLE, times=[0.0, 360.0],
        data={"VELOCITY U": [[0.1] * 5] * 2, "VELOCITY V": [[0.2] * 5] * 2,
              "WATER DEPTH": [[3.0] * 5, [3.5] * 5], "BOTTOM": [[-3.0] * 5] * 2})
    files = {"res3d_basin.slf": three, "res2d_basin.slf": two}
    monkeypatch.setattr(
        "trid3nt_server.workflows.solver.solver.download_result",
        lambda run_id, basename, error_code=None: basename)
    monkeypatch.setattr(
        "trid3nt_server.workflows.telemac.modules.outputs.read_selafin",
        lambda path, **_: files[path])
    sheet = _sheet(ED_RESULT_FILE="res2d_basin.slf")
    run = {"run_id": "RID", "utm_epsg": 32610, "result_basename": "res3d_basin.slf",
           "module": "telemac3d", "name": "basin", "sheet": sheet.state(),
           "tracer_names": [_TRACER],
           "reference_time": "2026-01-01T00:00:00+00:00",
           "module_output": [{"token": token, "module": module, "file": row.file,
                              "style": row.style, "varies": row.varies,
                              "has_edge": bool(row.has_edge),
                              "injected": bool(row.injected)}
                             for token, module, row in sheet.published()]}
    return seen, run


def test_the_companion_s_depth_and_bottom_are_published_from_its_own_file(published):
    from trid3nt_server.workflows.telemac import workflow as door

    seen, run = published
    asyncio.run(door.publish_outputs(run=run, outputs=[], captions={}, params={}))
    drawn = {item.product.group: item.product.file for item in seen
             if hasattr(item.product, "group")}
    assert drawn["WATER DEPTH"] == "res2d_basin.slf"
    # a row that does not vary is its last frame, named by the instant
    assert drawn["Bottom at t = 360 s"] == "res2d_basin.slf"
    assert drawn["ELEVATION Z"] == "res3d_basin.slf"
    # a spelling both files carry is one layer, read off the 3D file's row
    assert drawn["VELOCITY U"] == "res3d_basin.slf"
