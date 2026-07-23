"""Unit tests for ``compute_urban_heat_island`` (no network).

All inputs are SYNTHESIZED locally and passed via the override URIs
(``lst_uri`` / ``landcover_uri``), so the full pipeline (stage -> resample
onto the class grid -> per-class stats -> UHI delta -> COG) runs offline.

Coverage:
1.  ``test_registered`` -- tool in TOOL_REGISTRY, cacheable=False /
    live-no-cache.
2.  ``test_uhi_delta_hand_computed_same_grid`` -- LST on the exact land-cover
    grid (value-identical passthrough): built cells 40 C, tree cells 30 C ->
    per-class means exact, uhi_delta_c == 10.0.
3.  ``test_coarse_lst_resampled`` -- a 4x4 uniform 35 C LST resamples onto
    the 40x40 class grid (bilinear of a constant = constant) -> delta 0 with
    the coarse->fine note.
4.  ``test_no_built_class_gives_none`` / ``test_no_vegetation_gives_none`` --
    honest None delta + note (never a fabricated 0).
5.  ``test_no_info_classes_excluded`` -- No-Data/Clouds classes carry no row.
6.  ``test_output_raster_readable`` -- the emitted COG carries the aligned
    deg-C values with the land_surface_temp_c paint.
7.  ``test_bad_bbox_raises`` / ``test_aoi_clamp_raises`` /
    ``test_bad_daynight_raises`` -- typed input validation.
8.  ``test_category_and_corpus`` -- primary category + routing-corpus presence.
"""

from __future__ import annotations

import pathlib

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds

from trid3nt_contracts.execution import LayerURI

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.processing.compute_urban_heat_island import (
    UhiAoiTooLargeError,
    UhiInputError,
    UrbanHeatIslandLayerURI,
    compute_urban_heat_island,
)

BBOX = (-117.05, 34.00, -116.95, 34.05)

# Synthetic class grid: 40x40 at 30 m in UTM zone 11N.
N = 40
RES = 30.0
X0, Y0 = 500000.0, 4000000.0
CRS = "EPSG:32611"
NODATA = -9999.0

BUILT_LST_C = 40.0
TREE_LST_C = 30.0


def _write_raster(
    path: str, data: np.ndarray, *, res: float = RES, n: int = N, dtype: str = "float32"
) -> str:
    transform = from_bounds(X0, Y0, X0 + n * res, Y0 + n * res, n, n)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=n,
        width=n,
        count=1,
        dtype=dtype,
        crs=CRS,
        transform=transform,
        nodata=NODATA,
    ) as dst:
        dst.write(data.astype(dtype), 1)
    return path


@pytest.fixture()
def split_city(tmp_path):
    """Left half Built area (7), right half Trees (2); LST 40 C / 30 C on the
    SAME grid (value-identical passthrough -> exact hand-checked means)."""
    lc = np.full((N, N), 2, dtype="float64")
    lc[:, : N // 2] = 7
    lc_path = _write_raster(str(tmp_path / "landcover.tif"), lc, dtype="int16")

    lst = np.full((N, N), TREE_LST_C, dtype="float64")
    lst[:, : N // 2] = BUILT_LST_C
    lst_path = _write_raster(str(tmp_path / "lst.tif"), lst)
    return lst_path, lc_path


def test_registered() -> None:
    entry = TOOL_REGISTRY["compute_urban_heat_island"]
    assert entry.fn is compute_urban_heat_island
    assert entry.metadata.cacheable is False
    assert entry.metadata.ttl_class == "live-no-cache"
    assert entry.metadata.open_world_hint is True  # fetches MODIS + landcover


def test_uhi_delta_hand_computed_same_grid(split_city, tmp_path) -> None:
    lst_path, lc_path = split_city
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    result = compute_urban_heat_island(
        bbox=BBOX,
        lst_uri=lst_path,
        landcover_uri=lc_path,
        _output_dir=str(out_dir),
    )

    assert isinstance(result, UrbanHeatIslandLayerURI)
    assert isinstance(result, LayerURI)
    assert result.layer_type == "raster"
    assert result.style_preset == "land_surface_temp_c"
    assert "deg C" in (result.units or "")
    assert tuple(result.bbox) == BBOX
    assert result.daynight == "day"

    # Hand-checked: built mean 40, vegetation mean 30, delta exactly 10.
    assert result.built_mean_lst_c == pytest.approx(BUILT_LST_C)
    assert result.vegetation_mean_lst_c == pytest.approx(TREE_LST_C)
    assert result.uhi_delta_c == pytest.approx(BUILT_LST_C - TREE_LST_C)

    # Per-class table: exactly the two classes, each half the joint cells.
    table = {row["class_code"]: row for row in result.per_class_lst_c}
    assert set(table) == {2, 7}
    assert table[7]["label"] == "Built area"
    assert table[7]["mean_lst_c"] == pytest.approx(BUILT_LST_C)
    assert table[7]["pixel_count"] == N * N // 2
    assert table[7]["share"] == pytest.approx(0.5)
    assert table[2]["mean_lst_c"] == pytest.approx(TREE_LST_C)

    assert any("uhi_delta_c" in n for n in result.notes)
    # Same-grid LST passes through value-identical (noted).
    assert any("value-identical" in n for n in result.notes)


def test_coarse_lst_resampled(split_city, tmp_path) -> None:
    """A 4x4 uniform 35 C LST (coarse) interpolates to a constant on the fine
    grid -> both class means 35, delta 0, and the coarse->fine note present."""
    _, lc_path = split_city
    coarse = np.full((4, 4), 35.0, dtype="float64")
    lst_path = _write_raster(
        str(tmp_path / "lst_coarse.tif"), coarse, res=RES * 10, n=4
    )
    result = compute_urban_heat_island(
        bbox=BBOX,
        lst_uri=lst_path,
        landcover_uri=lc_path,
        _output_dir=str(tmp_path),
    )
    assert result.built_mean_lst_c == pytest.approx(35.0, abs=1e-6)
    assert result.vegetation_mean_lst_c == pytest.approx(35.0, abs=1e-6)
    assert result.uhi_delta_c == pytest.approx(0.0, abs=1e-6)
    assert any("COARSE->FINE" in n for n in result.notes)


def test_no_built_class_gives_none(split_city, tmp_path) -> None:
    lst_path, _ = split_city
    lc = np.full((N, N), 2, dtype="float64")  # all Trees
    lc_path = _write_raster(str(tmp_path / "trees.tif"), lc, dtype="int16")
    result = compute_urban_heat_island(
        bbox=BBOX, lst_uri=lst_path, landcover_uri=lc_path, _output_dir=str(tmp_path)
    )
    assert result.uhi_delta_c is None
    assert result.built_mean_lst_c is None
    assert result.vegetation_mean_lst_c is not None
    assert any("no Built-area" in n for n in result.notes)


def test_no_vegetation_gives_none(split_city, tmp_path) -> None:
    lst_path, _ = split_city
    lc = np.full((N, N), 7, dtype="float64")  # all Built
    lc[:5, :5] = 1  # some Water (NOT in the vegetation union)
    lc_path = _write_raster(str(tmp_path / "built.tif"), lc, dtype="int16")
    result = compute_urban_heat_island(
        bbox=BBOX, lst_uri=lst_path, landcover_uri=lc_path, _output_dir=str(tmp_path)
    )
    assert result.uhi_delta_c is None
    assert result.built_mean_lst_c is not None
    assert result.vegetation_mean_lst_c is None
    assert any("no vegetated" in n for n in result.notes)


def test_no_info_classes_excluded(split_city, tmp_path) -> None:
    lst_path, _ = split_city
    lc = np.full((N, N), 7, dtype="float64")
    lc[:, N // 2 :] = 2
    lc[:4, :] = 10  # Clouds -- no surface information
    lc_path = _write_raster(str(tmp_path / "cloudy.tif"), lc, dtype="int16")
    result = compute_urban_heat_island(
        bbox=BBOX, lst_uri=lst_path, landcover_uri=lc_path, _output_dir=str(tmp_path)
    )
    codes = {row["class_code"] for row in result.per_class_lst_c}
    assert 10 not in codes
    assert codes == {2, 7}


def test_output_raster_readable(split_city, tmp_path) -> None:
    lst_path, lc_path = split_city
    result = compute_urban_heat_island(
        bbox=BBOX, lst_uri=lst_path, landcover_uri=lc_path, _output_dir=str(tmp_path)
    )
    assert pathlib.Path(result.uri).exists()
    with rasterio.open(result.uri) as src:
        a = src.read(1)
        assert src.shape == (N, N)  # the land-cover (reference) grid
        assert src.crs.to_epsg() == 32611
    assert float(a[0, 0]) == pytest.approx(BUILT_LST_C)  # west = built
    assert float(a[0, N - 1]) == pytest.approx(TREE_LST_C)  # east = trees


def test_bad_bbox_raises() -> None:
    with pytest.raises(UhiInputError):
        compute_urban_heat_island(bbox=(1.0, 2.0, 3.0))  # type: ignore[arg-type]


def test_aoi_clamp_raises() -> None:
    with pytest.raises(UhiAoiTooLargeError):
        compute_urban_heat_island(bbox=(-120.0, 33.0, -117.0, 36.0))


def test_bad_daynight_raises(split_city, tmp_path) -> None:
    lst_path, lc_path = split_city
    with pytest.raises(UhiInputError):
        compute_urban_heat_island(
            bbox=BBOX,
            daynight="dusk",
            lst_uri=lst_path,
            landcover_uri=lc_path,
            _output_dir=str(tmp_path),
        )


def test_category_and_corpus() -> None:
    import yaml

    from trid3nt_server import categories
    from trid3nt_server.tools.discovery import search_tools as dd

    assert (
        categories.PRIMARY_CATEGORY["compute_urban_heat_island"]
        == "land_cover_development"
    )
    assert "weather_atmosphere" in categories.SECONDARY_CATEGORIES[
        "compute_urban_heat_island"
    ]
    corpus_path = pathlib.Path(dd._default_corpus_path())
    corpus = yaml.safe_load(corpus_path.read_text())
    assert len(corpus.get("compute_urban_heat_island", [])) >= 5
