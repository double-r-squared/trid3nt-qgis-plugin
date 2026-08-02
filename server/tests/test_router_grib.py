"""Offline coverage for the weather/GRIB fold (ADR 0069).

Migrates the value-bearing unit coverage of the deleted fetch_mrms_qpe twin onto
the router surface: the S3-listed key resolve phase (latest / targeted walkback)
and the grib_object whole-object decode (gunzip -> GRIB -> window -> sentinel-nodata
-> conditional reproject). The live network is stubbed by monkeypatching the shared
transport ``get_bytes`` with synthetic bodies (an S3 ListBucket XML for the resolve
probes; a gzipped GeoTIFF standing in for the .grib2.gz -- both formats decode via
GDAL, exactly the twin's own test stand-in). Live value-identical parity is the
separate LIVE gate (ADR 0069, harness PASS).
"""

from __future__ import annotations

import datetime as _dt
import gzip
import io

import numpy as np
import pytest
import rasterio
from rasterio.crs import CRS
from rasterio.io import MemoryFile
from rasterio.transform import from_bounds

from trid3nt_server.agent.tools.fetchers._router import router
from trid3nt_server.agent.tools.fetchers._router.errors import RouterError
from trid3nt_server.agent.tools.fetchers._router.executors import raster_cog
from trid3nt_server.agent.tools.fetchers._router.hooks import mrms_qpe as mq
from trid3nt_server.agent.tools.fetchers._router.spec import compose_specs_from_tree

_NODATA = -9999.0
_CONUS = (-130.0, 20.0, -60.0, 55.0)
_FLORIDA = (-82.0, 25.0, -80.0, 27.0)


@pytest.fixture(scope="module")
def spec():
    return compose_specs_from_tree()["fetch_mrms_qpe"]


# --------------------------------------------------------------------------- #
# Spec wiring
# --------------------------------------------------------------------------- #


def test_spec_is_grib_object_with_resolve_hooks(spec):
    assert spec.shape == "raster-cog"
    assert spec.source_class == "mrms_qpe"
    assert spec.supports_global_query is True
    assert (spec.ingest or {})["access"] == "grib_object"
    assert spec.hooks.resolve_build == "mrms_qpe.resolve_build"
    assert spec.hooks.resolve_parse == "mrms_qpe.resolve_parse"
    go = spec.ingest["grib_object"]
    assert go["nodata"] == _NODATA
    assert -3.0 in go["sentinel_equals"] and -1.0 in go["sentinel_equals"]


def test_promoted_tool_registered_under_twin_name():
    from trid3nt_server.agent import tools as _tools

    assert "fetch_mrms_qpe" in _tools.TOOL_REGISTRY
    meta = _tools.TOOL_REGISTRY["fetch_mrms_qpe"].metadata
    assert meta.ttl_class == "dynamic-1h"
    assert meta.source_class == "mrms_qpe"
    assert meta.cacheable is True


# --------------------------------------------------------------------------- #
# Accumulation normalization (alias table + resolve-hook raise)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("raw,canon", [("1h", "01H"), ("6h", "06H"), ("24h", "24H"),
                                       ("72h", "72H"), ("24H", "24H"), ("01H", "01H")])
def test_accumulation_normalizes(spec, raw, canon):
    assert mq._normalize_accumulation(spec, raw) == canon


def test_accumulation_router_alias_canonicalizes_for_cache_key(spec):
    # The str-alias table canonicalizes at validate-time so "1h" and "01H" key alike.
    assert router.validate_params(spec, {"accumulation": "1h", "valid_time": "x"})["accumulation"] == "01H"
    assert router.validate_params(spec, {"accumulation": "24H"})["accumulation"] == "24H"


def test_unknown_accumulation_raises_input_error(spec):
    with pytest.raises(RouterError) as ei:
        mq._normalize_accumulation(spec, "99h")
    assert ei.value.error_code == "MRMS_QPE_INPUT_ERROR"
    assert ei.value.retryable is False


# --------------------------------------------------------------------------- #
# valid_time parsing
# --------------------------------------------------------------------------- #


def test_valid_time_zulu_parses_utc(spec):
    dt = mq._parse_valid_time(spec, "2026-08-01T19:00:00Z")
    assert dt.tzinfo is not None and dt.hour == 19


def test_valid_time_naive_assumed_utc(spec):
    dt = mq._parse_valid_time(spec, "2026-08-01T19:00:00")
    assert dt.utcoffset() == _dt.timedelta(0)


def test_valid_time_none_is_latest(spec):
    assert mq._parse_valid_time(spec, None) is None


def test_bad_valid_time_raises_input_error(spec):
    with pytest.raises(RouterError) as ei:
        mq._parse_valid_time(spec, "not-a-date")
    assert ei.value.error_code == "MRMS_QPE_INPUT_ERROR"


# --------------------------------------------------------------------------- #
# Resolve phase (pure): targeted first-present + latest max-key + typed errors
# --------------------------------------------------------------------------- #


def _list_xml(keys):
    body = "".join(f"<Contents><Key>{k}</Key></Contents>" for k in keys)
    return f"<ListBucketResult>{body}</ListBucketResult>".encode()


def test_resolve_build_targeted_emits_hourly_probes(spec):
    plans = mq.resolve_build(spec, {"accumulation": "24H", "valid_time": "2026-08-01T19:00:00Z"})
    assert len(plans) == mq._WALKBACK_HOURS + 1
    # first probe is the requested hour (hours_back=0)
    assert "20260801-190000.grib2.gz" in plans[0].url
    assert "20260801-180000.grib2.gz" in plans[1].url


def test_resolve_build_latest_emits_date_dir_listings(spec):
    plans = mq.resolve_build(spec, {"accumulation": "24H"})
    assert len(plans) == mq._LATEST_DATE_DIRS
    assert "MultiSensor_QPE_24H_Pass2_00.00" in plans[0].url


def test_resolve_parse_targeted_first_present_wins(spec):
    params = {"accumulation": "24H", "valid_time": "2026-08-01T19:00:00Z"}
    keys = mq._targeted_keys("24H", mq._parse_valid_time(spec, params["valid_time"]))
    # hour 0 absent (empty body), hour 1 present -> resolve to the hour-1 key.
    bodies = [b"<ListBucketResult></ListBucketResult>", _list_xml([keys[1]])]
    bodies += [b"<ListBucketResult></ListBucketResult>"] * (len(keys) - 2)
    out = mq.resolve_parse(spec, params, bodies)
    assert out["_grib_key"] == keys[1]


def test_resolve_parse_targeted_none_raises_not_available(spec):
    params = {"accumulation": "24H", "valid_time": "2026-08-01T19:00:00Z"}
    empties = [b"<ListBucketResult></ListBucketResult>"] * (mq._WALKBACK_HOURS + 1)
    with pytest.raises(RouterError) as ei:
        mq.resolve_parse(spec, params, empties)
    assert ei.value.error_code == "MRMS_QPE_NOT_AVAILABLE"
    assert ei.value.retryable is False


def test_resolve_parse_latest_picks_max_key(spec):
    older = "CONUS/MultiSensor_QPE_24H_Pass2_00.00/20260801/MRMS_MultiSensor_QPE_24H_Pass2_00.00_20260801-170000.grib2.gz"
    newer = "CONUS/MultiSensor_QPE_24H_Pass2_00.00/20260801/MRMS_MultiSensor_QPE_24H_Pass2_00.00_20260801-190000.grib2.gz"
    out = mq.resolve_parse(spec, {"accumulation": "24H"}, [_list_xml([older, newer]), _list_xml([])])
    assert out["_grib_key"] == newer


def test_resolve_parse_latest_empty_bucket_raises_upstream(spec):
    with pytest.raises(RouterError) as ei:
        mq.resolve_parse(spec, {"accumulation": "24H"}, [_list_xml([]), _list_xml([])])
    assert ei.value.error_code == "MRMS_QPE_UPSTREAM_ERROR"
    assert ei.value.retryable is True


# --------------------------------------------------------------------------- #
# grib_object decode (synthetic GeoTIFF stands in for the .grib2, GDAL-decoded)
# --------------------------------------------------------------------------- #


def _synthetic_mrms_gz(*, sentinels=True, shape=(350, 700)):
    h, w = shape
    arr = np.full(shape, 5.0, dtype="float32")
    if sentinels:
        arr[0:h // 4, 0:w // 4] = -3.0
        arr[h // 2:h * 3 // 4, w // 2:w * 3 // 4] = -1.0
        arr[h * 3 // 4:, w * 3 // 4:] = 50.0
    profile = {"driver": "GTiff", "height": h, "width": w, "count": 1, "dtype": "float32",
               "crs": CRS.from_epsg(4326), "transform": from_bounds(*_CONUS, w, h)}
    with MemoryFile() as memf:
        with memf.open(**profile) as dst:
            dst.write(arr, 1)
        return gzip.compress(memf.read())


@pytest.fixture
def patch_transport(monkeypatch, spec):
    """Patch the shared transport GET so grib_object receives a synthetic .grib2.gz."""
    def _install(gz_bytes):
        import trid3nt_server.agent.tools.fetchers._router.transport as tp
        monkeypatch.setattr(tp, "get_client", lambda: object())
        monkeypatch.setattr(tp, "get_bytes", lambda *a, **k: (gz_bytes, "application/octet-stream", "u"))
    return _install


def _decode(spec, params):
    return raster_cog.execute(spec, params)


def test_grib_object_collapses_sentinels(spec, patch_transport):
    patch_transport(_synthetic_mrms_gz(sentinels=True))
    out = _decode(spec, {"_grib_key": "x.grib2.gz", "accumulation": "24H"})
    with MemoryFile(out) as mf, mf.open() as src:
        assert src.crs.to_epsg() == 4326
        assert src.nodata == _NODATA
        assert src.dtypes[0] == "float32"
        a = src.read(1)
        assert a[a > 0].max() >= 50.0
        # -3 / -1 sentinels collapsed to nodata (no residual negative-but-nonnodata)
        assert not ((a > -3.5) & (a < 0)).any()


def test_grib_object_bbox_clip_is_smaller(spec, patch_transport):
    patch_transport(_synthetic_mrms_gz(sentinels=False))
    full = _decode(spec, {"_grib_key": "x.grib2.gz", "accumulation": "24H"})
    patch_transport(_synthetic_mrms_gz(sentinels=False))
    clip = _decode(spec, {"_grib_key": "x.grib2.gz", "accumulation": "24H", "bbox": list(_FLORIDA)})
    with MemoryFile(full) as m, m.open() as fs, MemoryFile(clip) as m2, m2.open() as cs:
        assert cs.width * cs.height < fs.width * fs.height
        assert cs.crs.to_epsg() == 4326
        b = cs.bounds
        assert b.left <= _FLORIDA[2] and b.right >= _FLORIDA[0]


def test_grib_object_offshore_bbox_raises_empty(spec, patch_transport):
    patch_transport(_synthetic_mrms_gz(sentinels=False))
    with pytest.raises(RouterError) as ei:
        _decode(spec, {"_grib_key": "x.grib2.gz", "accumulation": "24H", "bbox": [10.0, 40.0, 11.0, 41.0]})
    assert ei.value.error_code == "MRMS_QPE_EMPTY"
    assert ei.value.retryable is False


# --------------------------------------------------------------------------- #
# Payload estimator (synthesized bbox_area)
# --------------------------------------------------------------------------- #


def test_payload_estimate_positive_and_scales(spec):
    est = router.synthesize_payload_estimator(spec)
    small = est(bbox=[-82.0, 26.0, -81.0, 27.0])
    big = est(bbox=[-90.0, 25.0, -80.0, 35.0])
    assert small > 0.0
    assert big > small
