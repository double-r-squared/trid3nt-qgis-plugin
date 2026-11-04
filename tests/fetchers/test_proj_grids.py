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


def test_served_pairs_at_a_sand_point_names_the_pair_the_seams_serve_there(proj_grids):
    # VERTCON never reaches Alaska; the refusal a tidal pair gets there names
    # the one pair the grids do convert at the point, so a frame can be stated.
    proj_grids("alaska")
    seam.served_pairs.cache_clear()
    assert seam.served_pairs(*_SAND_POINT) == (("NAVD88", "EGM2008"),)


def test_served_pairs_over_conus_names_every_geodetic_pair(proj_grids):
    proj_grids("capecod")
    seam.served_pairs.cache_clear()
    pairs = seam.served_pairs(*_JBCC_SW)
    assert pairs == (("NGVD29", "NAVD88"), ("NAVD88", "EGM2008"),
                     ("NGVD29", "EGM2008"))


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


def test_a_grid_that_is_not_installed_refuses_naming_the_script(monkeypatch, tmp_path):
    from types import SimpleNamespace as NS

    monkeypatch.setenv("TRID3NT_PROJ_GRIDS", str(tmp_path))
    grid = NS(short_name="us_noaa_g2018u0.tif", available=False)
    absent = NS(best_available=False, unavailable_operations=[NS(grids=[grid])])
    monkeypatch.setattr(seam, "_group", lambda pair, bounds: absent)
    with pytest.raises(seam.DatumGridUnavailable, match=r"us_noaa_g2018u0.tif.*fetch_proj_grids.sh"):
        seam.geodetic_offset("NAVD88", "EGM2008", -70.0, 41.7)
