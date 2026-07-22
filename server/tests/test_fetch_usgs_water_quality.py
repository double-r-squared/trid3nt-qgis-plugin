"""Unit tests for the ``fetch_usgs_water_quality`` atomic tool.

Covers the observed water-quality companion to the MODFLOW-GWT contamination
demos: a USGS / EPA Water Quality Portal (WQP) sample-site fetcher (observed
characteristic concentrations) that joins the Station service (locations) with
the Result service (measured values). All HTTP is mocked — no live network.

Coverage:
- Tool is registered in TOOL_REGISTRY with expected metadata.
- Categorized under hydrology.
- Error classes carry correct retryable + error_code attributes.
- Characteristic alias resolution (friendly -> canonical WQP name + passthrough).
- Input validation: missing bbox, bad bbox shape/range, degenerate bbox,
  bbox too large, empty characteristic.
- Station GeoJSON parse: features -> {site_id: location}; drops bad geometry.
- Result CSV parse: latest numeric result per site; skips non-numeric;
  picks the most-recent ActivityStartDate.
- Join: left-on-stations; a site with no result still renders (null value).
- Happy path: Station + Result -> point FGB with merged latest value props.
- Station empty -> WqpNoSitesError (honest typed error, never empty success).
- WQP HTTP 400 (bad characteristic) -> WqpInputError.
- LayerURI shape: layer_type="vector", role="primary", style_preset, bbox set.
- Payload estimator returns a positive float.
- Extra-kwargs absorption (LLM hallucination guard).

Live test (gated by TRID3NT_TEST_LIVE_WQP=1): real WQP Station+Result request
for a small Iowa corn-belt bbox; confirms >=1 nitrate site with a value.
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
from trid3nt_server.tools.fetch_usgs_water_quality import (
    WqpError,
    WqpInputError,
    WqpNoSitesError,
    WqpUpstreamError,
    _build_result_url,
    _build_station_url,
    _join_sites,
    _parse_result_csv,
    _parse_station_geojson,
    _records_bbox,
    _resolve_characteristic,
    _validate_bbox,
    estimate_payload_mb,
    fetch_usgs_water_quality,
)


# ---------------------------------------------------------------------------
# Constants / fixtures.
# ---------------------------------------------------------------------------

_LIVE_WQP = os.environ.get("TRID3NT_TEST_LIVE_WQP") == "1"

# A small Iowa corn-belt bbox (~0.5 x 0.5 = 0.25 deg^2) — heavy nitrate
# agriculture, well under the area cap.
_IOWA_BBOX = (-94.0, 42.0, -93.5, 42.5)

# An oversized bbox (~12 x 12 = 144 deg^2) — exceeds the 100 deg^2 cap.
_HUGE_BBOX = (-100.0, 30.0, -88.0, 42.0)

_PINNED_NOW = datetime.datetime(2026, 6, 27, 12, 0, 0, tzinfo=datetime.timezone.utc)


def _make_station_geojson(
    sites: list[tuple[str, str, str, float, float]],
) -> bytes:
    """Build a WQP Station-service GeoJSON FeatureCollection.

    sites: (site_id, name, type, lon, lat).
    """
    feats = []
    for site_id, name, stype, lon, lat in sites:
        feats.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": {
                    "MonitoringLocationIdentifier": site_id,
                    "MonitoringLocationName": name,
                    "ResolvedMonitoringLocationTypeName": stype,
                },
            }
        )
    return json.dumps({"type": "FeatureCollection", "features": feats}).encode("utf-8")


def _make_result_csv(rows: list[dict[str, str]]) -> bytes:
    """Build a WQP Result-service CSV body (the resultPhysChem columns we read)."""
    cols = [
        "MonitoringLocationIdentifier",
        "CharacteristicName",
        "ResultMeasureValue",
        "ResultMeasure/MeasureUnitCode",
        "ActivityStartDate",
        "ResultSampleFractionText",
    ]
    lines = [",".join(cols)]
    for r in rows:
        lines.append(",".join(str(r.get(c, "")) for c in cols))
    return "\n".join(lines).encode("utf-8")


# ---------------------------------------------------------------------------
# In-memory S3 read-through injector (mirrors sibling fetcher tests).
# ---------------------------------------------------------------------------


class FakeStorageClient:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}


def _make_read_through_injector(fake_gcs):
    """S3-only in-memory read-through injector (GCP decommissioned)."""
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


def _read_fgb(fgb_bytes: bytes):
    import geopandas as gpd

    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        path = f.name
        f.write(fgb_bytes)
    try:
        return gpd.read_file(path, engine="pyogrio")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Registration / categorization.
# ---------------------------------------------------------------------------


def test_tool_is_registered():
    assert "fetch_usgs_water_quality" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_usgs_water_quality"]
    assert entry.metadata.name == "fetch_usgs_water_quality"
    assert entry.metadata.ttl_class == "semi-static-7d"
    assert entry.metadata.source_class == "usgs_water_quality"
    assert entry.metadata.cacheable is True
    assert entry.metadata.payload_mb_estimator_name == "estimate_payload_mb"


def test_supports_global_query_is_false():
    entry = TOOL_REGISTRY["fetch_usgs_water_quality"]
    sgq = getattr(entry.metadata, "supports_global_query", None)
    assert sgq in (False, None), f"expected False or None; got {sgq!r}"


def test_categorized_under_hydrology():
    # Category wiring lands centrally (categories.py) in the main session, which
    # unions the metadata this tool returns. Until then PRIMARY_CATEGORY has no
    # entry; once wired it MUST be hydrology. Assert the invariant either way.
    from trid3nt_server.categories import PRIMARY_CATEGORY, tools_for_category

    primary = PRIMARY_CATEGORY.get("fetch_usgs_water_quality")
    if primary is None:
        pytest.skip(
            "fetch_usgs_water_quality not yet wired into categories.py "
            "(main session unions the returned metadata)"
        )
    assert primary == "hydrology"
    assert "fetch_usgs_water_quality" in tools_for_category("hydrology")


# ---------------------------------------------------------------------------
# Error class attributes.
# ---------------------------------------------------------------------------


def test_error_classes_attributes():
    for cls, retryable in [
        (WqpError, True),
        (WqpInputError, False),
        (WqpUpstreamError, True),
        (WqpNoSitesError, False),
    ]:
        inst = cls("test")
        assert inst.retryable is retryable, f"{cls.__name__}.retryable wrong"
        assert isinstance(inst.error_code, str) and inst.error_code != ""


# ---------------------------------------------------------------------------
# Characteristic alias resolution.
# ---------------------------------------------------------------------------


def test_resolve_characteristic_aliases():
    assert _resolve_characteristic("nitrate") == "Nitrate"
    assert _resolve_characteristic("  Lead ") == "Lead"
    assert _resolve_characteristic("do") == "Dissolved oxygen (DO)"
    assert _resolve_characteristic("specific_conductance") == "Specific conductance"
    assert _resolve_characteristic("ph") == "pH"


def test_resolve_characteristic_passthrough():
    # An unmapped canonical name passes through verbatim (full WQP vocabulary).
    assert _resolve_characteristic("Kjeldahl nitrogen") == "Kjeldahl nitrogen"


def test_resolve_characteristic_empty_raises():
    with pytest.raises(WqpInputError, match="characteristic is required"):
        _resolve_characteristic("")
    with pytest.raises(WqpInputError, match="characteristic is required"):
        _resolve_characteristic("   ")


# ---------------------------------------------------------------------------
# Input validation.
# ---------------------------------------------------------------------------


def test_validate_bbox_ok():
    _validate_bbox(_IOWA_BBOX)  # no exception


def test_validate_bbox_degenerate():
    with pytest.raises(WqpInputError, match="degenerate"):
        _validate_bbox((-94.0, 42.0, -94.0, 42.5))


def test_validate_bbox_wrong_length():
    with pytest.raises(WqpInputError, match="west, south, east, north"):
        _validate_bbox((1.0, 2.0, 3.0))  # type: ignore[arg-type]


def test_validate_bbox_out_of_range():
    with pytest.raises(WqpInputError, match="lon"):
        _validate_bbox((-200.0, 42.0, -93.5, 42.5))


def test_no_bbox_raises_input_error():
    with pytest.raises(WqpInputError, match="requires bbox"):
        fetch_usgs_water_quality()


def test_bbox_too_large_raises():
    with pytest.raises(WqpInputError, match="exceeds"):
        fetch_usgs_water_quality(bbox=_HUGE_BBOX, characteristic="nitrate")


# ---------------------------------------------------------------------------
# URL builders.
# ---------------------------------------------------------------------------


def test_build_station_url():
    url = _build_station_url(bbox=_IOWA_BBOX, characteristic="Nitrate")
    assert url.startswith("https://www.waterqualitydata.us/data/Station/search")
    assert "mimeType=geojson" in url
    assert "characteristicName=Nitrate" in url
    assert "bBox=" in url


def test_build_result_url():
    url = _build_result_url(bbox=_IOWA_BBOX, characteristic="Nitrate")
    assert url.startswith("https://www.waterqualitydata.us/data/Result/search")
    assert "mimeType=csv" in url
    assert "dataProfile=resultPhysChem" in url
    assert "characteristicName=Nitrate" in url


# ---------------------------------------------------------------------------
# Station GeoJSON parsing.
# ---------------------------------------------------------------------------


def test_parse_station_geojson_extracts_points():
    raw = _make_station_geojson([
        ("USGS-05469860", "Mud Lake Ditch", "Stream", -93.64, 42.31),
        ("USGS-12345678", "Some Well", "Well", -93.80, 42.10),
    ])
    stations = _parse_station_geojson(raw)
    assert set(stations) == {"USGS-05469860", "USGS-12345678"}
    s = stations["USGS-05469860"]
    assert s["site_name"] == "Mud Lake Ditch"
    assert s["site_type"] == "Stream"
    assert abs(s["lon"] - (-93.64)) < 1e-9 and abs(s["lat"] - 42.31) < 1e-9


def test_parse_station_geojson_drops_bad_geometry():
    raw = json.dumps({
        "type": "FeatureCollection",
        "features": [
            {  # not a Point
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
                "properties": {"MonitoringLocationIdentifier": "BAD-1"},
            },
            {  # missing id
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [-93.0, 42.0]},
                "properties": {},
            },
            {  # good
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [-93.0, 42.0]},
                "properties": {"MonitoringLocationIdentifier": "GOOD-1"},
            },
        ],
    }).encode("utf-8")
    stations = _parse_station_geojson(raw)
    assert set(stations) == {"GOOD-1"}


def test_parse_station_geojson_empty():
    assert _parse_station_geojson(b"") == {}
    assert _parse_station_geojson(
        json.dumps({"type": "FeatureCollection", "features": []}).encode("utf-8")
    ) == {}


def test_parse_station_geojson_bad_json_raises_upstream():
    with pytest.raises(WqpUpstreamError, match="not valid GeoJSON"):
        _parse_station_geojson(b"<html>not json</html>")


# ---------------------------------------------------------------------------
# Result CSV parsing.
# ---------------------------------------------------------------------------


def test_parse_result_csv_keeps_latest_numeric_per_site():
    raw = _make_result_csv([
        {
            "MonitoringLocationIdentifier": "S1",
            "CharacteristicName": "Nitrate",
            "ResultMeasureValue": "1.0",
            "ResultMeasure/MeasureUnitCode": "mg/l as N",
            "ActivityStartDate": "2010-05-01",
            "ResultSampleFractionText": "Dissolved",
        },
        {  # newer -> wins
            "MonitoringLocationIdentifier": "S1",
            "CharacteristicName": "Nitrate",
            "ResultMeasureValue": "3.5",
            "ResultMeasure/MeasureUnitCode": "mg/l as N",
            "ActivityStartDate": "2020-06-15",
            "ResultSampleFractionText": "Dissolved",
        },
        {  # non-numeric -> skipped
            "MonitoringLocationIdentifier": "S2",
            "CharacteristicName": "Nitrate",
            "ResultMeasureValue": "ND",
            "ResultMeasure/MeasureUnitCode": "mg/l as N",
            "ActivityStartDate": "2021-01-01",
            "ResultSampleFractionText": "Dissolved",
        },
    ])
    latest = _parse_result_csv(raw)
    assert set(latest) == {"S1"}
    assert latest["S1"]["value"] == 3.5
    assert latest["S1"]["date"] == "2020-06-15"
    assert latest["S1"]["unit"] == "mg/l as N"
    assert latest["S1"]["fraction"] == "Dissolved"


def test_parse_result_csv_empty():
    assert _parse_result_csv(b"") == {}
    # header-only CSV -> no data rows
    assert _parse_result_csv(_make_result_csv([])) == {}


# ---------------------------------------------------------------------------
# Join (left-on-stations).
# ---------------------------------------------------------------------------


def test_join_sites_left_on_stations():
    stations = {
        "S1": {"site_id": "S1", "site_name": "A", "site_type": "Stream", "lon": -93.0, "lat": 42.0},
        "S2": {"site_id": "S2", "site_name": "B", "site_type": "Well", "lon": -93.1, "lat": 42.1},
    }
    results = {
        "S1": {"value": 3.5, "unit": "mg/l as N", "date": "2020-06-15",
               "fraction": "Dissolved", "characteristic": "Nitrate"},
    }
    recs = {r["site_id"]: r for r in _join_sites(stations, results)}
    assert set(recs) == {"S1", "S2"}
    assert recs["S1"]["value"] == 3.5
    assert recs["S1"]["result_date"] == "2020-06-15"
    # S2 has no result -> renders with null value but survives.
    assert recs["S2"]["value"] is None
    assert recs["S2"]["unit"] == ""


def test_join_sites_empty_stations():
    assert _join_sites({}, {}) == []


# ---------------------------------------------------------------------------
# Records-extent helper.
# ---------------------------------------------------------------------------


def test_records_bbox_pads_single_point():
    extent = _records_bbox([{"lon": -93.6, "lat": 42.3}])
    assert extent is not None
    west, south, east, north = extent
    assert west < -93.6 < east
    assert south < 42.3 < north


def test_records_bbox_empty_is_none():
    assert _records_bbox([]) is None


# ---------------------------------------------------------------------------
# Happy path -> point FGB with merged latest value props.
# ---------------------------------------------------------------------------


def test_happy_path_layer_uri_shape():
    if not _have_geo():
        pytest.skip("geopandas/shapely not installed")

    fake_gcs = FakeStorageClient()
    station_geojson = _make_station_geojson([
        ("USGS-S1", "Mud Lake Ditch", "Stream", -93.64, 42.31),
        ("USGS-S2", "Mason Creek", "Stream", -93.70, 42.20),
    ])
    result_csv = _make_result_csv([
        {
            "MonitoringLocationIdentifier": "USGS-S1",
            "CharacteristicName": "Nitrate",
            "ResultMeasureValue": "3.5",
            "ResultMeasure/MeasureUnitCode": "mg/l as N",
            "ActivityStartDate": "2020-06-15",
            "ResultSampleFractionText": "Dissolved",
        },
    ])

    def fake_http_get(url: str, timeout: float = 180.0) -> bytes:
        if "/Result/search" in url:
            return result_csv
        return station_geojson

    with (
        patch("trid3nt_server.tools.fetch_usgs_water_quality._http_get", side_effect=fake_http_get),
        patch(
            "trid3nt_server.tools.fetch_usgs_water_quality.read_through",
            side_effect=_make_read_through_injector(fake_gcs),
        ),
    ):
        result = fetch_usgs_water_quality(bbox=_IOWA_BBOX, characteristic="nitrate")

    assert result.layer_type == "vector"
    assert result.role == "primary"
    assert result.style_preset == "water_quality"
    assert result.units == "Nitrate"
    assert result.uri.startswith("s3://")
    assert "usgs_water_quality" in result.uri
    assert result.layer_id.startswith("wqp-")
    assert result.bbox is not None
    west, south, east, north = result.bbox
    assert west <= -93.70 and east >= -93.64
    assert south <= 42.20 and north >= 42.31

    # Read back the FGB and verify 2 site points; S1 carries the latest value,
    # S2 survives with a null value (left-on-stations join).
    assert len(fake_gcs.store) == 1
    gdf = _read_fgb(next(iter(fake_gcs.store.values())))
    assert len(gdf) == 2
    assert set(gdf["site_id"]) == {"USGS-S1", "USGS-S2"}
    s1 = gdf[gdf["site_id"] == "USGS-S1"].iloc[0]
    assert abs(s1["value"] - 3.5) < 1e-6
    assert s1["unit"] == "mg/l as N"
    assert s1["result_date"] == "2020-06-15"
    s2 = gdf[gdf["site_id"] == "USGS-S2"].iloc[0]
    assert s2["value"] is None or str(s2["value"]) in ("nan", "None")


# ---------------------------------------------------------------------------
# Station empty -> honest typed error (never an empty success layer).
# ---------------------------------------------------------------------------


def test_no_sites_raises_no_sites_error():
    fake_gcs = FakeStorageClient()
    empty_stations = _make_station_geojson([])

    with (
        patch(
            "trid3nt_server.tools.fetch_usgs_water_quality._http_get",
            return_value=empty_stations,
        ),
        patch(
            "trid3nt_server.tools.fetch_usgs_water_quality.read_through",
            side_effect=_make_read_through_injector(fake_gcs),
        ),
        pytest.raises(WqpNoSitesError, match="No Water Quality Portal monitoring"),
    ):
        fetch_usgs_water_quality(bbox=_IOWA_BBOX, characteristic="nitrate")

    # Nothing written to cache on the honest-error path.
    assert len(fake_gcs.store) == 0


# ---------------------------------------------------------------------------
# WQP HTTP 400 (bad characteristic) -> WqpInputError.
# ---------------------------------------------------------------------------


def test_http_400_maps_to_input_error():
    import urllib.error

    fake_gcs = FakeStorageClient()

    def fake_http_get(url: str, timeout: float = 180.0) -> bytes:
        # Mirror the real _http_get's 400 -> WqpInputError mapping by raising
        # from the patched seam directly (the tool relies on _http_get's typed
        # translation; here we assert the tool surfaces it).
        raise WqpInputError("Water Quality Portal rejected the request (HTTP 400)")

    with (
        patch("trid3nt_server.tools.fetch_usgs_water_quality._http_get", side_effect=fake_http_get),
        patch(
            "trid3nt_server.tools.fetch_usgs_water_quality.read_through",
            side_effect=_make_read_through_injector(fake_gcs),
        ),
        pytest.raises(WqpInputError, match="HTTP 400"),
    ):
        fetch_usgs_water_quality(bbox=_IOWA_BBOX, characteristic="bogus_characteristic")
    assert len(fake_gcs.store) == 0


# ---------------------------------------------------------------------------
# Payload estimator.
# ---------------------------------------------------------------------------


def test_estimate_payload_mb_positive():
    assert estimate_payload_mb(bbox=_IOWA_BBOX) > 0.0
    assert estimate_payload_mb() > 0.0


# ---------------------------------------------------------------------------
# Extra-kwargs absorption (LLM hallucination guard).
# ---------------------------------------------------------------------------


def test_extra_kwargs_absorbed():
    if not _have_geo():
        pytest.skip("geopandas/shapely not installed")

    fake_gcs = FakeStorageClient()
    station_geojson = _make_station_geojson([
        ("USGS-S1", "Mud Lake Ditch", "Stream", -93.64, 42.31),
    ])
    result_csv = _make_result_csv([
        {
            "MonitoringLocationIdentifier": "USGS-S1",
            "CharacteristicName": "Nitrate",
            "ResultMeasureValue": "3.5",
            "ResultMeasure/MeasureUnitCode": "mg/l as N",
            "ActivityStartDate": "2020-06-15",
            "ResultSampleFractionText": "Dissolved",
        },
    ])

    def fake_http_get(url: str, timeout: float = 180.0) -> bytes:
        if "/Result/search" in url:
            return result_csv
        return station_geojson

    with (
        patch("trid3nt_server.tools.fetch_usgs_water_quality._http_get", side_effect=fake_http_get),
        patch(
            "trid3nt_server.tools.fetch_usgs_water_quality.read_through",
            side_effect=_make_read_through_injector(fake_gcs),
        ),
    ):
        result = fetch_usgs_water_quality(
            bbox=_IOWA_BBOX,
            characteristic="nitrate",
            invented_param="foo",  # type: ignore[call-arg]
            another_fake=42,  # type: ignore[call-arg]
        )
    assert result.layer_type == "vector"


# ---------------------------------------------------------------------------
# Live integration test (TRID3NT_TEST_LIVE_WQP=1 to run).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _LIVE_WQP,
    reason="Set TRID3NT_TEST_LIVE_WQP=1 to run live WQP tests",
)
def test_live_iowa_nitrate_returns_sites():
    from trid3nt_server.tools.fetch_usgs_water_quality import _fetch_water_quality_bytes

    fgb_bytes, extent = _fetch_water_quality_bytes(
        bbox=_IOWA_BBOX, characteristic="Nitrate"
    )
    assert isinstance(fgb_bytes, bytes) and len(fgb_bytes) > 100
    assert extent is not None

    gdf = _read_fgb(fgb_bytes)
    assert len(gdf) >= 1
    n_val = int(gdf["value"].notna().sum())
    print(f"\n[LIVE WQP] {len(gdf)} nitrate site(s) in Iowa bbox; {n_val} with a value")
    assert n_val >= 1
    for _, row in gdf.iterrows():
        assert -94.5 <= row.geometry.x <= -93.0
        assert 41.5 <= row.geometry.y <= 42.6
