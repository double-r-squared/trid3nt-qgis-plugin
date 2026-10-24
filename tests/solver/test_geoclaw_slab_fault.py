"""The SlabFault composite: a stated rupture rectangle laid on the real Slab2 interface as a dtopo file.

Offline. The slab record is the row's own read of an excerpt of USGS's Slab2 release (Cascadia, -126.4 to -123.4 E and
46.0 to 47.9 N). Pinned: every subfault's depth, strike and dip are the grid's values at its own centre, read
independently off the release members by bilinear blend; the subfaults tile the stated length and width along the
interface (each row steps the stated width in three dimensions); the rake and slip are the stated ones, unscaled;
a size the subfault does not divide, an unstated value, a rupture another composite wrote and an anchor or subfault
off the slab refuse by name.
"""

from __future__ import annotations

import math
import tarfile
from pathlib import Path

import netCDF4
import numpy as np
import pytest
from pyproj import Geod
from scipy.interpolate import RegularGridInterpolator

from trid3nt_server.tools.fetchers import _slab2_grids as slab2
from trid3nt_server.tools.fetchers._router.registration import get_spec
from trid3nt_server.tools.fetchers.hazard.fetch_slab_geometry import hooks
from trid3nt_server.workflows.geoclaw.errors import GeoclawError
from trid3nt_server.workflows.geoclaw.modules import GEO

_TAR = Path(__file__).parents[1] / "fixtures" / "geoclaw" / "slab2" / "slab2_excerpt.tar.gz"
_RUN = {"GEO_COORDINATE_SYSTEM": 2}


@pytest.fixture(autouse=True)
def _archive(monkeypatch):
    monkeypatch.setattr(slab2, "archive_bytes", lambda spec: _TAR.read_bytes())


def _slab(bbox=(-126.2, 46.3, -123.6, 47.6)) -> dict:
    return hooks.record(get_spec("fetch_slab_geometry"), {"bbox": list(bbox)}, [])


def _stated(**over) -> dict:
    return {"slab": _slab(), "longitude": -125.0, "latitude": 47.0, "anchor": "top", "length": 100e3, "width": 50e3,
            "subfault_length": 25e3, "subfault_width": 25e3, "rake": 90.0, "slip": 5.0, "spacing": 0.05, "buffer": 0.5,
            "times": [1.0], **over}


def _subfaults(**over) -> list[dict]:
    expanded, files = GEO.COMPOSITES["SlabFault"].apply(_stated(**over), _RUN)
    return files[expanded["DTOPO_DTOPOFILES"][0][1]]["dtopo"]["subfaults"]


def _raw_grids() -> dict:
    """The excerpt's own cas grids as interpolators over (lat, lon 0..360), straight off the release members."""
    out = {}
    with tarfile.open(_TAR) as tar:
        for member in tar:
            for kind in ("dep", "str", "dip"):
                if member.name.split("/")[-1].startswith(f"cas_slab2_{kind}_"):
                    ds = netCDF4.Dataset("x", memory=tar.extractfile(member).read())
                    x, y, z = np.asarray(ds["x"][:]), np.asarray(ds["y"][:]), np.asarray(ds["z"][:].filled(np.nan))
                    out[kind] = (x, y, z)
    x, y, _ = out["dep"]
    return {"dep": RegularGridInterpolator((y, x), out["dep"][2]), "dip": RegularGridInterpolator((y, x), out["dip"][2]),
            "sin": RegularGridInterpolator((y, x), np.sin(np.radians(out["str"][2]))),
            "cos": RegularGridInterpolator((y, x), np.cos(np.radians(out["str"][2])))}


def test_every_subfault_reads_depth_strike_and_dip_from_the_grids_at_its_own_centre():
    subs, raw = _subfaults(), _raw_grids()

    assert len(subs) == 8
    for sub in subs:
        at = (sub["latitude"], sub["longitude"] % 360)
        assert sub["depth"] == pytest.approx(-float(raw["dep"](at)) * 1000.0, abs=1e-6)
        assert sub["dip"] == pytest.approx(float(raw["dip"](at)), abs=1e-9)
        strike = math.degrees(math.atan2(float(raw["sin"](at)), float(raw["cos"](at)))) % 360
        assert sub["strike"] == pytest.approx(strike, abs=1e-4)
        assert (sub["rake"], sub["slip"], sub["length"], sub["width"], sub["coordinate_specification"]) == (
            90.0, 5.0, 25e3, 25e3, "centroid")


def test_the_rows_step_the_stated_width_along_the_interface_and_the_columns_the_stated_length_along_strike():
    subs, geod = _subfaults(), Geod(ellps="WGS84")
    columns = [subs[0:2], subs[2:4], subs[4:6], subs[6:8]]

    for column in columns:
        _, _, run = geod.inv(column[0]["longitude"], column[0]["latitude"], column[1]["longitude"], column[1]["latitude"])
        chord = math.hypot(run, column[1]["depth"] - column[0]["depth"])
        assert chord == pytest.approx(25e3, rel=0.01) and column[1]["depth"] > column[0]["depth"]
    for left, right in zip(columns, columns[1:]):
        _, _, along = geod.inv(left[0]["longitude"], left[0]["latitude"], right[0]["longitude"], right[0]["latitude"])
        assert along == pytest.approx(25e3, rel=0.05)


def test_a_centre_anchor_puts_the_stated_point_between_the_two_rows_of_a_two_row_rectangle():
    top = _subfaults(anchor="top", length=25e3)
    centre = _subfaults(anchor="centre", length=25e3)
    geod = Geod(ellps="WGS84")
    _, _, off = geod.inv(-125.0, 47.0, (centre[0]["longitude"] + centre[1]["longitude"]) / 2,
                         (centre[0]["latitude"] + centre[1]["latitude"]) / 2)

    assert len(top) == len(centre) == 2 and off < 1.0e3
    assert centre[0]["depth"] < 12.5e3 + 25e3 / 2 * math.sin(math.radians(10)) + 1.0e3 < top[1]["depth"] + 5e3
    assert top[0]["depth"] > centre[0]["depth"]


def test_the_dtopo_builds_and_lifts_the_sea_floor_for_a_thrust_slip():
    from clawpack.geoclaw import dtopotools

    expanded, files = GEO.COMPOSITES["SlabFault"].apply(_stated(), _RUN)
    spec = files[expanded["DTOPO_DTOPOFILES"][0][1]]["dtopo"]
    fault, subs = dtopotools.Fault(), []
    for stated in spec["subfaults"]:
        sub = dtopotools.SubFault()
        for field in ("longitude", "latitude", "depth", "strike", "dip", "rake", "slip", "length", "width", "coordinate_specification"):
            setattr(sub, field, stated[field])
        sub.calculate_geometry()
        subs.append(sub)
    fault.subfaults = subs
    x, y = (np.linspace(*spec[axis][:2], int(spec[axis][2])) for axis in ("x", "y"))
    dz = fault.create_dtopography(x, y, [1.0]).dZ[-1]

    assert 0.0 < dz.max() < 5.0 and dz.min() < 0.0 and 6.5 < fault.Mw() < 8.0


@pytest.mark.parametrize("over,match", [
    ({"width": 60e3}, "width 60000 m is not a whole number of subfaults of 25000 m"),
    ({"length": 110e3}, "length 110000 m is not a whole number"),
    ({"slip": None}, "states no slip"),
    ({"rake": None}, "states no rake"),
    ({"anchor": "middle"}, "anchor 'middle' is none of top or centre"),
    ({"extent": 1}, "states extent"),
    ({"spacing": -1.0}, "spacing -1 must be positive"),
    ({"slip": float("nan")}, "slip nan is no finite number"),
    ({"longitude": -100.0}, "SlabFault anchor: Slab2 states no interface at"),
    ({"latitude": 47.55}, "SlabFault walking along strike from the anchor: Slab2 states no interface"),
])
def test_an_unstated_unusable_or_off_slab_value_refuses_by_name(over, match):
    with pytest.raises(GeoclawError, match=match):
        GEO.COMPOSITES["SlabFault"].apply(_stated(**over), _RUN)


def test_a_rupture_another_composite_wrote_and_a_record_that_is_no_slab_refuse():
    with pytest.raises(GeoclawError, match="DTOPO_DTOPOFILES is already stated"):
        GEO.COMPOSITES["SlabFault"].apply(_stated(), {**_RUN, "DTOPO_DTOPOFILES": [[3, "x"]]})
    with pytest.raises(GeoclawError, match="slab record holds no zone"):
        GEO.COMPOSITES["SlabFault"].apply(_stated(slab={"zones": []}), _RUN)
    with pytest.raises(GeoclawError, match="GEO_COORDINATE_SYSTEM"):
        GEO.COMPOSITES["SlabFault"].apply(_stated(), {})


def test_a_place_two_zones_hold_refuses_instead_of_choosing_one(monkeypatch):
    record = hooks.record(get_spec("fetch_slab_geometry"), {"bbox": [133.6, 34.7, 134.2, 35.3], "zone": "ryu"}, [])
    other = hooks.record(get_spec("fetch_slab_geometry"), {"bbox": [133.6, 34.7, 134.2, 35.3], "zone": "kur"}, [])
    both = {"zones": record["zones"] + other["zones"]}

    with pytest.raises(GeoclawError, match="SlabFault anchor: .*lies in 2 Slab2 zones at different depths"):
        GEO.COMPOSITES["SlabFault"].apply(_stated(slab=both, longitude=133.9, latitude=35.0), _RUN)
