"""Engine template ``tomawac_wave_field`` - TOMAWAC spectral (phase-averaged)
wave engine.

The LLM-facing exposure of TELEMAC's TOMAWAC third-generation wave-action solver:
the refinement-grade complement to SFINCS/SnapWave coastal screening (fidelity
ladder). ONE question-class tool with FOUR modes (the board's four distinct
TOMAWAC question classes):

  * ``fetch_growth``    - fetch-limited wind-wave growth (Hs grows downwind across
                          the fetch; the discriminating proof-norm-#9 pair is the
                          upwind vs downwind shore under the SAME storm).
  * ``shoaling``        - an offshore swell shoals (Hs rises) then depth-breaks up
                          a beach.
  * ``bottom_friction`` - a shallow shelf dissipates wave energy (Hs lower with
                          friction ON).
  * ``wave_current``    - an opposing current amplifies Hs, a following current
                          damps it.

Two bathymetry paths (see the worker ``tomawac_build``):
  * ``noaa_greatlakes`` - a REAL US Great Lake AOI, bed sampled from the NOAA NGDC
    lake-datum bathymetry (Superior/Michigan/Huron/Erie/Ontario).
  * ``idealized``       - the geography-free rectangular basin the sandbox proved
    (replicates the physics of the official TOMAWAC verification cases; clears the
    citations law like the GWE analytic V&V).

Structural sibling of ``telemac_river_dye`` (same LOCAL-DOCKER solve seam, same
run_solver dispatch, same publish_layer render path): a registered engine TEMPLATE
tagged ``engine="telemac", tier="template"``. Determinism boundary (invariant 1):
every wave number the agent narrates comes from the typed
``TelemacWaveLayerURI.hs_max_m`` / ``.hs_upwind_m`` / ``.hs_downwind_m`` fields the
postprocess computed - never free-generated. The ``fallback_note`` carries the
honesty floor (spectral-screening, not a calibrated hindcast).
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
    TELEMAC_WAVE_STYLE_PRESET,
    TelemacWaveLayerURI,
)
from trid3nt_contracts.tool_registry import AtomicToolMetadata, ResolutionSpec

from trid3nt_server.tools import TOOL_REGISTRY, register_tool
from trid3nt_server.tools.tool_arg_normalizer import coerce_bbox_value
from trid3nt_server.workflows.telemac._template_card import TemplateCard
from trid3nt_server.workflows.telemac.postprocess_telemac import (
    PostprocessTelemacError,
    postprocess_tomawac,
)
from trid3nt_server.workflows.telemac.run_telemac import TOMAWAC_SOLVER_NAME

logger = logging.getLogger("trid3nt_server.workflows.telemac.wave_field")

__all__ = ["tomawac_wave_field", "model_tomawac_wave_field", "TomawacWaveError"]


class TomawacWaveError(RuntimeError):
    """Raised when the TOMAWAC wave chain fails fatally before producing a layer."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


#: The four question classes this tool covers (one tool, four modes).
_WAVE_MODES = ("fetch_growth", "shoaling", "bottom_friction", "wave_current")

#: Rough lon/lat bboxes of the five Great Lakes open water (auto real-bathy gate).
_GREAT_LAKES: dict[str, tuple[float, float, float, float]] = {
    "superior": (-92.2, 46.4, -84.3, 49.1),
    "michigan": (-88.1, 41.6, -84.7, 46.1),
    "huron": (-84.8, 43.0, -79.7, 46.3),
    "erie": (-83.5, 41.3, -78.8, 42.9),
    "ontario": (-79.9, 43.2, -76.0, 44.3),
}

#: LOUD labeled demo defaults (no wave-forcing fetcher exists yet): a prescribed
#: steady storm wind + swell. These are narrated demo defaults, never observations.
DEFAULT_WIND_MPS = 20.0
DEFAULT_WIND_DIR_FROM_DEG = 270.0     # westerly -> waves grow toward the east shore
DEFAULT_BOUNDARY_HS_M = 1.5
DEFAULT_BOUNDARY_PERIOD_S = 10.0
DEFAULT_DURATION_HOURS = 4.0
DEFAULT_REAL_RES_M = 2000.0
DEFAULT_IDEALIZED_RES_M = 1500.0


def _great_lake_for(lon: float, lat: float) -> str | None:
    for name, (x0, y0, x1, y1) in _GREAT_LAKES.items():
        if x0 <= lon <= x1 and y0 <= lat <= y1:
            return name
    return None


def _classify_mode(text: str | None, explicit: str | None) -> str:
    """Pick the wave question class from an explicit arg or prompt keywords."""
    if explicit and str(explicit).strip().lower() in _WAVE_MODES:
        return str(explicit).strip().lower()
    t = (text or "").lower()
    if any(w in t for w in ("shoal", "breaking", "nearshore", "beach", "surf")):
        return "shoaling"
    if any(w in t for w in ("current", "opposing", "following", "tidal jet", "ebb")):
        return "wave_current"
    if any(w in t for w in ("bottom friction", "friction", "dissipat", "shelf")):
        return "bottom_friction"
    return "fetch_growth"


#: DECLARED target_resolution_m range. Grid floor GRID_H_FLOOR_M (150 m
#: in the worker); a large lake is coarsened under the node budget (self-labeled).
_TOMAWAC_RES_SPEC = ResolutionSpec(
    param="target_resolution_m",
    unit="m",
    min_value=150.0,
    native_hint="NOAA Great Lakes lake-datum bathymetry (~90 m) / idealized grid",
    constraint_source="solver",
    rationale=(
        "target grid node spacing; GRID_H_FLOOR_M=150 m is the finest the wave "
        "grid authors, a large lake is coarsened under the GRID_NODE_CAP budget "
        "(self-labeled); a spectral screening field gains nothing finer"
    ),
)

_TOMAWAC_METADATA = AtomicToolMetadata(
    name="tomawac_wave_field",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="telemac",
    tier="template",
    resolution_specs=(_TOMAWAC_RES_SPEC,),
)

TEMPLATE_CARD = TemplateCard(
    question=(
        "the SPECTRAL WAVE FIELD (significant wave height Hs) a storm builds over "
        "a lake or coast - fetch-limited wind-wave GROWTH across a lake, swell "
        "SHOALING + depth-breaking up a beach, wave-CURRENT interaction "
        "(opposing/following), or bottom-friction dissipation on a shallow shelf; "
        "TOMAWAC third-generation wave-action solver (refinement-grade, the "
        "complement to SFINCS/SnapWave coastal screening)"
    ),
    required_inputs=["location OR bbox (a lake / coastal AOI)"],
    knobs=(
        "wave_mode (fetch_growth / shoaling / bottom_friction / wave_current), "
        "wind_speed_mps, wind_direction_deg, boundary_hs_m, boundary_period_s, "
        "current_speed_mps, bottom_friction, target_resolution_m, "
        "sim_duration_hours, bathy_source (noaa_greatlakes / idealized)"
    ),
)


@register_tool(
    _TOMAWAC_METADATA,
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def tomawac_wave_field(
    location: str | None = None,
    bbox: tuple[float, float, float, float] | list[float] | str | None = None,
    wave_mode: str | None = None,
    wind_speed_mps: float = DEFAULT_WIND_MPS,
    wind_direction_deg: float = DEFAULT_WIND_DIR_FROM_DEG,
    boundary_hs_m: float = DEFAULT_BOUNDARY_HS_M,
    boundary_period_s: float = DEFAULT_BOUNDARY_PERIOD_S,
    current_speed_mps: float = -2.5,
    bottom_friction: bool | None = None,
    target_resolution_m: float | None = None,
    sim_duration_hours: float = DEFAULT_DURATION_HOURS,
    bathy_source: str = "auto",
    compute_class: str = "medium",
    input_mode: str | None = None,
    **_extra_ignored: Any,
) -> TelemacWaveLayerURI | dict[str, Any]:
    """The SPECTRAL WAVE FIELD (significant wave height Hs) a storm builds over a lake or coast.

    Fidelity: TOMAWAC third-generation spectral (phase-averaged) wave-action
    solver - wind-wave generation (WAM cycle 4), shoaling/breaking, wave-current
    interaction, bottom friction. Refinement-grade wave physics (the complement to
    SFINCS/SnapWave coastal SCREENING). A planning-grade demo driven by a
    PRESCRIBED steady storm wind / boundary swell (no wave-forcing fetcher yet),
    not a calibrated hindcast.

    THE tool for "how big do the waves get", "significant wave height", "wave
    field / sea state", "fetch-limited wave growth across the lake", "how far do
    waves grow downwind", "swell shoaling / breaking at the beach", "wave-current
    interaction", "wave energy dissipation on a shallow shelf". Answers FOUR
    question classes via ``wave_mode``:

      - ``fetch_growth`` (default) - wind-wave growth across the fetch; Hs grows
        from the upwind shore to the downwind shore under the same storm.
      - ``shoaling`` - an offshore swell steepens (Hs rises) then depth-breaks up a
        beach.
      - ``bottom_friction`` - a shallow shelf dissipates wave energy (Hs lower).
      - ``wave_current`` - an opposing current amplifies Hs, a following one damps.

    Do NOT use this for: inundation DEPTH (``sfincs_flood`` / ``geoclaw_inundation``);
    a river dye/contaminant plume (``telemac_river_dye``); storm-surge water level
    (SFINCS). This tool returns a WAVE-HEIGHT field, not a water level or depth.

    Params:
        location: a lake / coastal place near the AOI (e.g. "Lake Superior",
            "Marquette, Michigan"). Supply this OR ``bbox`` - geocoded, never
            hand-typed coords.
        bbox: OPTIONAL explicit AOI ``(min_lon, min_lat, max_lon, max_lat)``
            EPSG:4326 (open water inside a lake for the real-bathy path).
        wave_mode: OPTIONAL question class - ``fetch_growth`` / ``shoaling`` /
            ``bottom_friction`` / ``wave_current``. Unset -> inferred from the
            prompt (defaults to fetch_growth).
        wind_speed_mps: sustained storm wind speed (m/s). Default 20 (a LOUD demo
            default - no wave-forcing fetcher exists). Clamped [0, 60].
        wind_direction_deg: meteorological direction the wind blows FROM (compass,
            0=N/90=E/180=S/270=W). Default 270 (westerly). Only for wind modes.
        boundary_hs_m: incident swell significant wave height at the open boundary
            (m), for shoaling / wave_current. Default 1.5.
        boundary_period_s: incident swell peak period (s). Default 10.
        current_speed_mps: wave_current mode - the current magnitude ramped across
            the domain (m/s). NEGATIVE opposes the swell (amplifies Hs), POSITIVE
            follows it. Default -2.5 (opposing).
        bottom_friction: OPTIONAL - force bottom-friction dissipation ON. Unset
            auto-arms it for the ``bottom_friction`` mode.
        target_resolution_m: OPTIONAL grid node spacing (m). Unset -> a labeled
            default (real lake 2000 m, idealized 1500 m). Floored at 150 m and
            coarsened under the node budget (self-labeled).
        sim_duration_hours: simulated storm duration (h). Default 4. Clamped
            [1, 24].
        bathy_source: ``"auto"`` (default - a Great Lakes AOI uses real NOAA
            lake-datum bathymetry, else an idealized basin labeled as such) |
            ``"noaa_greatlakes"`` | ``"idealized"``.
        compute_class: compute class. Default ``"medium"``.
        input_mode: ``"user_gated"`` reviews the resolved forcing before the solve;
            ``"auto"`` (default) proceeds labeled.

    Returns:
        On success: ``TelemacWaveLayerURI`` (``LayerURI`` subtype) - the emitter
        loads the Hs COG onto the map and the client animates the TOMAWAC SELAFIN
        mesh sibling. Carries ``hs_max_m`` / ``hs_mean_m`` / ``hs_upwind_m`` /
        ``hs_downwind_m`` / ``wave_mode`` (narrate these typed numbers only -
        invariant 1) + a ``fallback_note`` (spectral-screening honesty floor).
        On failure: dict with ``status="error"`` + ``error_code`` +
        ``error_message``.
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
                    "error_code": "TOMAWAC_PARAMS_INVALID",
                    "error_message": f"invalid bbox (need 4 numbers): {bbox!r}",
                }
        else:
            coerced_bbox = tuple(cb)  # type: ignore[assignment]

    has_loc = bool(location and str(location).strip())
    if not has_loc and coerced_bbox is None:
        return {
            "status": "error",
            "error_code": "TOMAWAC_PARAMS_INCOMPLETE",
            "error_message": (
                "tomawac_wave_field needs a `location` (geocoded lake/coast) or an "
                "explicit `bbox` AOI."
            ),
        }
    if has_loc and coerced_bbox is not None:
        # location wins when both present (an LLM-invented bbox is not ground truth)
        coerced_bbox = None

    mode = _classify_mode(location, wave_mode)
    try:
        wind_speed_mps = max(0.0, min(60.0, float(wind_speed_mps)))
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
    if target_resolution_m is not None:
        try:
            target_resolution_m = max(150.0, float(target_resolution_m))
        except (TypeError, ValueError):
            target_resolution_m = None

    logger.info(
        "tomawac_wave_field location=%r bbox=%s mode=%s wind=%.1f m/s from %.0f "
        "bathy=%s res=%s",
        location, coerced_bbox, mode, wind_speed_mps, wind_direction_deg,
        bathy_source, target_resolution_m,
    )

    try:
        layer = await model_tomawac_wave_field(
            location=location if has_loc else None,
            bbox=coerced_bbox,
            wave_mode=mode,
            wind_speed_mps=wind_speed_mps,
            wind_direction_deg=wind_direction_deg,
            boundary_hs_m=float(boundary_hs_m),
            boundary_period_s=float(boundary_period_s),
            current_speed_mps=float(current_speed_mps),
            bottom_friction=bottom_friction,
            target_resolution_m=target_resolution_m,
            sim_duration_hours=sim_duration_hours,
            bathy_source=str(bathy_source or "auto"),
            compute_class=compute_class,
            input_mode=input_mode,
        )
        logger.info(
            "tomawac_wave_field complete layer_id=%s hs_max=%.4g upwind=%s "
            "downwind=%s uri=%s",
            layer.layer_id, layer.hs_max_m, layer.hs_upwind_m,
            layer.hs_downwind_m, layer.uri,
        )
        return layer
    except asyncio.CancelledError:
        raise
    except (TomawacWaveError, PostprocessTelemacError) as exc:
        logger.warning("tomawac_wave_field failed: %s (%s)",
                       getattr(exc, "error_code", "?"), exc)
        return {
            "status": "error",
            "error_code": getattr(exc, "error_code", "TOMAWAC_RUN_FAILED"),
            "error_message": str(exc),
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("tomawac_wave_field unexpected failure")
        return {
            "status": "error",
            "error_code": "TOMAWAC_INTERNAL_ERROR",
            "error_message": str(exc),
        }


# --------------------------------------------------------------------------- #
# The composer.
# --------------------------------------------------------------------------- #
def _bbox_center(bbox) -> tuple[float, float]:
    return (0.5 * (bbox[0] + bbox[2]), 0.5 * (bbox[1] + bbox[3]))


def _stage_wave_manifest(wave: dict[str, Any], run_tag: str) -> str:
    """Write the tomawac ``wave`` worker manifest to the cache bucket; return uri."""
    from trid3nt_server.workflows.solver.solver import _get_s3_client

    cache_bucket = (os.environ.get("TRID3NT_CACHE_BUCKET") or "").strip()
    if not cache_bucket:
        raise TomawacWaveError(
            "TOMAWAC_STAGING_FAILED",
            "TRID3NT_CACHE_BUCKET must be set to stage the TOMAWAC manifest.")
    manifest = {
        "wave": wave,
        "run_id": run_tag,
        "inputs": [],
        "telemac_args": [],
        "outputs": [
            "res_wave.slf", "geo_wave.slf", "bc_wave.cli", "tom_wave.cas",
            "full_listing.log", "tomawac_wave.log", "bed_bathymetry.tif",
            "telemac_metrics.json",
        ],
    }
    key = f"tomawac/{run_tag}/manifest.json"
    _get_s3_client().put_object(
        Bucket=cache_bucket, Key=key,
        Body=json.dumps(manifest, indent=2).encode("utf-8"),
        ContentType="application/json")
    return f"s3://{cache_bucket}/{key}"


def _download_wave_result(run_id: str) -> tuple[str, dict[str, Any]]:
    """Download ``res_wave.slf`` + read telemac_metrics.json. Returns (path, metrics)."""
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
        logger.warning("tomawac: metrics read failed for run %s: %s", run_id, exc)
    slf_key = f"{run_id}/res_wave.slf"
    tmp_dir = tempfile.mkdtemp(prefix=f"tomawac-{run_id}-")
    slf_path = str(Path(tmp_dir) / "res_wave.slf")
    try:
        resp = s3.get_object(Bucket=runs_bucket, Key=slf_key)
        with open(slf_path, "wb") as fh:
            fh.write(resp["Body"].read())
    except Exception as exc:  # noqa: BLE001
        raise TomawacWaveError(
            "TOMAWAC_OUTPUT_MISSING",
            f"TOMAWAC run {run_id} completed but s3://{runs_bucket}/{slf_key} "
            f"was not downloadable: {exc}") from exc
    if metrics.get("utm_epsg") is None:
        raise TomawacWaveError(
            "TOMAWAC_OUTPUT_MISSING",
            f"TOMAWAC run {run_id} produced no utm_epsg; cannot georeference.")
    return slf_path, metrics


async def model_tomawac_wave_field(
    *,
    location: str | None,
    bbox: tuple[float, float, float, float] | None,
    wave_mode: str,
    wind_speed_mps: float,
    wind_direction_deg: float,
    boundary_hs_m: float,
    boundary_period_s: float,
    current_speed_mps: float,
    bottom_friction: bool | None,
    target_resolution_m: float | None,
    sim_duration_hours: float,
    bathy_source: str,
    compute_class: str = "medium",
    input_mode: str | None = None,
    pipeline_emitter: Any = None,
) -> TelemacWaveLayerURI:
    """Compose place/AOI -> TOMAWAC wave field -> published Hs layer.

    Real Great Lakes AOI -> NOAA lake-datum bathymetry; otherwise an idealized
    basin (labeled). Stages the ``wave`` manifest, dispatches the generic
    run_solver seam (solver=tomawac_wave), downloads the result SELAFIN, and
    postprocesses the final-frame Hs field to a 4326 COG.
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

    # --- Stage 1: resolve AOI + decide the bathymetry path -------------------- #
    _planned = 3  # run_solver + postprocess + publish
    if location:
        _planned += 1  # geocode
    begin_substeps(current_emitter(), _planned)

    center_lon = center_lat = None
    location_name = location or "AOI"
    if location:
        geocode_fn = TOOL_REGISTRY["geocode_location"].fn
        async with substep(current_emitter(), "geocode_location"):
            geo = await asyncio.to_thread(geocode_fn, location)
        # geocode returns a dict/obj with lon/lat; be defensive across shapes.
        center_lon = _geo_field(geo, ("lon", "longitude", "x"))
        center_lat = _geo_field(geo, ("lat", "latitude", "y"))
        if center_lon is None or center_lat is None:
            raise TomawacWaveError(
                "TOMAWAC_GEOCODE_FAILED",
                f"could not geocode {location!r} to a lake/coastal AOI.")
    else:
        center_lon, center_lat = _bbox_center(bbox)

    src = str(bathy_source or "auto").lower()
    lake = _great_lake_for(float(center_lon), float(center_lat))
    if src == "noaa_greatlakes" or (src == "auto" and lake is not None):
        real = True
    else:
        real = False

    # real-bathy AOI bbox: use the given bbox, else a ~0.9 deg box around the
    # centroid clipped to the lake open water.
    if real:
        if bbox is not None:
            aoi = tuple(float(v) for v in bbox)
        else:
            h = 0.7
            aoi = (round(center_lon - h, 4), round(center_lat - 0.4, 4),
                   round(center_lon + h, 4), round(center_lat + 0.4, 4))
        bathy_label = f"real NOAA Great Lakes lake-datum bathymetry ({lake or 'AOI'})"
    else:
        aoi = None
        bathy_label = ("idealized basin (no real bathymetry fetched for this AOI; "
                       "geography-free verification physics)")

    if bottom_friction is None:
        bottom_friction = (wave_mode == "bottom_friction")

    res_m = float(target_resolution_m) if target_resolution_m is not None else (
        DEFAULT_REAL_RES_M if real else DEFAULT_IDEALIZED_RES_M)
    res_default = target_resolution_m is None

    # --- LOUD labeled defaults (no wave-forcing fetcher): forcing is reviewable - #
    _review_entries = [
        SyntheticInput(
            param="wind_speed_mps", value=round(wind_speed_mps, 1), units="m/s",
            basis="default_demo", consequence="physics", note="prescribed steady storm wind (no wave-forcing fetcher)"),
        SyntheticInput(
            param="target_resolution_m", value=round(res_m, 0), units="m",
            basis="default_demo" if res_default else "user", consequence="numerical",
            note="wave grid node spacing"),
        SyntheticInput(
            param="bathy_source", value="noaa_greatlakes" if real else "idealized",
            basis="fetched" if real else "default_demo", consequence="physics", note=bathy_label),
    ]
    _review = await gate_input_review(
        tool_name="tomawac_wave_field", mode=input_mode,
        entries=_review_entries, params={"wind_speed_mps": float(wind_speed_mps)})
    if _review.cancelled:
        raise TomawacWaveError("USER_INPUT_CANCELLED",
                               f"tomawac_wave_field {_review.cancel_reason}")
    wind_speed_mps = float(_review.params.get("wind_speed_mps", wind_speed_mps))

    # --- Stage 2: stage the wave manifest ------------------------------------- #
    wave: dict[str, Any] = {
        "name": _slug(location_name),
        "wave_mode": wave_mode,
        "bathy_source": "noaa_greatlakes" if real else "idealized",
        "wind_speed_mps": float(wind_speed_mps),
        "wind_dir_from_deg": float(wind_direction_deg),
        "boundary_hs_m": float(boundary_hs_m),
        "boundary_fp_hz": round(1.0 / max(float(boundary_period_s), 1e-3), 5),
        "current_uc_mps": float(current_speed_mps),
        "bottom_friction": bool(bottom_friction),
        "target_resolution_m": float(res_m),
        "duration_hours": float(sim_duration_hours),
    }
    if real:
        wave["bbox"] = [round(v, 4) for v in aoi]
    run_tag = new_ulid()
    manifest_uri = await asyncio.to_thread(_stage_wave_manifest, wave, run_tag)
    logger.info("model_tomawac_wave_field staged manifest run_tag=%s mode=%s "
                "bathy=%s -> %s", run_tag, wave_mode, wave["bathy_source"], manifest_uri)

    # --- Stage 3: dispatch to the solver -------------------------------------- #
    handle = run_solver(
        solver=TOMAWAC_SOLVER_NAME, model_setup_uri=manifest_uri,
        compute_class=compute_class)
    run_id = handle.run_id
    _sim_step_id = await mint_dispatch_and_sim_cards(
        emitter=emitter, solver=TOMAWAC_SOLVER_NAME, handle=handle,
        compute_class=compute_class)
    if emitter is not None and _sim_step_id is not None:
        set_emitter_binding(EmitterBinding(emitter=emitter, step_id=_sim_step_id))
    _progress_task = asyncio.ensure_future(
        drive_live_solve_progress(
            emitter=current_emitter(), run_id=run_id, solver=TOMAWAC_SOLVER_NAME,
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
        raise TomawacWaveError(
            "TOMAWAC_RUN_FAILED",
            f"TOMAWAC wave solve did not complete "
            f"(status={getattr(run_result, 'status', None)}, "
            f"error_code={getattr(run_result, 'error_code', None)}): "
            f"{getattr(run_result, 'error_message', '') or ''}")

    # --- Stage 4: download + postprocess to the Hs COG ------------------------ #
    batch_run_id = getattr(run_result, "run_id", None) or run_id
    slf_path, metrics = await asyncio.to_thread(_download_wave_result, batch_run_id)
    utm_epsg = int(metrics["utm_epsg"])
    reach_name = _slug(location_name)
    try:
        async with substep(emitter, "postprocess_tomawac"):
            layers, pmetrics = await asyncio.to_thread(
                postprocess_tomawac, slf_path, run_id=batch_run_id,
                utm_epsg=utm_epsg, reach_name=reach_name, wave_mode=wave_mode)
    finally:
        try:
            Path(slf_path).unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
    if not layers:
        raise TomawacWaveError("TOMAWAC_NO_LAYERS",
                               "postprocess_tomawac produced no wave layer.")
    raw = layers[0]

    # fold the worker's discriminating shore pair + forcing onto the layer so the
    # agent narrates typed numbers (invariant 1).
    enriched = raw.model_copy(update={
        "hs_upwind_m": metrics.get("hs_upwind_m"),
        "hs_downwind_m": metrics.get("hs_downwind_m"),
        "peak_period_max_s": metrics.get("peak_period_max_s"),
        "wind_speed_mps": metrics.get("wind_speed_mps"),
        "mesh_size_m": metrics.get("dx_m"),
        "mesh_resolution_label": (
            f"{'real NOAA lake bathy' if real else 'idealized'} grid "
            f"{metrics.get('dx_m', res_m):g} m"
            + (" (coarsened under node budget)" if metrics.get("coarsened") else "")),
    })

    from trid3nt_server.tools.publish_layer.publish_layer import PublishLayerError

    async with substep(emitter, "publish_layer"):
        published = enriched
        if enriched.uri.startswith(("s3://", "gs://")):
            try:
                pub_uri = await asyncio.to_thread(
                    publish_layer,
                    layer_uri=enriched.uri,
                    layer_id=enriched.layer_id,
                    style_preset=enriched.style_preset or TELEMAC_WAVE_STYLE_PRESET,
                )
                published = enriched.model_copy(update={"uri": pub_uri})
            except PublishLayerError as exc:
                logger.warning("tomawac publish_layer failed (%s) - unpublished COG", exc)

    # in-worker bed input (S3): the NOAA lake-datum bed is sampled inside
    # the solver container (no agent-side router fetch), so the composer rides the
    # bed COG the worker recorded through publish_raster_input_cog. Best-effort;
    # only the real-bathy path writes one (metrics.bed_cog present).
    from trid3nt_server.workflows.telemac._bed_input import (
        surface_in_worker_bed_input,
    )
    await surface_in_worker_bed_input(
        emitter, run_metrics=metrics, run_id=batch_run_id,
        name=(f"Input: lake bed bathymetry ({reach_name}, "
              f"NOAA Great Lakes lake-datum, in-worker)"))
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
    # nested {'center': {...}} / {'geometry': {...}}
    for sub in ("center", "geometry", "location", "result"):
        nested = getattr(geo, sub, None) or (geo.get(sub) if isinstance(geo, dict) else None)
        if nested is not None:
            f = _geo_field(nested, keys)
            if f is not None:
                return f
    return None


def _slug(name: str) -> str:
    s = "".join(c if c.isalnum() else "_" for c in str(name or "wave").lower())
    return "_".join(p for p in s.split("_") if p)[:48] or "wave_field"
