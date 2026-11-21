"""GOES cloud cover over a point, and ASOS sky cover as its stated fallback.

The GOES answers are one recorded ABI-L2-ACMC CONUS scan (GOES-19, 2026-11-19 21:01 UTC) cropped to 100 x 60 pixels
around Des Moines and the recorded S3 listing of two hours of its bucket (tests/fixtures/fetcher_fields/README.md); the
bucket is read through a stand-in client that serves them. Cases a recorded block cannot show (bad quality, fill) edit
a copy of the recorded block and say so."""

from __future__ import annotations

import io
import json
import math
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import netCDF4
import pytest

from trid3nt_contracts.coverage import Member
from trid3nt.errors import CLOUD_COVER_FROM_ASOS, cloud_cover_from_asos_warning
from trid3nt.inputs import run_notes
from trid3nt.inputs.cloud import cloud
from trid3nt.inputs.fields import FieldError, Record
from trid3nt.inputs.observation import ObservationError
from trid3nt.tools.fetchers._router import registration
from trid3nt.tools.fetchers.weather.fetch_goes_cloud_cover import hooks as goes

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "fetcher_fields"
_SCAN = _FIXTURES / "goes19_acmc_conus_des_moines_crop.nc"
_LISTING = json.loads((_FIXTURES / "goes19_acmc_listing_20261119_h20_h21.json").read_text())
_SPEC = registration.get_spec("fetch_goes_cloud_cover")
_HOUR = datetime(2026, 11, 19, 21, tzinfo=timezone.utc)


def _lonlat(column: int, row: int) -> tuple[float, float]:
    """The longitude and latitude of one pixel's centre, from the file's own projection read by pyproj."""
    from pyproj import Proj

    with netCDF4.Dataset(str(_SCAN)) as ds:
        proj = ds["goes_imager_projection"]
        height = float(proj.perspective_point_height)
        x = column * float(ds["x"].scale_factor) + float(ds["x"].add_offset)
        y = row * float(ds["y"].scale_factor) + float(ds["y"].add_offset)
        geos = Proj(proj="geos", h=height, lon_0=float(proj.longitude_of_projection_origin), sweep="x",
                    a=float(proj.semi_major_axis), b=float(proj.semi_minor_axis))
    return geos(x * height, y * height, inverse=True)


def _box(data: bytes, column: int, row: int) -> dict:
    lon, lat = _lonlat(column, row)
    return goes._box(data, lon, lat, "scan.nc", "GOES_CLOUD")


def _edited(tmp_path, **edit) -> bytes:
    """A copy of the recorded crop with a block's BCM or DQF values stated by hand."""
    path = tmp_path / "edited.nc"
    shutil.copy(_SCAN, path)
    with netCDF4.Dataset(str(path), "r+") as ds:
        ds.set_auto_maskandscale(False)
        for name, (rows, cols, value) in edit.items():
            ds[name][rows, cols] = value
    return path.read_bytes()


def test_a_point_lands_on_the_pixel_pyproj_projects_it_to_and_its_block_is_five_by_five():
    got = _box(_SCAN.read_bytes(), 1092, 299)
    assert (got["grid_x"], got["grid_y"], got["box_x0"], got["box_y0"], got["n_pixels"]) == (1092, 299, 1090, 295, 25)


def test_the_recorded_clear_block_is_zero_octas_with_every_pixel_counted():
    got = _box(_SCAN.read_bytes(), 1092, 299)
    assert (got["n_used"], got["n_cloudy"], got["cloud_fraction"], got["cloud_octas"]) == (25, 0, 0.0, 0.0)
    assert got["platform_ID"] == "G19" and got["scan_start"].startswith("2026-11-19T21:01")


def test_octas_are_eight_times_the_cloudy_fraction_unrounded():
    got = _box(_SCAN.read_bytes(), 1115, 255)
    assert (got["n_used"], got["n_cloudy"]) == (25, 6)
    assert got["cloud_octas"] == 8.0 * 6 / 25 and got["cloud_octas"] != round(got["cloud_octas"])


def test_a_bad_quality_block_has_no_cloud_cover_and_never_reads_as_clear(tmp_path):
    block = (slice(50, 55), slice(50, 55), 1)
    got = _box(_edited(tmp_path, DQF=block), 1092, 299)
    assert got["n_dqf_bad"] == 25 and got["n_used"] == 0
    assert got["cloud_octas"] is None and got["cloud_fraction"] is None


def test_a_degraded_pixel_is_counted_and_left_out_of_the_fraction(tmp_path):
    got = _box(_edited(tmp_path, DQF=(slice(50, 51), slice(50, 51), 6)), 1092, 299)
    assert (got["n_dqf_degraded"], got["n_used"], got["n_dqf_good"]) == (1, 24, 24)


def test_a_fill_pixel_of_the_mask_is_counted_apart_and_never_read_as_clear(tmp_path):
    got = _box(_edited(tmp_path, BCM=(slice(50, 55), slice(50, 55), 255)), 1092, 299)
    assert got["n_mask_fill"] == 25 and got["n_used"] == 0 and got["cloud_octas"] is None


def test_a_space_pixel_is_counted_apart(tmp_path):
    got = _box(_edited(tmp_path, DQF=(slice(50, 52), slice(50, 55), 2)), 1092, 299)
    assert (got["n_dqf_space"], got["n_used"]) == (10, 15)


def test_the_satellite_is_the_one_whose_projection_is_centred_nearer_the_point():
    assert goes._satellite("G", -93.6, datetime(2026, 11, 1, tzinfo=timezone.utc))[0] == "G19"
    assert goes._satellite("G", -122.7, datetime(2026, 11, 1, tzinfo=timezone.utc))[0] == "G18"
    assert goes._satellite("G", -105.9, datetime(2026, 11, 1, tzinfo=timezone.utc))[0] == "G19"
    assert goes._satellite("G", -106.1, datetime(2026, 11, 1, tzinfo=timezone.utc))[0] == "G18"


def test_a_window_before_the_satellites_first_hour_refuses():
    with pytest.raises(Exception) as caught:
        goes._satellite("GOES_CLOUD", -93.6, datetime(2024, 10, 15, 17, tzinfo=timezone.utc))
    assert caught.value.error_code == "GOES_CLOUD_INPUT_ERROR"
    goes._satellite("GOES_CLOUD", -93.6, datetime(2024, 10, 15, 18, tzinfo=timezone.utc))
    with pytest.raises(Exception):
        goes._satellite("GOES_CLOUD", -122.7, datetime(2022, 5, 11, 18, tzinfo=timezone.utc))


class _Client:
    """The bucket as the recorded listing and the cropped scan state it; every key serves the one scan."""

    def __init__(self, data: bytes) -> None:
        self.data, self.got = data, []

    def get_paginator(self, name):
        listing = self

        class _Pages:
            def paginate(self, *, Bucket, Prefix):
                yield {"Contents": _LISTING.get(Prefix, [])}

        assert name == "list_objects_v2"
        return _Pages()

    def get_object(self, *, Bucket, Key):
        self.got.append(Key)
        return {"Body": io.BytesIO(self.data)}


def _read(monkeypatch, data, **params):
    monkeypatch.setattr("trid3nt.tools.fetchers._public_s3.public_s3_client", lambda *a: _Client(data))
    lon, lat = _lonlat(1092, 299)
    asked = {"seed_point": [lon, lat], "start_time": "2026-11-19T21:00:00+00:00",
             "end_time": "2026-11-19T21:00:00+00:00", **params}
    return goes.read(_SPEC, asked, timeout_s=60.0)


def _listed():
    client = _Client(b"")
    hours = [_HOUR - timedelta(hours=2), _HOUR - timedelta(hours=1), _HOUR]
    return {hour: goes._keys(client, "noaa-goes19", hour) for hour in hours}


def test_the_scan_at_an_instant_is_the_last_one_to_begin_at_or_before_it():
    held = goes._scan_at(_listed(), _HOUR.replace(minute=7))
    assert held[1] == _LISTING["ABI-L2-ACMC/2026/323/21/"][1]["Key"]
    assert held[0] == datetime(2026, 11, 19, 21, 6, 18, tzinfo=timezone.utc)


def test_the_scan_before_the_first_of_an_hour_is_found_in_the_hour_before():
    held = goes._scan_at(_listed(), _HOUR)
    assert held[0] == datetime(2026, 11, 19, 20, 56, 18, tzinfo=timezone.utc)
    assert held[1] == _LISTING["ABI-L2-ACMC/2026/323/20/"][-1]["Key"]


def test_a_window_reads_one_feature_per_distinct_scan_at_the_point_with_every_field(monkeypatch):
    rows = _read(monkeypatch, _SCAN.read_bytes(), start_time="2026-11-19T20:30:00+00:00",
                 end_time="2026-11-19T21:30:00+00:00")
    assert len(rows) == 2
    props = rows[0]["properties"]
    for kept in ("site", "bucket", "key", "scan_start", "scan_end", "n_dqf_good", "n_dqf_degraded", "n_dqf_bad",
                 "n_dqf_space", "n_dqf_other", "n_mask_fill", "n_used", "n_cloudy", "cloud_fraction", "cloud_octas"):
        assert kept in props
    assert props["site"] == "G19" and rows[0]["geometry"]["type"] == "Point"


def test_the_row_states_its_members_by_the_fields_it_keeps():
    fields = _SPEC.fields
    assert (fields["site"].field, fields["time"].field, fields["cloud_cover"].field) == (
        "site", "scan_start", "cloud_octas")
    assert fields["cloud_cover"].unit == "octa" and "cloud_cover_accuracy" not in fields


def test_a_window_with_no_good_box_refuses_by_name(monkeypatch, tmp_path):
    bad = (slice(50, 55), slice(50, 55), 1)
    with pytest.raises(Exception) as caught:
        _read(monkeypatch, _edited(tmp_path, DQF=bad))
    assert caught.value.error_code == "GOES_CLOUD_EMPTY"


# -- the ASOS fallback ---------------------------------------------------------------------------------------------- #

_ASOS = registration.get_spec("fetch_asos_metar").fields
_TABLE3 = {"CLR": (0.0, 5.0), "FEW": (5.0, 25.0), "SCT": (25.0, 50.0), "BKN": (50.0, 87.0), "OVC": (87.0, 100.0)}


@pytest.mark.parametrize("code,range_pct", sorted(_TABLE3.items()))
def test_asos_sky_cover_is_the_midpoint_of_table_3_in_octas_and_its_accuracy_half_the_range(code, range_pct):
    record = Record({"skyc1": code}, _ASOS, source="fetch_asos_metar")
    low, high = (p * 8.0 / 100.0 for p in range_pct)
    assert record.read("cloud_cover").value == pytest.approx((low + high) / 2.0)
    assert record.read("cloud_cover").unit == "octa"
    assert record.read("cloud_cover_accuracy").value == pytest.approx((high - low) / 2.0)


@pytest.mark.parametrize("code", ["SKC", "VV", "NCD", "ZZZ"])
def test_a_sky_cover_code_outside_table_3_refuses_untabled(code):
    with pytest.raises(FieldError) as refused:
        Record({"skyc1": code}, _ASOS, source="fetch_asos_metar").read("cloud_cover")
    assert refused.value.error_code == "FIELD_UNTABLED"


def _asos_feature(hour: int, code: str = "BKN"):
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-122.6, 45.6]},
            "properties": {"station": "PDX", "valid": f"2026-11-19T{hour:02d}:00:00Z", "skyc1": code}}


def _doc(features):
    return {"type": "FeatureCollection", "features": features}


def _read_cloud(doc, fields):
    token = run_notes.bind_notes()
    try:
        found = cloud(doc, fields=fields, producer="fetch_asos_metar", near={"lon": -122.6, "lat": 45.6},
                      at="2026-11-19T21:00:00Z", window_s=3600.0)
    finally:
        notes = run_notes.drain_notes(token)
    return found, notes


def test_the_asos_fallback_reads_octas_on_the_runs_clock_and_warns_of_the_swap_by_its_code():
    found, notes = _read_cloud(_doc([_asos_feature(h) for h in (20, 21, 22, 23)]), _ASOS)
    assert found.columns["cloud_octas"].values[0] == pytest.approx(5.48) and set(found.columns) == {"cloud_octas"}
    assert cloud_cover_from_asos_warning() in notes and cloud_cover_from_asos_warning().startswith(
        CLOUD_COVER_FROM_ASOS + ":")


def test_the_swap_warning_names_the_swap_and_the_ceilometer_limit_and_interpolates_nothing():
    text = cloud_cover_from_asos_warning()
    assert "GOES" in text and "ASOS" in text and "12,000 ft" in text
    assert "{" not in text and text == cloud_cover_from_asos_warning()


def test_a_gridded_source_reading_cloud_in_octas_carries_no_swap_warning():
    fields = {"site": Member(field="site"), "time": Member(field="scan_start"),
              "cloud_cover": Member(field="cloud_octas", unit="octa")}
    rows = [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [-93.6, 41.6]},
             "properties": {"site": "G19", "scan_start": f"2026-11-19T{h:02d}:01:18Z", "cloud_octas": 1.92}}
            for h in (20, 21, 22, 23)]
    found, notes = _read_cloud(_doc(rows), fields)
    assert found.columns["cloud_octas"].values[0] == pytest.approx(1.92)
    assert not any(CLOUD_COVER_FROM_ASOS in note for note in notes)


def test_cloud_refuses_a_source_that_maps_no_cloud():
    fields = {"site": Member(field="station"), "time": Member(field="valid")}
    with pytest.raises(ObservationError, match="cloud_cover"):
        _read_cloud(_doc([_asos_feature(21)]), fields)


def test_a_box_with_no_cloud_cover_is_a_missing_reading_not_a_clear_one():
    fields = {"site": Member(field="site"), "time": Member(field="scan_start"),
              "cloud_cover": Member(field="cloud_octas", unit="octa")}
    rows = [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [-93.6, 41.6]},
             "properties": {"site": "G19", "scan_start": f"2026-11-19T{h:02d}:01:18Z",
                            "cloud_octas": None if h == 22 else 0.0}} for h in (20, 21, 22, 23)]
    found, _ = _read_cloud(_doc(rows), fields)
    series = found.columns["cloud_octas"]
    assert len(series.values) == 3 and 3678.0 not in series.times_s


# -- the match -------------------------------------------------------------------------------------------------------- #

def _ranked(lon, lat, opens="2026-11-19T21:00:00+00:00", until="2026-11-19T22:00:00+00:00"):
    from trid3nt.tools.search.match import Need, match, sources_with_coverage

    need = Need(slot="cloud", data_class="cloud cover", lon=lon, lat=lat, geometry="point", opens=opens, until=until)
    return match(need, sources_with_coverage())


def test_goes_ranks_first_for_cloud_cover_and_asos_is_the_next_survivor_over_des_moines():
    choice = _ranked(-93.6, 41.6)
    assert [row.fetcher for row in choice.rows[:2]] == ["fetch_goes_cloud_cover", "fetch_asos_metar"]
    assert choice.picked == "fetch_goes_cloud_cover"


def test_a_place_outside_the_goes_sector_is_left_to_asos():
    choice = _ranked(-150.0, 61.2)
    assert choice.picked == "fetch_asos_metar"


def test_a_window_before_goes_holds_anything_is_left_to_asos():
    assert _ranked(-93.6, 41.6, "2020-06-01T00:00:00+00:00", "2020-06-02T00:00:00+00:00").picked == "fetch_asos_metar"


def test_the_goes_row_is_asked_by_the_point_and_the_window():
    from trid3nt.tools.search.match import Need, base_ask

    choice = _ranked(-93.6, 41.6)
    ask = base_ask(choice, "cloud", None, -93.6, 41.6, "2026-11-19T21:00:00+00:00", "2026-11-19T22:00:00+00:00")
    assert ask["seed_point"] == [-93.6, 41.6]
    assert (ask["start_time"], ask["end_time"]) == ("2026-11-19T20:00:00+00:00", "2026-11-19T23:00:00+00:00")


# -- the templates and the atmosphere ----------------------------------------------------------------------------- #

@pytest.mark.parametrize("template", ["water_temperature", "ice_cover"])
def test_the_templates_that_read_cloud_state_a_cloud_cover_need_and_hand_it_to_the_atmosphere(template):
    from trid3nt.inputs.slots import CLOUD
    import importlib

    module = importlib.import_module(f"trid3nt.workflows.telemac.templates.{template}.{template}")
    row = module.INPUTS.cloud
    assert row.slot is CLOUD and row.data_class == "cloud cover"
    assert module.STEERING.atmosphere["cloud_octas"] == "cloud"


def test_the_atmosphere_reads_the_cloud_record_on_the_station_records_clock():
    from trid3nt.inputs.weather import weather
    from trid3nt.workflows.telemac.authoring.atmosphere import _resolved

    station_fields = {"site": Member(field="s"), "time": Member(field="t"),
                      "air_temperature": Member(field="a", unit="degC")}
    cloud_fields = {"site": Member(field="s"), "time": Member(field="t"),
                    "cloud_cover": Member(field="c", unit="octa")}

    def rows(extra):
        return {"type": "FeatureCollection", "features": [
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-93.6, 41.6]},
             "properties": {"s": "X", "t": f"2026-11-19T{h:02d}:00:00Z", **extra(h)}} for h in (21, 22, 23)]}

    opens = "2026-11-19T21:00:00Z"
    station = weather(rows(lambda h: {"a": 10.0 + h - 21}), fields=station_fields, near={"lon": -93.6, "lat": 41.6},
                      at=opens, window_s=3600.0)
    cloudy = cloud(rows(lambda h: {"c": 2.0 * (h - 21)}), fields=cloud_fields, near={"lon": -93.6, "lat": 41.6},
                   at=opens, window_s=3600.0)
    from trid3nt.workflows.telemac.authoring.atmosphere import Atmosphere

    times, stated, _note = _resolved(Atmosphere(observed=station, cloud_octas=cloudy), ("air_temp_c", "cloud_octas"))
    assert stated["cloud_octas"] == [0.0, 2.0, 4.0][: len(times)] or stated["cloud_octas"] == pytest.approx(
        [2.0 * t / 3600.0 for t in times])
