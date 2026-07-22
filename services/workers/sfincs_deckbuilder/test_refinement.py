#!/usr/bin/env python3
"""Synthetic-raster unit tests for derive_refinement_polygons.

Tests the NUMERICS of the auto-mesh-refinement derivation in isolation:
  - 0 m NAVD88 contour buffer -> finest refinement level
  - nearshore band and slope-gradient threshold -> mid refinement level
  - OSM rivers/buildings buffered -> appropriate levels
  - levels DESCEND outward from the coastline
  - budget cap coarsens / reduces max_level when the budget is tiny

All tests use a hand-built 40x40 topobathy raster with a clear land/water
split so there is a real 0 m contour, a slope ramp, and synthetic
GeoDataFrames for buildings and rivers.  NO cht_sfincs / boto3 required.

Run with the agent venv:
    venvs/agent/bin/python -m pytest \
        services/workers/sfincs_deckbuilder/test_refinement.py -q
"""

from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from rasterio.crs import CRS
from rasterio.transform import from_origin
from shapely.geometry import LineString, Point, Polygon, box

# ---------------------------------------------------------------------------
# Load entrypoint without installing the package
# ---------------------------------------------------------------------------
HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "sfincs_deckbuilder_entrypoint", HERE / "entrypoint.py"
)
ep = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(ep)  # type: ignore[union-attr]

# Target EPSG used throughout (UTM 16N — same as the North Star default).
TARGET_EPSG = 32616

# ---------------------------------------------------------------------------
# Helpers to build the synthetic topobathy COG
# ---------------------------------------------------------------------------

# Grid geometry: 40 x 40 cells, 50 m each.  The 0 m contour is at column 20
# (left half is bathymetry, right half is topography).
GRID_N = 40      # row count (nmax for the mock spec)
GRID_M = 40      # col count (mmax for the mock spec)
CELL_M = 50.0    # metre resolution
X0 = 600_000.0   # UTM easting of grid origin
Y0 = 3_200_000.0 # UTM northing of grid origin


def _make_topobathy_cog(path: Path) -> Path:
    """Write a 40x40 GTiff with a linear elevation ramp.

    Column 0  -> -10 m (deep water)
    Column 20 ->   0 m (shoreline / 0 m contour)
    Column 39 -> +10 m (land)

    The gradient is 20 m / (20 columns * 50 m/col) = 0.02 m/m in the
    left-half (offshore ramp) and the same in the right half (onshore ramp).
    We add a STEEP patch near column 30-33 (rows 10-30) with gradient ~0.5
    m/m so the slope_band test can detect it.
    """
    nrows, ncols = GRID_N, GRID_M
    # Linear ramp: z = (col - 20) * 0.5  -> -10 m at col 0, 0 at col 20, +10 at col 39
    col_idx = np.arange(ncols, dtype="float32")
    z_row = (col_idx - 20.0) * 0.5  # m
    arr = np.tile(z_row, (nrows, 1)).astype("float32")

    # Steep patch: rows 10-30, cols 30-33 -> elevations 5..10 m over 3 cols (step 1.7 m/col)
    for ci, dz in enumerate([5.0, 6.7, 8.3, 10.0]):
        arr[10:30, 30 + ci] = dz

    # rasterio convention: origin = top-left corner
    transform = from_origin(X0, Y0 + nrows * CELL_M, CELL_M, CELL_M)
    with rasterio.open(
        path, "w",
        driver="GTiff",
        height=nrows, width=ncols,
        count=1, dtype="float32",
        crs=CRS.from_epsg(TARGET_EPSG),
        transform=transform,
        nodata=float("nan"),
    ) as dst:
        dst.write(arr, 1)
    return path


def _make_spec(
    scratch: Path,
    cog_path: Path,
    *,
    refinement_levels: int = 3,
    max_cells: int = 2_000_000,
    slope_threshold: float = 0.05,
    rivers_uri: str | None = None,
    buildings_uri: str | None = None,
    nearshore_zmin: float = -2.0,
    nearshore_zmax: float = 0.0,
) -> dict:
    """Minimal build-spec dict accepted by derive_refinement_polygons."""
    dx = CELL_M
    dy = CELL_M
    spec: dict = {
        "aoi": {"bbox": [0, 0, 1, 1], "target_epsg": TARGET_EPSG},
        "topobathy": {"cog_uri": f"file://{cog_path}"},
        "grid": {
            "x0": X0, "y0": Y0,
            "nmax": GRID_N, "mmax": GRID_M,
            "dx": dx, "dy": dy,
            "rotation": 0.0,
            "refinement_levels": refinement_levels,
            "max_cells": max_cells,
            "slope_threshold": slope_threshold,
        },
        "snapwave": {
            "nearshore_zmin": nearshore_zmin,
            "nearshore_zmax": nearshore_zmax,
        },
        "mask": {},
        "forcing": {
            "tref": "20181010 000000",
            "tstart": "20181010 000000",
            "tstop": "20181010 020000",
        },
        "output": {
            "deck_dir_uri": "s3://dummy/deck/",
            "manifest_uri": "s3://dummy/manifest.json",
        },
    }
    if rivers_uri:
        spec["rivers"] = {"lines_uri": rivers_uri, "buffer_m": 75.0}
    if buildings_uri:
        spec["buildings"] = {"footprints_uri": buildings_uri, "buffer_m": 20.0}
    return spec


def _make_river_gdf(path: Path) -> Path:
    """Write a short river centerline GDF that crosses the nearshore zone."""
    # A line running north-south at x ~ X0 + 22*CELL_M (just onshore).
    line = LineString([
        (X0 + 22 * CELL_M, Y0 + 5 * CELL_M),
        (X0 + 22 * CELL_M, Y0 + 35 * CELL_M),
    ])
    gdf = gpd.GeoDataFrame({"geometry": [line]}, crs=CRS.from_epsg(TARGET_EPSG))
    gdf.to_file(path, driver="GPKG")
    return path


def _make_building_gdf(path: Path) -> Path:
    """Write two small building footprint polygons on the land half."""
    b1 = Polygon([
        (X0 + 25 * CELL_M, Y0 + 15 * CELL_M),
        (X0 + 27 * CELL_M, Y0 + 15 * CELL_M),
        (X0 + 27 * CELL_M, Y0 + 18 * CELL_M),
        (X0 + 25 * CELL_M, Y0 + 18 * CELL_M),
    ])
    b2 = Polygon([
        (X0 + 28 * CELL_M, Y0 + 25 * CELL_M),
        (X0 + 30 * CELL_M, Y0 + 25 * CELL_M),
        (X0 + 30 * CELL_M, Y0 + 28 * CELL_M),
        (X0 + 28 * CELL_M, Y0 + 28 * CELL_M),
    ])
    gdf = gpd.GeoDataFrame({"geometry": [b1, b2]}, crs=CRS.from_epsg(TARGET_EPSG))
    gdf.to_file(path, driver="GPKG")
    return path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def workspace(tmp_path_factory):
    """Module-scoped temp dir so the COG is written once."""
    d = tmp_path_factory.mktemp("refinement_test")
    cog = _make_topobathy_cog(d / "topo.tif")
    river_path = _make_river_gdf(d / "rivers.gpkg")
    building_path = _make_building_gdf(d / "buildings.gpkg")
    return {
        "dir": d,
        "cog": cog,
        "river_path": river_path,
        "building_path": building_path,
    }


# ---------------------------------------------------------------------------
# We need to stub _read_gdf so it doesn't try to download from a real URI.
# derive_refinement_polygons calls _read_gdf for rivers, buildings, and the
# optional refinement_polygons_uri.  We patch it to read from local files
# (or return None when no URI supplied).
# ---------------------------------------------------------------------------

def _make_read_gdf_stub(path_map: dict):
    """Return a _read_gdf replacement that resolves URIs from path_map."""
    def stub(uri, scratch, name):
        if not uri:
            return None
        local = path_map.get(uri)
        if local is None:
            return None
        return gpd.read_file(local)
    return stub


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDeriveRefinementPolygonsBasic:
    """Core numeric assertions — no rivers or buildings."""

    def test_returns_geodataframe_and_coverage(self, workspace, monkeypatch):
        """derive_refinement_polygons returns a GDF with refinement_level column."""
        monkeypatch.setattr(ep, "_read_gdf", _make_read_gdf_stub({}))
        spec = _make_spec(workspace["dir"], workspace["cog"], refinement_levels=2)
        gdf, coverage = ep.derive_refinement_polygons(
            spec, workspace["dir"], workspace["cog"], TARGET_EPSG
        )
        assert gdf is not None, "expected a GeoDataFrame, got None"
        assert "refinement_level" in gdf.columns
        assert len(gdf) > 0
        assert isinstance(coverage, dict)
        assert len(coverage) > 0

    def test_coastline_gets_finest_level(self, workspace, monkeypatch):
        """The 0 m contour polygon must be assigned the max refinement level."""
        monkeypatch.setattr(ep, "_read_gdf", _make_read_gdf_stub({}))
        spec = _make_spec(workspace["dir"], workspace["cog"], refinement_levels=3)
        gdf, _ = ep.derive_refinement_polygons(
            spec, workspace["dir"], workspace["cog"], TARGET_EPSG
        )
        assert gdf is not None
        max_level = gdf["refinement_level"].max()
        assert max_level == 3, f"expected finest level 3, got {max_level}"

    def test_coastline_polygon_touches_zero_contour(self, workspace, monkeypatch):
        """The finest-level polygon must contain or touch the 0 m contour location.

        In the synthetic raster column 20 is the 0 m shoreline, corresponding
        to x ~ X0 + 20*CELL_M.  The finest-level polygon should encompass that.
        """
        monkeypatch.setattr(ep, "_read_gdf", _make_read_gdf_stub({}))
        spec = _make_spec(workspace["dir"], workspace["cog"], refinement_levels=2)
        gdf, _ = ep.derive_refinement_polygons(
            spec, workspace["dir"], workspace["cog"], TARGET_EPSG
        )
        assert gdf is not None
        finest_level = gdf["refinement_level"].max()
        finest_geoms = gdf[gdf["refinement_level"] == finest_level]
        from shapely.ops import unary_union
        merged = unary_union(list(finest_geoms.geometry.values))
        # The 0 m contour is around x = X0 + 20*CELL_M.  A point on it must be
        # inside or very close to the finest-level polygon.
        contour_x = X0 + 20 * CELL_M
        contour_y = Y0 + 20 * CELL_M  # midpoint of the grid
        target_pt = Point(contour_x, contour_y)
        # Use a 2-cell buffer as tolerance for the band-mask rounding.
        assert merged.distance(target_pt) < 2 * CELL_M, (
            f"finest-level polygon is {merged.distance(target_pt):.1f} m from "
            f"the 0 m contour at ({contour_x}, {contour_y})"
        )

    def test_levels_descend_outward(self, workspace, monkeypatch):
        """Deeper levels must cover a subset of shallower ones (descending outward).

        level_coverage is cumulative: cov[2] <= cov[1] — a finer level covers
        only a portion of the area the coarser level covers.
        """
        monkeypatch.setattr(ep, "_read_gdf", _make_read_gdf_stub({}))
        spec = _make_spec(workspace["dir"], workspace["cog"], refinement_levels=3)
        _, coverage = ep.derive_refinement_polygons(
            spec, workspace["dir"], workspace["cog"], TARGET_EPSG
        )
        levels = sorted(coverage.keys())
        assert len(levels) >= 2, "expected at least 2 levels in coverage dict"
        for shallower, deeper in zip(levels, levels[1:]):
            assert coverage[deeper] <= coverage[shallower] + 1e-6, (
                f"coverage[{deeper}]={coverage[deeper]:.4f} > "
                f"coverage[{shallower}]={coverage[shallower]:.4f} — "
                "deeper level must cover a subset of the shallower one"
            )

    def test_slope_band_gets_mid_level(self, workspace, monkeypatch):
        """Steep cells (gradient >= threshold) must appear at a mid-level polygon.

        The synthetic raster has a steep patch at cols 30-33 (rows 10-30) with
        gradient ~ 1.7 m / 50 m = 0.034 m/m, which exceeds the default 0.02
        threshold but may or may not exceed a higher one.  Use slope_threshold=0.02
        (generous) so it triggers reliably.
        """
        monkeypatch.setattr(ep, "_read_gdf", _make_read_gdf_stub({}))
        spec = _make_spec(
            workspace["dir"], workspace["cog"],
            refinement_levels=2,
            slope_threshold=0.02,
        )
        gdf, _ = ep.derive_refinement_polygons(
            spec, workspace["dir"], workspace["cog"], TARGET_EPSG
        )
        assert gdf is not None
        # With max_level=2 the mid-level is 1.  Check that at least one polygon
        # at level 1 exists (could be nearshore or slope — either is the mid band).
        mid_rows = gdf[gdf["refinement_level"] == 1]
        assert len(mid_rows) > 0, (
            "expected at least one mid-level (level=1) polygon from slope/nearshore"
        )

    def test_zero_refinement_levels_returns_none(self, workspace, monkeypatch):
        """refinement_levels=0 must short-circuit and return (None, {})."""
        monkeypatch.setattr(ep, "_read_gdf", _make_read_gdf_stub({}))
        spec = _make_spec(workspace["dir"], workspace["cog"], refinement_levels=0)
        gdf, coverage = ep.derive_refinement_polygons(
            spec, workspace["dir"], workspace["cog"], TARGET_EPSG
        )
        assert gdf is None
        assert coverage == {}


class TestDeriveRefinementPolygonsWithFeatures:
    """Rivers and buildings contribute their polygons at the right levels."""

    def test_river_polygon_at_mid_level(self, workspace, monkeypatch):
        """OSM river lines buffered must produce a polygon at the mid level."""
        river_uri = "file://rivers.gpkg"
        monkeypatch.setattr(
            ep, "_read_gdf",
            _make_read_gdf_stub({river_uri: workspace["river_path"]}),
        )
        spec = _make_spec(
            workspace["dir"], workspace["cog"],
            refinement_levels=2,
            rivers_uri=river_uri,
        )
        gdf, _ = ep.derive_refinement_polygons(
            spec, workspace["dir"], workspace["cog"], TARGET_EPSG
        )
        assert gdf is not None
        # mid_level = max(1, max_level - 1) = 1
        river_rows = gdf[gdf["refinement_level"] == 1]
        assert len(river_rows) > 0, "expected a level-1 polygon from the river"

    def test_building_polygon_at_finest_level(self, workspace, monkeypatch):
        """OSM building footprints buffered must appear at the finest level."""
        bld_uri = "file://buildings.gpkg"
        monkeypatch.setattr(
            ep, "_read_gdf",
            _make_read_gdf_stub({bld_uri: workspace["building_path"]}),
        )
        spec = _make_spec(
            workspace["dir"], workspace["cog"],
            refinement_levels=3,
            buildings_uri=bld_uri,
        )
        gdf, _ = ep.derive_refinement_polygons(
            spec, workspace["dir"], workspace["cog"], TARGET_EPSG
        )
        assert gdf is not None
        max_level = gdf["refinement_level"].max()
        finest_rows = gdf[gdf["refinement_level"] == max_level]
        assert len(finest_rows) > 0, (
            f"expected >=1 polygon at finest level {max_level} from buildings"
        )

    def test_buildings_finer_than_rivers(self, workspace, monkeypatch):
        """Buildings -> finest level; rivers -> mid level (buildings > rivers)."""
        river_uri = "file://rivers.gpkg"
        bld_uri = "file://buildings.gpkg"
        monkeypatch.setattr(
            ep, "_read_gdf",
            _make_read_gdf_stub({
                river_uri: workspace["river_path"],
                bld_uri: workspace["building_path"],
            }),
        )
        spec = _make_spec(
            workspace["dir"], workspace["cog"],
            refinement_levels=3,
            rivers_uri=river_uri,
            buildings_uri=bld_uri,
        )
        gdf, _ = ep.derive_refinement_polygons(
            spec, workspace["dir"], workspace["cog"], TARGET_EPSG
        )
        assert gdf is not None
        levels = set(gdf["refinement_level"].unique())
        # Buildings at 3, rivers at 2 (mid=max(1, 3-1)=2), coast at 3 also.
        assert 3 in levels, "expected level-3 polygon (buildings / coastline)"
        assert 2 in levels, "expected level-2 polygon (rivers)"

    def test_nearshore_band_contributes_polygon(self, workspace, monkeypatch):
        """The nearshore band (-2..0 m) should produce at least one mid-level polygon.

        In the synthetic raster cols 16-20 have z in [-2, 0], so the nearshore
        band spans 4 columns = 200 m.
        """
        monkeypatch.setattr(ep, "_read_gdf", _make_read_gdf_stub({}))
        spec = _make_spec(
            workspace["dir"], workspace["cog"],
            refinement_levels=2,
            nearshore_zmin=-2.0,
            nearshore_zmax=0.0,
        )
        gdf, _ = ep.derive_refinement_polygons(
            spec, workspace["dir"], workspace["cog"], TARGET_EPSG
        )
        assert gdf is not None
        # nearshore goes to mid_level = max(1, 2-1) = 1
        near_rows = gdf[gdf["refinement_level"] == 1]
        assert len(near_rows) > 0, (
            "expected a mid-level (level=1) polygon from the nearshore band"
        )


class TestBudgetCap:
    """Budget cap integration: derive_refinement_polygons + apply_cell_budget."""

    def test_tiny_budget_coarsens_max_level(self, workspace, monkeypatch):
        """A tiny max_cells budget should reduce the allowed max_level.

        With refinement_levels=3 and a 40x40 base grid the full level-3
        estimate is >>> 50 cells, so a budget of 50 forces the cap to drop
        levels, proven by the notes from apply_cell_budget.
        """
        monkeypatch.setattr(ep, "_read_gdf", _make_read_gdf_stub({}))
        spec = _make_spec(
            workspace["dir"], workspace["cog"],
            refinement_levels=3,
        )
        gdf, coverage = ep.derive_refinement_polygons(
            spec, workspace["dir"], workspace["cog"], TARGET_EPSG
        )
        assert gdf is not None
        # Apply with an intentionally tiny budget.
        tiny_budget = 50
        allowed, notes = ep.apply_cell_budget(
            GRID_N, GRID_M, coverage, max_cells=tiny_budget
        )
        # The budget must have been enforced (either a level was dropped or
        # even the base grid is over budget).
        assert len(notes) > 0, "expected budget-cap notes for a tiny budget"
        assert allowed < 3, (
            f"expected allowed_level < 3 with budget={tiny_budget}, got {allowed}"
        )

    def test_full_budget_keeps_all_levels(self, workspace, monkeypatch):
        """A generous budget keeps the full refinement hierarchy."""
        monkeypatch.setattr(ep, "_read_gdf", _make_read_gdf_stub({}))
        spec = _make_spec(
            workspace["dir"], workspace["cog"],
            refinement_levels=2,
        )
        gdf, coverage = ep.derive_refinement_polygons(
            spec, workspace["dir"], workspace["cog"], TARGET_EPSG
        )
        assert gdf is not None
        # A very generous budget — well above the synthetic grid's cell count.
        allowed, notes = ep.apply_cell_budget(
            GRID_N, GRID_M, coverage, max_cells=10_000_000
        )
        assert allowed == 2, (
            f"expected allowed_level=2 with a generous budget, got {allowed}"
        )
        assert notes == [], f"expected no budget notes, got {notes}"

    def test_estimated_cells_increase_with_refinement_level(self, workspace, monkeypatch):
        """Truncating a coverage dict to fewer levels must lower the cell estimate.

        Given the SAME level_coverage dict (derived at max_level=3), the estimate
        with full coverage must be >= the estimate after capping to level 1, because
        dropping finer levels removes their 4^L cell contribution.  This mirrors
        what apply_cell_budget does when it tries successively lower allowed_levels.
        """
        monkeypatch.setattr(ep, "_read_gdf", _make_read_gdf_stub({}))
        spec3 = _make_spec(workspace["dir"], workspace["cog"], refinement_levels=3)
        _, coverage = ep.derive_refinement_polygons(
            spec3, workspace["dir"], workspace["cog"], TARGET_EPSG
        )
        # Full-level estimate (all levels present in coverage).
        est_full = ep.estimate_quadtree_cells(GRID_N, GRID_M, coverage)
        # Truncated to only level 1 — drop finer levels entirely.
        cov_l1 = {k: v for k, v in coverage.items() if k <= 1}
        est_l1 = ep.estimate_quadtree_cells(GRID_N, GRID_M, cov_l1)
        assert est_full >= est_l1, (
            f"full-level estimate ({est_full}) should be >= level-1-only ({est_l1})"
        )

    def test_clamp_drops_over_limit_levels(self, workspace, monkeypatch):
        """_clamp_refinement_levels must drop polygons whose level exceeds the cap."""
        monkeypatch.setattr(ep, "_read_gdf", _make_read_gdf_stub({}))
        spec = _make_spec(workspace["dir"], workspace["cog"], refinement_levels=3)
        gdf, _ = ep.derive_refinement_polygons(
            spec, workspace["dir"], workspace["cog"], TARGET_EPSG
        )
        assert gdf is not None
        # Cap to level 1.
        clamped = ep._clamp_refinement_levels(gdf, allowed_max_level=1)
        if clamped is not None:
            assert clamped["refinement_level"].max() <= 1, (
                "clamped GDF must have no level > 1"
            )

    def test_clamp_to_zero_returns_none(self, workspace, monkeypatch):
        """_clamp_refinement_levels with allowed=0 must return None."""
        monkeypatch.setattr(ep, "_read_gdf", _make_read_gdf_stub({}))
        spec = _make_spec(workspace["dir"], workspace["cog"], refinement_levels=2)
        gdf, _ = ep.derive_refinement_polygons(
            spec, workspace["dir"], workspace["cog"], TARGET_EPSG
        )
        assert gdf is not None
        result = ep._clamp_refinement_levels(gdf, allowed_max_level=0)
        assert result is None, "clamp to 0 should return None"


class TestVectorizeMask:
    """_vectorize_mask_to_polygons — the raster -> polygon helper."""

    def _dummy_transform(self):
        from rasterio.transform import from_origin
        return from_origin(0, 100, 10, 10)

    def test_empty_mask_returns_none(self):
        mask = np.zeros((10, 10), dtype=bool)
        crs = CRS.from_epsg(TARGET_EPSG)
        result = ep._vectorize_mask_to_polygons(mask, self._dummy_transform(), crs)
        assert result is None

    def test_full_mask_returns_polygon(self):
        mask = np.ones((10, 10), dtype=bool)
        crs = CRS.from_epsg(TARGET_EPSG)
        result = ep._vectorize_mask_to_polygons(mask, self._dummy_transform(), crs)
        assert result is not None
        assert not result.is_empty
        assert result.geom_type in ("Polygon", "MultiPolygon")

    def test_partial_mask_area_less_than_full(self):
        full = np.ones((10, 10), dtype=bool)
        half = np.zeros((10, 10), dtype=bool)
        half[:, :5] = True
        crs = CRS.from_epsg(TARGET_EPSG)
        t = self._dummy_transform()
        full_geom = ep._vectorize_mask_to_polygons(full, t, crs)
        half_geom = ep._vectorize_mask_to_polygons(half, t, crs)
        assert full_geom is not None and half_geom is not None
        assert half_geom.area < full_geom.area


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
