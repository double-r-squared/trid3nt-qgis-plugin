"""Unit tests for ``fetch_goes_archive_animation`` (fire-animation demo B+C).

The HISTORICAL Fire Temperature animation built from the RAW noaa-goes18 S3
ABI-L2-MCMIPC archive (PATH B). Coverage:

- Registration + metadata + the SAME style preset / output shape as
  ``fetch_goes_animation`` (Track A + the scrubber consume it unchanged).
- The Fire Temperature band-math recipe: the C07 BT 0-60 C RED stretch, the C06
  100 % GREEN + C05 75 % BLUE reflectance stretches, gamma 1, per-channel
  clip [0,1], scaled to 0-255 uint8.
- A HOT-PIXEL red->white range assertion: a fire core (hot 3.9um BT + high
  2.2um + 1.6um reflectance) reads red->yellow->white; a cool/dark pixel reads
  black; a warm-but-not-saturated pixel reads pure red.
- CF scale_factor/add_offset is applied (rasterio NETCDF does NOT auto-apply).
- The historical S3 key listing: parse the _s<...> start-time, window, order
  ascending, dedupe, even-subsample to the frame cap.
- bbox-required + unknown band/satellite raise typed errors.
- The emitted "GOES Fire Temperature (Archive) step <N> <ISO> (<SAT>)" name
  token (the scrubber-group contract).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.fetchers.imagery import fetch_goes_archive_animation as mod
from trid3nt_server.tools.fetchers.imagery.fetch_goes_satellite import GOESInputError
from trid3nt_server.tools.fetchers.imagery.fetch_goes_archive_animation import (
    FIRE_TEMP_BLUE_REFL_MAX,
    FIRE_TEMP_GREEN_REFL_MAX,
    FIRE_TEMP_RED_KELVIN_RANGE,
    ARCHIVE_BANDS,
    FIRE_BT_C07_MIN_K,
    FIRE_BT_DIFF_MIN_K,
    FIRE_DETECT_BANDS,
    GOESArchiveBboxRequiredError,
    GOESArchiveEmptyError,
    GOESArchiveInputError,
    GOESArchiveUpstreamError,
    _band_valid_dn_range,
    _bake_fire_over_base,
    _detect_active_fire_mask,
    _fire_hotspots_rgba,
    _fire_temperature_rgb,
    _key_start_datetime,
    _list_archive_keys_in_window,
    _parse_utc,
    _select_window_keys,
    _stretch_brightness_temp_red,
    _stretch_reflectance,
    fetch_goes_archive_animation,
)

# Utah fire cluster (Iron + Hastings + the eastern-NV fires) from the design spike.
_UT_BBOX = (-114.05, 37.0, -109.04, 42.0)


# ---- registration ---------------------------------------------------------


def test_tool_is_registered():
    assert "fetch_goes_archive_animation" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_goes_archive_animation"]
    assert entry.metadata.name == "fetch_goes_archive_animation"
    assert entry.metadata.ttl_class == "dynamic-1h"
    assert entry.metadata.source_class == "goes_animation"
    assert entry.metadata.cacheable is True


# ---- _parse_utc -----------------------------------------------------------


def test_parse_utc_forms():
    assert _parse_utc("2026-06-22T13:30:00Z") == datetime(2026, 6, 22, 13, 30, tzinfo=timezone.utc)
    assert _parse_utc("2026-06-22T13:30:00+00:00") == datetime(2026, 6, 22, 13, 30, tzinfo=timezone.utc)
    assert _parse_utc("2026-06-22 13:30:00") == datetime(2026, 6, 22, 13, 30, tzinfo=timezone.utc)
    assert _parse_utc("2026-06-22") == datetime(2026, 6, 22, 0, 0, tzinfo=timezone.utc)


def test_parse_utc_rejects_garbage():
    with pytest.raises(GOESArchiveInputError):
        _parse_utc("not-a-date")


# ---- ABI key start-time parsing (JULIAN day-of-year) ----------------------


def test_key_start_datetime_parses_julian_doy():
    # DOY 173 = 2026-06-22 (2026 is not a leap year). 19:26:00.1 -> 19:26:00.
    key = "ABI-L2-MCMIPC/2026/173/19/OR_ABI-L2-MCMIPC-M6_G18_s20261731926001_e20261731928374_c20261731928....nc"
    dt = _key_start_datetime(key)
    assert dt == datetime(2026, 6, 22, 19, 26, 0, tzinfo=timezone.utc)
    assert dt.tzinfo == timezone.utc


def test_key_start_datetime_doy_001_is_jan_1():
    key = "OR_ABI-L2-MCMIPC-M6_G18_s20240010000000_e..._c....nc"
    assert _key_start_datetime(key) == datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def test_key_start_datetime_none_when_no_start_time():
    assert _key_start_datetime("ABI-L2-MCMIPC/2026/173/19/") is None


# ---- frame-list assembly --------------------------------------------------


def test_select_window_keys_keeps_all_under_cap():
    keys = [f"k{i}" for i in range(5)]
    assert _select_window_keys(keys, cap=10) == keys


def test_select_window_keys_subsamples_keeping_endpoints():
    keys = [f"k{i}" for i in range(100)]
    kept = _select_window_keys(keys, cap=10)
    assert kept[0] == "k0"
    assert kept[-1] == "k99"
    assert len(kept) <= 10
    # Strictly increasing (preserves the ascending input order).
    idx = [int(k[1:]) for k in kept]
    assert all(idx[i] < idx[i + 1] for i in range(len(idx) - 1))


def _mk_key(dt: datetime) -> str:
    """Build a synthetic MCMIPC key with the given start datetime (JULIAN DOY)."""
    doy = dt.timetuple().tm_yday
    s = f"{dt.year:04d}{doy:03d}{dt.hour:02d}{dt.minute:02d}{dt.second:02d}0"
    return (
        f"ABI-L2-MCMIPC/{dt.year}/{doy:03d}/{dt.hour:02d}/"
        f"OR_ABI-L2-MCMIPC-M6_G18_s{s}_e..._c....nc"
    )


def test_list_archive_keys_in_window_windows_and_orders(monkeypatch):
    """The S3 walk lists every touched hour-partition, parses each key's start-
    time, keeps the in-window ones, and returns them ASCENDING by time."""
    base = datetime(2026, 6, 22, 13, 0, tzinfo=timezone.utc)
    # 5-min cadence across 13:00..14:00; the listing returns reverse-chron tiles
    # (S3 is unordered) so the function must sort.
    all_times = [base + timedelta(minutes=5 * i) for i in range(13)]  # 13:00..14:00
    keys_by_hour: dict[tuple[int, int], list[str]] = {}
    for dt in all_times:
        keys_by_hour.setdefault((dt.hour,), []).append(_mk_key(dt))
    # Shuffle within each hour to prove we sort.
    for v in keys_by_hour.values():
        v.reverse()

    def _fake_list(bucket, prefix, *, session=None):
        # prefix = ABI-L2-MCMIPC/2026/173/<HH>/
        hh = int(prefix.rstrip("/").split("/")[-1])
        return keys_by_hour.get((hh,), [])

    monkeypatch.setattr(mod, "_list_keys_for_prefix", _fake_list)

    start = datetime(2026, 6, 22, 13, 10, tzinfo=timezone.utc)
    end = datetime(2026, 6, 22, 13, 30, tzinfo=timezone.utc)
    pairs = _list_archive_keys_in_window("goes-18", start, end)
    times = [t for t, _ in pairs]
    # 13:10, 13:15, 13:20, 13:25, 13:30 inclusive.
    assert times == [
        datetime(2026, 6, 22, 13, m, tzinfo=timezone.utc) for m in (10, 15, 20, 25, 30)
    ]
    # Ascending.
    assert times == sorted(times)


def test_list_archive_keys_empty_window_returns_empty(monkeypatch):
    monkeypatch.setattr(mod, "_list_keys_for_prefix", lambda *a, **k: [])
    start = datetime(2026, 6, 22, 13, 0, tzinfo=timezone.utc)
    end = datetime(2026, 6, 22, 13, 30, tzinfo=timezone.utc)
    assert _list_archive_keys_in_window("goes-18", start, end) == []


def test_list_archive_keys_all_listings_fail_raises_upstream(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("S3 down")

    monkeypatch.setattr(mod, "_list_keys_for_prefix", _boom)
    start = datetime(2026, 6, 22, 13, 0, tzinfo=timezone.utc)
    end = datetime(2026, 6, 22, 13, 30, tzinfo=timezone.utc)
    with pytest.raises(GOESArchiveUpstreamError):
        _list_archive_keys_in_window("goes-18", start, end)


def test_list_archive_keys_unknown_satellite_raises():
    # After the shared-normalizer migration, a genuinely-unknown bird now fails
    # LOUD on the shared _normalize_satellite seam (typed GOESInputError listing
    # the accepted forms) BEFORE the bucket lookup -- never a silent 404.
    start = datetime(2026, 6, 22, 13, 0, tzinfo=timezone.utc)
    end = datetime(2026, 6, 22, 13, 30, tzinfo=timezone.utc)
    with pytest.raises(GOESInputError):
        _list_archive_keys_in_window("himawari-9", start, end)


# ---- Fire Temperature band math (the testable recipe core) ----------------


def test_red_stretch_brightness_temp_endpoints():
    """RED: 273.15 K (0 C) -> 0.0; 333.15 K (60 C) -> 1.0; clipped; gamma 1."""
    lo, hi = FIRE_TEMP_RED_KELVIN_RANGE
    arr = np.array([lo, (lo + hi) / 2.0, hi, lo - 50.0, hi + 50.0], dtype=np.float32)
    red = _stretch_brightness_temp_red(arr)
    assert red[0] == pytest.approx(0.0)        # 0 C
    assert red[1] == pytest.approx(0.5)        # 30 C -> midpoint (gamma 1, linear)
    assert red[2] == pytest.approx(1.0)        # 60 C
    assert red[3] == pytest.approx(0.0)        # below range clips to 0
    assert red[4] == pytest.approx(1.0)        # above range clips to 1


def test_red_stretch_nan_to_zero():
    red = _stretch_brightness_temp_red(np.array([np.nan, 303.15], dtype=np.float32))
    assert red[0] == pytest.approx(0.0)  # no-data reads dark, not NaN


def test_green_reflectance_stretch_100pct():
    """GREEN: 0..1.0 reflectance -> 0..1 (100 %); gamma 1; clip."""
    g = _stretch_reflectance(
        np.array([0.0, 0.5, 1.0, 1.5], dtype=np.float32), FIRE_TEMP_GREEN_REFL_MAX
    )
    assert g[0] == pytest.approx(0.0)
    assert g[1] == pytest.approx(0.5)
    assert g[2] == pytest.approx(1.0)
    assert g[3] == pytest.approx(1.0)  # over 100 % clips


def test_blue_reflectance_stretch_75pct():
    """BLUE: 0..0.75 reflectance -> 0..1 (75 %); so 0.75 saturates, 0.375 -> 0.5."""
    b = _stretch_reflectance(
        np.array([0.0, 0.375, 0.75, 1.0], dtype=np.float32), FIRE_TEMP_BLUE_REFL_MAX
    )
    assert b[0] == pytest.approx(0.0)
    assert b[1] == pytest.approx(0.5)
    assert b[2] == pytest.approx(1.0)
    assert b[3] == pytest.approx(1.0)  # above 0.75 clips


def test_fire_temperature_rgb_shape_and_dtype():
    c07 = np.full((4, 5), 303.15, dtype=np.float32)  # 30 C
    c06 = np.full((4, 5), 0.5, dtype=np.float32)
    c05 = np.full((4, 5), 0.375, dtype=np.float32)
    rgb = _fire_temperature_rgb(c07, c06, c05)
    assert rgb.shape == (3, 4, 5)
    assert rgb.dtype == np.uint8


def test_fire_temperature_hot_pixel_reads_red_to_white():
    """HOT-PIXEL recipe assertion: a saturated fire core (very hot 3.9um BT + high
    2.2um + 1.6um reflectance) reads WHITE (R=G=B=255); a hot-but-low-reflectance
    pixel reads pure RED; a moderately warm pixel reads partial red; a cool/dark
    pixel reads near-black. This is the red->yellow->white fire ramp."""
    # One 3-pixel row: [cool, warm-only-red, white-hot-core].
    # RED channel (C07 BT, K): 263 K (cold, below 0 C) | 303.15 K (30 C) | 343 K (>60 C, saturates)
    c07 = np.array([[263.0, 303.15, 343.0]], dtype=np.float32)
    # GREEN channel (C06 refl): 0 | 0 | 1.0 (max -> 100 %)
    c06 = np.array([[0.0, 0.0, 1.0]], dtype=np.float32)
    # BLUE channel (C05 refl): 0 | 0 | 0.75 (max -> 75 % saturates)
    c05 = np.array([[0.0, 0.0, 0.75]], dtype=np.float32)
    rgb = _fire_temperature_rgb(c07, c06, c05)

    # Pixel 0 (cool/dark): all channels near 0 -> black.
    assert tuple(int(v) for v in rgb[:, 0, 0]) == (0, 0, 0)
    # Pixel 1 (warm, only thermal): R high (30 C -> ~0.5 -> ~128), G=B=0 -> pure red.
    assert rgb[0, 0, 1] > 100      # red present
    assert rgb[1, 0, 1] == 0       # no green
    assert rgb[2, 0, 1] == 0       # no blue
    # Pixel 2 (white-hot core): R saturates, G + B climb -> white (all 255).
    assert tuple(int(v) for v in rgb[:, 0, 2]) == (255, 255, 255)
    # The ramp: red rises across the row (cold -> warm -> hot).
    assert rgb[0, 0, 0] < rgb[0, 0, 1] < rgb[0, 0, 2]


def test_fire_temperature_rgb_co_registration_shape_mismatch_raises():
    c07 = np.zeros((4, 5), dtype=np.float32)
    c06 = np.zeros((4, 6), dtype=np.float32)  # wrong width
    c05 = np.zeros((4, 5), dtype=np.float32)
    with pytest.raises(GOESArchiveUpstreamError):
        _fire_temperature_rgb(c07, c06, c05)


# ---- CF scale_factor/add_offset is applied --------------------------------


def test_cf_scaling_applied_in_reproject(monkeypatch, tmp_path):
    """``_reproject_fire_temperature`` must apply CF scale_factor/add_offset per
    band (rasterio NETCDF does NOT auto-apply). We stub netCDF4 + rasterio so the
    test asserts the raw int16 DN are unscaled into physical units BEFORE the Fire-
    Temp composite -- the band-math contract.

    We use a DISTINCTIVE scale/offset so a missing-CF bug is unmissable: C07
    scale=0.05, offset=200 -> DN 300 * 0.05 + 200 = 215 K (below 0 C -> red 0). If
    CF scaling were NOT applied, DN 300 would read as 300 K (= 26.85 C -> red ~114).
    The two outcomes are far apart, so RED==0 proves the CF transform ran. All raw
    DN stay inside the ABI valid_range [0, 4095] (a DN above 4095 is masked to NaN
    as out-of-range, so test inputs must respect that).
    """
    import trid3nt_server.tools.fetchers.imagery.fetch_goes_archive_animation as m

    # Fake netCDF4: a Dataset whose variables carry scale_factor/add_offset.
    class _FakeVar:
        def __init__(self, scale, offset, fill):
            self.scale_factor = scale
            self.add_offset = offset
            self._FillValue = fill

    class _FakeDataset:
        def __init__(self, path):
            self.variables = {
                "CMI_C07": _FakeVar(0.05, 200.0, -1),   # DN 300 -> 215 K (cold!)
                "CMI_C06": _FakeVar(0.0001, 0.0, -1),   # DN 4000 -> 0.4 refl
                "CMI_C05": _FakeVar(0.0001, 0.0, -1),   # DN 3000 -> 0.3 refl
            }

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _FakeNetCDF4:
        Dataset = _FakeDataset

    monkeypatch.setattr(m, "netCDF4", _FakeNetCDF4, raising=False)
    # Patch the lazily-imported netCDF4 inside the function via sys.modules so the
    # `import netCDF4` line picks up the fake.
    import sys

    monkeypatch.setitem(sys.modules, "netCDF4", _FakeNetCDF4)

    # Fake rasterio: src.open returns a handle; reproject fills the destination
    # with a fixed raw DN per variable (keyed off the subdataset URI).
    raw_dn = {"CMI_C07": 300, "CMI_C06": 4000, "CMI_C05": 3000}

    class _FakeSrc:
        def __init__(self, var):
            self.var = var
            self.crs = "GEOSTATIONARY"
            self.nodata = None
            self.transform = object()

        def close(self):
            pass

    class _FakeRasterio:
        @staticmethod
        def open(uri):
            var = uri.rsplit(":", 1)[-1]
            return _FakeSrc(var)

        @staticmethod
        def band(src, n):
            return src.var

    def _fake_reproject(source=None, destination=None, **kw):
        # ``source`` is the var name (from FakeRasterio.band). Fill dest with the
        # raw DN so the CF transform downstream is observable.
        destination[:] = raw_dn[source]

    from rasterio.transform import from_bounds as _real_from_bounds

    class _FakeWarp:
        Resampling = type("R", (), {"nearest": 0})()
        reproject = staticmethod(_fake_reproject)

    monkeypatch.setattr(m, "rasterio", _FakeRasterio, raising=False)
    monkeypatch.setitem(sys.modules, "rasterio", _FakeRasterio)
    monkeypatch.setitem(sys.modules, "rasterio.warp", _FakeWarp)
    # rasterio.transform.from_bounds is real (pure math); keep it.
    import types

    _transform_mod = types.SimpleNamespace(from_bounds=_real_from_bounds)
    monkeypatch.setitem(sys.modules, "rasterio.transform", _transform_mod)

    bbox = (-112.0, 39.0, -111.9, 39.08)
    rgb, transform, w, h = m._reproject_fire_temperature("/fake/path.nc", bbox)
    assert rgb.shape[0] == 3
    # C07 DN 300 with scale=0.05 offset=200 -> 215 K (< 273.15 = 0 C) -> RED == 0.
    # If CF scaling were NOT applied, DN 300 read as 300 K -> RED ~114. So RED==0
    # PROVES scale_factor/add_offset ran.
    assert int(rgb[0].max()) == 0, "RED must be 0 (215 K is below 0 C) -- proves CF scaling applied"
    # C06 DN 4000 * 0.0001 = 0.4 refl over 100 % max -> GREEN round(0.4*255) = 102.
    # If CF scaling were skipped, DN 4000 read raw would be masked (>4095 boundary)
    # or wildly wrong; 102 proves scale_factor ran on GREEN too.
    assert int(rgb[1].max()) == 102
    # C05 DN 3000 * 0.0001 = 0.3 refl over 0.75 max -> 0.4 -> BLUE round(0.4*255) = 102.
    assert int(rgb[2].max()) == 102


# ---- C07 14-bit valid_range (the RED-collapse regression) ------------------


def test_band_valid_dn_range_reads_per_band_range():
    """``_band_valid_dn_range`` must return the band's OWN valid_range -- 14-bit
    for the emissive C07, 12-bit for the reflective C05/C06. Hardcoding 4095
    masked ~every warm-land C07 pixel and collapsed RED to 0."""

    class _Var:
        def __init__(self, vr):
            self.valid_range = vr

    # Real ABI ranges: C07 (3.9um, emissive) is 14-bit, C05/C06 are 12-bit.
    assert _band_valid_dn_range(_Var([0, 16383])) == (0, 16383)  # C07
    assert _band_valid_dn_range(_Var([0, 4095])) == (0, 4095)    # C05/C06
    # numpy-array valid_range (how netCDF4 actually returns it) also works.
    assert _band_valid_dn_range(_Var(np.array([0, 16383], dtype="i2"))) == (0, 16383)


def test_band_valid_dn_range_falls_back_when_attr_absent_or_bad():
    """A missing / malformed ``valid_range`` falls back to the 14-bit default --
    a WIDE fallback never masks real DN (where the narrow 4095 did)."""

    class _NoAttr:
        pass

    class _BadAttr:
        valid_range = [5]  # too short

    class _DegenerateAttr:
        valid_range = [100, 100]  # hi not > lo

    assert _band_valid_dn_range(_NoAttr()) == (0, 16383)
    assert _band_valid_dn_range(_BadAttr()) == (0, 16383)
    assert _band_valid_dn_range(_DegenerateAttr()) == (0, 16383)


def test_warm_c07_dn_above_4095_unpacks_to_plausible_kelvin_and_reads_red(
    monkeypatch, tmp_path
):
    """REGRESSION (real-data RED=0 bug): the emissive C07 is a 14-bit band
    (valid_range [0, 16383]). A warm midday-land brightness temperature ~320 K
    is raw DN ~9368 -- WELL above the 12-bit 4095 ceiling. The old mask
    ``(warped > 4095)`` flagged that valid DN as out-of-range -> NaN -> RED 0
    across the whole frame (while the 12-bit G/B reflectance channels populated
    fine). This test pins: (a) a representative warm C07 DN unpacks to a
    plausible Kelvin in [270, 340], and (b) it survives the valid-range mask and
    produces a strong NON-ZERO RED. With the buggy hardcoded 4095 ceiling, RED
    would be 0 here."""
    import sys
    import types

    import trid3nt_server.tools.fetchers.imagery.fetch_goes_archive_animation as m

    # Real C07 CF params (from a live noaa-goes18 MCMIPC granule): scale
    # 0.01309618, offset 197.31, 14-bit valid_range [0, 16383], _FillValue -1.
    C07_SCALE, C07_OFFSET = 0.01309618, 197.31
    WARM_C07_DN = 9368  # -> ~320 K (a hot summer-land 3.9um BT)
    unpacked_k = WARM_C07_DN * C07_SCALE + C07_OFFSET
    assert 270.0 <= unpacked_k <= 340.0, f"warm C07 DN should be ~320 K, got {unpacked_k}"
    assert WARM_C07_DN > 4095, "the regression hinges on a valid DN above the old 4095 ceiling"

    class _FakeVar:
        def __init__(self, scale, offset, fill, valid_range):
            self.scale_factor = scale
            self.add_offset = offset
            self._FillValue = fill
            self.valid_range = valid_range

    class _FakeDataset:
        def __init__(self, path):
            self.variables = {
                # C07: 14-bit thermal, warm DN above the old 4095 boundary.
                "CMI_C07": _FakeVar(C07_SCALE, C07_OFFSET, -1, [0, 16383]),
                # C05/C06: 12-bit reflective.
                "CMI_C06": _FakeVar(0.00031746, 0.0, -1, [0, 4095]),
                "CMI_C05": _FakeVar(0.00031746, 0.0, -1, [0, 4095]),
            }

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _FakeNetCDF4:
        Dataset = _FakeDataset

    monkeypatch.setattr(m, "netCDF4", _FakeNetCDF4, raising=False)
    monkeypatch.setitem(sys.modules, "netCDF4", _FakeNetCDF4)

    raw_dn = {"CMI_C07": WARM_C07_DN, "CMI_C06": 2000, "CMI_C05": 2000}

    class _FakeSrc:
        def __init__(self, var):
            self.var = var
            self.crs = "GEOSTATIONARY"
            self.nodata = None
            self.transform = object()

        def close(self):
            pass

    class _FakeRasterio:
        @staticmethod
        def open(uri):
            return _FakeSrc(uri.rsplit(":", 1)[-1])

        @staticmethod
        def band(src, n):
            return src.var

    def _fake_reproject(source=None, destination=None, **kw):
        destination[:] = raw_dn[source]

    from rasterio.transform import from_bounds as _real_from_bounds

    class _FakeWarp:
        Resampling = type("R", (), {"nearest": 0})()
        reproject = staticmethod(_fake_reproject)

    monkeypatch.setattr(m, "rasterio", _FakeRasterio, raising=False)
    monkeypatch.setitem(sys.modules, "rasterio", _FakeRasterio)
    monkeypatch.setitem(sys.modules, "rasterio.warp", _FakeWarp)
    _transform_mod = types.SimpleNamespace(from_bounds=_real_from_bounds)
    monkeypatch.setitem(sys.modules, "rasterio.transform", _transform_mod)

    bbox = (-112.0, 39.0, -111.9, 39.08)
    rgb, transform, w, h = m._reproject_fire_temperature("/fake/path.nc", bbox)

    # The crux: warm C07 (~320 K) survives the valid-range mask and yields a
    # STRONG non-zero RED. ~320 K -> (320 - 273.15) / 60 ~= 0.78 -> ~199/255.
    assert int(rgb[0].max()) > 0, "warm C07 DN > 4095 must NOT be masked (the RED-collapse bug)"
    assert int(rgb[0].min()) > 100, "warm midday land must read a STRONG red, not near-black"
    # G/B unchanged (still 12-bit, well within range).
    assert int(rgb[1].max()) > 0
    assert int(rgb[2].max()) > 0


# ---- typed-error surface --------------------------------------------------


def test_bbox_none_raises_bbox_required():
    with pytest.raises(GOESArchiveBboxRequiredError):
        fetch_goes_archive_animation(bbox=None)  # type: ignore[arg-type]


def test_unknown_satellite_raises():
    # A genuinely-unknown bird fails LOUD on the shared _normalize_satellite seam
    # (typed GOESInputError listing the accepted forms) before any bucket/path is
    # built -- never a silent 404 / blank fetch.
    with pytest.raises(GOESInputError):
        fetch_goes_archive_animation(bbox=_UT_BBOX, satellite="himawari-9")


def test_unknown_band_raises():
    with pytest.raises(GOESArchiveInputError):
        fetch_goes_archive_animation(bbox=_UT_BBOX, band="geocolor")


# ---- shared satellite-normalizer migration (GOES-18 / goes18 / GOES West) ---


@pytest.mark.parametrize("spelling", ["GOES-18", "goes18", "GOES West"])
def test_forgiving_satellite_spelling_resolves_and_proceeds(monkeypatch, spelling):
    """The shared _normalize_satellite seam accepts every spelling: GOES-18 /
    goes18 / "GOES West" all canonicalize to goes-18 and the run PROCEEDS (instead
    of being rejected). We mirror the end-to-end mock (stub the S3 key listing +
    read_through) and assert the emitted layer names carry the canonical (GOES-18)
    bird -- proof the normalized token flowed all the way through.
    """
    times = [datetime(2026, 6, 22, 18, m, tzinfo=timezone.utc) for m in (0, 5, 10)]
    pairs = [(t, _mk_key(t)) for t in times]
    monkeypatch.setattr(mod, "_list_archive_keys_in_window", lambda *a, **k: list(pairs))

    def _fake_read_through(metadata, params, ext, fetch_fn):
        # The cache-key params must already carry the canonical token, never the
        # raw spelling -- the normalize-before-any-key-built contract.
        assert params["satellite"] == "goes-18"
        return _FakeReadResult(uri=f"s3://fake/{params['ts_start']}.tif")

    monkeypatch.setattr(mod, "read_through", _fake_read_through)

    layers = fetch_goes_archive_animation(
        bbox=_UT_BBOX,
        satellite=spelling,
        start_utc="2026-06-22T17:30:00Z",
        end_utc="2026-06-22T18:30:00Z",
    )
    assert len(layers) == 3
    # The (SAT) token in every emitted name is the canonical bird, not the raw
    # spelling -- so GOES-18 / goes18 / "GOES West" all land on GOES-18.
    assert all("(GOES-18)" in lyr.name for lyr in layers)


def test_genuinely_unknown_bird_still_raises_loud():
    """A genuinely-unknown bird (GOES-99) is not a real GOES satellite, so the
    shared normalizer fails LOUD (typed GOESInputError listing the accepted forms)
    -- never a silent 404 / blank fetch."""
    with pytest.raises(GOESInputError):
        fetch_goes_archive_animation(bbox=_UT_BBOX, satellite="GOES-99")


def test_list_archive_keys_forgiving_spelling_resolves(monkeypatch):
    """The directly-callable helper also normalizes its own entry: "GOES West"
    resolves to goes-18 and lists against the noaa-goes18 bucket (proceeds), where
    pre-migration it would have rejected the spelling."""
    seen_buckets: list[str] = []

    def _fake_list(bucket, prefix, *, session=None):
        seen_buckets.append(bucket)
        return []

    monkeypatch.setattr(mod, "_list_keys_for_prefix", _fake_list)
    start = datetime(2026, 6, 22, 13, 0, tzinfo=timezone.utc)
    end = datetime(2026, 6, 22, 13, 30, tzinfo=timezone.utc)
    # "GOES West" -> goes-18 -> the noaa-goes18 bucket (no rejection).
    assert _list_archive_keys_in_window("GOES West", start, end) == []
    assert seen_buckets and all(b == "noaa-goes18" for b in seen_buckets)


def test_list_archive_keys_genuinely_unknown_bird_raises_loud():
    """The helper fails LOUD on a genuinely-unknown bird via the shared seam."""
    start = datetime(2026, 6, 22, 13, 0, tzinfo=timezone.utc)
    end = datetime(2026, 6, 22, 13, 30, tzinfo=timezone.utc)
    with pytest.raises(GOESInputError):
        _list_archive_keys_in_window("GOES-99", start, end)


def test_degenerate_bbox_raises():
    with pytest.raises(GOESArchiveInputError):
        fetch_goes_archive_animation(bbox=(-112.0, 39.0, -112.0, 39.0))


def test_empty_window_raises_typed_empty(monkeypatch):
    """No archived frames in the window -> honest typed empty (never blank anim)."""
    monkeypatch.setattr(mod, "_list_archive_keys_in_window", lambda *a, **k: [])
    with pytest.raises(GOESArchiveEmptyError):
        fetch_goes_archive_animation(
            bbox=_UT_BBOX,
            satellite="goes-18",
            start_utc="2020-01-01T00:00:00Z",
            end_utc="2020-01-01T01:00:00Z",
        )


# ---- emitted name token: matches the fetch_goes_animation scrubber contract --


class _FakeReadResult:
    def __init__(self, uri):
        self.uri = uri


def test_emitted_name_carries_step_token_iso_and_style(monkeypatch):
    """The frames carry the SAME scrubber-group contract as fetch_goes_animation:
    a "step <N>" monotonic token, the product label stem, the ISO valid-time, and
    the shared "goes_rgb_animation" style preset (so Track A + the web scrubber
    consume the archive frames unchanged)."""
    times = [datetime(2026, 6, 22, 18, m, tzinfo=timezone.utc) for m in (0, 5, 10)]
    pairs = [(t, _mk_key(t)) for t in times]

    monkeypatch.setattr(mod, "_list_archive_keys_in_window", lambda *a, **k: list(pairs))

    def _fake_read_through(metadata, params, ext, fetch_fn):
        return _FakeReadResult(uri=f"s3://fake/{params['ts_start']}.tif")

    monkeypatch.setattr(mod, "read_through", _fake_read_through)

    layers = fetch_goes_archive_animation(
        bbox=_UT_BBOX,
        satellite="goes-18",
        start_utc="2026-06-22T17:30:00Z",
        end_utc="2026-06-22T18:30:00Z",
    )
    assert len(layers) == 3
    for n, (layer, t) in enumerate(zip(layers, times), start=1):
        iso = t.strftime("%Y-%m-%dT%H:%M:%SZ")
        assert layer.name == f"GOES Fire Temperature (Archive) step {n} {iso} (GOES-18)"
        assert layer.layer_type == "raster"
        assert layer.role == "context"
        assert layer.style_preset == "goes_rgb_animation"
    # Monotonic step values 1..3.
    steps = [int(re.search(r"step (\d+)", lyr.name).group(1)) for lyr in layers]
    assert steps == [1, 2, 3]
    # Shared style preset across the group + distinct "(Archive)" stem.
    assert {lyr.style_preset for lyr in layers} == {"goes_rgb_animation"}
    assert all("(Archive)" in lyr.name for lyr in layers)


def test_run_honesty_floor_all_frames_empty(monkeypatch):
    """Every frame empty/failed -> the run honesty-floors (typed empty, no blank
    animation)."""
    times = [datetime(2026, 6, 22, 18, m, tzinfo=timezone.utc) for m in (0, 5, 10)]
    pairs = [(t, _mk_key(t)) for t in times]
    monkeypatch.setattr(mod, "_list_archive_keys_in_window", lambda *a, **k: list(pairs))

    def _always_empty(metadata, params, ext, fetch_fn):
        raise GOESArchiveEmptyError("AOI crop empty")

    monkeypatch.setattr(mod, "read_through", _always_empty)
    with pytest.raises(GOESArchiveEmptyError):
        fetch_goes_archive_animation(
            bbox=_UT_BBOX,
            satellite="goes-18",
            start_utc="2026-06-22T17:30:00Z",
            end_utc="2026-06-22T18:30:00Z",
        )


def test_start_after_end_raises():
    with pytest.raises(GOESArchiveInputError):
        fetch_goes_archive_animation(
            bbox=_UT_BBOX,
            start_utc="2026-06-22T20:00:00Z",
            end_utc="2026-06-22T13:00:00Z",
        )


# ===========================================================================
# Active-fire ISOLATION ("fire-only") product + BAKE composite (the new work).
# ===========================================================================


# ---- the active-fire detection threshold logic ----------------------------


def test_detect_active_fire_uses_standard_thresholds():
    """The detector uses the STANDARD shortwave-vs-longwave discriminator, NOT a
    single-band brightness threshold:

      active_fire = (C07 BT >= bt_c07_min_k) AND (C07 - C13 BT >= bt_diff_min_k)

    Synthetic 3-pixel row, with the DEFAULT thresholds (320 K / 10 K):
      - fire pixel:     C07=345 K, C13=300 K -> hot (>=320) AND diff=45 (>=10) -> FIRE
      - warm land:      C07=320 K, C13=315 K -> hot (>=320) BUT diff=5 (<10)   -> NOT
      - cool pixel:     C07=290 K, C13=288 K -> not hot (<320)                 -> NOT
    The warm-land pixel is the crux: a single-band C07 threshold (320 K) would
    FLAG it (it IS hot), but the small C07-C13 split rejects it as warm land.
    """
    c07 = np.array([[345.0, 320.0, 290.0]], dtype=np.float32)
    c13 = np.array([[300.0, 315.0, 288.0]], dtype=np.float32)
    mask = _detect_active_fire_mask(c07, c13)
    assert mask.dtype == bool
    assert mask.shape == (1, 3)
    assert bool(mask[0, 0]) is True, "fire pixel (hot + big split) must flag"
    assert bool(mask[0, 1]) is False, "warm land (hot but small split) must NOT flag"
    assert bool(mask[0, 2]) is False, "cool pixel must NOT flag"


def test_detect_active_fire_fire_pixel_passes():
    """A canonical fire pixel C07=345 K / C13=300 K passes (the kickoff case)."""
    mask = _detect_active_fire_mask(
        np.array([[345.0]], dtype=np.float32), np.array([[300.0]], dtype=np.float32)
    )
    assert bool(mask[0, 0]) is True


def test_detect_active_fire_warm_land_small_diff_fails():
    """Warm daytime land C07=320 K / C13=315 K (small diff) FAILS (the kickoff
    case): it is absolutely hot but the 5 K split is below the 10 K floor, so the
    difference test rejects it -- exactly what a single-band threshold can't do."""
    mask = _detect_active_fire_mask(
        np.array([[320.0]], dtype=np.float32), np.array([[315.0]], dtype=np.float32)
    )
    assert bool(mask[0, 0]) is False


def test_detect_active_fire_cool_pixel_fails():
    """A cool pixel (below the absolute floor) FAILS even with a moderate split."""
    mask = _detect_active_fire_mask(
        np.array([[305.0]], dtype=np.float32), np.array([[290.0]], dtype=np.float32)
    )
    # diff is 15 K (>=10) but C07 305 K < 320 K floor -> NOT fire.
    assert bool(mask[0, 0]) is False


def test_detect_active_fire_nan_pixels_are_not_fire():
    """A NaN in either band (no-data / off-disk) yields NOT-fire (no false flag)."""
    c07 = np.array([[np.nan, 345.0]], dtype=np.float32)
    c13 = np.array([[300.0, np.nan]], dtype=np.float32)
    mask = _detect_active_fire_mask(c07, c13)
    assert bool(mask[0, 0]) is False
    assert bool(mask[0, 1]) is False


def test_detect_active_fire_thresholds_are_tunable():
    """Raising bt_diff_min_k makes the detector STRICTER; lowering bt_c07_min_k
    makes it more permissive -- both gates are tunable params."""
    c07 = np.array([[330.0]], dtype=np.float32)
    c13 = np.array([[318.0]], dtype=np.float32)  # diff = 12 K
    # Default (10 K diff floor): 12 K passes -> fire.
    assert bool(_detect_active_fire_mask(c07, c13)[0, 0]) is True
    # Stricter (20 K diff floor): 12 K fails -> NOT fire.
    assert bool(_detect_active_fire_mask(c07, c13, bt_diff_min_k=20.0)[0, 0]) is False
    # A cool-but-high-split pixel: lowering the absolute floor admits it.
    cool = np.array([[312.0]], dtype=np.float32)
    cool_lw = np.array([[295.0]], dtype=np.float32)  # diff = 17 K
    assert bool(_detect_active_fire_mask(cool, cool_lw)[0, 0]) is False  # 312<320
    assert bool(
        _detect_active_fire_mask(cool, cool_lw, bt_c07_min_k=310.0)[0, 0]
    ) is True


def test_detect_active_fire_shape_mismatch_raises():
    with pytest.raises(GOESArchiveUpstreamError):
        _detect_active_fire_mask(
            np.zeros((2, 3), dtype=np.float32), np.zeros((2, 4), dtype=np.float32)
        )


def test_default_thresholds_are_defensible():
    """The shipped defaults match the documented derivation: 320 K absolute floor
    (above warm-land BT, below ~330 K C07 saturation) + 10 K MODIS delta-T
    heritage difference floor; C07/C13 are the detection bands."""
    assert FIRE_BT_C07_MIN_K == pytest.approx(320.0)
    assert FIRE_BT_DIFF_MIN_K == pytest.approx(10.0)
    assert FIRE_DETECT_BANDS["shortwave"] == "CMI_C07"
    assert FIRE_DETECT_BANDS["longwave"] == "CMI_C13"
    # The three original thermal/fire products plus the additive true_color band.
    assert set(ARCHIVE_BANDS) == {
        "fire_temperature",
        "true_color",
        "fire_hotspots",
        "fire_baked",
    }


# ---- true_color band: finer res, CIMSS synthetic green, defaults unchanged -----


def test_default_res_grid_unchanged_for_thermal_bands():
    """The existing thermal/fire bands MUST stay on the 0.02 deg grid -- a fixed
    bbox produces the SAME (width, height) as before the res_deg plumbing, and an
    explicit _OUT_RES_DEG is byte-identical to the default."""
    bbox = (-114.0, 39.0, -113.0, 40.0)  # 1x1 degree
    _, w_default, h_default = mod._grid_for_bbox(bbox)
    assert (w_default, h_default) == (50, 50)  # 1.0 / 0.02 == 50
    _, w_explicit, h_explicit = mod._grid_for_bbox(bbox, mod._OUT_RES_DEG)
    assert (w_explicit, h_explicit) == (w_default, h_default)
    # _resolve_res_deg pins every thermal/fire band to _OUT_RES_DEG regardless of
    # any true_color override.
    for band in ("fire_temperature", "fire_hotspots", "fire_baked"):
        assert mod._resolve_res_deg(band, None) == mod._OUT_RES_DEG
        assert mod._resolve_res_deg(band, 0.001) == mod._OUT_RES_DEG


def test_true_color_reaches_finer_resolution():
    """true_color reprojects onto the finer _TRUE_COLOR_RES_DEG (0.005 deg ~0.5 km)
    grid -- a 4x-finer cell than the 0.02 deg thermal grid for the SAME bbox."""
    bbox = (-114.0, 39.0, -113.0, 40.0)
    _, w_thermal, h_thermal = mod._grid_for_bbox(bbox, mod._OUT_RES_DEG)
    _, w_tc, h_tc = mod._grid_for_bbox(bbox, mod._resolve_res_deg("true_color", None))
    assert mod._TRUE_COLOR_RES_DEG == pytest.approx(0.005)
    assert mod._resolve_res_deg("true_color", None) == pytest.approx(0.005)
    assert w_tc == 4 * w_thermal
    assert h_tc == 4 * h_thermal
    # An explicit override wins.
    assert mod._resolve_res_deg("true_color", 0.0025) == pytest.approx(0.0025)


def test_true_color_rgb_synthetic_green_recipe():
    """_true_color_rgb composites R=C02, B=C01, synthetic G=a*R+b*veg+c*B with the
    CIMSS coefficients (0.45, 0.10, 0.45) and a brightening gamma; NaN -> 0."""
    a, b, c = mod.TRUE_COLOR_GREEN_COEFFS
    assert (a, b, c) == (0.45, 0.10, 0.45)
    assert mod.TRUE_COLOR_GAMMA == pytest.approx(1.0 / 2.2)
    assert mod.TRUE_COLOR_BANDS == {
        "red": "CMI_C02",
        "blue": "CMI_C01",
        "veggie": "CMI_C03",
    }
    red = np.array([[0.6, 0.0]], dtype=np.float32)
    blue = np.array([[0.4, 0.0]], dtype=np.float32)
    veg = np.array([[0.5, np.nan]], dtype=np.float32)
    rgb = mod._true_color_rgb(red, blue, veg)
    assert rgb.shape == (3, 1, 2)
    assert rgb.dtype == np.uint8
    # Pixel 0: synthetic green = 0.45*0.6 + 0.10*0.5 + 0.45*0.4 = 0.5 (pre-gamma);
    # each channel gamma-stretched then * 255. Gamma<1 brightens, so each value is
    # >= its linear value.
    g = mod.TRUE_COLOR_GAMMA
    assert rgb[0, 0, 0] == int(round((0.6 ** g) * 255.0))
    expected_green01 = 0.45 * 0.6 + 0.10 * 0.5 + 0.45 * 0.4
    assert rgb[1, 0, 0] == int(round((expected_green01 ** g) * 255.0))
    assert rgb[2, 0, 0] == int(round((0.4 ** g) * 255.0))
    # Pixel 1: all-zero (NaN veg -> 0) reads black.
    assert rgb[0, 0, 1] == 0 and rgb[1, 0, 1] == 0 and rgb[2, 0, 1] == 0


def test_true_color_alias_normalization_and_unknown_band():
    """natural_color / geocolor_raw normalize to true_color; an unknown band still
    raises GOESArchiveInputError."""
    assert mod._BAND_ALIASES["natural_color"] == "true_color"
    assert mod._BAND_ALIASES["geocolor_raw"] == "true_color"
    assert mod._PRODUCT_LABELS["true_color"] == "True Color (Archive)"
    assert mod._PRODUCT_ID_SLUGS["true_color"] == "truecolor"
    assert mod._PRODUCT_STYLE_PRESETS["true_color"] == "goes_rgb_animation"


# ---- the fire-only RGBA isolation: alpha 0 off-fire, >0 only at fire -------


def test_fire_hotspots_rgba_alpha_transparent_off_fire():
    """The fire-only RGBA layer: alpha == 0 (fully transparent) on EVERY non-fire
    pixel and alpha > 0 ONLY where active fire is detected. 3-pixel row
    [fire, warm-land, cool]."""
    c07 = np.array([[345.0, 320.0, 290.0]], dtype=np.float32)
    c13 = np.array([[300.0, 315.0, 288.0]], dtype=np.float32)
    rgba = _fire_hotspots_rgba(c07, c13)
    assert rgba.shape == (4, 1, 3)
    assert rgba.dtype == np.uint8
    alpha = rgba[3]
    # Fire pixel: alpha opaque (>0).
    assert int(alpha[0, 0]) > 0
    # Warm-land + cool: fully transparent (alpha 0).
    assert int(alpha[0, 1]) == 0
    assert int(alpha[0, 2]) == 0
    # And the COLOR is zeroed on transparent pixels (no leaked color under a=0).
    assert tuple(int(v) for v in rgba[:3, 0, 1]) == (0, 0, 0)
    assert tuple(int(v) for v in rgba[:3, 0, 2]) == (0, 0, 0)


def test_fire_hotspots_rgba_fire_pixel_is_opaque_hot_color():
    """A detected fire pixel is OPAQUE (alpha 255) on the hot ramp -- red present
    (R high), and the hottest cores climb toward yellow/white."""
    # A very hot core (C07=348 K saturating) and a cooler-but-real fire (C07=325).
    c07 = np.array([[348.0, 325.0]], dtype=np.float32)
    c13 = np.array([[300.0, 305.0]], dtype=np.float32)  # diffs 48, 20 -> both fire
    rgba = _fire_hotspots_rgba(c07, c13)
    assert int(rgba[3, 0, 0]) == 255  # opaque fire
    assert int(rgba[3, 0, 1]) == 255
    # R is saturated across the hot ramp (fire is always red-dominant).
    assert int(rgba[0, 0, 0]) == 255
    assert int(rgba[0, 0, 1]) == 255
    # The hotter pixel (348 K) is closer to white -> its GREEN >= the cooler one's.
    assert int(rgba[1, 0, 0]) >= int(rgba[1, 0, 1])


def test_fire_hotspots_rgba_all_transparent_when_no_fire():
    """A frame with NO active fire (all warm land) is a fully-transparent RGBA --
    alpha 0 everywhere; not an error at the pure-function layer."""
    c07 = np.full((3, 3), 318.0, dtype=np.float32)  # warm but below 320 floor
    c13 = np.full((3, 3), 316.0, dtype=np.float32)  # small split
    rgba = _fire_hotspots_rgba(c07, c13)
    assert int(rgba[3].max()) == 0  # alpha 0 everywhere
    assert int(rgba[:3].max()) == 0  # no color anywhere


# ---- the BAKE alpha-composite ---------------------------------------------


def test_bake_fire_over_base_alpha_composite():
    """Bake = fire RGBA over a base RGB. Where fire alpha is 0 the base shows
    through UNCHANGED; where alpha is 255 the fire color fully replaces the base.
    3-pixel base row, fire only on pixel 1."""
    # Base: a neutral grey scene, (3 bands, H=1, W=3).
    base = np.full((3, 1, 3), 100, dtype=np.uint8)
    # Fire RGBA: pixel 1 opaque orange (255,140,0,255); others transparent.
    fire = np.zeros((4, 1, 3), dtype=np.uint8)
    fire[:, 0, 1] = (255, 140, 0, 255)
    out = _bake_fire_over_base(base, fire)
    assert out.shape == (3, 1, 3)
    assert out.dtype == np.uint8
    # Pixel 0 + 2 (fire alpha 0): base grey shows through UNCHANGED.
    assert tuple(int(v) for v in out[:, 0, 0]) == (100, 100, 100)
    assert tuple(int(v) for v in out[:, 0, 2]) == (100, 100, 100)
    # Pixel 1 (fire alpha 255): the fire color fully replaces the base.
    assert tuple(int(v) for v in out[:, 0, 1]) == (255, 140, 0)


def test_bake_fire_over_base_partial_alpha_blends():
    """A partial alpha blends base and fire: out = base*(1-a) + fire*a."""
    base = np.full((3, 1, 1), 100, dtype=np.uint8)
    fire = np.zeros((4, 1, 1), dtype=np.uint8)
    fire[:, 0, 0] = (200, 0, 0, 128)  # ~50% alpha
    out = _bake_fire_over_base(base, fire)
    a = 128 / 255.0
    expected_r = round(100 * (1 - a) + 200 * a)
    assert int(out[0, 0, 0]) == expected_r
    # G/B: base 100 fades toward fire 0.
    assert int(out[1, 0, 0]) == round(100 * (1 - a))


def test_bake_shape_mismatch_raises():
    with pytest.raises(GOESArchiveUpstreamError):
        _bake_fire_over_base(
            np.zeros((3, 2, 2), dtype=np.uint8), np.zeros((4, 2, 3), dtype=np.uint8)
        )


def test_bake_end_to_end_fire_over_fire_temp_base():
    """End-to-end: detect+isolate fire RGBA, then bake over a Fire-Temp base.
    The baked pixel at the fire core carries fire color; non-fire pixels keep the
    Fire-Temp base."""
    # Build a Fire-Temp base + a co-registered detection pair: pixel 0 fire,
    # pixel 1 warm land.
    c07 = np.array([[346.0, 320.0]], dtype=np.float32)
    c13 = np.array([[300.0, 316.0]], dtype=np.float32)
    c06 = np.array([[0.2, 0.2]], dtype=np.float32)
    c05 = np.array([[0.1, 0.1]], dtype=np.float32)
    base = _fire_temperature_rgb(c07, c06, c05)
    fire = _fire_hotspots_rgba(c07, c13)
    baked = _bake_fire_over_base(base, fire)
    assert baked.shape == (3, 1, 2)
    # Fire pixel: baked color == the fire color (alpha 255 fully replaced base).
    assert tuple(int(v) for v in baked[:, 0, 0]) == tuple(int(v) for v in fire[:3, 0, 0])
    # Warm-land pixel: baked == the untouched Fire-Temp base (fire alpha 0).
    assert tuple(int(v) for v in baked[:, 0, 1]) == tuple(int(v) for v in base[:, 0, 1])


# ---- the new band/product surface on the tool -----------------------------


def test_fire_hotspots_band_emits_rgba_preset_and_name(monkeypatch):
    """band='fire_hotspots' emits the isolated layer: its own product label +
    style preset + id slug, threshold params in the cache key, the SAME step/ISO
    scrubber contract."""
    times = [datetime(2026, 6, 23, 19, m, tzinfo=timezone.utc) for m in (0, 5)]
    pairs = [(t, _mk_key(t)) for t in times]
    monkeypatch.setattr(mod, "_list_archive_keys_in_window", lambda *a, **k: list(pairs))

    captured_params = []

    def _fake_read_through(metadata, params, ext, fetch_fn):
        captured_params.append(params)
        return _FakeReadResult(uri=f"s3://fake/{params['product']}-{params['ts_start']}.tif")

    monkeypatch.setattr(mod, "read_through", _fake_read_through)

    layers = fetch_goes_archive_animation(
        bbox=_UT_BBOX,
        satellite="goes-18",
        start_utc="2026-06-23T18:30:00Z",
        end_utc="2026-06-23T19:30:00Z",
        band="fire_hotspots",
        bt_c07_min_k=322.0,
        bt_diff_min_k=12.0,
    )
    assert len(layers) == 2
    for n, (layer, t) in enumerate(zip(layers, times), start=1):
        iso = t.strftime("%Y-%m-%dT%H:%M:%SZ")
        assert layer.name == f"GOES Active Fire Hotspots (Archive) step {n} {iso} (GOES-18)"
        assert layer.style_preset == "goes_fire_hotspots_rgba"
        assert "firehot" in layer.layer_id
        assert layer.layer_type == "raster"
        assert layer.role == "context"
    # The tunable thresholds entered the cache key.
    assert captured_params[0]["product"] == "fire_hotspots"
    assert captured_params[0]["bt_c07_min_k"] == pytest.approx(322.0)
    assert captured_params[0]["bt_diff_min_k"] == pytest.approx(12.0)


def test_fire_baked_band_emits_rgb_preset_and_name(monkeypatch):
    """band='fire_baked' emits the baked composite: its own label + the RGB
    passthrough preset (no new style) + its own id slug."""
    times = [datetime(2026, 6, 23, 19, m, tzinfo=timezone.utc) for m in (0,)]
    pairs = [(t, _mk_key(t)) for t in times]
    monkeypatch.setattr(mod, "_list_archive_keys_in_window", lambda *a, **k: list(pairs))
    monkeypatch.setattr(
        mod, "read_through",
        lambda metadata, params, ext, fetch_fn: _FakeReadResult(uri="s3://fake/baked.tif"),
    )
    layers = fetch_goes_archive_animation(
        bbox=_UT_BBOX,
        band="fire_baked",
        start_utc="2026-06-23T18:55:00Z",
        end_utc="2026-06-23T19:05:00Z",
    )
    assert len(layers) == 1
    assert layers[0].name.startswith("GOES Fire Baked on Imagery (Archive) step 1 ")
    assert layers[0].style_preset == "goes_rgb_animation"
    assert "firebaked" in layers[0].layer_id


def test_fire_temperature_band_unchanged(monkeypatch):
    """The original fire_temperature product is UNCHANGED: same label, preset, id
    slug, and cache-key product value -- both products coexist."""
    times = [datetime(2026, 6, 23, 19, 0, tzinfo=timezone.utc)]
    pairs = [(t, _mk_key(t)) for t in times]
    monkeypatch.setattr(mod, "_list_archive_keys_in_window", lambda *a, **k: list(pairs))

    seen = {}

    def _fake(metadata, params, ext, fetch_fn):
        seen.update(params)
        return _FakeReadResult(uri="s3://fake/ft.tif")

    monkeypatch.setattr(mod, "read_through", _fake)
    layers = fetch_goes_archive_animation(
        bbox=_UT_BBOX,
        band="fire_temperature",
        start_utc="2026-06-23T18:55:00Z",
        end_utc="2026-06-23T19:05:00Z",
    )
    assert layers[0].name.startswith("GOES Fire Temperature (Archive) step 1 ")
    assert layers[0].style_preset == "goes_rgb_animation"
    assert "firetemp" in layers[0].layer_id
    # Fire-Temp does NOT inject threshold params into the cache key (its key stays
    # stable vs the pre-change Fire-Temp objects).
    assert seen["product"] == "fire_temperature"
    assert "bt_c07_min_k" not in seen


def test_unknown_band_still_raises_with_new_set():
    with pytest.raises(GOESArchiveInputError):
        fetch_goes_archive_animation(bbox=_UT_BBOX, band="geocolor")


def test_non_finite_thresholds_raise():
    with pytest.raises(GOESArchiveInputError):
        fetch_goes_archive_animation(
            bbox=_UT_BBOX, band="fire_hotspots", bt_c07_min_k=float("nan")
        )


# ---- the I/O reproject path for the new products (stubbed netCDF/rasterio) -


def _install_fake_nc_rasterio(monkeypatch, dn_by_var, cf_by_var):
    """Install fake netCDF4 + rasterio + rasterio.warp so the reproject helpers
    fill each band's destination with a fixed raw DN (keyed by variable). Mirrors
    the existing CF-scaling test's harness, generalized over the band set."""
    import sys
    import types

    import trid3nt_server.tools.fetchers.imagery.fetch_goes_archive_animation as m

    class _FakeVar:
        def __init__(self, scale, offset, fill, valid_range):
            self.scale_factor = scale
            self.add_offset = offset
            self._FillValue = fill
            self.valid_range = valid_range

    class _FakeDataset:
        def __init__(self, path):
            self.variables = {
                var: _FakeVar(*cf_by_var[var]) for var in cf_by_var
            }

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _FakeNetCDF4:
        Dataset = _FakeDataset

    monkeypatch.setattr(m, "netCDF4", _FakeNetCDF4, raising=False)
    monkeypatch.setitem(sys.modules, "netCDF4", _FakeNetCDF4)

    class _FakeSrc:
        def __init__(self, var):
            self.var = var
            self.crs = "GEOSTATIONARY"
            self.nodata = None
            self.transform = object()

        def close(self):
            pass

    class _FakeRasterio:
        @staticmethod
        def open(uri):
            return _FakeSrc(uri.rsplit(":", 1)[-1])

        @staticmethod
        def band(src, n):
            return src.var

    def _fake_reproject(source=None, destination=None, **kw):
        destination[:] = dn_by_var[source]

    from rasterio.transform import from_bounds as _real_from_bounds

    class _FakeWarp:
        Resampling = type("R", (), {"nearest": 0})()
        reproject = staticmethod(_fake_reproject)

    monkeypatch.setattr(m, "rasterio", _FakeRasterio, raising=False)
    monkeypatch.setitem(sys.modules, "rasterio", _FakeRasterio)
    monkeypatch.setitem(sys.modules, "rasterio.warp", _FakeWarp)
    _transform_mod = types.SimpleNamespace(from_bounds=_real_from_bounds)
    monkeypatch.setitem(sys.modules, "rasterio.transform", _transform_mod)
    return m


def test_reproject_fire_hotspots_flags_only_fire(monkeypatch):
    """``_reproject_fire_hotspots`` reads C07 + C13, CF-scales them, and isolates
    fire. A warm-land DN pair (C07 ~322 K, C13 ~319 K, small split) yields a
    FULLY TRANSPARENT frame; a fire DN pair (C07 ~345 K, C13 ~300 K) yields an
    opaque hotspot. Uses CF scale 1.0 / offset 0.0 so DN == Kelvin for clarity."""
    cf = {
        "CMI_C07": (1.0, 0.0, -1, [0, 16383]),
        "CMI_C13": (1.0, 0.0, -1, [0, 16383]),
    }
    bbox = (-112.0, 39.0, -111.9, 39.08)

    # Warm land: C07 322, C13 319 -> diff 3 K < 10 -> NO fire -> all transparent.
    m = _install_fake_nc_rasterio(
        monkeypatch, {"CMI_C07": 322, "CMI_C13": 319}, cf
    )
    rgba, transform, w, h = m._reproject_fire_hotspots("/fake/path.nc", bbox)
    assert rgba.shape[0] == 4
    assert int(rgba[3].max()) == 0, "warm land must be fully transparent"

    # Fire: C07 345, C13 300 -> diff 45 K -> fire -> opaque hotspot.
    m = _install_fake_nc_rasterio(
        monkeypatch, {"CMI_C07": 345, "CMI_C13": 300}, cf
    )
    rgba2, _, _, _ = m._reproject_fire_hotspots("/fake/path.nc", bbox)
    assert int(rgba2[3].max()) == 255, "fire pixel must be opaque"
    assert int(rgba2[0].max()) == 255, "fire pixel red must saturate"


def test_reproject_fire_baked_bakes_fire_over_base(monkeypatch):
    """``_reproject_fire_baked`` reads C07/C06/C05 + C13 in ONE pass and bakes the
    fire over the Fire-Temp base -> a 3-band RGB. A fire DN set yields a non-black
    baked frame whose fire pixel is red-dominant."""
    cf = {
        "CMI_C07": (1.0, 0.0, -1, [0, 16383]),
        "CMI_C06": (0.0001, 0.0, -1, [0, 4095]),
        "CMI_C05": (0.0001, 0.0, -1, [0, 4095]),
        "CMI_C13": (1.0, 0.0, -1, [0, 16383]),
    }
    bbox = (-112.0, 39.0, -111.9, 39.08)
    m = _install_fake_nc_rasterio(
        monkeypatch,
        {"CMI_C07": 345, "CMI_C06": 2000, "CMI_C05": 1500, "CMI_C13": 300},
        cf,
    )
    rgb, transform, w, h = m._reproject_fire_baked("/fake/path.nc", bbox)
    assert rgb.shape[0] == 3  # baked is 3-band RGB
    assert rgb.any(), "baked frame must not be all-black"
    # The fire pixel red saturates (fire baked over the base).
    assert int(rgb[0].max()) == 255
