"""The coverage rows, read off the specs that carry them.

A row is a source's ONLY statement of coverage, so this pins what each one
declares rather than what its prose says."""

from __future__ import annotations

from pathlib import Path

import pytest

from trid3nt_server.tools.fetchers._router.spec import load_spec_from_path

_ROOT = Path(__file__).resolve().parents[2] / "trid3nt_server" / "tools" / "fetchers"

#: The sources the bed slot and the run series match through, and the classes
#: each one serves - a gauge that reports a flow AND a stage carries a row for
#: each.
ROWS = {
    "terrain/fetch_dem": ("terrain",),
    "terrain/fetch_copernicus_dem": ("terrain",),
    "terrain/fetch_3dep_extra": ("terrain",),
    "hydrology/fetch_ehydro_surveys": ("bathymetry",),
    "ocean/fetch_topobathy": ("bathymetry",),
    "ocean/fetch_bluetopo": ("bathymetry",),
    "ocean/fetch_greatlakes_bathymetry": ("bathymetry",),
    "hydrology/fetch_usgs_nwis_gauges": ("discharge series",
                                         "water level series"),
    "hydrology/fetch_noaa_nwm_streamflow": ("discharge series",),
    "ocean/fetch_greatlakes_water_level": ("water level series",),
    "ocean/fetch_noaa_coops_tides": ("water level series",),
    "hydrology/fetch_nhd_waterbody_at_point": ("hydrography",),
    "hydrology/fetch_river_reach": ("hydrography",),
}


@pytest.mark.parametrize("rel,classes", sorted(ROWS.items()))
def test_each_source_declares_one_row_per_class_it_serves(rel, classes):
    spec = load_spec_from_path(_ROOT / rel / "source.yaml")
    assert [row.data_class for row in spec.coverage] == list(classes)


def test_every_series_source_states_the_unit_of_its_value_columns():
    for rel in ROWS:
        for row in load_spec_from_path(_ROOT / rel / "source.yaml").coverage:
            if row.window.series:
                assert row.units, f"{rel} reports a series in no stated unit"
                assert row.value_column, f"{rel} names no value column"


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
