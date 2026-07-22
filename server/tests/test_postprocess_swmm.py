"""Targeted tests for the SWMM run-output postprocessor (sprint-16 P3,
``trid3nt_server.workflows.postprocess_swmm``).

The SWMM analogue of ``test_postprocess_flood`` / ``test_postprocess_modflow``.
SWMM emits NODE/LINK results, NOT a raster, so the postprocessor reads the
per-timestep node ``INVERT_DEPTH`` from the ``.out`` (pyswmm ``Output`` API),
scatters each node depth onto the mesh-cell grid, and emits the SAME
``(layers, metrics)`` shape ``postprocess_flood`` returns (peak primary + per-
frame context). These tests pin:

1. **Pure scatter** — ``scatter_node_depths_to_grid`` places ``S_{i}_{j}`` depth
   onto ``grid[i,j]``, skips ``OUT`` / non-cell names, and masks dropped (no
   node) + sub-``NODATA_DEPTH_M`` cells to NaN. (no SWMM needed)
2. **Pure metrics** — ``compute_swmm_depth_metrics`` computes max / area /
   building-count from a synthetic peak grid. (no SWMM needed)
3. **End-to-end on a real synthetic deck** — build a tiny quasi-2D SWMM mesh,
   RUN it headless, then postprocess (upload stubbed): assert the EXACT
   postprocess_flood return shape — ``layers[0]`` peak ``SWMMDepthLayerURI``
   (role primary, "Peak flood depth", continuous_flood_depth) + ``layers[1:]``
   per-frame (role context, contiguous "Flood depth step N", distinct URIs,
   <= MAX_FLOOD_FRAMES) — and that the peak COG is a VALID EPSG:4326 COG with the
   narration scalars set.

pyswmm + swmm-api + rasterio are required for test (3); skipped if absent. Tests
(1)+(2) need only numpy.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from trid3nt_server.workflows.postprocess_swmm import (
    MAX_FLOOD_FRAMES,
    NODATA_DEPTH_M,
    PostprocessSWMMError,
    compute_swmm_depth_metrics,
    postprocess_swmm,
    scatter_node_depths_to_grid,
)

# The web parseFrameToken regex (3rd FRAME_PATTERNS in LayerPanel.tsx) — the
# frame NAMES must match it or the sequential group never forms. Replicated so a
# name-format drift fails loudly on the Python side (same guard as
# test_postprocess_flood).
_WEB_STEP_TOKEN_RE = re.compile(r"\b(?:step|frame|idx|index)\s*\+?(\d{1,4})\b", re.I)


# ===========================================================================
# (1) Pure scatter — no SWMM dependency.
# ===========================================================================
def test_scatter_places_depths_and_skips_non_cells():
    """S_{i}_{j} -> grid[i,j]; OUT / non-cell names skipped; dropped cells stay
    NaN; sub-threshold cells masked to NaN."""
    snapshot = {
        "S_0_0": 0.50,   # wet -> kept
        "S_1_1": 0.02,   # sub-threshold -> NaN
        "S_2_2": 1.30,   # wet -> kept
        "OUT": 9.99,     # boundary outfall -> skipped (not a mesh cell)
        "L_0_0__1_0": 5.0,  # a conduit name -> skipped
    }
    grid = scatter_node_depths_to_grid(snapshot, (3, 3))
    assert grid.shape == (3, 3)
    assert grid[0, 0] == pytest.approx(0.50)
    assert grid[2, 2] == pytest.approx(1.30)
    # sub-threshold S_1_1 masked to NaN.
    assert np.isnan(grid[1, 1])
    # a DROPPED building cell (no node, e.g. (0,1)) stays NaN.
    assert np.isnan(grid[0, 1])
    # threshold is exactly NODATA_DEPTH_M (a cell at the threshold is kept).
    g2 = scatter_node_depths_to_grid({"S_0_0": NODATA_DEPTH_M}, (1, 1))
    assert g2[0, 0] == pytest.approx(NODATA_DEPTH_M)


def test_scatter_clamps_out_of_range_indices():
    """A node index outside the grid is ignored (no IndexError, no write)."""
    grid = scatter_node_depths_to_grid({"S_9_9": 1.0, "S_0_0": 0.7}, (2, 2))
    assert grid[0, 0] == pytest.approx(0.7)
    assert np.count_nonzero(~np.isnan(grid)) == 1  # only the in-range cell


# ===========================================================================
# (2) Pure metrics — no SWMM dependency.
# ===========================================================================
def test_metrics_on_synthetic_peak_grid():
    """max / mean / p95 / flooded_cell_count / flooded_area_km2 from a known grid."""
    grid = np.full((4, 4), np.nan)
    grid[0, 0] = 1.0
    grid[0, 1] = 2.0
    grid[1, 1] = 3.0  # max
    # 3 wet cells; 10 m cells -> 3 * 100 m^2 = 300 m^2 = 3e-4 km^2.
    m = compute_swmm_depth_metrics(grid, resolution_m=10.0)
    assert m["max_depth_m"] == pytest.approx(3.0)
    assert m["flooded_cell_count"] == 3
    assert m["flooded_area_km2"] == pytest.approx(300.0 / 1_000_000.0)
    assert m["mean_depth_m"] == pytest.approx(2.0)
    # no footprints supplied -> honest 0 building count.
    assert m["n_buildings_affected"] == 0


def test_metrics_all_dry_grid_is_zeroed():
    """An all-NaN (dry) grid yields zeroed scalars, never a NaN or a crash."""
    grid = np.full((3, 3), np.nan)
    m = compute_swmm_depth_metrics(grid, resolution_m=5.0)
    assert m["max_depth_m"] == 0.0
    assert m["flooded_area_km2"] == 0.0
    assert m["flooded_cell_count"] == 0
    assert m["n_buildings_affected"] == 0


def test_buildings_affected_counts_footprints_over_wet_cells():
    """A footprint over a WET cell counts toward n_buildings_affected; a footprint
    over a DRY cell does not (rasterize-onto-grid + wet-mask intersection)."""
    rio = pytest.importorskip("rasterio")
    from rasterio.transform import from_origin, xy
    from rasterio.warp import transform as warp_transform

    ox, oy, cell = 500000.0, 4000000.0, 10.0
    t = from_origin(ox, oy, cell, cell)
    grid = np.full((5, 5), np.nan)
    grid[2, 2] = 0.8  # the only wet cell

    def _footprint_over_cell(i: int, j: int) -> dict:
        x, y = xy(t, i, j)
        lons, lats = warp_transform("EPSG:32616", "EPSG:4326", [x], [y])
        lon, lat, d = lons[0], lats[0], 0.00006
        ring = [
            [lon - d, lat - d], [lon + d, lat - d],
            [lon + d, lat + d], [lon - d, lat + d], [lon - d, lat - d],
        ]
        return {
            "type": "FeatureCollection",
            "features": [
                {"type": "Feature", "properties": {},
                 "geometry": {"type": "Polygon", "coordinates": [ring]}}
            ],
        }

    m_wet = compute_swmm_depth_metrics(
        grid, resolution_m=cell, building_footprints=_footprint_over_cell(2, 2),
        grid_crs="EPSG:32616", grid_transform=t,
    )
    assert m_wet["n_buildings_affected"] == 1

    m_dry = compute_swmm_depth_metrics(
        grid, resolution_m=cell, building_footprints=_footprint_over_cell(0, 4),
        grid_crs="EPSG:32616", grid_transform=t,
    )
    assert m_dry["n_buildings_affected"] == 0


# ===========================================================================
# (3) End-to-end on a real synthetic deck.
# ===========================================================================
swmm_api = pytest.importorskip("swmm_api")
pyswmm = pytest.importorskip("pyswmm")
rasterio = pytest.importorskip("rasterio")

from trid3nt_server.workflows.swmm_mesh_builder import (  # noqa: E402
    build_swmm_mesh,
    run_swmm_deck,
)

_N = 14  # small grid -> fast solve
_CELL = 10.0
_EPSG = 32616  # UTM 16N (valid projected metres)
_OX, _OY = 500000.0, 4000000.0


def _write_dem_geotiff(path: Path) -> None:
    """Tilted plane draining to the low corner + a central pit (P0-spike shape)."""
    from rasterio.crs import CRS
    from rasterio.transform import from_origin

    ii, jj = np.meshgrid(np.arange(_N), np.arange(_N), indexing="ij")
    plane = 30.0 - 0.02 * _CELL * (ii + jj)
    ci = cj = (_N - 1) / 2.0
    pit = 2.0 * np.exp(-((ii - ci) ** 2 + (jj - cj) ** 2) / (2.0 * 3.0**2))
    dem = (plane - pit).astype("float32")
    profile = {
        "driver": "GTiff",
        "dtype": "float32",
        "count": 1,
        "height": _N,
        "width": _N,
        "crs": CRS.from_epsg(_EPSG),
        "transform": from_origin(_OX, _OY, _CELL, _CELL),
        "nodata": -9999.0,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(dem, 1)


@pytest.fixture()
def solved_run(tmp_path: Path):
    """Build + RUN a tiny quasi-2D SWMM deck; return (build, run)."""
    dem_path = tmp_path / "dem.tif"
    _write_dem_geotiff(dem_path)
    build = build_swmm_mesh(
        dem_path=str(dem_path),
        out_inp_path=str(tmp_path / "mesh.inp"),
        total_rain_depth_mm=120.0,
        storm_duration_hr=1.0,  # short storm keeps the test fast
        rain_interval_min=5,
        target_resolution_m=10.0,
        building_footprints=None,
        building_representation="drop",
        infiltration_method="none",
        barriers=None,
        enable_autoscale=False,
    )
    # generous tolerance: this tiny deck's continuity can exceed 5% but we only
    # need a real .out to postprocess (the gate is proven in the builder tests).
    run = run_swmm_deck(build, mass_balance_tolerance_pct=100.0)
    return build, run


def _fake_upload(local_cog, run_id, runs_bucket=None, *, dest_filename="swmm_depth_peak.tif"):  # noqa: ANN001
    return f"gs://test-runs/{run_id}/{dest_filename}"


def test_postprocess_swmm_emits_peak_plus_frames(solved_run):
    """postprocess_swmm returns the EXACT postprocess_flood shape: layers[0] peak
    primary + layers[1:] contiguous 'Flood depth step N' context frames, all
    SWMMDepthLayerURI with the narration scalars set."""
    from trid3nt_contracts.swmm_contracts import SWMMDepthLayerURI

    build, run = solved_run
    with patch(
        "trid3nt_server.workflows.postprocess_swmm._upload_cog_to_runs_bucket",
        side_effect=_fake_upload,
    ):
        layers, metrics = postprocess_swmm(run, build, run_id="run-swmm")

    # --- layers[0] = peak primary, the postprocess_flood-identical contract ---
    peak = layers[0]
    assert isinstance(peak, SWMMDepthLayerURI)
    assert peak.name == "Peak flood depth"
    assert peak.role == "primary"
    assert peak.layer_id == "swmm-depth-peak-run-swmm"
    assert peak.style_preset == "continuous_flood_depth"
    assert peak.layer_type == "raster"
    assert peak.units == "meters"
    # narration scalars are typed + non-negative (Invariant 1 / FR-AS-7).
    assert peak.max_depth_m >= 0.0
    assert peak.flooded_area_km2 >= 0.0
    assert peak.n_buildings_affected >= 0
    # metrics dict carries the peak aggregates + 4326 crs tag.
    assert metrics["max_depth_m"] == pytest.approx(peak.max_depth_m)
    assert metrics["crs"] == "EPSG:4326"

    # --- layers[1:] = per-frame context, contiguous "Flood depth step N" ------
    frames = layers[1:]
    assert len(frames) >= 2, f"expected a multi-frame group; got {len(frames)}"
    assert len(frames) <= MAX_FLOOD_FRAMES
    assert all(isinstance(f, SWMMDepthLayerURI) for f in frames)
    assert all(f.role == "context" for f in frames)
    assert all(f.style_preset == "continuous_flood_depth" for f in frames)
    names = [f.name for f in frames]
    assert names == [f"Flood depth step {i}" for i in range(1, len(frames) + 1)]
    # each name matches the web step-token regex (the grouping contract).
    for name in names:
        assert _WEB_STEP_TOKEN_RE.search(name) is not None, name
    # DISTINCT uris (distinct runs-bucket keys -> distinct identity key).
    uris = [f.uri for f in frames]
    assert len(set(uris)) == len(uris)
    assert peak.uri not in uris


def test_peak_cog_is_valid_4326(solved_run):
    """The peak COG written by postprocess_swmm is a VALID EPSG:4326 COG (the
    TiTiler-wedge / CRS round-trip guard) — captured via a non-uploading stub."""
    build, run = solved_run
    captured: dict[str, Path] = {}

    def _capture_upload(local_cog, run_id, runs_bucket=None, *, dest_filename="swmm_depth_peak.tif"):  # noqa: ANN001
        # copy the COG before postprocess unlinks it so we can re-open it.
        import shutil
        import tempfile as _tf

        keep = Path(_tf.NamedTemporaryFile(suffix="_keep.tif", delete=False).name)
        shutil.copy(str(local_cog), str(keep))
        if dest_filename == "swmm_depth_peak.tif":
            captured["peak"] = keep
        return f"gs://test-runs/{run_id}/{dest_filename}"

    with patch(
        "trid3nt_server.workflows.postprocess_swmm._upload_cog_to_runs_bucket",
        side_effect=_capture_upload,
    ):
        layers, _ = postprocess_swmm(run, build, run_id="run-cog")

    peak_cog = captured["peak"]
    try:
        with rasterio.open(peak_cog) as ds:
            assert ds.crs is not None
            assert ds.crs.to_epsg() == 4326, ds.crs
            assert ds.count == 1
            arr = ds.read(1)
            assert arr.ndim == 2
            # bbox is set on the peak layer from the COG bounds (zoom-to).
            assert layers[0].bbox is not None
            blon = abs(ds.bounds.left)
            assert blon <= 360.0  # geographic sanity (matches the CRS guard)
    finally:
        peak_cog.unlink(missing_ok=True)


def test_missing_out_raises_typed_error(solved_run, tmp_path):
    """A missing .out path surfaces a typed SWMM_OUTPUT_READ_FAILED, not a crash."""
    build, run = solved_run

    class _RunNoOut:
        out_path = str(tmp_path / "does_not_exist.out")

    with pytest.raises(PostprocessSWMMError) as exc:
        postprocess_swmm(_RunNoOut(), build, run_id="run-x")
    assert exc.value.error_code == "SWMM_OUTPUT_READ_FAILED"
