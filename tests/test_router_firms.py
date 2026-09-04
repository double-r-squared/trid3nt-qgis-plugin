"""Router value coverage for the fetch_firms_active_fire fold (ADR 0079).

The NASA FIRMS active-fire twin folded to a source.yaml + firms_active_fire hooks
(keyed CSV http_json with the MAP_KEY carried IN the URL path). These tests cover
the value-bearing surface the deleted twin's tests carried: the credential-resolution
missing-key parity, the AREA-endpoint URL build (rolling + historical-date), the
200-with-error-body auth split (parse_response) + the non-2xx body split
(classify_status), the CSV -> Point parse with the retained schema, the honest 0-feature
FGB, and the spec-identity flags pinned against the twin's registration.
"""

from __future__ import annotations

import os
import tempfile

import geopandas as gpd
import pytest

from trid3nt_server.tools.fetchers._router.executors.vector_fgb import (
    features_to_fgb_bytes,
)
from trid3nt_server.tools.fetchers._router.hooks import firms_active_fire as fh
from trid3nt_server.tools.fetchers._router.spec import compose_specs_from_tree

_HEADER = (
    "latitude,longitude,brightness,scan,track,acq_date,acq_time,"
    "satellite,instrument,confidence,version,bright_t31,frp,daynight"
)
_RETAINED = [
    "brightness", "scan", "track", "acq_date", "acq_time", "satellite",
    "instrument", "confidence", "version", "bright_t31", "frp", "daynight",
]


def _synth_csv(n: int = 5, lat0: float = 37.5, lon0: float = -120.5) -> bytes:
    rows = [_HEADER]
    for i in range(n):
        rows.append(
            f"{lat0 + i * 0.05:.4f},{lon0 + i * 0.05:.4f},"
            f"{320.5 + i:.1f},0.42,0.41,2026-06-07,1230,"
            f"N,VIIRS,nominal,2.0NRT,300.1,{15.5 + i * 0.5:.1f},D"
        )
    return ("\n".join(rows) + "\n").encode()


@pytest.fixture(scope="module")
def spec():
    return compose_specs_from_tree()["fetch_firms_active_fire"]


def _read(fgb: bytes):
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        f.write(fgb)
        p = f.name
    try:
        return gpd.read_file(p, engine="pyogrio")
    finally:
        os.unlink(p)


def test_spec_identity(spec):
    """SPEC-IDENTITY: the flags pinned against the twin's AtomicToolMetadata."""
    assert spec.name == "fetch_firms_active_fire"
    assert spec.shape == "vector-fgb"
    assert spec.error_code_prefix == "FIRMS"
    assert spec.supports_global_query is False
    assert spec.cache.ttl_class == "dynamic-1h"          # twin ttl
    assert spec.source_class == "firms_active_fire"       # twin cache prefix
    assert spec.output.style["kind"] == "reference"
    assert spec.output.role == "primary"


def test_missing_key_is_input_error_pre_network(spec, monkeypatch):
    monkeypatch.delenv("TRID3NT_FIRMS_MAP_KEY", raising=False)
    with pytest.raises(Exception) as ei:
        fh.build_request(spec, {"bbox": [-122, 38, -119, 40], "source": "VIIRS_SNPP_NRT", "days_back": 1})
    assert getattr(ei.value, "error_code", "") == "FIRMS_MISSING_KEY"
    assert getattr(ei.value, "retryable", None) is False


def test_key_kwarg_wins_over_env(spec, monkeypatch):
    monkeypatch.setenv("TRID3NT_FIRMS_MAP_KEY", "ENVKEY")
    plans = fh.build_request(
        spec, {"bbox": [-122.0, 38.0, -119.0, 40.0], "source": "VIIRS_SNPP_NRT", "days_back": 7, "map_key": "KW"}
    )
    assert len(plans) == 1
    assert plans[0].url == (
        "https://firms.modaps.eosdis.nasa.gov/api/area/csv/KW/VIIRS_SNPP_NRT/"
        "-122.0,38.0,-119.0,40.0/7"
    )


def test_env_key_and_rolling_url(spec, monkeypatch):
    monkeypatch.setenv("TRID3NT_FIRMS_MAP_KEY", "ENVKEY")
    plans = fh.build_request(
        spec, {"bbox": [-122.0, 38.0, -119.0, 40.0], "source": "MODIS_NRT", "days_back": 3}
    )
    assert plans[0].url.endswith("/ENVKEY/MODIS_NRT/-122.0,38.0,-119.0,40.0/3")


def test_historical_date_forces_day_one(spec):
    plans = fh.build_request(
        spec,
        {"bbox": [-113.346, 39.57, -111.765, 41.115], "source": "VIIRS_NOAA20_NRT",
         "days_back": 5, "date": "2026-06-22", "map_key": "K"},
    )
    # date forces the day-range to 1 and appends the trailing /<date> segment.
    assert plans[0].url.endswith(
        "/K/VIIRS_NOAA20_NRT/-113.346,39.57,-111.765,41.115/1/2026-06-22"
    )


def test_parse_csv_to_points_retained_schema(spec):
    feats = fh.parse_response(spec, {"bbox": [-124, 32, -114, 42]}, [_synth_csv(5)])
    assert len(feats) == 5
    assert feats[0]["geometry"] == {"type": "Point", "coordinates": [-120.5, 37.5]}
    gdf = _read(features_to_fgb_bytes(feats, spec, {}))
    assert len(gdf) == 5
    assert gdf.crs.to_epsg() == 4326
    for col in ("brightness", "frp", "confidence", "acq_date"):
        assert col in gdf.columns


def test_empty_header_only_is_zero_feature_fgb(spec):
    feats = fh.parse_response(spec, {}, [(_HEADER + "\n").encode()])
    assert feats == []
    # declared ingest.properties -> the honest-empty FGB carries the retained schema.
    gdf = _read(features_to_fgb_bytes(feats, spec, {}))
    assert len(gdf) == 0
    assert sorted(c for c in gdf.columns if c != "geometry") == sorted(_RETAINED)


def test_geography_correctness_california(spec):
    feats = fh.parse_response(spec, {}, [_synth_csv(5)])
    gdf = _read(features_to_fgb_bytes(feats, spec, {}))
    for geom in gdf.geometry:
        assert -124.5 <= geom.x <= -114.1
        assert 32.5 <= geom.y <= 42.0


@pytest.mark.parametrize("body", [b"Invalid MAP_KEY.", b"You have exceeded your transaction limit."])
def test_auth_body_200_raises_auth_error(spec, body):
    with pytest.raises(Exception) as ei:
        fh.parse_response(spec, {}, [body])
    assert getattr(ei.value, "error_code", "") == "FIRMS_AUTH_ERROR"


def test_blank_body_raises_upstream(spec):
    with pytest.raises(Exception) as ei:
        fh.parse_response(spec, {}, [b"   \n  "])
    assert getattr(ei.value, "error_code", "") == "FIRMS_UPSTREAM_ERROR"


def test_missing_columns_raises_upstream(spec):
    with pytest.raises(Exception) as ei:
        fh.parse_response(spec, {}, [b"foo,bar\n1,2\n"])
    assert getattr(ei.value, "error_code", "") == "FIRMS_UPSTREAM_ERROR"


@pytest.mark.parametrize("status,body,code", [
    (400, "Invalid MAP_KEY.\nInvalid day range.", "FIRMS_AUTH_ERROR"),
    (500, "server error", None),
    (404, "not found", None),
])
def test_classify_status_body_split(spec, status, body, code):
    err = fh.classify_status(spec, status, body)
    assert getattr(err, "error_code", None) == code
