"""The Fault composite: stated subfaults as a dtopo file Clawpack's Okada model writes.

Offline. Every raster here is SYNTHETIC and exists to verify the plumbing only - the
water test and the footprint; none is a bed and none is a deliverable. The fault is
chile2010's own USGS subfault. Pinned: the written file equals the one the example's
maketopo writes on the same grid, the DTOPO_DTOPOFILES row lands in dtopo.data and parses
back through the staged check, and each unstated value, a subfault outside the level-1
box, a footprint over land and DTOPO_DTOPOFILES stated beside the composite refuse by name.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from trid3nt_server.workflows.geoclaw.authoring.rundata import serialize
from trid3nt_server.workflows.geoclaw.authoring.staged_check import (
    check_staged_run,
)
from trid3nt_server.workflows.geoclaw.errors import GeoclawError
from trid3nt_server.workflows.geoclaw.modules import GEO
from trid3nt_server.workflows.runtime.sheet import fill

_AUTHORING = Path(__file__).with_name("test_geoclaw_authoring.py")
_spec = importlib.util.spec_from_file_location("geoclaw_authoring_sheet", _AUTHORING)
_chile = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_chile)

_BASE = {k: v for k, v in _chile._CHILE.items()
         if k not in ("TOPO_TOPOFILES", "DTOPO_DTOPOFILES")}

#: chile2010's USGS subfault as maketopo.make_dtopo states it.
_SUBFAULT = {"strike": 16.0, "length": 450.0e3, "width": 100.0e3, "depth": 35.0e3,
             "slip": 15.0, "rake": 104.0, "dip": 14.0, "longitude": -72.668,
             "latitude": -35.826, "coordinate_specification": "top center"}
_GRID = {"x": [-77.0, -67.0, 100], "y": [-40.0, -30.0, 100], "times": [1.0]}


def _fault(**over):
    return {"subfaults": [{**_SUBFAULT, **over.pop("sub", {})}], **_GRID, **over}


def _bed(path: Path, value: float) -> str:
    """SYNTHETIC: a constant elevation over chile2010's whole level-1 box."""
    with rasterio.open(path, "w", driver="GTiff", height=31, width=31, count=1,
                       dtype="float32", crs="EPSG:4326", nodata=-9999.0,
                       transform=from_origin(-121.0, 1.0, 2.0, 2.0)) as dst:
        dst.write(np.full((31, 31), value, "float32"), 1)
    return str(path)


def _written(tmp_path, fault, bed=-4000.0, **over):
    rundir = tmp_path / "run"
    run = {"vertical_frame": "NAVD88"}
    slots = {**_BASE, "Fault": fault, **over}
    if bed is not None:
        run["bed"] = _bed(tmp_path / "bed.tif", bed)
        slots["Topography"] = [{"surface": "bed"}]
    sheet = fill(GEO, produced=run, **slots)
    return rundir, sheet, serialize(sheet, rundir)


def _example_dtopo(path: Path) -> None:
    """maketopo.make_dtopo from Clawpack's chile2010 example, verbatim in what it states."""
    from clawpack.geoclaw import dtopotools

    sub = dtopotools.SubFault()
    for key, value in _SUBFAULT.items():
        setattr(sub, key, value)
    fault = dtopotools.Fault()
    fault.subfaults = [sub]
    fault.create_dtopography(np.linspace(-77, -67, 100), np.linspace(-40, -30, 100), [1.0])
    fault.dtopo.write(str(path), dtopo_type=3)


def _read(path: Path):
    from clawpack.geoclaw.dtopotools import DTopography

    dtopo = DTopography()
    dtopo.read(str(path), 3)
    return dtopo


def test_the_subfault_writes_the_dtopo_the_example_writes(tmp_path):
    rundir, sheet, written = _written(tmp_path, _fault())
    _example_dtopo(tmp_path / "example.tt3")

    ours, theirs = _read(rundir / "dtopo_01.tt3"), _read(tmp_path / "example.tt3")

    assert sheet.filled["DTOPO_DTOPOFILES"].value == [[3, "dtopo_01.tt3"]]
    assert ours.dZ.shape == theirs.dZ.shape == (1, 100, 100)
    assert np.abs(ours.dZ - theirs.dZ).max() == 0.0
    assert np.abs(ours.x - theirs.x).max() == 0.0 and np.abs(ours.y - theirs.y).max() == 0.0
    assert (rundir / "dtopo_01.tt3").read_bytes() == (tmp_path / "example.tt3").read_bytes()
    assert abs(float(np.abs(ours.dZ).max())) > 1.0


_RISE = {"rupture_time": 5.0, "rise_time": 20.0}
_TIMES = [0.0, 10.0, 20.0, 30.0]


def test_a_kinematic_subfault_writes_the_dtopo_clawpack_writes_at_each_instant(tmp_path):
    from clawpack.geoclaw import dtopotools

    rundir, sheet, written = _written(tmp_path, _fault(sub=_RISE, times=_TIMES, rupture_type="kinematic",
                                                       rise_shape="linear"))
    sub = dtopotools.SubFault()
    for key, value in {**_SUBFAULT, **_RISE, "rise_shape": "linear"}.items():
        setattr(sub, key, value)
    theirs = dtopotools.Fault()
    theirs.subfaults, theirs.rupture_type = [sub], "kinematic"
    theirs.create_dtopography(np.linspace(-77, -67, 100), np.linspace(-40, -30, 100), _TIMES).write(
        str(tmp_path / "example.tt3"), dtopo_type=3)

    ours = _read(rundir / "dtopo_01.tt3")

    assert ours.dZ.shape == (4, 100, 100) and list(ours.times) == _TIMES
    assert (rundir / "dtopo_01.tt3").read_bytes() == (tmp_path / "example.tt3").read_bytes()
    assert float(np.abs(ours.dZ[0]).max()) == 0.0 < float(np.abs(ours.dZ[1]).max()) < float(np.abs(ours.dZ[3]).max())
    assert written["written"] and "dtopo_01.tt3" in (rundir / "dtopo.data").read_text()


@pytest.mark.parametrize("fault, code, word", [
    (dict(sub=_RISE), "GEOCLAW_FAULT_SUBFAULT", "rupture_type 'kinematic'"),
    (dict(sub=_RISE, rupture_type="kinematic", times=_TIMES), "GEOCLAW_FAULT_RISE_SHAPE_UNSTATED", "rise_shape"),
    (dict(rupture_type="kinematic", rise_shape="linear", times=_TIMES), "GEOCLAW_FAULT_SUBFAULT", "no rupture_time"),
    (dict(sub=_RISE, rupture_type="kinematic", rise_shape="quadratic", times=[0.0, 10.0]),
     "GEOCLAW_FAULT_KINEMATIC_UNFINISHED", "last dtopo instant 10 s precedes the rupture's end 25 s"),
    (dict(sub=_RISE, rupture_type="kinematic", rise_shape="quadratic", times=[10.0, 0.0, 30.0]),
     "GEOCLAW_FAULT_GRID", "strictly increasing"),
    (dict(sub={"rupture_time": 0.0, "rise_time": -1.0}, rupture_type="kinematic", rise_shape="linear", times=_TIMES),
     "GEOCLAW_FAULT_SUBFAULT", "below zero"),
])
def test_a_kinematic_fault_unstated_or_unfinished_refuses_by_name(tmp_path, fault, code, word):
    with pytest.raises(GeoclawError) as caught:
        _written(tmp_path, _fault(**fault))

    assert caught.value.error_code == code and word in str(caught.value)


def test_the_dtopo_row_lands_in_dtopo_data_and_parses_back(tmp_path):
    rundir, sheet, written = _written(tmp_path, _fault())

    assert "dtopo_01.tt3" in (rundir / "dtopo.data").read_text()
    assert str(rundir) not in (rundir / "dtopo.data").read_text()
    read = check_staged_run(rundir, sheet=sheet, written=written["written"])
    assert "DTOPO_DTOPOFILES" in read["checked"]


@pytest.mark.parametrize("field", ["coordinate_specification", "slip", "rake", "depth"])
def test_an_unstated_subfault_value_refuses_by_name(tmp_path, field):
    sub = {k: v for k, v in _SUBFAULT.items() if k != field}

    with pytest.raises(GeoclawError) as caught:
        _written(tmp_path, {"subfaults": [sub], **_GRID})

    assert caught.value.error_code == "GEOCLAW_FAULT_SUBFAULT"
    assert f"states no {field}" in str(caught.value) and "subfault 1" in str(caught.value)


def test_an_unstated_grid_refuses_by_name(tmp_path):
    with pytest.raises(GeoclawError) as caught:
        _written(tmp_path, {"subfaults": [_SUBFAULT], "x": _GRID["x"], "y": _GRID["y"]})

    assert caught.value.error_code == "GEOCLAW_FAULT_UNSTATED"
    assert "no times" in str(caught.value)


def test_an_unknown_coordinate_specification_refuses_in_clawpacks_words(tmp_path):
    with pytest.raises(GeoclawError) as caught:
        _written(tmp_path, _fault(sub={"coordinate_specification": "somewhere"}))

    assert caught.value.error_code == "GEOCLAW_FAULT_SUBFAULT"
    assert "subfault 1" in str(caught.value)


def test_a_subfault_outside_the_level_one_box_refuses_naming_it(tmp_path):
    with pytest.raises(GeoclawError) as caught:
        _written(tmp_path, _fault(sub={"longitude": -58.0}))

    assert caught.value.error_code == "GEOCLAW_FAULT_DOMAIN"
    assert "subfault 1" in str(caught.value) and "outside the level-1 box" in str(caught.value)


def test_a_footprint_over_land_refuses(tmp_path):
    with pytest.raises(GeoclawError) as caught:
        _written(tmp_path, _fault(), bed=250.0)

    assert caught.value.error_code == "GEOCLAW_FAULT_DRY"
    assert "none below 0 m" in str(caught.value)


def test_a_grid_holding_no_node_under_the_subfault_refuses(tmp_path):
    with pytest.raises(GeoclawError) as caught:
        _written(tmp_path, _fault(x=[-100.0, -90.0, 10], y=[-10.0, -5.0, 10]))

    assert caught.value.error_code == "GEOCLAW_FAULT_GRID"


def test_with_no_topofile_the_domain_check_alone_runs(tmp_path):
    rundir, _, _ = _written(tmp_path, _fault(), bed=None, TOPO_TOPOFILES=[[3, "t.tt3"]])

    assert (rundir / "dtopo_01.tt3").exists()


def test_dtopofiles_stated_beside_fault_refuses_by_name(tmp_path):
    with pytest.raises(GeoclawError) as caught:
        _written(tmp_path, _fault(), DTOPO_DTOPOFILES=[[3, "other.tt3"]])

    assert caught.value.error_code == "GEOCLAW_FAULT_DTOPOFILES_STATED"
    assert "DTOPO_DTOPOFILES" in str(caught.value)


def test_a_cartesian_run_refuses(tmp_path):
    with pytest.raises(GeoclawError) as caught:
        _written(tmp_path, _fault(), GEO_COORDINATE_SYSTEM=1)

    assert caught.value.error_code == "GEOCLAW_FAULT_COORDINATES"
