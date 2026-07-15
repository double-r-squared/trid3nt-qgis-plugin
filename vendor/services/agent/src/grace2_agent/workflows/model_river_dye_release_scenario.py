"""TELEMAC-2D river-dye release composer (river-dye North Star, PHASE 4).

The TELEMAC analogue of ``model_wave_scenario`` (SWAN) /
``model_dambreak_geoclaw_scenario`` (GeoClaw): a deterministic orchestrator-style
workflow (Invariant 2 - no LLM in the chain) that turns a PLACE (or an AOI bbox)
into a rendered, ANIMATED river-dye plume:

    geocode the place -> centroid + AOI bbox (F46: the model NEVER hand-types
        coords -- a natural prompt geocodes)
      -> fetch_river_geometry(bbox) to confirm a real reach + pick a mid-reach
        SEED point on the largest flowline (the worker NLDI-snaps it to the
        COMID and navigates downstream)
      -> stage the ``telemac_river_dye`` worker manifest (ReachConfig overrides)
        to the cache bucket
      -> run_solver('telemac_river_dye', ...) -> wait_for_completion (the SAME
        generic solve seam SFINCS/SWAN/GeoClaw use; local-docker here)
      -> download the result SELAFIN (r2d_river.slf) + telemac_metrics.json
      -> postprocess_telemac (rasterize the PEAK dye concentration -> ONE COG +
        the SELAFIN mesh sibling the plugin animates)
      -> publish the peak COG through publish_layer (render chokepoint)
      -> return the TelemacDyeLayerURI (a LayerURI subtype so the emit_tool_call
        add_loaded_layer gate fires + export_case_to_qgis discovers the mesh).

The DELIBERATE difference from the flood engines: the primary deliverable is the
engine's NATIVE time-stepped SELAFIN mesh (MDAL opens .slf directly and animates
its DYE dataset group with zero new render infra). So this composer emits ONE
peak-concentration COG as the map anchor + narration carrier and lets the mesh
sibling (discovered by ``export_case_to_qgis`` next to the COG in the runs
bucket) carry the animation -- NO per-frame COGs.

Determinism boundary (Invariant 1): every dye number the agent narrates comes
from the typed ``TelemacDyeLayerURI`` fields the postprocess computed with plain
arithmetic over the SELAFIN tracer field - never free-generated. Honesty floor
(FR-AS-7): the layer's ``fallback_note`` labels the run an idealized-bed demo so
a release is never read as a calibrated site study.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from grace2_contracts import new_ulid
from grace2_contracts.telemac_contracts import (
    TELEMAC_DYE_STYLE_PRESET,
    TelemacDyeLayerURI,
)

from ..pipeline_emitter import (
    begin_substeps,
    current_emitter,
    mint_dispatch_and_sim_cards,
    route_sim_terminal,
    substep,
)
from ..tools import TOOL_REGISTRY
from ..tools.publish_layer import PublishLayerError, publish_layer
from .postprocess_telemac import PostprocessTelemacError, postprocess_telemac
from .run_telemac import TELEMAC_SOLVER_NAME
from .solve_progress import drive_live_solve_progress

logger = logging.getLogger("grace2_agent.workflows.model_river_dye_release_scenario")

__all__ = [
    "model_river_dye_release_scenario",
    "TelemacDyeScenarioError",
    "TelemacDyeScenarioInputError",
    "DEFAULT_RIVER_AOI_HALF_DEG",
]

#: Half-width (deg) of the bbox fetched around the geocoded centroid to locate a
#: river reach + pick the seed. ~0.06 deg (~6 km) reliably catches the main stem
#: even when the geocoded city centroid sits a few km off the channel.
DEFAULT_RIVER_AOI_HALF_DEG: float = 0.06

#: Demo defaults so a bare "dye spill in the river near X" runs end-to-end. These
#: mirror the worker ReachConfig demo defaults (Snake River near Twin Falls
#: tuning); the composer only overrides intent-bearing fields.
DEFAULT_REACH_LENGTH_KM: float = 6.0
DEFAULT_CHANNEL_WIDTH_M: float = 60.0
DEFAULT_MESH_SIZE_M: float = 14.0
DEFAULT_SPILL_FRACTION: float = 0.25
DEFAULT_PULSE_WINDOW_S: float = 300.0
DEFAULT_SOURCE_Q_M3S: float = 8.0
DEFAULT_DYE_CONC_MGL: float = 100.0
DEFAULT_SIM_DURATION_S: float = 3600.0


# --------------------------------------------------------------------------- #
# Typed errors
# --------------------------------------------------------------------------- #
class TelemacDyeScenarioError(RuntimeError):
    """Base class for ``model_river_dye_release_scenario`` failures.

    Carries an open-set ``error_code`` propagated to the agent emitter so the
    failure renders a typed error frame (never a silent dead-end)."""

    error_code: str = "TELEMAC_DYE_SCENARIO_ERROR"

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


class TelemacDyeScenarioInputError(TelemacDyeScenarioError):
    """Caller supplied neither a location string nor a bbox (or both)."""

    def __init__(self, message: str) -> None:
        super().__init__("TELEMAC_DYE_SCENARIO_INPUT_INVALID", message)


# --------------------------------------------------------------------------- #
# Registry / geometry helpers
# --------------------------------------------------------------------------- #
def _registry_fn(name: str) -> Any:
    entry = TOOL_REGISTRY.get(name)
    if entry is None:
        raise TelemacDyeScenarioError(
            "TELEMAC_DYE_SCENARIO_ERROR",
            f"required atomic tool {name!r} is not registered.",
        )
    return entry.fn


def _bbox_center(bbox: tuple[float, float, float, float]) -> tuple[float, float]:
    return (0.5 * (bbox[0] + bbox[2]), 0.5 * (bbox[1] + bbox[3]))


def _bbox_around(lon: float, lat: float, half_deg: float) -> tuple[float, float, float, float]:
    return (lon - half_deg, lat - half_deg, lon + half_deg, lat + half_deg)


def _layer_field(result: Any, field: str) -> Any:
    if result is None:
        return None
    if hasattr(result, field):
        return getattr(result, field)
    if isinstance(result, dict):
        return result.get(field)
    return None


def _river_seed_from_geometry(river_uri: str) -> tuple[float, float] | None:
    """Pick a mid-reach seed ``(lon, lat)`` on the LONGEST flowline in the fetched
    river FlatGeobuf, so the worker's NLDI snap lands on the main stem (not a
    stray ditch). Pure geopandas/shapely; downloads the FGB via the SAME boto3
    client the solver uses (MinIO-aware via AWS_ENDPOINT_URL). Returns ``None`` on
    ANY failure (the composer then falls back to the geocoded centroid, which the
    worker NLDI-snaps regardless)."""
    try:
        from ..tools.solver import _get_s3_client, _split_object_uri

        local_fgb: str | None = None
        if river_uri.startswith("s3://") or river_uri.startswith("gs://"):
            _scheme, bucket, key = _split_object_uri(river_uri)
            s3 = _get_s3_client()
            tmp = tempfile.NamedTemporaryFile(
                suffix=".fgb", delete=False, prefix="telemac_river_seed_"
            )
            tmp.close()
            resp = s3.get_object(Bucket=bucket, Key=key)
            with open(tmp.name, "wb") as fh:
                fh.write(resp["Body"].read())
            local_fgb = tmp.name
        else:
            local_fgb = river_uri  # a local path (test seam)

        import geopandas as gpd

        gdf = gpd.read_file(local_fgb)
        if gdf.empty:
            return None
        # Reproject to EPSG:4326 for consistent lon/lat + length ranking in a
        # metric-ish sense (geographic length is a fine proxy for "longest").
        if gdf.crs is not None and str(gdf.crs).upper() not in ("EPSG:4326", "WGS84"):
            try:
                gdf = gdf.to_crs(4326)
            except Exception:  # noqa: BLE001
                pass
        lines = gdf[gdf.geometry.type.isin(["LineString", "MultiLineString"])]
        if lines.empty:
            return None
        longest = max(lines.geometry, key=lambda g: g.length)
        # Explode a MultiLineString to its longest part, then take the midpoint.
        if longest.geom_type == "MultiLineString":
            longest = max(longest.geoms, key=lambda g: g.length)
        mid = longest.interpolate(0.5, normalized=True)
        return (float(mid.x), float(mid.y))
    except Exception as exc:  # noqa: BLE001 -- seed extraction is best-effort
        logger.warning("telemac dye: river-seed extraction failed (non-fatal): %s", exc)
        return None


# --------------------------------------------------------------------------- #
# Manifest staging (cache bucket)
# --------------------------------------------------------------------------- #
def _stage_manifest(reach: dict[str, Any], run_tag: str) -> str:
    """Write the ``telemac_river_dye`` worker manifest to the cache bucket and
    return its ``s3://`` URI (``run_solver`` downloads it to the rundir)."""
    from ..tools.solver import _get_s3_client

    cache_bucket = (os.environ.get("GRACE2_CACHE_BUCKET") or "").strip()
    if not cache_bucket:
        raise TelemacDyeScenarioError(
            "TELEMAC_DYE_STAGING_FAILED",
            "GRACE2_CACHE_BUCKET must be set to stage the TELEMAC manifest.",
        )
    manifest = {
        "reach": reach,
        "run_id": run_tag,
        "inputs": [],  # the pipeline self-fetches NHDPlus + the DEM
        "telemac_args": [],  # the image CMD drives the entrypoint
        "outputs": [
            "r2d_river.slf",
            "river.slf",
            "river.cli",
            "t2d_river.cas",
            "full_listing.log",
            "telemac_metrics.json",
        ],
    }
    key = f"telemac/{run_tag}/manifest.json"
    s3 = _get_s3_client()
    s3.put_object(
        Bucket=cache_bucket,
        Key=key,
        Body=json.dumps(manifest, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    return f"s3://{cache_bucket}/{key}"


def _download_telemac_result(run_id: str) -> tuple[str, int]:
    """Download ``r2d_river.slf`` + read ``utm_epsg`` from ``telemac_metrics.json``
    for a completed run. Returns ``(local_slf_path, utm_epsg)``. Raises
    ``TelemacDyeScenarioError`` when the SELAFIN result is missing."""
    from ..tools.solver import _get_runs_bucket, _get_s3_client

    runs_bucket = _get_runs_bucket()
    s3 = _get_s3_client()

    # utm_epsg from telemac_metrics.json (the SELAFIN carries no CRS).
    utm_epsg: int | None = None
    try:
        obj = s3.get_object(Bucket=runs_bucket, Key=f"{run_id}/telemac_metrics.json")
        metrics = json.loads(obj["Body"].read().decode("utf-8"))
        if isinstance(metrics, dict) and metrics.get("utm_epsg") is not None:
            utm_epsg = int(metrics["utm_epsg"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("telemac dye: metrics read failed for run %s: %s", run_id, exc)

    slf_key = f"{run_id}/r2d_river.slf"
    tmp_dir = tempfile.mkdtemp(prefix=f"telemac-dye-{run_id}-")
    slf_path = str(Path(tmp_dir) / "r2d_river.slf")
    try:
        resp = s3.get_object(Bucket=runs_bucket, Key=slf_key)
        with open(slf_path, "wb") as fh:
            fh.write(resp["Body"].read())
    except Exception as exc:  # noqa: BLE001
        raise TelemacDyeScenarioError(
            "TELEMAC_DYE_OUTPUT_MISSING",
            f"TELEMAC run {run_id} completed but s3://{runs_bucket}/{slf_key} "
            f"was not downloadable: {exc}",
        ) from exc

    if utm_epsg is None:
        raise TelemacDyeScenarioError(
            "TELEMAC_DYE_OUTPUT_MISSING",
            f"TELEMAC run {run_id} produced no utm_epsg in telemac_metrics.json; "
            "cannot georeference the SELAFIN mesh.",
        )
    return slf_path, utm_epsg


# --------------------------------------------------------------------------- #
# The composer
# --------------------------------------------------------------------------- #
async def model_river_dye_release_scenario(
    location: str | None = None,
    bbox: tuple[float, float, float, float] | None = None,
    spill_fraction: float = DEFAULT_SPILL_FRACTION,
    spill_duration_s: float = DEFAULT_PULSE_WINDOW_S,
    dye_concentration_mgl: float = DEFAULT_DYE_CONC_MGL,
    reach_length_km: float = DEFAULT_REACH_LENGTH_KM,
    sim_duration_s: float = DEFAULT_SIM_DURATION_S,
    source_q_m3s: float = DEFAULT_SOURCE_Q_M3S,
    channel_width_m: float = DEFAULT_CHANNEL_WIDTH_M,
    *,
    compute_class: str = "medium",
    pipeline_emitter: Any | None = None,
) -> TelemacDyeLayerURI:
    """Compose place/AOI -> river reach -> TELEMAC-2D dye pulse -> animated layer.

    Supply exactly one of ``location`` (a place name, geocoded - the natural-prompt
    path) or ``bbox`` (an explicit AOI, e.g. a drawn canvas AOI). Returns the
    published ``TelemacDyeLayerURI`` (a ``LayerURI`` subtype) so the emit_tool_call
    ``add_loaded_layer`` gate fires and ``export_case_to_qgis`` discovers the
    SELAFIN mesh sibling for animation.

    Raises ``TelemacDyeScenarioError`` (typed error_code) on any fatal step and
    propagates ``asyncio.CancelledError`` (Invariant 8).
    """
    has_loc = bool(location and str(location).strip())
    has_bbox = bbox is not None
    if has_loc == has_bbox:  # both or neither
        raise TelemacDyeScenarioInputError(
            "supply exactly one of location or bbox "
            f"(got location={has_loc}, bbox={has_bbox})."
        )

    emitter = pipeline_emitter or current_emitter()

    # Plan the user-meaningful atomic-tool count for the breadcrumb: geocode
    # (place path only) + fetch_river_geometry + run_solver + postprocess +
    # publish_layer. Each substep is a no-op when no emitter is bound.
    _planned = 4  # fetch_river_geometry + run_solver + postprocess + publish
    if has_loc:
        _planned += 1  # geocode_location
    begin_substeps(current_emitter(), _planned)

    # --- Stage 1: resolve the AOI + centroid (F46: geocode, never hand-type) -- #
    if has_loc:
        geocode_fn = _registry_fn("geocode_location")
        async with substep(current_emitter(), "geocode_location"):
            geo = await _maybe_emit(
                pipeline_emitter,
                name=f"Geocode: {location}",
                tool_name="geocode_location",
                invoke=lambda: geocode_fn(location),
            )
        glat = geo.get("latitude") if isinstance(geo, dict) else None
        glon = geo.get("longitude") if isinstance(geo, dict) else None
        if glat is None or glon is None:
            raise TelemacDyeScenarioError(
                "TELEMAC_DYE_GEOCODE_FAILED",
                f"geocode_location({location!r}) returned no centroid lat/lon.",
            )
        center_lon, center_lat = float(glon), float(glat)
        location_name = str(geo.get("name") or location)
    else:
        assert bbox is not None
        center_lon, center_lat = _bbox_center(bbox)
        location_name = f"AOI ({center_lat:.4f}, {center_lon:.4f})"

    river_bbox = _bbox_around(center_lon, center_lat, DEFAULT_RIVER_AOI_HALF_DEG)

    # --- Stage 2: fetch the river flowline + pick a mid-reach seed ------------ #
    fetch_river_fn = _registry_fn("fetch_river_geometry")
    async with substep(current_emitter(), "fetch_river_geometry"):
        river_layer = await _maybe_emit(
            pipeline_emitter,
            name="Fetch river geometry",
            tool_name="fetch_river_geometry",
            invoke=lambda: fetch_river_fn(bbox=river_bbox),
        )
    river_uri = _layer_field(river_layer, "uri")
    seed: tuple[float, float] | None = None
    if river_uri:
        seed = await asyncio.to_thread(_river_seed_from_geometry, str(river_uri))
    if seed is None:
        # Fall back to the geocoded centroid; the worker NLDI-snaps it to the
        # nearest flowline COMID regardless (honest degrade, never a dead-end).
        seed = (center_lon, center_lat)
        seed_source = "geocoded-centroid (NLDI will snap to the nearest flowline)"
    else:
        seed_source = "mid-reach point on the largest fetched flowline"
    seed_lon, seed_lat = seed

    # --- Stage 3: stage the worker manifest (ReachConfig overrides) ----------- #
    reach_name = _slug(location_name)
    reach: dict[str, Any] = {
        "name": reach_name,
        "seed_lon": round(seed_lon, 6),
        "seed_lat": round(seed_lat, 6),
        "nav_direction": "DM",
        "distance_km": float(reach_length_km),
        "channel_width_m": float(channel_width_m),
        "mesh_size_m": DEFAULT_MESH_SIZE_M,
        "dye_conc_mgl": float(dye_concentration_mgl),
        "spill_frac": float(min(max(spill_fraction, 0.0), 1.0)),
        "pulse_window_s": float(spill_duration_s),
        "source_q_m3s": float(source_q_m3s),
        "duration_s": float(sim_duration_s),
    }
    run_tag = new_ulid()
    manifest_uri = await asyncio.to_thread(_stage_manifest, reach, run_tag)
    logger.info(
        "model_river_dye_release_scenario staged manifest run_tag=%s seed=(%.5f,%.5f) "
        "seed_source=%s reach=%s -> %s",
        run_tag, seed_lon, seed_lat, seed_source, reach_name, manifest_uri,
    )

    # --- Stage 4: dispatch to the solver (generic run_solver seam) ------------ #
    from ..tools.solver import (
        EmitterBinding,
        run_solver,
        set_emitter_binding,
        wait_for_completion,
    )

    handle = run_solver(
        solver=TELEMAC_SOLVER_NAME,
        model_setup_uri=manifest_uri,
        compute_class=compute_class,
    )
    run_id = handle.run_id

    _sim_step_id = await mint_dispatch_and_sim_cards(
        emitter=emitter,
        solver=TELEMAC_SOLVER_NAME,
        handle=handle,
        compute_class=compute_class,
    )
    if emitter is not None and _sim_step_id is not None:
        set_emitter_binding(EmitterBinding(emitter=emitter, step_id=_sim_step_id))

    _progress_task = asyncio.ensure_future(
        drive_live_solve_progress(
            emitter=current_emitter(),
            run_id=run_id,
            solver=TELEMAC_SOLVER_NAME,
            grid_resolution_m=None,
            active_cell_count=None,
            vcpus=None,
            eta_seconds=None,
        )
    )
    run_result = None

    class _SolveReturnedFailed(RuntimeError):
        pass

    try:
        async with substep(emitter, "run_solver"):
            try:
                run_result = await wait_for_completion(handle)
            except asyncio.CancelledError:
                logger.info("model_river_dye_release_scenario cancelled awaiting solver")
                await route_sim_terminal(emitter, _sim_step_id, run_result=None)
                raise
            finally:
                _progress_task.cancel()
                try:
                    await _progress_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
                set_emitter_binding(None)
            if run_result.status != "complete":
                raise _SolveReturnedFailed
    except _SolveReturnedFailed:
        pass

    await route_sim_terminal(emitter, _sim_step_id, run_result=run_result)

    if run_result is None or run_result.status != "complete":
        raise TelemacDyeScenarioError(
            "TELEMAC_DYE_RUN_FAILED",
            "TELEMAC dye solve did not complete "
            f"(status={getattr(run_result, 'status', None)}, "
            f"error_code={getattr(run_result, 'error_code', None)}): "
            f"{getattr(run_result, 'error_message', '') or getattr(run_result, 'cancellation_reason', '') or ''}",
        )

    # --- Stage 5: download the SELAFIN result + postprocess to the dye COG ---- #
    batch_run_id = getattr(run_result, "run_id", None) or run_id
    slf_path, utm_epsg = await asyncio.to_thread(_download_telemac_result, batch_run_id)

    try:
        async with substep(emitter, "postprocess_telemac"):
            layers, metrics = await asyncio.to_thread(
                postprocess_telemac,
                slf_path,
                run_id=batch_run_id,
                utm_epsg=utm_epsg,
                reach_name=reach_name,
            )
    finally:
        try:
            Path(slf_path).unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass

    if not layers:
        raise TelemacDyeScenarioError(
            "TELEMAC_DYE_NO_LAYERS",
            "postprocess_telemac produced no dye layer (empty tracer field?).",
        )
    raw_peak = layers[0]

    # --- Stage 6: publish the peak COG (render chokepoint) + honest narration - #
    async with substep(emitter, "publish_layer"):
        peak = await asyncio.to_thread(
            _publish_peak_layer, raw_peak, batch_run_id, location_name, reach_name
        )

    logger.info(
        "model_river_dye_release_scenario complete run_id=%s reach=%s "
        "dye_cmax_mgl=%.4g plume_reach_m=%s active_frames=%s peak_uri=%s",
        batch_run_id, reach_name, peak.dye_cmax_mgl, peak.plume_reach_m,
        peak.active_frames, peak.uri,
    )

    # --- Best-effort downstream concentration chart (never blocks) ----------- #
    if emitter is not None:
        try:
            await _maybe_emit_chart(emitter, metrics, location_name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("telemac dye: concentration chart skipped: %s", exc)

    # --- AUTHORITATIVE LAST zoom-to ----------------------------------------- #
    if emitter is not None and peak.bbox:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(peak.bbox)})
        except Exception as exc:  # noqa: BLE001
            logger.warning("model_river_dye_release_scenario: zoom-to failed: %s", exc)

    return peak


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _slug(name: str) -> str:
    """A safe reach slug for the ReachConfig ``name`` (ASCII, underscores)."""
    keep = [c.lower() if (c.isalnum()) else "_" for c in str(name)]
    slug = "".join(keep).strip("_")
    while "__" in slug:
        slug = slug.replace("__", "_")
    return (slug or "river_dye")[:48]


def _publish_peak_layer(
    raw_peak: TelemacDyeLayerURI, run_id: str, location_name: str, reach_name: str
) -> TelemacDyeLayerURI:
    """Publish the peak dye COG through publish_layer (render chokepoint) and
    enrich the narration. On publish failure the raw peak is returned UNCHANGED
    (the raw s3:// COG still lets export_case_to_qgis discover the mesh sibling;
    the dispatch-level emit_layer_uri guardrail handles the map honesty)."""
    honesty = (
        f"Idealized demo: a FINITE mid-reach point-source dye pulse released on "
        f"the real {location_name} river reach (NLDI/NHDPlus geometry) over a "
        f"planar idealized channel bed with prescribed tracer dispersion. The "
        f"raster is the PEAK dye envelope over the run; the animation plays from "
        f"the native SELAFIN mesh. Not a calibrated site study."
    )
    if raw_peak.layer_type != "raster" or not (
        raw_peak.uri.startswith("gs://") or raw_peak.uri.startswith("s3://")
    ):
        return raw_peak.model_copy(update={"fallback_note": honesty})
    layer_id_for_pub = f"telemac-dye-peak-{run_id}"
    try:
        published_uri = publish_layer(
            layer_uri=raw_peak.uri,
            layer_id=layer_id_for_pub,
            style_preset=raw_peak.style_preset or TELEMAC_DYE_STYLE_PRESET,
        )
    except PublishLayerError as exc:
        logger.warning(
            "model_river_dye_release_scenario: publish_layer FAILED layer_id=%s "
            "error_code=%s (%s) - returning the unpublished peak.",
            layer_id_for_pub, exc.error_code, exc,
        )
        return raw_peak.model_copy(update={"fallback_note": honesty})
    return TelemacDyeLayerURI(
        layer_id=layer_id_for_pub,
        name=raw_peak.name,
        layer_type=raw_peak.layer_type,
        uri=published_uri,
        style_preset=raw_peak.style_preset or TELEMAC_DYE_STYLE_PRESET,
        role=raw_peak.role,
        units=raw_peak.units,
        bbox=raw_peak.bbox,
        legend=raw_peak.legend,
        fallback_note=honesty,
        dye_cmax_mgl=raw_peak.dye_cmax_mgl,
        dye_peak_time_s=raw_peak.dye_peak_time_s,
        plume_reach_m=raw_peak.plume_reach_m,
        active_frames=raw_peak.active_frames,
    )


async def _maybe_emit_chart(emitter: Any, metrics: dict[str, Any], location_name: str) -> None:
    """Best-effort dye-concentration summary chart (rise-to-peak). Non-blocking:
    swallows any failure so the map deliverable never depends on a chart. The two
    points are HONEST tracer-field scalars (t0=0 concentration -> the peak
    concentration at its arrival time), not a fabricated curve."""
    if not hasattr(emitter, "emit_chart"):
        return
    cmax = metrics.get("dye_cmax_mgl")
    peak_t = metrics.get("dye_peak_time_s")
    if cmax is None or peak_t is None:
        return
    from ..tools.chart_tools import build_chart_payload  # type: ignore

    vega_lite_spec = {
        "mark": {"type": "line", "point": True},
        "data": {
            "values": [
                {"t_s": 0.0, "dye_mgl": 0.0},
                {"t_s": float(peak_t), "dye_mgl": float(cmax)},
            ]
        },
        "encoding": {
            "x": {"field": "t_s", "type": "quantitative", "title": "Time (s)"},
            "y": {
                "field": "dye_mgl",
                "type": "quantitative",
                "title": "Dye concentration (mg/L)",
            },
        },
    }
    payload = build_chart_payload(
        vega_lite_spec=vega_lite_spec,
        title=f"Peak dye concentration - {location_name}",
        caption=(
            "Reach peak dye concentration and its arrival time (idealized-bed demo)."
        ),
    )
    await emitter.emit_chart(payload)


async def _maybe_emit(
    emitter: Any | None, *, name: str, tool_name: str, invoke: Any
) -> Any:
    """Run ``invoke()`` through ``emitter.emit_tool_call`` if given, else direct."""
    if emitter is not None:
        return await emitter.emit_tool_call(name=name, tool_name=tool_name, invoke=invoke)
    result = invoke()
    if asyncio.iscoroutine(result):
        result = await result
    return result
