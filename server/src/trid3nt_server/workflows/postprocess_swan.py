"""SWAN wave-field run-output postprocessing (Phase 1).

``postprocess_swan(out_dir, bbox, *, run_id, ...) -> (layers, metrics)`` reads a
solved SWAN run's gridded output (the ``swan_out.mat`` Matlab BLOCK SWAN writes
over the computational grid), rasterizes the significant wave-height field
(``Hsig`` / HSIGN) onto a regular EPSG:4326 grid over the AOI, masks calm /
sub-threshold cells to NaN, and emits the SAME ``(layers, metrics)`` shape as
``postprocess_geoclaw`` / ``postprocess_waves`` so the Phase-1 wave-animation
scrubber path consumes it UNCHANGED:

  - ``layers[0]`` = the PEAK significant-wave-height COG, role ``"primary"``, name
    ``"Peak wave height"``, style preset ``continuous_wave_height``. It is a
    :class:`~trid3nt_contracts.swan_contracts.WaveFieldLayerURI` carrying the four
    narration scalars (``max_hs_m`` / ``mean_tp_s`` / ``mean_dir_deg`` /
    ``wave_area_km2``) + the echoed run mode.
  - ``layers[1:]`` = up to ``MAX_FLOOD_FRAMES`` per-frame Hs COGs, role
    ``"context"``, names ``"Wave height step N"`` -- the EXACT web
    ``parseFrameToken`` / ``detectSequentialGroups`` token (the SAME stem the
    SnapWave ``postprocess_waves`` emits) so the LayerPanel collapses them into one
    bottom-center-scrubber temporal group. Each frame lands at a DISTINCT
    runs-bucket key (distinct TiTiler url) -> no dedup collapse.

This is the SWAN sibling of ``postprocess_waves`` (SnapWave) and
``postprocess_geoclaw``. SWAN writes a REGULAR-grid output (the BLOCK over the
computational grid), so -- unlike GeoClaw's AMR patches or SFINCS's quadtree faces
-- there is NO unstructured rasterization: the Hs array is already a regular
``(my, mx)`` grid we drop straight onto an EPSG:4326 COG over the bbox (simpler
than the AMR / quadtree cases).

Honesty floor (Invariant 1 / FR-AS-7): the wave scalars are computed with plain
arithmetic from the Hs / period / direction grids -- no LLM anywhere; the agent
narrates the typed fields, never invents them. A SWAN run that produced an empty /
all-calm wave field raises ``SWAN_OUTPUT_EMPTY`` -- it NEVER publishes a
silently-wrong layer that reads ``status=ok``.

Tier separation (Invariant 5): the COG lands in the runs bucket (scheme-aware via
``cache.storage_scheme()``); the agent does not re-render -- ``publish_layer`` /
TiTiler serves the tiles from the URI on the envelope.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from trid3nt_contracts.swan_contracts import (
    SWAN_WAVE_HEIGHT_STYLE_PRESET,
    WaveFieldLayerURI,
)

# Reuse the SFINCS postprocess constants/helpers (single source of truth so the
# SWAN + SnapWave + GeoClaw animation paths stay byte-compatible on the web side).
from .postprocess_flood import (
    MAX_FLOOD_FRAMES,
    RUNS_BUCKET_DEFAULT,
    _select_frame_time_indices,
)
from .postprocess_waves import NODATA_WAVE_M

__all__ = [
    "PostprocessSwanError",
    "postprocess_swan",
    "read_swan_mat_fields",
    "compute_swan_wave_metrics",
    "SWAN_WAVE_HEIGHT_STYLE_PRESET",
    "NODATA_WAVE_M",
    "MAX_FLOOD_FRAMES",
    "SWAN_MAT_OUTPUT",
]

logger = logging.getLogger("trid3nt_server.workflows.postprocess_swan")

#: The SWAN BLOCK output Matlab file the deck author targets (deck_builder
#: OUTPUT_MAT_FILENAME). The postprocess discovers it under the run output dir.
SWAN_MAT_OUTPUT: str = "swan_out.mat"

#: SWAN's exception (no-data / dry / calm) sentinel written via SET EXCEPTION in
#: the deck (deck_builder.SWAN_EXCEPTION_VALUE). Cells equal to it are masked.
_SWAN_EXCEPTION_VALUE: float = -999.0

#: Minimum COG pixel dimension (the larger of width/height) the Hs raster is
#: upsampled to before write. SWAN's computational mesh is COARSE (the v0.1 deck
#: is a fixed (100, 100) grid -> a 101x101 BLOCK output), and a COG that small
#: ships with NO internal overviews (the GDAL COG driver only builds overviews
#: once a dimension exceeds the block size, and ``publish_layer``'s F33
#: overview-enforcement also no-ops below its 256px threshold). A no-overview COG
#: is exactly the "renders spotty / never paints" class: TiTiler reports a
#: single-level tilejson zoom window (min==max), so the MapLibre raster source
#: paints reliably only inside that 1-level band and cold-loads time out. We
#: NEAREST-NEIGHBOUR expand the masked Hs grid onto a denser grid so the COG is
#: large enough for the COG driver to build overviews and TiTiler reports a real
#: multi-level zoom window -- the SAME robustness the SFINCS/GeoClaw flood COGs
#: (hundreds-to-thousands of px wide) get for free. Nearest-neighbour (not
#: interpolation) preserves the calm/wave NaN mask edge EXACTLY and invents no
#: between-cell wave heights -- it is a pure DISPLAY upsample; every narrated
#: scalar is still computed on the NATIVE grid upstream (honesty floor intact).
_COG_MIN_DIM_PX: int = 768

#: Matlab variable-name candidates SWAN writes per quantity (SWAN appends a frame
#: suffix in nonstationary runs, so we match by PREFIX). Hsig is HSIGN; the period
#: var is RTp / Tps / Period; Dir is the mean direction.
_HS_PREFIXES: tuple[str, ...] = ("Hsig", "Hsign", "HSIGN", "Hs")
_TP_PREFIXES: tuple[str, ...] = ("RTp", "RTpeak", "Tps", "Tp", "Period", "TPS", "RTP")
_DIR_PREFIXES: tuple[str, ...] = ("Dir", "PkDir", "Pdir", "DIR", "Theta")


class PostprocessSwanError(RuntimeError):
    """Raised on read / rasterize / COG-write / upload failures.

    ``error_code`` matches the open-set A.6 surface so the agent emitter renders a
    typed error frame. Codes used here:

    - ``SWAN_OUTPUT_READ_FAILED`` -- could not read the SWAN ``swan_out.mat``.
    - ``SWAN_OUTPUT_EMPTY`` -- no SWAN output file / no wave-bearing cells.
    - ``SWAN_DEPENDENCY_MISSING`` -- numpy / scipy / rasterio not importable.
    - ``SWAN_COG_WRITE_FAILED`` -- rasterio could not write the Hs COG.
    - ``SWAN_CRS_TAG_MISMATCH`` -- the COG CRS tag did not round-trip.
    - ``SWAN_COG_UPLOAD_FAILED`` -- the runs-bucket upload of the COG failed.
    """

    error_code: str = "POSTPROCESS_SWAN_FAILED"

    def __init__(
        self,
        error_code: str,
        *,
        message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message or error_code)
        self.error_code = error_code
        self.details: dict[str, Any] = dict(details or {})


# --------------------------------------------------------------------------- #
# SWAN .mat read (scipy.io.loadmat) -> per-frame Hs / Tp / Dir grids.
# --------------------------------------------------------------------------- #
def _match_frame_vars(keys: list[str], prefixes: tuple[str, ...]) -> list[str]:
    """Return matlab var names matching any prefix, sorted (frame-suffix order).

    SWAN writes ``Hsig`` (stationary) or ``Hsig_<frame_suffix>`` (nonstationary)
    per BLOCK dump. We match by prefix and keep stable sorted order so the frame
    sequence is deterministic. Skips matlab metadata keys (``__*__``).
    """
    out: list[str] = []
    for k in keys:
        if k.startswith("__"):
            continue
        for pre in prefixes:
            if k == pre or k.startswith(pre + "_") or k.startswith(pre):
                out.append(k)
                break
    return sorted(set(out))


def read_swan_mat_fields(mat_path: Path) -> dict[str, list[Any]]:
    """Read a SWAN ``swan_out.mat`` into per-frame Hs / Tp / Dir grid lists.

    Returns ``{"hs": [grid, ...], "tp": [grid, ...], "dir": [grid, ...]}`` where
    each grid is an ``(my, mx)`` numpy array (row 0 = south, SWAN's idla layout),
    with SWAN's exception sentinel masked to NaN. The lists are aligned by frame
    index (stationary -> a single frame; nonstationary -> one per BLOCK dump). When
    a period / direction field is absent its list is empty (Hs is the required
    primary field).

    Pure numpy/scipy -- unit-testable on a synthetic .mat (mirrors
    ``parse_fort_q_frame``).
    """
    try:
        import numpy as np
        from scipy.io import loadmat
    except Exception as exc:  # noqa: BLE001
        raise PostprocessSwanError(
            "SWAN_DEPENDENCY_MISSING",
            message=f"numpy/scipy unavailable for SWAN .mat read: {exc}",
            details={"mat_path": str(mat_path)},
        ) from exc

    try:
        mat = loadmat(str(mat_path))
    except Exception as exc:  # noqa: BLE001
        raise PostprocessSwanError(
            "SWAN_OUTPUT_READ_FAILED",
            message=f"scipy could not read {mat_path}: {exc}",
            details={"mat_path": str(mat_path)},
        ) from exc

    keys = list(mat.keys())
    hs_vars = _match_frame_vars(keys, _HS_PREFIXES)
    tp_vars = _match_frame_vars(keys, _TP_PREFIXES)
    dir_vars = _match_frame_vars(keys, _DIR_PREFIXES)

    def _grid(name: str) -> Any:
        arr = np.asarray(mat[name], dtype="float64")
        # Mask SWAN's exception sentinel + any pre-existing NaN.
        arr = np.where(np.isclose(arr, _SWAN_EXCEPTION_VALUE), np.nan, arr)
        return arr

    return {
        "hs": [_grid(n) for n in hs_vars],
        "tp": [_grid(n) for n in tp_vars],
        "dir": [_grid(n) for n in dir_vars],
    }


# --------------------------------------------------------------------------- #
# Pure metric math (unit-testable on a synthetic peak grid).
# --------------------------------------------------------------------------- #
def compute_swan_wave_metrics(
    peak_hs: Any,
    *,
    bbox: tuple[float, float, float, float],
    tp_grid: Any = None,
    dir_grid: Any = None,
) -> dict[str, Any]:
    """Compute the four narration scalars from the PEAK Hs grid (+ optional Tp/Dir).

    Pure arithmetic over the masked Hs grid (calm/sub-threshold already NaN):

      - ``max_hs_m``       global max significant wave height (0.0 if all calm).
      - ``mean_tp_s``      mean peak period over the wave-bearing cells (0.0 when
        no Tp field present -- honest: we cannot narrate a period we did not read).
      - ``mean_dir_deg``   circular mean wave direction over the wave-bearing cells
        (0.0 when no Dir field present). Computed as the atan2 of mean sin/cos so
        the wrap at 0/360 is handled correctly.
      - ``wave_area_km2``  (#wave cells) * mean-cell-area (km^2), cos(lat) corrected.

    Also returns ``mean_hs_m`` / ``p95_hs_m`` / ``wave_cell_count`` for parity with
    the GeoClaw/SnapWave peak_metrics dict.
    """
    import math

    import numpy as np

    hs = np.asarray(peak_hs, dtype="float64")
    # A cell is wave-bearing when Hs is finite AND above the calm threshold.
    wet_mask = np.isfinite(hs) & (hs >= NODATA_WAVE_M)
    wet = hs[wet_mask]

    nrows, ncols = hs.shape
    min_lon, min_lat, max_lon, max_lat = bbox
    mean_lat = 0.5 * (min_lat + max_lat)
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * max(math.cos(math.radians(mean_lat)), 1e-6)
    cell_w_m = ((max_lon - min_lon) / max(ncols, 1)) * m_per_deg_lon
    cell_h_m = ((max_lat - min_lat) / max(nrows, 1)) * m_per_deg_lat
    cell_area_m2 = abs(cell_w_m * cell_h_m)

    if wet.size == 0:
        return {
            "max_hs_m": 0.0,
            "mean_hs_m": 0.0,
            "p95_hs_m": 0.0,
            "wave_cell_count": 0,
            "wave_area_km2": 0.0,
            "mean_tp_s": 0.0,
            "mean_dir_deg": 0.0,
        }

    wave_cell_count = int(wet.size)

    mean_tp = 0.0
    if tp_grid is not None:
        try:
            tp = np.asarray(tp_grid, dtype="float64")
            if tp.shape == hs.shape:
                tp_wet = tp[wet_mask & np.isfinite(tp)]
                if tp_wet.size:
                    mean_tp = float(np.nanmean(tp_wet))
        except Exception:  # noqa: BLE001 -- metric is best-effort
            pass

    mean_dir = 0.0
    if dir_grid is not None:
        try:
            d = np.asarray(dir_grid, dtype="float64")
            if d.shape == hs.shape:
                d_wet = d[wet_mask & np.isfinite(d)]
                if d_wet.size:
                    rad = np.radians(d_wet)
                    mean_dir = float(
                        math.degrees(
                            math.atan2(
                                float(np.mean(np.sin(rad))),
                                float(np.mean(np.cos(rad))),
                            )
                        )
                        % 360.0
                    )
        except Exception:  # noqa: BLE001
            pass

    return {
        "max_hs_m": float(np.nanmax(wet)),
        "mean_hs_m": float(np.nanmean(wet)),
        "p95_hs_m": float(np.nanpercentile(wet, 95)),
        "wave_cell_count": wave_cell_count,
        "wave_area_km2": wave_cell_count * cell_area_m2 / 1_000_000.0,
        "mean_tp_s": mean_tp,
        "mean_dir_deg": mean_dir,
    }


# --------------------------------------------------------------------------- #
# COG write (EPSG:4326 grid) + CRS round-trip guard.
# --------------------------------------------------------------------------- #
def _upsample_for_cog(arr: Any, min_dim_px: int = _COG_MIN_DIM_PX) -> Any:
    """NEAREST-NEIGHBOUR expand a masked Hs grid so its larger side >= ``min_dim_px``.

    SWAN's coarse mesh (a 101x101 BLOCK output for the v0.1 deck) yields a COG too
    small for the GDAL COG driver to build internal overviews, so TiTiler reports a
    1-level (min==max) tilejson zoom window and the layer paints only inside that
    narrow band / times out cold (the no-overview "renders spotty" class). Tiling
    each cell into an integer NxN block (``np.repeat``) makes the raster large
    enough that the COG driver writes overviews -- WITHOUT inventing any data:
    nearest-neighbour preserves the EXACT cell values + the calm/wave NaN edge, so
    no between-cell wave heights are fabricated and the narration scalars (computed
    on the native grid upstream) stand unchanged. No-op when the grid is already
    >= ``min_dim_px`` on its larger side (the SFINCS/GeoClaw-sized case).
    """
    import numpy as np

    a = np.asarray(arr)
    if a.ndim != 2 or a.size == 0:
        return a
    larger = max(a.shape)
    if larger >= min_dim_px:
        return a
    factor = int(np.ceil(min_dim_px / larger))
    if factor <= 1:
        return a
    return np.repeat(np.repeat(a, factor, axis=0), factor, axis=1)


def _write_hs_cog_4326(
    hs_grid: Any,
    bbox: tuple[float, float, float, float],
) -> Path:
    """Write a masked ``(my, mx)`` SWAN Hs grid to an EPSG:4326 COG over ``bbox``.

    SWAN's idla=1 grid is row 0 = south; COGs are row 0 = NORTH, so we FLIP the
    grid vertically before writing. Sub-threshold / exception cells are NaN. The
    masked grid is then NEAREST-NEIGHBOUR upsampled (``_upsample_for_cog``) so the
    COG is large enough for the GDAL COG driver to build internal OVERVIEWS -- a
    coarse SWAN mesh otherwise ships a no-overview COG that renders spotty / never
    paints (TiTiler reports a 1-level zoom window). Re-opens the COG to assert the
    CRS tag round-trips AND that overviews actually landed (the TiTiler-wedge
    guard). Mirrors ``postprocess_geoclaw._write_depth_cog_4326``.
    """
    import numpy as np
    import rasterio
    from rasterio.transform import from_bounds

    arr = np.asarray(hs_grid, dtype="float32")
    # SWAN south-up -> COG north-up.
    arr = np.flipud(arr)
    # Mask anything below the calm threshold (so calm water paints no wave).
    arr = np.where(np.isfinite(arr) & (arr >= NODATA_WAVE_M), arr, np.float32("nan"))
    # Upsample a coarse mesh so the COG gets internal overviews (no data invented;
    # nearest-neighbour preserves the exact values + the calm/wave NaN edge).
    arr = _upsample_for_cog(arr)
    nrows, ncols = arr.shape
    min_lon, min_lat, max_lon, max_lat = bbox
    transform = from_bounds(min_lon, min_lat, max_lon, max_lat, ncols, nrows)
    dst_crs = "EPSG:4326"

    dst_cog = Path(
        tempfile.NamedTemporaryFile(suffix="_swan_hs_4326.tif", delete=False).name
    )
    try:
        # OVERVIEW_RESAMPLING=NEAREST keeps the calm/wave edge crisp in the
        # decimated overviews (averaging would bleed wave into calm cells); the
        # COG driver writes the overviews in the same pass once a dimension
        # exceeds the block size (the upsample above guarantees that).
        profile = {
            "driver": "COG",
            "crs": dst_crs,
            "transform": transform,
            "width": ncols,
            "height": nrows,
            "count": 1,
            "dtype": "float32",
            "nodata": float("nan"),
            "compress": "LZW",
            "overview_resampling": "nearest",
        }
        with rasterio.open(dst_cog, "w", **profile) as dst:
            dst.write(arr, 1)
    except Exception as exc:  # noqa: BLE001
        _safe_unlink(dst_cog)
        raise PostprocessSwanError(
            "SWAN_COG_WRITE_FAILED",
            message=f"Hs COG write failed: {exc}",
            details={"bbox": list(bbox)},
        ) from exc

    # --- CRS round-trip guard (TiTiler-wedge / mistagged-raster) ---
    try:
        with rasterio.open(dst_cog, "r") as verify:
            if str(verify.crs) != dst_crs:
                raise PostprocessSwanError(
                    "SWAN_CRS_TAG_MISMATCH",
                    message=(
                        f"COG written crs={dst_crs!r} but rasterio read back "
                        f"{verify.crs!r}"
                    ),
                    details={"bbox": list(bbox)},
                )
            bounds_max = max(abs(verify.bounds.left), abs(verify.bounds.right))
            if bounds_max > 360:
                raise PostprocessSwanError(
                    "SWAN_CRS_TAG_MISMATCH",
                    message=(
                        f"COG tagged EPSG:4326 but bounds.left={verify.bounds.left} "
                        f"implies projected coords (|x|>360)"
                    ),
                    details={"bbox": list(bbox)},
                )
    except PostprocessSwanError:
        _safe_unlink(dst_cog)
        raise

    return dst_cog


def _safe_unlink(p: Path) -> None:
    try:
        p.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# Upload (scheme-aware) -- mirrors postprocess_geoclaw._upload_cog_to_runs_bucket.
# --------------------------------------------------------------------------- #
def _upload_cog_to_runs_bucket(
    local_cog: Path,
    run_id: str,
    runs_bucket: str | None = None,
    *,
    dest_filename: str = "swan_wave_height_peak.tif",
) -> str:
    """Upload the staged COG to ``{scheme}://<runs_bucket>/<run_id>/<dest_filename>``.

    Scheme-aware via ``cache.storage_scheme()``. Per-frame callers pass a DISTINCT
    ``dest_filename`` so each frame lands at its own object key (its own TiTiler
    url / identity key -> no dedup collapse). Mirrors
    ``postprocess_geoclaw._upload_cog_to_runs_bucket`` exactly.
    """
    from ..tools.cache import storage_scheme

    scheme = storage_scheme()
    if scheme == "s3":
        bucket = runs_bucket or (os.environ.get("TRID3NT_RUNS_BUCKET") or "").strip()
        if not bucket:
            raise PostprocessSwanError(
                "SWAN_COG_UPLOAD_FAILED",
                message=(
                    "TRID3NT_RUNS_BUCKET must be set under "
                    "TRID3NT_STORAGE_BACKEND=s3 (no GCP-named default on AWS)"
                ),
                details={"local_cog": str(local_cog)},
            )
        dest = f"s3://{bucket}/{run_id}/{dest_filename}"
        try:
            from ..tools.solver import _get_s3_client

            with local_cog.open("rb") as fh:
                _get_s3_client().put_object(
                    Bucket=bucket,
                    Key=f"{run_id}/{dest_filename}",
                    Body=fh,
                    ContentType="image/tiff",
                )
        except Exception as exc:  # noqa: BLE001
            raise PostprocessSwanError(
                "SWAN_COG_UPLOAD_FAILED",
                message=f"upload of {local_cog} to {dest} failed: {exc}",
                details={"local_cog": str(local_cog), "dest": dest},
            ) from exc
        logger.info("uploaded SWAN Hs COG to %s (boto3)", dest)
        return dest

    bucket = runs_bucket or os.environ.get("TRID3NT_RUNS_BUCKET", RUNS_BUCKET_DEFAULT)
    dest = f"gs://{bucket}/{run_id}/{dest_filename}"
    try:
        import fsspec  # type: ignore[import-not-found]

        fs = fsspec.filesystem("gcs")
        fs.put(str(local_cog), dest)
    except Exception as exc:  # noqa: BLE001
        raise PostprocessSwanError(
            "SWAN_COG_UPLOAD_FAILED",
            message=f"upload of {local_cog} to {dest} failed: {exc}",
            details={"local_cog": str(local_cog), "dest": dest},
        ) from exc
    logger.info("uploaded SWAN Hs COG to %s", dest)
    return dest


# --------------------------------------------------------------------------- #
# SWAN output discovery.
# --------------------------------------------------------------------------- #
def _discover_mat(out_dir: Path) -> Path | None:
    """Find the SWAN ``swan_out.mat`` under the run output dir (or a subdir)."""
    candidates = [out_dir / SWAN_MAT_OUTPUT]
    sub = out_dir / "_output"
    if sub.is_dir():
        candidates.insert(0, sub / SWAN_MAT_OUTPUT)
    for c in candidates:
        if c.is_file():
            return c
    # Fallback: any .mat under the tree.
    for d in (sub, out_dir):
        if d.is_dir():
            for p in sorted(d.iterdir()):
                if p.is_file() and p.suffix.lower() == ".mat":
                    return p
    return None


# --------------------------------------------------------------------------- #
# Top-level postprocess.
# --------------------------------------------------------------------------- #
def postprocess_swan(
    out_dir: str | Path,
    bbox: tuple[float, float, float, float],
    *,
    run_id: str,
    mode: str = "stationary",
    runs_bucket: str | None = None,
) -> tuple[list[WaveFieldLayerURI], dict[str, Any]]:
    """Rasterize a solved SWAN run into a peak + per-frame Hs-COG layer set.

    Reads the SWAN ``swan_out.mat`` from ``out_dir`` (the downloaded output),
    rasterizes the Hs field per frame onto a regular EPSG:4326 grid over ``bbox``,
    selects the PEAK frame (largest total wave energy), writes the PEAK + up to
    ``MAX_FLOOD_FRAMES`` per-frame Hs COGs, uploads them, and returns the EXACT
    ``(layers, metrics)`` shape ``postprocess_waves`` / ``postprocess_geoclaw``
    return so the Phase-1 scrubber path consumes it unchanged.

    Args:
        out_dir: directory containing the SWAN output (``swan_out.mat``).
        bbox: AOI ``(min_lon, min_lat, max_lon, max_lat)`` EPSG:4326 -- the raster
            extent + zoom-to bbox.
        run_id: the run identifier the COGs are keyed under in the runs bucket.
        mode: the SWAN run mode (echoed onto the layers).
        runs_bucket: optional override for the runs bucket name.

    Returns:
        ``(layers, metrics)``: ``layers[0]`` peak ``WaveFieldLayerURI`` +
        ``layers[1:]`` per-frame; ``metrics`` the peak aggregates dict.

    Raises:
        PostprocessSwanError: any read / rasterize / COG-write / upload failure,
            or ``SWAN_OUTPUT_EMPTY`` when there is no wave field (the honesty floor
            -- a run that produced no waves NEVER reads status ok).
    """
    try:
        import numpy as np
    except Exception as exc:  # noqa: BLE001
        raise PostprocessSwanError(
            "SWAN_DEPENDENCY_MISSING",
            message=f"numpy unavailable for SWAN postprocess: {exc}",
        ) from exc

    out = Path(out_dir)
    mat_path = _discover_mat(out)
    if mat_path is None:
        raise PostprocessSwanError(
            "SWAN_OUTPUT_EMPTY",
            message=f"no SWAN {SWAN_MAT_OUTPUT} output found under {out}",
            details={"out_dir": str(out)},
        )

    fields = read_swan_mat_fields(mat_path)
    hs_frames: list[Any] = fields["hs"]
    tp_frames: list[Any] = fields["tp"]
    dir_frames: list[Any] = fields["dir"]

    if not hs_frames:
        raise PostprocessSwanError(
            "SWAN_OUTPUT_EMPTY",
            message=f"SWAN {mat_path.name} carries no Hsig (HSIGN) wave field",
            details={"mat_path": str(mat_path)},
        )

    # --- PEAK frame = the frame with the largest total finite wave energy. ---
    best_idx = 0
    best_sum = -1.0
    for i, g in enumerate(hs_frames):
        arr = np.asarray(g, dtype="float64")
        s = float(np.nansum(np.where(np.isfinite(arr) & (arr >= NODATA_WAVE_M), arr, 0.0)))
        if s > best_sum:
            best_sum = s
            best_idx = i
    peak_hs = hs_frames[best_idx]
    peak_tp = tp_frames[best_idx] if best_idx < len(tp_frames) else None
    peak_dir = dir_frames[best_idx] if best_idx < len(dir_frames) else None

    metrics = compute_swan_wave_metrics(
        peak_hs, bbox=bbox, tp_grid=peak_tp, dir_grid=peak_dir
    )
    metrics["crs"] = "EPSG:4326"

    # Honesty floor: a run that produced no wave-bearing cells is NOT an OK layer.
    if int(metrics["wave_cell_count"]) == 0:
        raise PostprocessSwanError(
            "SWAN_OUTPUT_EMPTY",
            message=(
                "SWAN solve produced no wave-bearing cells (Hs everywhere below "
                f"the {NODATA_WAVE_M} m calm threshold) -- not a usable wave field"
            ),
            details={"mat_path": str(mat_path), "run_id": run_id},
        )

    n_steps = len(hs_frames)
    logger.info(
        "postprocess_swan run_id=%s mode=%s n_frames=%d max_hs_m=%.4g "
        "mean_tp_s=%.4g mean_dir_deg=%.1f wave_area_km2=%.6g",
        run_id,
        mode,
        n_steps,
        metrics["max_hs_m"],
        metrics["mean_tp_s"],
        metrics["mean_dir_deg"],
        metrics["wave_area_km2"],
    )

    # --- PEAK layer (always layers[0]) ---
    peak_cog = _write_hs_cog_4326(peak_hs, bbox)
    try:
        peak_uri = _upload_cog_to_runs_bucket(
            peak_cog, run_id, runs_bucket, dest_filename="swan_wave_height_peak.tif"
        )
    finally:
        _safe_unlink(peak_cog)

    layers: list[WaveFieldLayerURI] = [
        WaveFieldLayerURI(
            layer_id=f"swan-wave-height-peak-{run_id}",
            name="Peak wave height",
            layer_type="raster",
            uri=peak_uri,
            style_preset=SWAN_WAVE_HEIGHT_STYLE_PRESET,
            role="primary",
            units="meters",
            bbox=tuple(bbox),
            max_hs_m=float(metrics["max_hs_m"]),
            mean_tp_s=float(metrics["mean_tp_s"]),
            mean_dir_deg=float(metrics["mean_dir_deg"]),
            wave_area_km2=float(metrics["wave_area_km2"]),
            mode=mode,  # type: ignore[arg-type]
        )
    ]

    # --- per-frame layers (engine-agnostic wave animation, Phase 1) ---
    if n_steps > 1:
        frame_indices = _select_frame_time_indices(n_steps)
        frame_layers = _emit_frame_layers(
            hs_frames,
            tp_frames,
            dir_frames,
            frame_indices,
            bbox=bbox,
            run_id=run_id,
            runs_bucket=runs_bucket,
            mode=mode,
        )
        if len(frame_layers) >= 2:
            layers.extend(frame_layers)
        else:
            logger.info(
                "postprocess_swan: < 2 frame layers (%d) -- emitting peak only "
                "(no animation group) for run_id=%s",
                len(frame_layers),
                run_id,
            )

    if len(layers) > 1:
        logger.info(
            "postprocess_swan: emitted peak layer + %d time-step frames "
            "(animation group) for run_id=%s",
            len(layers) - 1,
            run_id,
        )
    return layers, metrics


def _emit_frame_layers(
    hs_frames: list[Any],
    tp_frames: list[Any],
    dir_frames: list[Any],
    frame_indices: list[int],
    *,
    bbox: tuple[float, float, float, float],
    run_id: str,
    runs_bucket: str | None,
    mode: str,
) -> list[WaveFieldLayerURI]:
    """Write + upload per-frame Hs COGs as contiguous ``Wave height step N`` layers.

    A single corrupt frame must NOT sink the whole animation OR the peak layer: on
    a frame write/upload failure we clean up the partial frames and return ``[]``
    (the caller degrades to peak-only). Mirrors postprocess_geoclaw.
    """
    frame_layers: list[WaveFieldLayerURI] = []
    written_cogs: list[Path] = []
    try:
        for frame_no, t_idx in enumerate(frame_indices, start=1):
            hs = hs_frames[t_idx]
            tp = tp_frames[t_idx] if t_idx < len(tp_frames) else None
            dr = dir_frames[t_idx] if t_idx < len(dir_frames) else None
            frame_cog = _write_hs_cog_4326(hs, bbox)
            written_cogs.append(frame_cog)
            fm = compute_swan_wave_metrics(hs, bbox=bbox, tp_grid=tp, dir_grid=dr)
            frame_uri = _upload_cog_to_runs_bucket(
                frame_cog,
                run_id,
                runs_bucket,
                dest_filename=f"swan_wave_height_frame_{frame_no:02d}.tif",
            )
            _safe_unlink(frame_cog)
            written_cogs.pop()
            frame_layers.append(
                WaveFieldLayerURI(
                    layer_id=f"swan-wave-height-frame-{frame_no:02d}-{run_id}",
                    name=f"Wave height step {frame_no}",
                    layer_type="raster",
                    uri=frame_uri,
                    style_preset=SWAN_WAVE_HEIGHT_STYLE_PRESET,
                    role="context",
                    units="meters",
                    bbox=tuple(bbox),
                    max_hs_m=float(fm["max_hs_m"]),
                    mean_tp_s=float(fm["mean_tp_s"]),
                    mean_dir_deg=float(fm["mean_dir_deg"]),
                    wave_area_km2=float(fm["wave_area_km2"]),
                    mode=mode,  # type: ignore[arg-type]
                )
            )
    except PostprocessSwanError as exc:
        logger.warning(
            "postprocess_swan: a frame COG write/upload failed (%s); degrading "
            "to peak-only (no animation group).",
            exc,
        )
        for p in written_cogs:
            _safe_unlink(p)
        return []
    return frame_layers
