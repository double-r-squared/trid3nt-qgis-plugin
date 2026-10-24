"""The offline vertical seam: heights converted per point on PROJ's grids, from real grid excerpts.

Expected numbers are what the FULL published grids give at the same points (GEOID18, GEOID12B,
VERTCON 2, EGM2008 at 2.5 arcmin through the WGS 84 (G1674) -> NAD83(2011) Helmert step), so an
excerpt that reads wrong, or a chain that drops the Helmert step, moves a number."""

from __future__ import annotations

import numpy as np
import pytest

from trid3nt_server.tools.fetchers import _proj_grids as seam

_JBCC_SW = (-70.62, 41.59)
_SAND_POINT = (-160.505, 55.333)


def test_egm2008_to_navd88_on_geoid18_includes_the_ellipsoid_step(proj_grids):
    proj_grids("capecod")
    assert seam.geodetic_offset("EGM2008", "NAVD88", *_JBCC_SW) == pytest.approx(0.3668, abs=5e-4)


def test_ngvd29_to_navd88_is_vertcon(proj_grids):
    proj_grids("capecod")
    assert seam.geodetic_offset("NGVD29", "NAVD88", *_JBCC_SW) == pytest.approx(-0.2605, abs=5e-4)


def test_alaska_converts_on_geoid12b_where_geoid18_has_no_model(proj_grids):
    proj_grids("alaska")
    assert seam.geodetic_offset("EGM2008", "NAVD88", *_SAND_POINT) == pytest.approx(0.7584, abs=5e-4)
    assert seam.grids_used("EGM2008", "NAVD88", *_SAND_POINT) == [
        "us_nga_egm08_25.tif", "us_noaa_g2012ba0.tif"]


def test_a_chain_through_navd88_adds_its_two_offsets(proj_grids):
    proj_grids("capecod")
    chained = seam.geodetic_offset("NGVD29", "EGM2008", *_JBCC_SW)
    assert chained == pytest.approx(-0.2605 - 0.3668, abs=1e-3)


def test_the_reverse_direction_is_the_negation(proj_grids):
    proj_grids("capecod")
    forward = seam.geodetic_offset("EGM2008", "NAVD88", *_JBCC_SW)
    assert seam.geodetic_offset("NAVD88", "EGM2008", *_JBCC_SW) == pytest.approx(-forward, abs=1e-6)


def test_arrays_come_back_one_value_per_point(proj_grids):
    proj_grids("capecod")
    lon, lat = np.array([-70.62, -70.5]), np.array([41.59, 41.68])
    offsets = seam.geodetic_offset("NGVD29", "NAVD88", lon, lat)
    assert offsets.shape == (2,)
    assert offsets[0] == pytest.approx(-0.2605, abs=5e-4)
    assert offsets[1] == pytest.approx(-0.2606, abs=5e-4)


def test_a_frame_with_no_grid_at_the_place_refuses_and_never_reads_zero(proj_grids):
    # VERTCON is CONUS only; PROJ's ballpark step would answer 0.0 in Alaska.
    proj_grids("alaska")
    with pytest.raises(seam.NoGridCoverage) as caught:
        seam.geodetic_offset("NGVD29", "NAVD88", *_SAND_POINT)
    assert caught.value.error_code == "DATUM_GRID_NO_COVERAGE"


def test_a_point_outside_the_grid_refuses_naming_the_point(proj_grids):
    proj_grids("capecod")
    with pytest.raises(seam.NoGridCoverage) as caught:
        seam.geodetic_offset("EGM2008", "NAVD88", -70.62, 41.59 + 5.0)
    assert "41.59" not in str(caught.value) and "46.59" in str(caught.value)


def test_a_frame_the_seam_does_not_convert_is_a_value_error(proj_grids):
    proj_grids("capecod")
    with pytest.raises(ValueError, match="MHW"):
        seam.geodetic_offset("MHW", "NAVD88", *_JBCC_SW)


def test_a_missing_grid_is_fetched_once_and_read_from_disk_after(tmp_path, monkeypatch):
    import pathlib
    from types import SimpleNamespace

    import pyproj

    from trid3nt_server.tools.fetchers import _proj_grids

    fixture = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "proj_grids" / "capecod"
    url = "https://cdn.proj.org/us_noaa_vertcone.tif"
    monkeypatch.setattr(pyproj.datadir, "get_user_data_dir", lambda create=False: str(tmp_path))
    asked: list[str] = []

    def served(client, asked_url, **kw):
        asked.append(asked_url)
        return (fixture / "us_noaa_vertcone.tif").read_bytes(), "image/tiff", asked_url

    def group(pair, bounds):
        present = (tmp_path / "us_noaa_vertcone.tif").exists()
        grid = SimpleNamespace(short_name="us_noaa_vertcone.tif", url=url, available=present)
        return SimpleNamespace(best_available=present,
                               unavailable_operations=[] if present else [SimpleNamespace(grids=[grid])])

    monkeypatch.setattr(_proj_grids, "get_bytes", served)
    monkeypatch.setattr(_proj_grids, "_group", group)
    original = pyproj.datadir.get_data_dir()
    try:
        for _ in range(2):
            _proj_grids._ensure(("EPSG:4269+7968", "EPSG:4269+5703"), (-70.62, 41.59, -70.5, 41.68))
    finally:
        pyproj.datadir.set_data_dir(original)
    assert asked == [url]
    assert (tmp_path / "us_noaa_vertcone.tif").read_bytes() == (fixture / "us_noaa_vertcone.tif").read_bytes()
    assert not list(tmp_path.glob("*.part"))
