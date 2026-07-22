"""Regression tests for _extract_peak_depth_geotiff Y/X orientation guard (job-0086).

Coverage:
1. ``test_y_ascending_gets_flipped`` — synthetic netCDF with y ascending along rows
   (SFINCS south-at-row-0 convention) → after _extract_peak_depth_geotiff, high
   values land at the SOUTH edge (COG row index = height-1 since row 0 = north).
2. ``test_y_descending_is_idempotent`` — y already descending (north at row 0) →
   guard is a no-op; COG is identical to what a direct write would produce.
3. ``test_metrics_are_flip_invariant`` — max_depth_m / mean_depth_m / p95_depth_m /
   flooded_cell_count are identical regardless of y direction (they're aggregates).
4. ``test_x_descending_gets_flipped`` — synthetic netCDF with x descending along cols
   → belt-and-suspenders X guard fires and the COG columns are east-to-west corrected.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np
import pytest

# These imports are heavy (rasterio, xarray) — mark them skippable in
# environments that lack the deps (the CI .venv-agent has them).
pytest.importorskip("rasterio")
pytest.importorskip("xarray")
pytest.importorskip("numpy")


def _make_sfincs_nc(
    tmp_path: Path,
    *,
    x_vals: list[float],
    y_vals: list[float],
    hmax_pattern: np.ndarray,
    crs_wkt: str = "EPSG:32617",
    filename: str = "sfincs_map.nc",
) -> Path:
    """Write a minimal synthetic SFINCS-style netCDF to ``tmp_path/filename``.

    hmax_pattern shape must be (len(y_vals), len(x_vals)).
    """
    import xarray as xr
    import numpy as np_inner

    assert hmax_pattern.shape == (len(y_vals), len(x_vals)), (
        f"hmax_pattern shape {hmax_pattern.shape} != "
        f"({len(y_vals)}, {len(x_vals)})"
    )

    # Wrap hmax with a singleton time dimension (SFINCS emits (timemax=1, n, m)).
    hmax_3d = hmax_inner = hmax_pattern[np_inner.newaxis, :, :]  # (1, ny, nx)

    ds = xr.Dataset(
        {
            "hmax": xr.DataArray(
                hmax_3d,
                dims=["timemax", "n", "m"],
                attrs={"units": "m"},
            ),
            "crs": xr.DataArray(
                0,
                attrs={
                    "crs_wkt": (
                        # Use pyproj to emit a real WKT for the given EPSG string.
                        # Fallback: store the EPSG string itself (job-0063 path picks it up).
                        _epsg_to_wkt(crs_wkt)
                    ),
                    "grid_mapping_name": "transverse_mercator",
                },
            ),
        },
        coords={
            "x": xr.DataArray(np_inner.array(x_vals, dtype="float64"), dims=["m"]),
            "y": xr.DataArray(np_inner.array(y_vals, dtype="float64"), dims=["n"]),
        },
    )
    out = tmp_path / filename
    ds.to_netcdf(str(out))
    return out


def _epsg_to_wkt(epsg_str: str) -> str:
    """Convert 'EPSG:NNNN' to WKT via pyproj; fall back to the string itself."""
    try:
        import pyproj
        return pyproj.CRS.from_string(epsg_str).to_wkt()
    except Exception:
        return epsg_str


def _make_sfincs_nc_timeseries(
    tmp_path: Path,
    *,
    x_vals: list[float],
    y_vals: list[float],
    n_steps: int,
    crs_wkt: str = "EPSG:32617",
    filename: str = "sfincs_map.nc",
) -> Path:
    """Write a synthetic SFINCS netCDF with TIME-VARYING ``zs(time,n,m)`` + ``zb(n,m)``.

    This is the flood-animation source shape: water-level time series + bed
    level, from which per-frame depth = (zs - zb).clip(0). Depth rises
    monotonically with the time index (step 0 = dry, last step = deepest) so the
    frames are easy to assert on. Also emits ``zsmax`` (= max over time) + ``zb``
    so the PEAK path resolves to zsmax-zb (a genuine max field, not a single
    frame). ``n_steps`` controls the time-dim length.
    """
    import numpy as np_inner
    import xarray as xr

    ny, nx = len(y_vals), len(x_vals)
    # Bed level 0 everywhere; water level ramps 0 -> 3.0 m across the time steps.
    zb = np_inner.zeros((ny, nx), dtype="float32")
    zs = np_inner.zeros((n_steps, ny, nx), dtype="float32")
    for t in range(n_steps):
        # Uniform water level rising with t: 0 at t=0 up to 3.0 at the last step.
        level = 3.0 * (t / max(1, n_steps - 1))
        zs[t, :, :] = level
    zsmax = zs.max(axis=0)  # (ny, nx) — the true peak water level

    ds = xr.Dataset(
        {
            "zs": xr.DataArray(zs, dims=["time", "n", "m"], attrs={"units": "m"}),
            "zsmax": xr.DataArray(zsmax, dims=["n", "m"], attrs={"units": "m"}),
            "zb": xr.DataArray(zb, dims=["n", "m"], attrs={"units": "m"}),
            "crs": xr.DataArray(
                0,
                attrs={
                    "crs_wkt": _epsg_to_wkt(crs_wkt),
                    "grid_mapping_name": "transverse_mercator",
                },
            ),
        },
        coords={
            "x": xr.DataArray(np_inner.array(x_vals, dtype="float64"), dims=["m"]),
            "y": xr.DataArray(np_inner.array(y_vals, dtype="float64"), dims=["n"]),
            "time": xr.DataArray(
                np_inner.arange(n_steps, dtype="int64"), dims=["time"]
            ),
        },
    )
    out = tmp_path / filename
    ds.to_netcdf(str(out))
    return out


# ---------------------------------------------------------------------------
# Shared asymmetric depth pattern.
# High values (3.0 m) at y-index 0 (the low-y / south row in ascending y).
# Zero at y-index 3 (the high-y / north row).
# After the Y-flip, the high values should land at COG row (height-1).
# ---------------------------------------------------------------------------
# Use realistic UTM Zone 17N coordinates (Fort Myers area, EPSG:32617).
# x ≈ 420000 easting, y ≈ 2937000 northing — both well above 1000 so the
# CRS sanity check (projected CRS → |x| > 1000) in postprocess_flood passes.
X_VALS_ASC = [420000.0, 420030.0, 420060.0, 420090.0, 420120.0]  # 5 cols, 30 m spacing
Y_VALS_ASC = [2937000.0, 2937030.0, 2937060.0, 2937090.0]  # 4 rows, south → north
Y_VALS_DESC = [2937090.0, 2937060.0, 2937030.0, 2937000.0]  # 4 rows, north → south

# Row 0 = south (high-y index 0 in ascending convention = lowest y).
# Place a 3.0 m depth block on row 0 (all cols), zero elsewhere.
HMAX_SOUTH_HIGH = np.array(
    [
        [3.0, 3.0, 3.0, 3.0, 3.0],  # row 0: y=0 (south) → high depth
        [1.0, 1.0, 1.0, 1.0, 1.0],  # row 1: y=10
        [0.5, 0.5, 0.5, 0.5, 0.5],  # row 2: y=20
        [0.0, 0.0, 0.0, 0.0, 0.0],  # row 3: y=30 (north) → dry
    ],
    dtype="float32",
)

# Same depths but stored in descending y order (north at row 0) — no flip needed.
HMAX_NORTH_HIGH = HMAX_SOUTH_HIGH[::-1, :].copy()  # row 0 = north = dry


# ---------------------------------------------------------------------------
# Test 1: y ascending → guard fires, high values land at COG south edge
# ---------------------------------------------------------------------------

def test_y_ascending_gets_flipped(tmp_path: Path) -> None:
    """Y-ascending SFINCS data → guard flips rows; deep flood at south edge of COG."""
    from grace2_agent.workflows.postprocess_flood import _extract_peak_depth_geotiff
    import rasterio

    nc = _make_sfincs_nc(
        tmp_path,
        x_vals=X_VALS_ASC,
        y_vals=Y_VALS_ASC,
        hmax_pattern=HMAX_SOUTH_HIGH,
    )

    cog_path, metrics = _extract_peak_depth_geotiff(nc)
    try:
        with rasterio.open(cog_path) as src:
            data = src.read(1)  # shape (height, width)

        height = data.shape[0]
        # The south edge of the COG is at row (height-1) because row 0 = north.
        south_row = data[height - 1, :]  # should be high-depth (≥ 2.5 m)
        north_row = data[0, :]           # should be NaN/dry (≤ NODATA_DEPTH_M)

        # After flip the 3.0 m block is at the south (last row).
        assert np.all(south_row > 2.5), (
            f"Expected south row ≥ 2.5 m (deep flood) after Y-flip; "
            f"got {south_row}"
        )
        # North row should be NaN (masked dry) since original row 3 had 0 m depth.
        assert np.all(np.isnan(north_row)), (
            f"Expected north row to be NaN (dry) after Y-flip; got {north_row}"
        )
    finally:
        Path(cog_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Test 2: y descending → guard is idempotent, no flip
# ---------------------------------------------------------------------------

def test_y_descending_is_idempotent(tmp_path: Path) -> None:
    """Y-descending SFINCS data (north at row 0) → guard is a no-op."""
    from grace2_agent.workflows.postprocess_flood import _extract_peak_depth_geotiff
    import rasterio

    nc = _make_sfincs_nc(
        tmp_path,
        x_vals=X_VALS_ASC,
        y_vals=Y_VALS_DESC,
        hmax_pattern=HMAX_NORTH_HIGH,  # row 0 = north = 0.0 m (dry), row 3 = south = 3.0 m
    )

    cog_path, metrics = _extract_peak_depth_geotiff(nc)
    try:
        with rasterio.open(cog_path) as src:
            data = src.read(1)

        height = data.shape[0]
        south_row = data[height - 1, :]  # row 3 = south → high depth
        north_row = data[0, :]           # row 0 = north → dry

        assert np.all(south_row > 2.5), (
            f"Y-descending: expected south row ≥ 2.5 m; got {south_row}"
        )
        assert np.all(np.isnan(north_row)), (
            f"Y-descending: expected north row NaN (dry); got {north_row}"
        )
    finally:
        Path(cog_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Test 3: Aggregate metrics are flip-invariant
# ---------------------------------------------------------------------------

def test_metrics_are_flip_invariant(tmp_path: Path) -> None:
    """max/mean/p95/flooded_cell_count are identical for ascending vs descending y."""
    from grace2_agent.workflows.postprocess_flood import _extract_peak_depth_geotiff

    nc_asc = _make_sfincs_nc(
        tmp_path,
        x_vals=X_VALS_ASC,
        y_vals=Y_VALS_ASC,
        hmax_pattern=HMAX_SOUTH_HIGH,
        filename="sfincs_asc.nc",
    )
    nc_desc = _make_sfincs_nc(
        tmp_path,
        x_vals=X_VALS_ASC,
        y_vals=Y_VALS_DESC,
        hmax_pattern=HMAX_NORTH_HIGH,
        filename="sfincs_desc.nc",
    )

    _, m_asc = _extract_peak_depth_geotiff(nc_asc)
    _, m_desc = _extract_peak_depth_geotiff(nc_desc)

    for key in ("max_depth_m", "mean_depth_m", "p95_depth_m", "flooded_cell_count"):
        assert m_asc[key] == pytest.approx(m_desc[key], rel=1e-5), (
            f"Metric '{key}' differs between ascending ({m_asc[key]}) and "
            f"descending ({m_desc[key]}) y; must be flip-invariant."
        )


# ---------------------------------------------------------------------------
# Test 4: X descending → belt-and-suspenders X guard fires
# ---------------------------------------------------------------------------

def test_x_descending_gets_flipped(tmp_path: Path) -> None:
    """X-descending SFINCS data (east at col 0) → X-axis guard flips columns."""
    from grace2_agent.workflows.postprocess_flood import _extract_peak_depth_geotiff
    import rasterio

    # x descending: col 0 = east, last col = west (realistic UTM coords)
    x_vals_desc = [420120.0, 420090.0, 420060.0, 420030.0, 420000.0]

    # Place high depth in col 0 (east) of the source array; after X-flip
    # it should land in the west of the COG (col 0 of COG = west).
    hmax_east_high = np.zeros((4, 5), dtype="float32")
    hmax_east_high[:, 0] = 3.0   # col 0 in source = east (descending x)

    nc = _make_sfincs_nc(
        tmp_path,
        x_vals=x_vals_desc,
        y_vals=Y_VALS_DESC,  # Use descending y so Y-guard is no-op
        hmax_pattern=hmax_east_high,
    )

    cog_path, _ = _extract_peak_depth_geotiff(nc)
    try:
        with rasterio.open(cog_path) as src:
            data = src.read(1)
            width = src.width

        # After X-flip, col 0 of COG = west, col (width-1) = east.
        # The high-depth block (originally at east col in source) should now
        # be at col (width-1) of the COG.
        east_col = data[:, width - 1]
        west_col = data[:, 0]

        assert np.any(east_col > 2.5), (
            f"X-descending: expected east col (idx={width-1}) ≥ 2.5 m after X-flip; "
            f"got {east_col}"
        )
        assert np.all(np.isnan(west_col)), (
            f"X-descending: expected west col (idx=0) to be NaN/dry after X-flip; "
            f"got {west_col}"
        )
    finally:
        Path(cog_path).unlink(missing_ok=True)


# ===========================================================================
# Flood-animation Phase 1 (engine-agnostic time-stepped inundation)
# ===========================================================================
#
# postprocess_flood now emits N per-frame depth COGs (one per output timestep)
# as a SINGLE Wave-1 sequential temporal group the web LayerPanel
# (parseFrameToken + detectSequentialGroups + SequenceScrubber) renders with a
# bottom-center scrubber. These tests pin: the per-frame extraction, the <=24
# frame cap + even subsampling, the EXACT web frame-token name format, valid
# per-frame COGs, PEAK-aggregate metrics, and full backward-compat for the
# no-time-dim (hmax/zsmax-only) path that all the legacy fixtures exercise.


# --- Pin the web parseFrameToken contract from the Python side. ------------
# This is the THIRD FRAME_PATTERNS regex in web/src/LayerPanel.tsx:262 —
#   /\b(?:step|frame|idx|index)\s*\+?(\d{1,4})\b/i
# If the frame NAMES don't match it the web group never forms. Replicating the
# regex here makes a name-format drift fail loudly in CI on the Python side.
import re  # noqa: E402

_WEB_STEP_TOKEN_RE = re.compile(r"\b(?:step|frame|idx|index)\s*\+?(\d{1,4})\b", re.I)


def test_frame_names_match_web_parseFrameToken_step_pattern(tmp_path: Path) -> None:
    """Each frame LayerURI.name matches the web 'step N' token + yields strictly
    increasing distinct ints (the contract detectSequentialGroups requires)."""
    from grace2_agent.workflows.postprocess_flood import _extract_depth_frames

    nc = _make_sfincs_nc_timeseries(
        tmp_path, x_vals=X_VALS_ASC, y_vals=Y_VALS_DESC, n_steps=6
    )
    _peak_cog, _peak_metrics, frame_cogs, frame_labels = _extract_depth_frames(nc)
    try:
        assert len(frame_cogs) == 6, f"expected 6 frames; got {len(frame_cogs)}"
        # frame_labels are the provenance labels; the AUTHORITATIVE web token is
        # the LayerURI NAME the caller assigns. Reconstruct that name here exactly
        # as postprocess_flood does: "Flood depth step N" (N=1..k).
        names = [f"Flood depth step {i}" for i in range(1, len(frame_cogs) + 1)]
        values: list[int] = []
        for name in names:
            m = _WEB_STEP_TOKEN_RE.search(name)
            assert m is not None, (
                f"frame name {name!r} does not match the web step token regex "
                f"{_WEB_STEP_TOKEN_RE.pattern!r} — the sequential group will NOT "
                f"form on the web side"
            )
            values.append(int(m.group(1)))
        # Strictly increasing + distinct (detectSequentialGroups requirement).
        assert values == sorted(values), f"frame values not increasing: {values}"
        assert len(set(values)) == len(values), f"frame values not distinct: {values}"
        assert values == list(range(1, len(frame_cogs) + 1)), (
            f"frame values must be contiguous 1..k; got {values}"
        )
    finally:
        for p in frame_cogs:
            Path(p).unlink(missing_ok=True)
        Path(_peak_cog).unlink(missing_ok=True)


def test_per_frame_cogs_are_valid_and_capped(tmp_path: Path) -> None:
    """More raw steps than the cap -> <=MAX_FLOOD_FRAMES frames, evenly subsampled
    with first+last kept; each frame COG round-trips through rasterio with the
    correct CRS tag. (The cap was raised 24 -> 144 for fine coastal/wave cadence;
    this test drives ENOUGH raw steps to exceed whatever the cap is.)"""
    import rasterio
    from grace2_agent.workflows.postprocess_flood import (
        MAX_FLOOD_FRAMES,
        _extract_depth_frames,
    )

    # Always overshoot the cap (whatever its configured value) so the subsample
    # path is exercised: 2*cap+2 raw steps -> must clamp to <=MAX_FLOOD_FRAMES.
    n_steps = MAX_FLOOD_FRAMES * 2 + 2
    nc = _make_sfincs_nc_timeseries(
        tmp_path, x_vals=X_VALS_ASC, y_vals=Y_VALS_DESC, n_steps=n_steps
    )
    peak_cog, peak_metrics, frame_cogs, frame_labels = _extract_depth_frames(nc)
    try:
        assert len(frame_cogs) <= MAX_FLOOD_FRAMES, (
            f"frame count {len(frame_cogs)} exceeds cap {MAX_FLOOD_FRAMES}"
        )
        # Capping must have actually FIRED (fewer frames than raw steps).
        assert len(frame_cogs) < n_steps, "subsample/cap did not fire"
        assert len(frame_cogs) >= 2, "expected a multi-frame group"
        assert len(frame_cogs) == len(frame_labels)

        # Each frame COG must be a VALID COG (rasterio round-trip) with the
        # dataset CRS tag (EPSG:32617) — the TiTiler-wedge guard.
        for fp in frame_cogs:
            with rasterio.open(fp) as src:
                assert src.crs is not None
                assert src.crs.to_epsg() == 32617, (
                    f"frame COG {fp} CRS {src.crs} != EPSG:32617"
                )
                arr = src.read(1)
                assert arr.ndim == 2 and arr.shape == (
                    len(Y_VALS_DESC),
                    len(X_VALS_ASC),
                )

        # First frame (step 1) = the FIRST raw timestep (dry, ~0 m → all masked
        # to NaN); last frame = the LAST raw timestep (deepest). The depth ramps
        # monotonically, so the last frame must carry MORE wet cells than (or
        # equal to) the first — proving endpoints are kept + ordering ascending.
        with rasterio.open(frame_cogs[0]) as a, rasterio.open(frame_cogs[-1]) as b:
            first = a.read(1)
            last = b.read(1)
        first_wet = int(np.count_nonzero(~np.isnan(first)))
        last_wet = int(np.count_nonzero(~np.isnan(last)))
        assert last_wet >= first_wet, (
            f"last frame wet-cell count {last_wet} < first {first_wet}; "
            f"endpoints/ordering wrong"
        )
    finally:
        for p in frame_cogs:
            Path(p).unlink(missing_ok=True)
        Path(peak_cog).unlink(missing_ok=True)


def test_peak_metrics_are_peak_aggregates_not_a_single_frame(tmp_path: Path) -> None:
    """The returned peak_metrics are computed over the PEAK (max-over-time) field,
    NOT a single timestep — guards the FloodMetrics / habitat / Pelicun contract."""
    from grace2_agent.workflows.postprocess_flood import _extract_depth_frames

    nc = _make_sfincs_nc_timeseries(
        tmp_path, x_vals=X_VALS_ASC, y_vals=Y_VALS_DESC, n_steps=8
    )
    peak_cog, peak_metrics, frame_cogs, _labels = _extract_depth_frames(nc)
    try:
        # Water ramps 0 -> 3.0 m; the PEAK (zsmax) is 3.0 m everywhere → max
        # depth ~3.0 m. A single mid-frame would be < 3.0. Assert we got the peak.
        assert peak_metrics["max_depth_m"] == pytest.approx(3.0, rel=1e-3), (
            f"peak max_depth_m {peak_metrics['max_depth_m']} != 3.0 — metrics "
            f"must be the PEAK aggregate, not a single frame"
        )
        assert peak_metrics["flooded_cell_count"] == len(X_VALS_ASC) * len(Y_VALS_DESC)
        assert peak_metrics["crs"] == "EPSG:32617"
    finally:
        for p in frame_cogs:
            Path(p).unlink(missing_ok=True)
        Path(peak_cog).unlink(missing_ok=True)


def test_no_time_dim_falls_back_to_single_peak_layer(tmp_path: Path) -> None:
    """hmax-only fixture (no time dim) → _extract_depth_frames returns ZERO frames
    and postprocess_flood emits EXACTLY ONE layer 'Peak flood depth' (role primary)
    with the legacy depth_metrics. Backward-compat for habitat/Pelicun/honesty-floor."""
    from unittest.mock import patch

    from grace2_agent.workflows.postprocess_flood import (
        _extract_depth_frames,
        postprocess_flood,
    )

    nc = _make_sfincs_nc(
        tmp_path,
        x_vals=X_VALS_ASC,
        y_vals=Y_VALS_DESC,
        hmax_pattern=HMAX_NORTH_HIGH,
    )

    # 1. _extract_depth_frames returns NO frames for the no-time-dim case.
    peak_cog, peak_metrics, frame_cogs, frame_labels = _extract_depth_frames(nc)
    Path(peak_cog).unlink(missing_ok=True)
    assert frame_cogs == [], f"hmax-only fixture must yield no frames; got {frame_cogs}"
    assert frame_labels == []

    # 2. postprocess_flood (with upload stubbed) emits EXACTLY ONE layer, the
    #    primary peak layer — the legacy single-max-COG contract is preserved.
    def _fake_upload(local_cog, run_id, runs_bucket=None, *, dest_filename="flood_depth_peak.tif"):  # noqa: ANN001
        return f"gs://test-runs/{run_id}/{dest_filename}"

    with patch(
        "grace2_agent.workflows.postprocess_flood._upload_cog_to_runs_bucket",
        side_effect=_fake_upload,
    ):
        layers, metrics = postprocess_flood(str(nc), run_id="run-xyz")

    assert len(layers) == 1, (
        f"no-time-dim path must emit exactly ONE layer (the peak); got "
        f"{[l.name for l in layers]}"
    )
    assert layers[0].name == "Peak flood depth"
    assert layers[0].role == "primary"
    assert layers[0].layer_id == "flood-depth-peak-run-xyz"
    assert metrics["max_depth_m"] == pytest.approx(3.0, rel=1e-3)


def test_postprocess_flood_emits_peak_plus_frames_with_distinct_uris(
    tmp_path: Path,
) -> None:
    """postprocess_flood on a time-series fixture emits layers[0]=peak (primary)
    + N frame layers (role context, distinct URIs, 'Flood depth step N' names)."""
    from unittest.mock import patch

    from grace2_agent.workflows.postprocess_flood import postprocess_flood

    nc = _make_sfincs_nc_timeseries(
        tmp_path, x_vals=X_VALS_ASC, y_vals=Y_VALS_DESC, n_steps=5
    )

    def _fake_upload(local_cog, run_id, runs_bucket=None, *, dest_filename="flood_depth_peak.tif"):  # noqa: ANN001
        return f"gs://test-runs/{run_id}/{dest_filename}"

    with patch(
        "grace2_agent.workflows.postprocess_flood._upload_cog_to_runs_bucket",
        side_effect=_fake_upload,
    ):
        layers, metrics = postprocess_flood(str(nc), run_id="run-abc")

    # layers[0] = peak primary (the regression-safe representative).
    assert layers[0].name == "Peak flood depth"
    assert layers[0].role == "primary"
    assert layers[0].layer_id == "flood-depth-peak-run-abc"

    # layers[1:] = 5 frames named "Flood depth step 1..5", role context.
    frames = layers[1:]
    assert len(frames) == 5, f"expected 5 frames; got {[f.name for f in frames]}"
    assert [f.name for f in frames] == [f"Flood depth step {i}" for i in range(1, 6)]
    assert all(f.role == "context" for f in frames)
    assert all(f.style_preset == "continuous_flood_depth" for f in frames)

    # DISTINCT uris (distinct runs-bucket keys) → distinct _layer_identity_key →
    # no dedup collapse → the web group keeps all members.
    uris = [f.uri for f in frames]
    assert len(set(uris)) == len(uris), f"frame uris must be distinct; got {uris}"
    # And distinct from the peak uri.
    assert layers[0].uri not in uris


def _make_sfincs_nc_running_max_blocks(
    tmp_path: Path,
    *,
    x_vals: list[float],
    y_vals: list[float],
    n_steps: int,
    n_maxblocks: int,
    crs_wkt: str = "EPSG:32617",
    filename: str = "sfincs_map.nc",
) -> Path:
    """SFINCS netCDF where ``hmax``/``zsmax`` carry a MULTI-block ``timemax`` axis.

    Reproduces the live Fort Myers 100-yr break: when the deck sets ``dtmaxout``
    finer than the sim window, SFINCS writes a SEQUENCE of running-max snapshots,
    so ``hmax`` arrives as ``(timemax=N, n, m)`` with N>1 (NOT the size-1 global
    max the legacy fixtures use). The representative peak is the max OVER those
    blocks. Also carries the time-varying ``zs(time,n,m)`` so the frame path runs.
    """
    import numpy as np_inner
    import xarray as xr

    ny, nx = len(y_vals), len(x_vals)
    zb = np_inner.zeros((ny, nx), dtype="float32")
    zs = np_inner.zeros((n_steps, ny, nx), dtype="float32")
    for t in range(n_steps):
        zs[t, :, :] = 3.0 * (t / max(1, n_steps - 1))
    # Running-max blocks: each block holds the cumulative max up to that block;
    # the LAST block is the global max (3.0). Shape (timemax, n, m), timemax>1.
    hmax_blocks = np_inner.zeros((n_maxblocks, ny, nx), dtype="float32")
    for b in range(n_maxblocks):
        hmax_blocks[b, :, :] = 3.0 * ((b + 1) / n_maxblocks)
    zsmax_blocks = hmax_blocks.copy()  # zb=0 so depth == level

    ds = xr.Dataset(
        {
            "zs": xr.DataArray(zs, dims=["time", "n", "m"], attrs={"units": "m"}),
            "zb": xr.DataArray(zb, dims=["n", "m"], attrs={"units": "m"}),
            "hmax": xr.DataArray(hmax_blocks, dims=["timemax", "n", "m"], attrs={"units": "m"}),
            "zsmax": xr.DataArray(zsmax_blocks, dims=["timemax", "n", "m"], attrs={"units": "m"}),
            "crs": xr.DataArray(
                0,
                attrs={
                    "crs_wkt": _epsg_to_wkt(crs_wkt),
                    "grid_mapping_name": "transverse_mercator",
                },
            ),
        },
        coords={
            "x": xr.DataArray(np.array(x_vals, dtype="float64"), dims=["m"]),
            "y": xr.DataArray(np.array(y_vals, dtype="float64"), dims=["n"]),
            "time": xr.DataArray(np.arange(n_steps, dtype="int64"), dims=["time"]),
            "timemax": xr.DataArray(np.arange(n_maxblocks, dtype="int64"), dims=["timemax"]),
        },
    )
    out = tmp_path / filename
    ds.to_netcdf(str(out))
    return out


def test_multiblock_running_max_collapses_to_peak_and_emits_frames(
    tmp_path: Path,
) -> None:
    """REGRESSION (live Fort Myers 100-yr, 2026-06-19): ``hmax(timemax=N>1,n,m)``.

    Finer-than-sim ``dtmaxout`` makes SFINCS emit a multi-block running-max field.
    Before the fix ``_select_peak_depth`` returned the 3D ``hmax`` as-is and the
    COG writer's squeeze raised ``RUN_OUTPUT_UNEXPECTED_SHAPE`` — sinking BOTH the
    peak layer AND every animation frame on an otherwise-good solve. The fix
    collapses the ``timemax`` axis to a true 2D global peak.
    """
    from unittest.mock import patch

    from grace2_agent.workflows.postprocess_flood import (
        _extract_depth_frames,
        _select_peak_depth,
        postprocess_flood,
    )

    nc = _make_sfincs_nc_running_max_blocks(
        tmp_path,
        x_vals=X_VALS_ASC,
        y_vals=Y_VALS_DESC,
        n_steps=25,
        n_maxblocks=24,  # the real shape: hmax (timemax=24, n, m)
    )

    # 1. _select_peak_depth must collapse the timemax axis to 2D (n, m).
    import xarray as xr

    with xr.open_dataset(str(nc)) as ds:
        peak_da = _select_peak_depth(ds)
        assert peak_da.ndim == 2, f"peak must be 2D after collapse; got {peak_da.dims}"

    # 2. _extract_depth_frames must NOT raise and must produce peak + frames.
    peak_cog, peak_metrics, frame_cogs, frame_labels = _extract_depth_frames(nc)
    Path(peak_cog).unlink(missing_ok=True)
    for f in frame_cogs:
        Path(f).unlink(missing_ok=True)
    assert peak_metrics["max_depth_m"] == pytest.approx(3.0, rel=1e-3)
    assert len(frame_cogs) >= 2, "time-varying zs must still yield animation frames"

    # 3. End-to-end: peak primary + frames, all with the fixed preset.
    def _fake_upload(local_cog, run_id, runs_bucket=None, *, dest_filename="flood_depth_peak.tif"):  # noqa: ANN001
        return f"s3://test-runs/{run_id}/{dest_filename}"

    with patch(
        "grace2_agent.workflows.postprocess_flood._upload_cog_to_runs_bucket",
        side_effect=_fake_upload,
    ):
        layers, metrics = postprocess_flood(str(nc), run_id="run-fortmyers")

    assert layers[0].name == "Peak flood depth" and layers[0].role == "primary"
    frames = layers[1:]
    assert len(frames) >= 2
    assert all(f.style_preset == "continuous_flood_depth" for f in frames)
