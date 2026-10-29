"""``mark_lake_outlets`` over real NHDPlus HR features of the Cape Cod box, offline.

The fixtures are the service's own rows (Johns Pond, Coonamessett Pond and Parker Road Pond with the artificial
paths inside them and the lines below), simplified in geometry only. Covered: the two-hop join tagging the line below
each lake's last in-lake path, a lake with two outlets tagging both, an in-lake line of a lake absent from the
waterbody layer tagging nothing, a line two lakes run into refusing, and the missing columns and unknown layer id
refusing by name.
"""

from __future__ import annotations

import asyncio
import pathlib

import geopandas as gpd
import pytest

from trid3nt_contracts.execution import LayerURI
from trid3nt_server.tools.derive import DeriveError
from trid3nt_server.tools.derive.mark_lake_outlets.mark_lake_outlets import RECEIVES, mark_lake_outlets, marked

_FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "nhd_hr"
_JOHNS, _COONAMESSETT, _PARKER_ROAD = "136693229", "136693233", "136665259"


def _layer(path: pathlib.Path, name: str) -> LayerURI:
    return LayerURI(layer_id=name, name=name, layer_type="vector", uri=str(path))


def _lines(tmp_path: pathlib.Path, edit=None) -> LayerURI:
    frame = gpd.read_file(_FIXTURES / "cape_cod_lake_flowlines.geojson")
    if edit is not None:
        edit(frame)
    path = tmp_path / "flowlines.geojson"
    frame.to_file(path, driver="GeoJSON")
    return _layer(path, "flowlines")


def _lakes(tmp_path: pathlib.Path, drop: tuple[str, ...] = ()) -> LayerURI:
    frame = gpd.read_file(_FIXTURES / "cape_cod_lakes.geojson")
    path = tmp_path / "waterbodies.geojson"
    frame[~frame["permanent_identifier"].isin(drop)].to_file(path, driver="GeoJSON")
    return _layer(path, "waterbodies")


def _read(layer: LayerURI) -> gpd.GeoDataFrame:
    return gpd.read_file(layer.uri)


def test_the_line_below_each_lakes_last_path_receives_from_that_lake(tmp_path):
    out = _read(marked(_lines(tmp_path), _lakes(tmp_path), _output_dir=str(tmp_path)))

    tagged = out[out[RECEIVES].notna()]
    assert sorted(zip(tagged["gnis_name"], tagged[RECEIVES])) == sorted([
        ("Childs River", _JOHNS), ("Quashnet River", _JOHNS),
        ("Coonamessett River", _COONAMESSETT), ("Coonamessett River", _PARKER_ROAD)])
    assert len(out) == 17 and set(tagged["ftype"]) == {460}


def test_no_line_inside_a_lake_is_tagged(tmp_path):
    out = _read(marked(_lines(tmp_path), _lakes(tmp_path), _output_dir=str(tmp_path)))

    assert out.loc[out["wbarea_permanent_identifier"].notna(), RECEIVES].isna().all()


def test_a_lake_the_waterbody_layer_lacks_marks_nothing(tmp_path):
    out = _read(marked(_lines(tmp_path), _lakes(tmp_path, drop=(_PARKER_ROAD,)), _output_dir=str(tmp_path)))

    assert _PARKER_ROAD not in set(out[RECEIVES].dropna())
    assert {_JOHNS, _COONAMESSETT} == set(out[RECEIVES].dropna())


def test_a_line_that_two_lakes_run_into_refuses_naming_both(tmp_path):
    def merge(frame):
        outlet = frame.loc[frame["gnis_name"] == "Childs River", "hydroseq"].iloc[0]
        frame.loc[frame["wbarea_permanent_identifier"] == _COONAMESSETT, "dnhydroseq"] = outlet

    with pytest.raises(DeriveError, match=f"{_JOHNS}|{_COONAMESSETT}") as refused:
        marked(_lines(tmp_path, merge), _lakes(tmp_path), _output_dir=str(tmp_path))
    assert refused.value.error_code == "MARK_LAKE_OUTLETS_AMBIGUOUS"


def test_flowlines_without_topology_refuse_naming_the_columns(tmp_path):
    bare = _lines(tmp_path, lambda frame: frame.drop(columns=["dnhydroseq"], inplace=True))

    with pytest.raises(DeriveError, match="dnhydroseq") as refused:
        marked(bare, _lakes(tmp_path), _output_dir=str(tmp_path))
    assert refused.value.error_code == "MARK_LAKE_OUTLETS_COLUMNS_MISSING"


def test_an_unknown_layer_id_refuses(tmp_path):
    with pytest.raises(DeriveError, match="names no layer") as refused:
        marked("no-such-layer", _lakes(tmp_path), _output_dir=str(tmp_path))
    assert refused.value.error_code == "MARK_LAKE_OUTLETS_LAYER_UNKNOWN"


def test_the_tool_needs_both_layers():
    with pytest.raises(DeriveError, match="needs the flowlines and the waterbodies"):
        asyncio.run(mark_lake_outlets(flowlines="x"))
