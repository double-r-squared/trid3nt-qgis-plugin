"""``drop_minor_divergences`` over real NHDPlus HR flowlines of the Des Moines River braid, offline.

The fixture is the service's own rows (NetworkNHDFlowline, layer 3) around the river's two divergences near
-93.68, 41.65 and -93.66, 41.67, coordinates rounded to 1e-6 degree. Covered: the three lines on minor paths
dropped and every other line kept, each divergence then left by one line, a kept line whose downstream line was
dropped counted, and the missing columns, an unstated order, an all-minor layer and an unknown layer refusing.
"""

from __future__ import annotations

import asyncio
import pathlib

import geopandas as gpd
import pytest

from trid3nt_contracts.execution import LayerURI
from trid3nt_server.tools.derive import DeriveError
from trid3nt_server.tools.derive.drop_minor_divergences.drop_minor_divergences import (
    dendritic, drop_minor_divergences)

_FIXTURE = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "nhd_hr" / "des_moines_braid_flowlines.geojson"
_MINOR = {22000900030701, 22000900024914, 22000900001663}
# Each line ending at a divergence, and the main and minor lines leaving it.
_SPLITS = {22000900019039: (22000900001628, 22000900030701), 22000900010296: (22000900010294, 22000900024914)}


def _layer(tmp_path: pathlib.Path, edit=None) -> LayerURI:
    frame = gpd.read_file(_FIXTURE)
    if edit is not None:
        edit(frame)
    path = tmp_path / "flowlines.geojson"
    frame.to_file(path, driver="GeoJSON")
    return LayerURI(layer_id="flowlines", name="flowlines", layer_type="vector", uri=str(path))


def _kept(tmp_path: pathlib.Path, edit=None) -> gpd.GeoDataFrame:
    return gpd.read_file(dendritic(_layer(tmp_path, edit), _output_dir=str(tmp_path)).uri)


def test_the_lines_on_minor_paths_are_dropped_and_every_other_line_kept(tmp_path):
    source = gpd.read_file(_FIXTURE)
    out = _kept(tmp_path)

    assert set(out["nhdplusid"].astype("int64")) == set(source["nhdplusid"].astype("int64")) - _MINOR
    assert len(out) == len(source) - 3


def test_each_divergence_is_left_by_its_main_line_alone(tmp_path):
    out = _kept(tmp_path)
    ids = set(out["nhdplusid"].astype("int64"))

    for above, (main, minor) in _SPLITS.items():
        tonode = out.loc[out["nhdplusid"].astype("int64") == above, "tonode"].iloc[0]
        leaving = set(out.loc[out["fromnode"] == tonode, "nhdplusid"].astype("int64"))
        assert leaving == {main}
        assert minor not in ids


def test_a_kept_line_running_into_a_dropped_one_is_counted(tmp_path, monkeypatch):
    notes: list[str] = []
    monkeypatch.setattr("trid3nt_server.inputs.run_notes.journal_note", notes.append)

    def tributary_into_the_minor_branch(frame):
        minor = frame.loc[frame["nhdplusid"] == 22000900030701, "hydroseq"].iloc[0]
        frame.loc[frame["nhdplusid"] == 22000900019039, "dnhydroseq"] = minor

    _kept(tmp_path, tributary_into_the_minor_branch)
    assert notes and "3 of 36 flowlines" in notes[0] and "1 kept line(s) ran into a dropped one" in notes[0]


def test_flowlines_without_the_computed_order_refuse_naming_it(tmp_path):
    with pytest.raises(DeriveError, match="streamcalc") as refused:
        _kept(tmp_path, lambda frame: frame.drop(columns=["streamcalc"], inplace=True))
    assert refused.value.error_code == "DROP_MINOR_DIVERGENCES_COLUMNS_MISSING"


def test_a_line_stating_no_order_refuses(tmp_path):
    def blank(frame):
        frame["streamcalc"] = frame["streamcalc"].astype("float64")
        frame.loc[frame["nhdplusid"] == 22000900010294, "streamcalc"] = float("nan")

    with pytest.raises(DeriveError, match="22000900010294") as refused:
        _kept(tmp_path, blank)
    assert refused.value.error_code == "DROP_MINOR_DIVERGENCES_ORDER_UNSTATED"


def test_a_layer_of_minor_paths_only_refuses(tmp_path):
    def all_minor(frame):
        frame.drop(frame.index[~frame["nhdplusid"].isin(list(_MINOR))], inplace=True)

    with pytest.raises(DeriveError) as refused:
        _kept(tmp_path, all_minor)
    assert refused.value.error_code == "DROP_MINOR_DIVERGENCES_EMPTY"


def test_an_unknown_layer_id_refuses_by_name(monkeypatch):
    monkeypatch.setattr("trid3nt_server.render.uri_registry.lookup_layer_for_handle", lambda _h: None)

    with pytest.raises(DeriveError, match="nope") as refused:
        asyncio.run(drop_minor_divergences(flowlines="nope"))
    assert refused.value.error_code == "DROP_MINOR_DIVERGENCES_LAYER_UNKNOWN"


def test_no_flowlines_refuses():
    with pytest.raises(DeriveError) as refused:
        asyncio.run(drop_minor_divergences())
    assert refused.value.error_code == "DROP_MINOR_DIVERGENCES_ARGS_INVALID"
