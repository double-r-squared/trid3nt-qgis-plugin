"""Measures and legends under the WET MASK, and the edge a row declares.

Offline: the result a read opens is stated through the ``telemac_result``
fixture. What is proved is that a film on a drying bar moves neither a number nor
a legend, and that whether a variable is masked at an edge is its row's word.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from trid3nt_server.workflows.telemac.modules import T2D, field, max_over_time, series
from trid3nt_server.workflows.telemac.modules.module import Output
from trid3nt_server.workflows.telemac.modules.outputs import (
    OutputEmpty,
    Solved,
    profile,
)
from trid3nt_server.workflows.telemac.modules.sheet import fill

#: Four nodes the run holds two metres of water on, and one bank film a
#: centimetre deep - the shape that put 45 C into a reach-wide answer.
_WET_NODES = 4
_FILM_DEPTH_M = 0.01


def _table(tracers: int = 1) -> list[dict[str, Any]]:
    sheet = fill(T2D, NUMBER_OF_TRACERS=tracers,
                 NAMES_OF_TRACERS=["TEMPERATURE     DEG"][:tracers])
    return [{"token": token, "module": module, "style": row.style,
             "varies": row.varies, "has_edge": bool(row.has_edge),
             "injected": bool(row.injected)}
            for token, module, row in sheet.published()]


@pytest.fixture()
def filmed(monkeypatch, telemac_result):
    """A reach with a hot film on a drying bar: node 4 is a centimetre deep and
    reads 45 C while the channel sits between 10 and 12."""
    x = [500000.0, 500120.0, 500000.0, 500120.0, 500060.0]
    y = [4400000.0, 4400000.0, 4400110.0, 4400110.0, 4400055.0]
    depth = [[2.0] * _WET_NODES + [_FILM_DEPTH_M]] * 2
    temperature = [[10.0, 10.5, 11.0, 11.5, 45.0],
                   [10.5, 11.0, 11.5, 12.0, 45.0]]
    telemac_result(varnames=["WATER DEPTH", "TEMPERATURE"], x=x, y=y,
                   ikle=[[0, 1, 4], [1, 3, 4], [2, 3, 4], [0, 2, 4]],
                   times=[0.0, 60.0],
                   data={"WATER DEPTH": depth, "TEMPERATURE": temperature})
    monkeypatch.setattr(
        "trid3nt_server.workflows.solver.solver.download_result",
        lambda run_id, basename, error_code=None: "/tmp/does-not-matter.slf")
    return Solved({"run_id": "RID", "utm_epsg": 32610, "result_basename": "r2d.slf",
                   "module": "telemac2d", "name": "reach", "mesh_size_m": 7.5,
                   "tracer_names": ["TEMPERATURE     DEG"],
                   "module_output": _table(),
                   "started_at": "2026-01-01T00:00:00+00:00"}, T2D)


def test_the_series_is_the_maximum_over_the_nodes_the_run_held_water_on(filmed):
    read = T2D.READS["series"](series("T1"), filmed)
    assert list(read.values) == [11.5, 12.0]
    assert read.measures["max"] == 12.0 and read.measures["min"] == 10.0


def test_a_field_at_an_instant_is_measured_over_the_same_nodes(filmed):
    read = T2D.READS["field"](field("T1", t=-1), filmed)
    assert read.measures["nodes"] == _WET_NODES
    assert read.measures["max"] == 12.0
    # the film is still DRAWN - it is what the run produced - and simply is not
    # what the number is about
    assert list(read.values)[-1] == 45.0
    assert list(read.wet) == [True] * _WET_NODES + [False]


def test_the_envelope_reads_each_node_over_the_instants_it_held_water(filmed):
    read = T2D.READS["max_over_time"](max_over_time("T1"), filmed)
    assert read.measures["max"] == 12.0
    assert read.measures["p99"] == pytest.approx(12.0, abs=0.1)


def test_the_temporal_layers_legend_is_ranged_over_the_same_nodes(filmed):
    """The picture and the number agree by construction: the legend spans the
    water, and the film saturates at the top rather than owning the ramp."""
    from trid3nt_server.workflows.telemac.modules.outputs import deliver

    primitive = field("T1", t="every").animate()
    read = T2D.READS["field"](primitive, filmed)
    item = deliver(primitive, read, filmed, caption="water temperature",
                   name="reach", where="the river")
    assert item.product.value_range == pytest.approx((10.0, 12.0))


def test_a_profile_down_the_reach_leaves_the_bank_film_off_it(monkeypatch,
                                                              telemac_result):
    """The reach-wide answer this mask exists for: a bank a centimetre deep
    heats to 45 C, and the swing ALONG the channel is 0.7 C, not 35."""
    from pyproj import Transformer

    channel = [(500000.0 + 30.0 * i, 4400000.0) for i in range(8)]
    bank = [(x, 4400030.0) for x, _ in channel]
    x = [p[0] for p in channel + bank]
    y = [p[1] for p in channel + bank]
    cells = [[i, i + 1, 8 + i] for i in range(7)] \
        + [[i + 1, 9 + i, 8 + i] for i in range(7)]
    depth = [[2.0] * 8 + [_FILM_DEPTH_M] * 8] * 2
    warm = [10.0 + 0.1 * i for i in range(8)]
    temperature = [warm + [45.0] * 8, warm + [45.0] * 8]
    telemac_result(varnames=["WATER DEPTH", "TEMPERATURE", "VELOCITY U",
                             "VELOCITY V"],
                   x=x, y=y, ikle=cells, times=[0.0, 60.0],
                   data={"WATER DEPTH": depth, "TEMPERATURE": temperature,
                         "VELOCITY U": [[0.5] * 16] * 2,
                         "VELOCITY V": [[0.0] * 16] * 2})
    monkeypatch.setattr(
        "trid3nt_server.workflows.solver.solver.download_result",
        lambda run_id, basename, error_code=None: "/tmp/does-not-matter.slf")
    solved = Solved({"run_id": "RID", "utm_epsg": 32610,
                     "result_basename": "r2d.slf", "module": "telemac2d",
                     "tracer_names": ["TEMPERATURE     DEG"],
                     "module_output": _table(), "name": "reach"}, T2D)
    back = Transformer.from_crs(32610, 4326, always_xy=True)
    line = {"type": "LineString",
            "coordinates": [list(back.transform(px, py))
                            for px, py in (channel[0], channel[-1])]}
    read = T2D.READS["profile"](profile("T1", along=line), solved)
    assert read.measures["max"] == pytest.approx(10.7, abs=0.05)
    assert read.measures["range"] == pytest.approx(0.7, abs=0.05)


def test_a_variable_the_run_never_wet_answers_with_why_rather_than_a_number(
        monkeypatch, telemac_result):
    telemac_result(varnames=["WATER DEPTH", "TEMPERATURE"],
                   x=[0.0, 1.0, 0.0], y=[0.0, 0.0, 1.0], ikle=[[0, 1, 2]],
                   times=[0.0, 1.0],
                   data={"WATER DEPTH": [[0.0] * 3] * 2,
                         "TEMPERATURE": [[45.0] * 3] * 2})
    monkeypatch.setattr(
        "trid3nt_server.workflows.solver.solver.download_result",
        lambda run_id, basename, error_code=None: "/tmp/does-not-matter.slf")
    solved = Solved({"run_id": "RID", "utm_epsg": 32610,
                     "result_basename": "r2d.slf", "module": "telemac2d",
                     "tracer_names": ["TEMPERATURE     DEG"],
                     "module_output": _table(), "name": "dry"}, T2D)
    with pytest.raises(OutputEmpty, match="never read on a node"):
        T2D.READS["series"](series("T1"), solved)


def test_the_domains_own_rows_are_not_masked_by_the_water(filmed):
    """A row that does not vary in time is the DOMAIN's - the bed it was cut
    from - and is defined where there is no water at all."""
    assert filmed.varies("B") is False
    assert filmed.mask_for("B", np.zeros((2, 5))) is None
    assert filmed.mask_for("T1", np.zeros((2, 5))) is not None


def test_whether_a_variable_is_masked_at_an_edge_is_its_rows_word(filmed):
    """An injected plume masks below a fraction of its magnitude; a background
    variable like oxygen or temperature is drawn whole."""
    assert filmed.has_edge("T1") is True
    assert filmed.has_edge("H") is False
    appended = Output("DISSOLVED O2", "mgO2/L", has_edge=False)
    assert appended.has_edge is False
    # a row that says nothing leaves it to the table it is read off
    assert Output("FROUDE NUMBER", "").has_edge is None
