"""Orchestrator: run the shared NetCDF -> COG postprocess on a LOCAL sfincs_map.nc.

This is the single entry point the SFINCS worker entrypoints call after the solve
(before ``_write_completion``). It:

  1. reads the LOCAL ``sfincs_map.nc`` (no S3 download) via :mod:`sfincs_reader`,
  2. encodes the peak + frame COGs in PARALLEL (bounded ProcessPool, one GDAL
     dataset per process -> bounded peak memory, GDAL-safe), writing each COG to a
     DETERMINISTIC key inside the deck dir (``flood_depth_peak.tif`` /
     ``flood_depth_frame_NN.tif`` / ``wave_height_*.tif``) so the entrypoint's
     existing ``*.tif`` upload sweep ships them with no new upload code,
  3. precomputes :mod:`band_stats` per COG (so the agent skips the COG re-read),
  4. computes the per-layer EPSG:4326 bbox,
  5. assembles the typed :mod:`manifest` dict (status + peak metrics + layers),
  6. applies the EMPTY-FIELD HONESTY GATE (flooded_cell_count==0 -> status=error
     with the typed code) so the agent never registers a status=ok-but-empty
     layer (Invariant 1 / FR-AS-7).

The orchestrator never imports cht_sfincs and never imports agent code. It does
NOT itself upload (the entrypoint owns the bucket/scheme/run-id + the sweep) nor
write completion.json — it RETURNS the manifest dict + the status the entrypoint
folds into completion.json (the manifest is written BEFORE completion.json so a
Spot reclaim mid-postprocess leaves no completion.json -> the agent retries).
"""

from __future__ import annotations

import logging
import multiprocessing as _mp
import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import band_stats as _band_stats
from . import cog as _cog
from . import manifest as _manifest
from . import sfincs_reader as _reader

LOG = logging.getLogger("grace2.worker.raster_postprocess.postprocess")

#: Bounded ProcessPool worker count. Defaults to min(cpu_count, 8) so a c7i box
#: parallelizes frames without oversubscribing GDAL/BLAS. Override via env for
#: ops tuning. A value <= 1 forces SERIAL encode (tests / tiny boxes).
def _default_workers() -> int:
    try:
        cpu = os.cpu_count() or 1
    except Exception:  # noqa: BLE001
        cpu = 1
    return max(1, min(cpu, 8))


POSTPROCESS_WORKERS: int = int(
    os.environ.get("GRACE2_POSTPROCESS_WORKERS", str(_default_workers()))
)


@dataclass
class PostprocessResult:
    """What the entrypoint folds into completion.json + writes as the manifest."""

    status: str  # "ok" | "error"
    manifest: dict[str, Any]
    metrics: dict[str, Any]  # top-level peak aggregates (FloodMetrics source)
    cog_paths: list[Path]  # local COGs written into the deck dir (for the sweep)
    error_code: str | None = None
    error_message: str | None = None


# --------------------------------------------------------------------------- #
# The picklable per-frame encode task (module-level so ProcessPool can pickle it).
# --------------------------------------------------------------------------- #


def _encode_one(task: dict[str, Any]) -> dict[str, Any]:
    """Encode ONE field to its deterministic COG path; return metrics + path.

    Runs in a SUBPROCESS (ProcessPool). Receives only plain numpy arrays + floats
    + str (no open dataset, no cht). One GDAL dataset per process => bounded peak
    memory + GDAL-safe. Returns ``{"dest_filename","metrics"}`` or raises CogError.
    """
    out_path = Path(task["out_path"])
    metrics = _cog.write_field_cog(
        out_path=out_path,
        crs=task["crs"],
        nodata_threshold_m=task["nodata_threshold_m"],
        face_values=task.get("face_values"),
        face_x=task.get("face_x"),
        face_y=task.get("face_y"),
        bbox=task.get("bbox"),
        resolution_m=task.get("resolution_m", 30.0),
        regular_arr=task.get("regular_arr"),
        regular_bounds=task.get("regular_bounds"),
        orient_kwargs=task.get("orient_kwargs"),
    )
    return {"dest_filename": task["dest_filename"], "metrics": metrics}


def _build_tasks(
    extract: "_reader.ExtractResult", deck_dir: Path
) -> list[dict[str, Any]]:
    """Build the picklable encode tasks (one per FieldFrame), in manifest order."""
    tasks: list[dict[str, Any]] = []
    for fr in extract.frames:
        task: dict[str, Any] = {
            "dest_filename": fr.dest_filename,
            "out_path": str(deck_dir / fr.dest_filename),
            "crs": extract.crs,
            "nodata_threshold_m": fr.nodata_threshold_m,
            "resolution_m": extract.resolution_m,
        }
        if extract.is_quadtree:
            task["face_values"] = fr.face_values
            task["face_x"] = extract.face_x
            task["face_y"] = extract.face_y
            task["bbox"] = extract.bbox
        else:
            task["regular_arr"] = fr.regular_arr
            task["regular_bounds"] = extract.regular_bounds
            task["orient_kwargs"] = extract.orient_kwargs
        tasks.append(task)
    return tasks


def _run_encode(tasks: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Encode all tasks; return {dest_filename: metrics}. Parallel or serial.

    A failure on the PEAK (first) task re-raises (it sinks the run). A failure on
    a FRAME task is logged + that frame dropped (degrade to peak-only) — matching
    the agent's per-frame VALID-COG guard.
    """
    results: dict[str, dict[str, Any]] = {}
    if not tasks:
        return results

    peak_task, frame_tasks = tasks[0], tasks[1:]

    # Peak ALWAYS encoded first + serially (its failure sinks the run).
    peak_out = _encode_one(peak_task)
    results[peak_out["dest_filename"]] = peak_out["metrics"]

    if not frame_tasks:
        return results

    if POSTPROCESS_WORKERS <= 1:
        for t in frame_tasks:
            try:
                out = _encode_one(t)
            except _cog.CogError as exc:
                LOG.warning(
                    "raster_postprocess: frame %s encode failed (%s); dropping.",
                    t["dest_filename"], exc.error_code,
                )
                continue
            results[out["dest_filename"]] = out["metrics"]
        return results

    workers = min(POSTPROCESS_WORKERS, len(frame_tasks))
    # 'spawn' (not the default 'fork') avoids fork-from-a-multithreaded-parent
    # deadlocks and gives each frame a CLEAN GDAL/PROJ process (one dataset per
    # process => bounded peak memory, GDAL-safe). Falls back to the default
    # context if 'spawn' is unavailable on the platform.
    try:
        mp_ctx = _mp.get_context("spawn")
    except (ValueError, RuntimeError):  # pragma: no cover — platform fallback
        mp_ctx = None
    with ProcessPoolExecutor(max_workers=workers, mp_context=mp_ctx) as ex:
        futures = {ex.submit(_encode_one, t): t for t in frame_tasks}
        for fut, t in futures.items():
            try:
                out = fut.result()
            except _cog.CogError as exc:
                LOG.warning(
                    "raster_postprocess: frame %s encode failed (%s); dropping.",
                    t["dest_filename"], exc.error_code,
                )
                continue
            except Exception as exc:  # noqa: BLE001 — a subprocess crash drops the frame
                LOG.warning(
                    "raster_postprocess: frame %s encode crashed (%s); dropping.",
                    t["dest_filename"], exc,
                )
                continue
            results[out["dest_filename"]] = out["metrics"]
    return results


def _cog_bbox_4326(cog_path: Path) -> list[float] | None:
    """Read a COG's bounds + reproject to EPSG:4326 -> [minlon,minlat,maxlon,maxlat]."""
    try:
        import rasterio  # type: ignore
        from rasterio.warp import transform_bounds  # type: ignore

        with rasterio.open(str(cog_path)) as src:
            b = src.bounds
            minx, miny, maxx, maxy = transform_bounds(
                src.crs, "EPSG:4326", b.left, b.bottom, b.right, b.top
            )
            return [float(minx), float(miny), float(maxx), float(maxy)]
    except Exception as exc:  # noqa: BLE001
        LOG.debug("bbox-4326 read failed (%s: %s)", type(exc).__name__, exc)
        return None


def run_postprocess(
    netcdf_path: Path,
    *,
    run_id: str,
    deck_dir: Path,
    runs_uri_for,
    kind: str = "depth",
    engine: str = "sfincs_quadtree",
    bbox: tuple[float, float, float, float] | None = None,
    resolution_m: float = 30.0,
    empty_error_code: str | None = None,
) -> PostprocessResult:
    """Run the full postprocess for ONE field kind ("depth" | "waves").

    Args:
        netcdf_path: LOCAL ``sfincs_map.nc`` (already in the deck dir).
        run_id: the run identifier (for the manifest + the cog_uri keys).
        deck_dir: where the COGs are written (the entrypoint sweeps + uploads them).
        runs_uri_for: callable ``rel -> s3://.../run_id/rel`` (the entrypoint's
            ``lambda rel: _runs_uri(run_id, rel)``) — used to fill ``cog_uri``.
        kind: "depth" (flood) or "waves" (SnapWave). Waves return an OK manifest
            with NO layers when there is no wave field (honest depth-only degrade).
        engine: the manifest ``engine`` tag.
        bbox: optional AOI bbox (EPSG:4326) to bound the quadtree raster grid.
        resolution_m: target raster resolution (metres).
        empty_error_code: the typed code for the empty-field gate
            (``RUN_OUTPUT_EMPTY`` for depth; ``SWAN_OUTPUT_EMPTY`` style for waves).
    """
    if kind == "waves":
        extract = _reader.extract_waves(
            netcdf_path, bbox=bbox, resolution_m=resolution_m
        )
        if extract is None:
            # Not a SnapWave run — honest empty manifest, NO layers, status ok
            # (the run is fine; it simply has no wave product). The depth pass
            # owns the honesty gate.
            return PostprocessResult(
                status="ok",
                manifest=_manifest.build_manifest(
                    engine=engine, run_id=run_id, status="ok",
                    frame_count=0, metrics={}, layers=[],
                ),
                metrics={},
                cog_paths=[],
            )
        empty_error_code = empty_error_code or "SWAN_OUTPUT_EMPTY"
    else:
        extract = _reader.extract_depth(
            netcdf_path, bbox=bbox, resolution_m=resolution_m
        )
        empty_error_code = empty_error_code or "RUN_OUTPUT_EMPTY"

    tasks = _build_tasks(extract, deck_dir)
    metrics_by_dest = _run_encode(tasks)

    # Assemble layers in the ORIGINAL frame order, keeping only frames that
    # actually encoded (a dropped frame is absent from metrics_by_dest). A lone
    # surviving frame (no group) is dropped so the web never paints a fake group.
    peak_frame = extract.frames[0]
    surviving_frames = [
        fr for fr in extract.frames[1:] if fr.dest_filename in metrics_by_dest
    ]
    if len(surviving_frames) < 2:
        for fr in surviving_frames:
            try:
                (deck_dir / fr.dest_filename).unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
            metrics_by_dest.pop(fr.dest_filename, None)
        surviving_frames = []

    ordered = [peak_frame, *surviving_frames]

    peak_metrics = metrics_by_dest.get(peak_frame.dest_filename, {})
    # Honesty gate: an empty peak field sinks the run (no status=ok-but-empty).
    flooded = int(peak_metrics.get("flooded_cell_count", 0) or 0)
    if flooded <= 0:
        # Clean up the empty COGs; emit an error manifest.
        cog_paths = [deck_dir / fr.dest_filename for fr in ordered]
        for p in cog_paths:
            try:
                p.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
        msg = (
            f"{kind} field is empty (flooded_cell_count=0); honesty gate fired."
        )
        LOG.warning("raster_postprocess: %s", msg)
        return PostprocessResult(
            status="error",
            manifest=_manifest.build_manifest(
                engine=engine, run_id=run_id, status="error",
                frame_count=0, metrics=peak_metrics, layers=[],
                error_code=empty_error_code,
            ),
            metrics=peak_metrics,
            cog_paths=[],
            error_code=empty_error_code,
            error_message=msg,
        )

    layers: list[dict[str, Any]] = []
    cog_paths: list[Path] = []
    for fr in ordered:
        cog_path = deck_dir / fr.dest_filename
        cog_paths.append(cog_path)
        bbox_4326 = _cog_bbox_4326(cog_path)
        stats = _band_stats.compute_band_stats(cog_path)
        per_layer_metrics = metrics_by_dest.get(fr.dest_filename)
        # Peak carries its metrics; frames carry only band_stats (lighter manifest).
        layer_metrics = per_layer_metrics if fr.role == "primary" else None
        layers.append(
            _manifest.build_layer_entry(
                layer_id_stem=fr.layer_id_stem,
                name=fr.name,
                role=fr.role,
                style_preset=fr.style_preset,
                units="meters",
                cog_uri=runs_uri_for(fr.dest_filename),
                frame_no=fr.frame_no,
                bbox=bbox_4326,
                band_stats=stats,
                metrics=layer_metrics,
                has_overviews=True,
            )
        )

    frame_count = len(surviving_frames)
    manifest = _manifest.build_manifest(
        engine=engine,
        run_id=run_id,
        status="ok",
        frame_count=frame_count,
        metrics=peak_metrics,
        layers=layers,
    )
    LOG.info(
        "raster_postprocess: %s OK — peak + %d frame(s), max=%.3f flooded=%d",
        kind, frame_count,
        float(peak_metrics.get("max_depth_m", 0.0)),
        flooded,
    )
    return PostprocessResult(
        status="ok",
        manifest=manifest,
        metrics=peak_metrics,
        cog_paths=cog_paths,
    )
