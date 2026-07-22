"""SnapWave wave-field postprocessing (sprint-17 wave-animation).

``postprocess_waves(run_outputs_uri) -> (list[LayerURI], dict)`` reads a SFINCS
quadtree+SnapWave run's ``sfincs_map.nc`` and extracts the TIME-RESOLVED
significant wave-height field (``hm0`` — incident sig wave height; fallback
``hm0ig`` — infragravity) that SnapWave writes EVERY output step on the quadtree
path (verified vs SFINCS ``ncoutput.F90``: both are written with dims
``(nmesh2d_face, time)`` gated only on ``snapwave=1``). It rasterizes the
per-face field onto a regular COG per timestep (reusing the quadtree-aware
``_write_verified_cog`` from ``postprocess_flood``), uploads the COGs to the runs
bucket, and returns typed ``LayerURI`` rows that form a SEPARATE bottom-center
scrubber group on the web ("Wave height step N" vs "Flood depth step N").

This is the WAVE sibling of ``postprocess_flood``: it REUSES that module's
resolve / frame-select / COG-write / upload seams (additive — no behavioral
change to depth). The headline product is the visibly-animating SnapWave wave
field on the Mexico Beach (Hurricane Michael) coastal North Star demo.

Key differences from depth:
- The variable is ``hm0`` (fallback ``hm0ig``) — already a HEIGHT in metres,
  so there is NO ``zs - zb`` arithmetic (depth = water-level minus bed; wave
  height is published directly).
- The dry/no-data threshold is ``NODATA_WAVE_M = 0.05`` m (a 5 cm wave floor)
  — distinct from the depth threshold so a thin film of water with no waves does
  not paint a wave layer.
- The style preset is ``continuous_wave_height`` (a cyan/blue ramp, distinct
  from depth's white->blue->green) so depth and waves are visually separable.

The wave field only exists on a SnapWave/quadtree solve; ``model_flood_scenario``
gates the call on ``quadtree_run_result is not None`` and wraps it in a
degrade-not-fail try/except (a wave-postprocess failure must NEVER sink the depth
layers or the envelope).

This module is workflow-internal — not registered as an atomic tool.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from trid3nt_contracts.execution import LayerURI

# Reuse the depth module's shared seams (additive — no depth behavior change).
from .postprocess_flood import (
    MAX_FLOOD_FRAMES,
    PostprocessError,
    _resolve_run_output_to_local,
    _select_frame_time_indices,
    _upload_cog_to_runs_bucket,
    _write_verified_cog,
)

__all__ = [
    "postprocess_waves",
    "WAVE_HEIGHT_STYLE_PRESET",
    "NODATA_WAVE_M",
    "WAVE_HEIGHT_VARIABLES",
]

logger = logging.getLogger("trid3nt_server.workflows.postprocess_waves")


#: TiTiler style preset name the workflow attaches to the wave-height COGs.
#: Mirrors ``FLOOD_DEPTH_STYLE_PRESET`` in ``postprocess_flood.py``. Resolves to
#: a CYAN/BLUE ramp in ``publish_layer._TITILER_STYLE_REGISTRY`` (P3), visibly
#: distinct from depth's ylgnbu so the two layer groups never look identical.
WAVE_HEIGHT_STYLE_PRESET: str = "continuous_wave_height"

#: Minimum wave height (m) below which cells are masked to NaN (treated as
#: calm / no wave). 5 cm — a thin water film with no real wave energy should
#: not paint a wave layer. Distinct from ``NODATA_DEPTH_M`` (same numeric value,
#: different physical meaning: a wave-height floor, not a wet-cell floor).
NODATA_WAVE_M: float = 0.05

#: Variable selection order for the SnapWave significant-wave-height field.
#: ``hm0`` = incident significant wave height; ``hm0ig`` = infragravity-band
#: significant wave height (the longer-period run-up driver). Both are written
#: per output step on the quadtree+SnapWave path (verified vs ncoutput.F90).
WAVE_HEIGHT_VARIABLES: tuple[str, ...] = ("hm0", "hm0ig")


def _select_wave_variable(ds: Any) -> str:
    """Pick the SnapWave wave-height variable present in the dataset.

    Returns ``"hm0"`` (incident sig wave height) when present, else ``"hm0ig"``
    (infragravity). Raises ``RUN_OUTPUT_EMPTY`` when neither is present — which
    is the honest signal that this was NOT a SnapWave run (no wave field to
    extract); the caller's degrade-not-fail try/except keeps the depth layers.
    """
    for var in WAVE_HEIGHT_VARIABLES:
        if var in ds.variables:
            return var
    raise PostprocessError(
        "RUN_OUTPUT_EMPTY",
        message=(
            "sfincs_map.nc carries no SnapWave wave-height field "
            f"({' / '.join(WAVE_HEIGHT_VARIABLES)}); not a SnapWave run."
        ),
        details={"variables": list(ds.variables.keys())},
    )


def _extract_wave_frames(
    netcdf_path: Path,
    *,
    bbox: tuple[float, float, float, float] | None = None,
) -> tuple[Path, dict[str, Any], list[Path]]:
    """Extract the PEAK (max-over-time) wave-height COG + N per-frame wave COGs.

    Returns ``(peak_cog, peak_metrics, frame_cogs)``:

    - ``peak_cog`` — the representative max-over-time wave-height COG (always
      produced when a wave field exists). The per-face peak is ``hm0.max(time)``
      (NO zs-zb arithmetic — hm0 is already a height).
    - ``peak_metrics`` — PEAK aggregates (max/mean/p95/flooded_cell_count) +
      crs/units over the peak field.
    - ``frame_cogs`` — up to ``MAX_FLOOD_FRAMES`` per-timestep wave-height COGs
      in ASCENDING time order (first + last always kept). EMPTY when the wave
      field has no usable time dim (>1) -> caller emits ONLY the peak layer.

    The field is face-indexed on the quadtree path, so each COG write routes
    through the quadtree-aware ``_write_verified_cog`` (P1) — the per-face values
    are rasterized onto a regular metric grid. ``bbox`` (EPSG:4326) bounds that
    output grid when supplied.
    """
    try:
        import xarray as xr  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise PostprocessError(
            "RUN_OUTPUT_READ_FAILED",
            message=f"xarray/rasterio/numpy not available: {exc}",
            details={"netcdf_path": str(netcdf_path)},
        ) from exc

    try:
        ds = xr.open_dataset(str(netcdf_path))
    except Exception as exc:  # noqa: BLE001
        raise PostprocessError(
            "RUN_OUTPUT_READ_FAILED",
            message=f"xarray could not open {netcdf_path}: {exc}",
            details={"netcdf_path": str(netcdf_path)},
        ) from exc

    frame_cogs: list[Path] = []
    try:
        var = _select_wave_variable(ds)
        wave = ds[var]
        has_time = "time" in wave.dims

        # --- PEAK (max-over-time) wave height — ALWAYS the representative. ---
        peak_field = wave.max(dim="time") if has_time else wave
        peak_cog, peak_metrics = _write_verified_cog(
            peak_field.values,
            ds=ds,
            netcdf_path=netcdf_path,
            face_values=peak_field.values,
            bbox=bbox,
            nodata_threshold_m=NODATA_WAVE_M,
        )

        # --- Per-frame wave field (the animation) — only when time dim > 1. ---
        if has_time:
            n_steps = int(ds.sizes.get("time", wave.sizes.get("time", 0)))
            if n_steps > 1:
                indices = _select_frame_time_indices(n_steps)
                for frame_no, t_idx in enumerate(indices, start=1):
                    field_t = wave.isel(time=t_idx)
                    try:
                        frame_cog, _m = _write_verified_cog(
                            field_t.values,
                            ds=ds,
                            netcdf_path=netcdf_path,
                            face_values=field_t.values,
                            bbox=bbox,
                            nodata_threshold_m=NODATA_WAVE_M,
                        )
                    except PostprocessError:
                        # A single corrupt wave frame must not sink the whole
                        # animation OR the peak layer. Drop partial frames and
                        # degrade to peak-only (honest: one good layer beats a
                        # broken group). Peak-write failures already raised above.
                        logger.warning(
                            "postprocess_waves: wave frame %d (t=%d) COG write/"
                            "verify failed; degrading to peak-only.",
                            frame_no, t_idx,
                        )
                        for p in frame_cogs:
                            try:
                                p.unlink(missing_ok=True)
                            except Exception:  # noqa: BLE001
                                pass
                        frame_cogs = []
                        break
                    frame_cogs.append(frame_cog)
                # A 1-frame "group" can never form on the web (needs >= 2 distinct
                # members); drop a lone frame so we never publish a single styled
                # wave-frame row that pretends to be an animation.
                if len(frame_cogs) < 2:
                    for p in frame_cogs:
                        try:
                            p.unlink(missing_ok=True)
                        except Exception:  # noqa: BLE001
                            pass
                    frame_cogs = []

        return peak_cog, peak_metrics, frame_cogs
    finally:
        try:
            ds.close()
        except Exception:  # noqa: BLE001
            pass


def postprocess_waves(
    run_outputs_uri: str,
    *,
    run_id: str,
    runs_bucket: str | None = None,
    bbox: tuple[float, float, float, float] | None = None,
) -> tuple[list[LayerURI], dict[str, Any]]:
    """Convert a SnapWave run's NetCDF output into wave-height COG ``LayerURI``s.

    Use this AFTER ``postprocess_flood`` on a quadtree+SnapWave solve to
    materialize the visibly-animating wave layers for the map. Mirrors
    ``postprocess_flood``'s shape but selects ``hm0`` (fallback ``hm0ig``) and
    publishes the field DIRECTLY as a height (no zs-zb arithmetic).

    Args:
        run_outputs_uri: the ``s3://`` / local URI of the run output (the
            ``RunResult.output_uri``; may be a directory containing
            ``sfincs_map.nc`` or the NetCDF directly).
        run_id: the run identifier the COGs are keyed under in the runs bucket.
        runs_bucket: optional override for the runs bucket name.
        bbox: optional AOI bbox (EPSG:4326) to bound the rasterized output grid
            (the quadtree face field has no regular extent of its own).

    Returns:
        A tuple ``(layers, metrics)``:

        - ``layers[0]`` is ALWAYS the representative PEAK (max-over-time)
          wave-height COG: ``layer_id=wave-height-peak-{run_id}``, name
          ``"Peak wave height"``, role ``"primary"``,
          ``style_preset=continuous_wave_height``, units ``"meters"`` (uploaded
          as ``wave_height_peak.tif``).
        - ``layers[1:]`` (present ONLY when the wave field carries a time dim >1)
          are up to ``MAX_FLOOD_FRAMES`` per-timestep wave COGs named
          ``"Wave height step N"`` (N = 1..k) with role ``"context"`` (uploaded
          as ``wave_height_frame_{NN:02d}.tif``). The "Wave height step N"
          name stem forms a SEPARATE web scrubber group from "Flood depth step
          N" (``detectSequentialGroups`` keys on the name stem). Each frame COG
          lands at a DISTINCT runs-bucket key so its tile ``url=`` (hence the web
          ``_layer_identity_key``) is distinct — no dedup collapse.

        ``metrics`` carries the PEAK aggregates (``max_depth_m`` etc. — the
        shared ``_write_verified_cog`` metric keys; here they denote wave height)
        + crs/units for the caller's narration/telemetry.

    Raises:
        PostprocessError: any step of the read -> COG-write -> upload chain
            failed (``RUN_OUTPUT_EMPTY`` when there is no SnapWave field — the
            honest "not a SnapWave run" signal the caller degrades on).
    """
    netcdf_path = _resolve_run_output_to_local(run_outputs_uri)
    peak_cog, metrics, frame_cogs = _extract_wave_frames(netcdf_path, bbox=bbox)

    # --- Peak (representative) wave layer — ALWAYS layers[0]. ---
    try:
        peak_uri = _upload_cog_to_runs_bucket(
            peak_cog, run_id, runs_bucket, dest_filename="wave_height_peak.tif"
        )
    finally:
        try:
            peak_cog.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass

    layers: list[LayerURI] = [
        LayerURI(
            layer_id=f"wave-height-peak-{run_id}",
            name="Peak wave height",
            layer_type="raster",
            uri=peak_uri,
            style_preset=WAVE_HEIGHT_STYLE_PRESET,
            role="primary",
            units="meters",
        )
    ]

    # --- Per-frame wave layers (the SnapWave animation). ---
    # Each frame uploads to a DISTINCT key wave_height_frame_{NN:02d}.tif so its
    # tile url= (-> _layer_identity_key) is unique and the dedup keeps every
    # frame. Names carry the EXACT web token ("Wave height step N") so the panel
    # forms a sequential group SEPARATE from the flood-depth group.
    for frame_no, frame_cog in enumerate(frame_cogs, start=1):
        try:
            frame_uri = _upload_cog_to_runs_bucket(
                frame_cog,
                run_id,
                runs_bucket,
                dest_filename=f"wave_height_frame_{frame_no:02d}.tif",
            )
        finally:
            try:
                frame_cog.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
        layers.append(
            LayerURI(
                layer_id=f"wave-height-frame-{frame_no:02d}-{run_id}",
                name=f"Wave height step {frame_no}",
                layer_type="raster",
                uri=frame_uri,
                style_preset=WAVE_HEIGHT_STYLE_PRESET,
                role="context",
                units="meters",
            )
        )

    if len(layers) > 1:
        logger.info(
            "postprocess_waves: emitted peak wave layer + %d time-step frames "
            "(animation group) for run_id=%s",
            len(layers) - 1,
            run_id,
        )
    return layers, metrics
