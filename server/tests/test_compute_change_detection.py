"""Unit tests for ``compute_change_detection`` (no network).

All inputs are SYNTHESIZED locally and passed via the precomputed-index
override URIs (``imagery_a_uri`` / ``imagery_b_uri``), so the full pipeline
(stage -> resample-onto-A -> delta -> threshold -> vectorize -> FGB) runs for
real without touching the Planetary Computer.

Coverage:
1.  ``test_registered`` -- tool in TOOL_REGISTRY, cacheable=False /
    ttl_class="live-no-cache".
2.  ``test_gain_loss_polygons_hand_checked`` -- a synthetic pair with a known
    gain block and a known loss block yields exactly those polygons with
    hand-computed areas + the categorical gain/loss legend.
3.  ``test_no_change_raises`` -- identical rasters raise the honest typed
    ``ChangeDetectionNoChangeError`` (never an empty layer).
4.  ``test_threshold_respected`` -- a delta below the threshold is no-change;
    lowering the threshold surfaces it.
5.  ``test_mismatched_grid_resampled`` -- raster B on a coarser grid is
    resampled onto A's grid and still detects the change.
6.  ``test_requires_both_override_uris`` / ``test_requires_dates_when_no_overrides``
    / ``test_bad_bbox_raises`` / ``test_aoi_clamp_raises`` /
    ``test_bad_index_raises`` -- typed input validation.
7.  ``test_category_and_corpus`` -- primary category + routing-corpus presence.
"""

from __future__ import annotations

import pathlib

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds

from trid3nt_contracts.execution import LayerURI

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.compute_change_detection import (
    ChangeDetectionAoiTooLargeError,
    ChangeDetectionInputError,
    ChangeDetectionLayerURI,
    ChangeDetectionNoChangeError,
    compute_change_detection,
)

# Small legal AOI (bbox is used for validation/labeling only when both
# override URIs are supplied; the synthetic rasters carry their own UTM grid).
BBOX = (-117.05, 34.00, -116.95, 34.05)

# Synthetic grid: 40x40 at 30 m in UTM zone 11N.
N = 40
RES = 30.0
X0, Y0 = 500000.0, 4000000.0
CRS = "EPSG:32611"

NODATA = -9999.0


def _write_raster(path: str, data: np.ndarray, *, res: float = RES, n: int = N) -> str:
    transform = from_bounds(X0, Y0, X0 + n * res, Y0 + n * res, n, n)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=n,
        width=n,
        count=1,
        dtype="float32",
        crs=CRS,
        transform=transform,
        nodata=NODATA,
    ) as dst:
        dst.write(data.astype("float32"), 1)
    return path


@pytest.fixture()
def index_pair(tmp_path):
    """Index A = 0.5 everywhere; B has a 10x10 gain (+0.3) and 8x8 loss (-0.3)."""
    a = np.full((N, N), 0.5, dtype="float32")
    b = a.copy()
    b[2:12, 2:12] += 0.3  # gain block: 100 cells
    b[20:28, 20:28] -= 0.3  # loss block: 64 cells
    a_path = _write_raster(str(tmp_path / "index_a.tif"), a)
    b_path = _write_raster(str(tmp_path / "index_b.tif"), b)
    return a_path, b_path


def test_registered() -> None:
    entry = TOOL_REGISTRY["compute_change_detection"]
    assert entry.fn is compute_change_detection
    assert entry.metadata.cacheable is False
    assert entry.metadata.ttl_class == "live-no-cache"
    assert entry.metadata.open_world_hint is True  # fetches S2 inputs


def test_gain_loss_polygons_hand_checked(index_pair, tmp_path) -> None:
    a_path, b_path = index_pair
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    result = compute_change_detection(
        bbox=BBOX,
        imagery_a_uri=a_path,
        imagery_b_uri=b_path,
        _output_dir=str(out_dir),
    )

    assert isinstance(result, ChangeDetectionLayerURI)
    assert isinstance(result, LayerURI)
    assert result.layer_type == "vector"
    assert result.style_preset == "change_detection"
    assert result.index == "ndvi"
    assert result.threshold == pytest.approx(0.15)
    assert tuple(result.bbox) == BBOX
    assert result.scene_a_id is None and result.scene_b_id is None
    assert isinstance(result.notes, list) and result.notes

    # One contiguous polygon per block.
    assert result.gain_count == 1
    assert result.loss_count == 1
    # Hand-computed areas: UTM cells are exactly 30x30 m but the speck filter
    # measures via Web-Mercator (inflated by ~1/cos(lat)^2 at 36N); allow a
    # generous relative band around the true area.
    true_gain = 100 * RES * RES  # 90_000 m^2
    true_loss = 64 * RES * RES  # 57_600 m^2
    assert true_gain * 0.8 < result.gain_area_m2 < true_gain * 2.0
    assert true_loss * 0.8 < result.loss_area_m2 < true_loss * 2.0
    assert result.gain_area_m2 > result.loss_area_m2

    # The artifact is a readable FlatGeobuf in EPSG:4326 with the class prop.
    assert pathlib.Path(result.uri).exists()
    gdf = gpd.read_file(result.uri)
    assert set(gdf["change"]) == {"gain", "loss"}
    assert gdf.crs is not None and gdf.crs.to_epsg() == 4326
    assert (gdf["area_m2"] > 0).all()

    # Legend rides on the LayerURI: categorical, driven by the change prop.
    assert result.legend is not None
    assert result.legend.kind == "categorical"
    assert result.legend.value_field == "change"
    assert {c.value for c in result.legend.classes} == {"gain", "loss"}


def test_no_change_raises(index_pair, tmp_path) -> None:
    a_path, _ = index_pair
    with pytest.raises(ChangeDetectionNoChangeError):
        compute_change_detection(
            bbox=BBOX,
            imagery_a_uri=a_path,
            imagery_b_uri=a_path,  # identical -> delta 0 everywhere
            _output_dir=str(tmp_path),
        )


def test_threshold_respected(tmp_path) -> None:
    a = np.full((N, N), 0.5, dtype="float32")
    b = a.copy()
    b[5:15, 5:15] += 0.10  # below the 0.15 default
    a_path = _write_raster(str(tmp_path / "a.tif"), a)
    b_path = _write_raster(str(tmp_path / "b.tif"), b)

    with pytest.raises(ChangeDetectionNoChangeError):
        compute_change_detection(
            bbox=BBOX,
            imagery_a_uri=a_path,
            imagery_b_uri=b_path,
            _output_dir=str(tmp_path),
        )

    result = compute_change_detection(
        bbox=BBOX,
        threshold=0.05,
        imagery_a_uri=a_path,
        imagery_b_uri=b_path,
        _output_dir=str(tmp_path),
    )
    assert result.gain_count == 1
    assert result.loss_count == 0
    assert result.threshold == pytest.approx(0.05)


def test_mismatched_grid_resampled(index_pair, tmp_path) -> None:
    """Raster B on a coarser (60 m, 20x20) grid resamples onto A's grid."""
    a_path, _ = index_pair
    n2 = N // 2
    b = np.full((n2, n2), 0.5, dtype="float32")
    b[2:8, 2:8] += 0.3  # gain block in coarse cells
    b_path = _write_raster(str(tmp_path / "b_coarse.tif"), b, res=RES * 2, n=n2)

    result = compute_change_detection(
        bbox=BBOX,
        imagery_a_uri=a_path,
        imagery_b_uri=b_path,
        _output_dir=str(tmp_path),
    )
    assert result.gain_count >= 1
    assert result.loss_count == 0


def test_requires_both_override_uris(index_pair, tmp_path) -> None:
    a_path, _ = index_pair
    with pytest.raises(ChangeDetectionInputError):
        compute_change_detection(
            bbox=BBOX, imagery_a_uri=a_path, _output_dir=str(tmp_path)
        )


def test_requires_dates_when_no_overrides() -> None:
    with pytest.raises(ChangeDetectionInputError):
        compute_change_detection(bbox=BBOX)


def test_bad_bbox_raises() -> None:
    with pytest.raises(ChangeDetectionInputError):
        compute_change_detection(bbox=(1.0, 2.0, 3.0))  # type: ignore[arg-type]


def test_aoi_clamp_raises() -> None:
    with pytest.raises(ChangeDetectionAoiTooLargeError):
        compute_change_detection(bbox=(-118.0, 33.0, -117.0, 34.0))


def test_bad_index_raises(index_pair, tmp_path) -> None:
    a_path, b_path = index_pair
    with pytest.raises(ChangeDetectionInputError):
        compute_change_detection(
            bbox=BBOX,
            index="evi",
            imagery_a_uri=a_path,
            imagery_b_uri=b_path,
            _output_dir=str(tmp_path),
        )


def test_ndwi_index_recorded(index_pair, tmp_path) -> None:
    a_path, b_path = index_pair
    result = compute_change_detection(
        bbox=BBOX,
        index="ndwi",
        imagery_a_uri=a_path,
        imagery_b_uri=b_path,
        _output_dir=str(tmp_path),
    )
    assert result.index == "ndwi"
    assert result.legend is not None and "NDWI" in (result.legend.label or "")


def test_category_and_corpus() -> None:
    import yaml

    from trid3nt_server import categories
    from trid3nt_server.tools import discover_dataset as dd

    assert (
        categories.PRIMARY_CATEGORY["compute_change_detection"]
        == "land_cover_development"
    )
    corpus_path = (
        pathlib.Path(dd.__file__).resolve().parents[1]
        / "data"
        / "tool_query_corpus.yaml"
    )
    corpus = yaml.safe_load(corpus_path.read_text())
    assert len(corpus.get("compute_change_detection", [])) >= 5
