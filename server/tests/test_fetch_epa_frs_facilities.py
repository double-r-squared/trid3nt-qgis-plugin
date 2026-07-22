"""Unit tests for the ``fetch_epa_frs_facilities`` atomic tool.

Coverage:
- facility_program resolution (canonical + aliases + default) and unknown-program
  input error.
- bbox validation (degenerate / out-of-range / non-finite) -> typed input error.
- _build_query_url emits envelope + geojson/json + pagination params correctly
  for both the point layers and the polygon Superfund layer.
- estimate_payload_mb scales with bbox area and per-program density.
- _normalize_point_feature / _normalize_superfund_feature shape correctness on
  synthetic features (geometry kept, FRS attrs mapped, program injected,
  Superfund point derived from LATITUDE/LONGITUDE).
- _features_to_flatgeobuf compute/shape correctness on synthetic point features.
- Honest-empty path: zero features -> a valid (header-only) FlatGeobuf, no raise.
- Non-point / null-geom / non-finite features are dropped.
- Mocked end-to-end: synthetic features -> FGB via the read-through cache shim.
- Live (env TRID3NT_TEST_LIVE_EPA_FRS=1): real EPA query over a Houston bbox
  returns >=1 regulated facility point.
"""

from __future__ import annotations

import io
import os
from unittest.mock import patch

import pytest

# Import the module directly (the central tools/__init__ union is owned by the
# main session; this test does not depend on central registration).
from trid3nt_server.tools.fetchers.hazard.fetch_epa_frs_facilities import (
    EPA_NEPASSIST_BASE,
    FACILITY_PROGRAMS,
    FRS_UNION_PROGRAMS,
    EpaFrsInputError,
    _build_query_url,
    _features_to_flatgeobuf,
    _normalize_point_feature,
    _normalize_superfund_feature,
    _resolve_program,
    _round_bbox_to_6dp,
    _validate_bbox,
    estimate_payload_mb,
)

_LIVE = os.environ.get("TRID3NT_TEST_LIVE_EPA_FRS") == "1"

# Houston Ship Channel industrial bbox used across tests.
_HOUSTON = (-95.30, 29.68, -95.05, 29.80)


def _frs_point(lon: float, lat: float, name: str = "TEST CHEM CO") -> dict:
    """Synthetic GeoJSON FRS point feature in the upstream schema."""
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {
            "registry_id": "110000000001",
            "pgm_sys_acrnm": "TRIS",
            "pgm_sys_id": "77002TXSCL1300B",
            "primary_name": name,
            "location_address": "1 INDUSTRIAL WAY",
            "city_name": "HOUSTON",
            "county_name": "HARRIS",
            "state_code": "TX",
            "postal_code": "77002",
            "epa_region": "06",
            "facility_url": "https://example.epa.gov/frs/110000000001",
        },
    }


def _superfund_record(lon: float, lat: float, name: str = "TEST NPL SITE") -> dict:
    """Synthetic ESRI-JSON Superfund record (attributes only, geometry derived)."""
    return {
        "attributes": {
            "EPA_ID": "TX0000605329",
            "Site_Name": name,
            "Address": "200 BAYOU RD",
            "City": "DEER PARK",
            "County": "HARRIS",
            "State": "TX",
            "Zip_Code": "77536",
            "Region": "06",
            "NPL_Status": "Final NPL",
            "LATITUDE": lat,
            "LONGITUDE": lon,
            "FACILITY_URL": "https://example.epa.gov/superfund/TX0000605329",
        }
    }


# ---------------------------------------------------------------------------
# facility_program resolution.
# ---------------------------------------------------------------------------


def test_resolve_canonical_programs():
    for key in FACILITY_PROGRAMS:
        assert _resolve_program(key) == key
    assert _resolve_program("frs") == "frs"


def test_resolve_default_none_and_empty_default():
    # None defaults to the "frs" union.
    assert _resolve_program(None) == "frs"


@pytest.mark.parametrize(
    "alias,expected",
    [
        ("all", "frs"),
        ("facilities", "frs"),
        ("EPA", "frs"),
        ("toxic_release", "tri"),
        ("TRIS", "tri"),
        ("npl", "superfund"),
        ("cercla", "superfund"),
        ("npdes", "water"),
        ("rcra", "hazwaste"),
        ("air_emissions", "air"),
        ("Brownfield", "brownfields"),
        ("  Hazardous-Waste  ", "hazwaste"),
    ],
)
def test_resolve_aliases(alias, expected):
    assert _resolve_program(alias) == expected


@pytest.mark.parametrize("bad", ["airports", "", "   ", 123, 4.5, ["frs"]])
def test_resolve_unknown_raises_input_error(bad):
    with pytest.raises(EpaFrsInputError) as ei:
        _resolve_program(bad)
    assert ei.value.retryable is False
    assert ei.value.error_code == "EPA_FRS_INPUT_INVALID"


# ---------------------------------------------------------------------------
# bbox validation (input-validation test).
# ---------------------------------------------------------------------------


def test_validate_bbox_ok():
    _validate_bbox(_HOUSTON)  # no raise


@pytest.mark.parametrize(
    "bad",
    [
        (-95.0, 29.0, -95.0, 30.0),  # degenerate lon (min == max)
        (-95.0, 30.0, -94.0, 30.0),  # degenerate lat
        (-95.0, 30.0, -96.0, 31.0),  # min_lon > max_lon
        (-200.0, 29.0, -95.0, 30.0),  # lon out of range
        (-95.0, -95.0, -94.0, 30.0),  # lat out of range
        (-95.0, 29.0, float("nan"), 30.0),  # non-finite
        (-95.0, 29.0, -94.0),  # wrong length
        "not-a-bbox",  # wrong type
    ],
)
def test_validate_bbox_rejects(bad):
    with pytest.raises(EpaFrsInputError) as ei:
        _validate_bbox(bad)  # type: ignore[arg-type]
    assert ei.value.retryable is False


def test_round_bbox_to_6dp():
    out = _round_bbox_to_6dp((-95.123456789, 29.987654321, -94.0, 30.0))
    assert out == (-95.123457, 29.987654, -94.0, 30.0)


# ---------------------------------------------------------------------------
# URL building.
# ---------------------------------------------------------------------------


def test_build_query_url_point_layer():
    url, params = _build_query_url("tri", _HOUSTON, result_offset=0)
    assert url == f"{EPA_NEPASSIST_BASE}/15/query"
    assert params["geometryType"] == "esriGeometryEnvelope"
    assert params["inSR"] == "4326"
    assert params["f"] == "geojson"
    assert params["resultRecordCount"] == "2000"
    assert params["orderByFields"] == "OBJECTID ASC"
    assert params["geometry"] == "-95.3,29.68,-95.05,29.8"
    # point layers do not suppress geometry
    assert "returnGeometry" not in params


def test_build_query_url_superfund_polygon_layer():
    url, params = _build_query_url("superfund", _HOUSTON, result_offset=2000)
    assert url == f"{EPA_NEPASSIST_BASE}/14/query"
    # polygon layer fetches attributes only as ESRI json
    assert params["f"] == "json"
    assert params["returnGeometry"] == "false"
    assert params["resultOffset"] == "2000"


# ---------------------------------------------------------------------------
# Payload estimator.
# ---------------------------------------------------------------------------


def test_estimate_payload_scales_with_area():
    small = estimate_payload_mb("tri", (-95.1, 29.7, -95.0, 29.8))
    big = estimate_payload_mb("tri", (-96.0, 29.0, -95.0, 30.0))
    assert big > small
    assert small >= 0.02  # floor


def test_estimate_payload_frs_union_denser_than_single():
    bbox = (-96.0, 29.0, -95.0, 30.0)
    assert estimate_payload_mb("frs", bbox) >= estimate_payload_mb("brownfields", bbox)


def test_estimate_payload_handles_bad_inputs():
    assert estimate_payload_mb(None, None) >= 0.02
    assert estimate_payload_mb("tri", ("x", 1, 2, 3)) >= 0.02  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Feature normalization.
# ---------------------------------------------------------------------------


def test_normalize_point_feature_maps_schema():
    norm = _normalize_point_feature(_frs_point(-95.1, 29.75), "tri", "Toxic Release (TRI)")
    assert norm is not None
    props = norm["properties"]
    assert props["program"] == "tri"
    assert props["program_label"] == "Toxic Release (TRI)"
    assert props["program_acronym"] == "TRIS"
    assert props["program_id"] == "77002TXSCL1300B"
    assert props["facility_name"] == "TEST CHEM CO"
    assert props["city"] == "HOUSTON"
    assert props["state"] == "TX"
    assert norm["geometry"]["coordinates"] == [-95.1, 29.75]


@pytest.mark.parametrize(
    "feat",
    [
        {"geometry": None, "properties": {}},
        {"geometry": {"type": "Polygon", "coordinates": []}, "properties": {}},
        {"geometry": {"type": "Point", "coordinates": [float("nan"), 1.0]}, "properties": {}},
        {"geometry": {"type": "Point", "coordinates": [1.0]}, "properties": {}},
        "not-a-dict",
    ],
)
def test_normalize_point_feature_drops_bad_geom(feat):
    assert _normalize_point_feature(feat, "tri", "x") is None  # type: ignore[arg-type]


def test_normalize_superfund_derives_point_from_latlon():
    norm = _normalize_superfund_feature(_superfund_record(-95.114583, 29.731944), "Superfund (NPL)")
    assert norm is not None
    assert norm["geometry"]["coordinates"] == [-95.114583, 29.731944]
    props = norm["properties"]
    assert props["program"] == "superfund"
    assert props["npl_status"] == "Final NPL"
    assert props["registry_id"] == "TX0000605329"
    assert props["facility_name"] == "TEST NPL SITE"


@pytest.mark.parametrize(
    "attrs",
    [
        {"LATITUDE": None, "LONGITUDE": -95.0},
        {"LATITUDE": 29.0, "LONGITUDE": None},
        {"LATITUDE": "abc", "LONGITUDE": -95.0},
        {"LATITUDE": 29.0, "LONGITUDE": -999.0},  # lon out of range
        {"LATITUDE": float("inf"), "LONGITUDE": -95.0},
    ],
)
def test_normalize_superfund_drops_bad_latlon(attrs):
    assert _normalize_superfund_feature({"attributes": attrs}, "x") is None


# ---------------------------------------------------------------------------
# FlatGeobuf serialization (compute/shape correctness).
# ---------------------------------------------------------------------------


def _read_fgb(data: bytes):
    import geopandas as gpd

    return gpd.read_file(io.BytesIO(data))


def test_features_to_flatgeobuf_preserves_points_and_attrs():
    cleaned = [
        _normalize_point_feature(_frs_point(-95.1, 29.75, "A"), "tri", "Toxic Release (TRI)"),
        _normalize_point_feature(_frs_point(-95.2, 29.70, "B"), "air", "Air Emissions"),
        _normalize_superfund_feature(_superfund_record(-95.15, 29.72, "NPL1"), "Superfund (NPL)"),
    ]
    data = _features_to_flatgeobuf([c for c in cleaned if c])
    gdf = _read_fgb(data)
    assert len(gdf) == 3
    assert set(gdf["program"]) == {"tri", "air", "superfund"}
    assert set(gdf["facility_name"]) == {"A", "B", "NPL1"}
    assert all(gdf.geometry.geom_type == "Point")
    assert str(gdf.crs).upper().endswith("4326")


def test_features_to_flatgeobuf_honest_empty():
    """Zero features -> a valid (header-only) FlatGeobuf, no raise."""
    data = _features_to_flatgeobuf([])
    assert isinstance(data, bytes) and len(data) > 0
    gdf = _read_fgb(data)
    assert len(gdf) == 0


# ---------------------------------------------------------------------------
# Mocked end-to-end through the read-through cache shim.
# ---------------------------------------------------------------------------


def test_end_to_end_mocked(tmp_path, monkeypatch):
    """Full fetch_epa_frs_facilities call with the network + S3 mocked out.

    Patches the per-layer fetcher to return synthetic features and the cache
    shim to a local-disk store, then asserts the LayerURI shape + FGB content.
    """
    from trid3nt_server.tools.fetchers.hazard import fetch_epa_frs_facilities as mod

    def fake_fetch_layer(program_key, bbox, **_kw):
        if program_key == "superfund":
            return [_superfund_record(-95.15, 29.72, "NPL1")]
        return [_frs_point(-95.10, 29.75, f"{program_key.upper()} CO")]

    # read_through writes to S3 in prod; here we short-circuit it to a local file.
    captured = {}

    def fake_read_through(metadata, params, ext, fetch_fn, **_kw):
        data = fetch_fn()
        out = tmp_path / "out.fgb"
        out.write_bytes(data)
        uri = f"s3://fake-bucket/{out.name}"
        captured["data"] = data

        class _R:
            pass

        r = _R()
        r.uri = uri
        r.data = data
        r.hit = False
        return r

    monkeypatch.setattr(mod, "_fetch_layer_paginated", fake_fetch_layer)
    monkeypatch.setattr(mod, "read_through", fake_read_through)

    lu = mod.fetch_epa_frs_facilities(bbox=_HOUSTON, facility_program="frs")
    assert lu.layer_type == "vector"
    assert lu.style_preset == "epa_frs_facilities"
    assert lu.role == "primary"
    assert lu.uri.startswith("s3://")
    assert lu.bbox == _round_bbox_to_6dp(_HOUSTON)

    gdf = _read_fgb(captured["data"])
    # frs union = 5 point programs, one synthetic feature each
    assert len(gdf) == len(FRS_UNION_PROGRAMS)
    assert set(gdf["program"]) == set(FRS_UNION_PROGRAMS)


def test_end_to_end_mocked_single_program(tmp_path, monkeypatch):
    from trid3nt_server.tools.fetchers.hazard import fetch_epa_frs_facilities as mod

    def fake_fetch_layer(program_key, bbox, **_kw):
        return [_superfund_record(-95.15, 29.72, "NPL1")]

    def fake_read_through(metadata, params, ext, fetch_fn, **_kw):
        data = fetch_fn()
        out = tmp_path / "sf.fgb"
        out.write_bytes(data)

        class _R:
            pass

        r = _R()
        r.uri = f"s3://fake-bucket/{out.name}"
        r.data = data
        r.hit = False
        return r

    monkeypatch.setattr(mod, "_fetch_layer_paginated", fake_fetch_layer)
    monkeypatch.setattr(mod, "read_through", fake_read_through)

    lu = mod.fetch_epa_frs_facilities(bbox=_HOUSTON, facility_program="superfund")
    assert "superfund" in lu.layer_id
    gdf = _read_fgb(lu.data) if hasattr(lu, "data") else None  # noqa: F841


def test_input_error_propagates_before_fetch():
    """An invalid program raises before any network call."""
    with pytest.raises(EpaFrsInputError):
        from trid3nt_server.tools.fetchers.hazard import fetch_epa_frs_facilities as mod

        mod.fetch_epa_frs_facilities(bbox=_HOUSTON, facility_program="not-a-program")


# ---------------------------------------------------------------------------
# Live integration (opt-in).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _LIVE, reason="set TRID3NT_TEST_LIVE_EPA_FRS=1 to run live")
def test_live_houston_returns_facilities():
    from trid3nt_server.tools.fetchers.hazard.fetch_epa_frs_facilities import _fetch_frs_bytes

    data = _fetch_frs_bytes("tri", _HOUSTON)
    gdf = _read_fgb(data)
    assert len(gdf) >= 1
    assert set(gdf["program"]) <= {"tri"}
