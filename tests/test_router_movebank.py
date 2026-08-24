"""Router value coverage for the fetch_movebank_tracks fold (ADR 0077).

The Movebank direct-read twin folded to a source.yaml + movebank_tracks hooks
(keyed CSV http_json with composite Basic-Auth via the resolver blob path). These
tests cover the value-bearing surface the deleted twin's tests carried: the
credential-resolution missing-key parity, the Basic-Auth header build, the CSV
parse + per-geometry_type feature shaping + the conservative linestring bbox clip,
the empty-schema-by-geometry_type header, and the classify_status split.
"""

from __future__ import annotations

import base64
import os
import tempfile

import geopandas as gpd
import pytest

from trid3nt_server.tools.fetchers._router.executors.vector_fgb import (
    features_to_fgb_bytes,
)
from trid3nt_server.tools.fetchers._router.hooks import movebank_tracks as mbh
from trid3nt_server.tools.fetchers._router.spec import compose_specs_from_tree

_CSV = (
    "individual_local_identifier,timestamp,location_lat,location_long,sensor_type_id\n"
    "A,2020-01-01 00:00:00.000,37.5,-122.4,653\n"
    "A,2020-01-01 01:00:00.000,37.6,-122.3,653\n"
    "A,2020-01-01 02:00:00.000,37.55,-122.35,653\n"
    "B,2020-01-02 00:00:00.000,37.7,-122.2,653\n"
    "B,2020-01-02 01:00:00.000,45.0,-100.0,653\n"
    "C,2020-01-03 00:00:00.000,37.52,-122.38,653\n"
)
_BBOX = [-122.5, 37.4, -122.1, 37.8]


@pytest.fixture(scope="module")
def spec():
    return compose_specs_from_tree()["fetch_movebank_tracks"]


def _read(fgb: bytes):
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        f.write(fgb)
        p = f.name
    try:
        return gpd.read_file(p)
    finally:
        os.unlink(p)


def test_spec_identity(spec):
    assert spec.name == "fetch_movebank_tracks"
    assert spec.shape == "vector-fgb"
    assert spec.error_code_prefix == "MOVEBANK"
    assert spec.supports_global_query is False


def test_missing_credentials_is_input_error_pre_network(spec):
    with pytest.raises(Exception) as ei:
        mbh.build_request(spec, {"study_id": 999})
    assert getattr(ei.value, "error_code", "") == "MOVEBANK_INPUT_ERROR"


def test_credentials_kwargs_build_basic_auth_header(spec):
    plans = mbh.build_request(
        spec, {"study_id": 42, "username": "u", "password": "p"}
    )
    assert len(plans) == 1
    auth = plans[0].headers["Authorization"]
    expected = base64.b64encode(b"u:p").decode()
    assert auth == f"Basic {expected}"
    assert plans[0].params["entity_type"] == "event"
    assert plans[0].params["study_id"] == 42


def test_secret_ref_blob_resolves_user_pass(spec):
    plans = mbh.build_request(spec, {"study_id": 42, "secret_ref": "user:pw"})
    assert plans[0].headers["Authorization"] == "Basic " + base64.b64encode(
        b"user:pw"
    ).decode()


def test_env_credentials_fallback(spec, monkeypatch):
    monkeypatch.setenv("TRID3NT_MOVEBANK_USER", "envu")
    monkeypatch.setenv("TRID3NT_MOVEBANK_PASSWORD", "envp")
    plans = mbh.build_request(spec, {"study_id": 7})
    assert plans[0].headers["Authorization"] == "Basic " + base64.b64encode(
        b"envu:envp"
    ).decode()


def test_time_range_and_sensor_query(spec):
    plans = mbh.build_request(
        spec,
        {
            "study_id": 7,
            "username": "u",
            "password": "p",
            "sensor_type_id": 653,
            "time_range": ["2020-01-01T00:00:00", "2020-01-02T00:00:00"],
        },
    )
    q = plans[0].params
    assert q["sensor_type_id"] == 653
    assert q["timestamp_start"] == "20200101000000000"
    assert q["timestamp_end"] == "20200102000000000"


def test_point_parse_bbox_clip(spec):
    params = {"study_id": 999, "bbox": _BBOX, "geometry_type": "point", "max_records": 500000}
    feats = mbh.parse_response(spec, params, [_CSV.encode()])
    gdf = _read(features_to_fgb_bytes(feats, spec, params))
    # B's 2nd fix (45,-100) is outside the bbox and dropped; 5 in-bbox fixes remain.
    assert len(gdf) == 5
    assert sorted(c for c in gdf.columns if c != "geometry") == [
        "individual_id", "sensor_type_id", "study_id", "timestamp",
    ]


def test_linestring_conservative_clip(spec):
    params = {"study_id": 999, "bbox": _BBOX, "geometry_type": "linestring", "max_records": 500000}
    feats = mbh.parse_response(spec, params, [_CSV.encode()])
    gdf = _read(features_to_fgb_bytes(feats, spec, params))
    # A: 3 in-bbox vertices -> one LineString. B: partly outside -> dropped.
    # C: single vertex -> not a line -> dropped.
    assert len(gdf) == 1
    row = gdf.iloc[0]
    assert row["individual_id"] == "A"
    assert row["n_points"] == 3
    assert row["first_timestamp"] == "2020-01-01 00:00:00.000"
    assert row["last_timestamp"] == "2020-01-01 02:00:00.000"


@pytest.mark.parametrize("gtype,cols", [
    ("linestring", ["individual_id", "n_points", "first_timestamp", "last_timestamp", "study_id"]),
    ("point", ["individual_id", "timestamp", "sensor_type_id", "study_id"]),
])
def test_empty_header_schema_by_geometry_type(spec, gtype, cols):
    params = {"study_id": 1, "bbox": [-1, -1, 1, 1], "geometry_type": gtype, "max_records": 9}
    header = b"individual_local_identifier,timestamp,location_lat,location_long,sensor_type_id\n"
    feats = mbh.parse_response(spec, params, [header])
    gdf = _read(features_to_fgb_bytes(feats, spec, params))
    assert len(gdf) == 0
    assert sorted(c for c in gdf.columns if c != "geometry") == sorted(cols)


def test_html_licence_body_raises_license_error(spec):
    with pytest.raises(Exception) as ei:
        mbh.parse_response(
            spec, {"study_id": 1, "geometry_type": "point", "max_records": 9},
            [b"<html>License Terms</html>"],
        )
    assert getattr(ei.value, "error_code", "") == "MOVEBANK_LICENSE_ERROR"


@pytest.mark.parametrize("status,code", [
    (401, "MOVEBANK_AUTH_ERROR"),
    (403, "MOVEBANK_LICENSE_ERROR"),
    (404, "MOVEBANK_INPUT_ERROR"),
])
def test_classify_status_split(spec, status, code):
    err = mbh.classify_status(spec, status, "body")
    assert getattr(err, "error_code", "") == code


def test_classify_status_5xx_falls_through(spec):
    assert mbh.classify_status(spec, 500, "body") is None
