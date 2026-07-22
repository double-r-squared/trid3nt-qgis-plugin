"""Orchestrator: run the MODFLOW plume + archetype postprocess on LOCAL outputs.

The entry points the MODFLOW worker entrypoint calls after the mf6 solve
(before ``_write_completion``) on the ``--build-spec-uri`` path:

Spill/plume path (no archetype or archetype=None):
  ``run_plume_postprocess`` - reads the LOCAL ``gwt_model.ucn``,
  reprojects to a plume COG, computes metrics, writes publish_manifest.json.

Archetype paths (TRID3NT_MODFLOW_ARCHETYPE_OFFLOAD=1):
  ``run_drawdown_postprocess``            - sustainable_yield: head decline COG
  ``run_dewatering_postprocess``          - mine_dewatering: DRN CBC COG
  ``run_budget_partition_postprocess``    - regional_water_budget: head COG + metrics
  ``run_mounding_postprocess``            - MAR: head rise COG
  ``run_asr_postprocess``                 - ASR: final-step head COG + efficiency
  ``run_wetland_hydroperiod_postprocess`` - wetland_hydroperiod: seasonal range COG

All paths:
  * AGENT-IMPORT-FREE -- never imports ``trid3nt_server.*``.
  * Returns ``ModflowPostprocessResult`` (status + manifest dict + COG paths).
  * Never raises for an expected-empty result -- returns status=error + typed code.
  * The manifest follows the shared ``_raster_postprocess.manifest`` schema.

Ported from ``trid3nt_server.workflows.postprocess_modflow`` (archetype branches):
same readers, same metric math, same honesty gates.  Worker operates on LOCAL
scratch files only; no S3/GCS download logic needed here.
"""

from __future__ import annotations

import glob
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from services.workers._raster_postprocess import manifest as _manifest

LOG = logging.getLogger("trid3nt.worker.modflow_postprocess")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: The MF6 concentration output stem the GWT OC package writes (gwt_adapter
#: registers ``gwt_model.ucn``). Recursive glob captures it wherever it lands.
GWT_UCN_FILENAME: str = "gwt_model.ucn"

#: GWF head output filename (gwt_adapter registers ``gwf_model.hds``).
GWF_HDS_FILENAME: str = "gwf_model.hds"

#: GWF cell-by-cell budget filename (gwt_adapter registers ``gwf_model.cbc``).
GWF_CBC_FILENAME: str = "gwf_model.cbc"

#: Concentration floor (mg/L) below which a cell is NOT counted as plume (and is
#: masked to NaN in the render COG). Byte-identical to the agent's
#: ``postprocess_modflow.PLUME_DETECTION_FLOOR_MGL``.
PLUME_DETECTION_FLOOR_MGL: float = 0.001

#: MF6 inactive/dry-cell sentinel magnitude (1e30). Cells above this absolute
#: value are masked to NaN. Byte-identical to the agent's _MF6_DRY_SENTINEL.
_MF6_DRY_SENTINEL: float = 1e29

#: Default per-cell area (m^2) when the deck georegistration is unavailable
#: (gwt_adapter CELL_SIZE_M == 50 m -> 2500 m^2). Mirrors the agent default.
_DEFAULT_CELL_AREA_M2: float = 2500.0

#: Default fallback grid shape when the deck georegistration is unavailable
#: (mirrors gwt_adapter default 40x40 demo grid).
_DEFAULT_GRID_SHAPE: tuple[int, int] = (40, 40)

#: Internal inter-cell CBC term excluded from the budget headline partition.
_BUDGET_EXCLUDE_FROM_HEADLINE: frozenset[str] = frozenset({"FLOW-JA-FACE"})

# Style preset keys (byte-identical to agent DRAWDOWN_STYLE_PRESET etc.)
PLUME_STYLE_PRESET: str = "continuous_plume_concentration"
DRAWDOWN_STYLE_PRESET: str = "continuous_drawdown_m"
DEWATERING_STYLE_PRESET: str = "continuous_dewatering_rate"
HEAD_STYLE_PRESET: str = "continuous_head_m"
MOUNDING_STYLE_PRESET: str = "continuous_mounding_m"
ASR_STYLE_PRESET: str = "continuous_head_m"
HYDROPERIOD_STYLE_PRESET: str = "continuous_hydroperiod_m"

#: The deterministic COG key written into the deck dir (the entrypoint's ``*.tif``
#: sweep ships it; the manifest points at the uploaded runs-bucket URI).
_PLUME_COG_FILENAME: str = "plume_concentration_4326.tif"


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class ModflowPostprocessResult:
    """What the entrypoint folds into completion.json + writes as the manifest."""

    status: str  # "ok" | "error"
    manifest: dict[str, Any] | None
    metrics: dict[str, Any] = field(default_factory=dict)
    cog_paths: list[Path] = field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None


# ---------------------------------------------------------------------------
# Shared geometry helpers
# ---------------------------------------------------------------------------


def _grid_georegistration(deck_dir: Path) -> dict[str, Any] | None:
    """Read grid origin + cell size + CRS from the deck via flopy.

    Prefers the GWT transport grid; falls back to GWF (GWF-only archetypes) or any
    model. Returns ``None`` if the deck cannot be loaded (identity transform then).
    """
    try:
        import flopy  # type: ignore[import-not-found]

        sim = flopy.mf6.MFSimulation.load(sim_ws=str(deck_dir), verbosity_level=0)
        model = None
        for prefix in ("gwt", "gwf"):
            for mname in sim.model_names:
                if mname.startswith(prefix):
                    model = sim.get_model(mname)
                    break
            if model is not None:
                break
        if model is None and sim.model_names:
            model = sim.get_model(sim.model_names[0])
        if model is None:
            return None
        mg = model.modelgrid
        return {
            "xorigin": float(mg.xoffset),
            "yorigin": float(mg.yoffset),
            "delr": float(mg.delr[0]),
            "delc": float(mg.delc[0]),
            "nrow": int(mg.nrow),
            "ncol": int(mg.ncol),
        }
    except Exception as exc:  # noqa: BLE001
        LOG.warning("could not read deck georegistration from %s: %s", deck_dir, exc)
        return None


def _cog_bbox_4326(cog_path: Path) -> list[float] | None:
    """Return the COG's ``[min_lon, min_lat, max_lon, max_lat]`` (or None)."""
    try:
        import rasterio  # type: ignore[import-not-found]

        with rasterio.open(cog_path) as ds:
            b = ds.bounds
            return [float(b.left), float(b.bottom), float(b.right), float(b.top)]
    except Exception:  # noqa: BLE001
        return None


def _band_stats(cog_path: Path) -> dict[str, Any]:
    """Precompute min/max/percentiles over the COG so the agent skips a read."""
    try:
        import numpy as np  # type: ignore[import-not-found]
        import rasterio  # type: ignore[import-not-found]

        with rasterio.open(cog_path) as ds:
            arr = ds.read(1, masked=True)
        finite = np.asarray(arr.compressed(), dtype="float64")
        finite = finite[np.isfinite(finite)]
        if finite.size == 0:
            return {"min": None, "max": None, "p2": None, "p98": None}
        return {
            "min": float(np.min(finite)),
            "max": float(np.max(finite)),
            "p2": float(np.percentile(finite, 2)),
            "p98": float(np.percentile(finite, 98)),
        }
    except Exception:  # noqa: BLE001
        return {"min": None, "max": None, "p2": None, "p98": None}


# ---------------------------------------------------------------------------
# COG writers
# ---------------------------------------------------------------------------


def _write_cog(
    arr2d: Any,
    model_crs: str,
    geo: dict[str, Any] | None,
    out_path: Path,
    *,
    mask_below_floor: bool = True,
) -> None:
    """Reproject a 2D float32 grid to an EPSG:4326 COG at ``out_path``.

    ``geo`` carries the deck georegistration (xorigin, yorigin, delr, delc, nrow,
    ncol); if None an identity transform is used (the COG is placed at 0,0).
    When ``mask_below_floor=True`` (the plume path) cells <= PLUME_DETECTION_FLOOR_MGL
    are set to NaN so only the plume renders. When False (head/decline/CBC paths)
    the array is written AS-IS (NaN off-grid only; negative recovery cells survive).
    """
    import numpy as np  # type: ignore[import-not-found]
    import rasterio  # type: ignore[import-not-found]
    from rasterio.warp import Resampling, calculate_default_transform
    from rasterio.warp import reproject as _warp_reproject

    arr = np.asarray(arr2d, dtype="float32")
    if mask_below_floor:
        arr = np.where(arr > PLUME_DETECTION_FLOOR_MGL, arr, np.nan).astype("float32")
    nrow, ncol = arr.shape

    if geo is not None:
        west = geo["xorigin"]
        north = geo["yorigin"] + nrow * float(geo["delc"])
        src_transform = rasterio.transform.from_origin(
            west, north, float(geo["delr"]), float(geo["delc"])
        )
    else:
        src_transform = rasterio.Affine.identity()

    src_tmp = out_path.with_suffix(".src.tif")
    try:
        with rasterio.open(
            src_tmp, "w", driver="GTiff", width=ncol, height=nrow, count=1,
            dtype="float32", crs=model_crs, transform=src_transform,
            nodata=float("nan"),
        ) as dst:
            dst.write(arr, 1)
        dst_crs = "EPSG:4326"
        with rasterio.open(src_tmp) as src:
            transform, out_w, out_h = calculate_default_transform(
                src.crs, dst_crs, src.width, src.height, *src.bounds
            )
            profile = {
                "driver": "COG", "crs": dst_crs, "transform": transform,
                "width": out_w, "height": out_h, "count": 1, "dtype": "float32",
                "nodata": float("nan"), "compress": "LZW",
            }
            with rasterio.open(out_path, "w", **profile) as dst2:
                _warp_reproject(
                    source=rasterio.band(src, 1),
                    destination=rasterio.band(dst2, 1),
                    src_transform=src.transform, src_crs=src.crs,
                    dst_transform=transform, dst_crs=dst_crs,
                    resampling=Resampling.bilinear,
                )
    finally:
        try:
            src_tmp.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass


# Keep the plume-specific writer as a thin alias (backward compat with the
# existing run_plume_postprocess call site).
def _write_plume_cog(
    final2d: Any, model_crs: str, geo: dict[str, Any] | None, out_path: Path
) -> None:
    _write_cog(final2d, model_crs, geo, out_path, mask_below_floor=True)


# ---------------------------------------------------------------------------
# Local file locators (worker operates on scratch dir only -- no S3/GCS)
# ---------------------------------------------------------------------------


def _locate_ucn(deck_dir: Path) -> Path | None:
    """Find ``gwt_model.ucn`` (or any ``*.ucn``) under the local deck dir."""
    hits = sorted(glob.glob(str(deck_dir / "**" / GWT_UCN_FILENAME), recursive=True))
    if not hits:
        hits = sorted(glob.glob(str(deck_dir / "**" / "*.ucn"), recursive=True))
    return Path(hits[0]) if hits else None


def _locate_hds(deck_dir: Path) -> Path | None:
    """Find ``gwf_model.hds`` (or any ``*.hds``) under the local deck dir."""
    hits = sorted(glob.glob(str(deck_dir / "**" / GWF_HDS_FILENAME), recursive=True))
    if not hits:
        hits = sorted(glob.glob(str(deck_dir / "**" / "*.hds"), recursive=True))
    return Path(hits[0]) if hits else None


def _locate_cbc(deck_dir: Path) -> Path | None:
    """Find ``gwf_model.cbc`` (or any ``*.cbc``) under the local deck dir."""
    hits = sorted(glob.glob(str(deck_dir / "**" / GWF_CBC_FILENAME), recursive=True))
    if not hits:
        hits = sorted(glob.glob(str(deck_dir / "**" / "*.cbc"), recursive=True))
    return Path(hits[0]) if hits else None


# ---------------------------------------------------------------------------
# GWT concentration readers (spill/plume path)
# ---------------------------------------------------------------------------


def _read_final_concentration(ucn_path: Path) -> Any:
    """Read the FINAL-timestep, max-over-layers concentration grid (mg/L, 2D).

    MF6 GWT concentration output is a binary HEADFILE-format array; flopy reads it
    via ``HeadFile(..., text="CONCENTRATION")``. ``get_data(totim=last)`` returns a
    ``(nlay, nrow, ncol)`` array; we take ``nanmax`` over the layer axis for a 2D
    worst-case (max-over-depth) grid. Byte-faithful to the agent reader.
    """
    import flopy.utils  # type: ignore[import-not-found]
    import numpy as np  # type: ignore[import-not-found]

    cobj = flopy.utils.HeadFile(str(ucn_path), text="CONCENTRATION")
    times = cobj.get_times()
    if not times:
        raise ValueError(f"{ucn_path} carries no concentration timesteps")
    data = cobj.get_data(totim=times[-1])  # (nlay, nrow, ncol)

    arr = np.asarray(data, dtype="float64")
    if arr.ndim == 3:
        final2d = np.nanmax(arr, axis=0)
    elif arr.ndim == 2:
        final2d = arr
    else:
        final2d = np.squeeze(arr)
        if final2d.ndim != 2:
            raise ValueError(
                f"concentration array has shape {arr.shape}; cannot reduce to 2D"
            )
    # MF6 inactive/dry cells are flagged with a large sentinel (1e30). Mask them.
    final2d = np.where(np.abs(final2d) > _MF6_DRY_SENTINEL, np.nan, final2d)
    return final2d


def _compute_plume_metrics(final_grid: Any, cell_area_m2: float) -> tuple[float, float]:
    """(max_concentration_mgl, plume_area_km2) from a 2D conc grid (mg/L).

    Byte-faithful to ``postprocess_modflow.compute_plume_metrics``.
    """
    import numpy as np  # type: ignore[import-not-found]

    arr = np.asarray(final_grid, dtype="float64")
    if arr.size == 0:
        return 0.0, 0.0
    finite = arr[np.isfinite(arr)]
    if finite.size == 0:
        return 0.0, 0.0
    max_conc = max(0.0, float(np.max(finite)))
    plume_cells = int(np.count_nonzero(finite > PLUME_DETECTION_FLOOR_MGL))
    plume_area_km2 = float(plume_cells) * float(cell_area_m2) / 1_000_000.0
    return max_conc, plume_area_km2


# ---------------------------------------------------------------------------
# GWF head readers (archetype paths)
# ---------------------------------------------------------------------------


def _to2d_head(data: Any) -> Any:
    """Reduce a (nlay, nrow, ncol) or 2D head array to 2D, masking sentinels."""
    import numpy as np  # type: ignore[import-not-found]

    a = np.asarray(data, dtype="float64")
    if a.ndim == 3:
        a2 = np.nanmax(a, axis=0)
    elif a.ndim == 2:
        a2 = a
    else:
        a2 = np.squeeze(a)
    return np.where(np.abs(a2) > _MF6_DRY_SENTINEL, np.nan, a2)


def _read_head_grid(hds_path: Path) -> Any:
    """Read the FINAL-timestep, max-over-layers head grid (m, 2D)."""
    import flopy.utils  # type: ignore[import-not-found]

    hobj = flopy.utils.HeadFile(str(hds_path))
    times = hobj.get_times()
    if not times:
        raise ValueError(f"{hds_path} carries no head timesteps")
    return _to2d_head(hobj.get_data(totim=times[-1]))


def _read_head_decline_grid(
    hds_path: Path, *, invert: bool = False
) -> tuple[Any, list[float] | None]:
    """Read the head DECLINE grid + well timeseries.

    Returns (decline_grid_2d, head_decline_timeseries):
      - decline = head(t0) - head(t_last) when invert=False (drawdown).
      - decline = head(t_last) - head(t0) when invert=True (mounding/MAR).
    The timeseries is the per-step decline AT THE PEAK-DECLINE CELL, or None
    when only a single step was saved. Byte-faithful to the agent reader.
    """
    import flopy.utils  # type: ignore[import-not-found]
    import numpy as np  # type: ignore[import-not-found]

    hobj = flopy.utils.HeadFile(str(hds_path))
    times = hobj.get_times()
    if not times:
        raise ValueError(f"{hds_path} carries no head timesteps")
    steps = [_to2d_head(hobj.get_data(totim=t)) for t in times]

    first, last = steps[0], steps[-1]
    decline = (last - first) if invert else (first - last)

    ts: list[float] | None = None
    if len(steps) > 1:
        finite = decline[np.isfinite(decline)]
        if finite.size:
            flat_idx = int(
                np.nanargmax(np.where(np.isfinite(decline), decline, -np.inf))
            )
            r, c = np.unravel_index(flat_idx, decline.shape)
            ts = []
            for step in steps:
                val = (
                    (step[r, c] - first[r, c]) if invert
                    else (first[r, c] - step[r, c])
                )
                ts.append(float(val) if np.isfinite(val) else 0.0)
    return decline, ts


def _read_head_steps(hds_path: Path) -> list[Any]:
    """Read EVERY saved head step into a list of 2D max-over-layers head grids.

    Used by ASR (well-head sawtooth) + wetland hydroperiod (seasonal range).
    Byte-faithful to the agent reader.
    """
    import flopy.utils  # type: ignore[import-not-found]

    hobj = flopy.utils.HeadFile(str(hds_path))
    times = hobj.get_times()
    if not times:
        raise ValueError(f"{hds_path} carries no head timesteps")
    return [_to2d_head(hobj.get_data(totim=t)) for t in times]


def _head_total_duration_days(hds_path: Path) -> float | None:
    """Return the last cumulative totim (total simulation duration, days)."""
    try:
        import flopy.utils  # type: ignore[import-not-found]

        times = flopy.utils.HeadFile(str(hds_path)).get_times()
        return float(times[-1]) if times else None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# GWF CBC readers (archetype paths)
# ---------------------------------------------------------------------------


def _normalize_cbc_record_names(cbc: Any) -> dict[str, str]:
    """Return a {UPPER label -> exact record name} map for a CBC file."""
    out: dict[str, str] = {}
    for r in cbc.get_unique_record_names(decode=True):
        name = r.strip() if isinstance(r, str) else r.strip().decode()
        key = name.upper()
        out.setdefault(key, name)
    return out


def _scatter_cbc_term_grid(
    cbc: Any, record_name: str, nrow: int, ncol: int
) -> Any:
    """Scatter the LAST-timestep CBC budget term onto a 2D (nrow, ncol) grid.

    The ``node`` field is 1-based flat index; we collapse layers onto the same
    2D cell (accumulate). Returns an all-NaN grid when the term has no records.
    """
    import math

    import numpy as np  # type: ignore[import-not-found]

    grid = np.full((nrow, ncol), np.nan, dtype="float64")
    data = cbc.get_data(text=record_name)
    if not data:
        return grid
    last = data[-1]
    try:
        nodes = list(last["node"])
        qvals = list(last["q"])
    except Exception:  # noqa: BLE001
        nodes = [int(r["node"]) for r in last]
        qvals = [float(r["q"]) for r in last]
    cells_per_layer = nrow * ncol
    for node, q in zip(nodes, qvals):
        local = (int(node) - 1) % cells_per_layer
        row = local // ncol
        col = local % ncol
        if 0 <= row < nrow and 0 <= col < ncol:
            cur = grid[row, col]
            grid[row, col] = float(q) if math.isnan(cur) else cur + float(q)
    return grid


def _infer_grid_shape_from_cbc(cbc_path: Path) -> tuple[int, int]:
    """Best-effort (nrow, ncol) from the CBC file header; falls back to (40, 40)."""
    try:
        import flopy.utils  # type: ignore[import-not-found]

        cbc = flopy.utils.CellBudgetFile(str(cbc_path))
        nrow = int(getattr(cbc, "nrow", 0) or 0)
        ncol = int(getattr(cbc, "ncol", 0) or 0)
        if nrow > 0 and ncol > 0:
            return nrow, ncol
    except Exception:  # noqa: BLE001
        pass
    return _DEFAULT_GRID_SHAPE


def _read_cbc_term_grid(
    cbc_path: Path, term: str, nrow: int, ncol: int
) -> Any:
    """Read ONE CBC budget term (e.g. DRN / WEL / RCH) into a 2D signed grid.

    Raises ``ValueError("DEWATER_OUTPUT_EMPTY")`` when the requested term is
    absent from the budget. Byte-faithful to the agent reader.
    """
    import flopy.utils  # type: ignore[import-not-found]

    cbc = flopy.utils.CellBudgetFile(str(cbc_path))
    names = _normalize_cbc_record_names(cbc)
    want = term.strip().upper()
    match = next((exact for key, exact in names.items() if want in key), None)
    if match is None:
        raise ValueError(
            f"DEWATER_OUTPUT_EMPTY: no {want} budget record in {cbc_path}; "
            f"records present: {sorted(names)}"
        )
    return _scatter_cbc_term_grid(cbc, match, nrow, ncol)


def _read_cbc_budget_partition(cbc_path: Path) -> dict[str, float]:
    """Read every CBC term and sum per-cell flux -> per-term total dict.

    Splits each term into _IN (positive/into-aquifer) and _OUT (negative/out)
    legs so a balanced boundary CHD narrates as separate inflow + outflow.
    Byte-faithful to the agent reader.
    """
    import flopy.utils  # type: ignore[import-not-found]
    import numpy as np  # type: ignore[import-not-found]

    cbc = flopy.utils.CellBudgetFile(str(cbc_path))
    names = _normalize_cbc_record_names(cbc)
    if not names:
        raise ValueError(f"BUDGET_OUTPUT_EMPTY: no budget records in {cbc_path}")

    totals: dict[str, float] = {}
    for key, exact in names.items():
        if key in _BUDGET_EXCLUDE_FROM_HEADLINE:
            continue
        try:
            data = cbc.get_data(text=exact)
        except Exception:  # noqa: BLE001
            continue
        if not data:
            continue
        last = data[-1]
        try:
            arr = np.asarray(last["q"], dtype="float64")
        except Exception:  # noqa: BLE001
            try:
                totals[key] = totals.get(key, 0.0) + float(
                    np.nansum(np.asarray(last, dtype="float64"))
                )
            except Exception:  # noqa: BLE001
                pass
            continue
        in_sum = float(np.nansum(arr[arr > 0.0]))
        out_sum = float(np.nansum(arr[arr < 0.0]))
        if abs(in_sum) > 0.0:
            totals[f"{key}_IN"] = totals.get(f"{key}_IN", 0.0) + in_sum
        if abs(out_sum) > 0.0:
            totals[f"{key}_OUT"] = totals.get(f"{key}_OUT", 0.0) + out_sum
        if abs(in_sum) == 0.0 and abs(out_sum) == 0.0:
            totals[key] = totals.get(key, 0.0) + float(np.nansum(arr))
    return totals


def _read_cbc_term_signed_totals(
    cbc_path: Path, term: str
) -> tuple[float, float, float]:
    """Sum a CBC term into (net, in_mag, out_mag) over ALL saved timesteps.

    Returns (0, 0, 0) when the term is absent. Byte-faithful to the agent reader.
    """
    import flopy.utils  # type: ignore[import-not-found]
    import numpy as np  # type: ignore[import-not-found]

    cbc = flopy.utils.CellBudgetFile(str(cbc_path))
    names = _normalize_cbc_record_names(cbc)
    want = term.strip().upper()
    match = next((exact for key, exact in names.items() if want in key), None)
    if match is None:
        return 0.0, 0.0, 0.0
    try:
        data = cbc.get_data(text=match)
    except Exception:  # noqa: BLE001
        return 0.0, 0.0, 0.0
    net = in_mag = out_mag = 0.0
    for rec in data:
        try:
            q = np.asarray(rec["q"], dtype="float64")
        except Exception:  # noqa: BLE001
            q = np.asarray(rec, dtype="float64").ravel()
        q = q[np.isfinite(q)]
        if q.size == 0:
            continue
        net += float(np.sum(q))
        in_mag += float(np.sum(q[q > 0.0]))
        out_mag += float(-np.sum(q[q < 0.0]))
    return net, in_mag, out_mag


# ---------------------------------------------------------------------------
# Pure metric functions (ported from agent; no I/O)
# ---------------------------------------------------------------------------


def compute_drawdown_metrics(decline_grid: Any) -> float:
    """Peak head DECLINE (m, >= 0) from a 2D decline grid."""
    import numpy as np  # type: ignore[import-not-found]

    arr = np.asarray(decline_grid, dtype="float64")
    if arr.size == 0:
        return 0.0
    finite = arr[np.isfinite(arr)]
    if finite.size == 0:
        return 0.0
    return max(0.0, float(np.max(finite)))


def compute_cbc_term_metrics(term_grid: Any) -> tuple[float, int]:
    """(total_magnitude_m3_day, active_cell_count) from a 2D CBC term grid."""
    import numpy as np  # type: ignore[import-not-found]

    arr = np.asarray(term_grid, dtype="float64")
    if arr.size == 0:
        return 0.0, 0
    finite = arr[np.isfinite(arr)]
    if finite.size == 0:
        return 0.0, 0
    return float(np.sum(np.abs(finite))), int(finite.size)


def compute_mounding_metrics(rise_grid: Any) -> float:
    """Peak head RISE (mounding, m, >= 0) from a 2D rise grid."""
    import numpy as np  # type: ignore[import-not-found]

    arr = np.asarray(rise_grid, dtype="float64")
    if arr.size == 0:
        return 0.0
    finite = arr[np.isfinite(arr)]
    if finite.size == 0:
        return 0.0
    return max(0.0, float(np.max(finite)))


def compute_recharged_volume_m3(rch_rate_m3_day: float, duration_days: float) -> float | None:
    """Total recharged volume (m^3) = rate * duration. None when not computable."""
    if not rch_rate_m3_day or not duration_days:
        return None
    rate = float(rch_rate_m3_day)
    days = float(duration_days)
    if rate <= 0.0 or days <= 0.0:
        return None
    return rate * days


def compute_seasonal_head_range_m(
    head_steps: list[Any],
) -> tuple[float, list[float] | None]:
    """Peak seasonal head range (m, >= 0) + at-peak-cell timeseries.

    The peak swing = max-over-time minus min-over-time at the cell with the
    largest seasonal movement. The timeseries is the per-step head at that cell.
    Byte-faithful to the agent reader.
    """
    import numpy as np  # type: ignore[import-not-found]

    if not head_steps:
        return 0.0, None
    stack = np.stack([np.asarray(s, dtype="float64") for s in head_steps], axis=0)
    if stack.size == 0:
        return 0.0, None
    with np.errstate(invalid="ignore"):
        cell_max = np.nanmax(stack, axis=0)
        cell_min = np.nanmin(stack, axis=0)
    swing = cell_max - cell_min
    finite = swing[np.isfinite(swing)]
    if finite.size == 0:
        return 0.0, None
    peak_range = max(0.0, float(np.nanmax(swing)))
    ts: list[float] | None = None
    if stack.shape[0] > 1:
        flat_idx = int(np.nanargmax(np.where(np.isfinite(swing), swing, -np.inf)))
        r, c = np.unravel_index(flat_idx, swing.shape)
        ts = [
            float(stack[i, r, c]) if np.isfinite(stack[i, r, c]) else 0.0
            for i in range(stack.shape[0])
        ]
    return peak_range, ts


def compute_recovery_efficiency(
    injected_m3: float, recovered_m3: float
) -> float | None:
    """ASR recovery efficiency (recovered/injected), clamped [0, 1] or None."""
    if not injected_m3 or not recovered_m3:
        return None
    inj = float(injected_m3)
    rec = float(recovered_m3)
    if inj <= 0.0:
        return None
    eff = rec / inj
    return max(0.0, min(1.0, eff))


def compute_budget_partition(term_totals: dict[str, float]) -> dict[str, float]:
    """Build the narration-ready budget partition from per-term CBC sums.

    Drops FLOW-JA-FACE and near-zero terms. Byte-faithful to the agent reader.
    """
    partition: dict[str, float] = {}
    for raw_name, value in term_totals.items():
        name = str(raw_name).strip().upper()
        if name in _BUDGET_EXCLUDE_FROM_HEADLINE:
            continue
        q = float(value)
        if abs(q) < 1e-9:
            continue
        partition[name.lower()] = q
    return partition


# ---------------------------------------------------------------------------
# Spill / plume postprocess (original, byte-identical)
# ---------------------------------------------------------------------------


def run_plume_postprocess(
    run_id: str,
    deck_dir: Path,
    model_crs: str,
    runs_uri_for: Any,
) -> ModflowPostprocessResult:
    """Run the plume postprocess on the LOCAL deck dir; return the manifest result.

    ``runs_uri_for`` is a callable ``rel -> uri`` (the entrypoint's
    ``lambda rel: _runs_uri(run_id, rel)``). The COG is written into ``deck_dir``
    under the deterministic key so the entrypoint's output sweep uploads it; the
    manifest's ``cog_uri`` is the resolved runs-bucket URI for that key.

    NEVER raises for an expected-empty result -- returns a status=error result with
    the typed ``MODFLOW_PLUME_EMPTY`` code (the honesty gate). A genuine read/write
    failure returns a status=error result with ``MODFLOW_POSTPROCESS_FAILED`` so
    the entrypoint surfaces it (never a silent ok-with-no-layer).
    """
    ucn_path = _locate_ucn(deck_dir)
    if ucn_path is None:
        return ModflowPostprocessResult(
            status="error", manifest=None,
            error_code="MODFLOW_PLUME_OUTPUT_MISSING",
            error_message=f"no {GWT_UCN_FILENAME} found under {deck_dir}",
        )
    try:
        final2d = _read_final_concentration(ucn_path)
    except Exception as exc:  # noqa: BLE001
        return ModflowPostprocessResult(
            status="error", manifest=None,
            error_code="MODFLOW_PLUME_OUTPUT_READ_FAILED",
            error_message=f"could not read concentration from {ucn_path}: {exc}",
        )

    geo = _grid_georegistration(deck_dir)
    cell_area_m2 = (
        float(geo["delr"]) * float(geo["delc"]) if geo is not None
        else _DEFAULT_CELL_AREA_M2
    )
    max_conc, plume_area_km2 = _compute_plume_metrics(final2d, cell_area_m2)
    LOG.info(
        "modflow postprocess run_id=%s max_concentration_mgl=%.6g plume_area_km2=%.6g",
        run_id, max_conc, plume_area_km2,
    )

    # --- EMPTY-PLUME HONESTY GATE (Invariant 1) --------------------------------
    if plume_area_km2 <= 0.0:
        return ModflowPostprocessResult(
            status="error",
            manifest=_manifest.build_manifest(
                engine="modflow", run_id=run_id, status="error",
                frame_count=0,
                metrics={
                    "max_concentration_mgl": max_conc,
                    "plume_area_km2": plume_area_km2,
                },
                layers=[], error_code="MODFLOW_PLUME_EMPTY",
            ),
            metrics={
                "max_concentration_mgl": max_conc,
                "plume_area_km2": plume_area_km2,
            },
            error_code="MODFLOW_PLUME_EMPTY",
            error_message=(
                "solve clean but the plume field is empty "
                "(no cell above the detection floor)"
            ),
        )

    cog_path = deck_dir / _PLUME_COG_FILENAME
    try:
        _write_plume_cog(final2d, model_crs, geo, cog_path)
    except Exception as exc:  # noqa: BLE001
        return ModflowPostprocessResult(
            status="error", manifest=None,
            error_code="MODFLOW_PLUME_COG_WRITE_FAILED",
            error_message=f"plume COG write/reproject failed: {exc}",
        )

    bbox = _cog_bbox_4326(cog_path)
    cog_uri = runs_uri_for(_PLUME_COG_FILENAME)
    layer = _manifest.build_layer_entry(
        layer_id_stem=f"plume-concentration-{run_id}",
        name="Contaminant Plume (peak concentration)",
        role="primary",
        style_preset=PLUME_STYLE_PRESET,
        units="mg/L",
        cog_uri=cog_uri,
        frame_no=None,
        bbox=bbox,
        band_stats=_band_stats(cog_path),
        metrics={
            "max_concentration_mgl": max_conc,
            "plume_area_km2": plume_area_km2,
        },
    )
    manifest = _manifest.build_manifest(
        engine="modflow", run_id=run_id, status="ok", frame_count=1,
        metrics={
            "max_concentration_mgl": max_conc,
            "plume_area_km2": plume_area_km2,
        },
        layers=[layer],
    )
    return ModflowPostprocessResult(
        status="ok", manifest=manifest,
        metrics={
            "max_concentration_mgl": max_conc,
            "plume_area_km2": plume_area_km2,
        },
        cog_paths=[cog_path],
    )


# ---------------------------------------------------------------------------
# Archetype postprocess runners (GWF-only: head + CBC paths)
#
# Each runner:
#   1. Locates + reads the LOCAL head or CBC file (no S3).
#   2. Computes the archetype-specific metric(s).
#   3. Writes a COG into deck_dir under a deterministic name.
#   4. Builds the publish_manifest.json dict via _manifest.build_manifest.
#   5. Returns ModflowPostprocessResult.
#   6. Implements the empty-result honesty gate (never fake-ok).
#
# Ported from ``trid3nt_server.workflows.postprocess_modflow``:
#   postprocess_drawdown / postprocess_dewatering / postprocess_budget_partition /
#   postprocess_mounding / postprocess_asr / postprocess_wetland_hydroperiod.
# ---------------------------------------------------------------------------


def run_drawdown_postprocess(
    run_id: str,
    deck_dir: Path,
    model_crs: str,
    runs_uri_for: Any,
    *,
    mounding: bool = False,
) -> ModflowPostprocessResult:
    """Head decline (drawdown) COG -> manifest for sustainable_yield archetype.

    When ``mounding=True`` the sign is inverted (head rise) for MAR; both share
    this runner. Honesty gate: max_drawdown_m <= 0 -> MODFLOW_ARCHETYPE_EMPTY_RESULT.
    """
    hds_path = _locate_hds(deck_dir)
    if hds_path is None:
        return ModflowPostprocessResult(
            status="error", manifest=None,
            error_code="MODFLOW_ARCHETYPE_OUTPUT_MISSING",
            error_message=f"no {GWF_HDS_FILENAME} found under {deck_dir}",
        )

    try:
        decline, ts = _read_head_decline_grid(hds_path, invert=mounding)
    except Exception as exc:  # noqa: BLE001
        return ModflowPostprocessResult(
            status="error", manifest=None,
            error_code="MODFLOW_ARCHETYPE_READ_FAILED",
            error_message=f"could not read head from {hds_path}: {exc}",
        )

    metric_val = compute_mounding_metrics(decline) if mounding else compute_drawdown_metrics(decline)
    metric_key = "max_mounding_m" if mounding else "max_drawdown_m"
    cog_filename = "mounding_4326.tif" if mounding else "drawdown_4326.tif"
    style_preset = MOUNDING_STYLE_PRESET if mounding else DRAWDOWN_STYLE_PRESET
    archetype_label = "MAR" if mounding else "sustainable_yield"
    layer_stem = ("mounding" if mounding else "drawdown") + f"-{run_id}"
    layer_name = (
        "Recharge Mounding (head rise)" if mounding
        else "Pumping Drawdown (head decline)"
    )

    LOG.info(
        "drawdown/mounding postprocess run_id=%s archetype=%s %s=%.6g",
        run_id, archetype_label, metric_key, metric_val,
    )

    # --- HONESTY GATE ----------------------------------------------------------
    if metric_val <= 0.0:
        return ModflowPostprocessResult(
            status="error",
            manifest=_manifest.build_manifest(
                engine="modflow", run_id=run_id, status="error", frame_count=0,
                metrics={metric_key: metric_val},
                layers=[], error_code="MODFLOW_ARCHETYPE_EMPTY_RESULT",
            ),
            metrics={metric_key: metric_val},
            error_code="MODFLOW_ARCHETYPE_EMPTY_RESULT",
            error_message=(
                f"{archetype_label} run produced no non-trivial result "
                f"({metric_key}={metric_val!r})"
            ),
        )

    geo = _grid_georegistration(deck_dir)
    cog_path = deck_dir / cog_filename
    try:
        _write_cog(decline, model_crs, geo, cog_path, mask_below_floor=False)
    except Exception as exc:  # noqa: BLE001
        return ModflowPostprocessResult(
            status="error", manifest=None,
            error_code="MODFLOW_ARCHETYPE_COG_WRITE_FAILED",
            error_message=f"COG write failed: {exc}",
        )

    bbox = _cog_bbox_4326(cog_path)
    metrics: dict[str, Any] = {metric_key: metric_val}
    if ts is not None:
        metrics["head_decline_timeseries"] = ts

    cog_uri = runs_uri_for(cog_filename)
    layer = _manifest.build_layer_entry(
        layer_id_stem=layer_stem,
        name=layer_name,
        role="primary",
        style_preset=style_preset,
        units="m",
        cog_uri=cog_uri,
        frame_no=None,
        bbox=bbox,
        band_stats=_band_stats(cog_path),
        metrics=metrics,
    )
    manifest = _manifest.build_manifest(
        engine="modflow", run_id=run_id, status="ok", frame_count=1,
        metrics=metrics, layers=[layer],
    )
    return ModflowPostprocessResult(
        status="ok", manifest=manifest, metrics=metrics, cog_paths=[cog_path],
    )


def run_mounding_postprocess(
    run_id: str,
    deck_dir: Path,
    model_crs: str,
    runs_uri_for: Any,
) -> ModflowPostprocessResult:
    """MAR groundwater mounding (head rise) COG -> manifest.

    Delegates to run_drawdown_postprocess with mounding=True and additionally
    computes the recharged volume from the CBC RCH/RCHA budget.
    """
    # Run the drawdown runner with mounding=True for the COG + max_mounding_m.
    base = run_drawdown_postprocess(
        run_id, deck_dir, model_crs, runs_uri_for, mounding=True
    )
    if base.status != "ok":
        return base

    # Best-effort recharged volume from CBC RCH/RCHA budget.
    recharged_volume_m3: float | None = None
    try:
        hds_path = _locate_hds(deck_dir)
        cbc_path = _locate_cbc(deck_dir)
        if hds_path is not None and cbc_path is not None:
            duration_days = _head_total_duration_days(hds_path)
            # _read_head_decline_grid already read the file; re-read step count
            # cheaply via get_times.
            import flopy.utils  # type: ignore[import-not-found]

            n_steps = len(flopy.utils.HeadFile(str(hds_path)).get_times())
            rch_in = 0.0
            for term in ("RCHA", "RCH"):
                _net, in_mag, _out = _read_cbc_term_signed_totals(cbc_path, term)
                if in_mag > 0.0:
                    rch_in += in_mag
            if duration_days and rch_in > 0.0:
                standing_rate = rch_in / float(max(n_steps, 1))
                recharged_volume_m3 = compute_recharged_volume_m3(
                    standing_rate, duration_days
                )
    except Exception:  # noqa: BLE001 -- non-fatal; partition still returned
        pass

    if recharged_volume_m3 is not None and base.manifest is not None:
        base.metrics["recharged_volume_m3"] = recharged_volume_m3
        # Patch the metric into the manifest layers[0] so the agent can read it.
        layers = base.manifest.get("layers") or []
        if layers:
            layers[0].setdefault("metrics", {})["recharged_volume_m3"] = recharged_volume_m3
        base.manifest.setdefault("metrics", {})["recharged_volume_m3"] = recharged_volume_m3

    LOG.info(
        "mounding postprocess run_id=%s max_mounding_m=%.6g recharged_volume_m3=%s",
        run_id,
        base.metrics.get("max_mounding_m", 0.0),
        recharged_volume_m3,
    )
    return base


def run_dewatering_postprocess(
    run_id: str,
    deck_dir: Path,
    model_crs: str,
    runs_uri_for: Any,
    *,
    term: str = "DRN",
) -> ModflowPostprocessResult:
    """Mine-dewatering DRN-flux COG -> manifest for mine_dewatering archetype.

    Honesty gate: dewatering_rate_m3_day <= 0 -> MODFLOW_ARCHETYPE_EMPTY_RESULT.
    """
    cbc_path = _locate_cbc(deck_dir)
    if cbc_path is None:
        return ModflowPostprocessResult(
            status="error", manifest=None,
            error_code="MODFLOW_ARCHETYPE_OUTPUT_MISSING",
            error_message=f"no {GWF_CBC_FILENAME} found under {deck_dir}",
        )

    geo = _grid_georegistration(deck_dir)
    nrow = int(geo["nrow"]) if geo is not None else None
    ncol = int(geo["ncol"]) if geo is not None else None
    if nrow is None or ncol is None:
        nrow, ncol = _infer_grid_shape_from_cbc(cbc_path)

    try:
        term_grid = _read_cbc_term_grid(cbc_path, term, nrow, ncol)
    except Exception as exc:  # noqa: BLE001
        return ModflowPostprocessResult(
            status="error", manifest=None,
            error_code="MODFLOW_ARCHETYPE_READ_FAILED",
            error_message=f"could not read {term} CBC from {cbc_path}: {exc}",
        )

    import numpy as np  # type: ignore[import-not-found]

    dewatering_rate_m3_day, drain_cell_count = compute_cbc_term_metrics(term_grid)
    LOG.info(
        "dewatering postprocess run_id=%s term=%s rate=%.6g cells=%d",
        run_id, term, dewatering_rate_m3_day, drain_cell_count,
    )

    # --- HONESTY GATE ----------------------------------------------------------
    if dewatering_rate_m3_day <= 0.0:
        return ModflowPostprocessResult(
            status="error",
            manifest=_manifest.build_manifest(
                engine="modflow", run_id=run_id, status="error", frame_count=0,
                metrics={
                    "dewatering_rate_m3_day": dewatering_rate_m3_day,
                    "drain_cell_count": drain_cell_count,
                },
                layers=[], error_code="MODFLOW_ARCHETYPE_EMPTY_RESULT",
            ),
            metrics={
                "dewatering_rate_m3_day": dewatering_rate_m3_day,
                "drain_cell_count": drain_cell_count,
            },
            error_code="MODFLOW_ARCHETYPE_EMPTY_RESULT",
            error_message=(
                f"mine_dewatering run produced no non-trivial result "
                f"(dewatering_rate_m3_day={dewatering_rate_m3_day!r})"
            ),
        )

    magnitude_grid = np.abs(np.asarray(term_grid, dtype="float64"))
    cog_filename = "dewatering_rate_4326.tif"
    cog_path = deck_dir / cog_filename
    try:
        _write_cog(magnitude_grid, model_crs, geo, cog_path, mask_below_floor=False)
    except Exception as exc:  # noqa: BLE001
        return ModflowPostprocessResult(
            status="error", manifest=None,
            error_code="MODFLOW_ARCHETYPE_COG_WRITE_FAILED",
            error_message=f"COG write failed: {exc}",
        )

    bbox = _cog_bbox_4326(cog_path)
    metrics: dict[str, Any] = {
        "dewatering_rate_m3_day": dewatering_rate_m3_day,
        "drain_cell_count": drain_cell_count,
    }
    cog_uri = runs_uri_for(cog_filename)
    layer = _manifest.build_layer_entry(
        layer_id_stem=f"dewatering-rate-{run_id}",
        name="Mine Dewatering Rate",
        role="primary",
        style_preset=DEWATERING_STYLE_PRESET,
        units="m^3/day",
        cog_uri=cog_uri,
        frame_no=None,
        bbox=bbox,
        band_stats=_band_stats(cog_path),
        metrics=metrics,
    )
    manifest = _manifest.build_manifest(
        engine="modflow", run_id=run_id, status="ok", frame_count=1,
        metrics=metrics, layers=[layer],
    )
    return ModflowPostprocessResult(
        status="ok", manifest=manifest, metrics=metrics, cog_paths=[cog_path],
    )


def run_budget_partition_postprocess(
    run_id: str,
    deck_dir: Path,
    model_crs: str,
    runs_uri_for: Any,
) -> ModflowPostprocessResult:
    """Regional water budget partition (metrics) + water-table head COG -> manifest.

    The spatial carrier is the water-table head COG (HEAD_STYLE_PRESET); the
    narration-ready payload is the budget partition dict. Honesty gate: empty
    partition dict -> MODFLOW_ARCHETYPE_EMPTY_RESULT.
    """
    cbc_path = _locate_cbc(deck_dir)
    if cbc_path is None:
        return ModflowPostprocessResult(
            status="error", manifest=None,
            error_code="MODFLOW_ARCHETYPE_OUTPUT_MISSING",
            error_message=f"no {GWF_CBC_FILENAME} found under {deck_dir}",
        )

    try:
        term_totals = _read_cbc_budget_partition(cbc_path)
        partition = compute_budget_partition(term_totals)
    except Exception as exc:  # noqa: BLE001
        return ModflowPostprocessResult(
            status="error", manifest=None,
            error_code="MODFLOW_ARCHETYPE_READ_FAILED",
            error_message=f"could not read budget partition from {cbc_path}: {exc}",
        )

    LOG.info(
        "budget partition postprocess run_id=%s terms=%s",
        run_id, {k: round(v, 3) for k, v in partition.items()},
    )

    # --- HONESTY GATE ----------------------------------------------------------
    if not partition:
        return ModflowPostprocessResult(
            status="error",
            manifest=_manifest.build_manifest(
                engine="modflow", run_id=run_id, status="error", frame_count=0,
                metrics={"budget_partition_m3_day": {}},
                layers=[], error_code="MODFLOW_ARCHETYPE_EMPTY_RESULT",
            ),
            metrics={"budget_partition_m3_day": {}},
            error_code="MODFLOW_ARCHETYPE_EMPTY_RESULT",
            error_message="regional_water_budget run produced no non-trivial partition",
        )

    # Spatial carrier: water-table head COG (best-effort; partition stays ok even
    # if the HDS is unavailable).
    geo = _grid_georegistration(deck_dir)
    cog_filename = "water_table_4326.tif"
    cog_path = deck_dir / cog_filename
    bbox: list[float] | None = None
    cog_written = False
    try:
        hds_path = _locate_hds(deck_dir)
        if hds_path is not None:
            head_grid = _read_head_grid(hds_path)
            _write_cog(head_grid, model_crs, geo, cog_path, mask_below_floor=False)
            bbox = _cog_bbox_4326(cog_path)
            cog_written = True
    except Exception:  # noqa: BLE001
        LOG.warning("budget-partition head COG unavailable (partition still returned)")

    cog_uri = runs_uri_for(cog_filename) if cog_written else ""
    metrics: dict[str, Any] = {"budget_partition_m3_day": partition}
    layer = _manifest.build_layer_entry(
        layer_id_stem=f"budget-partition-{run_id}",
        name="Regional Water Budget (zonal partition)",
        role="primary",
        style_preset=HEAD_STYLE_PRESET,
        units="m^3/day",
        cog_uri=cog_uri,
        frame_no=None,
        bbox=bbox,
        band_stats=_band_stats(cog_path) if cog_written else {},
        metrics=metrics,
    )
    manifest = _manifest.build_manifest(
        engine="modflow", run_id=run_id, status="ok", frame_count=1,
        metrics=metrics, layers=[layer],
    )
    result_cog_paths = [cog_path] if cog_written else []
    return ModflowPostprocessResult(
        status="ok", manifest=manifest, metrics=metrics, cog_paths=result_cog_paths,
    )


def run_asr_postprocess(
    run_id: str,
    deck_dir: Path,
    model_crs: str,
    runs_uri_for: Any,
) -> ModflowPostprocessResult:
    """ASR (aquifer storage & recovery) head COG + efficiency -> manifest.

    Honesty gate: head_timeseries is None/empty -> MODFLOW_ARCHETYPE_EMPTY_RESULT
    (the ASR series is the primary deliverable; recovery_efficiency may be None
    for a single-cycle run).
    """
    hds_path = _locate_hds(deck_dir)
    if hds_path is None:
        return ModflowPostprocessResult(
            status="error", manifest=None,
            error_code="MODFLOW_ARCHETYPE_OUTPUT_MISSING",
            error_message=f"no {GWF_HDS_FILENAME} found under {deck_dir}",
        )

    try:
        head_steps = _read_head_steps(hds_path)
    except Exception as exc:  # noqa: BLE001
        return ModflowPostprocessResult(
            status="error", manifest=None,
            error_code="MODFLOW_ARCHETYPE_READ_FAILED",
            error_message=f"could not read head steps from {hds_path}: {exc}",
        )

    _swing, head_timeseries = compute_seasonal_head_range_m(head_steps)

    # Recovery efficiency from WEL inject/recover budget.
    recovery_efficiency: float | None = None
    try:
        cbc_path = _locate_cbc(deck_dir)
        if cbc_path is not None:
            _net, injected, recovered = _read_cbc_term_signed_totals(cbc_path, "WEL")
            recovery_efficiency = compute_recovery_efficiency(injected, recovered)
    except Exception:  # noqa: BLE001
        pass

    LOG.info(
        "ASR postprocess run_id=%s recovery_efficiency=%s head_steps=%d",
        run_id,
        recovery_efficiency,
        len(head_timeseries) if head_timeseries is not None else 0,
    )

    # --- HONESTY GATE ----------------------------------------------------------
    if not head_timeseries:
        return ModflowPostprocessResult(
            status="error",
            manifest=_manifest.build_manifest(
                engine="modflow", run_id=run_id, status="error", frame_count=0,
                metrics={"recovery_efficiency": recovery_efficiency, "head_timeseries": None},
                layers=[], error_code="MODFLOW_ARCHETYPE_EMPTY_RESULT",
            ),
            metrics={"recovery_efficiency": recovery_efficiency, "head_timeseries": None},
            error_code="MODFLOW_ARCHETYPE_EMPTY_RESULT",
            error_message="ASR run produced no head timeseries (single-step run?)",
        )

    # Spatial carrier: final-step water-table head COG.
    geo = _grid_georegistration(deck_dir)
    cog_filename = "asr_head_4326.tif"
    cog_path = deck_dir / cog_filename
    head_grid = head_steps[-1]
    try:
        _write_cog(head_grid, model_crs, geo, cog_path, mask_below_floor=False)
    except Exception as exc:  # noqa: BLE001
        return ModflowPostprocessResult(
            status="error", manifest=None,
            error_code="MODFLOW_ARCHETYPE_COG_WRITE_FAILED",
            error_message=f"COG write failed: {exc}",
        )

    bbox = _cog_bbox_4326(cog_path)
    metrics: dict[str, Any] = {
        "recovery_efficiency": recovery_efficiency,
        "head_timeseries": head_timeseries,
    }
    cog_uri = runs_uri_for(cog_filename)
    layer = _manifest.build_layer_entry(
        layer_id_stem=f"asr-{run_id}",
        name="Aquifer Storage & Recovery (well head + recovery)",
        role="primary",
        style_preset=ASR_STYLE_PRESET,
        units="m",
        cog_uri=cog_uri,
        frame_no=None,
        bbox=bbox,
        band_stats=_band_stats(cog_path),
        metrics=metrics,
    )
    manifest = _manifest.build_manifest(
        engine="modflow", run_id=run_id, status="ok", frame_count=1,
        metrics=metrics, layers=[layer],
    )
    return ModflowPostprocessResult(
        status="ok", manifest=manifest, metrics=metrics, cog_paths=[cog_path],
    )


def run_wetland_hydroperiod_postprocess(
    run_id: str,
    deck_dir: Path,
    model_crs: str,
    runs_uri_for: Any,
) -> ModflowPostprocessResult:
    """Wetland hydroperiod seasonal range COG -> manifest.

    Renders the per-cell seasonal head RANGE (max-over-time minus min-over-time)
    as a COG. Honesty gate: seasonal_head_range_m <= 0 -> MODFLOW_ARCHETYPE_EMPTY_RESULT.
    """
    hds_path = _locate_hds(deck_dir)
    if hds_path is None:
        return ModflowPostprocessResult(
            status="error", manifest=None,
            error_code="MODFLOW_ARCHETYPE_OUTPUT_MISSING",
            error_message=f"no {GWF_HDS_FILENAME} found under {deck_dir}",
        )

    try:
        head_steps = _read_head_steps(hds_path)
    except Exception as exc:  # noqa: BLE001
        return ModflowPostprocessResult(
            status="error", manifest=None,
            error_code="MODFLOW_ARCHETYPE_READ_FAILED",
            error_message=f"could not read head steps from {hds_path}: {exc}",
        )

    import numpy as np  # type: ignore[import-not-found]

    seasonal_head_range_m, head_timeseries = compute_seasonal_head_range_m(head_steps)
    LOG.info(
        "wetland hydroperiod postprocess run_id=%s seasonal_head_range_m=%.6g steps=%d",
        run_id,
        seasonal_head_range_m,
        len(head_timeseries) if head_timeseries is not None else 0,
    )

    # --- HONESTY GATE ----------------------------------------------------------
    if seasonal_head_range_m <= 0.0:
        return ModflowPostprocessResult(
            status="error",
            manifest=_manifest.build_manifest(
                engine="modflow", run_id=run_id, status="error", frame_count=0,
                metrics={"seasonal_head_range_m": seasonal_head_range_m},
                layers=[], error_code="MODFLOW_ARCHETYPE_EMPTY_RESULT",
            ),
            metrics={"seasonal_head_range_m": seasonal_head_range_m},
            error_code="MODFLOW_ARCHETYPE_EMPTY_RESULT",
            error_message=(
                f"wetland_hydroperiod run produced no non-trivial seasonal range "
                f"(seasonal_head_range_m={seasonal_head_range_m!r})"
            ),
        )

    # Range COG: per-cell max-over-time minus min-over-time.
    geo = _grid_georegistration(deck_dir)
    stack = np.stack([np.asarray(s, dtype="float64") for s in head_steps], axis=0)
    with np.errstate(invalid="ignore"):
        range_grid = np.nanmax(stack, axis=0) - np.nanmin(stack, axis=0)

    cog_filename = "hydroperiod_range_4326.tif"
    cog_path = deck_dir / cog_filename
    try:
        _write_cog(range_grid, model_crs, geo, cog_path, mask_below_floor=False)
    except Exception as exc:  # noqa: BLE001
        return ModflowPostprocessResult(
            status="error", manifest=None,
            error_code="MODFLOW_ARCHETYPE_COG_WRITE_FAILED",
            error_message=f"COG write failed: {exc}",
        )

    bbox = _cog_bbox_4326(cog_path)
    metrics: dict[str, Any] = {
        "seasonal_head_range_m": seasonal_head_range_m,
        "head_timeseries": head_timeseries,
    }
    cog_uri = runs_uri_for(cog_filename)
    layer = _manifest.build_layer_entry(
        layer_id_stem=f"hydroperiod-{run_id}",
        name="Wetland Hydroperiod (seasonal water-table range)",
        role="primary",
        style_preset=HYDROPERIOD_STYLE_PRESET,
        units="m",
        cog_uri=cog_uri,
        frame_no=None,
        bbox=bbox,
        band_stats=_band_stats(cog_path),
        metrics=metrics,
    )
    manifest = _manifest.build_manifest(
        engine="modflow", run_id=run_id, status="ok", frame_count=1,
        metrics=metrics, layers=[layer],
    )
    return ModflowPostprocessResult(
        status="ok", manifest=manifest, metrics=metrics, cog_paths=[cog_path],
    )


# ---------------------------------------------------------------------------
# Archetype dispatch table
# ---------------------------------------------------------------------------

#: Maps each offloadable archetype name to the postprocess runner function name
#: exported from this module. PRT archetypes (capture_zone, wellhead_protection)
#: and saltwater_intrusion are LOCAL-ONLY and must NOT appear here.
_ARCHETYPE_POSTPROCESS_RUNNERS: dict[str, str] = {
    "sustainable_yield": "run_drawdown_postprocess",
    "mine_dewatering": "run_dewatering_postprocess",
    "regional_water_budget": "run_budget_partition_postprocess",
    "MAR": "run_mounding_postprocess",
    "ASR": "run_asr_postprocess",
    "wetland_hydroperiod": "run_wetland_hydroperiod_postprocess",
}
