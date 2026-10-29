"""``fetch_nhd_hr_waterbodies`` and ``fetch_nhdplus_hr_flowline_network``: the box in, NHD features with their identity out.

Offline. The driver read is replaced by recorded NHDPlus HR features (three
waterbodies, four flowlines from the Des Moines box), so what is covered is the
query the row makes, the columns it publishes and the ceiling that refuses
rather than truncating, beside the words a template's need selects them by.
"""

from __future__ import annotations

import pathlib
from urllib.parse import parse_qs, urlsplit

import geopandas as gpd
import pytest

from trid3nt_server.tools.fetchers._router.errors import RouterInputError
from trid3nt_server.tools.fetchers._router.executors import vector_ogr
from trid3nt_server.tools.fetchers._router.spec import compose_specs_from_tree
from trid3nt_server.tools.search.match import Need, match, sources_with_coverage

_FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "nhd_hr"
_BOX = (-93.75, 41.45, -93.45, 41.70)

_WATERBODY_COLUMNS = ["nhdplusid", "permanent_identifier", "ftype", "fcode",
                      "gnis_name", "areasqkm", "reachcode"]
_FLOWLINE_COLUMNS = ["nhdplusid", "gnis_name", "ftype", "fcode", "hydroseq",
                     "dnhydroseq", "levelpathi", "fromnode", "tonode",
                     "wbarea_permanent_identifier", "streamorde", "totdasqkm",
                     "slope", "lengthkm", "minelevsmo", "maxelevsmo"]


@pytest.fixture(scope="module")
def specs():
    return compose_specs_from_tree()


def _query(spec) -> dict[str, str]:
    url = vector_ogr.build_query(
        spec, _BOX, where=vector_ogr.build_where(spec, {"bbox": _BOX}),
        page_size=2000)
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def test_waterbodies_ask_only_lakes_and_reservoirs_with_their_identity(specs):
    q = _query(specs["fetch_nhd_hr_waterbodies"])
    assert q["where"] == "ftype IN (390, 436)"
    assert q["outFields"].split(",") == _WATERBODY_COLUMNS
    assert "elevation" not in q["outFields"]
    assert q["geometry"] == "-93.75,41.45,-93.45,41.7"
    assert "MapServer/9/query" in specs["fetch_nhd_hr_waterbodies"].endpoints["data"].url


def test_flowlines_ask_the_whole_network_with_topology(specs):
    q = _query(specs["fetch_nhdplus_hr_flowline_network"])
    assert q["where"] == "1=1"
    assert q["outFields"].split(",") == _FLOWLINE_COLUMNS
    assert "MapServer/3/query" in \
        specs["fetch_nhdplus_hr_flowline_network"].endpoints["data"].url


def _driver(monkeypatch, fixture: str, capture: list | None = None):
    frame = gpd.read_file(_FIXTURES / fixture)

    def read(path, **kwargs):
        if capture is not None:
            capture.append(kwargs)
        return frame

    import pyogrio
    monkeypatch.setattr(pyogrio, "read_dataframe", read)
    return frame


@pytest.mark.parametrize("name,fixture,columns", [
    ("fetch_nhd_hr_waterbodies", "waterbodies.geojson", _WATERBODY_COLUMNS),
    ("fetch_nhdplus_hr_flowline_network", "flowlines.geojson", _FLOWLINE_COLUMNS)])
def test_every_feature_the_driver_read_reaches_the_file(
        specs, monkeypatch, name, fixture, columns):
    frame = _driver(monkeypatch, fixture)
    spec = specs[name]
    data = vector_ogr.execute(spec, {"bbox": _BOX})
    import io
    out = gpd.read_file(io.BytesIO(data))
    assert len(out) == len(frame)
    assert list(out.columns) == columns + ["geometry"]
    assert sorted(out.nhdplusid) == sorted(frame.nhdplusid)


def test_a_read_that_reaches_the_ceiling_refuses_rather_than_truncating(
        specs, monkeypatch):
    frame = _driver(monkeypatch, "waterbodies.geojson")
    spec = specs["fetch_nhd_hr_waterbodies"]
    spec = spec.model_copy(update={"gates": spec.gates.model_copy(
        update={"max_features": len(frame)})})
    with pytest.raises(RouterInputError) as excinfo:
        vector_ogr.execute(spec, {"bbox": _BOX})
    assert excinfo.value.error_code.endswith("RESULT_TOO_LARGE")


def test_an_empty_box_refuses_by_name(specs, monkeypatch):
    frame = _driver(monkeypatch, "waterbodies.geojson")
    import pyogrio
    monkeypatch.setattr(pyogrio, "read_dataframe", lambda *a, **k: frame.iloc[0:0])
    with pytest.raises(Exception) as excinfo:
        vector_ogr.execute(specs["fetch_nhd_hr_waterbodies"], {"bbox": _BOX})
    assert excinfo.value.error_code == "NHD_HR_WATERBODIES_EMPTY"


@pytest.mark.parametrize("word,geometry,name", [
    ("waterbodies", "polygon", "fetch_nhd_hr_waterbodies"),
    ("flowline network", "polyline", "fetch_nhdplus_hr_flowline_network")])
def test_a_need_by_word_and_shape_is_answered_by_the_row(word, geometry, name):
    for lon, lat in ((None, None), (-93.6, 41.6)):
        choice = match(Need(slot="s", data_class="hydrography", of=word,
                            geometry=geometry, lon=lon, lat=lat),
                       sources_with_coverage())
        assert choice.picked == name


@pytest.mark.parametrize("word,geometry,name", [
    ("waterbodies", "polyline", "fetch_nhd_hr_waterbodies"),
    ("flowline network", "polygon", "fetch_nhdplus_hr_flowline_network")])
def test_the_wrong_shape_excludes_the_row(word, geometry, name):
    choice = match(Need(slot="s", data_class="hydrography", of=word,
                        geometry=geometry), sources_with_coverage())
    assert choice.picked != name


def test_the_rows_are_found_through_their_class(class_routes_to_the_match):
    class_routes_to_the_match("hydrography", "fetch_nhd_hr_waterbodies")
    class_routes_to_the_match("hydrography", "fetch_nhdplus_hr_flowline_network")


@pytest.mark.parametrize("word,point_row", [
    ("waterbody", "fetch_nhd_waterbody_at_point"),
    ("flowline", "fetch_nhdplus_nldi_navigate")])
def test_the_point_rows_keep_their_words_at_a_seed(word, point_row):
    choice = match(Need(slot="domain", data_class="hydrography", of=word,
                        lon=-123.21, lat=45.49), sources_with_coverage())
    assert choice.picked == point_row
