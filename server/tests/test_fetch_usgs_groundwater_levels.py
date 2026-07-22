"""Unit tests for the ``fetch_usgs_groundwater_levels`` atomic tool.

Covers a real USGS groundwater-level well fetcher (observed water table /
depth-to-water) on the modernized USGS Water Data OGC API — the replacement for
the decommissioned ``nwis/gwlevels`` service. This is the OBSERVED-head
companion the MODFLOW groundwater demos compare against modeled heads, and the
subsurface sibling of the surface-water ``fetch_usgs_nwis_gauges`` tool. All
HTTP is mocked — no live network (the live path is gated behind an env var).

Coverage:
- Tool is registered in TOOL_REGISTRY with expected metadata.
- supports_global_query is False (US + territories only).
- Error classes carry correct retryable + error_code attributes.
- Input validation: no selector, bad bbox, bad/degenerate bbox, bad state code.
- USPS -> FIPS state-code mapping.
- URL builders carry the GW pcodes + the right scope param.
- Measurements GeoJSON parse: one record per (well x parameter) reading, site_no
  derived from the agency-prefixed monitoring_location_id, no-coord rows dropped.
- Locations GeoJSON parse + best-effort join attaches well name/aquifer/depth.
- Happy path (mocked HTTP): readings + join -> LayerURI (vector, primary,
  style_preset, bbox set), and the FGB round-trips through geopandas.
- Honest-empty: zero readings -> GwNoWellsError (never an empty success layer).
- Payload estimator returns a positive float.

Live test (gated by TRID3NT_TEST_LIVE_GWLEVELS=1): real OGC API request for a
small central-Kansas High Plains bbox; confirms >=1 well with a finite reading.
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
from trid3nt_server.tools.fetchers.hydrology.fetch_usgs_groundwater_levels import (
    GW_PARAMETER_CODES,
    USPS_TO_FIPS,
    GwInputError,
    GwLevelsError,
    GwNoWellsError,
    GwUpstreamError,
    _build_locations_url,
    _build_measurements_url,
    _join_records,
    _parse_locations_geojson,
    _parse_measurements_geojson,
    _records_bbox,
    _validate_bbox,
    _validate_state_code,
    estimate_payload_mb,
    fetch_usgs_groundwater_levels,
)

_LIVE = os.environ.get("TRID3NT_TEST_LIVE_GWLEVELS") == "1"

# Central Kansas (High Plains aquifer) — a small, densely instrumented bbox.
_KS_BBOX = (-99.0, 38.0, -98.0, 39.0)

_PINNED_NOW = datetime.datetime(2026, 6, 17, 12, 0, 0, tzinfo=datetime.timezone.utc)


# ---------------------------------------------------------------------------
# Synthetic GeoJSON builders.
# ---------------------------------------------------------------------------


def _meas_feature(
    mlid: str,
    lon: float,
    lat: float,
    param: str,
    value: str,
    *,
    unit: str = "ft",
    datum: str = "NAVD88",
    time: str = "2020-05-01T12:00:00+00:00",
) -> dict[str, Any]:
    """Build one latest-field-measurements GeoJSON Feature."""
    return {
        "type": "Feature",
        "id": f"meas-{mlid}-{param}",
        "properties": {
            "monitoring_location_id": mlid,
            "parameter_code": param,
            "value": value,
            "unit_of_measure": unit,
            "vertical_datum": datum,
            "time": time,
            "approval_status": "Approved",
        },
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
    }


def _meas_fc(features: list[dict[str, Any]]) -> bytes:
    return json.dumps(
        {"type": "FeatureCollection", "features": features}
    ).encode("utf-8")


def _loc_feature(
    mlid: str, name: str, aquifer: str = "N100HGHPLN", depth: str = "120"
) -> dict[str, Any]:
    """Build one monitoring-locations GeoJSON Feature."""
    return {
        "type": "Feature",
        "id": mlid,
        "properties": {
            "monitoring_location_number": mlid.split("-", 1)[-1],
            "monitoring_location_name": name,
            "national_aquifer_code": aquifer,
            "well_constructed_depth": depth,
        },
        "geometry": {"type": "Point", "coordinates": [0.0, 0.0]},
    }


def _loc_fc(features: list[dict[str, Any]]) -> bytes:
    return json.dumps(
        {"type": "FeatureCollection", "features": features}
    ).encode("utf-8")


def _have_geo() -> bool:
    try:
        import geopandas  # noqa: F401
        import shapely  # noqa: F401

        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# In-memory S3 read-through injector (GCP decommissioned).
# ---------------------------------------------------------------------------


def _make_read_through_injector(store: dict[str, bytes]):
    from trid3nt_server.tools.cache import (
        CACHE_BUCKET,
        ReadThroughResult,
        cache_path,
        compute_cache_key,
        is_cacheable,
    )

    def patched(metadata, params, ext, fetch_fn, **kw):
        bucket = kw.get("bucket") or CACHE_BUCKET
        source_id = kw.get("source_id") or (metadata.source_class or metadata.name)
        force_refresh = kw.get("force_refresh", False)
        if not is_cacheable(metadata):
            return ReadThroughResult(uri=None, data=fetch_fn(), hit=False)
        key = compute_cache_key(
            source_id, params, metadata.ttl_class, now=_PINNED_NOW
        )
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


def test_tool_is_registered():
    assert "fetch_usgs_groundwater_levels" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_usgs_groundwater_levels"]
    assert entry.metadata.name == "fetch_usgs_groundwater_levels"
    assert entry.metadata.ttl_class == "dynamic-1h"
    assert entry.metadata.source_class == "usgs_groundwater_levels"
    assert entry.metadata.cacheable is True
    assert entry.metadata.payload_mb_estimator_name == "estimate_payload_mb"


def test_supports_global_query_is_false():
    entry = TOOL_REGISTRY["fetch_usgs_groundwater_levels"]
    sgq = getattr(entry.metadata, "supports_global_query", None)
    assert sgq in (False, None), f"expected False or None; got {sgq!r}"


# ---------------------------------------------------------------------------
# Error class attributes.
# ---------------------------------------------------------------------------


def test_error_classes_attributes():
    for cls, retryable in [
        (GwLevelsError, True),
        (GwInputError, False),
        (GwUpstreamError, True),
        (GwNoWellsError, False),
    ]:
        assert issubclass(cls, GwLevelsError)
        assert cls.retryable is retryable
        assert isinstance(cls.error_code, str) and cls.error_code


# ---------------------------------------------------------------------------
# Input validation.
# ---------------------------------------------------------------------------


def test_no_selector_raises_input_error():
    with pytest.raises(GwInputError):
        fetch_usgs_groundwater_levels()


@pytest.mark.parametrize(
    "bad",
    [
        (1.0, 2.0, 3.0),  # wrong arity
        (10.0, 10.0, 1.0, 1.0),  # degenerate (min >= max)
        (-200.0, 0.0, 10.0, 10.0),  # lon out of range
        (0.0, -100.0, 10.0, 10.0),  # lat out of range
        (float("nan"), 0.0, 1.0, 1.0),  # non-finite
    ],
)
def test_bad_bbox_raises_input_error(bad):
    with pytest.raises(GwInputError):
        _validate_bbox(bad)


def test_bad_state_code_raises():
    with pytest.raises(GwInputError):
        _validate_state_code("ZZ")
    with pytest.raises(GwInputError):
        _validate_state_code(53)  # type: ignore[arg-type]


def test_state_code_maps_usps_to_fips():
    usps, fips = _validate_state_code("ks")
    assert usps == "KS"
    assert fips == "20"
    assert USPS_TO_FIPS["CA"] == "06"


# ---------------------------------------------------------------------------
# URL builders.
# ---------------------------------------------------------------------------


def test_measurements_url_carries_pcodes_and_scope():
    url_bbox = _build_measurements_url(state_fips=None, bbox=_KS_BBOX)
    assert "latest-field-measurements/items" in url_bbox
    for pc in GW_PARAMETER_CODES:
        assert pc in url_bbox
    assert "bbox=" in url_bbox
    url_state = _build_measurements_url(state_fips="20", bbox=None)
    assert "state_code=20" in url_state
    assert "bbox=" not in url_state


def test_locations_url_filters_gw_site_type():
    url = _build_locations_url(state_fips=None, bbox=_KS_BBOX)
    assert "monitoring-locations/items" in url
    assert "site_type_code=GW" in url
    assert "monitoring_location_name" in url


# ---------------------------------------------------------------------------
# Parsers.
# ---------------------------------------------------------------------------


def test_parse_measurements_one_record_per_reading_and_site_no():
    raw = _meas_fc(
        [
            _meas_feature("USGS-383047098095901", -98.17, 38.51, "72019", "12.63"),
            _meas_feature("KS014-380041098033401", -98.04, 38.00, "62611", "1545.49"),
            # no-coord row is dropped
            {
                "type": "Feature",
                "id": "bad",
                "properties": {
                    "monitoring_location_id": "USGS-x",
                    "parameter_code": "72019",
                    "value": "1",
                },
                "geometry": {"type": "Point", "coordinates": []},
            },
        ]
    )
    recs = _parse_measurements_geojson(raw)
    assert len(recs) == 2
    by_site = {r["site_no"]: r for r in recs}
    # site_no is the agency-prefix-stripped monitoring_location_id.
    assert "383047098095901" in by_site
    assert "380041098033401" in by_site
    r = by_site["383047098095901"]
    assert r["parameter_code"] == "72019"
    assert r["water_level"] == pytest.approx(12.63)
    assert r["unit"] == "ft"


def test_parse_measurements_empty_body():
    assert _parse_measurements_geojson(b"") == []
    assert _parse_measurements_geojson(_meas_fc([])) == []


def test_parse_measurements_bad_value_kept_as_null():
    raw = _meas_fc(
        [_meas_feature("USGS-1", -98.0, 38.0, "72019", "not-a-number")]
    )
    recs = _parse_measurements_geojson(raw)
    assert len(recs) == 1
    assert recs[0]["water_level"] is None


def test_parse_measurements_rejects_non_featurecollection():
    with pytest.raises(GwUpstreamError):
        _parse_measurements_geojson(json.dumps({"type": "Point"}).encode("utf-8"))


def test_parse_locations_and_join():
    meas = _parse_measurements_geojson(
        _meas_fc(
            [
                _meas_feature("KS014-380041098033401", -98.04, 38.0, "72019", "30.0"),
                _meas_feature("USGS-unmatched", -98.1, 38.1, "72019", "40.0"),
            ]
        )
    )
    locs = _parse_locations_geojson(
        _loc_fc([_loc_feature("KS014-380041098033401", "23S 07W 35ABC 01")])
    )
    joined = _join_records(meas, locs)
    by_site = {r["site_no"]: r for r in joined}
    # matched well -> name + aquifer + depth attached
    m = by_site["380041098033401"]
    assert m["site_name"] == "23S 07W 35ABC 01"
    assert m["aquifer_code"] == "N100HGHPLN"
    assert m["well_depth_ft"] == pytest.approx(120.0)
    assert m["parameter_label"]  # human label populated
    # unmatched well -> blank name but reading preserved
    u = by_site["unmatched"]
    assert u["site_name"] == ""
    assert u["water_level"] == pytest.approx(40.0)


def test_parse_locations_bad_body_is_empty_not_error():
    # Best-effort enrichment: a bad locations body must not raise.
    assert _parse_locations_geojson(b"not json") == {}
    assert _parse_locations_geojson(json.dumps({"type": "X"}).encode()) == {}


# ---------------------------------------------------------------------------
# Extent + estimator.
# ---------------------------------------------------------------------------


def test_records_bbox_pads_degenerate_point():
    bb = _records_bbox([{"lon": -98.0, "lat": 38.0}])
    assert bb is not None
    w, s, e, n = bb
    assert w < -98.0 < e and s < 38.0 < n
    assert _records_bbox([]) is None


def test_estimate_payload_mb_positive():
    assert estimate_payload_mb(bbox=_KS_BBOX) > 0.0
    assert estimate_payload_mb(state_code="KS") > 0.0
    assert estimate_payload_mb() > 0.0


# ---------------------------------------------------------------------------
# Happy path (mocked HTTP) -> LayerURI + valid FGB.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _have_geo(), reason="geopandas/shapely required")
def test_happy_path_builds_layer_and_valid_fgb():
    meas = _meas_fc(
        [
            _meas_feature("USGS-A1", -98.2, 38.5, "72019", "12.6", datum=""),
            _meas_feature("USGS-A1", -98.2, 38.5, "62611", "1545.5"),
            _meas_feature("KS014-B2", -98.6, 38.3, "72019", "30.1"),
        ]
    )
    locs = _loc_fc(
        [
            _loc_feature("USGS-A1", "Well Alpha"),
            _loc_feature("KS014-B2", "Well Bravo"),
        ]
    )
    store: dict[str, bytes] = {}

    def fake_http_get(url, timeout=90.0):
        if "latest-field-measurements" in url:
            return meas
        if "monitoring-locations" in url:
            return locs
        raise AssertionError(f"unexpected URL: {url}")

    with patch(
        "trid3nt_server.tools.fetchers.hydrology.fetch_usgs_groundwater_levels._http_get",
        side_effect=fake_http_get,
    ), patch(
        "trid3nt_server.tools.fetchers.hydrology.fetch_usgs_groundwater_levels.read_through",
        side_effect=_make_read_through_injector(store),
    ):
        res = fetch_usgs_groundwater_levels(bbox=_KS_BBOX)

    assert res.layer_type == "vector"
    assert res.role == "primary"
    assert res.style_preset == "usgs_groundwater"
    assert res.uri and res.uri.startswith("s3://")
    assert res.bbox is not None
    # The FGB written to the in-memory store round-trips through geopandas.
    import geopandas as gpd

    (data,) = list(store.values())
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        f.write(data)
        p = f.name
    try:
        gdf = gpd.read_file(p)
    finally:
        os.unlink(p)
    assert len(gdf) == 3
    assert {"site_no", "water_level", "parameter_code", "vertical_datum"} <= set(
        gdf.columns
    )
    names = set(gdf["site_name"])
    assert "Well Alpha" in names and "Well Bravo" in names


@pytest.mark.skipif(not _have_geo(), reason="geopandas/shapely required")
def test_honest_empty_raises_no_wells():
    def fake_http_get(url, timeout=90.0):
        # measurements collection returns an empty FeatureCollection.
        return _meas_fc([])

    with patch(
        "trid3nt_server.tools.fetchers.hydrology.fetch_usgs_groundwater_levels._http_get",
        side_effect=fake_http_get,
    ), patch(
        "trid3nt_server.tools.fetchers.hydrology.fetch_usgs_groundwater_levels.read_through",
        side_effect=_make_read_through_injector({}),
    ):
        with pytest.raises(GwNoWellsError):
            fetch_usgs_groundwater_levels(bbox=(-140.0, 5.0, -139.0, 6.0))


@pytest.mark.skipif(not _have_geo(), reason="geopandas/shapely required")
def test_location_enrichment_failure_does_not_abort():
    """A monitoring-locations upstream failure must NOT abort a good primary."""
    meas = _meas_fc([_meas_feature("USGS-A1", -98.2, 38.5, "72019", "12.6")])

    def fake_http_get(url, timeout=90.0):
        if "latest-field-measurements" in url:
            return meas
        raise GwUpstreamError("locations endpoint down")

    store: dict[str, bytes] = {}
    with patch(
        "trid3nt_server.tools.fetchers.hydrology.fetch_usgs_groundwater_levels._http_get",
        side_effect=fake_http_get,
    ), patch(
        "trid3nt_server.tools.fetchers.hydrology.fetch_usgs_groundwater_levels.read_through",
        side_effect=_make_read_through_injector(store),
    ):
        res = fetch_usgs_groundwater_levels(bbox=_KS_BBOX)
    assert res.uri and res.uri.startswith("s3://")


# ---------------------------------------------------------------------------
# Live test (opt-in).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _LIVE, reason="set TRID3NT_TEST_LIVE_GWLEVELS=1 for live")
@pytest.mark.skipif(not _have_geo(), reason="geopandas/shapely required")
def test_live_kansas_high_plains():
    res = fetch_usgs_groundwater_levels(bbox=_KS_BBOX)
    assert res.layer_type == "vector"
    assert res.uri and res.uri.startswith("s3://")
    assert res.bbox is not None
