"""GeoClaw (Clawpack) shallow-water inundation composer (sprint-17).

The GeoClaw analogue of ``model_urban_flood_swmm`` (SWMM) /
``model_flood_scenario`` (SFINCS) / ``model_groundwater_contamination_scenario``
(MODFLOW). A deterministic orchestrator-style workflow (Invariant 2 - no LLM in
the chain) that composes the GeoClaw shallow-water engine end-to-end:

    fetch topo/bathy DEM (fetch_topobathy seamless land+bathy -> fetch_dem fallback)
      -> stage build_spec manifest + DEM reference to S3 (run_geoclaw)
      -> run_solver('geoclaw', ...) -> wait_for_completion (AWS Batch, the SAME
         generic dispatch seam SFINCS/SWMM-off-box use)
      -> download the GeoClaw fort.q frames from the Batch output
      -> postprocess_geoclaw (rasterize fort.q AMR frames -> peak primary COG +
         per-frame COGs)
      -> publish the peak primary + emit the frames out-of-band (the Phase-1
         scrubber animation group).

GeoClaw is BATCH-ONLY (the Clawpack Fortran lives in the worker image, never in
the agent venv), so there is no in-process lane - unlike SWMM this composer
ALWAYS dispatches to AWS Batch. It mirrors the SWMM off-box Batch lane verbatim
(two-card sim observability, live solve-progress heartbeat, telemetry, Batch
output download).

Returns the PEAK ``GeoClawDepthLayerURI`` directly (a ``LayerURI`` subtype) so
the ``emit_tool_call`` ``add_loaded_layer`` gate fires on it. Per-frame depth
COGs are emitted OUT-OF-BAND through ``emitter.add_loaded_layer`` so the web
``detectSequentialGroups`` LayerPanel scrubber group forms.

Determinism boundary (Invariant 1): every depth number the agent narrates comes
from the typed ``GeoClawDepthLayerURI.max_depth_m`` / ``.flooded_area_km2`` /
``.max_inundation_m`` fields the postprocess computed with plain arithmetic -
never free-generated.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Any

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.geoclaw_contracts import (
    GEOCLAW_DEPTH_STYLE_PRESET,
    GeoClawDepthLayerURI,
    GeoClawRunArgs,
)

from ..layer_uri_emit import emit_layer_uri
from ..pipeline_emitter import (
    begin_substeps,
    current_emitter,
    mint_dispatch_and_sim_cards,
    route_sim_terminal,
    substep,
)
from ..tools.publish_layer import PublishLayerError, publish_layer
from .postprocess_geoclaw import (
    GEOCLAW_TARGET_GROUND_RES_M,
    compute_geoclaw_grid_shape,
    postprocess_geoclaw,
)
from .run_geoclaw import (
    GEOCLAW_OFFSHORE_SCENARIOS,
    GEOCLAW_SOLVER_NAME,
    GeoClawWorkflowError,
    finalize_geoclaw_domain,
    plan_geoclaw_domain,
    plan_geoclaw_grid,
    reproject_dem_to_4326,
    resolve_offshore_source,
    stage_geoclaw_manifest,
)
from .solve_progress import drive_live_solve_progress

logger = logging.getLogger(
    "trid3nt_server.workflows.model_dambreak_geoclaw_scenario"
)

__all__ = [
    "model_dambreak_geoclaw_scenario",
    "GeoClawComposerError",
]

#: GeoClaw solve ETA heuristic (s) per base-grid cell - a coarse perf hint for
#: the live progress heartbeat (Invariant 1: a hint, never a narrated number).
_GEOCLAW_SEC_PER_CELL: float = 0.05

#: Output resolution (m) for the FINE nested coastal topo fetched over JUST the AOI
#: (the P2 dense-inundation fix). Fine enough to be well under the ~20 m finest AMR
#: cell so the run-up samples a REAL coast (not a ~450 m ETOPO step), but coarse
#: enough that the nested COG stays light (the worker decimates a too-fine topo
#: anyway). GeoClaw picks finest-in-overlap, so this fine AOI tile wins the coast.
_GEOCLAW_FINE_NEARSHORE_PIXEL_M: float = 10.0

#: Scenario families whose computational domain / AOI reaches the OPEN SEA, so the
#: published depth must be masked to OVERLAND cells (topo >= 0) to render coastal
#: inundation instead of the full water column that includes the ocean. tsunami =
#: the offshore Okada/dtopo source; surge = the coastal storm-surge forcing. An
#: inland ``dam_break`` (domain == AOI, no sea) is DELIBERATELY excluded so the mask
#: can never erase a legitimate inland flood (e.g. a below-MSL basin whose terrain
#: is negative relative to the vertical datum).
_GEOCLAW_OCEAN_MASK_SCENARIOS: frozenset[str] = GEOCLAW_OFFSHORE_SCENARIOS | frozenset(
    {"surge"}
)


class GeoClawComposerError(RuntimeError):
    """Raised on a fatal composer failure (carries an open-set ``error_code``)."""

    error_code: str = "GEOCLAW_COMPOSER_FAILED"

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


# --------------------------------------------------------------------------- #
# DEM acquisition (topobathy seamless -> fetch_dem fallback).
# --------------------------------------------------------------------------- #
def _fetch_topo_for_geoclaw(
    bbox: tuple[float, float, float, float],
    *,
    force_bathy_base: bool = False,
) -> str:
    """Fetch a topo/bathy DEM for the AOI and return its ``s3://`` URI.

    GeoClaw needs a SEAMLESS land+bathymetry DEM (the shallow-water bed): try
    ``fetch_topobathy`` first (the seamless coastal DEM, the right substrate for
    tsunami / surge run-up), fall back to ``fetch_dem`` (3DEP land-only) for an
    inland dam-break where bathymetry is irrelevant (the data-source fallback
    norm: primary -> fallback -> honest typed error).

    ``force_bathy_base`` (tsunami / offshore): pass through to ``fetch_topobathy``
    so the GLOBAL ETOPO 2022 topo-bathy is laid down as the ALWAYS-ON base over
    the FULL (offshore-extended) domain -- guaranteeing the open-ocean portion is
    genuinely-negative bathymetry rather than a flat land-DEM fill (the GeoClaw
    flat-ocean root cause).

    Returns the DEM cache/runs ``s3://`` URI (staged BY REFERENCE - the worker
    downloads it directly). Raises ``GeoClawComposerError`` only when BOTH fail.
    """
    from ..tools.fetchers.terrain.fetch_dem import fetch_dem
    from ..tools.fetchers.ocean.fetch_topobathy import fetch_topobathy

    try:
        layer = fetch_topobathy(bbox, force_bathy_base=force_bathy_base)
        uri = getattr(layer, "uri", None) or (
            layer.get("uri") if isinstance(layer, dict) else None
        )
        if uri:
            return str(uri)
    except Exception as exc:  # noqa: BLE001 - fall through to fetch_dem
        logger.info(
            "fetch_topobathy failed (%s); falling back to fetch_dem(10m)", exc
        )

    try:
        layer = fetch_dem(bbox, resolution_m=10)
        uri = getattr(layer, "uri", None) or (
            layer.get("uri") if isinstance(layer, dict) else None
        )
        if not uri:
            raise GeoClawComposerError(
                "GEOCLAW_DEM_FETCH_FAILED",
                f"fetch_dem returned no uri for bbox {bbox}",
            )
        return str(uri)
    except GeoClawComposerError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise GeoClawComposerError(
            "GEOCLAW_DEM_FETCH_FAILED",
            f"both DEM sources failed for bbox {bbox}: topobathy + fetch_dem-10m: {exc}",
        ) from exc


def _fetch_fine_nearshore_for_geoclaw(
    aoi_bbox: tuple[float, float, float, float],
) -> str | None:
    """Fetch a FINE (~10 m) nearshore topo-bathy COG over JUST the AOI for use as a
    GeoClaw nested SHORE topo; return its ``s3://`` URI, or ``None`` when no
    genuinely-fine source covers the AOI.

    The P2 dense-inundation fix. The PRIMARY topo (the coarse ETOPO base over the
    full offshore-extended domain) under-resolves the nearshore (~450 m), so a
    tsunami inundates only a handful of cells. This pulls the AOI-appropriate NCEI
    REGIONAL integrated topo-bathy DEM (~1 m; e.g. the CoNED Northern California
    collection that covers Crescent City, which CUDEM omits) OR CUDEM where it
    exists, capped to a light ~10 m COG, to stage as a fine NESTED topo over the
    AOI. GeoClaw layers it finest-last and picks finest-in-overlap, so the coast is
    sampled at ~10 m and the run-up resolves into a DENSE inundation sheet.

    Returns ``None`` (skip the nested layer) when neither a regional fine DEM nor
    CUDEM covers the AOI -- nesting another coarse ETOPO-over-ETOPO layer would add
    nothing. Best-effort: any fetch failure returns ``None`` (the run proceeds on
    the primary topo, exactly as before this fix).
    """
    from ..tools.fetchers.ocean.fetch_topobathy import fetch_topobathy

    try:
        layer = fetch_topobathy(
            aoi_bbox,
            include_regional_fine=True,
            min_pixel_m=_GEOCLAW_FINE_NEARSHORE_PIXEL_M,
        )
    except Exception as exc:  # noqa: BLE001 - the nested fine layer is best-effort
        logger.info(
            "fine nearshore topo fetch failed for AOI %s (%s); skipping the nested "
            "fine-topo layer (run proceeds on the coarse primary topo)",
            aoi_bbox, exc,
        )
        return None
    cudem_n = int(getattr(layer, "cudem_tile_count", 0) or 0)
    regional_n = int(getattr(layer, "regional_tile_count", 0) or 0)
    uri = getattr(layer, "uri", None) or (
        layer.get("uri") if isinstance(layer, dict) else None
    )
    if (cudem_n or regional_n) and uri:
        logger.info(
            "fine nearshore nested topo for AOI %s: %s (cudem_tiles=%d "
            "regional_tiles=%d, ~%g m)",
            aoi_bbox, uri, cudem_n, regional_n, _GEOCLAW_FINE_NEARSHORE_PIXEL_M,
        )
        return str(uri)
    logger.info(
        "no genuinely-fine nearshore source (regional/CUDEM) covers AOI %s "
        "(cudem_tiles=%d regional_tiles=%d); skipping the nested fine-topo layer",
        aoi_bbox, cudem_n, regional_n,
    )
    return None


def _rasterize_topo_to_depth_grid(
    dem_uri: str,
    bbox: tuple[float, float, float, float],
    grid_shape: tuple[int, int],
) -> Any:
    """Warp the STAGED EPSG:4326 topo/bathy DEM onto the SAME (H, W) grid + AOI
    ``bbox`` as the depth raster so postprocess can split land (topo >= 0) from
    ocean (topo < 0) cell-for-cell.

    ``dem_uri`` is the primary topo/bathy DEM GeoClaw actually ran on (the
    ``resolve_offshore_source`` / reproject_dem_to_4326 output — the seamless
    ETOPO-bathy base over the full offshore-extended domain, which covers the AOI).
    We read it with rasterio and reproject/resample it onto the depth grid's
    ``from_bounds`` transform (north-up, row 0 = north — the SAME orientation
    ``rasterize_frame_to_grid`` builds), bilinear so the coastline (the topo=0
    contour) is smooth. Runs off the asyncio loop (blocking S3 read + rasterio) —
    the caller wraps it in ``asyncio.to_thread``.

    Returns the ``(H, W)`` float elevation grid, or ``None`` on ANY failure
    (unreachable DEM, rasterio error) so the run degrades to publishing the
    unmasked total-depth exactly as before this fix (never a hard failure — the
    data-source fallback norm).
    """
    import os

    import numpy as np
    import rasterio
    from rasterio.transform import from_bounds
    from rasterio.warp import Resampling
    from rasterio.warp import reproject as _warp_reproject

    from .run_geoclaw import _dem_uri_to_local

    src_local: str | None = None
    is_temp = False
    try:
        src_local, is_temp = _dem_uri_to_local(dem_uri)
        nrows, ncols = int(grid_shape[0]), int(grid_shape[1])
        min_lon, min_lat, max_lon, max_lat = bbox
        dst_transform = from_bounds(min_lon, min_lat, max_lon, max_lat, ncols, nrows)
        dst = np.full((nrows, ncols), np.nan, dtype="float64")
        with rasterio.open(src_local) as src:
            _warp_reproject(
                source=rasterio.band(src, 1),
                destination=dst,
                src_transform=src.transform,
                src_crs=src.crs or "EPSG:4326",
                dst_transform=dst_transform,
                dst_crs="EPSG:4326",
                resampling=Resampling.bilinear,
            )
        return dst
    except Exception as exc:  # noqa: BLE001 — best-effort; degrade to unmasked depth
        logger.warning(
            "model_dambreak_geoclaw_scenario: could not rasterize staged topo %s "
            "onto the depth grid for the overland mask (%s); publishing UNMASKED "
            "total-depth",
            dem_uri,
            exc,
        )
        return None
    finally:
        if is_temp and src_local:
            try:
                os.unlink(src_local)
            except OSError:
                pass


def _record_geoclaw_batch_solve_telemetry(
    *,
    run_result: Any,
    handle: Any,
    staging: Any,
    compute_class: str,
    session_id: str | None = None,
    case_id: str | None = None,
) -> dict | None:
    """Record ONE SOLVE row for the GeoClaw Batch lane (mirrors the SWMM/SFINCS
    telemetry sibling). Best-effort; returns the recorded row or ``None``."""
    from ..telemetry import record_solve_telemetry

    meta = getattr(run_result, "batch_compute_meta", None) or {}
    if not isinstance(meta, dict):
        meta = {}

    row: dict = {
        "run_id": getattr(run_result, "run_id", None) or staging.run_id,
        "solver": GEOCLAW_SOLVER_NAME,
        "status": getattr(run_result, "status", None),
        "backend": str(getattr(handle, "workflow_name", "") or "unknown"),
        "compute_class": compute_class,
        "case_id": case_id,
        "session_id": session_id,
        "active_cell_count": int(getattr(staging, "n_active_cells", 0) or 0),
        "scenario": staging.run_args.scenario,
    }
    row.update(meta)
    return record_solve_telemetry(row)


# --------------------------------------------------------------------------- #
# The composer.
# --------------------------------------------------------------------------- #
async def model_dambreak_geoclaw_scenario(
    run_args: GeoClawRunArgs,
    *,
    dem_uri: str | None = None,
    run_id: str | None = None,
    compute_class: str = "standard",
    cleanup_outputs: bool = True,
) -> GeoClawDepthLayerURI:
    """Compose the full GeoClaw shallow-water inundation chain end-to-end (Batch).

    Args:
        run_args: the validated ``GeoClawRunArgs`` (bbox + scenario + forcing).
        dem_uri: optional topo/bathy DEM ``s3://`` URI. When ``None`` the composer
            fetches it (``fetch_topobathy`` -> ``fetch_dem`` fallback). Tests pass
            a synthetic URI to skip the fetch.
        run_id: optional ULID; minted by the staging step if absent.
        compute_class: FR-CE-3 compute class for the Batch sizing.
        cleanup_outputs: when True, the downloaded fort.q output dir is removed
            after postprocess (the COGs were already uploaded).

    Returns:
        The PEAK ``GeoClawDepthLayerURI`` (role ``"primary"``, name ``"Peak flood
        depth"``) carrying the three narration scalars + the echoed scenario.
        Per-frame depth layers are emitted out-of-band via the emitter.

    Raises:
        GeoClawComposerError / GeoClawWorkflowError / PostprocessGeoClawError on a
        fatal stage failure (the tool wrapper catches these and returns a typed
        error dict so the agent narrates honestly).
    """
    bbox = tuple(run_args.bbox)
    emitter = current_emitter()

    # --- Zoom-on-area-first: the map zooms before the solve runs. ---
    if emitter is not None:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(bbox)})
        except Exception as exc:  # noqa: BLE001 - non-fatal UX hint
            logger.warning(
                "model_dambreak_geoclaw_scenario: zoom-to emit failed: %s", exc
            )

    # --- Sub-step plan (task-168): fetch DEM -> stage -> solve -> postprocess
    #     -> publish peak. begin_substeps stamps the parent breadcrumb cap; it is
    #     a no-op outside emit_tool_call (current_emitter() is None).
    begin_substeps(emitter, 5)

    # --- Offshore-domain planning (tsunami) --------------------------------
    # An Okada (seafloor) source can only generate a run-up if the computational
    # domain EXTENDS offshore to span the deep-water source -> the AOI coast. For
    # a tsunami we size that extended domain HERE and fetch the bathymetry over
    # IT (not just the AOI); dam_break / surge keep domain == AOI.
    domain_bbox = plan_geoclaw_domain(bbox, run_args.scenario, run_args.source_lonlat)
    fetch_bbox = domain_bbox  # fetch topo/bathy over the FULL computational domain

    # --- Step 1: topo/bathy DEM (off-loop blocking I/O) ---------------------
    # For an OFFSHORE source (tsunami) force the ETOPO global topo-bathy as the
    # always-on base over the FULL offshore-extended domain so the open ocean is
    # genuinely-negative bathymetry (the flat-ocean root-cause fix), not a flat
    # land-DEM fill.
    _force_bathy_base = run_args.scenario in GEOCLAW_OFFSHORE_SCENARIOS
    if dem_uri is None:
        async with substep(emitter, "fetch_topobathy"):
            resolved_dem_uri = await asyncio.to_thread(
                _fetch_topo_for_geoclaw,
                fetch_bbox,
                force_bathy_base=_force_bathy_base,
            )
    else:
        resolved_dem_uri = dem_uri

    # --- CRS alignment: reproject the topo/bathy DEM to EPSG:4326 (lon/lat) ----
    # GeoClaw's tsunami solve runs in spherical lat/lon (coordinate_system=2) with
    # a lon/lat computational domain, but fetch_topobathy emits a PROJECTED-METRES
    # (UTM) COG -- a metres extent has ZERO overlap with the lon/lat domain, so
    # GeoClaw aborts ("topo arrays do not cover domain"). Reproject to 4326 BEFORE
    # source-placement (resolve_offshore_source samples the DEM as lon/lat) and
    # staging. Best-effort + idempotent (a 4326 DEM is returned unchanged).
    resolved_dem_uri = await asyncio.to_thread(
        reproject_dem_to_4326, resolved_dem_uri, run_id=run_id
    )

    logger.info(
        "model_dambreak_geoclaw_scenario: DEM=%s domain=%s aoi=%s",
        resolved_dem_uri,
        domain_bbox,
        bbox,
    )

    # --- Bathymetry-aware Okada source placement (tsunami synthetic source) --
    # Honor a user/composer offshore source when it is over deep water, else
    # project onto the deepest seaward cell of the fetched bathymetry. Skipped
    # for a STAGED dtopo (the source is prescribed by that file) and for the
    # non-offshore scenarios.
    source_override: tuple[float, float] | None = None
    if (
        run_args.scenario in GEOCLAW_OFFSHORE_SCENARIOS
        and run_args.tsunami_dtopo_uri is None
    ):
        source_override = await asyncio.to_thread(
            resolve_offshore_source,
            resolved_dem_uri,
            domain_bbox,
            bbox,
            run_args.source_lonlat,
        )
        if source_override is None:
            logger.warning(
                "model_dambreak_geoclaw_scenario: no below-waterline cell found in "
                "domain %s; keeping requested source %s (run may not inundate)",
                domain_bbox,
                run_args.source_lonlat,
            )
        else:
            logger.info(
                "model_dambreak_geoclaw_scenario: Okada source placed offshore at "
                "%s (requested=%s)",
                source_override,
                run_args.source_lonlat,
            )
            # --- Domain/source coordination (issue #9) ---------------------
            # The initial domain was sized from the AOI alone (plan_geoclaw_domain
            # above) but the bathymetry reaches FURTHER offshore, so the resolved
            # deep-water source can land OUTSIDE that domain -> the Okada
            # deformation falls outside the integrated box -> zero inundation.
            # Re-size the domain to ENCLOSE the resolved source (clamped to the
            # fetched-DEM coverage), asserting source-in-domain (loud failure on a
            # future drift). Skipped when no source was resolved (keep the AOI
            # domain + the honest no-inundation warning above).
            domain_bbox = await asyncio.to_thread(
                finalize_geoclaw_domain,
                bbox,
                run_args.scenario,
                source_override,
                resolved_dem_uri,
            )
            logger.info(
                "model_dambreak_geoclaw_scenario: domain re-sized to enclose "
                "source -> domain=%s source=%s aoi=%s",
                domain_bbox,
                source_override,
                bbox,
            )

    # Cost-bounded grid + AMR plan (the SOLVER_TIMEOUT fix): a COARSE base grid
    # over the full (offshore-extended) propagation domain + NESTED AMR refined
    # ONLY at the AOI to a tens-of-metres run-up resolution, with the finest mesh
    # bounded by a cell budget so a WET coastal solve finishes in minutes. The
    # planned amr_levels OVERRIDES run_args.amr_levels (a level-4 request over a
    # huge AOI is what TIMED OUT); est_finest_cells is the compute-class work proxy.
    (
        base_num_cells,
        planned_amr_levels,
        est_finest_cells,
        propagation_level,
        est_prop_domain_cells,
    ) = plan_geoclaw_grid(bbox, domain_bbox, run_args.amr_levels)
    logger.info(
        "model_dambreak_geoclaw_scenario: grid plan base=%s amr_levels=%s "
        "(requested=%s) est_finest_aoi_cells=%d propagation_level=%s "
        "est_propagation_domain_cells=%d domain=%s aoi=%s",
        base_num_cells,
        planned_amr_levels,
        run_args.amr_levels,
        est_finest_cells,
        propagation_level,
        est_prop_domain_cells,
        domain_bbox,
        bbox,
    )

    # Optional staged tsunami dtopo / surge forcing (already-staged URIs on args).
    dtopo_uri = run_args.tsunami_dtopo_uri
    surge_uri = run_args.surge_forcing_uri
    # Optional additional topo/bathy tiles (ordered coarse -> fine on the args).
    extra_dem_uris = list(run_args.extra_topo_uris or [])

    # --- P2 dense-inundation: a FINE (~10 m) nested SHORE topo over the AOI ------
    # The primary topo is the coarse ETOPO base over the full offshore-extended
    # domain (~450 m nearshore) -- so a tsunami inundates only a handful of cells.
    # Fetch the AOI-appropriate NCEI fine topo-bathy (regional ~1 m where CUDEM
    # omits the coast, e.g. CoNED Northern California over Crescent City; CUDEM
    # elsewhere) over JUST the AOI and append it as a fine NESTED topo (coarse ->
    # fine). GeoClaw picks finest-in-overlap, so the coast samples at ~10 m and the
    # finer AMR run-up mesh resolves a DENSE inundation sheet. Only for an OFFSHORE
    # (tsunami) AUTO-fetch run; skipped when no genuinely-fine source covers the AOI
    # (returns None -> run proceeds on the coarse primary, as before).
    if dem_uri is None and run_args.scenario in GEOCLAW_OFFSHORE_SCENARIOS:
        fine_uri = await asyncio.to_thread(_fetch_fine_nearshore_for_geoclaw, bbox)
        if fine_uri:
            # GeoClaw runs in lon/lat (coordinate_system=2): reproject the fine COG
            # to EPSG:4326 too (same as the primary) so it overlaps the domain.
            fine_uri = await asyncio.to_thread(
                reproject_dem_to_4326, fine_uri, run_id=run_id
            )
            extra_dem_uris.append(fine_uri)
            logger.info(
                "model_dambreak_geoclaw_scenario: staged fine nested SHORE topo "
                "for AOI %s -> %s", bbox, fine_uri,
            )

    # --- Step 2: stage the build_spec manifest + DEM reference --------------
    # The USER-GATED fault_* + coastal_gauge_lonlat + fgmax_arrival_tol_m live on
    # run_args and ride into the build_spec inside stage_geoclaw_manifest ->
    # build_geoclaw_build_spec (only the supplied fault_* are threaded). The
    # offshore-extended domain + resolved source ride in via the new kwargs.
    async with substep(emitter, "stage_geoclaw_manifest"):
        staging = await asyncio.to_thread(
            stage_geoclaw_manifest,
            run_args,
            dem_uri=resolved_dem_uri,
            run_id=run_id,
            dtopo_uri=dtopo_uri,
            surge_uri=surge_uri,
            extra_dem_uris=extra_dem_uris,
            base_num_cells=base_num_cells,
            domain_bbox=domain_bbox,
            source_lonlat_override=source_override,
            amr_levels_override=planned_amr_levels,
        )

    # Size the Batch instance from the FINEST-level AOI cell count (the real work
    # proxy: the finest mesh is pinned over the AOI for the whole run), not the
    # coarse base-grid count, so a wet solve is not under-provisioned.
    try:
        staging.n_active_cells = max(
            int(getattr(staging, "n_active_cells", 0) or 0), int(est_finest_cells)
        )
    except Exception:  # noqa: BLE001 - never break the chain on a proxy update
        pass

    # --- Auto vertical scaling from the base grid cell count ----------------
    from ..tools.simulation.solver import (
        select_compute_class,
        solve_progress_vcpus,
    )

    n_active = int(getattr(staging, "n_active_cells", 0) or 0)
    auto_class = select_compute_class(n_active) if n_active > 0 else compute_class
    # Honor an explicit HIGHER compute_class from the caller: take the larger of
    # the auto-sized tier and the requested tier so vertical auto-scaling still
    # scales UP for big domains, but a user asking for "large"/"xlarge" (quicker
    # turnaround) is never silently downgraded to the small/standard auto pick.
    _CLASS_RANK = {"small": 0, "standard": 1, "large": 2, "xlarge": 3}
    effective_compute_class = max(
        auto_class, compute_class, key=lambda c: _CLASS_RANK.get(c, 1)
    )
    # Deployment-aware CPU count (fingerprint audit A6): local-docker reports
    # the HOST cpu count; aws-batch keeps the tier lookup byte-identical.
    _vcpus = solve_progress_vcpus(effective_compute_class)

    # --- Step 3: dispatch to AWS Batch (the generic run_solver seam) --------
    from ..tools.simulation.solver import (
        EmitterBinding,
        run_solver,
        set_emitter_binding,
        wait_for_completion,
    )

    handle = run_solver(
        solver=GEOCLAW_SOLVER_NAME,
        model_setup_uri=staging.manifest_uri,
        compute_class=effective_compute_class,
    )

    # --- Two-card sim observability (dispatch card + Batch-bound sim card) --
    _sim_step_id = await mint_dispatch_and_sim_cards(
        emitter=emitter,
        solver=GEOCLAW_SOLVER_NAME,
        handle=handle,
        compute_class=effective_compute_class,
    )
    if emitter is not None and _sim_step_id is not None:
        set_emitter_binding(EmitterBinding(emitter=emitter, step_id=_sim_step_id))

    _progress_task = asyncio.ensure_future(
        drive_live_solve_progress(
            emitter=current_emitter(),
            run_id=staging.run_id,
            solver=GEOCLAW_SOLVER_NAME,
            grid_resolution_m=None,
            active_cell_count=n_active or None,
            vcpus=int(_vcpus) if _vcpus is not None else None,
            eta_seconds=(n_active * _GEOCLAW_SEC_PER_CELL) if n_active else None,
        )
    )
    run_result = None
    # task-168: surface the solve as a child "run_solver" row in the parent
    # timeline. The Sim card (mint_dispatch_and_sim_cards) STILL owns the live
    # Batch readout (hard invariant); this child is the timeline entry that goes
    # green/red/yellow with the solve. No-op outside emit_tool_call. The original
    # cancel/cleanup flow + telemetry + typed-error raise are PRESERVED verbatim:
    # a returned non-"complete" RunResult raises a SENTINEL inside the substep so
    # the child row reads red (honesty floor), which we swallow right after the
    # context exits so control falls through to the UNCHANGED telemetry + typed-
    # error block below (the parent's own state is owned there, not by the child).
    class _SolveReturnedFailed(RuntimeError):
        pass

    try:
        async with substep(emitter, "run_solver"):
            try:
                run_result = await wait_for_completion(handle)
            except asyncio.CancelledError:
                # Invariant 8: propagate the cancel; route it to the SIM card.
                logger.info(
                    "model_dambreak_geoclaw_scenario cancelled while awaiting solver"
                )
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
        # Child already marked red by the substep; fall through to the original
        # telemetry + typed-error path (which records + raises GeoClawWorkflowError).
        pass

    await route_sim_terminal(emitter, _sim_step_id, run_result=run_result)

    # --- SOLVE telemetry (Batch instance + size + timing) ------------------
    try:
        _record_geoclaw_batch_solve_telemetry(
            run_result=run_result,
            handle=handle,
            staging=staging,
            compute_class=effective_compute_class,
        )
    except Exception as exc:  # noqa: BLE001 - never break the solve
        logger.warning(
            "GeoClaw solve batch-compute telemetry failed (non-fatal): %s", exc
        )

    if run_result.status != "complete":
        raise GeoClawWorkflowError(
            "GEOCLAW_RUN_FAILED",
            message=(
                "GeoClaw Batch solve did not complete "
                f"(status={run_result.status}, "
                f"error_code={getattr(run_result, 'error_code', None)}): "
                f"{getattr(run_result, 'error_message', '') or getattr(run_result, 'cancellation_reason', '') or ''}"
            ),
            details={
                "run_id": staging.run_id,
                "output_uri": getattr(run_result, "output_uri", None),
            },
        )

    # Register-only fast path: if the worker wrote a publish_manifest alongside
    # completion.json, skip the fort.q download + agent-side postprocess. The
    # worker already produced COGs + band_stats + TiTiler URLs; we just register
    # them and return early. Falls through when the manifest is absent (pre-
    # manifest workers or unknown schema version).
    from .register_published_manifest import (
        read_publish_manifest,
        register_manifest_layers,
    )
    batch_run_id = getattr(run_result, "run_id", None) or staging.run_id
    _gc_manifest = await asyncio.to_thread(read_publish_manifest, run_result)
    if _gc_manifest is not None:
        async with substep(emitter, "postprocess_geoclaw"):
            _gc_reg = register_manifest_layers(
                _gc_manifest, run_id=batch_run_id, bbox=tuple(bbox)
            )
        if not _gc_reg.layers:
            raise GeoClawComposerError(
                "GEOCLAW_NO_LAYERS",
                "worker publish_manifest produced no registered depth layers "
                "(honesty floor: cannot narrate an empty solve)",
            )
        _gc_m = _gc_reg.metrics
        _gc_prim = _gc_reg.layers[0]
        _gc_frame_layers = _gc_reg.layers[1:]
        peak = GeoClawDepthLayerURI(
            uri=_gc_prim.uri,
            layer_type=_gc_prim.layer_type,
            layer_id=_gc_prim.layer_id,
            name=_gc_prim.name,
            style_preset=_gc_prim.style_preset,
            bbox=tuple(bbox),
            role=_gc_prim.role,
            max_depth_m=float(_gc_m.get("max_depth_m", 0.0)),
            flooded_area_km2=float(_gc_m.get("flooded_area_km2", 0.0)),
            max_inundation_m=float(_gc_m.get("max_inundation_m", 0.0)),
            arrival_time_s=(
                float(_gc_m["arrival_time_s"])
                if _gc_m.get("arrival_time_s") is not None
                else None
            ),
            scenario=run_args.scenario,
        )
        emitted_frames = await _emit_frame_layers(
            emitter,
            _gc_frame_layers,  # type: ignore[arg-type]
            batch_run_id,
        )
        logger.info(
            "model_dambreak_geoclaw_scenario (manifest path) run_id=%s "
            "scenario=%s max_depth_m=%.4g flooded_area_km2=%.6g "
            "max_inundation_m=%.4g arrival_time_s=%s "
            "frames_emitted=%d/%d peak_uri=%s",
            batch_run_id,
            run_args.scenario,
            peak.max_depth_m,
            peak.flooded_area_km2,
            peak.max_inundation_m,
            peak.arrival_time_s,
            emitted_frames,
            len(_gc_frame_layers),
            peak.uri,
        )
        if emitter is not None:
            try:
                await emitter.emit_map_command("zoom-to", {"bbox": list(bbox)})
            except Exception as _ze:  # noqa: BLE001
                logger.warning(
                    "model_dambreak_geoclaw_scenario: zoom-to (manifest path) "
                    "failed: %s",
                    _ze,
                )
        return peak

    # --- Step 4: download the Batch fort.q outputs -------------------------
    batch_run_id = getattr(run_result, "run_id", None) or staging.run_id
    out_dir = await asyncio.to_thread(_download_batch_geoclaw_outputs, batch_run_id)

    # Adaptive output raster: size the depth COG from the AOI at the native
    # run-up ground resolution (~25 m, matching the finest CoNED/AMR nest) so the
    # inundation renders as a smooth, dense sheet (SFINCS parity) instead of the
    # legacy fixed 256x256 specks. Floored at 256, capped for huge AOIs.
    geoclaw_grid_shape = compute_geoclaw_grid_shape(bbox)
    logger.info(
        "model_dambreak_geoclaw_scenario run_id=%s adaptive depth-raster grid "
        "H=%d W=%d (~%.0f m/px) over bbox=%s",
        staging.run_id,
        geoclaw_grid_shape[0],
        geoclaw_grid_shape[1],
        GEOCLAW_TARGET_GROUND_RES_M,
        tuple(bbox),
    )

    # --- Overland mask topo (offshore/coastal only) ------------------------
    # For tsunami / surge the AOI reaches the open sea, so the raw GeoClaw depth is
    # the FULL water column and an offshore AOI renders as ocean, not the coastal
    # flood. Rasterize the STAGED topo/bathy DEM (the one GeoClaw ran on) onto the
    # SAME adaptive depth grid so postprocess masks depth to OVERLAND cells (topo
    # >= 0). Inland dam_break is excluded (mask_ocean stays False) — its depth is
    # published in full, unchanged. A None topo_grid (fetch failed) degrades to the
    # unmasked total-depth (same as before this fix).
    mask_ocean = run_args.scenario in _GEOCLAW_OCEAN_MASK_SCENARIOS
    topo_grid = None
    if mask_ocean:
        topo_grid = await asyncio.to_thread(
            _rasterize_topo_to_depth_grid,
            resolved_dem_uri,
            bbox,
            geoclaw_grid_shape,
        )
        logger.info(
            "model_dambreak_geoclaw_scenario run_id=%s overland-mask topo %s for "
            "scenario=%s (grid H=%d W=%d)",
            staging.run_id,
            "rasterized" if topo_grid is not None else "UNAVAILABLE",
            run_args.scenario,
            geoclaw_grid_shape[0],
            geoclaw_grid_shape[1],
        )

    try:
        # --- Step 5: postprocess (rasterize fort.q -> peak + frames) -------
        async with substep(emitter, "postprocess_geoclaw"):
            layers, metrics = await asyncio.to_thread(
                postprocess_geoclaw,
                out_dir,
                bbox,
                run_id=staging.run_id,
                scenario=run_args.scenario,
                grid_shape=geoclaw_grid_shape,
                topo_grid=topo_grid,
                mask_ocean=mask_ocean,
                fgmax_arrival_tol_m=run_args.fgmax_arrival_tol_m,
            )
    finally:
        if cleanup_outputs:
            _cleanup_dir(out_dir)

    if not layers:
        raise GeoClawComposerError(
            "GEOCLAW_NO_LAYERS",
            "postprocess_geoclaw produced no depth layers (empty solve?)",
        )

    raw_peak = layers[0]
    frame_layers = layers[1:]

    # --- Step 6: publish the PEAK COG through publish_layer (render chokepoint)
    async with substep(emitter, "publish_layer"):
        peak = await asyncio.to_thread(_publish_peak_layer, raw_peak, staging.run_id)

    # --- Step 6b: publish + emit the per-frame animation layers OUT-OF-BAND --
    emitted_frames = await _emit_frame_layers(emitter, frame_layers, staging.run_id)

    logger.info(
        "model_dambreak_geoclaw_scenario complete run_id=%s scenario=%s "
        "max_depth_m=%.4g flooded_area_km2=%.6g max_inundation_m=%.4g "
        "arrival_time_s=%s frames_emitted=%d/%d peak_uri=%s",
        staging.run_id,
        run_args.scenario,
        peak.max_depth_m,
        peak.flooded_area_km2,
        peak.max_inundation_m,
        peak.arrival_time_s,
        emitted_frames,
        len(frame_layers),
        peak.uri,
    )

    # --- AUTHORITATIVE LAST zoom-to ----------------------------------------
    if emitter is not None:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(bbox)})
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "model_dambreak_geoclaw_scenario: authoritative zoom-to failed: %s",
                exc,
            )

    return peak


def _publish_peak_layer(
    raw_peak: GeoClawDepthLayerURI, run_id: str
) -> GeoClawDepthLayerURI:
    """Publish the PEAK depth COG through publish_layer (render chokepoint).

    Routes the raw s3:// peak COG through ``publish_layer`` and returns a NEW
    ``GeoClawDepthLayerURI`` carrying the published /tiles or WMS URL plus the
    narration scalars. On publish failure the raw peak is returned UNCHANGED: the
    dispatch-level ``emit_layer_uri`` guardrail then drops the dead raw-s3://
    raster from the map (honest) while the typed metrics still narrate.

    Mirrors the SWMM/SFINCS primary-publish path.
    """
    if raw_peak.layer_type != "raster" or not (
        raw_peak.uri.startswith("gs://") or raw_peak.uri.startswith("s3://")
    ):
        return raw_peak
    layer_id_for_pub = f"geoclaw-depth-peak-{run_id}"
    try:
        published_uri = publish_layer(
            layer_uri=raw_peak.uri,
            layer_id=layer_id_for_pub,
            style_preset=raw_peak.style_preset or GEOCLAW_DEPTH_STYLE_PRESET,
        )
    except PublishLayerError as exc:
        logger.warning(
            "model_dambreak_geoclaw_scenario: publish_layer FAILED for the peak "
            "layer_id=%s error_code=%s (%s) - returning the unpublished peak.",
            layer_id_for_pub,
            exc.error_code,
            exc,
        )
        return raw_peak
    return GeoClawDepthLayerURI(
        layer_id=layer_id_for_pub,
        name=raw_peak.name,
        layer_type=raw_peak.layer_type,
        uri=published_uri,
        style_preset=raw_peak.style_preset or GEOCLAW_DEPTH_STYLE_PRESET,
        role=raw_peak.role,
        units=raw_peak.units,
        bbox=raw_peak.bbox,
        max_depth_m=raw_peak.max_depth_m,
        flooded_area_km2=raw_peak.flooded_area_km2,
        max_inundation_m=raw_peak.max_inundation_m,
        arrival_time_s=raw_peak.arrival_time_s,
        scenario=raw_peak.scenario,
    )


async def _emit_frame_layers(
    emitter: Any, frame_layers: list[GeoClawDepthLayerURI], run_id: str
) -> int:
    """Publish + emit per-frame depth COGs out-of-band so the web scrubber forms.

    Each frame COG is routed through ``publish_layer`` (render chokepoint) so it
    carries a renderable URL before ``add_loaded_layer``. The "Flood depth step N"
    name token is preserved so the web ``detectSequentialGroups`` groups them. A
    frame that fails to publish is HONESTLY DROPPED. Returns the number emitted
    (0 when no emitter is bound). Never raises (mirrors SWMM).
    """
    if not frame_layers or emitter is None:
        if frame_layers:
            logger.info(
                "model_dambreak_geoclaw_scenario: %d animation frames available "
                "but no emitter bound - frames not emitted.",
                len(frame_layers),
            )
        return 0
    emitted = 0
    for lyr in frame_layers:
        if not (lyr.uri.startswith("gs://") or lyr.uri.startswith("s3://")):
            emit_layer: LayerURI = lyr
        else:
            try:
                frame_uri = await asyncio.to_thread(
                    publish_layer,
                    layer_uri=lyr.uri,
                    layer_id=lyr.layer_id,
                    style_preset=lyr.style_preset or GEOCLAW_DEPTH_STYLE_PRESET,
                )
            except PublishLayerError as exc:
                logger.warning(
                    "model_dambreak_geoclaw_scenario: publish_layer FAILED for "
                    "frame layer_id=%s error_code=%s (%s) - dropping this frame.",
                    lyr.layer_id,
                    exc.error_code,
                    exc,
                )
                continue
            emit_layer = GeoClawDepthLayerURI(
                layer_id=lyr.layer_id,
                name=lyr.name,
                layer_type=lyr.layer_type,
                uri=frame_uri,
                style_preset=lyr.style_preset or GEOCLAW_DEPTH_STYLE_PRESET,
                role=lyr.role,
                units=lyr.units,
                bbox=lyr.bbox,
                max_depth_m=lyr.max_depth_m,
                flooded_area_km2=lyr.flooded_area_km2,
                max_inundation_m=lyr.max_inundation_m,
                scenario=lyr.scenario,
            )
        try:
            safe = emit_layer_uri(emit_layer)
            if safe is not None:
                await emitter.add_loaded_layer(safe)
                emitted += 1
        except Exception as exc:  # noqa: BLE001 - never break the solve
            logger.warning(
                "model_dambreak_geoclaw_scenario: frame add_loaded_layer failed "
                "for %s: %s",
                emit_layer.layer_id,
                exc,
            )
    if emitted:
        logger.info(
            "model_dambreak_geoclaw_scenario: emitted %d/%d animation frames as a "
            "sequential group (run_id=%s)",
            emitted,
            len(frame_layers),
            run_id,
        )
    return emitted


def _cleanup_dir(d: str | Path) -> None:
    """Best-effort removal of a downloaded scratch dir."""
    try:
        shutil.rmtree(Path(d), ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass


def _download_batch_geoclaw_outputs(run_id: str) -> str:
    """Download the Batch fort.q outputs to a tmp ``_output/`` dir for postprocess.

    The GeoClaw Batch worker uploads its fort.q frames under
    ``s3://<runs_bucket>/<run_id>/_output/`` and records their URIs in
    completion.json ``output_uris``. We re-read completion.json (small, already on
    S3) to find the fort.* keys, download them via the SAME boto3 client the
    solver dispatch uses (no new client), and return the local dir holding an
    ``_output/`` subtree the postprocess discovers.

    Raises:
        GeoClawWorkflowError("GEOCLAW_BATCH_OUTPUT_MISSING"): the completed run
            produced no downloadable fort.q (a 'complete' solve with no output is
            a real failure - never a silent dead-end).
    """
    from ..tools.simulation.solver import (
        _get_runs_bucket,
        _get_s3_client,
        _split_object_uri,
        _try_get_completion_s3,
    )

    runs_bucket = _get_runs_bucket()
    s3 = _get_s3_client()

    keys: list[str] = []
    manifest = _try_get_completion_s3(runs_bucket, run_id)
    if isinstance(manifest, dict):
        for raw in manifest.get("output_uris") or []:
            uri = str(raw)
            try:
                _scheme, _bucket, key = _split_object_uri(uri)
            except Exception:  # noqa: BLE001
                continue
            base = key.rsplit("/", 1)[-1]
            if base.startswith("fort."):
                keys.append(key)
    if not keys:
        # Defensive fallback: list the runs prefix for fort.* objects.
        try:
            resp = s3.list_objects_v2(
                Bucket=runs_bucket, Prefix=f"{run_id}/_output/"
            )
            for obj in resp.get("Contents", []) or []:
                k = obj.get("Key", "")
                if k.rsplit("/", 1)[-1].startswith("fort."):
                    keys.append(k)
        except Exception as exc:  # noqa: BLE001
            logger.warning("GeoClaw output list fallback failed: %s", exc)

    tmp_dir = tempfile.mkdtemp(prefix=f"geoclaw-batch-out-{run_id}-")
    out_sub = Path(tmp_dir) / "_output"
    out_sub.mkdir(parents=True, exist_ok=True)

    downloaded = 0
    for key in keys:
        base = key.rsplit("/", 1)[-1]
        dest = out_sub / base
        try:
            resp = s3.get_object(Bucket=runs_bucket, Key=key)
            with dest.open("wb") as fh:
                shutil.copyfileobj(resp["Body"], fh)
            downloaded += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "GeoClaw Batch output download failed s3://%s/%s: %s",
                runs_bucket,
                key,
                exc,
            )

    has_frame = any(
        p.name.startswith("fort.q") for p in out_sub.iterdir() if p.is_file()
    )
    if not has_frame:
        _cleanup_dir(tmp_dir)
        raise GeoClawWorkflowError(
            "GEOCLAW_BATCH_OUTPUT_MISSING",
            message=(
                f"GeoClaw Batch run {run_id} completed but produced no downloadable "
                f"fort.q frames under s3://{runs_bucket}/{run_id}/_output/ "
                f"(downloaded {downloaded} fort.* objects)"
            ),
            details={"run_id": run_id, "runs_bucket": runs_bucket},
        )

    return tmp_dir
