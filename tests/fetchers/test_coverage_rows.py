"""The coverage rows, read off the specs that carry them.

A row is a source's ONLY statement of coverage, so this pins what each one
declares rather than what its prose says."""

from __future__ import annotations

from pathlib import Path

import pytest

from trid3nt_contracts.coverage import (
    DATA_CLASSES, PROVENANCE_KINDS, CoveragePoint)
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
    by_kind = {row.kind: row for row in rows}
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
