"""Unit tests for ``compute_flood_depth_damage`` (no network).

Inputs are SYNTHESIZED locally: a UTM depth raster with hand-placed depths +
a small EPSG:4326 point GeoJSON with NSI-shaped attributes (``val_struct`` /
``found_ht`` / ``occtype``), passed via ``assets_uri``, so the full pipeline
(stage -> bounds -> sample-in-raster-CRS -> curve -> USD -> FGB) runs offline.

Coverage:
1.  ``test_registered`` -- tool in TOOL_REGISTRY, cacheable=False /
    live-no-cache.
2.  ``test_curve_interpolation`` -- hand-checked curve cells (table rows,
    midpoints, below-0 floor, 16-ft cap).
3.  ``test_damage_matches_hand_computed`` -- a 4-ft-deep structure carries the
    EGM/HAZUS 0.471 fraction and fraction x val_struct dollars; totals agree.
4.  ``test_found_ht_reduces_damage`` -- foundation height shifts the curve.
5.  ``test_dry_and_nodata_points_zero`` -- dry cells + nodata cells = 0 damage
    with the honest nodata note.
6.  ``test_no_value_attribute`` -- fractions still computed; USD totals cover
    0 structures with a note.
7.  ``test_no_structures_raises`` -- empty asset layer raises the typed error.
8.  ``test_nsi_fetch_used_when_no_assets`` -- fetch_usace_nsi is called with
    the raster's EPSG:4326 bounds when assets_uri is omitted.
9.  ``test_bad_units_raises`` -- typed input validation.
10. ``test_category_and_corpus`` -- primary category + routing-corpus presence.
"""

from __future__ import annotations

import json
import pathlib

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds
from rasterio.warp import transform_bounds

from trid3nt_contracts.execution import LayerURI

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.compute_flood_depth_damage import (
    DEPTH_DAMAGE_CURVE_FT,
    FloodDamageInputError,
    FloodDamageNoStructuresError,
    FloodDepthDamageLayerURI,
    compute_flood_depth_damage,
    damage_fraction_at_depth,
)

# Synthetic depth grid: 40x40 at 30 m in UTM zone 11N.
N = 40
RES = 30.0
X0, Y0 = 500000.0, 4000000.0
CRS = "EPSG:32611"
NODATA = -9999.0

M_TO_FT = 3.280839895
FT_TO_M = 1.0 / M_TO_FT


def _cell_center(col: int, row: int) -> tuple[float, float]:
    """UTM coordinates of the center of grid cell (col, row from top)."""
    return X0 + (col + 0.5) * RES, Y0 + (N - row - 0.5) * RES


def _write_depth_raster(path: str, depth_m: np.ndarray) -> str:
    transform = from_bounds(X0, Y0, X0 + N * RES, Y0 + N * RES, N, N)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=N,
        width=N,
        count=1,
        dtype="float32",
        crs=CRS,
        transform=transform,
        nodata=NODATA,
    ) as dst:
        dst.write(depth_m.astype("float32"), 1)
    return path


def _utm_to_lonlat(x: float, y: float) -> tuple[float, float]:
    import pyproj

    tf = pyproj.Transformer.from_crs(CRS, "EPSG:4326", always_xy=True)
    return tf.transform(x, y)


def _write_assets(path: str, features: list[dict]) -> str:
    fc = {"type": "FeatureCollection", "features": features}
    pathlib.Path(path).write_text(json.dumps(fc))
    return path


def _point_feature(
    col: int, row: int, props: dict
) -> dict:
    lon, lat = _utm_to_lonlat(*_cell_center(col, row))
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": props,
    }


@pytest.fixture()
def depth_and_assets(tmp_path):
    """Depth raster with hand-placed depths + 4 NSI-shaped structure points.

    - cell (5, 5): 4 ft of water   -> structure A (val 200k, found_ht 0)
    - cell (10, 10): 6 ft of water -> structure B (val 100k, found_ht 2 ft)
    - cell (20, 20): dry (0)       -> structure C (val 300k)
    - cell (30, 30): NODATA        -> structure D (val 150k)
    """
    depth = np.zeros((N, N), dtype="float64")
    depth[5, 5] = 4.0 * FT_TO_M
    depth[10, 10] = 6.0 * FT_TO_M
    depth[30, 30] = NODATA
    raster = _write_depth_raster(str(tmp_path / "depth.tif"), depth)

    assets = _write_assets(
        str(tmp_path / "assets.geojson"),
        [
            _point_feature(5, 5, {"fd_id": 1, "occtype": "RES1", "val_struct": 200000.0, "found_ht": 0.0}),
            _point_feature(10, 10, {"fd_id": 2, "occtype": "RES1", "val_struct": 100000.0, "found_ht": 2.0}),
            _point_feature(20, 20, {"fd_id": 3, "occtype": "COM1", "val_struct": 300000.0, "found_ht": 0.0}),
            _point_feature(30, 30, {"fd_id": 4, "occtype": "RES1", "val_struct": 150000.0, "found_ht": 0.0}),
        ],
    )
    return raster, assets


def test_registered() -> None:
    entry = TOOL_REGISTRY["compute_flood_depth_damage"]
    assert entry.fn is compute_flood_depth_damage
    assert entry.metadata.cacheable is False
    assert entry.metadata.ttl_class == "live-no-cache"
    assert entry.metadata.open_world_hint is True  # fetches NSI


def test_curve_interpolation() -> None:
    # Published rows reproduce exactly.
    for depth_ft, frac in DEPTH_DAMAGE_CURVE_FT:
        assert damage_fraction_at_depth(depth_ft) == pytest.approx(frac)
    # Midpoint interpolates linearly: 0.5 ft -> (0.134 + 0.233) / 2.
    assert damage_fraction_at_depth(0.5) == pytest.approx((0.134 + 0.233) / 2)
    # Below the first floor -> 0 (no-basement curve).
    assert damage_fraction_at_depth(-0.5) == 0.0
    assert damage_fraction_at_depth(float("nan")) == 0.0
    # Capped at the 16-ft table maximum.
    assert damage_fraction_at_depth(40.0) == pytest.approx(0.807)


def test_damage_matches_hand_computed(depth_and_assets, tmp_path) -> None:
    raster, assets = depth_and_assets
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    result = compute_flood_depth_damage(
        depth_raster_uri=raster,
        assets_uri=assets,
        _output_dir=str(out_dir),
    )

    assert isinstance(result, FloodDepthDamageLayerURI)
    assert isinstance(result, LayerURI)
    assert result.layer_type == "vector"
    assert result.style_preset == "flood_depth_damage"
    assert result.n_structures == 4
    assert result.n_flooded == 2
    assert result.n_with_value == 4
    assert any("SCREENING ESTIMATE" in n for n in result.notes)
    assert any("NOT a Pelicun" in n for n in result.notes)

    gdf = gpd.read_file(result.uri).set_index("fd_id").sort_index()
    # Structure A: 4 ft above FFE -> EGM/HAZUS 0.471 -> 94,200 USD.
    assert gdf.loc[1, "depth_ft"] == pytest.approx(4.0, abs=0.01)
    assert gdf.loc[1, "damage_fraction"] == pytest.approx(0.471, abs=0.002)
    assert gdf.loc[1, "damage_usd"] == pytest.approx(0.471 * 200000.0, rel=0.005)
    # Structure B: 6 ft water - 2 ft foundation = 4 ft above FFE -> 0.471.
    assert gdf.loc[2, "depth_ft"] == pytest.approx(6.0, abs=0.01)
    assert gdf.loc[2, "depth_above_ffe_ft"] == pytest.approx(4.0, abs=0.01)
    assert gdf.loc[2, "damage_fraction"] == pytest.approx(0.471, abs=0.002)
    # Structures C (dry) and D (nodata): zero.
    assert gdf.loc[3, "damage_fraction"] == 0.0
    assert gdf.loc[4, "damage_fraction"] == 0.0
    assert gdf.loc[4, "depth_ft"] == 0.0

    expected_total = 0.471 * 200000.0 + 0.471 * 100000.0
    assert result.total_damage_usd == pytest.approx(expected_total, rel=0.005)
    assert result.max_damage_fraction == pytest.approx(0.471, abs=0.002)
    assert any("nodata" in n for n in result.notes)

    # Legend rides on the LayerURI, driven by the damage_fraction prop.
    assert result.legend is not None
    assert result.legend.kind == "categorical"
    assert result.legend.value_field == "damage_fraction"

    # Output is EPSG:4326 and bbox matches the raster's transformed bounds.
    assert gdf.crs is not None and gdf.crs.to_epsg() == 4326
    exp_bbox = transform_bounds(
        CRS, "EPSG:4326", X0, Y0, X0 + N * RES, Y0 + N * RES
    )
    assert result.bbox == pytest.approx(exp_bbox, abs=1e-4)


def test_found_ht_reduces_damage(depth_and_assets, tmp_path) -> None:
    raster, _ = depth_and_assets
    # Same 4-ft cell, but a 3-ft foundation -> 1 ft above FFE -> 0.233.
    assets = _write_assets(
        str(tmp_path / "raised.geojson"),
        [_point_feature(5, 5, {"fd_id": 1, "val_struct": 200000.0, "found_ht": 3.0})],
    )
    result = compute_flood_depth_damage(
        depth_raster_uri=raster, assets_uri=assets, _output_dir=str(tmp_path)
    )
    assert result.max_damage_fraction == pytest.approx(0.233, abs=0.002)


def test_dry_and_nodata_points_zero(depth_and_assets, tmp_path) -> None:
    raster, _ = depth_and_assets
    assets = _write_assets(
        str(tmp_path / "dry.geojson"),
        [
            _point_feature(20, 20, {"fd_id": 1, "val_struct": 300000.0}),
            _point_feature(30, 30, {"fd_id": 2, "val_struct": 150000.0}),
        ],
    )
    result = compute_flood_depth_damage(
        depth_raster_uri=raster, assets_uri=assets, _output_dir=str(tmp_path)
    )
    assert result.n_flooded == 0
    assert result.total_damage_usd == 0.0
    assert result.max_damage_fraction == 0.0


def test_no_value_attribute(depth_and_assets, tmp_path) -> None:
    raster, _ = depth_and_assets
    assets = _write_assets(
        str(tmp_path / "novalue.geojson"),
        [_point_feature(5, 5, {"fd_id": 1})],
    )
    result = compute_flood_depth_damage(
        depth_raster_uri=raster, assets_uri=assets, _output_dir=str(tmp_path)
    )
    assert result.n_with_value == 0
    assert result.total_damage_usd == 0.0
    assert result.max_damage_fraction == pytest.approx(0.471, abs=0.002)
    assert any("no val_struct" in n for n in result.notes)


def test_no_structures_raises(depth_and_assets, tmp_path) -> None:
    raster, _ = depth_and_assets
    assets = _write_assets(str(tmp_path / "empty.geojson"), [])
    with pytest.raises(FloodDamageNoStructuresError):
        compute_flood_depth_damage(
            depth_raster_uri=raster, assets_uri=assets, _output_dir=str(tmp_path)
        )


def test_nsi_fetch_used_when_no_assets(depth_and_assets, tmp_path, monkeypatch) -> None:
    raster, assets = depth_and_assets
    import trid3nt_server.tools.fetch_usace_nsi as nsi_mod

    captured: dict = {}

    def _fake_nsi(bbox, **_kw):
        captured["bbox"] = bbox
        return LayerURI(
            layer_id="nsi-test",
            name="NSI (test)",
            layer_type="vector",
            uri=assets,
            style_preset="usace_nsi",
        )

    monkeypatch.setattr(nsi_mod, "fetch_usace_nsi", _fake_nsi)
    result = compute_flood_depth_damage(
        depth_raster_uri=raster, _output_dir=str(tmp_path)
    )
    assert result.n_structures == 4
    # NSI was queried with the raster's EPSG:4326 bounds.
    exp_bbox = transform_bounds(
        CRS, "EPSG:4326", X0, Y0, X0 + N * RES, Y0 + N * RES
    )
    assert captured["bbox"] == pytest.approx(exp_bbox, abs=1e-6)
    assert any("National Structure Inventory" in n for n in result.notes)


def test_bad_units_raises(depth_and_assets, tmp_path) -> None:
    raster, assets = depth_and_assets
    with pytest.raises(FloodDamageInputError):
        compute_flood_depth_damage(
            depth_raster_uri=raster,
            assets_uri=assets,
            depth_units="inches",
            _output_dir=str(tmp_path),
        )
    with pytest.raises(FloodDamageInputError):
        compute_flood_depth_damage(
            depth_raster_uri="", assets_uri=assets, _output_dir=str(tmp_path)
        )


def test_category_and_corpus() -> None:
    import yaml

    from trid3nt_server import categories
    from trid3nt_server.tools import discover_dataset as dd

    assert (
        categories.PRIMARY_CATEGORY["compute_flood_depth_damage"]
        == "damage_assessment"
    )
    corpus_path = (
        pathlib.Path(dd.__file__).resolve().parents[1]
        / "data"
        / "tool_query_corpus.yaml"
    )
    corpus = yaml.safe_load(corpus_path.read_text())
    assert len(corpus.get("compute_flood_depth_damage", [])) >= 5
