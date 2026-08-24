"""Engine template ``telemac3d_stratified_flow`` - TELEMAC-3D stratified /
3D-hydrodynamics engine.

The LLM-facing exposure of TELEMAC's three-dimensional (hydrostatic /
non-hydrostatic) Navier-Stokes solver with active-tracer (temperature / salinity)
baroclinic coupling - the one genuinely NEW solver leg in the TELEMAC family and
the physics a 2D depth-averaging structurally cannot resolve. ONE question-class
tool with THREE modes (the board's TELEMAC-3D rows):

  * ``stratification``   - thermal stratification persistence vs wind mixing in a
                           lake (the lake-turnover question); the discriminating
                           pair is calm (keeps the thermocline) vs windy (mixes it
                           away). The stratified 3D column the AED2 lake-ecology
                           coupling (STOP) requires.
  * ``wind_circulation`` - the vertical velocity structure a steady wind builds in
                           a closed basin (surface downwind, bottom upwind return
                           flow, depth-integrated ~0). THE 3D-vs-2D discriminant (a
                           2D depth-averaged model returns ~zero velocity everywhere
                           in a closed basin).
  * ``salt_wedge``       - density-driven salt-wedge / gravity-current physics
                           (the classic lock-exchange: a dense saline column
                           produces a bottom current at the Benjamin front speed).
                           The salinity-intrusion / estuary baroclinic physics.
                           Hydrostatic vs non-hydrostatic is the dam-break-3D
                           fidelity rung.

Two bathymetry paths (see the worker ``telemac3d_build``):
  * ``noaa_greatlakes`` - a REAL US Great Lake AOI, bed sampled from the NOAA NGDC
    lake-datum bathymetry (Superior/Michigan/Huron - deep enough to stratify), for
    stratification / wind_circulation.
  * ``idealized``       - the geography-free closed basin the sandbox proved
    (replicates the classic TELEMAC-3D validation set; clears the citations law
    like the GWE analytic V&V). ``salt_wedge`` is idealized-only (a real estuary
    needs a tidal liquid boundary), labeled as such.

Structural sibling of ``tomawac_wave_field`` / ``artemis_harbor_agitation`` (same
LOCAL-DOCKER solve seam, same run_solver dispatch, same publish_layer render
path): a registered engine TEMPLATE tagged ``engine="telemac", tier="template"``.
Determinism boundary (invariant 1): every 3D number the agent narrates comes from
the typed ``Telemac3dLayerURI`` fields the worker/postprocess computed - never
free-generated. The ``fallback_note`` carries the honesty floor (3D screening,
idealized/prescribed forcing, not a calibrated hindcast).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from trid3nt_contracts import new_ulid
from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.telemac_contracts import (
    TELEMAC3D_STRATIFICATION_STYLE_PRESET,
    Telemac3dLayerURI,
)
from trid3nt_contracts.tool_registry import AtomicToolMetadata, ResolutionSpec

from trid3nt_server.tools import TOOL_REGISTRY, register_tool
from trid3nt_server.tools.tool_arg_normalizer import coerce_bbox_value
from trid3nt_server.workflows.telemac._template_card import TemplateCard
from trid3nt_server.workflows.telemac.postprocess_telemac import (
    PostprocessTelemacError,
    postprocess_telemac3d,
)
from trid3nt_server.workflows.telemac.run_telemac import TELEMAC3D_SOLVER_NAME

logger = logging.getLogger("trid3nt_server.workflows.telemac.stratified_flow")

__all__ = ["telemac3d_stratified_flow", "model_telemac3d_stratified_flow",
           "Telemac3dStratifiedError"]


class Telemac3dStratifiedError(RuntimeError):
    """Raised when the TELEMAC-3D chain fails fatally before producing a layer."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


#: The three question classes this tool covers (one tool, three modes).
_FLOW_MODES = ("stratification", "wind_circulation", "salt_wedge")

#: Rough lon/lat bboxes of the five Great Lakes open water (auto real-bathy gate).
_GREAT_LAKES: dict[str, tuple[float, float, float, float]] = {
    "superior": (-92.2, 46.4, -84.3, 49.1),
    "michigan": (-88.1, 41.6, -84.7, 46.1),
    "huron": (-84.8, 43.0, -79.7, 46.3),
    "erie": (-83.5, 41.3, -78.8, 42.9),
    "ontario": (-79.9, 43.2, -76.0, 44.3),
}

#: LOUD labeled demo defaults (no met-forcing fetcher exists yet): a prescribed
#: warm-over-cold column + optional steady wind. Narrated demo defaults, never
#: observations.
DEFAULT_WARM_TEMP_C = 25.0
DEFAULT_COLD_TEMP_C = 15.0
DEFAULT_THERMOCLINE_DEPTH_M = 8.0
DEFAULT_WIND_MPS = 0.0                 # calm by default (keeps the stratification)
DEFAULT_WIND_DIR_FROM_DEG = 270.0
DEFAULT_DURATION_HOURS = 5.0
DEFAULT_NPLAN = 13
DEFAULT_REAL_RES_M = 2000.0
DEFAULT_IDEALIZED_RES_M = 250.0


def _great_lake_for(lon: float, lat: float) -> str | None:
    for name, (x0, y0, x1, y1) in _GREAT_LAKES.items():
        if x0 <= lon <= x1 and y0 <= lat <= y1:
            return name
    return None


def _classify_mode(text: str | None, explicit: str | None) -> str:
    """Pick the 3D question class from an explicit arg or prompt keywords."""
    if explicit and str(explicit).strip().lower() in _FLOW_MODES:
        return str(explicit).strip().lower()
    t = (text or "").lower()
    if any(w in t for w in ("salt", "saline", "salinity", "estuary", "wedge",
                            "gravity current", "lock exchange", "intrusion",
                            "density current", "brackish")):
        return "salt_wedge"
    if any(w in t for w in ("wind-driven", "wind driven", "circulation", "gyre",
                            "return flow", "upwelling", "wind setup",
                            "vertical velocity", "seiche")):
        return "wind_circulation"
    return "stratification"


#: DECLARED target_resolution_m range. A 3D solve is heavy (NPOIN2 *
#: NPLAN nodes), so a coarse horizontal budget: GRID_H_FLOOR_M=400 m is the finest
#: the real grid authors; a large lake is coarsened under GRID_NODE_CAP.
_TELEMAC3D_RES_SPEC = ResolutionSpec(
    param="target_resolution_m",
    unit="m",
    min_value=100.0,
    native_hint="NOAA Great Lakes lake-datum bathymetry (~90 m) / idealized grid",
    constraint_source="solver",
    rationale=(
        "target horizontal grid node spacing; the 3D node count is NPOIN2*NPLAN, "
        "so a large lake is coarsened under the GRID_NODE_CAP budget (self-"
        "labeled); the vertical (sigma-plane) resolution is the nplan knob"
    ),
)

_TELEMAC3D_METADATA = AtomicToolMetadata(
    name="telemac3d_stratified_flow",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="telemac",
    tier="template",
    resolution_specs=(_TELEMAC3D_RES_SPEC,),
)

TEMPLATE_CARD = TemplateCard(
    question=(
        "the 3D VERTICAL STRUCTURE of a water body that a 2D depth-averaged model "
        "cannot resolve - thermal STRATIFICATION persistence vs wind mixing in a "
        "lake (does the thermocline hold or turn over), the WIND-DRIVEN vertical "
        "circulation in a closed basin (surface downwind / bottom return flow), or "
        "density-driven SALT-WEDGE / salinity intrusion (a bottom gravity current); "
        "TELEMAC-3D baroclinic Navier-Stokes solver (the 3D refinement tier)"
    ),
    required_inputs=["location OR bbox (a lake / basin AOI)"],
    knobs=(
        "flow_mode (stratification / wind_circulation / salt_wedge), "
        "wind_speed_mps, wind_direction_deg, warm_temp_c, cold_temp_c, "
        "thermocline_depth_m, non_hydrostatic, nplan, target_resolution_m, "
        "sim_duration_hours, bathy_source (noaa_greatlakes / idealized)"
    ),
)


@register_tool(
    _TELEMAC3D_METADATA,
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def telemac3d_stratified_flow(
    location: str | None = None,
    bbox: tuple[float, float, float, float] | list[float] | str | None = None,
    flow_mode: str | None = None,
    wind_speed_mps: float = DEFAULT_WIND_MPS,
    wind_direction_deg: float = DEFAULT_WIND_DIR_FROM_DEG,
    warm_temp_c: float = DEFAULT_WARM_TEMP_C,
    cold_temp_c: float = DEFAULT_COLD_TEMP_C,
    thermocline_depth_m: float = DEFAULT_THERMOCLINE_DEPTH_M,
    non_hydrostatic: bool = False,
    nplan: int = DEFAULT_NPLAN,
    target_resolution_m: float | None = None,
    sim_duration_hours: float = DEFAULT_DURATION_HOURS,
    bathy_source: str = "auto",
    compute_class: str = "medium",
    input_mode: str | None = None,
    **_extra_ignored: Any,
) -> Telemac3dLayerURI | dict[str, Any]:
    """The 3D VERTICAL STRUCTURE of a water body a 2D depth-averaged model cannot resolve.

    Fidelity: TELEMAC-3D three-dimensional (hydrostatic / non-hydrostatic)
    Navier-Stokes solver with active-tracer (temperature / salinity) baroclinic
    density coupling over sigma layers - the 3D refinement tier. A planning-grade
    demo driven by a PRESCRIBED warm-over-cold column / steady wind (no met-forcing
    fetcher yet), not a calibrated hindcast.

    THE tool for "does this lake stratify / turn over", "thermal stratification /
    thermocline", "epilimnion over hypolimnion", "wind-driven vertical circulation
    / return flow in a lake", "surface-vs-bottom current structure", "salt wedge /
    salinity intrusion in an estuary", "density-driven bottom gravity current".
    Answers THREE question classes via ``flow_mode``:

      - ``stratification`` (default) - a warm surface layer over a cold bottom
        either KEEPS its thermocline (calm) or is mixed away by wind. The metric is
        the persisting top-to-bottom temperature difference.
      - ``wind_circulation`` - a steady wind drives surface water downwind and a
        return flow at depth (surface + / bottom -, depth-average ~0) - invisible
        to a 2D model.
      - ``salt_wedge`` - a dense saline column drives a bottom gravity current at
        the Benjamin front speed (density-driven estuary physics). Idealized-only.

    Do NOT use this for: a 2D river dye/contaminant plume (``telemac_river_dye``);
    inundation DEPTH (``sfincs_flood`` / ``geoclaw_inundation``); the surface wave
    field (``tomawac_wave_field`` / ``artemis_harbor_agitation``). This tool returns
    a 3D surface/bottom field (temperature / velocity / salinity), not a depth or a
    wave height.

    Params:
        location: a lake / basin place near the AOI (e.g. "Lake Superior",
            "Lake Michigan"). Supply this OR ``bbox`` - geocoded, never hand-typed
            coords.
        bbox: OPTIONAL explicit AOI ``(min_lon, min_lat, max_lon, max_lat)``
            EPSG:4326 (deep open water inside a lake for the real-bathy path).
        flow_mode: OPTIONAL question class - ``stratification`` /
            ``wind_circulation`` / ``salt_wedge``. Unset -> inferred from the
            prompt (defaults to stratification).
        wind_speed_mps: sustained wind speed (m/s). Default 0 (CALM - the
            stratification-persists half of the calm-vs-windy pair; a nonzero value
            mixes the thermocline / drives the wind circulation). Clamped [0, 40].
        wind_direction_deg: meteorological direction the wind blows FROM (compass,
            0=N/90=E/180=S/270=W). Default 270 (westerly).
        warm_temp_c: epilimnion (warm surface) temperature, C. Default 25.
        cold_temp_c: hypolimnion (cold bottom) temperature, C. Default 15.
        thermocline_depth_m: depth of the thermocline below the surface, m.
            Default 8.
        non_hydrostatic: force the non-hydrostatic solver (salt_wedge dam-break-3D
            fidelity rung). Default False (hydrostatic).
        nplan: number of vertical sigma levels (the 3D degree of freedom).
            Default 13. Clamped [5, 30].
        target_resolution_m: OPTIONAL horizontal grid node spacing (m). Unset -> a
            labeled default (real lake 2000 m, idealized 250 m). Floored + coarsened
            under the node budget (self-labeled).
        sim_duration_hours: simulated duration (h). Default 5. Clamped [1, 24].
        bathy_source: ``"auto"`` (default - a Great Lakes AOI uses real NOAA
            lake-datum bathymetry, else an idealized basin labeled as such) |
            ``"noaa_greatlakes"`` | ``"idealized"``.
        compute_class: compute class. Default ``"medium"``.
        input_mode: ``"user_gated"`` reviews the resolved forcing before the solve;
            ``"auto"`` (default) proceeds labeled.

    Returns:
        On success: ``Telemac3dLayerURI`` (``LayerURI`` subtype) - the SURFACE-layer
        field COG (the emitter also loads the BOTTOM-layer companion + animates the
        TELEMAC-3D SELAFIN mesh sibling). Carries ``stratification_metric`` /
        ``stratification_dt`` / ``u_surface`` / ``u_bottom`` / ``front_speed_mps`` /
        ``flow_mode`` (narrate these typed numbers only - invariant 1) + a
        ``fallback_note`` (3D-screening honesty floor). On failure: dict with
        ``status="error"`` + ``error_code`` + ``error_message``.
    """
    coerced_bbox: tuple[float, float, float, float] | None = None
    if bbox is not None:
        cb = coerce_bbox_value(bbox)
        if cb is None:
            if isinstance(bbox, str) and any(c.isalpha() for c in bbox) \
                    and not (location and str(location).strip()):
                location, bbox = bbox, None
            else:
                return {
                    "status": "error",
                    "error_code": "TELEMAC3D_PARAMS_INVALID",
                    "error_message": f"invalid bbox (need 4 numbers): {bbox!r}",
                }
        else:
            coerced_bbox = tuple(cb)  # type: ignore[assignment]

    mode = _classify_mode(location, flow_mode)
    has_loc = bool(location and str(location).strip())
    # salt_wedge is idealized-only (analytic lock-exchange V&V) -> a location/bbox is
    # optional; the other two modes need an AOI (real lake OR an explicit basin).
    if mode != "salt_wedge" and not has_loc and coerced_bbox is None:
        return {
            "status": "error",
            "error_code": "TELEMAC3D_PARAMS_INCOMPLETE",
            "error_message": (
                "telemac3d_stratified_flow needs a `location` (geocoded lake/basin) "
                "or an explicit `bbox` AOI for the stratification / wind_circulation "
                "modes (the salt_wedge mode is an idealized analytic case and needs "
                "neither)."
            ),
        }
    if has_loc and coerced_bbox is not None:
        coerced_bbox = None  # location wins (an LLM-invented bbox is not truth)

    try:
        wind_speed_mps = max(0.0, min(40.0, float(wind_speed_mps)))
    except (TypeError, ValueError):
        wind_speed_mps = DEFAULT_WIND_MPS
    try:
        wind_direction_deg = float(wind_direction_deg) % 360.0
    except (TypeError, ValueError):
        wind_direction_deg = DEFAULT_WIND_DIR_FROM_DEG
    try:
        sim_duration_hours = max(1.0, min(24.0, float(sim_duration_hours)))
    except (TypeError, ValueError):
        sim_duration_hours = DEFAULT_DURATION_HOURS
    try:
        nplan = int(max(5, min(30, int(nplan))))
    except (TypeError, ValueError):
        nplan = DEFAULT_NPLAN
    if target_resolution_m is not None:
        try:
            target_resolution_m = max(100.0, float(target_resolution_m))
        except (TypeError, ValueError):
            target_resolution_m = None

    logger.info(
        "telemac3d_stratified_flow location=%r bbox=%s mode=%s wind=%.1f m/s "
        "nplan=%d bathy=%s res=%s",
        location, coerced_bbox, mode, wind_speed_mps, nplan, bathy_source,
        target_resolution_m,
    )

    try:
        layer = await model_telemac3d_stratified_flow(
            location=location if has_loc else None,
            bbox=coerced_bbox,
            flow_mode=mode,
            wind_speed_mps=wind_speed_mps,
            wind_direction_deg=wind_direction_deg,
            warm_temp_c=float(warm_temp_c),
            cold_temp_c=float(cold_temp_c),
            thermocline_depth_m=float(thermocline_depth_m),
            non_hydrostatic=bool(non_hydrostatic),
            nplan=nplan,
            target_resolution_m=target_resolution_m,
            sim_duration_hours=sim_duration_hours,
            bathy_source=str(bathy_source or "auto"),
            compute_class=compute_class,
            input_mode=input_mode,
        )
        logger.info(
            "telemac3d_stratified_flow complete layer_id=%s mode=%s metric=%.4g "
            "dt=%s u_surf=%s u_bot=%s uri=%s",
            layer.layer_id, layer.flow_mode, layer.stratification_metric,
            layer.stratification_dt, layer.u_surface, layer.u_bottom, layer.uri,
        )
        return layer
    except asyncio.CancelledError:
        raise
    except (Telemac3dStratifiedError, PostprocessTelemacError) as exc:
        logger.warning("telemac3d_stratified_flow failed: %s (%s)",
                       getattr(exc, "error_code", "?"), exc)
        return {
            "status": "error",
            "error_code": getattr(exc, "error_code", "TELEMAC3D_RUN_FAILED"),
            "error_message": str(exc),
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("telemac3d_stratified_flow unexpected failure")
        return {
            "status": "error",
            "error_code": "TELEMAC3D_INTERNAL_ERROR",
            "error_message": str(exc),
        }


# --------------------------------------------------------------------------- #
# The composer.
# --------------------------------------------------------------------------- #
def _bbox_center(bbox) -> tuple[float, float]:
    return (0.5 * (bbox[0] + bbox[2]), 0.5 * (bbox[1] + bbox[3]))


def _stage_stratified_manifest(stratified: dict[str, Any], run_tag: str) -> str:
    """Write the telemac3d ``stratified`` worker manifest to the cache bucket."""
    from trid3nt_server.workflows.solver.solver import _get_s3_client

    cache_bucket = (os.environ.get("TRID3NT_CACHE_BUCKET") or "").strip()
    if not cache_bucket:
        raise Telemac3dStratifiedError(
            "TELEMAC3D_STAGING_FAILED",
            "TRID3NT_CACHE_BUCKET must be set to stage the TELEMAC-3D manifest.")
    manifest = {
        "stratified": stratified,
        "run_id": run_tag,
        "inputs": [],
        "telemac_args": [],
        "outputs": [
            "t3d_surface.slf", "t3d_bottom.slf", "res3d_t3d.slf",
            "geo_t3d.slf", "bc_t3d.cli", "full_listing.log",
            "telemac_metrics.json",
        ],
    }
    key = f"telemac3d/{run_tag}/manifest.json"
    _get_s3_client().put_object(
        Bucket=cache_bucket, Key=key,
        Body=json.dumps(manifest, indent=2).encode("utf-8"),
        ContentType="application/json")
    return f"s3://{cache_bucket}/{key}"


def _download_stratified_result(run_id: str) -> tuple[str, str, dict[str, Any]]:
    """Download ``t3d_surface.slf`` + ``t3d_bottom.slf`` + telemac_metrics.json.
    Returns (surface_path, bottom_path, metrics)."""
    from trid3nt_server.workflows.solver.solver import (
        _get_runs_bucket,
        _get_s3_client,
    )

    runs_bucket = _get_runs_bucket()
    s3 = _get_s3_client()
    metrics: dict[str, Any] = {}
    try:
        obj = s3.get_object(Bucket=runs_bucket, Key=f"{run_id}/telemac_metrics.json")
        loaded = json.loads(obj["Body"].read().decode("utf-8"))
        if isinstance(loaded, dict):
            metrics = loaded
    except Exception as exc:  # noqa: BLE001
        logger.warning("telemac3d: metrics read failed for run %s: %s", run_id, exc)
    tmp_dir = tempfile.mkdtemp(prefix=f"telemac3d-{run_id}-")
    paths = {}
    for name in ("t3d_surface.slf", "t3d_bottom.slf"):
        p = str(Path(tmp_dir) / name)
        try:
            resp = s3.get_object(Bucket=runs_bucket, Key=f"{run_id}/{name}")
            with open(p, "wb") as fh:
                fh.write(resp["Body"].read())
        except Exception as exc:  # noqa: BLE001
            raise Telemac3dStratifiedError(
                "TELEMAC3D_OUTPUT_MISSING",
                f"TELEMAC-3D run {run_id} completed but s3://{runs_bucket}/{run_id}/"
                f"{name} was not downloadable: {exc}") from exc
        paths[name] = p
    return paths["t3d_surface.slf"], paths["t3d_bottom.slf"], metrics


async def model_telemac3d_stratified_flow(
    *,
    location: str | None,
    bbox: tuple[float, float, float, float] | None,
    flow_mode: str,
    wind_speed_mps: float,
    wind_direction_deg: float,
    warm_temp_c: float,
    cold_temp_c: float,
    thermocline_depth_m: float,
    non_hydrostatic: bool,
    nplan: int,
    target_resolution_m: float | None,
    sim_duration_hours: float,
    bathy_source: str,
    compute_class: str = "medium",
    input_mode: str | None = None,
    pipeline_emitter: Any = None,
) -> Telemac3dLayerURI:
    """Compose place/AOI -> TELEMAC-3D field -> published surface + bottom layers.

    A real Great Lakes AOI (stratification / wind_circulation) -> NOAA lake-datum
    bathymetry; salt_wedge + non-lake AOIs -> an idealized closed basin (labeled).
    Stages the ``stratified`` manifest, dispatches the generic run_solver seam
    (solver=telemac3d_strat), downloads the surface + bottom layers, and
    postprocesses them to two 4326 (or local-frame) COGs.
    """
    from trid3nt_server.emission.pipeline_emitter import (
        begin_substeps,
        current_emitter,
        mint_dispatch_and_sim_cards,
        route_sim_terminal,
        substep,
    )
    from trid3nt_server.gates.input_review import gate_input_review
    from trid3nt_server.tools.publish_layer.publish_layer import publish_layer
    from trid3nt_server.workflows.solver.solver import (
        EmitterBinding,
        run_solver,
        set_emitter_binding,
        wait_for_completion,
    )
    from trid3nt_server.workflows.shared.solve_progress import (
        drive_live_solve_progress,
    )

    emitter = pipeline_emitter or current_emitter()

    _planned = 3  # run_solver + postprocess + publish
    if location:
        _planned += 1  # geocode
    begin_substeps(current_emitter(), _planned)

    center_lon = center_lat = None
    location_name = location or ("lock-exchange" if flow_mode == "salt_wedge" else "AOI")
    if location:
        geocode_fn = TOOL_REGISTRY["geocode_location"].fn
        async with substep(current_emitter(), "geocode_location"):
            geo = await asyncio.to_thread(geocode_fn, location)
        center_lon = _geo_field(geo, ("lon", "longitude", "x"))
        center_lat = _geo_field(geo, ("lat", "latitude", "y"))
        if center_lon is None or center_lat is None:
            raise Telemac3dStratifiedError(
                "TELEMAC3D_GEOCODE_FAILED",
                f"could not geocode {location!r} to a lake/basin AOI.")
    elif bbox is not None:
        center_lon, center_lat = _bbox_center(bbox)

    src = str(bathy_source or "auto").lower()
    lake = (_great_lake_for(float(center_lon), float(center_lat))
            if (center_lon is not None and center_lat is not None) else None)
    # salt_wedge stays idealized (a real estuary needs a tidal liquid boundary).
    real = (flow_mode != "salt_wedge") and (
        src == "noaa_greatlakes" or (src == "auto" and lake is not None))

    if real:
        if bbox is not None:
            aoi = tuple(float(v) for v in bbox)
        else:
            h = 0.35
            aoi = (round(center_lon - h, 4), round(center_lat - 0.25, 4),
                   round(center_lon + h, 4), round(center_lat + 0.25, 4))
        bathy_label = f"real NOAA Great Lakes lake-datum bathymetry ({lake or 'AOI'})"
    else:
        aoi = None
        if flow_mode == "salt_wedge":
            bathy_label = ("idealized lock-exchange channel (analytic Benjamin "
                           "gravity-current V&V; no real estuary bathymetry)")
        else:
            bathy_label = ("idealized closed basin (no real bathymetry fetched for "
                           "this AOI; geography-free verification physics)")

    res_m = float(target_resolution_m) if target_resolution_m is not None else (
        DEFAULT_REAL_RES_M if real else DEFAULT_IDEALIZED_RES_M)
    res_default = target_resolution_m is None

    # --- LOUD labeled defaults (no met-forcing fetcher): forcing is reviewable --- #
    calm = wind_speed_mps <= 0.0
    _review_entries = [
        SyntheticInput(
            param="wind_speed_mps", value=round(wind_speed_mps, 1), units="m/s",
            basis="default_demo", consequence="physics",
            note=("calm (thermocline persists / no wind circulation)" if calm
                  else "prescribed steady wind (mixes the column / drives the gyre)")),
        SyntheticInput(
            param="bathy_source", value="noaa_greatlakes" if real else "idealized",
            basis="fetched" if real else "default_demo", consequence="physics", note=bathy_label),
        SyntheticInput(
            param="target_resolution_m", value=round(res_m, 0), units="m",
            basis="default_demo" if res_default else "user", consequence="numerical",
            note="horizontal grid node spacing"),
    ]
    if flow_mode == "stratification":
        _review_entries.append(SyntheticInput(
            param="thermocline", value=f"{warm_temp_c:g}C/{cold_temp_c:g}C",
            units="C", basis="default_demo", consequence="physics",
            note=(f"prescribed warm epilimnion over cold hypolimnion, thermocline "
                  f"at {thermocline_depth_m:g} m (no met-forcing fetcher)")))
    _review = await gate_input_review(
        tool_name="telemac3d_stratified_flow", mode=input_mode,
        entries=_review_entries, params={"wind_speed_mps": float(wind_speed_mps)})
    if _review.cancelled:
        raise Telemac3dStratifiedError("USER_INPUT_CANCELLED",
                                       f"telemac3d_stratified_flow {_review.cancel_reason}")
    wind_speed_mps = float(_review.params.get("wind_speed_mps", wind_speed_mps))

    # --- Stage the stratified manifest ---------------------------------------- #
    stratified: dict[str, Any] = {
        "name": _slug(location_name),
        "flow_mode": flow_mode,
        "bathy_source": "noaa_greatlakes" if real else "idealized",
        "wind_speed_mps": float(wind_speed_mps),
        "wind_dir_from_deg": float(wind_direction_deg),
        "warm_temp_c": float(warm_temp_c),
        "cold_temp_c": float(cold_temp_c),
        "thermocline_depth_m": float(thermocline_depth_m),
        "non_hydrostatic": bool(non_hydrostatic),
        "nplan": int(nplan),
        "target_resolution_m": float(res_m),
        "duration_hours": float(sim_duration_hours),
    }
    if real:
        stratified["bbox"] = [round(v, 4) for v in aoi]
    run_tag = new_ulid()
    manifest_uri = await asyncio.to_thread(_stage_stratified_manifest, stratified, run_tag)
    logger.info("model_telemac3d_stratified_flow staged manifest run_tag=%s mode=%s "
                "bathy=%s -> %s", run_tag, flow_mode, stratified["bathy_source"],
                manifest_uri)

    # --- Dispatch to the solver ----------------------------------------------- #
    handle = run_solver(
        solver=TELEMAC3D_SOLVER_NAME, model_setup_uri=manifest_uri,
        compute_class=compute_class)
    run_id = handle.run_id
    _sim_step_id = await mint_dispatch_and_sim_cards(
        emitter=emitter, solver=TELEMAC3D_SOLVER_NAME, handle=handle,
        compute_class=compute_class)
    if emitter is not None and _sim_step_id is not None:
        set_emitter_binding(EmitterBinding(emitter=emitter, step_id=_sim_step_id))
    _progress_task = asyncio.ensure_future(
        drive_live_solve_progress(
            emitter=current_emitter(), run_id=run_id, solver=TELEMAC3D_SOLVER_NAME,
            grid_resolution_m=res_m, active_cell_count=None, vcpus=None,
            eta_seconds=None))
    run_result = None
    try:
        async with substep(emitter, "run_solver"):
            try:
                run_result = await wait_for_completion(handle, timeout_s=3600.0)
            finally:
                _progress_task.cancel()
                try:
                    await _progress_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
                set_emitter_binding(None)
    finally:
        await route_sim_terminal(emitter, _sim_step_id, run_result=run_result)

    if run_result is None or run_result.status != "complete":
        raise Telemac3dStratifiedError(
            "TELEMAC3D_RUN_FAILED",
            f"TELEMAC-3D solve did not complete "
            f"(status={getattr(run_result, 'status', None)}, "
            f"error_code={getattr(run_result, 'error_code', None)}): "
            f"{getattr(run_result, 'error_message', '') or ''}")

    # --- Download + postprocess to the surface + bottom COGs ------------------ #
    batch_run_id = getattr(run_result, "run_id", None) or run_id
    surface_slf, bottom_slf, metrics = await asyncio.to_thread(
        _download_stratified_result, batch_run_id)
    utm_epsg = metrics.get("utm_epsg")   # None on the idealized path
    reach_name = _slug(location_name)
    try:
        async with substep(emitter, "postprocess_telemac3d"):
            layers, pmetrics = await asyncio.to_thread(
                postprocess_telemac3d, surface_slf, bottom_slf,
                run_id=batch_run_id,
                utm_epsg=int(utm_epsg) if utm_epsg is not None else None,
                worker_metrics=metrics, reach_name=reach_name,
                flow_mode=flow_mode)
    finally:
        for p in (surface_slf, bottom_slf):
            try:
                Path(p).unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
    if not layers:
        raise Telemac3dStratifiedError("TELEMAC3D_NO_LAYERS",
                                       "postprocess_telemac3d produced no layer.")
    surface_raw = layers[0]
    bottom_raw = layers[1] if len(layers) > 1 else None

    from trid3nt_server.tools.publish_layer.publish_layer import PublishLayerError

    # publish + emit the BOTTOM companion (the surface-vs-bottom contrast is the
    # discriminant), then publish + RETURN the surface layer.
    async with substep(emitter, "publish_layer"):
        if bottom_raw is not None and emitter is not None \
                and bottom_raw.uri.startswith(("s3://", "gs://")):
            try:
                b_uri = await asyncio.to_thread(
                    publish_layer, layer_uri=bottom_raw.uri,
                    layer_id=bottom_raw.layer_id,
                    style_preset=bottom_raw.style_preset
                    or TELEMAC3D_STRATIFICATION_STYLE_PRESET)
                bottom_pub = bottom_raw.model_copy(update={"uri": b_uri})
            except PublishLayerError as exc:
                logger.warning("telemac3d bottom publish failed (%s) - "
                               "emitting the unpublished COG", exc)
                bottom_pub = bottom_raw
            try:
                from trid3nt_server.emission.layer_uri_emit import publish_input_layer  # noqa: WPS433
                emitted = await publish_input_layer(emitter, bottom_pub)
                logger.info("telemac3d bottom layer emitted=%s id=%s", emitted,
                            bottom_pub.layer_id)
            except Exception as exc:  # noqa: BLE001 -- a missing bottom never voids surface
                logger.warning("telemac3d bottom emit failed (%s)", exc)

        published = surface_raw
        if surface_raw.uri.startswith(("s3://", "gs://")):
            try:
                pub_uri = await asyncio.to_thread(
                    publish_layer,
                    layer_uri=surface_raw.uri,
                    layer_id=surface_raw.layer_id,
                    style_preset=surface_raw.style_preset
                    or TELEMAC3D_STRATIFICATION_STYLE_PRESET,
                )
                published = surface_raw.model_copy(update={"uri": pub_uri})
            except PublishLayerError as exc:
                logger.warning("telemac3d surface publish failed (%s) - "
                               "unpublished COG", exc)
    return published


def _geo_field(geo: Any, keys: tuple[str, ...]) -> float | None:
    if geo is None:
        return None
    for k in keys:
        v = getattr(geo, k, None)
        if v is None and isinstance(geo, dict):
            v = geo.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    for sub in ("center", "geometry", "location", "result"):
        nested = getattr(geo, sub, None) or (geo.get(sub) if isinstance(geo, dict) else None)
        if nested is not None:
            f = _geo_field(nested, keys)
            if f is not None:
                return f
    return None


def _slug(name: str) -> str:
    s = "".join(c if c.isalnum() else "_" for c in str(name or "lake").lower())
    return "_".join(p for p in s.split("_") if p)[:48] or "stratified_flow"
