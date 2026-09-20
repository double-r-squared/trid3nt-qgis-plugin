"""The calibration subsystem, dormant and offline.

The model field is a synthetic raster written by the test and the record is
authored beside it, so the four axes - space, time, vertical datum and physical
quantity - are exercised for real with no world read. What is pinned is what the
pairing REFUSES to guess and what the metrics refuse to fabricate.
"""

from __future__ import annotations

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from trid3nt_server.workflows.calibration import CalibrationError
from trid3nt_server.workflows.calibration.metrics import metrics
from trid3nt_server.workflows.calibration.pairing import pairs

#: A 10 x 10 m field of water-surface elevation, rising a metre per cell east.
_ORIGIN = (-83.50, 35.10)
_CELL = 0.001


@pytest.fixture()
def field(tmp_path) -> str:
    z = np.tile(np.arange(10, dtype="float32"), (10, 1)) + 10.0
    z[0, 0] = np.nan
    path = str(tmp_path / "wse.tif")
    with rasterio.open(path, "w", driver="GTiff", height=10, width=10, count=1,
                       dtype="float32", crs="EPSG:4326", nodata=float("nan"),
                       transform=from_origin(_ORIGIN[0], _ORIGIN[1], _CELL, _CELL)) as dst:
        dst.write(z, 1)
        dst.update_tags(quantity="water_surface_elevation",
                        vertical_datum="NAVD88")
    return path


def _mark(col: float, row: float, value: float, **over) -> dict:
    return {"lon": _ORIGIN[0] + (col + 0.5) * _CELL,
            "lat": _ORIGIN[1] - (row + 0.5) * _CELL,
            "elev_m": value, "vertical_datum": "NAVD88", **over}


def test_the_model_is_read_at_every_mark_that_survives(field):
    record = [_mark(2, 5, 12.0), _mark(7, 5, 17.0)]
    paired = pairs(field, record, value_field="elev_m")
    assert len(paired) == 2
    assert paired.simulated == pytest.approx((12.0, 17.0))
    assert paired.observed == pytest.approx((12.0, 17.0))
    assert paired.frame == "NAVD88" and paired.units == "m"


def test_a_mark_off_the_field_is_dropped_carrying_its_reason(field):
    record = [_mark(2, 5, 12.0), _mark(90, 5, 12.0)]
    paired = pairs(field, record, value_field="elev_m")
    assert len(paired) == 1
    assert [row["reason"] for row in paired.dropped] == ["outside_footprint"]


def test_a_mark_one_cell_outside_the_wet_edge_still_pairs(field):
    """A mark standing just off the wet edge is a real measurement of a real
    flood; reading it as nodata would drop the observation that matters most."""
    paired = pairs(field, [_mark(0, 0, 10.0)], value_field="elev_m",
                   wet_reach_m=200.0)
    assert len(paired) == 1


def test_feet_are_converted_and_a_nameless_unit_refuses(field):
    in_feet = [{"lon": _ORIGIN[0] + 2.5 * _CELL, "lat": _ORIGIN[1] - 5.5 * _CELL,
                "elev_ft": 39.37, "vertical_datum": "NAVD88"}]
    paired = pairs(field, in_feet, value_field="elev_ft")
    assert paired.observed[0] == pytest.approx(12.0, abs=0.01)
    with pytest.raises(CalibrationError) as exc:
        pairs(field, [_mark(2, 5, 12.0, reading=12.0)], value_field="reading")
    assert exc.value.error_code == "CALIBRATION_UNITS_UNKNOWN"


def test_two_named_frames_that_differ_refuse_rather_than_pair(field):
    record = [_mark(2, 5, 12.0, vertical_datum="NGVD29")]
    with pytest.raises(CalibrationError) as exc:
        pairs(field, record, value_field="elev_m")
    assert exc.value.error_code == "CALIBRATION_DATUM_MISMATCH"
    paired = pairs(field, record, value_field="elev_m", shift_m=0.25)
    assert paired.observed[0] == pytest.approx(12.25)


def test_a_depth_model_against_elevation_marks_refuses_without_ground(tmp_path):
    """Their difference is dominated by ground elevation, so a residual across
    the two measures the terrain rather than the model."""
    path = str(tmp_path / "flood_depth.tif")
    with rasterio.open(path, "w", driver="GTiff", height=10, width=10, count=1,
                       dtype="float32", crs="EPSG:4326", nodata=float("nan"),
                       transform=from_origin(_ORIGIN[0], _ORIGIN[1], _CELL, _CELL)) as dst:
        dst.write(np.ones((10, 10), dtype="float32"), 1)
        dst.update_tags(quantity="flood_depth")
    with pytest.raises(CalibrationError) as exc:
        pairs(path, [_mark(2, 5, 12.0, quantity="water_surface_elevation")],
              value_field="elev_m")
    assert exc.value.error_code == "CALIBRATION_QUANTITY_MISMATCH"


def test_no_pair_at_all_refuses_by_counting_the_reasons(field):
    with pytest.raises(CalibrationError) as exc:
        pairs(field, [_mark(90, 5, 12.0)], value_field="elev_m")
    assert exc.value.error_code == "CALIBRATION_NO_PAIRS"
    assert "outside_footprint" in str(exc.value)


def test_the_metrics_read_the_pairs_and_say_what_they_cannot_measure(field):
    record = [_mark(c, 5, 10.0 + c) for c in range(2, 9)]
    scored = metrics(pairs(field, record, value_field="elev_m"), variable="stage")
    assert scored["NSE"] == pytest.approx(1.0)
    assert scored["RMSE"] == pytest.approx(0.0, abs=1e-6)
    assert scored["n"] == 7 and scored["variable"] == "stage"
    # A static spatial pairing has no simulated peak TIME to compare against.
    assert scored["peak_timing_error"] is None
    assert any("peak_timing_error is null" in note for note in scored["caveats"])


def test_no_acceptance_verdict_rides_on_the_metrics(field):
    scored = metrics(pairs(field, [_mark(c, 5, 10.0 + c) for c in range(2, 9)],
                           value_field="elev_m"))
    assert "verdict" not in scored and "rating" not in scored


def test_one_pair_is_not_a_measure_of_anything(field):
    with pytest.raises(CalibrationError) as exc:
        metrics(pairs(field, [_mark(2, 5, 12.0)], value_field="elev_m"))
    assert exc.value.error_code == "CALIBRATION_TOO_FEW_PAIRS"


def test_the_subsystem_registers_nothing():
    """Dormant: the observe slot is what reveals it, so nothing here is a tool."""
    from trid3nt_server.tools import TOOL_REGISTRY

    assert not [name for name in TOOL_REGISTRY if "calibrat" in name]
