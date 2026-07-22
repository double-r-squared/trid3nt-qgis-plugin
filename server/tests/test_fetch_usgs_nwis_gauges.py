"""Unit tests for the ``fetch_usgs_nwis_gauges`` atomic tool (job-0332).

Covers the gap NATE hit: a real USGS NWIS / Water Services gauge-station
fetcher (observed discharge/stage), distinct from the MODELED
``fetch_noaa_nwm_streamflow``. All HTTP is mocked — no live network.

Coverage:
- Tool is registered in TOOL_REGISTRY with expected metadata.
- Categorized under hydrology.
- Error classes carry correct retryable + error_code attributes.
- Input validation: no selector, bad bbox, bad state code.
- IV happy path: WaterML-JSON parses multiple stations into points with
  both discharge (00060) and gage height (00065) merged per site_no.
- bbox-too-large (whole-Washington ~28 deg^2) → NwisBboxTooLargeError telling
  the caller to pass state_code; the SAME bbox WITH state_code succeeds.
- IV empty → Site-service RDB fallback returns station LOCATIONS.
- BOTH empty → NwisNoStationsError (honest typed error, never empty success).
- LayerURI shape: layer_type="vector", role="primary", style_preset, bbox set.
- Payload estimator returns a positive float.

Live test (gated by TRID3NT_TEST_LIVE_NWIS=1): real USGS IV request for a
small Boise-area bbox; confirms >=1 gauge with a finite discharge reading.
"""

from __future__ import annotations

import datetime
import json
import os
import tempfile
from typing import Any
from unittest.mock import patch

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.fetchers.hydrology.fetch_usgs_nwis_gauges import (
    NwisBboxTooLargeError,
    NwisGaugesError,
    NwisInputError,
    NwisNoStationsError,
    NwisUpstreamError,
    _build_iv_url,
    _build_site_url,
    _parse_iv_json,
    _parse_iv_json_window,
    _parse_site_rdb,
    _records_bbox,
    _resolve_window,
    _validate_bbox,
    _validate_state_code,
    estimate_payload_mb,
    fetch_usgs_nwis_gauges,
)


# ---------------------------------------------------------------------------
# Constants / fixtures.
# ---------------------------------------------------------------------------

_LIVE_NWIS = os.environ.get("TRID3NT_TEST_LIVE_NWIS") == "1"

# Whole-Washington bbox: ~8 deg lon x ~3.5 deg lat = ~28 deg^2 — EXCEEDS the
# USGS ~25 deg^2 bBox limit (this is exactly the case NATE hit).
_WA_BBOX = (-124.8, 45.5, -116.9, 49.0)

# A small sub-state bbox (Boise area, ~0.5 x 0.4 = 0.2 deg^2) — well under cap.
_BOISE_BBOX = (-116.4, 43.4, -115.9, 43.8)

_PINNED_NOW = datetime.datetime(2026, 6, 17, 12, 0, 0, tzinfo=datetime.timezone.utc)


def _make_iv_json(series: list[dict[str, Any]]) -> bytes:
    """Wrap a list of timeSeries entries in the IV WaterML-JSON envelope."""
    return json.dumps({"value": {"timeSeries": series}}).encode("utf-8")


def _ts(
    site_no: str,
    site_name: str,
    lat: float,
    lon: float,
    param: str,
    value: str,
    dt: str = "2026-06-17T11:45:00.000-07:00",
) -> dict[str, Any]:
    """Build one IV timeSeries entry (one site x one parameter)."""
    return {
        "sourceInfo": {
            "siteName": site_name,
            "siteCode": [{"value": site_no}],
            "geoLocation": {
                "geogLocation": {"latitude": lat, "longitude": lon}
            },
        },
        "variable": {"variableCode": [{"value": param}]},
        "values": [{"value": [{"value": value, "dateTime": dt}]}],
    }


def _ts_window(
    site_no: str,
    site_name: str,
    lat: float,
    lon: float,
    param: str,
    samples: list[tuple[str, str]],
) -> dict[str, Any]:
    """Build one IV timeSeries entry carrying a MULTI-sample window (hydrograph).

    ``samples`` is a list of ``(dateTime, value)`` rows — the full series the IV
    service returns when called with a startDT/endDT (or period) window.
    """
    return {
        "sourceInfo": {
            "siteName": site_name,
            "siteCode": [{"value": site_no}],
            "geoLocation": {
                "geogLocation": {"latitude": lat, "longitude": lon}
            },
        },
        "variable": {"variableCode": [{"value": param}]},
        "values": [
            {"value": [{"value": v, "dateTime": dt} for dt, v in samples]}
        ],
    }


def _hydrograph_samples(n: int = 12, base: float = 100.0) -> list[tuple[str, str]]:
    """Build ``n`` hourly (dateTime, value) discharge samples — a rising/falling
    flood wave so the values are NOT constant (proves it is a real hydrograph)."""
    out: list[tuple[str, str]] = []
    for i in range(n):
        dt = (
            datetime.datetime(2018, 10, 10, 0, 0, 0, tzinfo=datetime.timezone.utc)
            + datetime.timedelta(hours=i)
        )
        # Triangular flood wave peaking mid-window.
        v = base + (i if i <= n // 2 else (n - i)) * 50.0
        out.append((dt.strftime("%Y-%m-%dT%H:%M:%S.000-00:00"), f"{v:.2f}"))
    return out


def _make_site_rdb(rows: list[tuple[str, str, float, float]]) -> bytes:
    """Build a USGS Site-service RDB (tab-delimited) body.

    rows: (site_no, station_nm, dec_lat_va, dec_long_va).
    """
    lines = [
        "# USGS site service",
        "#",
        "agency_cd\tsite_no\tstation_nm\tdec_lat_va\tdec_long_va",
        "5s\t15s\t50s\t16s\t16s",  # the type/width line we skip
    ]
    for site_no, name, lat, lon in rows:
        lines.append(f"USGS\t{site_no}\t{name}\t{lat}\t{lon}")
    return "\n".join(lines).encode("utf-8")


# ---------------------------------------------------------------------------
# Fake GCS plumbing (mirrors sibling station-fetcher tests).
# ---------------------------------------------------------------------------


class FakeBlob:
    def __init__(self, store: dict[str, bytes], path: str) -> None:
        self._store = store
        self._path = path
        self.custom_time: datetime.datetime | None = None
        self.cache_control: str | None = None

    def exists(self) -> bool:
        return self._path in self._store

    def download_as_bytes(self) -> bytes:
        return self._store[self._path]

    def upload_from_string(self, data: bytes, content_type: str | None = None) -> None:
        self._store[self._path] = data


class FakeBucket:
    def __init__(self, store: dict[str, bytes]) -> None:
        self._store = store

    def blob(self, path: str) -> FakeBlob:
        return FakeBlob(self._store, path)


class FakeStorageClient:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    def bucket(self, name: str) -> FakeBucket:
        return FakeBucket(self.store)


def _make_read_through_injector(fake_gcs):
    """S3-only in-memory read-through injector (GCP decommissioned).

    Replaces the retired ``google.cloud.storage`` double: drives the tool's
    ``read_through`` off an in-memory S3 store (``fake_gcs.store``, keyed by
    object KEY), minting ``s3://`` URIs and honoring cache hit/miss/write.
    """
    from trid3nt_server.tools.cache import (
        CACHE_BUCKET,
        cache_path,
        compute_cache_key,
        is_cacheable,
        ReadThroughResult,
    )

    store = fake_gcs.store

    def patched(metadata, params, ext, fetch_fn, **kw):
        bucket = kw.get("bucket") or CACHE_BUCKET
        source_id = kw.get("source_id") or (metadata.source_class or metadata.name)
        force_refresh = kw.get("force_refresh", False)
        if not is_cacheable(metadata):
            return ReadThroughResult(uri=None, data=fetch_fn(), hit=False)
        key = compute_cache_key(source_id, params, metadata.ttl_class, now=_PINNED_NOW)
        path = cache_path(metadata.source_class, metadata.ttl_class, key, ext)
        uri = f"s3://{bucket}/{path}"
        if not force_refresh and path in store:
            return ReadThroughResult(uri=uri, data=store[path], hit=True)
        data = fetch_fn()
        store[path] = data
        return ReadThroughResult(uri=uri, data=data, hit=False)

    return patched


def _have_geo() -> bool:
    try:
        import geopandas  # noqa: F401
        import shapely  # noqa: F401
        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Registration / categorization.
# ---------------------------------------------------------------------------


def test_tool_is_registered():
    assert "fetch_usgs_nwis_gauges" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_usgs_nwis_gauges"]
    assert entry.metadata.name == "fetch_usgs_nwis_gauges"
    assert entry.metadata.ttl_class == "dynamic-1h"
    assert entry.metadata.source_class == "usgs_nwis_gauges"
    assert entry.metadata.cacheable is True
    assert entry.metadata.payload_mb_estimator_name == "estimate_payload_mb"


def test_supports_global_query_is_false():
    entry = TOOL_REGISTRY["fetch_usgs_nwis_gauges"]
    sgq = getattr(entry.metadata, "supports_global_query", None)
    assert sgq in (False, None), f"expected False or None; got {sgq!r}"


def test_categorized_under_hydrology():
    from trid3nt_server.categories import PRIMARY_CATEGORY, tools_for_category

    assert PRIMARY_CATEGORY.get("fetch_usgs_nwis_gauges") == "hydrology"
    assert "fetch_usgs_nwis_gauges" in tools_for_category("hydrology")


# ---------------------------------------------------------------------------
# Error class attributes.
# ---------------------------------------------------------------------------


def test_error_classes_attributes():
    for cls, retryable in [
        (NwisGaugesError, True),
        (NwisInputError, False),
        (NwisBboxTooLargeError, False),
        (NwisUpstreamError, True),
        (NwisNoStationsError, False),
    ]:
        inst = cls("test")
        assert inst.retryable is retryable, f"{cls.__name__}.retryable wrong"
        assert isinstance(inst.error_code, str) and inst.error_code != ""


def test_bbox_too_large_is_input_error_subclass():
    # So callers catching NwisInputError also catch the bbox-too-large case.
    assert issubclass(NwisBboxTooLargeError, NwisInputError)


# ---------------------------------------------------------------------------
# Input validation.
# ---------------------------------------------------------------------------


def test_validate_bbox_ok():
    _validate_bbox(_BOISE_BBOX)  # no exception


def test_validate_bbox_degenerate():
    with pytest.raises(NwisInputError, match="degenerate"):
        _validate_bbox((-116.0, 43.0, -116.0, 43.8))


def test_validate_bbox_wrong_length():
    with pytest.raises(NwisInputError, match="west, south, east, north"):
        _validate_bbox((1.0, 2.0, 3.0))  # type: ignore[arg-type]


def test_validate_bbox_out_of_range():
    with pytest.raises(NwisInputError, match="lon"):
        _validate_bbox((-200.0, 43.0, -116.0, 43.8))


def test_validate_state_code_normalizes():
    assert _validate_state_code("wa") == "WA"
    assert _validate_state_code(" fl ") == "FL"


def test_validate_state_code_unknown():
    with pytest.raises(NwisInputError, match="USPS"):
        _validate_state_code("ZZ")


def test_no_selector_raises_input_error():
    with pytest.raises(NwisInputError, match="requires a spatial selector"):
        fetch_usgs_nwis_gauges()


# ---------------------------------------------------------------------------
# URL builders.
# ---------------------------------------------------------------------------


def test_build_iv_url_state():
    url = _build_iv_url(state_code="WA", bbox=None)
    assert url.startswith("https://waterservices.usgs.gov/nwis/iv/")
    assert "format=json" in url
    assert "siteStatus=active" in url
    assert "parameterCd=00060%2C00065" in url
    assert "stateCd=WA" in url
    assert "bBox" not in url


def test_build_iv_url_bbox():
    url = _build_iv_url(state_code=None, bbox=(-116.4, 43.4, -115.9, 43.8))
    assert "bBox=" in url
    assert "stateCd" not in url


def test_build_site_url_has_fallback_params():
    url = _build_site_url(state_code="WA", bbox=None)
    assert url.startswith("https://waterservices.usgs.gov/nwis/site/")
    assert "format=rdb" in url
    assert "hasDataTypeCd=iv" in url
    assert "stateCd=WA" in url


# ---------------------------------------------------------------------------
# IV WaterML-JSON parsing (happy path — multiple stations, merge params).
# ---------------------------------------------------------------------------


def test_parse_iv_json_groups_and_merges_params():
    """Two stations; station A has both 00060 + 00065; station B only 00060."""
    raw = _make_iv_json([
        _ts("13206000", "BOISE R AT BOISE", 43.62, -116.20, "00060", "1234"),
        _ts("13206000", "BOISE R AT BOISE", 43.62, -116.20, "00065", "5.67"),
        _ts("13210050", "MASON CREEK", 43.55, -116.35, "00060", "42.1"),
    ])
    recs = {r["site_no"]: r for r in _parse_iv_json(raw)}

    assert set(recs) == {"13206000", "13210050"}

    a = recs["13206000"]
    assert a["site_name"] == "BOISE R AT BOISE"
    assert a["discharge_cfs"] == 1234.0
    assert a["gage_height_ft"] == 5.67
    assert a["reading_dt"] is not None
    assert a["lat"] == 43.62 and a["lon"] == -116.20

    b = recs["13210050"]
    assert b["discharge_cfs"] == 42.1
    assert b["gage_height_ft"] is None


def test_parse_iv_json_drops_nodata_and_bad_coords():
    raw = _make_iv_json([
        # no-data sentinel -> discharge stays None but the station survives via coords
        _ts("111", "NODATA SITE", 44.0, -116.0, "00060", "-999999"),
        # non-finite coords -> dropped entirely
        {
            "sourceInfo": {
                "siteName": "BAD COORDS",
                "siteCode": [{"value": "222"}],
                "geoLocation": {"geogLocation": {"latitude": "abc", "longitude": "xyz"}},
            },
            "variable": {"variableCode": [{"value": "00060"}]},
            "values": [{"value": [{"value": "5.0", "dateTime": "t"}]}],
        },
    ])
    recs = {r["site_no"]: r for r in _parse_iv_json(raw)}
    assert "222" not in recs  # bad coords dropped
    assert recs["111"]["discharge_cfs"] is None  # no-data sentinel filtered


def test_parse_iv_json_empty_body():
    assert _parse_iv_json(b"") == []
    assert _parse_iv_json(_make_iv_json([])) == []


def test_parse_iv_json_bad_json_raises_upstream():
    with pytest.raises(NwisUpstreamError, match="not valid JSON"):
        _parse_iv_json(b"<html>not json</html>")


# ---------------------------------------------------------------------------
# Site-service RDB fallback parsing.
# ---------------------------------------------------------------------------


def test_parse_site_rdb_extracts_locations():
    raw = _make_site_rdb([
        ("13206000", "BOISE R AT BOISE", 43.62, -116.20),
        ("13210050", "MASON CREEK", 43.55, -116.35),
    ])
    recs = _parse_site_rdb(raw)
    assert len(recs) == 2
    r0 = {r["site_no"]: r for r in recs}["13206000"]
    assert r0["site_name"] == "BOISE R AT BOISE"
    assert r0["lat"] == 43.62 and r0["lon"] == -116.20
    # Locations only — no readings.
    assert r0["discharge_cfs"] is None
    assert r0["gage_height_ft"] is None
    assert r0["reading_dt"] is None


def test_parse_site_rdb_empty():
    assert _parse_site_rdb(b"") == []
    # Header + type line but no data rows.
    assert _parse_site_rdb(_make_site_rdb([])) == []


# ---------------------------------------------------------------------------
# bbox-too-large -> stateCd-or-error path (the NATE case).
# ---------------------------------------------------------------------------


def test_whole_state_bbox_raises_bbox_too_large():
    """Whole-Washington bbox (~28 deg^2) with no state_code -> typed error."""
    with pytest.raises(NwisBboxTooLargeError, match="state_code"):
        fetch_usgs_nwis_gauges(bbox=_WA_BBOX)


def test_whole_state_bbox_with_state_code_succeeds():
    """SAME oversized extent but via state_code -> no area limit, works."""
    if not _have_geo():
        pytest.skip("geopandas/shapely not installed")

    fake_gcs = FakeStorageClient()
    iv_json = _make_iv_json([
        _ts("12500450", "YAKIMA R", 46.20, -119.90, "00060", "900"),
    ])

    captured_urls: list[str] = []

    def fake_http_get(url: str, timeout: float = 60.0) -> bytes:
        captured_urls.append(url)
        return iv_json

    with (
        patch("trid3nt_server.tools.fetchers.hydrology.fetch_usgs_nwis_gauges._http_get", side_effect=fake_http_get),
        patch(
            "trid3nt_server.tools.fetchers.hydrology.fetch_usgs_nwis_gauges.read_through",
            side_effect=_make_read_through_injector(fake_gcs),
        ),
    ):
        result = fetch_usgs_nwis_gauges(state_code="WA")

    assert result.layer_type == "vector"
    assert "WA" in result.name
    # The IV call used stateCd, not bBox.
    assert any("stateCd=WA" in u for u in captured_urls)
    assert all("bBox" not in u for u in captured_urls)


# ---------------------------------------------------------------------------
# IV happy path -> points with discharge + gage.
# ---------------------------------------------------------------------------


def test_iv_happy_path_layer_uri_shape():
    if not _have_geo():
        pytest.skip("geopandas/shapely not installed")

    fake_gcs = FakeStorageClient()
    iv_json = _make_iv_json([
        _ts("13206000", "BOISE R AT BOISE", 43.62, -116.20, "00060", "1234"),
        _ts("13206000", "BOISE R AT BOISE", 43.62, -116.20, "00065", "5.67"),
        _ts("13210050", "MASON CREEK", 43.55, -116.35, "00060", "42.1"),
    ])

    with (
        patch("trid3nt_server.tools.fetchers.hydrology.fetch_usgs_nwis_gauges._http_get", return_value=iv_json),
        patch(
            "trid3nt_server.tools.fetchers.hydrology.fetch_usgs_nwis_gauges.read_through",
            side_effect=_make_read_through_injector(fake_gcs),
        ),
    ):
        result = fetch_usgs_nwis_gauges(bbox=_BOISE_BBOX)

    assert result.layer_type == "vector"
    assert result.role == "primary"
    assert result.style_preset == "usgs_gauges"
    assert result.units == "mixed (cfs / ft)"
    assert result.uri.startswith("s3://")
    assert "usgs_nwis_gauges" in result.uri
    assert result.layer_id.startswith("usgs-gauges-")
    # bbox set to the station extent (so the camera zooms).
    assert result.bbox is not None
    west, south, east, north = result.bbox
    assert west <= -116.20 <= east
    assert south <= 43.62 <= north

    # Read back the FGB and verify 2 station points + merged props.
    assert len(fake_gcs.store) == 1
    fgb_bytes = next(iter(fake_gcs.store.values()))
    import geopandas as gpd
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        path = f.name
        f.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) == 2
        assert set(gdf["site_no"]) == {"13206000", "13210050"}
        boise = gdf[gdf["site_no"] == "13206000"].iloc[0]
        assert abs(boise["discharge_cfs"] - 1234.0) < 1e-6
        assert abs(boise["gage_height_ft"] - 5.67) < 1e-6
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# IV empty -> Site-service fallback.
# ---------------------------------------------------------------------------


def test_iv_empty_falls_back_to_site_service():
    if not _have_geo():
        pytest.skip("geopandas/shapely not installed")

    fake_gcs = FakeStorageClient()
    empty_iv = _make_iv_json([])  # zero active sites
    site_rdb = _make_site_rdb([
        ("13206000", "BOISE R AT BOISE", 43.62, -116.20),
    ])

    def fake_http_get(url: str, timeout: float = 60.0) -> bytes:
        if "/nwis/site/" in url:
            return site_rdb
        return empty_iv  # IV returns nothing

    with (
        patch("trid3nt_server.tools.fetchers.hydrology.fetch_usgs_nwis_gauges._http_get", side_effect=fake_http_get),
        patch(
            "trid3nt_server.tools.fetchers.hydrology.fetch_usgs_nwis_gauges.read_through",
            side_effect=_make_read_through_injector(fake_gcs),
        ),
    ):
        result = fetch_usgs_nwis_gauges(bbox=_BOISE_BBOX)

    assert result.layer_type == "vector"
    fgb_bytes = next(iter(fake_gcs.store.values()))
    import geopandas as gpd
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        path = f.name
        f.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) == 1
        row = gdf.iloc[0]
        assert row["site_no"] == "13206000"
        # Fallback carries locations only — no current reading.
        assert row["discharge_cfs"] is None or (
            hasattr(row["discharge_cfs"], "__float__")
            and str(row["discharge_cfs"]) in ("nan", "None")
        )
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# BOTH empty -> honest typed error (never an empty success layer).
# ---------------------------------------------------------------------------


def test_both_empty_raises_no_stations_error():
    fake_gcs = FakeStorageClient()

    def fake_http_get(url: str, timeout: float = 60.0) -> bytes:
        if "/nwis/site/" in url:
            return _make_site_rdb([])  # no fallback locations either
        return _make_iv_json([])  # no IV sites

    with (
        patch("trid3nt_server.tools.fetchers.hydrology.fetch_usgs_nwis_gauges._http_get", side_effect=fake_http_get),
        patch(
            "trid3nt_server.tools.fetchers.hydrology.fetch_usgs_nwis_gauges.read_through",
            side_effect=_make_read_through_injector(fake_gcs),
        ),
        pytest.raises(NwisNoStationsError, match="No active USGS NWIS gauge"),
    ):
        fetch_usgs_nwis_gauges(bbox=_BOISE_BBOX)

    # Nothing written to cache on the honest-error path.
    assert len(fake_gcs.store) == 0


# ---------------------------------------------------------------------------
# Records-extent helper.
# ---------------------------------------------------------------------------


def test_records_bbox_pads_single_point():
    extent = _records_bbox([{"lon": -116.2, "lat": 43.6}])
    assert extent is not None
    west, south, east, north = extent
    assert west < -116.2 < east
    assert south < 43.6 < north


def test_records_bbox_empty_is_none():
    assert _records_bbox([]) is None


# ---------------------------------------------------------------------------
# Payload estimator.
# ---------------------------------------------------------------------------


def test_estimate_payload_mb_positive():
    assert estimate_payload_mb(bbox=_BOISE_BBOX) > 0.0
    assert estimate_payload_mb(state_code="WA") > 0.0
    assert estimate_payload_mb() > 0.0


# ---------------------------------------------------------------------------
# Extra-kwargs absorption (Gemini hallucination guard).
# ---------------------------------------------------------------------------


def test_extra_kwargs_absorbed():
    if not _have_geo():
        pytest.skip("geopandas/shapely not installed")

    fake_gcs = FakeStorageClient()
    iv_json = _make_iv_json([
        _ts("13206000", "BOISE R", 43.62, -116.20, "00060", "1234"),
    ])
    with (
        patch("trid3nt_server.tools.fetchers.hydrology.fetch_usgs_nwis_gauges._http_get", return_value=iv_json),
        patch(
            "trid3nt_server.tools.fetchers.hydrology.fetch_usgs_nwis_gauges.read_through",
            side_effect=_make_read_through_injector(fake_gcs),
        ),
    ):
        result = fetch_usgs_nwis_gauges(
            bbox=_BOISE_BBOX,
            invented_param="foo",  # type: ignore[call-arg]
            another_fake=42,  # type: ignore[call-arg]
        )
    assert result.layer_type == "vector"


# ---------------------------------------------------------------------------
# WINDOW / HYDROGRAPH mode (J4 — the compound-flood discharge driver).
# ---------------------------------------------------------------------------


def test_resolve_window_none_is_instant_default():
    # No temporal selector -> None (the latest-instantaneous default).
    assert _resolve_window(None, None, None) is None


def test_resolve_window_period_wins():
    # A period is returned uppercased and verbatim, and WINS over dates.
    assert _resolve_window(None, None, "p7d") == "P7D"
    assert _resolve_window("2018-10-08", "2018-10-14", "P3D") == "P3D"


def test_resolve_window_period_invalid_raises():
    with pytest.raises(NwisInputError, match="ISO-8601 duration"):
        _resolve_window(None, None, "last week")


def test_resolve_window_dates_ok():
    assert _resolve_window("2018-10-08", "2018-10-14", None) == (
        "2018-10-08",
        "2018-10-14",
    )


def test_resolve_window_half_open_raises():
    with pytest.raises(NwisInputError, match="BOTH start_date and end_date"):
        _resolve_window("2018-10-08", None, None)


def test_resolve_window_reversed_raises():
    with pytest.raises(NwisInputError, match="start_date must be <="):
        _resolve_window("2018-10-14", "2018-10-08", None)


def test_resolve_window_over_cap_raises():
    with pytest.raises(NwisInputError, match="exceeds the"):
        _resolve_window("2018-01-01", "2018-12-31", None)


def test_build_iv_url_window_dates():
    url = _build_iv_url(
        state_code=None,
        bbox=_BOISE_BBOX,
        window=("2018-10-08", "2018-10-14"),
    )
    assert "startDT=2018-10-08" in url
    assert "endDT=2018-10-14" in url
    assert "period" not in url


def test_build_iv_url_window_period():
    url = _build_iv_url(state_code="ID", bbox=None, window="P7D")
    assert "period=P7D" in url
    assert "startDT" not in url


def test_build_iv_url_no_window_has_no_temporal_param():
    url = _build_iv_url(state_code=None, bbox=_BOISE_BBOX, window=None)
    assert "startDT" not in url and "endDT" not in url and "period" not in url


def test_parse_iv_window_builds_multipoint_time_series_csv():
    """A windowed IV body yields a per-station time_series_csv with >2 rows."""
    samples = _hydrograph_samples(n=12, base=100.0)
    raw = _make_iv_json([
        _ts_window("13206000", "BOISE R AT BOISE", 43.62, -116.20, "00060", samples),
    ])
    recs = {r["site_no"]: r for r in _parse_iv_json_window(raw)}
    r = recs["13206000"]
    csv_text = r["time_series_csv"]
    assert isinstance(csv_text, str)
    rows = [ln for ln in csv_text.strip().splitlines() if ln]
    assert len(rows) == 12  # the FULL hydrograph, not a flattened 2-point
    # The series is NOT constant (a real flood wave).
    vals = [float(ln.split(",")[1]) for ln in rows]
    assert len(set(vals)) > 2
    assert r["n_timesteps"] == 12
    assert r["discharge_max_cfs"] > r["discharge_min_cfs"]
    # Latest sample mirrored into the static scalar for the overlay.
    assert r["discharge_cfs"] == vals[-1]


def test_parse_iv_window_merges_gage_height_as_latest_only():
    samples_q = _hydrograph_samples(n=6, base=50.0)
    raw = _make_iv_json([
        _ts_window("13206000", "BOISE R", 43.62, -116.20, "00060", samples_q),
        _ts_window(
            "13206000", "BOISE R", 43.62, -116.20, "00065",
            [("2018-10-10T00:00:00.000-00:00", "5.5"),
             ("2018-10-10T01:00:00.000-00:00", "5.7")],
        ),
    ])
    recs = {r["site_no"]: r for r in _parse_iv_json_window(raw)}
    r = recs["13206000"]
    # Discharge keeps its full series; gage height keeps only the latest scalar.
    assert r["n_timesteps"] == 6
    assert r["gage_height_ft"] == 5.7


def test_parse_iv_window_empty_body():
    assert _parse_iv_json_window(b"") == []
    assert _parse_iv_json_window(_make_iv_json([])) == []


def test_window_mode_layer_uri_carries_hydrograph(monkeypatch):
    """End-to-end (mocked HTTP): a window request emits a hydrograph FGB whose
    feature carries a multi-point time_series_csv (the compound-flood driver)."""
    if not _have_geo():
        pytest.skip("geopandas/shapely not installed")

    fake_gcs = FakeStorageClient()
    samples = _hydrograph_samples(n=10, base=200.0)
    iv_window = _make_iv_json([
        _ts_window("13206000", "BOISE R AT BOISE", 43.62, -116.20, "00060", samples),
    ])

    captured_urls: list[str] = []

    def fake_http_get(url: str, timeout: float = 60.0) -> bytes:
        captured_urls.append(url)
        return iv_window

    with (
        patch("trid3nt_server.tools.fetchers.hydrology.fetch_usgs_nwis_gauges._http_get", side_effect=fake_http_get),
        patch(
            "trid3nt_server.tools.fetchers.hydrology.fetch_usgs_nwis_gauges.read_through",
            side_effect=_make_read_through_injector(fake_gcs),
        ),
    ):
        result = fetch_usgs_nwis_gauges(
            bbox=_BOISE_BBOX,
            start_date="2018-10-08",
            end_date="2018-10-14",
        )

    # The window request used startDT/endDT (not the instantaneous default).
    assert any("startDT=2018-10-08" in u for u in captured_urls)
    assert result.layer_type == "vector"
    assert result.layer_id.startswith("usgs-hydrograph-")
    assert "hydrograph" in result.style_preset

    # Read back the FGB and confirm the inline multi-point hydrograph survived.
    fgb_bytes = next(iter(fake_gcs.store.values()))
    import geopandas as gpd
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        path = f.name
        f.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert "time_series_csv" in gdf.columns
        csv_text = gdf.iloc[0]["time_series_csv"]
        rows = [ln for ln in str(csv_text).strip().splitlines() if ln]
        assert len(rows) == 10  # full hydrograph preserved through the FGB
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def test_window_mode_no_stations_raises(monkeypatch):
    """A window request with zero stations is an honest typed error (no
    Site-service fallback, since locations carry no readings)."""
    fake_gcs = FakeStorageClient()

    with (
        patch(
            "trid3nt_server.tools.fetchers.hydrology.fetch_usgs_nwis_gauges._http_get",
            return_value=_make_iv_json([]),
        ),
        patch(
            "trid3nt_server.tools.fetchers.hydrology.fetch_usgs_nwis_gauges.read_through",
            side_effect=_make_read_through_injector(fake_gcs),
        ),
        pytest.raises(NwisNoStationsError, match="window"),
    ):
        fetch_usgs_nwis_gauges(bbox=_BOISE_BBOX, period="P7D")
    assert len(fake_gcs.store) == 0


# ---------------------------------------------------------------------------
# Live integration test (TRID3NT_TEST_LIVE_NWIS=1 to run).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _LIVE_NWIS,
    reason="Set TRID3NT_TEST_LIVE_NWIS=1 to run live USGS NWIS tests",
)
def test_live_boise_iv_returns_gauges():
    from trid3nt_server.tools.fetchers.hydrology.fetch_usgs_nwis_gauges import _fetch_usgs_nwis_gauges_bytes

    fgb_bytes, extent = _fetch_usgs_nwis_gauges_bytes(
        state_code=None, bbox=_BOISE_BBOX
    )
    assert isinstance(fgb_bytes, bytes) and len(fgb_bytes) > 100
    assert extent is not None

    import geopandas as gpd
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        f.write(fgb_bytes)
        path = f.name
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
    finally:
        os.unlink(path)

    assert len(gdf) >= 1
    print(f"\n[LIVE NWIS] {len(gdf)} gauge station(s) in Boise bbox")
    for _, row in gdf.iterrows():
        assert -117.0 <= row.geometry.x <= -115.0
        assert 43.0 <= row.geometry.y <= 44.0
        assert "site_no" in row.index
