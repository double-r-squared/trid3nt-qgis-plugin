"""The thirteen coverage rows, read off the specs that carry them.

A row is a source's ONLY statement of coverage, so this pins what each one
declares rather than what its prose says."""

from __future__ import annotations

from pathlib import Path

import pytest

from trid3nt_server.tools.fetchers._router.spec import load_spec_from_path

_ROOT = Path(__file__).resolve().parents[2] / "trid3nt_server" / "tools" / "fetchers"

#: The thirteen the bed slot and the run series match through, and the class
#: each declares.
ROWS = {
    "terrain/fetch_dem": "terrain",
    "terrain/fetch_copernicus_dem": "terrain",
    "terrain/fetch_3dep_extra": "terrain",
    "hydrology/fetch_ehydro_surveys": "bathymetry",
    "ocean/fetch_topobathy": "bathymetry",
    "ocean/fetch_bluetopo": "bathymetry",
    "ocean/fetch_greatlakes_bathymetry": "bathymetry",
    "hydrology/fetch_usgs_nwis_gauges": "discharge series",
    "hydrology/fetch_noaa_nwm_streamflow": "discharge series",
    "ocean/fetch_greatlakes_water_level": "water level series",
    "ocean/fetch_noaa_coops_tides": "water level series",
    "hydrology/fetch_nhd_waterbody_at_point": "hydrography",
    "hydrology/fetch_river_reach": "hydrography",
}


@pytest.mark.parametrize("rel,data_class", sorted(ROWS.items()))
def test_each_row_declares_its_class(rel, data_class):
    spec = load_spec_from_path(_ROOT / rel / "source.yaml")
    assert spec.coverage is not None, f"{rel} carries no coverage row"
    assert spec.coverage.data_class == data_class


def test_every_series_source_states_the_unit_of_its_value_columns():
    for rel in ROWS:
        coverage = load_spec_from_path(_ROOT / rel / "source.yaml").coverage
        if coverage.window.series:
            assert coverage.units, f"{rel} reports a series in no stated unit"


def test_the_gauge_network_states_both_its_columns_in_their_own_units():
    coverage = load_spec_from_path(
        _ROOT / "hydrology/fetch_usgs_nwis_gauges/source.yaml").coverage
    assert coverage.units["time_series_csv"] == "ft3/s"
    assert coverage.units["stage_series_csv"] == "ft"


def test_a_lake_row_covers_its_lake_and_not_the_open_coast():
    coverage = load_spec_from_path(
        _ROOT / "ocean/fetch_greatlakes_bathymetry/source.yaml").coverage
    assert coverage.extent.covers(-87.0, 43.0)
    assert not coverage.extent.covers(-82.5, 27.8)
