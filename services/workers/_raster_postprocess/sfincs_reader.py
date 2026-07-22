"""Read sfincs_map.nc: peak + per-timestep depth/wave field extraction.

LIFTED from the agent's ``postprocess_flood`` (``_select_peak_depth`` /
``_collapse_running_max`` / ``_select_frame_time_indices`` /
``_extract_depth_frames``) and ``postprocess_waves`` (``_select_wave_variable`` /
``_extract_wave_frames``) and made GPL-free + agent-import-free.

The reader OPENS the NetCDF once (xarray), resolves the CRS + face coords + the
orientation probe + the regular-grid bounds, and returns a list of picklable
``FieldFrame`` payloads (1D face arrays for the quadtree path, 2D arrays for the
regular grid) — NO open dataset crosses a process boundary. The orchestrator
(:mod:`postprocess`) then encodes those frames in parallel via the ProcessPool.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import cog as _cog

LOG = logging.getLogger("grace2.worker.raster_postprocess.sfincs_reader")

#: Style preset KEYS the agent's ``_TITILER_STYLE_REGISTRY`` resolves to a
#: (rescale, colormap). The worker references the KEY only; it never owns the
#: rescale/colormap table. Match the agent constants.
FLOOD_DEPTH_STYLE_PRESET = "continuous_flood_depth"
WAVE_HEIGHT_STYLE_PRESET = "continuous_wave_height"

#: Upper bound on per-frame COGs (agent ``MAX_FLOOD_FRAMES`` parity). Driven per
#: run from the WORKER Batch env so the #154 granularity gate still controls it.
MAX_FLOOD_FRAMES: int = int(os.environ.get("GRACE2_MAX_FLOOD_FRAMES", "144"))

#: SnapWave significant-wave-height variable selection order (agent parity).
WAVE_HEIGHT_VARIABLES: tuple[str, ...] = ("hm0", "hm0ig")


class ReaderError(_cog.CogError):
    """Raised on a read / extraction failure (reuses the CogError typed codes)."""


@dataclass
class FieldFrame:
    """One picklable field-to-encode payload (peak OR a single time step).

    Carries EITHER the quadtree face values (``face_values`` 1D) OR the
    regular-grid 2D array (``regular_arr``), never both, plus the per-frame
    naming the manifest needs. The encode (:func:`cog.write_field_cog`) runs in a
    subprocess, so every field here is a plain numpy array / scalar / str.
    """

    role: str  # "primary" (peak) | "context" (frame)
    name: str  # EXACT web grouping token ("Peak flood depth" / "Flood depth step N")
    layer_id_stem: str  # "flood-depth-peak" / "flood-depth-frame-01"
    dest_filename: str  # deterministic deck key ("flood_depth_peak.tif" / ...)
    frame_no: int | None  # None for peak, 1..k for frames
    style_preset: str
    nodata_threshold_m: float
    # quadtree
    face_values: Any = None
    # regular grid
    regular_arr: Any = None
    # extra per-layer scalars (wave narration etc.); merged into manifest metrics
    extra_metrics: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExtractResult:
    """Everything the orchestrator needs to encode + manifest a run."""

    crs: str
    is_quadtree: bool
    face_x: Any
    face_y: Any
    bbox: tuple[float, float, float, float] | None
    regular_bounds: tuple[float, float, float, float] | None
    orient_kwargs: dict[str, Any]
    resolution_m: float
    frames: list[FieldFrame]


# --------------------------------------------------------------------------- #
# Frame index subsample (agent _select_frame_time_indices parity).
# --------------------------------------------------------------------------- #


def select_frame_time_indices(n_steps: int) -> list[int]:
    """Pick up to ``MAX_FLOOD_FRAMES`` evenly-spaced time indices (endpoints kept).

    Lifted from postprocess_flood._select_frame_time_indices — never silently
    truncates (LOGs the subsample).
    """
    import numpy as np  # type: ignore

    if n_steps <= 0:
        return []
    if n_steps <= MAX_FLOOD_FRAMES:
        return list(range(n_steps))
    idx = np.linspace(0, n_steps - 1, MAX_FLOOD_FRAMES).round().astype(int)
    kept = [int(i) for i in np.unique(idx)]
    LOG.info(
        "raster_postprocess: %d raw map snapshots exceed MAX_FLOOD_FRAMES=%d; "
        "subsampling evenly to %d frames (first+last kept).",
        n_steps, MAX_FLOOD_FRAMES, len(kept),
    )
    return kept


# --------------------------------------------------------------------------- #
# Depth field selection (agent _select_peak_depth / _collapse_running_max).
# --------------------------------------------------------------------------- #


def _collapse_running_max(field_da: Any) -> Any:
    reduce_dims = [d for d in getattr(field_da, "dims", ()) if d in ("timemax", "time")]
    for d in reduce_dims:
        field_da = field_da.max(dim=d)
    return field_da


def _select_peak_depth(ds: Any) -> Any:
    """Select the PEAK (max-over-time) depth field. Lifted from the agent."""
    if "hmax" in ds.variables:
        return _collapse_running_max(ds["hmax"])
    if "zsmax" in ds.variables and "zb" in ds.variables:
        return _collapse_running_max(ds["zsmax"]) - ds["zb"]
    if "zs" in ds.variables and "zb" in ds.variables:
        return (ds["zs"].max(dim="time") - ds["zb"]).clip(min=0.0)
    raise ReaderError(
        "RUN_OUTPUT_EMPTY",
        message=(
            "sfincs_map.nc carries neither hmax nor zsmax/zs+zb; "
            "no depth field to extract."
        ),
        details={"variables": list(ds.variables.keys())},
    )


def _select_wave_variable(ds: Any) -> str:
    for var in WAVE_HEIGHT_VARIABLES:
        if var in ds.variables:
            return var
    raise ReaderError(
        "RUN_OUTPUT_EMPTY",
        message=(
            "sfincs_map.nc carries no SnapWave wave-height field "
            f"({' / '.join(WAVE_HEIGHT_VARIABLES)}); not a SnapWave run."
        ),
        details={"variables": list(ds.variables.keys())},
    )


def _open_dataset(netcdf_path: Path) -> Any:
    try:
        import xarray as xr  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise ReaderError(
            "RUN_OUTPUT_READ_FAILED",
            message=f"xarray/rasterio/numpy not available: {exc}",
            details={"netcdf_path": str(netcdf_path)},
        ) from exc
    try:
        return xr.open_dataset(str(netcdf_path))
    except Exception as exc:  # noqa: BLE001
        raise ReaderError(
            "RUN_OUTPUT_READ_FAILED",
            message=f"xarray could not open {netcdf_path}: {exc}",
            details={"netcdf_path": str(netcdf_path)},
        ) from exc


def _frame_values(da: Any, *, is_quadtree: bool) -> Any:
    """Materialize a frame DataArray to the picklable payload (1D face / 2D arr)."""
    import numpy as np  # type: ignore

    return np.asarray(da.values)


# --------------------------------------------------------------------------- #
# Extraction entry points.
# --------------------------------------------------------------------------- #


def extract_depth(
    netcdf_path: Path,
    *,
    bbox: tuple[float, float, float, float] | None = None,
    resolution_m: float = 30.0,
) -> ExtractResult:
    """Extract the PEAK depth field + N per-timestep depth frames (picklable).

    Mirrors the agent's ``_extract_depth_frames``: peak ALWAYS; per-frame only
    when ``zs(time,n,m)`` + ``zb`` carry a real time dim > 1; a 1-frame group is
    dropped (the web needs >= 2 to form a scrubber). Returns an
    :class:`ExtractResult` the orchestrator encodes in parallel.
    """
    ds = _open_dataset(netcdf_path)
    try:
        crs = _cog.read_crs_from_dataset(ds)
        is_quadtree = _cog.is_quadtree_output(ds)
        face_x = face_y = None
        regular_bounds = None
        orient_kwargs: dict[str, Any] = {}
        if is_quadtree:
            face_x, face_y = _cog.read_face_coords(ds)
        else:
            regular_bounds = _cog.read_regular_grid_bounds(ds)
            orient_kwargs = _cog.probe_regular_grid_orientation(ds)

        frames: list[FieldFrame] = []

        peak_field = _select_peak_depth(ds)
        frames.append(
            FieldFrame(
                role="primary",
                name="Peak flood depth",
                layer_id_stem="flood-depth-peak",
                dest_filename="flood_depth_peak.tif",
                frame_no=None,
                style_preset=FLOOD_DEPTH_STYLE_PRESET,
                nodata_threshold_m=_cog.NODATA_DEPTH_M,
                face_values=_frame_values(peak_field, is_quadtree=is_quadtree)
                if is_quadtree
                else None,
                regular_arr=None
                if is_quadtree
                else _frame_values(peak_field, is_quadtree=is_quadtree),
            )
        )

        has_timeseries = (
            "zs" in ds.variables
            and "zb" in ds.variables
            and "time" in ds["zs"].dims
        )
        if has_timeseries:
            n_steps = int(ds.sizes.get("time", ds["zs"].sizes.get("time", 0)))
            if n_steps > 1:
                zb = ds["zb"]
                indices = select_frame_time_indices(n_steps)
                frame_payloads: list[Any] = []
                for t_idx in indices:
                    depth_t = (ds["zs"].isel(time=t_idx) - zb).clip(min=0.0)
                    frame_payloads.append(
                        _frame_values(depth_t, is_quadtree=is_quadtree)
                    )
                # A 1-frame group can never form on the web; drop a lone frame.
                if len(frame_payloads) >= 2:
                    for frame_no, payload in enumerate(frame_payloads, start=1):
                        frames.append(
                            FieldFrame(
                                role="context",
                                name=f"Flood depth step {frame_no}",
                                layer_id_stem=f"flood-depth-frame-{frame_no:02d}",
                                dest_filename=f"flood_depth_frame_{frame_no:02d}.tif",
                                frame_no=frame_no,
                                style_preset=FLOOD_DEPTH_STYLE_PRESET,
                                nodata_threshold_m=_cog.NODATA_DEPTH_M,
                                face_values=payload if is_quadtree else None,
                                regular_arr=None if is_quadtree else payload,
                            )
                        )

        return ExtractResult(
            crs=crs,
            is_quadtree=is_quadtree,
            face_x=face_x,
            face_y=face_y,
            bbox=bbox,
            regular_bounds=regular_bounds,
            orient_kwargs=orient_kwargs,
            resolution_m=resolution_m,
            frames=frames,
        )
    finally:
        try:
            ds.close()
        except Exception:  # noqa: BLE001
            pass


def extract_waves(
    netcdf_path: Path,
    *,
    bbox: tuple[float, float, float, float] | None = None,
    resolution_m: float = 30.0,
) -> ExtractResult | None:
    """Extract the PEAK wave-height field + N per-step wave frames (picklable).

    The SnapWave wave field (``hm0`` / fallback ``hm0ig``) is a per-face HEIGHT in
    metres on the quadtree path — rasterized identically to depth (NO zs-zb
    arithmetic). Returns ``None`` (NOT an error) when there is no wave field — the
    honest "not a SnapWave run" signal the worker degrades on (depth-only run).
    """
    ds = _open_dataset(netcdf_path)
    try:
        try:
            var = _select_wave_variable(ds)
        except ReaderError:
            return None  # not a SnapWave run -> no wave layers (honest degrade)

        crs = _cog.read_crs_from_dataset(ds)
        is_quadtree = _cog.is_quadtree_output(ds)
        face_x = face_y = None
        regular_bounds = None
        orient_kwargs: dict[str, Any] = {}
        if is_quadtree:
            face_x, face_y = _cog.read_face_coords(ds)
        else:
            regular_bounds = _cog.read_regular_grid_bounds(ds)
            orient_kwargs = _cog.probe_regular_grid_orientation(ds)

        wave = ds[var]
        has_time = "time" in wave.dims

        frames: list[FieldFrame] = []
        peak_field = wave.max(dim="time") if has_time else wave
        frames.append(
            FieldFrame(
                role="primary",
                name="Peak wave height",
                layer_id_stem="wave-height-peak",
                dest_filename="wave_height_peak.tif",
                frame_no=None,
                style_preset=WAVE_HEIGHT_STYLE_PRESET,
                nodata_threshold_m=_cog.NODATA_DEPTH_M,
                face_values=_frame_values(peak_field, is_quadtree=is_quadtree)
                if is_quadtree
                else None,
                regular_arr=None
                if is_quadtree
                else _frame_values(peak_field, is_quadtree=is_quadtree),
            )
        )

        if has_time:
            n_steps = int(ds.sizes.get("time", wave.sizes.get("time", 0)))
            if n_steps > 1:
                indices = select_frame_time_indices(n_steps)
                frame_payloads = [
                    _frame_values(wave.isel(time=t_idx), is_quadtree=is_quadtree)
                    for t_idx in indices
                ]
                if len(frame_payloads) >= 2:
                    for frame_no, payload in enumerate(frame_payloads, start=1):
                        frames.append(
                            FieldFrame(
                                role="context",
                                name=f"Wave height step {frame_no}",
                                layer_id_stem=f"wave-height-frame-{frame_no:02d}",
                                dest_filename=f"wave_height_frame_{frame_no:02d}.tif",
                                frame_no=frame_no,
                                style_preset=WAVE_HEIGHT_STYLE_PRESET,
                                nodata_threshold_m=_cog.NODATA_DEPTH_M,
                                face_values=payload if is_quadtree else None,
                                regular_arr=None if is_quadtree else payload,
                            )
                        )

        return ExtractResult(
            crs=crs,
            is_quadtree=is_quadtree,
            face_x=face_x,
            face_y=face_y,
            bbox=bbox,
            regular_bounds=regular_bounds,
            orient_kwargs=orient_kwargs,
            resolution_m=resolution_m,
            frames=frames,
        )
    finally:
        try:
            ds.close()
        except Exception:  # noqa: BLE001
            pass
