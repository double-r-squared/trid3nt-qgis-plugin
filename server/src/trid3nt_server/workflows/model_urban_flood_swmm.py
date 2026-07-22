"""PySWMM quasi-2D urban-flood composer (sprint-16 P4, Path A - the LOCAL lane).

The SWMM analogue of ``model_flood_scenario`` (SFINCS) /
``model_groundwater_contamination_scenario`` (MODFLOW). A deterministic
orchestrator-style workflow (Invariant 2 - no LLM in the chain) that composes
the urban-flood engine end-to-end on NATE's PCSWMM screenshot path:

    fetch DEM (fetch_3dep_extra 1m -> fetch_dem 10m fallback)
      -> fetch_buildings(source=osm)
      -> lookup_precip_return_period (Atlas-14 design-storm depth)
      -> build_swmm_mesh (P2: quasi-2D node/link SWMM deck; barriers/buildings/
         infiltration/single-outfall/nested-hyetograph/mass-balance gate)
      -> run_swmm_local (P4: pyswmm IN-PROCESS - the dev primary path)
      -> postprocess_swmm (P3: rasterize per-timestep node INVERT_DEPTH ->
         peak primary COG + per-frame COGs)
      -> publish the peak primary + emit the frames via the Phase-1 Step-9b
         emitter block (frames out-of-band via emitter.add_loaded_layer; the
         peak is the single returned LayerURI).

Returns the PEAK ``SWMMDepthLayerURI`` directly (a ``LayerURI`` subtype) so the
``emit_tool_call`` ``add_loaded_layer`` gate fires on it - exactly like
``run_modflow_job`` returns a ``PlumeLayerURI``. The per-frame depth COGs are
emitted OUT-OF-BAND through ``emitter.add_loaded_layer`` (distinct runs-bucket
keys -> distinct TiTiler url -> no dedup collapse) so the web
``detectSequentialGroups`` LayerPanel scrubber group forms WITHOUT changing the
single-LayerURI return shape (no re-publish trip in ``summarize_tool_result``).

Determinism boundary (Invariant 1): every depth number the agent narrates comes
from the typed ``SWMMDepthLayerURI.max_depth_m`` / ``.flooded_area_km2`` /
``.n_buildings_affected`` fields the postprocess computed with plain arithmetic
- never free-generated.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.swmm_contracts import SWMMRunArgs
from trid3nt_contracts.swmm_contracts import SWMMDepthLayerURI

from ..pipeline_emitter import (
    begin_substeps,
    current_emitter,
    emit_chart_payloads,
    mint_dispatch_and_sim_cards,
    route_sim_terminal,
    substep,
)
from ..tools.publish_layer import PublishLayerError, publish_layer
from .postprocess_swmm import (
    CONCENTRATION_STYLE_PRESET,
    FLOOD_DEPTH_STYLE_PRESET,
    PostprocessSWMMError,
    postprocess_swmm,
    postprocess_swmm_pollutants,
)
from .run_swmm import (
    SWMM_SOLVER_NAME,
    SWMMWorkflowError,
    build_and_stage_swmm_deck,
    is_local_mode,
    run_swmm_local,
    stage_swmm_manifest,
)
from .solve_progress import drive_live_solve_progress
from .swmm_mesh_builder import estimate_swmm_solve_seconds
from .mesh_layer import make_swmm_mesh_layer_uri
from ..layer_uri_emit import emit_layer_uri, publish_input_layer

logger = logging.getLogger("trid3nt_server.workflows.model_urban_flood_swmm")

__all__ = [
    "model_urban_flood_swmm",
    "UrbanFloodWorkflowError",
]

#: Inches -> mm (Atlas-14 PFDS returns inches; the hyetograph builder wants mm).
_INCH_TO_MM: float = 25.4

#: Minimum urban-flood AOI side length (m). A geocoded single-building / street-
#: address bbox can be a few metres across (Nominatim returns the OSM feature's
#: own footprint bbox); an urban-flood scenario needs at least a block. Below
#: this, the bbox is EXPANDED (centred) to this side length. A normal city-block
#: / neighbourhood AOI is already far above this, so this is a no-op except on a
#: collapsed (single-building) bbox. Floor only; never shrinks. (D2 — the live
#: SWMM run that bounded a single building, case 01KVH4MZ9JF7GGHQ88D5PSWZVH.)
_MIN_URBAN_AOI_SIDE_M: float = 300.0


def _enforce_min_urban_aoi(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Expand a too-small AOI bbox to a sensible urban-flood minimum, centred.

    Floors BOTH side lengths to ``_MIN_URBAN_AOI_SIDE_M`` metres about the bbox
    centroid (lon scaled by cos(lat) for the metres->degrees conversion). Returns
    the bbox UNCHANGED when both sides already meet the floor. Never shrinks.

    Deterministic, safe-by-construction guardrail: it only ever EXPANDS a bbox
    below the floor and is a no-op for any reasonably-sized AOI. It cannot move
    or shrink a normal AOI. The deeper question (the model geocoding a too-precise
    single-building feature) is a judgment issue left for NATE; this is the
    minimal floor that stops a collapsed AOI from solving a near-trivial grid.
    """
    import math

    min_lon, min_lat, max_lon, max_lat = bbox
    cen_lat = 0.5 * (min_lat + max_lat)
    cen_lon = 0.5 * (min_lon + max_lon)
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * max(math.cos(math.radians(cen_lat)), 1e-6)
    width_m = (max_lon - min_lon) * m_per_deg_lon
    height_m = (max_lat - min_lat) * m_per_deg_lat
    if width_m >= _MIN_URBAN_AOI_SIDE_M and height_m >= _MIN_URBAN_AOI_SIDE_M:
        return bbox
    half_lon = 0.5 * max(width_m, _MIN_URBAN_AOI_SIDE_M) / m_per_deg_lon
    half_lat = 0.5 * max(height_m, _MIN_URBAN_AOI_SIDE_M) / m_per_deg_lat
    expanded = (
        cen_lon - half_lon,
        cen_lat - half_lat,
        cen_lon + half_lon,
        cen_lat + half_lat,
    )
    logger.info(
        "model_urban_flood_swmm: AOI floor applied - input bbox %s was "
        "%.0fm x %.0fm (below the %.0fm urban-flood minimum); expanded to %s",
        bbox,
        width_m,
        height_m,
        _MIN_URBAN_AOI_SIDE_M,
        expanded,
    )
    return expanded


class UrbanFloodWorkflowError(RuntimeError):
    """Raised on a fatal composer failure (carries an open-set ``error_code``)."""

    error_code: str = "URBAN_FLOOD_WORKFLOW_FAILED"

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


# --------------------------------------------------------------------------- #
# DEM acquisition (1 m 3DEP -> 10 m fallback) with localization.
# --------------------------------------------------------------------------- #
def _bbox_centroid_latlon(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float]:
    """Return the ``(lat, lon)`` centroid of a ``(min_lon, min_lat, max_lon,
    max_lat)`` bbox (the lat-first point the precip lookup wants)."""
    min_lon, min_lat, max_lon, max_lat = bbox
    return (0.5 * (min_lat + max_lat), 0.5 * (min_lon + max_lon))


def _localize_to_dem_path(uri: str) -> str:
    """Resolve a DEM ``LayerURI.uri`` (gs:// / s3:// / file:// / local) to an
    on-disk GeoTIFF path the mesh builder can read with rasterio.

    The mesh builder reads a local filesystem path; ``fetch_3dep_extra`` /
    ``fetch_dem`` return a cache URI. GCP is decommissioned: ``s3://`` objects
    are staged down to a temp file via boto3 (matching the sfincs_builder
    staging seam); ``file://`` + bare local paths pass through. On a synthetic /
    test path the URI is already local.
    """
    if uri.startswith("file://"):
        return uri[len("file://"):]
    if not uri.startswith("s3://"):
        return uri

    import hashlib

    cache_dir = Path(tempfile.gettempdir()) / "trid3nt-swmm-dem-stage"
    cache_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(uri).suffix or ".tif"
    local = cache_dir / (hashlib.sha256(uri.encode()).hexdigest()[:24] + suffix)
    if local.exists() and local.stat().st_size > 0:
        return str(local)
    tmp = local.with_suffix(local.suffix + ".part")
    from ..tools.solver import _get_s3_client

    bucket_name, _, obj_key = uri[len("s3://"):].partition("/")
    resp = _get_s3_client().get_object(Bucket=bucket_name, Key=obj_key)
    with tmp.open("wb") as fh:
        shutil.copyfileobj(resp["Body"], fh)
    os.replace(tmp, local)
    logger.info("staged DEM %s -> %s (%d bytes)", uri, local, local.stat().st_size)
    return str(local)


def _fetch_dem_for_urban(
    bbox: tuple[float, float, float, float],
) -> tuple[str, str]:
    """Fetch a DEM for the AOI: try ``fetch_3dep_extra`` 1 m first, fall back to
    ``fetch_dem`` 10 m (the data-source fallback norm: primary -> fallback,
    honest typed error if both fail).

    Returns ``(local_dem_path, source_label)``. Raises
    ``UrbanFloodWorkflowError("SWMM_DEM_FETCH_FAILED")`` only when BOTH fail.
    """
    from ..tools.data_fetch import fetch_dem
    from ..tools.fetch_3dep_extra import fetch_3dep_extra

    # Primary: 1 m LiDAR (building-scale resolution the screenshot path wants).
    try:
        layer = fetch_3dep_extra(bbox, resolution="1 meter")
        return _localize_to_dem_path(layer.uri), "USGS 3DEP 1m LiDAR"
    except Exception as exc:  # noqa: BLE001 - fall through to the 10 m fallback
        logger.info(
            "fetch_3dep_extra(1m) failed (%s); falling back to fetch_dem(10m)", exc
        )

    # Fallback: 10 m 3DEP (the canonical default).
    try:
        layer = fetch_dem(bbox, resolution_m=10)
        return _localize_to_dem_path(layer.uri), "USGS 3DEP 10m"
    except Exception as exc:  # noqa: BLE001
        raise UrbanFloodWorkflowError(
            "SWMM_DEM_FETCH_FAILED",
            f"both DEM sources failed for bbox {bbox}: 3DEP-1m + fetch_dem-10m: {exc}",
        ) from exc


def _fetch_buildings_for_urban(
    bbox: tuple[float, float, float, float],
) -> Any:
    """Fetch OSM building footprints for the AOI (the reliable footprint source,
    per memory project_building_footprints_source). Returns the GeoJSON
    FeatureCollection dict, or ``None`` on failure (footprints are an enhancement,
    not a hard gate - the mesh still builds without obstructions)."""
    from ..tools.data_fetch import fetch_buildings

    try:
        layer = fetch_buildings(bbox, source="osm")
    except Exception as exc:  # noqa: BLE001 - buildings are optional
        logger.info("fetch_buildings(osm) failed (%s); proceeding without footprints", exc)
        return None
    # The footprints come back as an inline GeoJSON FeatureCollection on the
    # LayerURI (job-0175 inline-GeoJSON convention) or as a cache URI; the mesh
    # builder accepts the FeatureCollection dict directly.
    fc = getattr(layer, "inline_geojson", None) or getattr(layer, "geojson", None)
    if isinstance(fc, dict) and fc.get("type") == "FeatureCollection":
        return fc
    return None


def make_buildings_input_layer_uri(
    building_footprints: Any,
    *,
    run_id: str,
    runs_bucket: str | None = None,
) -> LayerURI | None:
    """task #207: upload the OSM building footprints + return a role="input" vector.

    The urban-flood deck consumes building footprints as obstructions, but the
    fetched ``FeatureCollection`` was only ever passed to the mesh builder and
    discarded as a renderable layer. Mirror :func:`make_swmm_mesh_layer_uri`:
    upload the FC to the DURABLE runs bucket at
    ``s3://<runs_bucket>/<run_id>/buildings_input.geojson`` (so the emitter can
    inline the s3:// vector on every reconnect) and return a ``role="input"``
    vector ``LayerURI`` with ``bbox=None`` (an input must not emit a competing
    zoom-to).

    Returns ``None`` (best-effort, never fatal) when the FC is empty/malformed or
    the S3 upload fails. SYNC compute + boto3 upload -- the caller wraps it in
    ``asyncio.to_thread`` (never run sync boto3 on the asyncio loop).
    """
    if not isinstance(building_footprints, dict):
        return None
    feats = building_footprints.get("features")
    if not isinstance(feats, list) or len(feats) == 0:
        return None

    try:
        from ..tools.solver import _get_runs_bucket, _get_s3_client

        bucket = runs_bucket or _get_runs_bucket()
        key = f"{run_id}/buildings_input.geojson"
        _get_s3_client().put_object(
            Bucket=bucket,
            Key=key,
            Body=json.dumps(building_footprints).encode("utf-8"),
            ContentType="application/geo+json",
        )
        s3_uri = f"s3://{bucket}/{key}"
    except Exception as exc:  # noqa: BLE001 - best-effort; S3 put failure non-fatal
        logger.warning(
            "make_buildings_input_layer_uri: buildings_input.geojson S3 upload "
            "failed (non-fatal, buildings input absent; run_id=%s): %s",
            run_id,
            exc,
        )
        return None

    n = len(feats)
    plural = "footprint" if n == 1 else "footprints"
    return LayerURI(
        layer_id=f"buildings-input-{run_id}",
        name=f"Building {plural} ({n})",
        layer_type="vector",
        uri=s3_uri,
        style_preset="osm_buildings",
        role="input",
        bbox=None,
    )


def _atlas14_total_depth_mm(
    bbox: tuple[float, float, float, float],
    return_period_yr: int,
    storm_duration_hr: float,
) -> float | None:
    """Look up the Atlas-14 design-storm depth (mm) for the AOI centroid.

    Returns the total storm depth in mm, or ``None`` on lookup failure (the
    builder then uses its sane hyetograph default - never a silent dead-end).
    """
    from ..tools.data_fetch import lookup_precip_return_period

    lat, lon = _bbox_centroid_latlon(bbox)
    try:
        result = lookup_precip_return_period(
            location=(lat, lon),
            return_period_years=int(return_period_yr),
            duration_hours=float(storm_duration_hr),
        )
    except Exception as exc:  # noqa: BLE001 - fall back to the builder default
        logger.info(
            "lookup_precip_return_period failed (%s); using the builder's "
            "hyetograph default depth", exc
        )
        return None
    inches = result.get("precip_inches") if isinstance(result, dict) else None
    if inches is None:
        return None
    return float(inches) * _INCH_TO_MM


def _record_swmm_batch_solve_telemetry(
    *,
    run_result: Any,
    handle: Any,
    build: Any,
    run_id: str,
    compute_class: str,
    session_id: str | None = None,
    case_id: str | None = None,
) -> dict | None:
    """Record ONE SOLVE row for the SWMM off-box Batch lane (task-153).

    Merges the Spot instance + timing breakdown the wait-loop captured onto
    ``run_result.batch_compute_meta`` (best-effort, may be ``None``) with the
    SWMM mesh size descriptor (``build.n_active_cells`` + ``build.resolution_m``)
    + the solver name + terminal status + the run/case/session ids, and writes it
    to the SOLVE telemetry sink (``telemetry.record_solve_telemetry``) so a perf
    model can later infer SWMM completion time. Sibling of the SFINCS
    ``_record_flood_batch_solve_telemetry``. Best-effort; returns the recorded row
    (or ``None`` on any failure) so the call site stays trivial.
    """
    from ..telemetry import record_solve_telemetry

    meta = getattr(run_result, "batch_compute_meta", None) or {}
    if not isinstance(meta, dict):
        meta = {}

    n_active = getattr(build, "n_active_cells", None)
    resolution_m = getattr(build, "resolution_m", None)

    row: dict = {
        "run_id": getattr(run_result, "run_id", None) or run_id,
        "solver": SWMM_SOLVER_NAME,
        "status": getattr(run_result, "status", None),
        "backend": str(getattr(handle, "workflow_name", "") or "unknown"),
        "compute_class": compute_class,
        "case_id": case_id,
        "session_id": session_id,
        "active_cell_count": int(n_active) if n_active is not None else None,
        "resolution_m": float(resolution_m) if resolution_m is not None else None,
    }
    row.update(meta)
    return record_solve_telemetry(row)


# --------------------------------------------------------------------------- #
# The composer.
# --------------------------------------------------------------------------- #
async def model_urban_flood_swmm(
    run_args: SWMMRunArgs,
    *,
    dem_path: str | None = None,
    building_footprints: Any = None,
    run_id: str | None = None,
    compute_class: str = "standard",
    cleanup_deck: bool = True,
    enable_autoscale: bool = True,
) -> SWMMDepthLayerURI:
    """Compose the full quasi-2D PySWMM urban-flood chain end-to-end (LOCAL lane).

    Args:
        run_args: the validated ``SWMMRunArgs`` (bbox + design storm + building
            representation + infiltration + optional barriers).
        dem_path: optional on-disk DEM path. When ``None`` the composer fetches
            it (``fetch_3dep_extra`` 1 m -> ``fetch_dem`` 10 m fallback) from the
            ``run_args.bbox``. Tests pass a synthetic GeoTIFF to skip the fetch.
        building_footprints: optional GeoJSON FeatureCollection. When ``None``
            (and ``dem_path`` was NOT supplied) the composer fetches OSM
            footprints; when ``dem_path`` IS supplied, footprints are used as
            given (tests control them explicitly).
        run_id: optional ULID; minted by the staging step if absent.
        compute_class: FR-CE-3 compute class (carried for provenance; the LOCAL
            lane runs in-process regardless).
        cleanup_deck: when True, the scratch deck dir is removed after
            postprocess (the COGs were already uploaded). Tests pass False to
            inspect the deck.
        enable_autoscale: when True (default) the mesh builder's adaptive budget
            may COARSEN ``run_args.target_resolution_m`` to fit the cell cap.
            When False (the #154 granularity gate's ``narrow_scope`` path) the
            builder honours the user-chosen resolution EXACTLY (the gate already
            clamped it under the cap).

    Returns:
        The PEAK ``SWMMDepthLayerURI`` (role ``"primary"``, name
        ``"Peak flood depth"``) carrying the three narration scalars + the echoed
        barrier geometry. Per-frame depth layers are emitted out-of-band via the
        emitter (Step-9b) so the web scrubber group forms.

    Raises:
        UrbanFloodWorkflowError / SWMMWorkflowError / PostprocessSWMMError on a
        fatal stage failure (the tool wrapper catches these and returns a typed
        error dict so the agent narrates honestly).
    """
    bbox = _enforce_min_urban_aoi(
        tuple(run_args.bbox)  # (min_lon, min_lat, max_lon, max_lat)
    )
    emitter = current_emitter()

    # --- Zoom-on-area-first (job-0160): the map zooms before the solve runs. ---
    # AUTHORITATIVE AOI (job AGENT-AOI / #159): every AOI the user sees and the
    # sim consumes is the SAME ``bbox`` - the FLOORED value from
    # _enforce_min_urban_aoi above, not the raw geocoded ``run_args.bbox``. A
    # collapsed single-building geocode emits an early competing zoom-to to its
    # tiny bbox (server.py geocode snap); this floored zoom-to is emitted next so
    # the camera + the drawn AOI rectangle land on the floored extent (the one
    # the DEM/buildings/mesh use). The floored AOI is re-asserted as the
    # AUTHORITATIVE LAST zoom-to just before the return (see below), and the
    # returned peak's ``bbox`` is stamped to the SAME floored value so the
    # dispatch add_loaded_layer zoom-to + the persisted Case AOI agree with it.
    if emitter is not None:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(bbox)})
        except Exception as exc:  # noqa: BLE001 - non-fatal UX hint
            logger.warning("model_urban_flood_swmm: zoom-to emit failed: %s", exc)

    # --- Step 1: DEM (1 m 3DEP primary -> 10 m fallback) --------------------
    # BREAK B (event-loop starvation), pre-solve: _fetch_dem_for_urban is
    # SYNCHRONOUS blocking I/O (HTTP fetch + boto3 S3 stage-down + GDAL VSI
    # reads). Run it OFF the loop in a worker thread so the WS keepalive ping
    # coroutine keeps running while the fetch churns (mirrors the SFINCS
    # _fetcher_chain asyncio.to_thread wrap). _fetch_dem_for_urban does NOT call
    # the loop-bound PipelineEmitter mid-call - it only logs + returns a tuple -
    # so a plain to_thread wrap is correct (no run_coroutine_threadsafe marshaling
    # is required). The async frame still emits around (before/after) the wrap.
    deck_dir_to_clean: str | None = None
    # task-168: declare the planned internal-operation count so the parent card's
    # live breadcrumb can show "k/total". The fetches are conditional (a synthetic
    # dem_path / pre-supplied footprints skip them), so the plan counts only the
    # operations that will actually run for THIS invocation. Degrades gracefully:
    # begin_substeps(None) -> the web shows label + index with no "/N".
    _fetch_dem = dem_path is None
    _fetch_buildings = building_footprints is None and dem_path is None
    _fetch_precip = run_args.total_rain_depth_mm is None
    # DEM (if fetched) + buildings (if fetched) + precip (if looked up) + build
    # deck + solve + postprocess + publish-peak. The solve is ONE substep here
    # (run_solver/wait_for_completion in the Batch lane or run_swmm_local in the
    # in-process lane); the two-card Dispatch/Sim observability is untouched.
    _planned_substeps = (
        int(_fetch_dem)
        + int(_fetch_buildings)
        + int(_fetch_precip)
        + 4  # build deck, solve, postprocess, publish peak
    )
    begin_substeps(emitter, _planned_substeps)

    if dem_path is None:
        async with substep(emitter, "fetch_3dep_extra"):
            local_dem_path, dem_source = await asyncio.to_thread(
                _fetch_dem_for_urban, bbox
            )
    else:
        local_dem_path, dem_source = dem_path, "supplied"
    logger.info("model_urban_flood_swmm: DEM=%s (%s)", local_dem_path, dem_source)

    # --- Step 2: building footprints (OSM) ----------------------------------
    # BREAK B, pre-solve: _fetch_buildings_for_urban is a SYNCHRONOUS HTTP fetch
    # (OSM Overpass). Offload it off the loop too - it is emitter-free (logs +
    # returns a FeatureCollection dict / None), so a plain to_thread wrap is safe.
    if building_footprints is None and dem_path is None:
        async with substep(emitter, "fetch_buildings"):
            building_footprints = await asyncio.to_thread(
                _fetch_buildings_for_urban, bbox
            )

    # --- Step 3: Atlas-14 design-storm depth (populate run_args if unset) ----
    effective_args = run_args
    if run_args.total_rain_depth_mm is None:
        async with substep(emitter, "lookup_precip_return_period"):
            depth_mm = _atlas14_total_depth_mm(
                bbox, run_args.return_period_yr, run_args.storm_duration_hr
            )
        if depth_mm is not None:
            effective_args = run_args.model_copy(
                update={"total_rain_depth_mm": depth_mm}
            )
            logger.info(
                "model_urban_flood_swmm: Atlas-14 depth=%.1f mm (%d-yr, %.0f-hr)",
                depth_mm,
                run_args.return_period_yr,
                run_args.storm_duration_hr,
            )

    try:
        # --- Step 4: build the quasi-2D SWMM deck (build_swmm_mesh) ----------
        # BREAK B, pre-solve: build_and_stage_swmm_deck is a SYNCHRONOUS compute
        # (rasterio DEM read + adaptive-mesh build + .inp staging) with NO
        # loop-bound emitter calls, so offload it off the loop too (mirrors the
        # SFINCS deck-build asyncio.to_thread wrap). A plain to_thread wrap is
        # correct - no run_coroutine_threadsafe marshaling required.
        async with substep(emitter, "build_swmm_mesh"):
            staging = await asyncio.to_thread(
                build_and_stage_swmm_deck,
                effective_args,
                dem_path=local_dem_path,
                building_footprints=building_footprints,
                run_id=run_id,
                enable_autoscale=enable_autoscale,
            )
        deck_dir_to_clean = str(Path(staging.inp_path).parent)

        # --- Computational-mesh layer (NATE task #156) ----------------------
        # Auto-emit the quasi-2D SWMM uniform quad-cell mesh as a clickable
        # "mesh_grid" vector layer so the user can SEE the true mesh structure
        # (where the cells are) over the AOI - the same grid the solver runs on.
        # It is a DEFAULT-VISIBLE CONTEXT backdrop (role="context"), NOT the
        # primary result (that is the flood-depth raster). bbox=None on the
        # LayerURI so add_loaded_layer does NOT emit a competing zoom-to that
        # would fight the AOI camera (zoom-on-area-first owns the view).
        # BEST-EFFORT: a mesh-emit failure must NEVER break the solve - mirror
        # the _emit_frame_layers best-effort pattern (log a warning, continue).
        # The build is SYNC compute (re-reads the staged INP + reprojects every
        # corner) PLUS a boto3 upload of mesh.geojson, so it is OFFLOADED off the
        # event loop via asyncio.to_thread (never run sync compute / boto3 on the
        # asyncio loop). DURABILITY (NATE high-pri shipped bug): the mesh is now
        # uploaded to the DURABLE runs bucket (s3://<runs_bucket>/<run_id>/
        # mesh.geojson), NOT the deck staging dir - so when add_loaded_layer (and
        # any later session-state RE-emit/reconnect) re-reads the LayerURI via
        # _read_vector_uri_as_geojson it finds the object on S3 instead of a
        # deleted /tmp path (which made the mesh VANISH + storm warnings). The
        # deck_dir_to_clean removal later in this workflow no longer affects it.
        try:
            mesh_layer = await asyncio.to_thread(
                make_swmm_mesh_layer_uri,
                staging.build,
                run_id=staging.run_id,
            )
            if mesh_layer is not None and emitter is not None:
                safe = emit_layer_uri(mesh_layer)
                if safe is not None:
                    await emitter.add_loaded_layer(safe)
        except Exception as exc:  # noqa: BLE001 - mesh emit is non-fatal
            logger.warning(
                "model_urban_flood_swmm: mesh layer emit failed (non-fatal): %s",
                exc,
            )

        # --- task #207: surface the building footprints as an INPUT layer ----
        # The OSM footprints fed the mesh as obstructions but were never shown.
        # Surface them as a role="input" vector (bbox=None) alongside the mesh so
        # the user sees the buildings the model treated as obstacles. SYNC FC
        # upload -> OFFLOADED off the loop; BEST-EFFORT (publish_input_layer never
        # raises) so a failure can NEVER break the solve. No-op when no footprints
        # were fetched (building_footprints is None / empty).
        try:
            buildings_layer = await asyncio.to_thread(
                make_buildings_input_layer_uri,
                building_footprints,
                run_id=staging.run_id,
            )
            await publish_input_layer(emitter, buildings_layer)
        except Exception as exc:  # noqa: BLE001 - input surfacing is NEVER fatal
            logger.warning(
                "model_urban_flood_swmm: buildings input emit failed "
                "(non-fatal): %s",
                exc,
            )

        # --- Auto vertical scaling per case (NATE 2026-06-17) ----------------
        # Size the Batch compute_class from the built mesh's active-cell count
        # (the adaptive-mesh budget already coarsened the grid to fit a cap;
        # n_active_cells IS the element count) instead of the caller's blind
        # default. A big urban AOI grabs more compute (up to the new xlarge
        # 48-vCPU tier); a small one stays cheap. select_compute_class never
        # raises - a zero/absent count falls back to the caller's compute_class.
        from ..tools.solver import select_compute_class

        n_active = int(getattr(staging.build, "n_active_cells", 0) or 0)
        if n_active > 0:
            effective_compute_class = select_compute_class(n_active)
            logger.info(
                "model_urban_flood_swmm: auto vertical scaling n_active_cells=%d "
                "-> compute_class=%s (caller requested %s)",
                n_active,
                effective_compute_class,
                compute_class,
            )
        else:
            effective_compute_class = compute_class
            logger.info(
                "model_urban_flood_swmm: no active-cell count; using caller "
                "compute_class=%s for the dispatch",
                compute_class,
            )

        # --- Step 5+6: solve + postprocess ----------------------------------
        # is_local_mode() is True by DEFAULT (TRID3NT_SWMM_LOCAL unset): the
        # urban engine's primary path is pyswmm IN-PROCESS (the `else` branch
        # below, byte-identical to the proven local lane). When the env is
        # flipped (TRID3NT_SWMM_LOCAL=0) the `if not is_local_mode():` branch
        # routes the SAME staged deck through the GENERIC solver-dispatch seam
        # (run_solver -> wait_for_completion -> Batch output) instead. Zero
        # regression until the env is set.
        #
        # LIVE solve-progress heartbeat (NATE 2026-06-17): the solve emits
        # nothing for minutes (off-loop thread OR remote Batch job), so the
        # running card is a silent spinner. Drive the shared solve-progress
        # envelope ON the loop (the emitter is loop-bound) alongside the solve -
        # identical to the proven SFINCS pattern in model_flood_scenario.
        # Best-effort: emitter None -> no-op; cancelled + awaited in a finally
        # regardless of outcome. The heartbeat wraps BOTH lanes.
        # Deployment-aware CPU count (fingerprint audit A6): local-docker
        # reports the HOST cpu count (the web renders it with "CPU" wording);
        # aws-batch keeps the tier lookup byte-identical.
        from ..tools.solver import solve_progress_vcpus

        _swmm_vcpus = solve_progress_vcpus(effective_compute_class)
        if not is_local_mode():
            # --- Out-of-process lane (TRID3NT_SWMM_LOCAL=0): GENERIC Batch seam.
            # Stage the built deck + a worker-contract manifest to S3, then
            # dispatch through run_solver / wait_for_completion (the SAME seam
            # SFINCS uses in model_flood_scenario), PASSING the per-case computed
            # compute_class (auto vertical scaling). The SWMM Batch worker
            # (services/workers/swmm/entrypoint.py) solves the deck and writes
            # completion.json + the .out/.rpt to s3://<runs_bucket>/<run_id>/; we
            # download the .out/.rpt and postprocess from the BATCH output.
            from ..tools.solver import (
                EmitterBinding,
                run_solver,
                set_emitter_binding,
                wait_for_completion,
            )

            manifest_uri = await asyncio.to_thread(stage_swmm_manifest, staging)
            # task-168: surface the off-box solve as ONE nested "run_solver" child
            # row under the parent workflow card. The substep spans the dispatch ->
            # wait -> non-complete guard so a cancel/non-complete solve marks the
            # child red (honesty floor); a complete solve exits the child green.
            # The two-card Dispatch/Sim observability (mint_dispatch_and_sim_cards +
            # route_sim_terminal) and the live Batch readout stay owned by the Sim
            # card EXACTLY as before - this child row is purely additive.
            async with substep(emitter, "run_solver"):
                handle = run_solver(
                    solver=SWMM_SOLVER_NAME,
                    model_setup_uri=manifest_uri,
                    compute_class=effective_compute_class,
                )
                # --- Two-card sim observability (task-149) ------------------
                # Mint the TWO cards the off-box lane shows: a "Dispatch" tool
                # card (records the submit -- solver, queue, Batch jobId) that
                # lands complete immediately, and a "Sim" compute card bound to
                # the SAME jobId that tracks the live Batch job. The ephemeral
                # Batch worker has NO inbound WS; its status flows agent-side via
                # wait_for_completion's poller over the EXISTING WS, so we point
                # the emitter binding at the SIM step before the wait and route
                # the terminal there. Best-effort: emitter None / emit failure
                # never breaks the solve.
                _sim_step_id = await mint_dispatch_and_sim_cards(
                    emitter=emitter,
                    solver=SWMM_SOLVER_NAME,
                    handle=handle,
                    compute_class=effective_compute_class,
                )
                if emitter is not None and _sim_step_id is not None:
                    set_emitter_binding(
                        EmitterBinding(emitter=emitter, step_id=_sim_step_id)
                    )
                _progress_task = asyncio.ensure_future(
                    drive_live_solve_progress(
                        emitter=current_emitter(),
                        run_id=staging.run_id,
                        solver=SWMM_SOLVER_NAME,
                        grid_resolution_m=getattr(
                            staging.build, "resolution_m", None
                        ),
                        active_cell_count=getattr(
                            staging.build, "n_active_cells", None
                        ),
                        vcpus=int(_swmm_vcpus) if _swmm_vcpus is not None else None,
                        eta_seconds=estimate_swmm_solve_seconds(
                            int(getattr(staging.build, "n_active_cells", 0) or 0)
                        ),
                    )
                )
                try:
                    run_result = await wait_for_completion(handle)
                except asyncio.CancelledError:
                    # Invariant 8: the cancel chain is owned by
                    # wait_for_completion; propagate immediately so the WS handler
                    # emits cancelled. Route the cancel to the SIM card
                    # (best-effort terminal send, J-B-i).
                    logger.info(
                        "model_urban_flood_swmm cancelled while awaiting solver"
                    )
                    await route_sim_terminal(emitter, _sim_step_id, run_result=None)
                    raise
                finally:
                    # Tear down the heartbeat (success, failure, OR cancel) +
                    # clear the compute-card emitter binding.
                    _progress_task.cancel()
                    try:
                        await _progress_task
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass
                    set_emitter_binding(None)

                # task-149: route the SIM compute card to its terminal state from
                # the RunResult (complete -> green, non-complete -> red) before the
                # workflow's own non-complete guard re-raises.
                await route_sim_terminal(
                    emitter, _sim_step_id, run_result=run_result
                )

                # --- SOLVE telemetry (task-153): Batch instance + size + timing -
                # Record ONE solve row merging run_result.batch_compute_meta (the
                # Spot instance + queue/compute/total timing the wait-loop
                # captured) with the SWMM mesh size descriptor (n_active_cells +
                # resolution_m) so a perf model can later infer SWMM completion
                # time. Records for BOTH success and failure (a censored failure is
                # itself a data point). Best-effort; a telemetry failure never
                # affects the solve result.
                try:
                    _record_swmm_batch_solve_telemetry(
                        run_result=run_result,
                        handle=handle,
                        build=staging.build,
                        run_id=staging.run_id,
                        compute_class=effective_compute_class,
                    )
                except Exception as exc:  # noqa: BLE001 -- never break the solve
                    logger.warning(
                        "SWMM solve batch-compute telemetry failed (non-fatal): %s",
                        exc,
                    )

                if run_result.status != "complete":
                    # SOLVER_FAILED / SOLVER_TIMEOUT / cancelled -> typed failure
                    # (mirror model_flood_scenario's non-complete guard). The
                    # SWMMWorkflowError below is caught by the except clause +
                    # turned into a typed error dict by the tool wrapper. Raising
                    # it INSIDE the substep marks the run_solver child red.
                    raise SWMMWorkflowError(
                        "SWMM_LOCAL_RUN_FAILED",
                        message=(
                            "SWMM Batch solve did not complete "
                            f"(status={run_result.status}, "
                            f"error_code={run_result.error_code}): "
                            f"{run_result.error_message or run_result.cancellation_reason or ''}"
                        ),
                        details={
                            "run_id": staging.run_id,
                            "output_uri": run_result.output_uri,
                        },
                    )

            # Register-only fast path: if the Batch worker wrote a
            # publish_manifest (MANIFEST_SCHEMA_VERSION=1) alongside
            # completion.json, skip the .out download + agent-side postprocess
            # entirely.  The worker already produced COGs + band_stats + TiTiler
            # tile URLs; we just register them and return early.  Falls through
            # to the legacy download+postprocess path when the manifest is absent
            # (pre-manifest workers, manifest schema unknown).
            from .register_published_manifest import (
                read_publish_manifest,
                register_manifest_layers,
            )
            batch_run_id = getattr(run_result, "run_id", None) or staging.run_id
            _swmm_manifest = await asyncio.to_thread(
                read_publish_manifest, run_result
            )
            if _swmm_manifest is not None:
                async with substep(emitter, "postprocess_swmm"):
                    _swmm_reg = register_manifest_layers(
                        _swmm_manifest, run_id=batch_run_id, bbox=tuple(bbox)
                    )
                if not _swmm_reg.layers:
                    raise SWMMWorkflowError(
                        "SWMM_NO_LAYERS",
                        "worker publish_manifest produced no registered depth "
                        "layers (honesty floor: cannot narrate an empty solve)",
                    )
                _swmm_m = _swmm_reg.metrics
                _swmm_prim = _swmm_reg.layers[0]
                _swmm_frame_layers = _swmm_reg.layers[1:]
                peak = SWMMDepthLayerURI(
                    uri=_swmm_prim.uri,
                    layer_type=_swmm_prim.layer_type,
                    layer_id=_swmm_prim.layer_id,
                    name=_swmm_prim.name,
                    style_preset=_swmm_prim.style_preset,
                    bbox=tuple(bbox),
                    role=_swmm_prim.role,
                    max_depth_m=float(_swmm_m.get("max_depth_m", 0.0)),
                    flooded_area_km2=float(_swmm_m.get("flooded_area_km2", 0.0)),
                    n_buildings_affected=int(
                        _swmm_m.get("n_buildings_affected", 0)
                    ),
                )
                # Authoritative bbox stamp + buildings-obstacle name suffix
                # (mirror the non-manifest path below).
                _n_bldg_dropped = int(
                    getattr(staging.build, "n_buildings_dropped", 0) or 0
                )
                _peak_upd: dict[str, Any] = {}
                if tuple(peak.bbox or ()) != tuple(bbox):
                    _peak_upd["bbox"] = tuple(bbox)
                if _n_bldg_dropped > 0 and "(" not in (peak.name or ""):
                    _plural = (
                        "building" if _n_bldg_dropped == 1 else "buildings"
                    )
                    _peak_upd["name"] = (
                        f"{peak.name} ({_n_bldg_dropped} {_plural} as obstacles)"
                    )
                if _peak_upd:
                    peak = peak.model_copy(update=_peak_upd)
                # Emit frame animation layers (already TiTiler URLs; no
                # publish_layer round-trip needed).
                _emitted_frames = await _emit_frame_layers(
                    emitter,
                    _swmm_frame_layers,  # type: ignore[arg-type]
                    batch_run_id,
                )
                # Authoritative zoom-to (mirrors the non-manifest path).
                if emitter is not None:
                    try:
                        await emitter.emit_map_command(
                            "zoom-to", {"bbox": list(bbox)}
                        )
                    except Exception as _ze:  # noqa: BLE001
                        logger.warning(
                            "model_urban_flood_swmm: zoom-to (manifest path) "
                            "failed: %s",
                            _ze,
                        )
                if cleanup_deck and deck_dir_to_clean:
                    _cleanup_deck_dir(deck_dir_to_clean)
                logger.info(
                    "model_urban_flood_swmm (manifest path) run_id=%s "
                    "max_depth_m=%.4g flooded_area_km2=%.6g "
                    "n_buildings_dropped=%d n_buildings_affected=%d "
                    "frames_emitted=%d/%d peak_uri=%s",
                    batch_run_id,
                    peak.max_depth_m,
                    peak.flooded_area_km2,
                    _n_bldg_dropped,
                    peak.n_buildings_affected,
                    _emitted_frames,
                    len(_swmm_frame_layers),
                    peak.uri,
                )
                return peak

            # --- Legacy path: download .out + agent-side postprocess ----------
            # Download the Batch .out (+ .rpt for continuity provenance) to a
            # local tmp dir, then postprocess from a run-shim carrying the local
            # out_path (postprocess_swmm reads only run.out_path; the S_i_j
            # cell<->node map lives in staging.build, agent-side, unchanged).
            #
            # ROOT-CAUSE (NATE: "Batch succeeded + published layers but the
            # composer's RESULT came back null/no narration"): the AWS-Batch
            # dispatch (_run_solver_aws_batch) MINTS A FRESH run_id (new_ulid())
            # for the job and the worker writes completion.json + the .out/.rpt
            # under s3://<runs_bucket>/<run_result.run_id>/ -- NOT under the
            # deck-build's staging.run_id. Passing staging.run_id here pointed
            # the download at an EMPTY prefix, so completion.json + .out were
            # never found and the branch raised SWMM_BATCH_OUTPUT_MISSING (or,
            # worse, the postprocess ran on an absent/empty out and the result
            # never populated the narration scalars) -- exactly the
            # silent-no-narration symptom. Mirror model_flood_scenario's SFINCS
            # Batch path, which postprocesses from run_result.output_uri /
            # run_result.run_id (the worker's run_id), NEVER the staged deck's
            # id. Fall back to staging.run_id only if the RunResult carries no
            # run_id (defensive).
            batch_run_id = getattr(run_result, "run_id", None) or staging.run_id
            # task-168: the Batch-output download + rasterize-to-COG postprocess is
            # ONE user-meaningful "postprocess_swmm" child row. A download miss
            # (SWMM_BATCH_OUTPUT_MISSING) or a postprocess failure raises inside the
            # substep and marks the child red; a clean run exits it green.
            async with substep(emitter, "postprocess_swmm"):
                run, batch_out_dir = await asyncio.to_thread(
                    _download_batch_swmm_outputs, run_result, batch_run_id
                )
                try:
                    layers, metrics = await asyncio.to_thread(
                        postprocess_swmm,
                        run,
                        staging.build,
                        run_id=staging.run_id,
                        building_footprints=building_footprints,
                    )
                finally:
                    _cleanup_deck_dir(batch_out_dir)
        else:
            # --- In-process lane (DEFAULT): pyswmm in this venv ---------------
            # BREAK B (event-loop starvation): run_swmm_local is a SYNCHRONOUS
            # ~16-min pyswmm solve. Calling it inline on the async event loop
            # blocks the loop for the entire solve -> the WS keepalive ping
            # coroutine never runs -> the socket dies (ConnectionClosedError x40)
            # -> every later emit/persist lands on a dead socket and the terminal
            # layer never surfaces. The remedy is to push the blocking call OFF
            # the loop onto a worker thread so the loop stays responsive
            # (ping/pong keeps the WS alive) while pyswmm churns. run_swmm_deck
            # (the body of run_swmm_local) does NOT report progress through the
            # async PipelineEmitter mid-solve - it is a self-contained
            # synchronous compute with no loop-bound calls - so a plain to_thread
            # wrap is correct here: no asyncio.run_coroutine_threadsafe
            # marshaling / progress-queue draining is required (there are no
            # emitter calls to marshal back). When mid-solve emitter progress IS
            # added later, switch to run_coroutine_threadsafe(loop) inside the
            # worker. (Mirrors model_flood_scenario's asyncio.to_thread
            # off-loading of its blocking fetcher/solve stages.)
            # task-168: surface the in-process pyswmm solve as a "run_solver" child
            # row (engine-agnostic raw label the web humanizes to "Running the
            # solver"). The substep spans the heartbeat-wrapped solve so a cancel /
            # solve failure marks the child red; the live solve heartbeat is
            # unchanged inside.
            async with substep(emitter, "run_solver"):
                _progress_task = asyncio.ensure_future(
                    drive_live_solve_progress(
                        emitter=current_emitter(),
                        run_id=staging.run_id,
                        solver=SWMM_SOLVER_NAME,
                        grid_resolution_m=getattr(
                            staging.build, "resolution_m", None
                        ),
                        active_cell_count=getattr(
                            staging.build, "n_active_cells", None
                        ),
                        vcpus=int(_swmm_vcpus) if _swmm_vcpus is not None else None,
                        eta_seconds=estimate_swmm_solve_seconds(
                            int(getattr(staging.build, "n_active_cells", 0) or 0)
                        ),
                    )
                )
                try:
                    run = await asyncio.to_thread(run_swmm_local, staging)
                finally:
                    # Tear down the heartbeat (success, failure, OR cancel).
                    _progress_task.cancel()
                    try:
                        await _progress_task
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass

            # --- Step 6: postprocess (rasterize node depths -> peak + frames) -
            # BREAK B, post-solve: postprocess_swmm is a SYNCHRONOUS compute
            # (pyswmm Output read + per-step grid scatter + COG rasterize/reproject
            # + S3 upload) - heavy blocking I/O + GDAL that would stall the loop
            # inline. It builds the peak + frame COGs OFF-LINE (its own internal
            # _emit_frame_layers only WRITES COGs - it does NOT touch the
            # loop-bound PipelineEmitter / add_loaded_layer; the emitter
            # add_loaded_layer happens back on the loop in _emit_frame_layers
            # below) so a plain to_thread wrap is correct - no
            # run_coroutine_threadsafe marshaling required.
            async with substep(emitter, "postprocess_swmm"):
                layers, metrics = await asyncio.to_thread(
                    postprocess_swmm,
                    run,
                    staging.build,
                    run_id=staging.run_id,
                    building_footprints=building_footprints,
                )
    except (SWMMWorkflowError, PostprocessSWMMError):
        # Cleanup before re-raising - the tool wrapper turns these into a typed
        # error dict.
        if cleanup_deck and deck_dir_to_clean:
            _cleanup_deck_dir(deck_dir_to_clean)
        raise

    if not layers:
        if cleanup_deck and deck_dir_to_clean:
            _cleanup_deck_dir(deck_dir_to_clean)
        raise UrbanFloodWorkflowError(
            "SWMM_NO_LAYERS",
            "postprocess_swmm produced no depth layers (empty solve?)",
        )

    raw_peak = layers[0]
    frame_layers = layers[1:]

    # --- Step 7 (BREAK A): publish the PEAK COG through publish_layer ---------
    # postprocess_swmm returns the peak + frame COGs as RAW s3:// object URIs.
    # A raw object-store URI NEVER renders in MapLibre and the job-0254 emission
    # guardrail (layer_uri_emit) DROPS a renderable raster carrying s3:// - so
    # without publishing, the peak silently vanishes from the map and persists no
    # renderable loaded_layer (BREAK A). Mirror the SFINCS model_flood_scenario
    # Step-9 publish-or-honest-drop path: route the peak COG through publish_layer
    # (the _resolve_titiler_style_params render chokepoint) so it carries a
    # published /tiles or WMS URL before it is returned. The returned LayerURI's
    # dispatch-level emit_layer_uri seam then PASSES it (http(s) renders) and
    # persists it as a renderable primary loaded_layer.
    #
    # On publish failure we return the peak UNPUBLISHED (raw s3://): the dispatch
    # guardrail drops the dead raster from the map (honest - no broken row) while
    # the typed narration scalars (max_depth_m / flooded_area_km2 /
    # n_buildings_affected) still reach the LLM so the failure is narrated and the
    # job-0177 retry loop can re-attempt. The wrapper REQUIRES a SWMMDepthLayerURI
    # return, so we never drop the whole layer - only its renderability.
    # BREAK B, post-solve: _publish_peak_layer drives publish_layer (the COG
    # rasterize/reproject/upload + the publish-status time.sleep polls) - all
    # SYNCHRONOUS blocking work. It does NOT call the loop-bound PipelineEmitter
    # (the peak's add_loaded_layer fires at the dispatch site, held #6, on the
    # returned LayerURI - NOT inside this function), so offload the whole call off
    # the loop. A plain to_thread wrap is correct - no run_coroutine_threadsafe
    # marshaling required.
    async with substep(emitter, "publish_layer"):
        peak = await asyncio.to_thread(_publish_peak_layer, raw_peak, staging.run_id)

    # --- AUTHORITATIVE AOI stamp (job AGENT-AOI / #159) ----------------------
    # Stamp the returned peak's ``bbox`` to the SAME floored AOI the sim/DEM/
    # buildings/mesh consumed (``bbox`` == _enforce_min_urban_aoi(run_args.bbox)),
    # NOT the COG-derived extent that may drift from the floor by a mesh-cell snap.
    # The dispatch-site add_loaded_layer fires a zoom-to from THIS bbox (the last
    # live zoom-to of the turn) and the persisted Case AOI is derived from it, so
    # this single value guarantees the drawn rectangle == the sim extent and a
    # re-entry snaps to the floored extent rather than the collapsed geocode bbox.
    # model_copy keeps every narration scalar + the published uri intact.
    #
    # OBSERVABILITY (NATE): also fold n_buildings_dropped - the count of building
    # footprints applied as OBSTACLES (holes in the mesh) - into the peak layer
    # NAME so it is VISIBLE in the LayerPanel + the narration that buildings were
    # used as obstacles (n_buildings_affected is a SEPARATE flooding metric that
    # reads 0 on a dry run, which made NATE doubt obstacles were applied). The
    # bbox + name updates are merged into a single model_copy so neither is lost.
    n_buildings_dropped = int(getattr(staging.build, "n_buildings_dropped", 0) or 0)
    peak_updates: dict[str, Any] = {}
    if tuple(peak.bbox or ()) != tuple(bbox):
        peak_updates["bbox"] = tuple(bbox)
    if n_buildings_dropped > 0 and "(" not in (peak.name or ""):
        plural = "building" if n_buildings_dropped == 1 else "buildings"
        peak_updates["name"] = (
            f"{peak.name} ({n_buildings_dropped} {plural} as obstacles)"
        )
    if peak_updates:
        peak = peak.model_copy(update=peak_updates)

    # --- Step 7b / 9b: publish + emit the per-frame animation layers OUT-OF-BAND
    # Mirrors model_flood_scenario Step-9b: each frame is a DISTINCT COG (distinct
    # runs-bucket key -> distinct published url -> no dedup collapse). Each frame
    # COG is published through publish_layer (renderable URL) and emitted in
    # ascending step order via emitter.add_loaded_layer so all N frames arrive as
    # one contiguous sequential group; the "Flood depth step N" name token is
    # preserved so the web detectSequentialGroups scrubber group forms. Frames are
    # emitted ONLY through the emitter (NOT returned), so they never reach
    # summarize_tool_result. When the emitter is None (direct/smoke/test) frame
    # emission is skipped - the frames still live in `layers` for tests to assert.
    emitted_frames = await _emit_frame_layers(emitter, frame_layers, staging.run_id)

    # --- Step 7c: WATER-QUALITY (buildup/washoff) additive context ----------
    # When the run authored WQ sections (staging.pollutants non-empty), read the
    # outfall pollutograph + cumulative outfall LOAD + per-cell peak washoff-
    # concentration COGs from the SAME solved .out/.rpt, publish each concentration
    # layer through the render chokepoint + emit it as role="context", and emit the
    # pollutograph chart. Depth stays the PRIMARY return: WQ is additive, so a WQ
    # failure never sinks the flood headline (best-effort, mirrors the input-layer
    # emits + the registry-quantities block below). Off-loop per the
    # no-sync-blocking norm (pyswmm Output read + COG rasterize/upload).
    if getattr(staging, "pollutants", None):
        try:
            await _publish_and_emit_wq(
                emitter, run, staging, bbox=tuple(bbox)
            )
        except Exception as exc:  # noqa: BLE001 - WQ is additive; never fatal
            logger.warning(
                "model_urban_flood_swmm: WQ postprocess/emit failed (non-fatal, "
                "depth headline intact) run_id=%s: %s",
                staging.run_id,
                exc,
            )

    # --- levers STEP 3 (gated): ADDITIVE registry quantities -----------------
    # The depth peak + frames above are the byte-identical headline. When the
    # registry-quantities flag is on, ALSO publish FLOODING_LOSSES / PONDED_VOLUME
    # / conduit FLOW_RATE / FLOW_VELOCITY as context layers from the SAME .out.
    # Non-fatal: a failure here never sinks the depth layers.
    import os as _os

    if _os.environ.get("TRID3NT_SWMM_REGISTRY_QUANTITIES", "").lower() in (
        "1", "true", "on", "yes"
    ):
        try:
            from .postprocess_swmm import publish_swmm_quantities
            from .register_published_manifest import register_manifest_layers

            reg = await asyncio.to_thread(
                lambda: publish_swmm_quantities(
                    run,
                    staging.build,
                    run_id=staging.run_id,
                    register_manifest_layers=register_manifest_layers,
                    bbox=tuple(bbox),
                )
            )
            if emitter is not None and reg is not None:
                for extra_layer in getattr(reg, "layers", []) or []:
                    try:
                        await emitter.add_loaded_layer(extra_layer)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("could not add swmm registry layer: %s", exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "model_urban_flood_swmm registry-quantity publish failed "
                "(non-fatal): %s",
                exc,
            )

    # OBSERVABILITY (NATE): the completion log surfaces n_buildings_dropped (the
    # count of building footprints applied as OBSTACLES - holes in the mesh, the
    # "drop" representation) ALONGSIDE n_buildings_affected. The prior log showed
    # only n_buildings_affected (a FLOODING metric that read 0 on a dry run),
    # which made it look like buildings were never used as obstacles. The two
    # numbers are distinct: n_buildings_dropped = obstacles applied to the mesh,
    # n_buildings_affected = footprints touched by water at peak. n_buildings_
    # dropped was already computed above (folded into the peak name).
    logger.info(
        "model_urban_flood_swmm complete run_id=%s max_depth_m=%.4g "
        "flooded_area_km2=%.6g n_buildings_dropped=%d n_buildings_affected=%d "
        "frames_emitted=%d/%d continuity=%+.3f%% peak_uri=%s",
        staging.run_id,
        peak.max_depth_m,
        peak.flooded_area_km2,
        n_buildings_dropped,
        peak.n_buildings_affected,
        emitted_frames,
        len(frame_layers),
        run.continuity_error_pct,
        peak.uri,
    )

    # --- Step 8: cleanup the scratch deck (COGs already uploaded) -----------
    if cleanup_deck and deck_dir_to_clean:
        _cleanup_deck_dir(deck_dir_to_clean)

    # --- AUTHORITATIVE LAST zoom-to (job AGENT-AOI / #159) -------------------
    # Re-assert the floored AOI as the FINAL composer-side zoom-to so it
    # SUPERSEDES the early competing geocode snap (a collapsed single-building
    # bbox) regardless of whether the peak published renderably. The dispatch
    # add_loaded_layer below also zooms from peak.bbox (now floored), but a
    # publish-degraded peak whose s3:// uri is dropped at the guardrail must STILL
    # leave the floored AOI as the last camera command - so we emit it here too.
    # Best-effort: emitter None / emit failure never affects the returned peak.
    if emitter is not None:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(bbox)})
        except Exception as exc:  # noqa: BLE001 - non-fatal UX hint
            logger.warning(
                "model_urban_flood_swmm: authoritative zoom-to emit failed: %s", exc
            )

    # The PEAK SWMMDepthLayerURI is returned directly - the emit_tool_call
    # add_loaded_layer gate fires on it (a LayerURI subtype) and persists it as a
    # renderable primary loaded_layer. Invariant 1: the agent narrates
    # peak.max_depth_m / .flooded_area_km2 / .n_buildings_affected.
    return peak


def _publish_peak_layer(
    raw_peak: SWMMDepthLayerURI, run_id: str
) -> SWMMDepthLayerURI:
    """Publish the PEAK depth COG through publish_layer (BREAK A render chokepoint).

    Routes the raw s3:// peak COG through ``publish_layer`` (the
    ``_resolve_titiler_style_params`` render seam) and returns a NEW
    ``SWMMDepthLayerURI`` carrying the published /tiles or WMS URL plus the
    narration scalars + echoed barriers. On publish failure (e.g. QGIS-on-AWS not
    yet landed - job-0308) the raw peak is returned UNCHANGED: the dispatch-level
    ``emit_layer_uri`` guardrail then drops the dead raw-s3:// raster from the map
    (honest - no broken layer row) while the typed metrics still narrate. The
    wrapper requires a ``SWMMDepthLayerURI`` return, so we never drop the layer
    object itself - only its renderability degrades.

    Mirrors the SFINCS ``model_flood_scenario`` Step-9 primary publish (a raster
    carrying a raw object-store URI takes the publish-or-honest-drop gate).
    """
    if raw_peak.layer_type != "raster" or not (
        raw_peak.uri.startswith("gs://") or raw_peak.uri.startswith("s3://")
    ):
        # Already a renderable URL (defensive) - return as-is.
        return raw_peak
    layer_id_for_pub = f"swmm-depth-peak-{run_id}"
    try:
        published_uri = publish_layer(
            layer_uri=raw_peak.uri,
            layer_id=layer_id_for_pub,
            style_preset=raw_peak.style_preset or FLOOD_DEPTH_STYLE_PRESET,
        )
    except PublishLayerError as exc:
        logger.warning(
            "model_urban_flood_swmm: publish_layer FAILED for the peak "
            "layer_id=%s error_code=%s (%s) - returning the unpublished peak. "
            "Its raw s3:// uri never renders, so the dispatch guardrail drops it "
            "from the map; the depth metrics still narrate honestly and the "
            "retry-on-failure loop (job-0177) can re-attempt publish.",
            layer_id_for_pub,
            exc.error_code,
            exc,
        )
        return raw_peak
    # Substitute the published URL into a fresh SWMMDepthLayerURI so the returned
    # layer renders directly while preserving the narration scalars + barriers.
    return SWMMDepthLayerURI(
        layer_id=layer_id_for_pub,
        name=raw_peak.name,
        layer_type=raw_peak.layer_type,
        uri=published_uri,
        style_preset=raw_peak.style_preset or FLOOD_DEPTH_STYLE_PRESET,
        role=raw_peak.role,
        units=raw_peak.units,
        bbox=raw_peak.bbox,
        max_depth_m=raw_peak.max_depth_m,
        flooded_area_km2=raw_peak.flooded_area_km2,
        n_buildings_affected=raw_peak.n_buildings_affected,
        barriers=raw_peak.barriers,
    )


async def _emit_frame_layers(
    emitter: Any, frame_layers: list[SWMMDepthLayerURI], run_id: str
) -> int:
    """Publish + emit per-frame depth COGs out-of-band so the web scrubber forms.

    Each frame COG is routed through ``publish_layer`` (BREAK A render chokepoint)
    so it carries a renderable /tiles or WMS URL before ``add_loaded_layer``;
    without this every frame is a raw s3:// COG the job-0254 guardrail drops, so
    the scrubber group never forms on the map. The "Flood depth step N" name token
    is preserved so the web ``detectSequentialGroups`` groups them. A frame that
    fails to publish is HONESTLY DROPPED (its raw uri never renders) - the
    remaining frames + the peak stay intact; if too many drop the group may fall
    below 2 members and simply not form (acceptable, never a fake row).

    Returns the number of frames emitted (0 when no emitter is bound - the
    direct/smoke/test path). Never raises - a frame publish/emit failure must not
    sink the peak layer (the postprocess_flood honesty stance carried into the
    composer).
    """
    if not frame_layers or emitter is None:
        if frame_layers:
            logger.info(
                "model_urban_flood_swmm: %d animation frames available but no "
                "emitter bound (direct/smoke/test) - frames not emitted.",
                len(frame_layers),
            )
        return 0
    emitted = 0
    for lyr in frame_layers:
        # Defensive: a frame that is already a renderable URL (not raw object
        # store) emits as-is; otherwise publish it through the render chokepoint.
        if not (lyr.uri.startswith("gs://") or lyr.uri.startswith("s3://")):
            emit_layer: LayerURI = lyr
        else:
            try:
                # BREAK B, post-solve: offload ONLY the publish_layer compute
                # (COG rasterize/reproject/upload + the publish-status time.sleep
                # polls - SYNCHRONOUS blocking work) off the loop. The
                # add_loaded_layer emit MUST stay on the loop (it is loop-bound),
                # so this thread-offloads the per-frame publish and then emits on
                # the loop below - NEVER the whole emit loop.
                frame_uri = await asyncio.to_thread(
                    publish_layer,
                    layer_uri=lyr.uri,
                    layer_id=lyr.layer_id,
                    style_preset=lyr.style_preset or FLOOD_DEPTH_STYLE_PRESET,
                )
            except PublishLayerError as exc:
                logger.warning(
                    "model_urban_flood_swmm: publish_layer FAILED for frame "
                    "layer_id=%s error_code=%s (%s) - dropping this frame from "
                    "the animation group (its raw s3:// uri never renders).",
                    lyr.layer_id,
                    exc.error_code,
                    exc,
                )
                continue
            # Keep the "Flood depth step N" name token so the web grouping forms.
            emit_layer = SWMMDepthLayerURI(
                layer_id=lyr.layer_id,
                name=lyr.name,
                layer_type=lyr.layer_type,
                uri=frame_uri,
                style_preset=lyr.style_preset or FLOOD_DEPTH_STYLE_PRESET,
                role=lyr.role,
                units=lyr.units,
                bbox=lyr.bbox,
                max_depth_m=lyr.max_depth_m,
                flooded_area_km2=lyr.flooded_area_km2,
                n_buildings_affected=lyr.n_buildings_affected,
                barriers=lyr.barriers,
            )
        try:
            await emitter.add_loaded_layer(emit_layer)
            emitted += 1
        except Exception as exc:  # noqa: BLE001 - never break the solve
            logger.warning(
                "model_urban_flood_swmm: frame add_loaded_layer failed for %s: %s",
                emit_layer.layer_id,
                exc,
            )
    if emitted:
        logger.info(
            "model_urban_flood_swmm: emitted %d/%d animation frames as a "
            "sequential group (run_id=%s)",
            emitted,
            len(frame_layers),
            run_id,
        )
    return emitted


async def _publish_and_emit_wq(
    emitter: Any, run: Any, staging: Any, *, bbox: tuple[float, float, float, float]
) -> None:
    """Read + publish + emit the WATER-QUALITY additive context (sprint-WQ).

    Runs ``postprocess_swmm_pollutants`` off-loop (pyswmm Output read + COG
    rasterize/upload — sync blocking work), publishes each per-cell peak washoff-
    concentration COG through ``publish_layer`` (the render chokepoint) so it
    carries a renderable URL, emits it as a ``role="context"`` layer beside the
    depth headline, and emits the outfall pollutograph chart. Best-effort at every
    step: a publish/emit miss drops just that piece (its raw s3:// uri never
    renders, so the guardrail hides it), never the depth primary. No-op when no
    emitter is bound (direct/smoke/test) beyond computing the metrics for the log.
    """
    async with substep(emitter, "postprocess_swmm_pollutants"):
        pol_layers, series, metrics = await asyncio.to_thread(
            postprocess_swmm_pollutants,
            run,
            staging.build,
            run_id=staging.run_id,
        )

    logger.info(
        "model_urban_flood_swmm WQ run_id=%s metrics=%s",
        staging.run_id,
        {
            n: {
                "outfall_load": round(float(m.get("outfall_load", 0.0)), 4),
                "units": m.get("outfall_load_units"),
                "peak_conc": round(float(m.get("peak_outfall_conc", 0.0)), 4),
                "washoff_frac": m.get("washoff_mass_fraction"),
                "wq_continuity_pct": m.get("wq_continuity_error_pct"),
            }
            for n, m in (metrics or {}).items()
        },
    )

    if emitter is None:
        return

    # Publish + emit each concentration layer through the render chokepoint.
    from trid3nt_contracts.swmm_contracts import SWMMPollutantLayerURI

    for lyr in pol_layers:
        emit_layer: LayerURI = lyr
        if lyr.uri.startswith("gs://") or lyr.uri.startswith("s3://"):
            try:
                pub_uri = await asyncio.to_thread(
                    publish_layer,
                    layer_uri=lyr.uri,
                    layer_id=lyr.layer_id,
                    style_preset=lyr.style_preset or CONCENTRATION_STYLE_PRESET,
                )
            except PublishLayerError as exc:
                logger.warning(
                    "model_urban_flood_swmm: WQ publish_layer FAILED for %s "
                    "(%s) - dropping the concentration raster (metrics still "
                    "narrate).",
                    lyr.layer_id,
                    exc,
                )
                continue
            # Stamp the AOI bbox + the published URL onto a fresh layer so it
            # renders and the WQ scalars stay intact (model_copy keeps all fields).
            emit_layer = lyr.model_copy(update={"uri": pub_uri, "bbox": tuple(bbox)})
        try:
            await emitter.add_loaded_layer(emit_layer)
        except Exception as exc:  # noqa: BLE001 - never break the solve
            logger.warning(
                "model_urban_flood_swmm: WQ add_loaded_layer failed for %s: %s",
                emit_layer.layer_id,
                exc,
            )

    # Emit the outfall pollutograph chart (best-effort; None when < 2 points).
    try:
        from ..tools.chart_tools import build_pollutograph_chart

        units_by = {
            n: str(m.get("pollutant_units", "")) for n, m in (metrics or {}).items()
        }
        chart = build_pollutograph_chart(
            series_by_pollutant=series,
            units_by_pollutant=units_by,
        )
        if chart is not None:
            await emit_chart_payloads(chart)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "model_urban_flood_swmm: pollutograph chart emit failed (non-fatal): %s",
            exc,
        )


def _cleanup_deck_dir(deck_dir: str) -> None:
    """Best-effort removal of the scratch deck dir (mirrors run_modflow_tool)."""
    try:
        p = Path(deck_dir)
        # Only remove a temp dir we created (prefix swmm-).
        base = p
        for _ in range(3):
            if base.name.startswith("swmm-"):
                shutil.rmtree(base, ignore_errors=True)
                return
            base = base.parent
        shutil.rmtree(p, ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass


class _BatchSWMMRun:
    """A minimal ``swmm_mesh_builder.RunResult`` shim for the Batch lane.

    ``postprocess_swmm`` reads ONLY ``run.out_path`` (the local pyswmm ``.out``)
    plus ``run.continuity_error_pct`` for narration provenance; the S_i_j
    cell<->node map lives in ``staging.build`` (agent-side, unchanged). The Batch
    worker solved the deck remotely and uploaded the ``.out``/``.rpt`` to the
    runs bucket, so we hand postprocess a shim carrying the DOWNLOADED local
    ``out_path`` (+ the continuity read from the downloaded ``.rpt``). No change
    to ``postprocess_swmm`` is required (Change 3: do the download in the
    composer, keep postprocess minimal)."""

    def __init__(self, out_path: str, continuity_error_pct: float) -> None:
        self.out_path = out_path
        self.continuity_error_pct = continuity_error_pct


def _download_batch_swmm_outputs(run_result: Any, run_id: str) -> tuple[Any, str]:
    """Download the Batch ``.out`` (+ ``.rpt``) to a tmp dir for postprocess.

    The SWMM Batch worker (``services/workers/swmm/entrypoint.py``) uploads the
    ``mesh.out`` / ``mesh.rpt`` it produced under
    ``s3://<runs_bucket>/<run_id>/`` and records their full URIs in the
    completion.json ``output_uris``. We re-read completion.json (small, already
    on S3) to find the EXACT ``.out``/``.rpt`` keys (robust to the deck filename),
    download them via the SAME boto3 client the solver dispatch uses (no new
    client), read continuity from the ``.rpt`` (``swmm_mesh_builder``'s
    ``read_flow_routing_continuity``), and return a run-shim carrying the local
    ``out_path`` + a tmp-dir path for the caller to clean up.

    Args:
        run_result: the terminal ``RunResult`` from ``wait_for_completion``
            (``output_uri = s3://<runs_bucket>/<run_id>/``).
        run_id: the run id the outputs are keyed under.

    Returns:
        ``(_BatchSWMMRun, tmp_dir)`` — feed the shim to ``postprocess_swmm`` and
        pass ``tmp_dir`` to ``_cleanup_deck_dir`` afterward.

    Raises:
        SWMMWorkflowError("SWMM_BATCH_OUTPUT_MISSING"): the completed run did not
            produce a downloadable ``.out`` (a 'complete' solve with no output is
            a real failure - never a silent dead-end).
    """
    from ..tools.solver import (
        _get_runs_bucket,
        _get_s3_client,
        _split_object_uri,
        _try_get_completion_s3,
    )
    from .swmm_mesh_builder import read_flow_routing_continuity

    runs_bucket = _get_runs_bucket()
    s3 = _get_s3_client()

    # Resolve the exact .out/.rpt object keys from completion.json output_uris;
    # fall back to the conventional mesh.out / mesh.rpt under the runs prefix.
    out_keys: list[str] = []
    rpt_keys: list[str] = []
    manifest = _try_get_completion_s3(runs_bucket, run_id)
    if isinstance(manifest, dict):
        for raw in manifest.get("output_uris") or []:
            uri = str(raw)
            try:
                _scheme, _bucket, key = _split_object_uri(uri)
            except Exception:  # noqa: BLE001 — skip an unparseable entry
                continue
            if key.endswith(".out"):
                out_keys.append(key)
            elif key.endswith(".rpt"):
                rpt_keys.append(key)
    if not out_keys:
        out_keys = [f"{run_id}/mesh.out"]
    if not rpt_keys:
        rpt_keys = [f"{run_id}/mesh.rpt"]

    tmp_dir = tempfile.mkdtemp(prefix=f"swmm-batch-out-{run_id}-")

    def _download(key: str) -> str | None:
        dest = Path(tmp_dir) / Path(key).name
        try:
            resp = s3.get_object(Bucket=runs_bucket, Key=key)
            with dest.open("wb") as fh:
                shutil.copyfileobj(resp["Body"], fh)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "SWMM Batch output download failed s3://%s/%s: %s",
                runs_bucket,
                key,
                exc,
            )
            return None
        return str(dest)

    local_out = next((p for p in (_download(k) for k in out_keys) if p), None)
    if local_out is None:
        _cleanup_deck_dir(tmp_dir)
        raise SWMMWorkflowError(
            "SWMM_BATCH_OUTPUT_MISSING",
            message=(
                f"SWMM Batch run {run_id} completed but produced no downloadable "
                f".out under s3://{runs_bucket}/{run_id}/ "
                f"(looked for {out_keys!r})"
            ),
            details={"run_id": run_id, "runs_bucket": runs_bucket},
        )

    local_rpt = next((p for p in (_download(k) for k in rpt_keys) if p), None)
    continuity = 0.0
    if local_rpt is not None:
        try:
            cont = read_flow_routing_continuity(local_rpt)
            if cont is not None:
                continuity = float(cont)
        except Exception as exc:  # noqa: BLE001 — provenance only; never fatal
            logger.warning(
                "SWMM Batch .rpt continuity read failed (%s): %s", local_rpt, exc
            )

    return _BatchSWMMRun(out_path=local_out, continuity_error_pct=continuity), tmp_dir
