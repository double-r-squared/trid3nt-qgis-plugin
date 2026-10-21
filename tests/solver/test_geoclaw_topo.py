"""The Topography composite: bed surfaces as topotype-3 lon/lat topofiles.

Offline. Every raster here is SYNTHETIC and exists to verify the plumbing only - the
extents, the registration, the offset path and the coverage rule; none is a bed and
none is a deliverable. Pinned: each surface becomes one topofile Clawpack's own reader
returns with the same extent and values, one TOPO_TOPOFILES row in the written
topo.data, and the union of the files - never one file - is what must cover the level-1
box; a surface on another zero needs its measured offset, a node nothing measured
refuses, and TOPO_TOPOFILES stated beside the composite refuses by name.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from trid3nt_server.inputs.user_input import UserInputError
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

#: SYNTHETIC (verification only): chile2010's level-1 box is (-120, -60) x (-60, 0).
_NO_TOPO = {k: v for k, v in _chile._CHILE.items() if k != "TOPO_TOPOFILES"}


def _raster(path: Path, west, north, cell, rows, cols, fill_value=None, crs="EPSG:4326"):
    """SYNTHETIC elevations: a ramp in x so a read-back can tell a shifted or flipped grid."""
    values = (np.arange(cols, dtype="float32")[None, :] * np.ones((rows, 1), "float32")
              + np.arange(rows, dtype="float32")[:, None] * 1000.0)
    if fill_value is not None:
        values[0, 0] = fill_value
    with rasterio.open(path, "w", driver="GTiff", height=rows, width=cols, count=1,
                       dtype="float32", crs=crs, nodata=-9999.0,
                       transform=from_origin(west, north, cell, cell)) as dst:
        dst.write(np.where(np.isnan(values), -9999.0, values).astype("float32"), 1)
    return str(path)


def _ladder(tmp_path):
    """SYNTHETIC: a 2 degree surface over the whole box and a 1 degree one inside it."""
    coarse = _raster(tmp_path / "coarse.tif", -121.0, 1.0, 2.0, 31, 31)
    fine = _raster(tmp_path / "fine.tif", -80.0, -20.0, 1.0, 10, 10)
    return coarse, fine


def _sheet(run, surfaces, **over):
    return fill(GEO, produced={"vertical_frame": "NAVD88", **run},
                **{**_NO_TOPO, "Topography": surfaces, **over})


def _staged_dtopo(rundir: Path) -> None:
    rundir.mkdir(parents=True, exist_ok=True)
    (rundir / "dtopo_usgs100227.tt3").write_bytes(b"staged dtopography\n")


def _written(tmp_path, run, surfaces, **over):
    rundir = tmp_path / "run"
    _staged_dtopo(rundir)
    sheet = _sheet(run, surfaces, **over)
    return rundir, sheet, serialize(sheet, rundir)


def _read(path: Path):
    from clawpack.geoclaw.topotools import Topography

    topo = Topography()
    topo.read(str(path), 3)
    return topo


def test_two_surfaces_become_two_topofiles_clawpack_reads_back(tmp_path):
    coarse, fine = _ladder(tmp_path)
    run = {"coarse": coarse, "fine": fine}

    rundir, sheet, written = _written(
        tmp_path, run, [{"surface": "coarse"}, {"surface": "fine"}])

    assert sheet.filled["TOPO_TOPOFILES"].value == [[3, "topo_01.tt3"], [3, "topo_02.tt3"]]
    assert "topo.data" in written["written"]
    first, second = _read(rundir / "topo_01.tt3"), _read(rundir / "topo_02.tt3")
    assert (first.x[0], first.x[-1]) == pytest.approx((-120.0, -60.0))
    assert (first.y[0], first.y[-1]) == pytest.approx((-60.0, 0.0))
    assert (second.x[0], second.x[-1]) == pytest.approx((-79.5, -70.5))
    assert (second.y[0], second.y[-1]) == pytest.approx((-29.5, -20.5))
    with rasterio.open(fine) as src:
        expect = src.read(1)[::-1]
    assert np.allclose(second.Z, expect, atol=1e-3)
    text = (rundir / "topo.data").read_text()
    assert "topo_01.tt3" in text and "topo_02.tt3" in text
    assert "xllcenter" in (rundir / "topo_01.tt3").read_text()


def test_the_written_topo_set_parses_back_through_the_staged_check(tmp_path):
    coarse, fine = _ladder(tmp_path)
    rundir, sheet, written = _written(
        tmp_path, {"coarse": coarse, "fine": fine},
        [{"surface": "coarse"}, {"surface": "fine"}])

    read = check_staged_run(rundir, sheet=sheet, written=written["written"])

    assert "TOPO_TOPOFILES" in read["checked"]


def test_a_fine_surface_inside_a_coarse_one_passes_and_a_gap_refuses(tmp_path):
    coarse, fine = _ladder(tmp_path)
    _written(tmp_path, {"coarse": coarse, "fine": fine},
             [{"surface": "coarse"}, {"surface": "fine"}])
    short = _raster(tmp_path / "short.tif", -121.0, 1.0, 2.0, 31, 20)

    with pytest.raises(GeoclawError) as caught:
        _written(tmp_path / "gap", {"short": short, "fine": fine},
                 [{"surface": "short"}, {"surface": "fine"}])

    assert caught.value.error_code == "GEOCLAW_TOPO_COVERAGE"
    text = str(caught.value)
    assert "topofile 1 spans x -120..-82" in text and "topofile 2" in text
    assert "uncovered part lies within (-82.0, -60.0, -60.0, 0.0)" in text


def test_a_surface_alone_inside_the_box_refuses(tmp_path):
    _, fine = _ladder(tmp_path)

    with pytest.raises(GeoclawError) as caught:
        _written(tmp_path, {"fine": fine}, [{"surface": "fine"}])

    assert caught.value.error_code == "GEOCLAW_TOPO_COVERAGE"


def test_topofiles_stated_beside_topography_refuses_by_name(tmp_path):
    coarse, _ = _ladder(tmp_path)

    with pytest.raises(GeoclawError) as caught:
        _sheet({"coarse": coarse}, [{"surface": "coarse"}],
               TOPO_TOPOFILES=[[2, "etopo10min120W60W60S0S.asc"]])

    assert caught.value.error_code == "GEOCLAW_TOPOGRAPHY_TOPOFILES_STATED"
    assert "TOPO_TOPOFILES" in str(caught.value)


def test_a_run_naming_no_frame_refuses_rather_than_assuming_one(tmp_path):
    coarse, _ = _ladder(tmp_path)

    with pytest.raises(GeoclawError) as caught:
        fill(GEO, produced={"coarse": coarse}, Topography=[{"surface": "coarse"}],
             **_NO_TOPO)

    assert caught.value.error_code == "GEOCLAW_TOPOGRAPHY_FRAME_UNSTATED"


def _egm(path):
    return {"uri": path, "vertical_datum": "EGM2008 (metres, positive up)",
            "name": "synthetic egm2008 surface"}


def test_a_surface_on_another_zero_without_its_offset_refuses_by_name(tmp_path):
    coarse, _ = _ladder(tmp_path)

    with pytest.raises(UserInputError) as caught:
        _sheet({"coarse": _egm(coarse)}, [{"surface": "coarse"}])

    assert "EGM2008" in str(caught.value) and "NAVD88" in str(caught.value)


def test_a_surface_on_another_zero_is_written_shifted_by_its_measured_offset(tmp_path):
    coarse, _ = _ladder(tmp_path)
    # SYNTHETIC offset, verification only: the record shape a vertical-datum fetch returns.
    offset = {"offset_m": 2.5, "from_frame": "egm2008", "to_frame": "navd88",
              "source": "synthetic test record"}

    rundir, _, _ = _written(tmp_path, {"coarse": _egm(coarse), "off": offset},
                            [{"surface": "coarse", "offset": "off"}])

    with rasterio.open(coarse) as src:
        raw = src.read(1)[::-1]
    assert np.allclose(_read(rundir / "topo_01.tt3").Z, raw + 2.5, atol=1e-3)


def test_a_node_nothing_measured_refuses_naming_the_count(tmp_path):
    holed = _raster(tmp_path / "holed.tif", -121.0, 1.0, 2.0, 31, 31, fill_value=np.nan)

    with pytest.raises(GeoclawError) as caught:
        _written(tmp_path, {"holed": holed}, [{"surface": "holed"}])

    assert caught.value.error_code == "GEOCLAW_TOPO_NODATA"
    assert "1 of 961" in str(caught.value)


def test_a_projected_surface_is_written_in_lon_lat(tmp_path):
    from pyproj import Transformer

    to_m = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    west, south = to_m.transform(-121.0, -61.0)
    east, north = to_m.transform(-59.0, 1.0)
    cell = 20_000.0
    cols, rows = int((east - west) // cell) + 1, int((north - south) // cell) + 1
    projected = _raster(tmp_path / "merc.tif", west, north, cell, rows, cols,
                        crs="EPSG:3857")

    rundir, _, _ = _written(tmp_path, {"merc": projected}, [{"surface": "merc"}])

    topo = _read(rundir / "topo_01.tt3")
    assert topo.x[0] < -120.0 < -60.0 < topo.x[-1]
    assert -90.0 < topo.y[0] < topo.y[-1] < 90.0
    assert abs(topo.delta[0] - topo.delta[1]) < 1e-8 * topo.delta[0]


def test_a_depth_in_metres_is_no_surface(tmp_path):
    with pytest.raises(GeoclawError) as caught:
        _sheet({}, [{"surface": 20.0}])

    assert caught.value.error_code == "GEOCLAW_TOPOGRAPHY_DEPTH"


def test_a_utm_surface_loses_only_the_corner_wedges_its_reprojection_leaves(tmp_path):
    from pyproj import Transformer

    # SYNTHETIC UTM 10N grid, verification only: rotated in lon/lat, so its lon/lat bounds hold wedges.
    utm = _raster(tmp_path / "utm.tif", 380_000.0, 5_300_000.0, 5_000.0, 60, 60,
                  crs="EPSG:32610")
    rundir, _, _ = _written(
        tmp_path, {"utm": utm}, [{"surface": "utm"}],
        CLAW_LOWER=[-123.0, 46.0], CLAW_UPPER=[-122.0, 47.0], CLAW_NUM_CELLS=[10, 10])

    topo = _read(rundir / "topo_01.tt3")
    back = Transformer.from_crs("EPSG:4326", "EPSG:32610", always_xy=True)
    corners = [back.transform(x, y) for x in (topo.x[0], topo.x[-1])
               for y in (topo.y[0], topo.y[-1])]
    assert all(380_000.0 <= cx <= 680_000.0 and 5_000_000.0 <= cy <= 5_300_000.0
               for cx, cy in corners)
    assert not np.isnan(topo.Z).any()
