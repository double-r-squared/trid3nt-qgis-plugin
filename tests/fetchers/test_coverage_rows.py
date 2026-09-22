"""The coverage rows, read off the specs that carry them.

A row is a source's ONLY statement of coverage, so this pins what each one
declares rather than what its prose says."""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import pytest

from trid3nt_contracts.coverage import (
    DATA_CLASSES, PROVENANCE_KINDS, CoverageExtent, CoveragePoint)
from trid3nt_server.tools.fetchers._router.spec import load_spec_from_path

_ROOT = Path(__file__).resolve().parents[2] / "trid3nt_server" / "tools" / "fetchers"

#: Every fetcher spec in the tree - the sweep reads what each one declares
#: rather than a hand-kept list that goes stale the moment a source lands.
SPECS = sorted(_ROOT.glob("*/*/source.yaml"))


@pytest.mark.parametrize("path", SPECS, ids=lambda p: p.parent.name)
def test_every_spec_loads_and_its_rows_validate(path):
    spec = load_spec_from_path(path)
    for row in spec.coverage:
        assert row.data_class in DATA_CLASSES
        assert row.kind in PROVENANCE_KINDS


@pytest.mark.parametrize("path", SPECS, ids=lambda p: p.parent.name)
def test_a_source_with_no_row_says_it_is_model_callable(path):
    spec = load_spec_from_path(path)
    if spec.coverage:
        return
    assert any("model-callable, not matched" in caveat
               for caveat in spec.caveats), (
        f"{path.parent.name} states no coverage and no reason for stating none")


@pytest.mark.parametrize("path", SPECS, ids=lambda p: p.parent.name)
def test_a_station_row_lists_its_stations(path):
    """A ring alone says a place is in the region and nothing about whether a
    station stands near it, which is what the reach is measured from."""
    for row in load_spec_from_path(path).coverage:
        if row.extent.kind == "stations":
            assert (row.extent.points or row.extent.read_from
                    or row.extent.discover), path.parent.name


#: A degree, in metres, so an arc-second is 1/3600 of one. The published names
#: round that to whole metres - an arc-second is "about 30 m", a third of one
#: "about 10 m" - which is the rounding a row's own number is read against.
_DEGREE_M = 111_320.0

#: The params a terrain source states the cell it fetches under, by the spelling
#: it uses. A source that states none serves one grid and the row is its own.
_RESOLUTION_PARAMS = ("resolution", "resolution_m")


def _cell_m(stated: object) -> float:
    """The cell one stated resolution is, in metres."""
    text = str(stated).strip()
    if "arc-second" in text:
        return float(Fraction(text.split()[0])) * _DEGREE_M / 3600.0
    if text.split()[-1].startswith("meter"):
        return float(Fraction(text.split()[0]))
    return float(text)


@pytest.mark.parametrize("path", SPECS, ids=lambda p: p.parent.name)
def test_a_terrain_row_states_the_cell_its_ask_fetches(path):
    """A row claiming a finer cell than the one it is fetched under outranks the
    sources that state theirs honestly, and every bed matched on it gets coarser
    than the declaration asked for. The number is the cell the ask states, else
    the cell the source defaults to."""
    spec = load_spec_from_path(path)
    params = dict(spec.params or {})
    name = next((p for p in _RESOLUTION_PARAMS if p in params), "")
    for row in spec.coverage:
        if row.data_class != "terrain" or not name:
            continue
        stated = row.ask.get(name, params[name].default)
        assert row.resolution_m == pytest.approx(_cell_m(stated), rel=0.05), (
            f"{path.parent.name} is fetched at {stated} and its row claims "
            f"{row.resolution_m} m")


def test_the_contract_refuses_a_station_set_drawn_as_rings_alone():
    """The refusal is the contract's, not the sweep's: a stations extent with
    rings and no listing, hook or discovery never constructs."""
    ring = [(-72.0, 41.0), (-71.0, 41.0), (-71.0, 42.0), (-72.0, 42.0)]
    with pytest.raises(ValueError, match="a station set lists its stations"):
        CoverageExtent(kind="stations", rings=[ring])
    assert CoverageExtent(kind="stations", rings=[ring], discover="bbox")
    assert CoverageExtent(kind="stations", rings=[ring], points=[
        CoveragePoint(id="8452660", lon=-71.32614, lat=41.504333)])


@pytest.mark.parametrize("path", SPECS, ids=lambda p: p.parent.name)
def test_every_series_source_states_the_unit_of_its_value_columns(path):
    for row in load_spec_from_path(path).coverage:
        if row.window.series:
            assert row.units, f"{path} reports a series in no stated unit"


def test_the_gauge_network_states_each_column_on_its_own_row():
    rows = {row.data_class: row for row in load_spec_from_path(
        _ROOT / "hydrology/fetch_usgs_nwis_gauges/source.yaml").coverage}
    flow = rows["discharge series"]
    assert flow.series_column == "time_series_csv"
    assert flow.units["time_series_csv"] == "ft3/s"
    stage = rows["water level series"]
    assert stage.series_column == "stage_series_csv"
    assert stage.units["stage_series_csv"] == "ft"
    # a gage height is a height above the gauge's OWN zero, and the row names
    # the column carrying that zero's elevation.
    assert stage.above_column == "gauge_datum_ft"


def test_a_lake_row_covers_its_lake_and_not_the_open_coast():
    row = load_spec_from_path(
        _ROOT / "ocean/fetch_greatlakes_bathymetry/source.yaml").coverage[0]
    assert row.extent.covers(-87.0, 43.0)
    assert not row.extent.covers(-82.5, 27.8)


def test_the_tide_gauge_states_the_record_and_the_prediction_apart():
    """One source serving a measured and a predicted series of one class states
    a row for each, and each names what it is fetched under."""
    rows = load_spec_from_path(
        _ROOT / "ocean/fetch_noaa_coops_tides/source.yaml").coverage
    by_kind = {row.kind: row for row in rows
               if row.data_class == "water level series"}
    assert set(by_kind) == {"measured", "predicted"}
    assert by_kind["measured"].ask == {"product": "water_level"}
    assert by_kind["predicted"].ask == {"product": "predictions"}
    assert all(row.extent.read_from == "noaa_coops.stations" for row in rows)


def test_the_nearest_station_is_what_a_listed_set_is_measured_from():
    row = load_spec_from_path(
        _ROOT / "ocean/fetch_noaa_coops_tides/source.yaml").coverage[0]
    listed = row.extent.model_copy(update={"points": [
        CoveragePoint(id="8452660", lon=-71.32614, lat=41.504333),
        CoveragePoint(id="8461490", lon=-72.0900, lat=41.3614)]})
    # Point Judith's mouth: Newport is the nearer of the two, and the distance
    # the reach is read against is the station's, not the coastal ring's.
    assert listed.nearest(-71.505, 41.353).id == "8452660"
    assert 20.0 < listed.distance_km(-71.505, 41.353) < 25.0


#: The features a hydrography source publishes, by the fetcher that publishes
#: them. Written here as the pin because the class alone says a source maps
#: water and nothing about WHICH water: the row's vocabulary is what a need
#: selects on, and a source that stops publishing a feature has to say so.
_HYDROGRAPHY_FEATURES = {
    "fetch_osm_features": {"coastline"},
    "fetch_river_geometry": {"channel network", "drainage network"},
    "fetch_nhdplus_nldi_navigate": {"flowline"},
    "fetch_nhd_water_surface": {"water surface"},
    "fetch_watershed": {"basin"},
    "fetch_nhd_waterbody_at_point": {"waterbody"},
}


@pytest.mark.parametrize("path", SPECS, ids=lambda p: p.parent.name)
def test_every_hydrography_row_names_the_features_it_publishes(path):
    """One class, many things: a coastline, a waterbody, a flowline, the water
    surface it runs between and a traced basin are all hydrography, and a row
    that named none of them would answer a question asking for any of them."""
    spec = load_spec_from_path(path)
    rows = [row for row in spec.coverage if row.data_class == "hydrography"]
    if not rows:
        return
    for row in rows:
        assert set(row.vocabulary) == _HYDROGRAPHY_FEATURES[path.parent.name]


def test_the_channel_network_is_asked_for_in_the_sources_own_word():
    """The feature travels onto the param this source states it in, so a
    question names the network once and the tag set follows."""
    spec = load_spec_from_path(_ROOT / "hydrology" / "fetch_river_geometry"
                               / "source.yaml")
    row, = spec.coverage
    assert row.ask["waterway_type"] == "need:of"
    assert row.word_for("channel network") == "default"
