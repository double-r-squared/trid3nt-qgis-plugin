"""Unit tests for the ``fetch_hifld_transmission_lines`` atomic tool.

Coverage:
- bbox validation (degenerate / out-of-range / non-finite / wrong arity) -> typed
  input error.
- min_voltage_kv validation (None / numeric / negative / non-numeric / bool).
- _build_query_url emits envelope + geojson + pagination params correctly, and a
  VOLTAGE >= where clause when a voltage floor is supplied.
- estimate_payload_mb scales with bbox area; advisory bad-input never raises.
- _features_to_flatgeobuf compute/shape correctness on synthetic polyline
  features (LineString + MultiLineString kept, HIFLD attrs preserved,
  infra_type/infra_label injected).
- Honest-empty path: zero features -> a valid (header-only) FlatGeobuf, no raise.
- Non-line / null-geom features are dropped.
- Mocked end-to-end: synthetic GeoJSON -> FGB via the fetch+serialize path.
- Typed upstream errors: HTTP >=400 and ArcGIS error envelope.
- Live (env TRID3NT_TEST_LIVE_HIFLD=1): real ArcGIS query over a Houston bbox
  returns >=1 transmission line segment with VOLTAGE.
"""

from __future__ import annotations

import io
import os
from unittest.mock import patch

import pytest

# Import the module directly (the central tools/__init__ union is owned by the
# main session; this test does not depend on central registration).
from trid3nt_server.tools.fetch_hifld_transmission_lines import (
    HIFLDTransmissionInputError,
    HIFLDTransmissionUpstreamError,
    INFRA_LABEL,
    INFRA_TYPE,
    _build_query_url,
    _features_to_flatgeobuf,
    _round_bbox_to_6dp,
    _validate_bbox,
    _validate_min_voltage,
    estimate_payload_mb,
)

_LIVE = os.environ.get("TRID3NT_TEST_LIVE_HIFLD") == "1"

# Houston metro bbox used across tests.
_HOUSTON = (-95.80, 29.50, -95.00, 30.10)


def _line_feature(
    coords: list[list[float]],
    *,
    voltage: float = 138,
    owner: str = "CENTERPOINT ENERGY",
    multi: bool = False,
) -> dict:
    geom = (
        {"type": "MultiLineString", "coordinates": [coords]}
        if multi
        else {"type": "LineString", "coordinates": coords}
    )
    return {
        "type": "Feature",
        "geometry": geom,
        "properties": {
            "ID": "300101",
            "TYPE": "AC; OVERHEAD",
            "STATUS": "NOT AVAILABLE",
            "OWNER": owner,
            "VOLTAGE": voltage,
            "VOLT_CLASS": "100-161",
            "SUB_1": "TAP1",
            "SUB_2": "TAP2",
        },
    }


# ---------------------------------------------------------------------------
# bbox validation (input-validation test).
# ---------------------------------------------------------------------------


def test_validate_bbox_accepts_good():
    _validate_bbox(_HOUSTON)  # no raise


@pytest.mark.parametrize(
    "bad",
    [
        (-95.0, 29.5, -95.8, 30.1),  # min_lon >= max_lon (degenerate)
        (-95.8, 30.1, -95.0, 29.5),  # min_lat >= max_lat
        (-200.0, 29.5, -95.0, 30.1),  # lon out of range
        (-95.8, 29.5, -95.0, 100.0),  # lat out of range
        (-95.8, 29.5, -95.0),  # wrong arity
        (-95.8, float("nan"), -95.0, 30.1),  # non-finite
    ],
)
def test_validate_bbox_rejects_bad(bad):
    with pytest.raises(HIFLDTransmissionInputError) as ei:
        _validate_bbox(bad)
    assert ei.value.retryable is False
    assert ei.value.error_code == "HIFLD_TRANSMISSION_INPUT_INVALID"


def test_round_bbox_to_6dp():
    assert _round_bbox_to_6dp((-95.8000001, 29.50000009, -95.0, 30.1)) == (
        -95.8, 29.5, -95.0, 30.1,
    )


# ---------------------------------------------------------------------------
# min_voltage_kv validation.
# ---------------------------------------------------------------------------


def test_validate_min_voltage_none_returns_none():
    assert _validate_min_voltage(None) is None


@pytest.mark.parametrize("v,expected", [(0, 0.0), (230, 230.0), (345.0, 345.0)])
def test_validate_min_voltage_numeric(v, expected):
    assert _validate_min_voltage(v) == expected


@pytest.mark.parametrize("bad", [-1, "230", float("nan"), float("inf"), True, [230]])
def test_validate_min_voltage_rejects_bad(bad):
    with pytest.raises(HIFLDTransmissionInputError) as ei:
        _validate_min_voltage(bad)
    assert ei.value.retryable is False


# ---------------------------------------------------------------------------
# URL building.
# ---------------------------------------------------------------------------


def test_build_query_url_params():
    url, params = _build_query_url(_HOUSTON, result_offset=2000)
    assert url.endswith("/Electric_Power_Transmission_Lines/FeatureServer/0/query")
    assert params["where"] == "1=1"
    assert params["geometryType"] == "esriGeometryEnvelope"
    assert params["inSR"] == "4326"
    assert params["outSR"] == "4326"
    assert params["f"] == "geojson"
    assert params["spatialRel"] == "esriSpatialRelIntersects"
    assert params["geometry"] == "-95.8,29.5,-95.0,30.1"
    assert params["resultOffset"] == "2000"
    assert params["resultRecordCount"] == "2000"
    assert params["orderByFields"] == "OBJECTID ASC"


def test_build_query_url_voltage_floor_where_clause():
    _url, params = _build_query_url(_HOUSTON, min_voltage_kv=230.0)
    assert params["where"] == "VOLTAGE >= 230"


# ---------------------------------------------------------------------------
# Payload estimator.
# ---------------------------------------------------------------------------


def test_estimate_scales_with_area():
    small = estimate_payload_mb((-95.5, 29.7, -95.4, 29.8))
    big = estimate_payload_mb((-96.0, 29.0, -94.0, 31.0))
    assert big > small
    assert small >= 0.02


def test_estimate_handles_bad_bbox_gracefully():
    # Advisory only; must not raise.
    val = estimate_payload_mb("not-a-bbox")
    assert val >= 0.02


# ---------------------------------------------------------------------------
# Features -> FlatGeobuf (compute/shape correctness).
# ---------------------------------------------------------------------------


def test_features_to_fgb_shape_and_injected_columns():
    import geopandas as gpd

    feats = [
        _line_feature([[-95.3, 29.8], [-95.31, 29.81]], voltage=138),
        _line_feature([[-95.4, 29.9], [-95.41, 29.91]], voltage=345, multi=True),
    ]
    fgb = _features_to_flatgeobuf(feats)
    assert isinstance(fgb, bytes) and len(fgb) > 0

    gdf = gpd.read_file(io.BytesIO(fgb))
    assert len(gdf) == 2
    assert set(gdf.geom_type.unique()) <= {"LineString", "MultiLineString"}
    # HIFLD source attrs preserved.
    assert "VOLTAGE" in gdf.columns and "OWNER" in gdf.columns
    assert set(gdf["VOLTAGE"]) == {138, 345}
    # Injected self-describing columns.
    assert (gdf["infra_type"] == INFRA_TYPE).all()
    assert (gdf["infra_label"] == INFRA_LABEL).all()


def test_features_to_fgb_drops_non_line_and_null_geom():
    import geopandas as gpd

    feats = [
        _line_feature([[-95.3, 29.8], [-95.31, 29.81]]),
        {"type": "Feature", "geometry": None, "properties": {"ID": "NULLGEOM"}},
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [-95.3, 29.8]},
            "properties": {"ID": "POINT"},
        },
        {
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]],
            },
            "properties": {"ID": "POLY"},
        },
    ]
    fgb = _features_to_flatgeobuf(feats)
    gdf = gpd.read_file(io.BytesIO(fgb))
    assert len(gdf) == 1
    assert gdf.iloc[0]["ID"] == "300101"
    assert gdf.iloc[0]["infra_label"] == INFRA_LABEL


def test_features_to_fgb_honest_empty_path():
    """Zero features -> a valid (header-only) FlatGeobuf, not an error."""
    import geopandas as gpd

    fgb = _features_to_flatgeobuf([])
    assert isinstance(fgb, bytes) and len(fgb) > 0
    gdf = gpd.read_file(io.BytesIO(fgb))
    assert len(gdf) == 0


# ---------------------------------------------------------------------------
# Mocked end-to-end through the fetch+serialize path.
# ---------------------------------------------------------------------------


def test_end_to_end_mocked_fetch_and_serialize():
    """Synthetic GeoJSON -> tool fetcher -> FGB; pagination terminates on short page."""
    import geopandas as gpd

    from trid3nt_server.tools import fetch_hifld_transmission_lines as mod

    fake_geojson = {
        "type": "FeatureCollection",
        "features": [
            _line_feature([[-95.3, 29.8], [-95.31, 29.81]], voltage=138),
            _line_feature([[-95.45, 29.7], [-95.46, 29.71]], voltage=345),
        ],
    }

    class _Resp:
        status_code = 200

        def json(self):
            return fake_geojson

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None, headers=None):
            # Short page (2 < page size) so pagination terminates immediately.
            return _Resp()

    with patch.object(mod.httpx, "Client", _Client):
        feats = mod._fetch_features_paginated(_HOUSTON)
        assert len(feats) == 2
        fgb = mod._features_to_flatgeobuf(feats)

    gdf = gpd.read_file(io.BytesIO(fgb))
    assert len(gdf) == 2
    assert set(gdf["VOLTAGE"]) == {138, 345}


def test_upstream_http_error_is_typed():
    from trid3nt_server.tools import fetch_hifld_transmission_lines as mod

    class _Resp:
        status_code = 500
        text = "boom"

        def json(self):
            return {}

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None, headers=None):
            return _Resp()

    with patch.object(mod.httpx, "Client", _Client):
        with pytest.raises(HIFLDTransmissionUpstreamError) as ei:
            mod._fetch_features_paginated(_HOUSTON)
    assert ei.value.retryable is True


def test_error_envelope_is_typed():
    from trid3nt_server.tools import fetch_hifld_transmission_lines as mod

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"error": {"code": 400, "message": "Invalid query"}}

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None, headers=None):
            return _Resp()

    with patch.object(mod.httpx, "Client", _Client):
        with pytest.raises(HIFLDTransmissionUpstreamError):
            mod._fetch_features_paginated(_HOUSTON)


# ---------------------------------------------------------------------------
# Live integration (opt-in).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _LIVE, reason="set TRID3NT_TEST_LIVE_HIFLD=1 to run live")
def test_live_houston_transmission_returns_lines():
    from trid3nt_server.tools.fetch_hifld_transmission_lines import (
        _fetch_features_paginated,
    )

    feats = _fetch_features_paginated(_HOUSTON)
    assert len(feats) >= 1
    g = feats[0]["geometry"]
    assert g["type"] in ("LineString", "MultiLineString")
    # Voltage present on the HIFLD schema.
    assert "VOLTAGE" in feats[0]["properties"]


@pytest.mark.skipif(not _LIVE, reason="set TRID3NT_TEST_LIVE_HIFLD=1 to run live")
def test_live_voltage_floor_filters():
    from trid3nt_server.tools.fetch_hifld_transmission_lines import (
        _fetch_features_paginated,
    )

    hv = _fetch_features_paginated(_HOUSTON, min_voltage_kv=300.0)
    # Houston has 345 kV backbone; every returned segment must clear the floor.
    for f in hv:
        v = f["properties"].get("VOLTAGE")
        assert v is None or v >= 300.0
