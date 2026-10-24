"""The Slab2 row: the interface depth, strike and dip read from the release's own grids.

Offline. The fixture tarball is an excerpt of USGS's Slab2Distribute_Mar2018 in the release's own layout and
format (HDF5 netCDF, lon 0..360, depth negative): Cascadia (cas) from -126.4 to -123.4 E and 46.0 to 47.9 N, and the
Kuril (kur) and Ryukyu (ryu) windows around the node (133.9 E, 35.0 N) that both zones hold at different depths.
Pinned: a node reads exactly the grid's value (depth with its sign flipped), a point between nodes blends, a stencil
node the grid masks refuses rather than extrapolates, a place two zones hold refuses until a zone is stated, and a
miss names the nearest node that states an interface.
"""

from __future__ import annotations

import io
import math
import tarfile
from pathlib import Path

import netCDF4
import numpy as np
import pytest

from trid3nt_server.tools.fetchers import _slab2_grids as slab2
from trid3nt_server.tools.fetchers._router.registration import get_spec
from trid3nt_server.tools.fetchers.hazard.fetch_slab_geometry import hooks

_TAR = Path(__file__).parents[1] / "fixtures" / "geoclaw" / "slab2" / "slab2_excerpt.tar.gz"


@pytest.fixture(autouse=True)
def _archive(monkeypatch):
    monkeypatch.setattr(slab2, "archive_bytes", lambda spec: _TAR.read_bytes())


def _raw(zone: str, lon: float, lat: float) -> tuple[float, float, float]:
    """Depth (positive down), strike and dip at a node, read straight off the tarball's members."""
    out = {}
    with tarfile.open(_TAR) as tar:
        for member in tar:
            for kind in ("dep", "str", "dip"):
                if member.name.split("/")[-1].startswith(f"{zone}_slab2_{kind}_"):
                    ds = netCDF4.Dataset("x", memory=tar.extractfile(member).read())
                    x, y = np.asarray(ds["x"][:]), np.asarray(ds["y"][:])
                    i, j = int(np.argmin(abs(x - lon % 360))), int(np.argmin(abs(y - lat)))
                    assert abs(x[i] - lon % 360) < 1e-6 and abs(y[j] - lat) < 1e-6
                    out[kind] = float(ds["z"][j, i])
    return -out["dep"], out["str"], out["dip"]


def _fetch(**params) -> dict:
    return hooks.record(get_spec("fetch_slab_geometry"), params, [])


@pytest.mark.parametrize("lon,lat", [(-124.5, 47.1), (-125.0, 47.0), (-123.6, 46.4)])
def test_a_point_on_a_node_reads_the_grids_own_depth_strike_and_dip(lon, lat):
    at = _fetch(point=[lon, lat])["at"]
    depth, strike, dip = _raw("cas", lon, lat)

    assert at["zone"] == "cas" and (at["longitude"], at["latitude"]) == (lon, lat)
    assert (at["depth_km"], at["strike"], at["dip"]) == (depth, strike, dip)
    assert depth > 0 and 0 <= strike < 360


def test_a_point_between_nodes_blends_depth_and_dip_and_averages_strike_as_a_vector():
    a, b = _raw("cas", -125.0, 47.0), _raw("cas", -124.95, 47.0)
    at = _fetch(point=[-124.975, 47.0])["at"]
    sin = math.sin(math.radians(a[1])) + math.sin(math.radians(b[1]))
    cos = math.cos(math.radians(a[1])) + math.cos(math.radians(b[1]))

    assert at["depth_km"] == pytest.approx((a[0] + b[0]) / 2) and at["dip"] == pytest.approx((a[2] + b[2]) / 2)
    assert at["strike"] == pytest.approx(math.degrees(math.atan2(sin, cos)) % 360)


def test_a_stencil_node_the_grid_masks_refuses_and_a_node_beside_it_reads():
    with pytest.raises(Exception, match="states no interface at") as caught:
        _fetch(point=[-125.68, 46.0])
    assert caught.value.error_code == "SLAB_NO_COVERAGE" and "nearest node that states one" in str(caught.value)
    assert _fetch(point=[-125.65, 46.0])["at"]["depth_km"] == _raw("cas", -125.65, 46.0)[0]


def test_a_place_outside_every_zone_names_the_nearest_node_and_its_distance():
    with pytest.raises(Exception) as caught:
        _fetch(point=[-100.0, 47.0])
    text = str(caught.value)

    assert caught.value.error_code == "SLAB_NO_COVERAGE"
    assert "(-123.40, 47.90) in zone cas" in text and "km away" in text


def test_a_place_two_zones_hold_refuses_naming_both_until_a_zone_is_stated():
    with pytest.raises(Exception) as caught:
        _fetch(point=[133.9, 35.0])
    assert caught.value.error_code == "SLAB_INPUT_INVALID"
    assert "kur 402.6 km" in str(caught.value) and "ryu 98.0 km" in str(caught.value)

    kur, ryu = (_fetch(point=[133.9, 35.0], zone=z)["at"] for z in ("kur", "ryu"))
    assert (kur["depth_km"], ryu["depth_km"]) == (_raw("kur", 133.9, 35.0)[0], _raw("ryu", 133.9, 35.0)[0])
    assert [z["zone"] for z in _fetch(point=[133.9, 35.0], zone="ryu")["zones"]] == ["ryu"]


def test_a_box_returns_each_covering_zones_window_with_one_node_of_margin_in_the_boxs_frame():
    record = _fetch(bbox=[-125.0, 46.5, -124.5, 47.0])
    (cas,) = record["zones"]

    assert "at" not in record and cas["zone"] == "cas" and cas["spacing_deg"] == 0.05
    assert cas["lons"][0] == pytest.approx(-125.05) and cas["lons"][-1] == pytest.approx(-124.45)
    assert cas["lats"][0] == pytest.approx(46.45) and cas["lats"][-1] == pytest.approx(47.05)
    j, i = cas["lats"].index(next(v for v in cas["lats"] if abs(v - 47.0) < 1e-9)), cas["lons"].index(
        next(v for v in cas["lons"] if abs(v + 125.0) < 1e-9))
    assert (cas["depth_km"][j][i], cas["strike"][j][i], cas["dip"][j][i]) == _raw("cas", -125.0, 47.0)


def test_a_box_over_no_interface_and_an_unstated_or_doubled_place_refuse_by_name():
    with pytest.raises(Exception) as none:
        _fetch(bbox=[-100.0, 10.0, -99.0, 11.0])
    assert none.value.error_code == "SLAB_NO_COVERAGE"
    for params in ({}, {"point": [-124.5, 47.1], "bbox": [-125.0, 46.5, -124.5, 47.0]}):
        with pytest.raises(Exception, match="exactly one of point"):
            _fetch(**params)
    with pytest.raises(Exception, match="zone 'zzz' is none of the release's zones: cas, kur, ryu"):
        _fetch(point=[-124.5, 47.1], zone="zzz")


def test_a_zone_missing_one_of_its_grids_refuses_naming_the_zone():
    with tarfile.open(_TAR) as src:
        members = [(m, src.extractfile(m).read()) for m in src if m.isfile() and "cas_slab2_dip" not in m.name]
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as out:
        for member, data in members:
            out.addfile(member, io.BytesIO(data))

    with pytest.raises(slab2.SlabError, match="zone 'cas' without all of its depth, strike and dip grids"):
        slab2.read_archive(buf.getvalue())


def test_a_strike_across_north_blends_through_north_not_through_south():
    """Hand-built 2 x 2 nodes (isolated verification, not release data): strikes 359 and 1 degrees."""
    ones = np.ones((2, 2))
    zone = slab2.Zone("tst", 0.05, np.array([0.0, 0.05]), np.array([0.0, 0.05]), 10.0 * ones,
                      np.array([[359.0, 1.0], [359.0, 1.0]]), 5.0 * ones)

    got = slab2.sample([zone], 0.025, 0.0)

    assert got["strike"] == pytest.approx(0.0, abs=1e-9) or got["strike"] == pytest.approx(360.0, abs=1e-9)
    assert got["depth_km"] == 10.0 and got["dip"] == 5.0
