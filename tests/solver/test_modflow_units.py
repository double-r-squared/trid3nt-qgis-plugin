"""A raster is read onto the model's own frame: its published unit converted onto the sheet's.

Offline: the lattice is two squares of pixels over a 20 m mesh in UTM 32617; the expected
numbers are the unit table's own factors worked by hand.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trid3nt_server.inputs.temporal import convert_units
from trid3nt_server.workflows.modflow.authoring.griddata import frame_of, place, sample
from trid3nt_server.workflows.modflow.errors import ModflowError
from trid3nt_server.workflows.modflow.modules import GWF
from trid3nt_server.workflows.runtime.sheet import fill

_POINTS = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0),
           (20.0, 0.0), (20.0, 5.0)]
_CELLS = [(0, 1, 2), (0, 2, 3), (1, 4, 5), (1, 5, 2)]
_METERS_DAYS = ("meters", "days")


def _raster(path: Path, value: float = 365.25) -> Path:
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    with rasterio.open(
            path, "w", driver="GTiff", height=2, width=2, count=1,
            dtype="float32", crs="EPSG:32617",
            transform=from_origin(0.0, 20.0, 10.0, 10.0)) as dst:
        dst.write(np.full((2, 2), value, dtype="float32"), 1)
    return path


def _read(tmp_path, units, dimension, frame=_METERS_DAYS, value=365.25):
    artifact = ({"uri": str(_raster(tmp_path / "r.tif", value)), "units": units}
                if units is not None else str(_raster(tmp_path / "r.tif", value)))
    return sample(artifact, label="X", name="r", scratch=str(tmp_path),
                  points=_POINTS, cells=_CELLS, utm_epsg=32617,
                  dimension=dimension, frame=frame)


def test_recharge_in_mm_per_year_reads_in_metres_per_day(tmp_path):
    assert _read(tmp_path, "mm/yr", "L/T") == pytest.approx(
        [365.25 / 365.25 / 1000.0] * 4, rel=1e-6)


def test_the_same_recharge_onto_a_feet_model_reads_in_feet_per_day(tmp_path):
    assert _read(tmp_path, "mm/yr", "L/T", frame=("feet", "days")) == pytest.approx(
        [1.0 / 1000.0 / 0.3048] * 4, rel=1e-6)


def test_a_time_unit_of_the_model_other_than_days_scales_the_rate(tmp_path):
    assert _read(tmp_path, "m/day", "L/T", frame=("meters", "hours"),
                 value=24.0) == pytest.approx([1.0] * 4, rel=1e-6)


def test_a_length_in_feet_reads_in_metres_and_a_centimetre_model_in_centimetres(tmp_path):
    assert _read(tmp_path, "ft", "L", value=10.0) == pytest.approx([3.048] * 4, rel=1e-6)
    assert _read(tmp_path, "m", "L", frame=("centimeters", "days"),
                 value=1.0) == pytest.approx([100.0] * 4, rel=1e-6)


def test_a_transmissivity_in_feet_squared_per_day_reads_in_metres_squared_per_day(tmp_path):
    assert _read(tmp_path, "ft2/day", "L2/T", value=100.0) == pytest.approx(
        [100.0 * 0.09290304] * 4, rel=1e-6)


def test_a_well_rate_in_a_flow_unit_reads_in_cubic_metres_per_day(tmp_path):
    assert _read(tmp_path, "m3/s", "L3/T", value=1.0) == pytest.approx(
        [86400.0] * 4, rel=1e-6)


def test_a_storage_per_length_reads_per_the_model_length(tmp_path):
    assert _read(tmp_path, "1/ft", "1/L", value=1.0) == pytest.approx(
        [1.0 / 0.3048] * 4, rel=1e-6)


def test_a_pure_number_needs_no_unit_and_no_frame(tmp_path):
    assert _read(tmp_path, None, "1", frame=None, value=0.2) == pytest.approx([0.2] * 4)


def test_a_raster_stating_no_unit_refuses(tmp_path):
    with pytest.raises(ModflowError) as caught:
        _read(tmp_path, None, "L/T")
    assert caught.value.error_code == "MODFLOW_UNITS_UNSTATED"
    assert "no unit" in str(caught.value)


@pytest.mark.parametrize("frame,word", [
    ((None, "days"), "DISV_LENGTH_UNITS"), (("unknown", "days"), "DISV_LENGTH_UNITS"),
    (("meters", None), "TDIS_TIME_UNITS"), (("meters", "unknown"), "TDIS_TIME_UNITS"),
    (None, "DISV_LENGTH_UNITS")])
def test_a_model_stating_no_length_or_time_unit_refuses_naming_it(tmp_path, frame, word):
    with pytest.raises(ModflowError) as caught:
        _read(tmp_path, "mm/yr", "L/T", frame=frame)
    assert caught.value.error_code == "MODFLOW_UNITS_UNSTATED"
    assert word in str(caught.value)


def test_a_unit_of_another_quantity_refuses(tmp_path):
    with pytest.raises(ModflowError) as caught:
        _read(tmp_path, "m", "L/T")
    assert caught.value.error_code == "MODFLOW_UNITS_UNSTATED"
    assert "different quantities" in str(caught.value)


def test_a_unit_the_table_does_not_declare_refuses(tmp_path):
    with pytest.raises(ModflowError) as caught:
        _read(tmp_path, "furlong/fortnight", "L/T")
    assert caught.value.error_code == "MODFLOW_UNITS_UNSTATED"


def test_a_length_only_model_reads_a_length_without_a_time_unit(tmp_path):
    assert _read(tmp_path, "m", "L", frame=("meters", None), value=3.0) == [3.0] * 4


@pytest.mark.parametrize("unit,kind", [
    ("mm/yr", "depth_rate"), ("mm/year", "depth_rate"), ("m/day", "depth_rate"),
    ("m/d", "depth_rate"), ("ft/day", "depth_rate"), ("m2/day", "area_rate"),
    ("m^2/day", "area_rate"), ("m2/d", "area_rate"), ("ft2/day", "area_rate"),
    ("m2/s", "area_rate"), ("m", "length"), ("ft", "length")])
def test_every_unit_the_hydrology_fetchers_publish_resolves(unit, kind):
    base = {"depth_rate": "mm/day", "area_rate": "m2/day", "length": "m"}[kind]
    assert convert_units(1.0, unit, base) > 0.0


def test_a_year_is_read_as_mf6_reads_it():
    assert convert_units(365.25, "mm/yr", "mm/day") == pytest.approx(1.0)
    assert convert_units(1.0, "ft2/day", "m2/day") == pytest.approx(0.09290304)


def test_a_keyword_with_no_stated_dimension_refuses_naming_it(tmp_path):
    artifact = {"uri": str(_raster(tmp_path / "r.tif")), "units": "m"}
    with pytest.raises(ModflowError) as caught:
        place("r", slot=GWF.slot("NPF_ANGLE1"), nlay=1, run={"r": artifact},
              points=_POINTS, cells=_CELLS, utm_epsg=32617, frame=_METERS_DAYS)
    assert caught.value.error_code == "MODFLOW_UNITS_UNSTATED"
    assert "NPF_ANGLE1" in str(caught.value)


def test_a_conductivity_keyword_is_read_as_length_per_time(tmp_path):
    artifact = {"uri": str(_raster(tmp_path / "r.tif", 1.0)), "units": "m/s"}
    placed = place("r", slot=GWF.slot("NPF_K"), nlay=1, run={"r": artifact},
                   points=_POINTS, cells=_CELLS, utm_epsg=32617, frame=_METERS_DAYS)
    assert placed == [pytest.approx([86400.0] * 4)]


def test_the_frame_is_what_the_sheet_states():
    sheet = fill(GWF, DISV_LENGTH_UNITS="feet", TDIS_TIME_UNITS="hours", DISV_NLAY=1,
                 DISV_BOTM=[-1.0])
    assert frame_of(sheet) == ("feet", "hours")
    assert frame_of(fill(GWF, DISV_NLAY=1, DISV_BOTM=[-1.0])) == (None, None)


def test_a_length_spelled_out_reads_as_its_symbol():
    from trid3nt_server.inputs.temporal import convert_units

    assert convert_units(3.0, "meters", "m") == 3.0
    assert convert_units(3.0, "metres", "m") == 3.0
    assert convert_units(10.0, "feet", "m") == 3.048
