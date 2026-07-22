"""Unit tests for the ``fetch_noaa_sst`` atomic tool (NOAA CRW daily SST).

Coverage:
- Registration in TOOL_REGISTRY with expected metadata (+ payload estimator).
- Input validation: degenerate / out-of-range / non-finite / too-large bbox,
  bad variable, future / pre-coverage date -> typed ``SSTInputError`` (not
  retryable).
- Synthetic griddap NetCDF -> the COG-build path round-trips to a cached
  single-band float32 SST COG with the right values (degrees C), north-up
  orientation, EPSG:4326, and the ``sst_celsius`` style preset.
- Honest no-data: an all-NaN (land) window raises ``SSTNoDataError`` (honest,
  not fabricated) and is NOT retryable; a 404 "axis maximum" ERDDAP body maps
  to ``SSTNoDataError`` (date out of coverage).
- Cache-key determinism: different bbox / date / variable -> different keys; a
  cache hit on the second identical call does not re-invoke the fetcher.
- Variable selection: ``variable="anomaly"`` resolves to CRW_SSTANOMALY +
  ``sst_anomaly`` style; friendly aliases map.

Network is fully mocked: the ERDDAP HTTP GET (``_fetch_griddap_nc``) is patched
to return a synthetic griddap-shaped NetCDF, so no real request is made.
"""

from __future__ import annotations

import datetime as _dt
from datetime import datetime, timezone
from unittest.mock import patch

import numpy as np
import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools import fetch_noaa_sst as sst_mod
from trid3nt_server.tools.fetch_noaa_sst import (
    _METADATA,
    SSTInputError,
    SSTNoDataError,
    SSTUpstreamError,
    estimate_payload_mb,
    fetch_noaa_sst,
)

_PINNED_NOW = datetime(2026, 6, 27, 12, 0, 0, tzinfo=timezone.utc)

# Florida Gulf ocean bbox -- small AOI inside the guardrail.
_GULF_BBOX = (-84.5, 26.0, -83.0, 27.5)
_GOOD_DATE = "2026-06-26"


# ---------------------------------------------------------------------------
# Synthetic griddap NetCDF builder (mimics the NOAA_DHW .nc subset shape:
# time(1) x latitude(DESCENDING) x longitude(ascending), Celsius).
# ---------------------------------------------------------------------------


def _synthetic_griddap_nc(
    *,
    var: str = "CRW_SST",
    bbox=_GULF_BBOX,
    n: int = 8,
    fill=None,
    all_nan: bool = False,
) -> bytes:
    """Build an in-memory griddap-shaped NetCDF and return its bytes."""
    import xarray as xr

    west, south, east, north = bbox
    # latitude DESCENDS to mirror NOAA_DHW (north-up).
    lat = np.linspace(north, south, n).astype("float64")
    lon = np.linspace(west, east, n).astype("float64")
    if all_nan:
        data = np.full((1, n, n), np.nan, dtype="float64")
    elif fill is not None:
        data = np.full((1, n, n), float(fill), dtype="float64")
    else:
        # A smooth warm-Gulf gradient ~29..31 C.
        base = np.linspace(29.0, 31.0, n)
        data = np.tile(base, (n, 1))[np.newaxis, :, :].astype("float64")
    da = xr.DataArray(
        data,
        dims=("time", "latitude", "longitude"),
        coords={
            "time": [np.datetime64("2026-06-26T12:00:00")],
            "latitude": lat,
            "longitude": lon,
        },
        name=var,
        attrs={"units": "Celsius", "long_name": "sea surface temperature"},
    )
    ds = da.to_dataset()
    # The tool reads with the netcdf4 engine, so the fixture must be a real
    # NETCDF4 file (the no-path bytes return uses the scipy NETCDF3 writer,
    # which the netcdf4 reader rejects). Write to a temp path with the same
    # engine the tool uses, then return the bytes.
    import os
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".nc", prefix="trid3nt_sst_fixture_")
    os.close(fd)
    try:
        ds.to_netcdf(path, engine="netcdf4")
        with open(path, "rb") as f:
            return f.read()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# In-memory S3 read-through injector (mirrors sibling test pattern).
# ---------------------------------------------------------------------------


class _FakeStore:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}


def _make_read_through_injector(fake):
    from trid3nt_server.tools.cache import (
        CACHE_BUCKET,
        cache_path,
        compute_cache_key as ck,
        is_cacheable,
        ReadThroughResult,
    )

    store = fake.store

    def patched(metadata, params, ext, fetch_fn, **kw):
        bucket = kw.get("bucket") or CACHE_BUCKET
        source_id = kw.get("source_id") or (metadata.source_class or metadata.name)
        force_refresh = kw.get("force_refresh", False)
        if not is_cacheable(metadata):
            return ReadThroughResult(uri=None, data=fetch_fn(), hit=False)
        key = ck(source_id, params, metadata.ttl_class, now=_PINNED_NOW)
        path = cache_path(metadata.source_class, metadata.ttl_class, key, ext)
        uri = f"s3://{bucket}/{path}"
        if not force_refresh and path in store:
            return ReadThroughResult(uri=uri, data=store[path], hit=True)
        data = fetch_fn()
        store[path] = data
        return ReadThroughResult(uri=uri, data=data, hit=False)

    return patched


# ---------------------------------------------------------------------------
# Registration / metadata.
# ---------------------------------------------------------------------------


def test_tool_is_registered() -> None:
    assert "fetch_noaa_sst" in TOOL_REGISTRY
    meta = TOOL_REGISTRY["fetch_noaa_sst"].metadata
    assert meta.name == "fetch_noaa_sst"
    assert meta.ttl_class == "static-30d"
    assert meta.source_class == "noaa_sst"
    assert meta.cacheable is True
    assert meta.supports_global_query is False
    assert meta.payload_mb_estimator_name == "estimate_payload_mb"


def test_payload_estimator_scales_with_area() -> None:
    # The CRW 5 km COG is genuinely tiny; a sub-floor small bbox stays at the
    # floor, while a large regional-sea bbox (16 deg^2) exceeds it, so the
    # scaling is observable above the floor.
    small = estimate_payload_mb(bbox=(-84.5, 26.0, -84.4, 26.1))
    big = estimate_payload_mb(bbox=(-84.5, 26.0, -80.5, 30.0))
    assert big > small
    assert estimate_payload_mb(bbox=None) > 0


# ---------------------------------------------------------------------------
# Input validation (no network).
# ---------------------------------------------------------------------------


def test_degenerate_bbox_raises() -> None:
    with pytest.raises(SSTInputError):
        fetch_noaa_sst(bbox=(-84.0, 26.0, -84.0, 26.0))


def test_out_of_range_lon_bbox_raises() -> None:
    with pytest.raises(SSTInputError, match="lon out of"):
        fetch_noaa_sst(bbox=(-200.0, 26.0, -83.0, 27.0))


def test_nonfinite_bbox_raises() -> None:
    with pytest.raises(SSTInputError):
        fetch_noaa_sst(bbox=(float("nan"), 26.0, -83.0, 27.0))


def test_bad_length_bbox_raises() -> None:
    with pytest.raises(SSTInputError):
        fetch_noaa_sst(bbox=(-84.0, 26.0, -83.0))  # type: ignore[arg-type]


def test_too_large_bbox_raises() -> None:
    with pytest.raises(SSTInputError, match="guardrail"):
        fetch_noaa_sst(bbox=(-100.0, -10.0, -50.0, 10.0))


def test_unknown_variable_raises() -> None:
    with pytest.raises(SSTInputError, match="unknown variable"):
        fetch_noaa_sst(bbox=_GULF_BBOX, variable="zonk")


def test_future_date_raises() -> None:
    with pytest.raises(SSTInputError, match="future"):
        fetch_noaa_sst(bbox=_GULF_BBOX, date="2099-01-01")


def test_pre_coverage_date_raises() -> None:
    with pytest.raises(SSTInputError, match="coverage start"):
        fetch_noaa_sst(bbox=_GULF_BBOX, date="1980-01-01")


def test_malformed_date_raises() -> None:
    with pytest.raises(SSTInputError, match="YYYY-MM-DD"):
        fetch_noaa_sst(bbox=_GULF_BBOX, date="not-a-date")


# ---------------------------------------------------------------------------
# Variable resolution + style preset.
# ---------------------------------------------------------------------------


def test_variable_aliases_resolve() -> None:
    assert sst_mod._resolve_variable(None) == "sst"
    assert sst_mod._resolve_variable("temperature") == "sst"
    assert sst_mod._resolve_variable("CRW_SST") == "sst"
    assert sst_mod._resolve_variable("anomaly") == "anomaly"
    assert sst_mod._resolve_variable("sst_anomaly") == "anomaly"


def test_url_uses_descending_latitude_order() -> None:
    """NOAA_DHW latitude descends; the constraint must be written north:south."""
    url = sst_mod._build_griddap_url(
        "CRW_SST", _GULF_BBOX, _dt.date(2026, 6, 26)
    )
    assert "NOAA_DHW.nc?CRW_SST" in url
    # north (27.5) appears before south (26.0) in the lat constraint.
    assert "[(27.5):(26.0)]" in url
    assert "[(-84.5):(-83.0)]" in url


# ---------------------------------------------------------------------------
# Synthetic griddap NetCDF -> COG round-trip (correctness).
# ---------------------------------------------------------------------------


def test_synthetic_sst_roundtrips_to_cog() -> None:
    import rasterio

    fake = _FakeStore()
    nc = _synthetic_griddap_nc(var="CRW_SST")
    injector = _make_read_through_injector(fake)

    with patch.object(sst_mod, "read_through", injector), patch.object(
        sst_mod, "_fetch_griddap_nc", return_value=nc
    ):
        res = fetch_noaa_sst(bbox=_GULF_BBOX, date=_GOOD_DATE, variable="sst")

    assert res.layer_type == "raster"
    assert res.style_preset == "sst_celsius"
    assert res.role == "primary"
    assert res.units == "degrees Celsius"
    assert res.uri.startswith("s3://")
    assert res.bbox is not None

    # The COG persisted to the fake store; read it back and verify values.
    assert len(fake.store) == 1
    cog_bytes = next(iter(fake.store.values()))
    with rasterio.MemoryFile(cog_bytes) as mf, mf.open() as src:
        assert src.count == 1
        assert src.dtypes[0] == "float32"
        assert str(src.crs).endswith("4326")
        # north-up: negative y-step in the transform.
        assert src.transform.e < 0
        band = src.read(1)
        finite = band[np.isfinite(band)]
        assert finite.size > 0
        # Synthetic gradient is 29..31 C.
        assert 28.5 <= float(finite.min()) <= 31.5
        assert 28.5 <= float(finite.max()) <= 31.5


def test_anomaly_variable_uses_anomaly_style() -> None:
    fake = _FakeStore()
    nc = _synthetic_griddap_nc(var="CRW_SSTANOMALY", fill=1.5)
    injector = _make_read_through_injector(fake)

    with patch.object(sst_mod, "read_through", injector), patch.object(
        sst_mod, "_fetch_griddap_nc", return_value=nc
    ):
        res = fetch_noaa_sst(bbox=_GULF_BBOX, date=_GOOD_DATE, variable="anomaly")

    assert res.style_preset == "sst_anomaly"
    assert "Anomaly" in res.name


# ---------------------------------------------------------------------------
# Honest no-data.
# ---------------------------------------------------------------------------


def test_all_nan_window_raises_no_data() -> None:
    """A fully-land (all-NaN) window is honest no-data, not a fabricated layer."""
    fake = _FakeStore()
    nc = _synthetic_griddap_nc(var="CRW_SST", all_nan=True)
    injector = _make_read_through_injector(fake)

    with patch.object(sst_mod, "read_through", injector), patch.object(
        sst_mod, "_fetch_griddap_nc", return_value=nc
    ):
        with pytest.raises(SSTNoDataError):
            fetch_noaa_sst(bbox=_GULF_BBOX, date=_GOOD_DATE, variable="sst")
    # Nothing fabricated / cached on the no-data path.
    assert len(fake.store) == 0


def test_erddap_404_axis_max_maps_to_no_data() -> None:
    """A 404 'greater than the axis maximum' body is honest date-out-of-range."""

    class _Resp:
        status_code = 404
        text = (
            'Error { code=404; message="Not Found: Your query produced no '
            'matching results. Query error: ... is greater than the axis '
            'maximum=2026-06-26T12:00:00Z." }'
        )

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            return _Resp()

    import httpx

    with patch.object(httpx, "Client", _Client):
        with pytest.raises(SSTNoDataError, match="no SST"):
            sst_mod._fetch_griddap_nc(
                "https://example/NOAA_DHW.nc?CRW_SST[...]", _dt.date(2026, 6, 26)
            )


def test_erddap_500_maps_to_upstream_error() -> None:
    class _Resp:
        status_code = 500
        text = "Internal Server Error"

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            return _Resp()

    import httpx

    with patch.object(httpx, "Client", _Client):
        with pytest.raises(SSTUpstreamError, match="HTTP 500"):
            sst_mod._fetch_griddap_nc(
                "https://example/NOAA_DHW.nc?CRW_SST[...]", _dt.date(2026, 6, 26)
            )


# ---------------------------------------------------------------------------
# Cache-key determinism + hit-on-repeat.
# ---------------------------------------------------------------------------


def test_repeat_call_hits_cache_and_skips_fetch() -> None:
    fake = _FakeStore()
    nc = _synthetic_griddap_nc(var="CRW_SST")
    injector = _make_read_through_injector(fake)
    calls = {"n": 0}

    def counting_fetch(url, date):
        calls["n"] += 1
        return nc

    with patch.object(sst_mod, "read_through", injector), patch.object(
        sst_mod, "_fetch_griddap_nc", side_effect=counting_fetch
    ):
        r1 = fetch_noaa_sst(bbox=_GULF_BBOX, date=_GOOD_DATE, variable="sst")
        r2 = fetch_noaa_sst(bbox=_GULF_BBOX, date=_GOOD_DATE, variable="sst")

    assert r1.uri == r2.uri
    # The fetch ran exactly once; the second call was a cache hit.
    assert calls["n"] == 1
    assert len(fake.store) == 1


def test_distinct_inputs_distinct_cache_keys() -> None:
    from trid3nt_server.tools.cache import compute_cache_key

    def k(params):
        return compute_cache_key(
            _METADATA.source_class, params, _METADATA.ttl_class, now=_PINNED_NOW
        )

    base = {"bbox": list(_GULF_BBOX), "date": _GOOD_DATE, "variable": "sst",
            "dataset": "NOAA_DHW"}
    k_base = k(base)
    k_date = k({**base, "date": "2026-06-20"})
    k_var = k({**base, "variable": "anomaly"})
    k_bbox = k({**base, "bbox": [-84.5, 26.0, -83.5, 27.0]})
    assert len({k_base, k_date, k_var, k_bbox}) == 4
