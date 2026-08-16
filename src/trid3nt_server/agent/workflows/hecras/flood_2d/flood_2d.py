"""Engine template ``hecras_flood_2d`` -- HEC-RAS 2D flood on a GENUINELY-NEW AOI.

The promotion of the authoring chain: unlike
``hecras_riverine_flood`` (which reparameterizes HEC's FROZEN shipped Muncie
geometry), this template AUTHORS the 2D mesh + terrain-sampled subgrid tables for a
place the user names, then solves it with the production HEC-RAS 6.6 engines. The
backend is the proven ``flood2d_pipeline`` chain:

    fetch_dem (seam-1)  -> reproject to a local ftUS grid + mesh seeds
      -> the AUTHORING worker image (trid3nt-local/hecras2025-authoring:
         ras createterrain + AuthorMesh TryCreateMesh topology +
         MeshPropertyTables.ComputeFrom subgrid tables over the terrain)
      -> the deck composer (Mesh2D + tables -> a complete pure-2D deck, stamped
         with the AOI's CRS so the depth COG geolocates)
      -> run_solver (the generic seam; the composed deck rides as manifest
         ``inputs`` and the hecras worker's no-archetype M3-gate path solves it)
      -> postprocess_hecras -> peak-depth COG + 2D mesh preview + inflow chart.

FIDELITY (loud, NATE no-hand-wave): the SOLVE is the refinement-grade production
6.x solver; the GEOMETRY is authored by the HEC-RAS 2025 AuthorMesh path, validated
end-to-end (the transplant-path: subgrid tables 0.99988 corr / writer dWSE 0.0 /
topology bijection). It is SCREENING-grade until broader per-AOI
V&V. For a FAST screening flood use ``sfincs_flood``; for pluvial/precipitation
forcing on this engine, that is the OI-D residual (not yet wired). ASCII only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from trid3nt_contracts import new_ulid
from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.hecras_contracts import (
    HECRAS_INPUT_INVALID,
    HECRAS_SOLVE_FAILED,
    HecrasDepthLayerURI,
)
from trid3nt_contracts.tool_registry import AtomicToolMetadata, ResolutionSpec

from trid3nt_server.agent.tools import register_tool
from trid3nt_server.agent.tools.resolution_declared import (
    ResolutionOutOfRangeError,
    resolve_resolution,
)
from trid3nt_server.agent.gates.input_review import gate_input_review
from trid3nt_server.agent.workflows.hecras._template_card import TemplateCard

logger = logging.getLogger("trid3nt_server.agent.workflows.hecras.flood_2d.flood_2d")

__all__ = [
    "hecras_flood_2d",
    "model_hecras_flood_2d",
    "model_hecras_flood_2d_rog",
    "acquire_channel_inputs",
    "HecrasFlood2dError",
    "TEMPLATE_CARD",
]

#: The authoring backend lives in the workers tree (proprietary natives image +
#: pure-python composer); imported at CALL time (not import time) so the server
#: package carries no hard dependency on services/workers.
_WORKERS_FRESHTOPO = (
    Path(__file__).resolve().parents[6]
    / "services/workers/hecras2025/subst/crux/freshtopo"
)

#: Default target peak inflow when the user names none (a modest bankfull-ish
#: event; the user pins a real discharge via ``target_peak_cfs``).
_DEFAULT_PEAK_CFS: float = 5000.0

#: Resolution band (m) -- coarser than SFINCS (the 2D subgrid solve is heavier).
_MIN_RES_M: float = 20.0
_MAX_RES_M: float = 200.0
_DEFAULT_RES_M: float = 60.0

#: DECLARED resolution range. The 20-200 m window is a SOLVER constraint
#: (the HEC-RAS 2025 AuthorMesh subgrid-table path): finer overwhelms subgrid-table
#: authoring for a screening solve, coarser drops the channel. Out-of-range asks are
#: QUOTED BACK (typed error), never silently clamped -- the labeled-snap is
#: upgraded to the full ruling here. In-range requests may still autoscale-coarsen for
#: the soft cell cap (a labeled ``derived`` note, not a silent snap).
_RES_SPEC = ResolutionSpec(
    param="resolution_m",
    unit="m",
    min_value=_MIN_RES_M,
    max_value=_MAX_RES_M,
    native_hint="3DEP 10 m (fetch_dem)",
    constraint_source="solver",
    rationale=(
        "HEC-RAS 2025 AuthorMesh 2D subgrid solve accepts a 20-200 m cell; finer "
        "overwhelms subgrid-table authoring for a screening run, coarser loses the "
        "channel"
    ),
)
#: Soft cell-count ceiling the resolution autoscaler respects (keeps a cheap
#: screening solve minutes-scale); the estimate + this cap are the granularity
#: suggestion surfaced for override (the user-controlled-granularity norm).
_SOFT_CELL_CAP: int = 12000

#: User-facing 2D equation-set choices -> the plan-HDF string the engine reads.
#: "diffusion_wave" is the validated default (every acceptance solved with it at
#: low volume error); "full_swe" is the heavier full-momentum shallow-water solver
#: (advanced, less-tested on authored meshes).
_EQUATION_SET_MAP: dict[str, str] = {
    "diffusion_wave": "Diffusion Wave",
    "full_swe": "SWE-ELM",
}
_DEFAULT_EQUATION_SET: str = "diffusion_wave"

#: The RasUnsteady 2D time step (patched into the .bNN Computation Interval). The
#: primary numerical-stability knob for the stability-diagnostic sweep: a coarse
#: step overshoots the peak (spurious water-surface spikes), tightening it converges
#: the peak. Integer + SEC/MIN/HOUR; None keeps the composer 2MIN default.
_COMPUTATION_INTERVAL_RE = re.compile(r"^\d+(SEC|MIN|HOUR)$")

_FIDELITY_NOTE: str = (
    "REFINEMENT-GRADE production HEC-RAS 6.x solver on a 2025-AUTHORED 2D mesh "
    "(headless AuthorMesh topology + terrain-sampled subgrid tables), transplant-"
    "path validated end-to-end. This floods the AOI you named (NOT frozen "
    "demonstration geometry). SCREENING-grade until broader per-AOI V&V; for a "
    "fast screening flood use sfincs_flood. Forcing is a synthetic inflow "
    "hydrograph unless a real peak discharge is pinned."
)


class HecrasFlood2dError(RuntimeError):
    """Fatal fault before a layer is produced (typed error_code to the emitter)."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


TEMPLATE_CARD = TemplateCard(
    question=(
        "peak 2D inundation depth + water surface for a flood at a REAL AOI you "
        "name (refinement-grade HEC-RAS 6.x solver on a headless-AUTHORED 2D mesh "
        "+ terrain-sampled subgrid tables from a fetched DEM). Pin the peak "
        "discharge or scale a default event"
    ),
    required_inputs=["bbox (or a location that resolves to one)"],
    knobs="target_peak_cfs, resolution_m, sim_hours, inlet_edge, outlet_edge, equation_set, computation_interval, input_mode",
)

_METADATA = AtomicToolMetadata(
    name="hecras_flood_2d",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="hecras",
    tier="template",
    resolution_specs=(_RES_SPEC,),
)


def _estimate_cells(bbox: list[float], resolution_m: float) -> int:
    """Rough 2D cell-count estimate for a bbox at a resolution (granularity gate)."""
    min_lon, min_lat, max_lon, max_lat = bbox
    lat_mid = 0.5 * (min_lat + max_lat)
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * max(math.cos(math.radians(lat_mid)), 1e-6)
    w_m = abs(max_lon - min_lon) * m_per_deg_lon
    h_m = abs(max_lat - min_lat) * m_per_deg_lat
    return int((w_m / resolution_m) * (h_m / resolution_m))


def _autoscale_resolution(bbox: list[float], resolution_m: float) -> float:
    """Coarsen the resolution until the cell estimate is under the soft cap.

    The autoscaler SUGGESTION (the user overrides via ``resolution_m``); mirrors
    the #154 granularity gate -- a heavy solve never silently launches an
    intractable mesh."""
    res = float(resolution_m)
    while _estimate_cells(bbox, res) > _SOFT_CELL_CAP and res < _MAX_RES_M:
        res = min(res * 1.25, _MAX_RES_M)
    return res


@register_tool(
    _METADATA,
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def hecras_flood_2d(
    bbox: list[float] | None = None,
    location: str | None = None,
    target_peak_cfs: float | None = None,
    resolution_m: float = _DEFAULT_RES_M,
    sim_hours: float = 24.0,
    inlet_edge: str | None = None,
    outlet_edge: str | None = None,
    equation_set: str = _DEFAULT_EQUATION_SET,
    computation_interval: str | None = None,
    input_mode: str | None = None,
    design_storm_mm_per_hr: float | None = None,
    storm_duration_hr: float = 6.0,
    curve_number: float | None = None,
    amc_condition: str = "normal",
    channel_refinement: float | None = None,
    **_extra_ignored: Any,
) -> HecrasDepthLayerURI | dict[str, Any]:
    """REFINEMENT-GRADE HEC-RAS 2D FLOOD at a REAL AOI you name (headless-authored geometry).

    THE tool for "flood <a place> with HEC-RAS", "run a HEC-RAS 2D flood at this
    AOI", "what does a big flood look like on <this river reach>", "HEC-RAS
    inundation depth for a real location". Unlike ``hecras_riverine_flood`` (frozen
    Muncie demonstration geometry), this AUTHORS the 2D mesh + terrain subgrid
    tables for the AOI from a fetched DEM, then solves it with the production
    HEC-RAS 6.x engines -- so it floods the place the user actually names.

    Fidelity: production 6.x full-physics 2D unsteady hydraulics on a 2025-authored
    mesh (transplant-path validated end-to-end). SCREENING-grade until broader
    per-AOI V&V. For a FAST screening flood use ``sfincs_flood``; for urban drainage
    use ``swmm_urban_flood``; for a dye/contaminant plume ``telemac_river_dye``.

    Params:
        bbox: the AOI as ``[min_lon, min_lat, max_lon, max_lat]`` (EPSG:4326). The
            primary input -- resolve a named place to a bbox (a county/reach/city
            or a drawn AOI), NOT a single-building geocode.
        location: OPTIONAL convenience -- a place name geocoded to a bbox when
            ``bbox`` is not given (best-effort; prefer passing ``bbox``).
        target_peak_cfs: the PEAK inflow discharge (cfs) that forces the run. Pin
            it to a real gauge/NWM peak; default ~5000 cfs when unset. The inflow
            hydrograph is a ramp to this peak (a real hydrograph override is the
            OI-D residual).
        resolution_m: the 2D cell size (m). Supported 20-200 m (mesh/solver); data
            native 3DEP 10 m. Out-of-range asks are quoted the range (typed error),
            never silently snapped. An in-range value may auto-coarsen for
            the soft cell cap (labeled derived note); overridable.
        sim_hours: unsteady window length (hours); default 24.
        inlet_edge / outlet_edge: OPTIONAL compass overrides ("n"/"s"/"e"/"w") for
            where flow enters / drains. Defaults: inflow on the lowest-elevation
            perimeter run, outlet on the south edge (the drainage physics).
        equation_set: the 2D solver -- ``"diffusion_wave"`` (default) or
            ``"full_swe"`` (the full-momentum shallow-water solver, SWE-ELM). Both
            are validated. On a steep dam-flood the two agree on the peak-inundation
            envelope (extent/max depth to sub-inch) and separate ONLY at a small set
            (~0.3% of cells, up to ~1.9 ft) of momentum-dominated cells (channel
            constrictions, rapid transitions). Use ``full_swe`` when that local
            inertial detail matters; diffusion wave otherwise (cheaper, same footprint).
        computation_interval: the RasUnsteady 2D time step (e.g. ``"30SEC"``,
            ``"1MIN"``, ``"5MIN"``; default 2MIN). The numerical-stability knob -- a
            coarse step overshoots the peak (spurious water-surface spikes), a finer
            step converges it. Sweep it (coarse -> fine) to diagnose 2D-model
            stability; tighten it if a run reports an unphysical peak.
        input_mode: ``"user_gated"`` reviews the forcing + resolution + fetched-
            terrain basis before the (heavy) solve; ``"auto"`` (default) proceeds
            with them labeled.
        design_storm_mm_per_hr: set this to run RAIN-ON-GRID instead of an inflow
            hydrograph -- a uniform design storm (mm/hr) falls on every cell and
            drains to a pour-point outlet, solved on the HEC-RAS 2025 MANAGED engine
            (beta). Use for "flood <place> from X mm/hr of rain", pluvial/flash-flood,
            and rainfall-runoff questions. Validated on Coweeta Creek (Godara et al.
            2024 envelope: 25 mm/hr x 6 h). LIMITATION: the 2025 beta has NO
            infiltration layer, so rain-on-grid is RAIN-ONLY (gross rainfall, an
            upper-bound runoff -- state this to the user); curve_number/amc are
            accepted but INERT until infiltration ships.
        storm_duration_hr: rain-on-grid storm length (hours; default 6). The constant
            storm forces the whole window; the outlet hydrograph peaks at equilibrium.
        curve_number / amc_condition: SCS-CN loss knobs -- INERT on the 2025 beta (no
            infiltration layer); reserved for when it ships. Ignored for the inflow path.
        channel_refinement: RAIN-ON-GRID ONLY. None (default) meshes a UNIFORM grid at
            resolution_m. Set a target channel cell size (m, e.g. 22) to author a
            paper-style GRADED mesh instead -- coarse resolution_m background grading down
            to that size along the delineated channel network (Godara et al. refined the
            river with breaklines + nested regions). It sharpens channel ROUTING (earlier
            peak, higher channel velocity, crisper depth) but leaves the peak-Q magnitude,
            wet extent and mass balance essentially unchanged, at ~2x wall time (the fine
            cells force a smaller CFL step). Default OFF: turn it on when channel timing /
            velocity / hydrograph shape is the question, not for a screening peak/extent.
            (Coweeta 22 m: peak 5.7 h -> 4.9 h, max vel 5.7 -> 6.8 m/s, peak Q 195 -> 200
            m3/s, 99.6% mass closure both, 415 s vs 218 s.)

    Returns:
        On success: ``HecrasDepthLayerURI`` -- the peak-depth COG (loaded beside
        the 2D mesh preview + the inflow chart), carrying ``depth_max_ft`` /
        ``depth_mean_ft`` / ``wet_cell_count`` / ``wse_max_ft`` / ``peak_inflow_cfs``
        / ``volume_error_pct`` (narrate these typed numbers only -- invariant 1).
        On failure: dict with ``status="error"`` + ``error_code`` + ``error_message``.
    """
    # --- resolve the AOI bbox -------------------------------------------------- #
    aoi = _coerce_bbox(bbox)
    if aoi is None and location:
        aoi = await asyncio.to_thread(_geocode_bbox, location)
    if aoi is None:
        return {
            "status": "error",
            "error_code": HECRAS_INPUT_INVALID,
            "error_message": "hecras_flood_2d needs a bbox [min_lon,min_lat,max_lon,max_lat] "
            "(or a location that geocodes to one)",
        }

    # --- arg hardening --------------------------------------------------------- #
    try:
        resolution_m = float(resolution_m)
    except (TypeError, ValueError):
        resolution_m = _DEFAULT_RES_M
    # an out-of-declared-range resolution_m is QUOTED BACK as a typed error
    # (the range + native hint), never silently clamped. An in-range value may still
    # autoscale-coarsen within the range (labeled derived note).
    try:
        _resolved = resolve_resolution(
            resolution_m, spec=_RES_SPEC, autoscale=lambda r: _autoscale_resolution(aoi, r)
        )
        resolution_m, _res_basis, _res_note = (
            _resolved.value, _resolved.basis, _resolved.note
        )
    except ResolutionOutOfRangeError as exc:
        return {
            "status": "error",
            "error_code": HECRAS_INPUT_INVALID,
            "error_message": str(exc),
        }

    peak = _DEFAULT_PEAK_CFS
    if target_peak_cfs is not None:
        try:
            p = float(target_peak_cfs)
            if p > 0.0:
                peak = p
        except (TypeError, ValueError):
            pass

    eq_key = str(equation_set or "").strip().lower()
    if eq_key not in _EQUATION_SET_MAP:
        logger.warning("hecras_flood_2d: equation_set %r unknown - using diffusion_wave", equation_set)
        eq_key = _DEFAULT_EQUATION_SET

    interval = None
    if computation_interval is not None:
        cand = str(computation_interval).strip().upper()
        if _COMPUTATION_INTERVAL_RE.match(cand):
            interval = cand
        else:
            logger.warning(
                "hecras_flood_2d: computation_interval %r invalid (need int+SEC/MIN/HOUR) "
                "- using the 2MIN default", computation_interval)

    logger.info(
        "hecras_flood_2d bbox=%s res=%.1fm peak=%.0fcfs sim=%sh inlet=%s outlet=%s eq=%s interval=%s",
        aoi, resolution_m, peak, sim_hours, inlet_edge, outlet_edge, eq_key, interval or "2MIN",
    )

    # RAIN-ON-GRID branch: a design storm dispatches to the HEC-RAS 2025 managed
    # engine (author + prepare + solve on the CPU) instead of the 6.6 inflow solve.
    storm = None
    if design_storm_mm_per_hr is not None:
        try:
            s = float(design_storm_mm_per_hr)
            if s > 0.0:
                storm = s
        except (TypeError, ValueError):
            storm = None

    try:
        if storm is not None:
            refine = None
            if channel_refinement is not None:
                try:
                    refine = float(channel_refinement)
                    if not (0.0 < refine < resolution_m):
                        logger.warning(
                            "hecras_flood_2d: channel_refinement %r must be 0<x<resolution_m"
                            " (%.0f); ignoring", channel_refinement, resolution_m)
                        refine = None
                except (TypeError, ValueError):
                    refine = None
            depth = await model_hecras_flood_2d_rog(
                bbox=aoi, design_storm_mm_per_hr=storm,
                storm_duration_hr=float(storm_duration_hr), resolution_m=resolution_m,
                equation_set=eq_key, input_mode=input_mode, channel_refinement=refine,
                resolution_basis=_res_basis, resolution_note=_res_note,
            )
            return depth
        depth = await model_hecras_flood_2d(
            bbox=aoi, target_peak_cfs=peak, resolution_m=resolution_m,
            sim_hours=float(sim_hours), inlet_edge=inlet_edge, outlet_edge=outlet_edge,
            equation_set=eq_key, computation_interval=interval, input_mode=input_mode,
            resolution_basis=_res_basis, resolution_note=_res_note,
        )
        if isinstance(depth, dict):
            return depth
        return depth
    except asyncio.CancelledError:
        raise
    except HecrasFlood2dError as exc:
        logger.warning("hecras_flood_2d failed: %s (%s)", exc.error_code, exc)
        return {"status": "error", "error_code": exc.error_code, "error_message": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.exception("hecras_flood_2d unexpected failure")
        return {"status": "error", "error_code": "HECRAS_INTERNAL_ERROR", "error_message": str(exc)}


def _coerce_bbox(bbox: Any) -> list[float] | None:
    if not bbox:
        return None
    try:
        vals = [float(x) for x in bbox]
    except (TypeError, ValueError):
        return None
    if len(vals) != 4:
        return None
    min_lon, min_lat, max_lon, max_lat = vals
    if not (max_lon > min_lon and max_lat > min_lat):
        return None
    return [min_lon, min_lat, max_lon, max_lat]


def _geocode_bbox(location: str) -> list[float] | None:
    """Best-effort geocode of a place name to a bbox via ``geocode_location``."""
    try:
        from trid3nt_server.agent.tools import TOOL_REGISTRY

        res = TOOL_REGISTRY["geocode_location"].fn(query=location)
        bb = getattr(res, "bbox", None) or (res.get("bbox") if isinstance(res, dict) else None)
        return _coerce_bbox(bb)
    except Exception as exc:  # noqa: BLE001
        logger.info("hecras_flood_2d geocode of %r failed: %s", location, exc)
        return None


# --------------------------------------------------------------------------- #
# The composer.
# --------------------------------------------------------------------------- #
from trid3nt_server.emission.pipeline_emitter import (
    begin_substeps,
    current_emitter,
    mint_dispatch_and_sim_cards,
    route_sim_terminal,
    substep,
)
from trid3nt_server.agent.tools.publish_layer.publish_layer import (
    PublishLayerError,
    publish_layer,
)
from trid3nt_server.emission.layer_uri_emit import (
    publish_input_layer,
)
from trid3nt_server.agent.workflows.hecras.postprocess_hecras import (
    PostprocessHecrasError,
    postprocess_hecras,
)
from trid3nt_server.agent.workflows.hecras.run_hecras import HECRAS_FLOOD2D_SOLVER_NAME


def _fetch_dem_local(bbox: list[float]) -> tuple[str, str]:
    """Fetch the AOI DEM (seam-1) and download it to a local temp GeoTIFF.

    Returns ``(local_path, s3_uri)`` -- the s3 uri rides the cache COG so the
    composer can surface the fetched terrain as a role=context input.
    """
    from trid3nt_server.agent.tools import TOOL_REGISTRY

    layer = TOOL_REGISTRY["fetch_dem"].fn(
        bbox=list(bbox), resolution_m=10, purpose="terrain")
    uri = getattr(layer, "uri", None) or (layer.get("uri") if isinstance(layer, dict) else None)
    if not uri:
        raise HecrasFlood2dError(HECRAS_SOLVE_FAILED, f"fetch_dem returned no uri for bbox {bbox}")
    from trid3nt_server.agent.tools.simulation.solver.solver import _download_object

    tmp = Path(tempfile.mkdtemp(prefix="flood2d-dem-")) / "dem.tif"
    _download_object(str(uri), tmp)
    return str(tmp), str(uri)


def acquire_channel_inputs(bbox: list[float], workdir: str, pour_point=None):
    """Delineated catchment + channel network for the AOI (refined mesh).

    Reuses the TELEMAC rain-on-grid acquisition: an explicit ``pour_point`` (lon,lat)
    is the drainage outlet when given (the user-named catchment outlet); otherwise it
    is the lowest-elevation DEM cell in the bbox. The catchment is delineated there and
    the channel network is ``fetch_river_geometry``. Returns ``(catchment_geojson_path,
    flowlines_path)`` or ``(None, None)`` on any failure -- the caller then degrades to
    the uniform mesh honestly. Shared with ``generate_mesh`` mode=hecras."""
    import json as _json

    try:
        import numpy as np
        import rasterio
        from shapely.geometry import mapping
        from trid3nt_server.agent.tools import TOOL_REGISTRY
        from trid3nt_server.agent.workflows.telemac.rain_on_grid.mesh_acquisition import (
            _delineate_catchment,
        )

        rundir = Path(workdir) / "channel"
        rundir.mkdir(parents=True, exist_ok=True)
        # D8 delineation needs a NATIVELY GEOGRAPHIC (EPSG:4326) DEM -- the lon/lat frame
        # the index-space outlet snap expects. 3DEP is projected (EPSG:5070) and its
        # snap mis-maps (a spurious "pour point outside the window"), so delineate on
        # Copernicus GLO-30 (the same choice the TELEMAC watershed mesher makes).
        dem_layer = TOOL_REGISTRY["fetch_dem"].fn(
            bbox=list(bbox), resolution_m=30, source="copernicus")
        dem_uri = getattr(dem_layer, "uri", None) or (
            dem_layer.get("uri") if isinstance(dem_layer, dict) else None)
        from trid3nt_server.agent.tools.simulation.solver.solver import _download_object
        dem_local = rundir / "dem.tif"
        _download_object(str(dem_uri), dem_local)
        if pour_point is not None:
            # the user-named catchment outlet (matches generate_mesh's pour_point).
            px, py = float(pour_point[0]), float(pour_point[1])
        else:
            # pour point = lowest-elevation cell (the outlet the catchment drains to)
            with rasterio.open(dem_local) as src:
                arr = src.read(1, masked=True)
                r, c = np.unravel_index(int(np.ma.argmin(arr)), arr.shape)
                px, py = src.xy(r, c)
                if str(src.crs).upper() not in ("EPSG:4326", "OGC:CRS84"):
                    from pyproj import Transformer
                    px, py = Transformer.from_crs(
                        src.crs, "EPSG:4326", always_xy=True).transform(px, py)
        catch, _outlet, area_km2, cell_count = _delineate_catchment(
            rundir, list(bbox), (float(px), float(py)), str(dem_uri))
        catch_path = rundir / "catchment.geojson"
        catch_path.write_text(_json.dumps({"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {}, "geometry": mapping(catch)}]}))

        rv = TOOL_REGISTRY["fetch_river_geometry"].fn(bbox=tuple(bbox), source="nhdplus_hr")
        from trid3nt_server.agent.tools.cache import read_object_bytes_s3
        fl_path = rundir / "flowlines.fgb"
        fl_path.write_bytes(
            read_object_bytes_s3(rv.uri) if str(rv.uri).startswith("s3://")
            else Path(rv.uri).read_bytes())
        logger.info("hecras_flood_2d RoG: channel inputs ready (catchment %.1f km2, %d cells)",
                    area_km2, cell_count)
        return str(catch_path), str(fl_path)
    except Exception as exc:  # noqa: BLE001 -- refinement is best-effort
        logger.warning("hecras_flood_2d RoG: channel-input acquisition failed (%s)", exc)
        return None, None


def _author_and_compose(dem_tif: str, workdir: str, *, peak_cfs: float,
                        resolution_m: float, inlet_edge: str | None,
                        outlet_edge: str | None,
                        equation_set: str = "Diffusion Wave",
                        computation_interval: str | None = None) -> Any:
    """Run the authoring + compose stages (docker author + host compose)."""
    import sys

    if str(_WORKERS_FRESHTOPO) not in sys.path:
        sys.path.insert(0, str(_WORKERS_FRESHTOPO))
        sys.path.insert(0, str(_WORKERS_FRESHTOPO.parents[2]))  # hecras2025 (writer)
    from flood2d_pipeline import author_and_compose, Flood2dPipelineError  # type: ignore

    try:
        result, info = author_and_compose(
            dem_tif, workdir, peak_cfs=peak_cfs, resolution_m=resolution_m,
            inflow_edge=inlet_edge, ds_edge=(outlet_edge or "s"),
            equation_set=equation_set, computation_interval=computation_interval,
        )
    except Flood2dPipelineError as exc:
        raise HecrasFlood2dError(HECRAS_SOLVE_FAILED, f"authoring/compose failed: {exc}") from exc
    return result


def _stage_deck_manifest(deck_dir: str, run_tag: str) -> str:
    """Upload the composed deck files to the cache bucket + write the run_solver
    manifest (M3-gate no-archetype path: plan_hdf + geom_suffix on staged inputs)."""
    from trid3nt_server.agent.tools.simulation.solver.solver import _get_s3_client

    cache_bucket = (os.environ.get("TRID3NT_CACHE_BUCKET") or "").strip()
    if not cache_bucket:
        raise HecrasFlood2dError(HECRAS_SOLVE_FAILED, "TRID3NT_CACHE_BUCKET must be set")
    s3 = _get_s3_client()
    deck = Path(deck_dir)
    deck_files = ["Fresh2D.p04.tmp.hdf", "Fresh2D.x04", "Fresh2D.b04"]
    inputs = []
    for fn in deck_files:
        p = deck / fn
        if not p.is_file():
            raise HecrasFlood2dError(HECRAS_SOLVE_FAILED, f"composed deck missing {fn}")
        key = f"hecras_flood2d/{run_tag}/{fn}"
        s3.put_object(Bucket=cache_bucket, Key=key, Body=p.read_bytes())
        inputs.append({"gs_uri": f"s3://{cache_bucket}/{key}", "dest": fn})
    manifest = {
        "run_id": run_tag,
        "plan_hdf": "Fresh2D.p04.tmp.hdf",
        "geom_suffix": "x04",
        "run_geompre": True,
        "inputs": inputs,
        "hecras_args": [],
        "outputs": ["Fresh2D.p04.tmp.hdf", "hecras_metrics.json"],
    }
    key = f"hecras_flood2d/{run_tag}/manifest.json"
    s3.put_object(
        Bucket=cache_bucket, Key=key,
        Body=json.dumps(manifest, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    return f"s3://{cache_bucket}/{key}"


def _download_plan_hdf(run_id: str) -> str:
    from trid3nt_server.agent.tools.simulation.solver.solver import (
        _get_runs_bucket, _get_s3_client,
    )

    s3 = _get_s3_client()
    key = f"{run_id}/Fresh2D.p04.tmp.hdf"
    tmp = Path(tempfile.mkdtemp(prefix=f"flood2d-{run_id}-")) / "Fresh2D.p04.tmp.hdf"
    try:
        resp = s3.get_object(Bucket=_get_runs_bucket(), Key=key)
        tmp.write_bytes(resp["Body"].read())
    except Exception as exc:  # noqa: BLE001
        raise HecrasFlood2dError(
            HECRAS_SOLVE_FAILED, f"solved plan HDF not downloadable ({key}): {exc}"
        ) from exc
    return str(tmp)


async def model_hecras_flood_2d(
    *,
    bbox: list[float],
    target_peak_cfs: float,
    resolution_m: float,
    sim_hours: float = 24.0,
    inlet_edge: str | None = None,
    outlet_edge: str | None = None,
    equation_set: str = _DEFAULT_EQUATION_SET,
    computation_interval: str | None = None,
    input_mode: str | None = None,
    resolution_basis: str = "derived",
    resolution_note: str | None = None,
) -> HecrasDepthLayerURI | dict[str, Any]:
    """fetch DEM -> author+compose -> run_solver -> postprocess -> publish."""
    emitter = current_emitter()
    begin_substeps(emitter, 4)  # fetch+author, run_solver, postprocess, publish

    eq_key = str(equation_set or "").strip().lower()
    if eq_key not in _EQUATION_SET_MAP:
        eq_key = _DEFAULT_EQUATION_SET
    eq_hec = _EQUATION_SET_MAP[eq_key]

    n_cells_est = _estimate_cells(bbox, resolution_m)
    review_entries: list[SyntheticInput] = [
        SyntheticInput(
            param="geometry", value="authored 2D mesh (HEC-RAS 2025 AuthorMesh)",
            basis="derived",
            note="TryCreateMesh topology + MeshPropertyTables.ComputeFrom subgrid "
            "tables over the fetched terrain (transplant-path validated)",
        ),
        SyntheticInput(
            param="terrain", value="fetch_dem (3DEP/Copernicus)", basis="fetched",
            note="reprojected to a local ftUS grid; elevations m->US survey feet",
        ),
        SyntheticInput(
            param="peak_inflow_cfs", value=round(float(target_peak_cfs), 1), units="cfs",
            basis="user", note="peak of the inflow hydrograph forcing the run",
        ),
        SyntheticInput(
            param="resolution_m", value=round(float(resolution_m), 1), units="m",
            basis=resolution_basis,
            note=(resolution_note or
                  f"2D cell size (~{n_cells_est} cells; granularity-gated)"),
        ),
        SyntheticInput(
            param="equation_set", value=eq_hec,
            basis="user" if eq_key != _DEFAULT_EQUATION_SET else "default",
            note="2D solver stamped on the plan (Diffusion Wave = default; "
            "SWE-ELM = full-momentum; same footprint, local inertial detail differs)",
        ),
        SyntheticInput(
            param="computation_interval", value=(computation_interval or "2MIN"),
            basis="user" if computation_interval else "default",
            note="RasUnsteady 2D time step (stability knob; coarser overshoots the peak)",
        ),
    ]
    review = await gate_input_review(
        tool_name="hecras_flood_2d",
        mode=input_mode,
        entries=review_entries,
        params={"bbox": bbox, "target_peak_cfs": target_peak_cfs, "resolution_m": resolution_m},
    )
    if not review.proceed:
        return {
            "status": "error",
            "error_code": "HECRAS_INPUT_REVIEW_CANCELLED",
            "error_message": review.cancel_reason or "input review not approved; the solver did not run",
        }
    target_peak_cfs = float(review.params.get("target_peak_cfs", target_peak_cfs) or target_peak_cfs)
    resolution_m = float(review.params.get("resolution_m", resolution_m) or resolution_m)

    # --- Stage 1: fetch DEM + author + compose the deck (heavy; off-loop) ------ #
    run_tag = new_ulid()
    workdir = tempfile.mkdtemp(prefix=f"flood2d-{run_tag}-")
    async with substep(emitter, "author_compose"):
        dem_tif, _dem_s3_uri = await asyncio.to_thread(_fetch_dem_local, bbox)
        result = await asyncio.to_thread(
            _author_and_compose, dem_tif, workdir,
            peak_cfs=target_peak_cfs, resolution_m=resolution_m,
            inlet_edge=inlet_edge, outlet_edge=outlet_edge,
            equation_set=eq_hec, computation_interval=computation_interval,
        )
    logger.info(
        "hecras_flood_2d authored deck run_tag=%s cells=%d faces=%d crs=local-ftUS",
        run_tag, result.cells_real, result.faces,
    )

    # --- Stage 2: dispatch the composed deck to run_solver --------------------- #
    manifest_uri = await asyncio.to_thread(_stage_deck_manifest, result.deck_dir, run_tag)
    from trid3nt_server.agent.tools.simulation.solver.solver import (
        run_solver, wait_for_completion,
    )

    handle = run_solver(
        solver=HECRAS_FLOOD2D_SOLVER_NAME, model_setup_uri=manifest_uri, compute_class="medium",
    )
    sim_step_id = await mint_dispatch_and_sim_cards(
        emitter=emitter, solver=HECRAS_FLOOD2D_SOLVER_NAME, handle=handle, compute_class="medium",
    )
    run_result = None
    try:
        async with substep(emitter, "run_solver"):
            run_result = await wait_for_completion(handle)
    except asyncio.CancelledError:
        await route_sim_terminal(emitter, sim_step_id, run_result=None)
        raise
    await route_sim_terminal(emitter, sim_step_id, run_result=run_result)

    if run_result is None or run_result.status != "complete":
        raise HecrasFlood2dError(
            HECRAS_SOLVE_FAILED,
            f"HEC-RAS 2D solve did not complete (status={getattr(run_result,'status',None)}, "
            f"error_code={getattr(run_result,'error_code',None)}): "
            f"{getattr(run_result,'error_message','') or ''}",
        )
    batch_run_id = getattr(run_result, "run_id", None) or run_tag

    metrics = await asyncio.to_thread(_read_run_metrics, batch_run_id)
    va = metrics.get("volume_accounting") or {}
    try:
        vol_err = float(va.get("Error Percent")) if va.get("Error Percent") is not None else None
    except (TypeError, ValueError):
        vol_err = None
    peak_cfs = metrics.get("peak_inflow_cfs") or target_peak_cfs

    # --- Stage 3: postprocess the solved plan HDF ------------------------------ #
    plan_path = await asyncio.to_thread(_download_plan_hdf, batch_run_id)
    try:
        async with substep(emitter, "postprocess_hecras"):
            layers, pp_metrics = await asyncio.to_thread(
                postprocess_hecras, plan_path, run_id=batch_run_id, flow_scale=1.0,
                peak_inflow_cfs=(float(peak_cfs) if peak_cfs is not None else None),
                volume_error_pct=vol_err, fallback_note=_FIDELITY_NOTE,
            )
    except PostprocessHecrasError as exc:
        raise HecrasFlood2dError(exc.error_code, str(exc)) from exc
    finally:
        try:
            Path(plan_path).unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass

    if not layers:
        raise HecrasFlood2dError(HECRAS_SOLVE_FAILED, "postprocess produced no depth layer")
    depth = layers[0]
    assert isinstance(depth, HecrasDepthLayerURI)
    mesh_layer = layers[1] if len(layers) > 1 else None

    # --- Stage 4: publish the peak-depth COG (render chokepoint) --------------- #
    async with substep(emitter, "publish_layer"):
        depth = await asyncio.to_thread(_publish_depth_layer, depth, review_entries)

    if mesh_layer is not None:
        try:
            await publish_input_layer(emitter, mesh_layer, role="context")
        except Exception as exc:  # noqa: BLE001
            logger.warning("hecras_flood_2d mesh preview emit skipped: %s", exc)

    if emitter is not None:
        try:
            await _maybe_emit_inflow_chart(emitter, pp_metrics, bbox)
        except Exception as exc:  # noqa: BLE001
            logger.warning("hecras_flood_2d inflow chart skipped: %s", exc)

    if emitter is not None and depth.bbox:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(depth.bbox)})
        except Exception as exc:  # noqa: BLE001
            logger.warning("hecras_flood_2d zoom-to failed: %s", exc)

    logger.info(
        "model_hecras_flood_2d complete run_id=%s depth_max_ft=%.3g wet_cells=%s "
        "peak_cfs=%s vol_err=%s uri=%s",
        batch_run_id, depth.depth_max_ft, depth.wet_cell_count, peak_cfs,
        depth.volume_error_pct, depth.uri,
    )
    return depth


_ROG_FIDELITY_NOTE: str = (
    "HEC-RAS 2025 MANAGED engine (beta), rain-on-grid: a uniform design storm on a "
    "structured 2D area over the fetched terrain, prepared + solved on the CPU. "
    "RAIN-ONLY -- the 2025 beta exposes NO infiltration layer, so this is gross "
    "rainfall (an upper-bound runoff, no SCS-CN loss). Screening-grade; validated on "
    "the Coweeta Creek Godara-2024 envelope (25 mm/hr x 6 h).."
)


async def model_hecras_flood_2d_rog(
    *,
    bbox: list[float],
    design_storm_mm_per_hr: float,
    storm_duration_hr: float = 6.0,
    resolution_m: float = _DEFAULT_RES_M,
    equation_set: str = _DEFAULT_EQUATION_SET,
    input_mode: str | None = None,
    channel_refinement: float | None = None,
    resolution_basis: str = "derived",
    resolution_note: str | None = None,
) -> HecrasDepthLayerURI | dict[str, Any]:
    """Rain-on-grid on the HEC-RAS 2025 managed engine -> peak-depth COG.

    fetch DEM -> author + prepare + solve on the 2025 CPU engine (rog2025_pipeline,
    mounted-driver, no worker-image rebuild) -> rasterize max depth to a 4326 COG ->
    publish. Rain-only (no infiltration in the beta). ``channel_refinement`` (a target
    channel cell size, m) authors the paper-style GRADED mesh instead of the
    uniform grid -- it needs the delineated catchment + channel network, acquired for the
    AOI; if acquisition fails it degrades to the uniform mesh with a note."""
    import sys

    emitter = current_emitter()
    begin_substeps(emitter, 2)  # rog_solve, publish

    eq_key = str(equation_set or "").strip().lower()
    diffusion = eq_key != "full_swe"

    # --- consume a pre-built HEC-RAS mesh from the case, if one exists + the
    # user accepts. A generate_mesh mode=hecras artifact carries the graded seeds +
    # breaklines + local terrain frame; the gate offers it (labeled default USE), and
    # an accepted mesh is re-realized + solved directly (no fresh delineation / seeding
    # / channel acquisition). Declined / absent / incompatible -> unchanged 0209/0210. --
    from trid3nt_server.agent.workflows.mesh.precondition_gate import gate_supplied_mesh
    from trid3nt_server.agent.tools.simulation.solver.solver import _get_s3_client

    supplied_note: str | None = None
    supplied_art = None
    try:
        mesh_decision = await gate_supplied_mesh(
            tool_name="hecras_flood_2d", engine="hecras", input_mode=input_mode,
            s3_client=_get_s3_client())
        supplied_note = mesh_decision.note
        if mesh_decision.use and mesh_decision.artifact is not None:
            supplied_art = mesh_decision.artifact
    except Exception as exc:  # noqa: BLE001 -- the gate never blocks the run
        logger.warning("hecras_flood_2d RoG: mesh precondition gate skipped (%s)", exc)

    consumed = supplied_art is not None
    if consumed:
        channel_refinement = None  # the case mesh defines the resolution field
        geom_value = f"consumed case mesh {supplied_art.name!r} (HEC-RAS 2025, channel-refined)"
    else:
        geom_value = ("channel-refined 2D mesh (HEC-RAS 2025)" if channel_refinement
                      else "structured 2D area (HEC-RAS 2025)")
    review_entries: list[SyntheticInput] = [
        SyntheticInput(param="geometry", value=geom_value,
                       basis=("user" if consumed else "derived"),
                       note=(supplied_note or
                             "managed-engine rain-on-grid mesh over the fetched terrain")),
        SyntheticInput(param="terrain", value="fetch_dem (3DEP/Copernicus)", basis="fetched",
                       note="reprojected to a local SI grid (metres)"),
        SyntheticInput(param="design_storm_mm_per_hr", value=round(float(design_storm_mm_per_hr), 2),
                       units="mm/hr", basis="user", note="uniform constant precipitation forcing"),
        SyntheticInput(param="storm_duration_hr", value=round(float(storm_duration_hr), 2),
                       units="h", basis="user", note="constant storm window"),
        SyntheticInput(param="infiltration", value="none (rain-only)", basis="derived",
                       note="the 2025 beta has no infiltration layer -- gross rainfall, upper-bound runoff"),
    ]
    # (audit #6): surface the resolution basis, incl. the supported-range
    # clamp when it bound (unless a channel-refined/consumed case mesh defines the
    # field, in which case the uniform resolution is not the operative granularity).
    if not consumed and not channel_refinement:
        review_entries.append(SyntheticInput(
            param="resolution_m", value=round(float(resolution_m), 1), units="m",
            basis=resolution_basis,
            note=(resolution_note or "uniform 2D cell size (granularity-gated)"),
        ))
    review = await gate_input_review(
        tool_name="hecras_flood_2d", mode=input_mode, entries=review_entries,
        params={"bbox": bbox, "design_storm_mm_per_hr": design_storm_mm_per_hr})
    if not review.proceed:
        return {"status": "error", "error_code": "HECRAS_INPUT_REVIEW_CANCELLED",
                "error_message": review.cancel_reason or "input review not approved; the solver did not run"}

    run_tag = new_ulid()
    workdir = tempfile.mkdtemp(prefix=f"rog2025-{run_tag}-")

    if str(_WORKERS_FRESHTOPO) not in sys.path:
        sys.path.insert(0, str(_WORKERS_FRESHTOPO))
        sys.path.insert(0, str(_WORKERS_FRESHTOPO.parents[2]))
    from rog2025_pipeline import (  # type: ignore
        run_rog2025, run_rog2025_prebuilt, build_depth_cog, build_depth_cog_unstructured)

    catchment_geojson = None
    flowlines_path = None

    if consumed:
        # CONSUME the case mesh: stage its stored authoring bundle + re-realize the SAME
        # cell mesh + solve. NO fetch_dem, NO delineation, NO re-seeding -- the mesh NATE
        # inspected IS the mesh that solves (TryCreateMesh deterministic on the seeds).
        from trid3nt_server.agent.workflows.mesh.artifact import (
            materialize_hecras_mesh_inputs,
        )
        bundle = await asyncio.to_thread(
            materialize_hecras_mesh_inputs, supplied_art, workdir, _get_s3_client())
        catchment_geojson = bundle.get("catchment")
        prep_doc = json.loads(Path(bundle["prep_json"]).read_text())
        async with substep(emitter, "rog_solve"):
            result = await asyncio.to_thread(
                run_rog2025_prebuilt, prep_doc, bundle["local_dem"], bundle["seeds"],
                bundle["breaklines"], workdir,
                precip_mm_hr=float(design_storm_mm_per_hr),
                storm_hours=float(storm_duration_hr), diffusion=diffusion,
                catchment_geojson=catchment_geojson)
        logger.info(
            "hecras_flood_2d RoG: CONSUMED case mesh %r (%d cells) -- re-realized from "
            "the stored seeds, NO fresh delineation/seeding",
            supplied_art.name, supplied_art.element_count)
    else:
        if channel_refinement is not None:
            catchment_geojson, flowlines_path = await asyncio.to_thread(
                acquire_channel_inputs, bbox, workdir)
            if catchment_geojson is None:        # acquisition failed -> uniform, honest
                logger.warning("hecras_flood_2d RoG: channel refinement inputs unavailable; "
                               "falling back to the uniform mesh")
                channel_refinement = None
                review_entries[0] = SyntheticInput(
                    param="geometry", value="structured 2D area (HEC-RAS 2025)", basis="derived",
                    note="channel-refinement inputs unavailable for this AOI; uniform mesh")
        async with substep(emitter, "rog_solve"):
            dem_tif = await asyncio.to_thread(_fetch_dem_local, bbox)
            result = await asyncio.to_thread(
                run_rog2025, dem_tif, workdir,
                precip_mm_hr=float(design_storm_mm_per_hr), storm_hours=float(storm_duration_hr),
                cell_size=float(resolution_m), elev_units="m", bbox4326=list(bbox),
                diffusion=diffusion, channel_refinement=channel_refinement,
                flowlines_path=flowlines_path, catchment_geojson=catchment_geojson)

    m = result["metrics"]
    refined = consumed or (channel_refinement is not None)
    logger.info(
        "hecras_flood_2d RoG(2025) run=%s consumed=%s peak_q=%.3g m3/s max_depth=%.3g m coeff=%s wall=%ss",
        run_tag, consumed, m["peak_outlet_q_m3s"], m["max_depth_m"], m["runoff_coeff"], result["wall_s"])

    # --- rasterize + publish the peak-depth COG (feet, matching the depth preset) - #
    from trid3nt_contracts.execution import LegendKey
    from trid3nt_contracts.hecras_contracts import HECRAS_DEPTH_STYLE_PRESET
    from trid3nt_server.agent.workflows.shared import cog_io
    from trid3nt_server.agent.tools.simulation.solver.solver import _get_runs_bucket

    async with substep(emitter, "publish"):
        cog_tif = str(Path(workdir) / "rog_depth.tif")
        _cog_fn = build_depth_cog_unstructured if refined else build_depth_cog
        cinfo = await asyncio.to_thread(
            _cog_fn, result["result_h5"], result["prep"], cog_tif, catchment_geojson, 1.0 / 0.3048)
        cog_uri = await asyncio.to_thread(
            cog_io.upload_cog, Path(cog_tif), run_tag, _get_runs_bucket(),
            dest_filename="hecras_rog_depth.tif", log_label="HEC-RAS 2025 RoG depth COG")
        bb = cinfo["bbox4326"]
        dmax_ft = round(cinfo["depth_max"], 3)
        depth = HecrasDepthLayerURI(
            layer_id=f"hecras-rog-depth-{run_tag}",
            name=f"Peak rain-on-grid depth (HEC-RAS 2025, {design_storm_mm_per_hr:.0f} mm/hr x {storm_duration_hr:.0f} h)",
            layer_type="raster", uri=cog_uri, style_preset=HECRAS_DEPTH_STYLE_PRESET,
            role="primary", units="ft", bbox=(bb[0], bb[1], bb[2], bb[3]),
            legend=LegendKey(kind="continuous", colormap="blues", vmin=0.0,
                             vmax=dmax_ft, units="ft", label="Peak water depth (ft)"),
            fallback_note=_ROG_FIDELITY_NOTE,
            depth_max_ft=dmax_ft, depth_mean_ft=round(cinfo["depth_mean"], 3),
            wet_cell_count=int(m["n_catchment_cells"] if m.get("n_catchment_cells") else cinfo["wet_px"]),
            wse_max_ft=dmax_ft,
            synthetic_inputs=list(review_entries),
        )
        depth = await asyncio.to_thread(_publish_depth_layer, depth, review_entries)

    if emitter is not None and depth.bbox:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(depth.bbox)})
        except Exception as exc:  # noqa: BLE001
            logger.warning("hecras_flood_2d RoG zoom-to failed: %s", exc)
    return depth


def _read_run_metrics(run_id: str) -> dict[str, Any]:
    from trid3nt_server.agent.tools.simulation.solver.solver import (
        _get_runs_bucket, _get_s3_client,
    )

    try:
        s3 = _get_s3_client()
        obj = s3.get_object(Bucket=_get_runs_bucket(), Key=f"{run_id}/hecras_metrics.json")
        loaded = json.loads(obj["Body"].read().decode("utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except Exception as exc:  # noqa: BLE001
        logger.info("hecras_flood_2d: run metrics read miss for %s: %s", run_id, exc)
        return {}


def _publish_depth_layer(
    depth: HecrasDepthLayerURI, synthetic_inputs: list[SyntheticInput]
) -> HecrasDepthLayerURI:
    out = depth
    if synthetic_inputs:
        try:
            out = out.model_copy(update={"synthetic_inputs": list(synthetic_inputs)})
        except Exception:  # noqa: BLE001
            pass
    try:
        published_uri = publish_layer(
            layer_uri=out.uri, layer_id=out.layer_id, style_preset=out.style_preset,
        )
        return out.model_copy(update={"uri": published_uri})
    except PublishLayerError as exc:
        logger.warning("hecras_flood_2d publish_layer FAILED layer_id=%s (%s)", out.layer_id, exc)
        return out


async def _maybe_emit_inflow_chart(emitter: Any, metrics: dict[str, Any], bbox: list[float]) -> None:
    if not hasattr(emitter, "emit_chart"):
        return
    series = metrics.get("inflow_hydrograph") or []
    if not series:
        return
    from trid3nt_server.agent.tools.processing.charts_common import build_chart_payload

    values = [{"time_hr": p["t_hr"], "inflow_cfs": p["q_cfs"]} for p in series]
    spec = {
        "data": {"values": values},
        "mark": {"type": "line", "point": True, "color": "#1f5fbf"},
        "encoding": {
            "x": {"field": "time_hr", "type": "quantitative", "title": "time (hours)"},
            "y": {"field": "inflow_cfs", "type": "quantitative", "title": "inflow (cfs)"},
        },
    }
    payload = build_chart_payload(
        vega_lite_spec=spec,
        title="HEC-RAS 2D inflow hydrograph forcing",
        caption="The unsteady inflow forcing the authored-AOI HEC-RAS 2D solve ran with.",
    )
    await emitter.emit_chart(payload)
