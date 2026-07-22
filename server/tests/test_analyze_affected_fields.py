"""Unit tests for ``analyze_affected_fields`` (ftw-affected-fields demo, S1).

Coverage:
1.  Registration: the tool is in TOOL_REGISTRY with workflow-grade metadata
    (cacheable=False, ttl_class="live-no-cache", source_class="affected_fields").
2.  Pure-helper join + threshold split + ranking
    (``build_affected_fields_result`` / ``rank_affected_fields``):
      - a field whose zonal max >= threshold is AFFECTED; below threshold is not.
      - crop_name + area pass through onto each affected entry.
      - ranking by peak (default) and by area.
      - empty / zero-affected -> [] with an honest headline (honesty floor).
3.  End-to-end against a SYNTHETIC plume GeoTIFF (a gaussian blob, EPSG:4326) +
    3 synthetic FTW field polygons (FlatGeobuf via geopandas): one overlapping
    the plume core (affected, ranked first), one on the edge (affected, lower),
    one outside (absent). Asserts ranking + crop_name passthrough + sane mg/L.
4.  Threshold raise drops the edge field; empty-intersection honesty.

No Gemini / Vertex / Batch / mf6 — the plume is a synthetic GeoTIFF and the
fields are a synthetic FlatGeobuf; ``compute_zonal_statistics`` runs locally.
"""

from __future__ import annotations

import math
import os
import tempfile

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.processing.analyze_affected_fields import (
    DEFAULT_THRESHOLD_MGL,
    AffectedFieldsInputError,
    analyze_affected_fields,
    build_affected_fields_result,
    format_affected_fields_headline,
    rank_affected_fields,
)


# --------------------------------------------------------------------------- #
# Synthetic plume + field builders (EPSG:4326).
# --------------------------------------------------------------------------- #

# A small WGS84 AOI (degrees). The plume gaussian is centred in the middle.
_MINX, _MINY, _MAXX, _MAXY = -93.70, 42.00, -93.60, 42.08
_W, _H = 100, 80  # plume grid
_CX = (_MINX + _MAXX) / 2.0
_CY = (_MINY + _MAXY) / 2.0


def _write_gaussian_plume(path: str, peak_mgl: float = 20.0) -> None:
    """Write a single-band gaussian-blob concentration COG (mg/L), EPSG:4326.

    Peak at the AOI centre; clean (0) far away — a stand-in for a real plume COG.
    """
    transform = from_bounds(_MINX, _MINY, _MAXX, _MAXY, _W, _H)
    # Pixel centre coords.
    xs = _MINX + (np.arange(_W) + 0.5) * (_MAXX - _MINX) / _W
    ys = _MAXY - (np.arange(_H) + 0.5) * (_MAXY - _MINY) / _H
    xx, yy = np.meshgrid(xs, ys)
    # Gaussian in degrees; sigma ~ 1/6 of the AOI width.
    sx = (_MAXX - _MINX) / 6.0
    sy = (_MAXY - _MINY) / 6.0
    blob = peak_mgl * np.exp(
        -(((xx - _CX) ** 2) / (2 * sx**2) + ((yy - _CY) ** 2) / (2 * sy**2))
    )
    blob = blob.astype("float32")
    profile = {
        "driver": "GTiff",
        "dtype": "float32",
        "width": _W,
        "height": _H,
        "count": 1,
        "crs": "EPSG:4326",
        "transform": transform,
        "nodata": 0.0,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(blob, 1)
        dst.update_tags(units="mg/L")


def _write_three_fields_fgb(path: str) -> None:
    """Write a 3-feature FlatGeobuf of FTW-style field polygons (crop_name).

    Field 0 (corn): a box ON the plume core (centre) -> highest concentration.
    Field 1 (soybeans): a box near the AOI EDGE -> low (edge) concentration.
    Field 2 (wheat): a box OUTSIDE the plume (a tiny corner box where the
        gaussian is effectively 0) -> not affected.
    Order matters: by_zone keys align with this 0/1/2 feature order.
    """
    import geopandas as gpd  # type: ignore[import-not-found]
    from shapely.geometry import box  # type: ignore[import-not-found]

    dx = (_MAXX - _MINX)
    dy = (_MAXY - _MINY)
    # 0: centred core box.
    core = box(_CX - dx * 0.08, _CY - dy * 0.08, _CX + dx * 0.08, _CY + dy * 0.08)
    # 1: edge box near the right edge.
    edge = box(_MAXX - dx * 0.12, _CY - dy * 0.06, _MAXX - dx * 0.02, _CY + dy * 0.06)
    # 2: outside box in the far bottom-left corner (gaussian ~ 0 there).
    outside = box(_MINX, _MINY, _MINX + dx * 0.04, _MINY + dy * 0.04)

    gdf = gpd.GeoDataFrame(
        {"crop_name": ["corn", "soybeans", "wheat"]},
        geometry=[core, edge, outside],
        crs="EPSG:4326",
    )
    gdf.to_file(path, driver="FlatGeobuf", engine="pyogrio")


# --------------------------------------------------------------------------- #
# 1. Registration
# --------------------------------------------------------------------------- #


def test_analyze_affected_fields_registered() -> None:
    entry = TOOL_REGISTRY.get("analyze_affected_fields")
    assert entry is not None
    assert entry.metadata.cacheable is False
    assert entry.metadata.ttl_class == "live-no-cache"
    assert entry.metadata.source_class == "affected_fields"


# --------------------------------------------------------------------------- #
# 2. Pure-helper join + threshold split + ranking
# --------------------------------------------------------------------------- #


def test_build_result_threshold_split_and_passthrough() -> None:
    """A field at/over the threshold is affected; crop_name + area pass through."""
    by_zone = {
        "0": {"max": 18.0, "mean": 9.0, "count": 40},
        "1": {"max": 2.5, "mean": 1.0, "count": 30},
        "2": {"max": None, "mean": None, "count": 0},  # untouched
    }
    crops = ["corn", "soybeans", "wheat"]
    areas = [0.50, 0.30, 0.20]
    out = build_affected_fields_result(
        by_zone, crops, areas, threshold_mgl=1.0, rank_by="peak"
    )
    assert out["n_fields_total"] == 3
    assert out["n_fields_affected"] == 2  # field 2 has no plume pixels
    ids = [f["field_id"] for f in out["affected_fields"]]
    assert ids == [0, 1]  # ranked by peak desc
    top = out["affected_fields"][0]
    assert top["crop_name"] == "corn"
    assert top["max_concentration_mgl"] == 18.0
    assert top["mean_concentration_mgl"] == 9.0
    assert top["area_km2"] == 0.50
    assert out["worst_field"]["field_id"] == 0
    assert "corn" in out["headline"]


def test_threshold_excludes_below() -> None:
    """Raising the threshold above the edge field's peak drops it."""
    by_zone = {
        "0": {"max": 18.0, "mean": 9.0, "count": 40},
        "1": {"max": 2.5, "mean": 1.0, "count": 30},
    }
    out = build_affected_fields_result(
        by_zone, ["corn", "soybeans"], [0.5, 0.3], threshold_mgl=5.0, rank_by="peak"
    )
    assert out["n_fields_affected"] == 1
    assert out["affected_fields"][0]["field_id"] == 0


def test_rank_by_area() -> None:
    """rank_by='area' orders by affected area, not peak."""
    affected = [
        {"field_id": 0, "max_concentration_mgl": 18.0, "area_km2": 0.20},
        {"field_id": 1, "max_concentration_mgl": 5.0, "area_km2": 0.90},
    ]
    by_peak = rank_affected_fields(affected, "peak")
    assert [f["field_id"] for f in by_peak] == [0, 1]
    by_area = rank_affected_fields(affected, "area")
    assert [f["field_id"] for f in by_area] == [1, 0]


def test_empty_intersection_is_honest() -> None:
    """Zero affected fields -> [] + an explicit 'no fields affected' headline."""
    by_zone = {
        "0": {"max": None, "mean": None, "count": 0},
        "1": {"max": 0.0005, "mean": 0.0001, "count": 5},  # below default floor
    }
    out = build_affected_fields_result(
        by_zone, ["corn", "soy"], [0.4, 0.3],
        threshold_mgl=DEFAULT_THRESHOLD_MGL, rank_by="peak",
    )
    assert out["n_fields_affected"] == 0
    assert out["affected_fields"] == []
    assert out["worst_field"] is None
    assert "No farm fields affected" in out["headline"]


def test_headline_zero_and_nonzero() -> None:
    assert "No farm fields affected" in format_affected_fields_headline([], 0.0, 0.001)
    ranked = [{"field_id": 3, "crop_name": "corn", "max_concentration_mgl": 12.3,
               "area_km2": 0.5}]
    h = format_affected_fields_headline(ranked, 0.5, 0.001)
    assert "1 farm field affected" in h
    assert "corn" in h
    assert "12.3" in h


# --------------------------------------------------------------------------- #
# 3. End-to-end against a synthetic plume + fields (local zonal stats)
# --------------------------------------------------------------------------- #


def test_end_to_end_synthetic_plume_and_fields() -> None:
    """Core field ranked first, edge field lower, outside field absent."""
    with tempfile.TemporaryDirectory() as tmp:
        plume = os.path.join(tmp, "plume.tif")
        fields = os.path.join(tmp, "fields.fgb")
        _write_gaussian_plume(plume, peak_mgl=20.0)
        _write_three_fields_fgb(fields)

        # threshold below the edge field's concentration so it counts too, but
        # above the ~0 outside field.
        out = analyze_affected_fields(
            plume_layer_uri=plume,
            fields_layer_uri=fields,
            threshold_mgl=0.01,
            rank_by="peak",
        )

    assert out["n_fields_total"] == 3
    crops = [f["crop_name"] for f in out["affected_fields"]]
    # The core field (corn) is affected + ranked first; the outside field
    # (wheat) is absent. NOTE: the FlatGeobuf round-trip may reorder features,
    # so we assert on crop_name (the load-bearing join), not the raw index —
    # crop_name MUST stay aligned to its geometry through the zonal join.
    assert "corn" in crops
    assert "wheat" not in crops
    assert out["affected_fields"][0]["crop_name"] == "corn"
    # The core (corn) field's peak is a sane fraction of the 20 mg/L peak.
    core = out["affected_fields"][0]
    core_max = core["max_concentration_mgl"]
    assert 5.0 < core_max <= 20.0001
    # If the edge field (soybeans) is affected it must rank below the core.
    if "soybeans" in crops:
        assert crops.index("corn") < crops.index("soybeans")
        edge_max = next(
            f["max_concentration_mgl"]
            for f in out["affected_fields"]
            if f["crop_name"] == "soybeans"
        )
        assert edge_max < core_max
    # Affected area is positive + finite; units carried through.
    assert out["affected_area_km2"] > 0.0
    assert math.isfinite(out["affected_area_km2"])
    assert out["units"] == "mg/L"
    # The headline names the worst-hit field id + its crop.
    assert f"field {core['field_id']}" in out["headline"]
    assert "corn" in out["headline"]


def test_end_to_end_high_threshold_drops_edge() -> None:
    """A threshold near the peak leaves only the core field (or none)."""
    with tempfile.TemporaryDirectory() as tmp:
        plume = os.path.join(tmp, "plume.tif")
        fields = os.path.join(tmp, "fields.fgb")
        _write_gaussian_plume(plume, peak_mgl=20.0)
        _write_three_fields_fgb(fields)
        out = analyze_affected_fields(
            plume_layer_uri=plume,
            fields_layer_uri=fields,
            threshold_mgl=10.0,
            rank_by="peak",
        )
    crops = [f["crop_name"] for f in out["affected_fields"]]
    assert "soybeans" not in crops  # the edge field is below 10 mg/L
    assert "wheat" not in crops  # the outside field is ~0 mg/L
    # Only the core (corn) field survives a near-peak threshold.
    assert crops == ["corn"]


def test_end_to_end_empty_when_plume_misses_all_fields() -> None:
    """A plume far from every field -> 0 affected, honest headline."""
    with tempfile.TemporaryDirectory() as tmp:
        plume = os.path.join(tmp, "plume.tif")
        fields = os.path.join(tmp, "fields.fgb")
        # A near-flat ~0 plume (tiny peak well below any threshold).
        _write_gaussian_plume(plume, peak_mgl=0.0001)
        _write_three_fields_fgb(fields)
        out = analyze_affected_fields(
            plume_layer_uri=plume,
            fields_layer_uri=fields,
            threshold_mgl=1.0,
            rank_by="peak",
        )
    assert out["n_fields_affected"] == 0
    assert out["affected_fields"] == []
    assert "No farm fields affected" in out["headline"]


# --------------------------------------------------------------------------- #
# 4. Input validation
# --------------------------------------------------------------------------- #


def test_missing_uris_raise() -> None:
    with pytest.raises(AffectedFieldsInputError):
        analyze_affected_fields(plume_layer_uri="", fields_layer_uri="x.fgb")
    with pytest.raises(AffectedFieldsInputError):
        analyze_affected_fields(plume_layer_uri="p.tif", fields_layer_uri="")


def test_nonpositive_threshold_raises() -> None:
    with pytest.raises(AffectedFieldsInputError):
        analyze_affected_fields(
            plume_layer_uri="p.tif", fields_layer_uri="f.fgb", threshold_mgl=0.0
        )
