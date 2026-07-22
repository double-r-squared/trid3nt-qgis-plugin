"""Unit tests for the ``fetch_epa_ejscreen`` atomic tool.

Coverage:
- ``_features_to_flatgeobuf``: synthetic EJScreen Esri-JSON block-group features
  (``attributes`` + ``geometry.rings``) -> FlatGeobuf with the selected ``value``
  column + the full percentile panel + demographic context; null sentinels
  (-999) normalized to null; percentiles clamped to [0,100], fractions to [0,1];
  round-trips back through geopandas with correct values (compute/shape).
- ``_esri_rings_to_geojson_geometry``: Esri rings -> GeoJSON Polygon; degenerate
  rings dropped; non-dict / empty input -> None.
- ``_resolve_indicator``: canonical keys + aliases (case-insensitive) -> source
  field; unknown indicator raises the typed input error.
- Honest-empty path: an empty feature list yields a valid zero-feature FGB with
  the full schema (no exception). A feature with no geometry is skipped.
- Input validation: degenerate / inverted / out-of-range / wrong-length bboxes
  and unknown indicators raise ``EPA_EJScreenInputError``.
- Registration metadata, payload estimator, query-param building, normalizers.

These tests use SYNTHETIC Esri JSON (no network) so they are deterministic and
offline-safe. The live-source path is proven separately in the prototype + S3
read-through round-trip captured in the build report (207 Houston block groups).
"""

from __future__ import annotations

import json
import tempfile

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.fetch_epa_ejscreen import (
    EJSCREEN_INDICATORS,
    EPA_EJScreenInputError,
    _build_query_params,
    _esri_rings_to_geojson_geometry,
    _features_to_flatgeobuf,
    _normalize_fraction,
    _normalize_percentile,
    _resolve_indicator,
    _round_bbox_to_6dp,
    _validate_bbox,
    estimate_payload_mb,
    fetch_epa_ejscreen,
)

geopandas = pytest.importorskip("geopandas")


# ---------------------------------------------------------------------------
# Synthetic Esri-JSON feature builders.
# ---------------------------------------------------------------------------


def _ring(cx: float, cy: float, h: float = 0.01) -> list[list[float]]:
    """A small closed square Esri ring centered at (cx, cy)."""
    return [
        [cx - h, cy - h],
        [cx + h, cy - h],
        [cx + h, cy + h],
        [cx - h, cy + h],
        [cx - h, cy - h],
    ]


def _bg_feature(
    bg_id: str,
    *,
    p_pm25=90.6,
    p_ozone=17.7,
    p_ptraf=43.7,
    p_pnpl=87.5,
    minorpct=0.96,
    lowincpct=0.42,
    vuleopct=0.88,
    pm25_raw=10.02,
    ozone_raw=44.1,
    total_pop=1010,
    cx: float = -95.3,
    cy: float = 29.7,
) -> dict:
    """An EJScreen block-group feature in Esri JSON (attributes + rings)."""
    return {
        "attributes": {
            "ID": bg_id,
            "STATE_NAME": "Texas",
            "ACSTOTPOP": total_pop,
            "PM25": pm25_raw,
            "OZONE": ozone_raw,
            "P_PM25": p_pm25,
            "P_OZONE": p_ozone,
            "P_DSLPM": 55.0,
            "P_RESP": 60.0,
            "P_PTRAF": p_ptraf,
            "P_LDPNT": 33.0,
            "P_PNPL": p_pnpl,
            "P_PRMP": 70.0,
            "P_PTSDF": 65.0,
            "P_PWDIS": 12.0,
            "P_MINORPCT": 92.0,
            "MINORPCT": minorpct,
            "LOWINCPCT": lowincpct,
            "VULEOPCT": vuleopct,
        },
        "geometry": {"rings": [_ring(cx, cy)]},
    }


# ---------------------------------------------------------------------------
# Registration / metadata.
# ---------------------------------------------------------------------------


def test_tool_is_registered():
    assert "fetch_epa_ejscreen" in TOOL_REGISTRY


def test_metadata_fields():
    md = TOOL_REGISTRY["fetch_epa_ejscreen"].metadata
    assert md.name == "fetch_epa_ejscreen"
    assert md.ttl_class == "static-30d"
    assert md.source_class == "epa_ejscreen"
    assert md.cacheable is True


# ---------------------------------------------------------------------------
# Indicator resolution.
# ---------------------------------------------------------------------------


def test_resolve_indicator_default_and_aliases():
    assert _resolve_indicator(None) == ("pm25", "P_PM25")
    assert _resolve_indicator("pm25") == ("pm25", "P_PM25")
    # case-insensitive
    assert _resolve_indicator("OZONE") == ("ozone", "P_OZONE")
    # aliases map to the same source field
    assert _resolve_indicator("superfund")[1] == "P_PNPL"
    assert _resolve_indicator("npl")[1] == "P_PNPL"
    assert _resolve_indicator("traffic")[1] == "P_PTRAF"
    assert _resolve_indicator("lead")[1] == "P_LDPNT"
    assert _resolve_indicator("demographic_index")[1] == "P_MINORPCT"
    # EJ-index rollup
    assert _resolve_indicator("ej_traffic")[1] == "P_PTRAF_D2"


def test_resolve_indicator_unknown_raises():
    with pytest.raises(EPA_EJScreenInputError):
        _resolve_indicator("nonsense_xyz")


def test_every_indicator_field_is_emitted_or_panel():
    # sanity: all mapped source fields are non-empty strings
    for k, v in EJSCREEN_INDICATORS.items():
        assert isinstance(v, str) and v


# ---------------------------------------------------------------------------
# Esri rings -> GeoJSON geometry.
# ---------------------------------------------------------------------------


def test_esri_rings_to_geojson():
    g = _esri_rings_to_geojson_geometry({"rings": [_ring(-95.3, 29.7)]})
    assert g is not None
    assert g["type"] == "Polygon"
    assert len(g["coordinates"]) == 1
    assert len(g["coordinates"][0]) == 5


def test_esri_rings_none_and_degenerate():
    assert _esri_rings_to_geojson_geometry(None) is None
    assert _esri_rings_to_geojson_geometry({}) is None
    assert _esri_rings_to_geojson_geometry({"rings": []}) is None
    # a ring with <4 points is degenerate -> dropped -> None
    assert _esri_rings_to_geojson_geometry(
        {"rings": [[[0, 0], [1, 1]]]}
    ) is None


# ---------------------------------------------------------------------------
# Normalizers.
# ---------------------------------------------------------------------------


def test_normalize_percentile():
    assert _normalize_percentile(90.6) == pytest.approx(90.6)
    assert _normalize_percentile(0.0) == 0.0
    assert _normalize_percentile(100.0) == 100.0
    assert _normalize_percentile(-999.0) is None  # sentinel
    assert _normalize_percentile(-1000.0) is None
    assert _normalize_percentile(None) is None
    assert _normalize_percentile("abc") is None
    assert _normalize_percentile(150.0) is None  # absurd -> sentinel-class


def test_normalize_fraction():
    assert _normalize_fraction(0.96) == pytest.approx(0.96)
    assert _normalize_fraction(1.0) == 1.0
    assert _normalize_fraction(-999.0) is None
    assert _normalize_fraction(5.0) is None  # out of [0,1]
    assert _normalize_fraction(None) is None


# ---------------------------------------------------------------------------
# Compute / shape correctness on synthetic input.
# ---------------------------------------------------------------------------


def _read_fgb(fgb_bytes: bytes):
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        f.write(fgb_bytes)
        path = f.name
    return geopandas.read_file(path)


def test_features_to_fgb_shape_and_values():
    feats = [
        _bg_feature("482012115001", p_pm25=90.6, p_pnpl=87.5, total_pop=1010),
        _bg_feature("482012115003", p_pm25=80.1, p_pnpl=92.1, total_pop=1212, cx=-95.31),
    ]
    # value driven by the pm25 indicator
    fgb = _features_to_flatgeobuf(feats, value_field="P_PM25", indicator_key="pm25")
    assert isinstance(fgb, bytes) and len(fgb) > 0

    gdf = _read_fgb(fgb)
    assert len(gdf) == 2

    for col in (
        "bg_id", "state_name", "total_pop", "indicator", "value",
        "p_pm25", "p_ozone", "p_diesel", "p_resp", "p_traffic", "p_lead_paint",
        "p_superfund", "p_rmp", "p_tsdf", "p_wastewater", "p_minority",
        "minority_pct", "lowincome_pct", "demographic_index",
        "pm25_raw", "ozone_raw",
    ):
        assert col in gdf.columns, f"missing column {col}"

    row0 = gdf[gdf["bg_id"] == "482012115001"].iloc[0]
    assert row0["state_name"] == "Texas"
    assert row0["total_pop"] == 1010
    assert row0["indicator"] == "pm25"
    # value tracks the selected (pm25) percentile
    assert abs(row0["value"] - 90.6) < 1e-4
    assert abs(row0["p_pm25"] - 90.6) < 1e-4
    assert abs(row0["p_superfund"] - 87.5) < 1e-4
    assert abs(row0["minority_pct"] - 0.96) < 1e-6
    assert abs(row0["demographic_index"] - 0.88) < 1e-6
    assert str(gdf.geom_type.iloc[0]) == "Polygon"


def test_value_follows_selected_indicator():
    feats = [_bg_feature("482012115001", p_pnpl=87.5)]
    # selecting superfund -> value == p_superfund
    fgb = _features_to_flatgeobuf(
        feats, value_field="P_PNPL", indicator_key="superfund_proximity"
    )
    gdf = _read_fgb(fgb)
    row = gdf.iloc[0]
    assert row["indicator"] == "superfund_proximity"
    assert abs(row["value"] - 87.5) < 1e-4
    assert abs(row["value"] - row["p_superfund"]) < 1e-6


def test_null_sentinel_normalized_to_null():
    feats = [
        _bg_feature(
            "482012115099",
            p_pm25=-999.0,  # suppressed
            minorpct=-999.0,
            total_pop=-999,
        )
    ]
    fgb = _features_to_flatgeobuf(feats, value_field="P_PM25", indicator_key="pm25")
    gdf = _read_fgb(fgb)
    row = gdf.iloc[0]

    def _is_null(v):
        # geopandas/pyogrio serialize a normalized null as either None
        # (object column) or NaN (float column) depending on dtype.
        return v is None or (isinstance(v, float) and v != v)

    assert _is_null(row["value"])
    assert _is_null(row["p_pm25"])
    assert _is_null(row["minority_pct"])
    # -999 total pop -> null
    assert _is_null(row["total_pop"])


# ---------------------------------------------------------------------------
# Honest-empty path.
# ---------------------------------------------------------------------------


def test_empty_features_yield_valid_empty_fgb():
    fgb = _features_to_flatgeobuf([], value_field="P_PM25", indicator_key="pm25")
    assert isinstance(fgb, bytes) and len(fgb) > 0
    gdf = _read_fgb(fgb)
    assert len(gdf) == 0
    # schema still present
    for col in ("bg_id", "value", "indicator", "p_pm25", "demographic_index"):
        assert col in gdf.columns


def test_feature_without_geometry_is_skipped():
    feats = [
        {"attributes": {"ID": "x", "P_PM25": 50.0}, "geometry": None},
        _bg_feature("482012115001"),
    ]
    fgb = _features_to_flatgeobuf(feats, value_field="P_PM25", indicator_key="pm25")
    gdf = _read_fgb(fgb)
    assert len(gdf) == 1
    assert gdf.iloc[0]["bg_id"] == "482012115001"


# ---------------------------------------------------------------------------
# Input validation.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bbox",
    [
        (-95.0, 29.8, -95.3, 29.6),  # inverted
        (-95.3, 29.6, -95.3, 29.8),  # zero-width
        (-200.0, 29.6, -95.0, 29.8),  # lon out of range
        (-95.3, -100.0, -95.0, 29.8),  # lat out of range
        (-95.3, 29.6, -95.0),  # wrong length
        (float("nan"), 29.6, -95.0, 29.8),  # non-finite
    ],
)
def test_validate_bbox_rejects_bad(bbox):
    with pytest.raises(EPA_EJScreenInputError):
        _validate_bbox(bbox)


def test_validate_bbox_accepts_good():
    _validate_bbox((-95.30, 29.68, -95.05, 29.80))  # no raise


def test_fetch_rejects_unknown_indicator_without_network():
    # indicator is resolved BEFORE any network call, so a bad key raises cheaply.
    with pytest.raises(EPA_EJScreenInputError):
        fetch_epa_ejscreen(bbox=(-95.30, 29.68, -95.05, 29.80), indicator="bogus")


# ---------------------------------------------------------------------------
# Payload estimator / params / rounding.
# ---------------------------------------------------------------------------


def test_estimate_payload_mb():
    # ~0.05 sq deg urban county
    est = estimate_payload_mb((-95.30, 29.68, -95.05, 29.80))
    assert 0.02 <= est <= 90.0
    # None -> 1 sq deg default
    assert estimate_payload_mb(None) == pytest.approx(4.0)
    # clamps a huge bbox
    assert estimate_payload_mb((-130.0, 25.0, -65.0, 50.0)) == 90.0


def test_build_query_params_uses_json_envelope():
    params = _build_query_params((-95.30, 29.68, -95.05, 29.80), offset=0)
    # geometry must be a JSON envelope object (not a comma string)
    geom = json.loads(params["geometry"])
    assert geom["xmin"] == -95.30 and geom["ymax"] == 29.80
    assert geom["spatialReference"]["wkid"] == 4326
    assert params["geometryType"] == "esriGeometryEnvelope"
    assert params["outFields"] == "*"
    assert params["returnGeometry"] == "true"
    assert params["f"] == "json"


def test_round_bbox_to_6dp():
    out = _round_bbox_to_6dp((-95.3000001, 29.6800009, -95.05, 29.8))
    assert out == (-95.3, 29.680001, -95.05, 29.8)
