"""Atomic tool ``run_swan_waves`` -- SWAN (Simulating WAves Nearshore) spectral
nearshore wave engine (Phase 1).

The LLM-facing exposure of the SWAN third-generation spectral wave engine. SWAN is
the ADDITIVE comparison engine: it runs STANDALONE over a coastal AOI and produces
its OWN engineering-grade wave field (significant wave height Hs, peak period Tp,
mean direction Dir) so a user can COMPARE SWAN against the existing SFINCS+SnapWave
output on the SAME case. ``run_swan_waves(...)`` takes the ``SwanRunArgs`` grid /
boundary fields, runs the deterministic fetch -> stage -> Batch-solve ->
postprocess chain (``workflows/model_wave_scenario.py``), and returns a
``WaveFieldLayerURI`` the emitter loads onto the map (it subclasses ``LayerURI`` so
the ``emit_tool_call`` ``add_loaded_layer`` gate fires).

This is the SWAN analogue of ``run_geoclaw_inundation`` (GeoClaw) /
``run_swmm_urban_flood`` (SWMM). Like those wrappers it declares
``cacheable=False`` + ``ttl_class="live-no-cache"`` +
``source_class="workflow_dispatch"`` (FR-DC-6 - workflow exposure surface; never
touches the cache shim). Confirmation before consequence (Invariant 9 - a solver
run) is enforced by the server confirmation hook around this tool.

SWAN is BATCH-ONLY (the GPL Fortran lives in the worker container image, never in
the agent venv), so this always dispatches to AWS Batch.

ROUTING GUIDANCE (the engine-spike crux, section 3): SWAN is the DEFENSIBLE
nearshore wave field (full 2D spectra, wind-sea growth, swell, engineering-grade
Hs/Tp/Dir for buoy validation / overtopping inputs / "show the incoming waves").
SFINCS+SnapWave (``run_model_flood_scenario``) is the FAST compound-flood setup
path (one combined solve). Route SWAN when the user wants a defensible wave field
to COMPARE; route SFINCS for fast inundation. SWAN does NOT replace SFINCS.

Determinism boundary (Invariant 1): every wave number the agent narrates comes
from the typed ``WaveFieldLayerURI.max_hs_m`` / ``.mean_tp_s`` / ``.mean_dir_deg``
/ ``.wave_area_km2`` fields the postprocess computed - never free-generated.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from trid3nt_contracts.swan_contracts import (
    SwanRunArgs,
    SwanWaveBoundary,
    WaveFieldLayerURI,
)
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tool_arg_normalizer import coerce_bbox_value
from trid3nt_server.workflows.model_wave_scenario import (
    SwanComposerError,
    model_wave_scenario,
)
from trid3nt_server.workflows.postprocess_swan import PostprocessSwanError
from trid3nt_server.workflows.run_swan import SwanWorkflowError

logger = logging.getLogger("trid3nt_server.tools.simulation.run_swan_tool")

__all__ = ["run_swan_waves", "RunSwanError"]


class RunSwanError(RuntimeError):
    """Raised when the SWAN chain fails fatally before producing a layer."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


# The strict boundary-side contract is Literal["N","S","E","W"], but the LLM
# routinely passes a free-text direction ("south", "from the south", "S ") --
# which fails SwanWaveBoundary validation and surfaces the recurring transient
# "failed: SWAN wave sim" card (it self-corrects to "S" on retry, but the demo
# flashes a red card first). Normalize any sane direction phrasing to a single
# cardinal up front so the first attempt succeeds.
_SIDE_WORD_TO_CARDINAL = {
    "N": "N", "NORTH": "N", "NORTHERN": "N", "NORTHWARD": "N",
    "S": "S", "SOUTH": "S", "SOUTHERN": "S", "SOUTHWARD": "S",
    "E": "E", "EAST": "E", "EASTERN": "E", "EASTWARD": "E",
    "W": "W", "WEST": "W", "WESTERN": "W", "WESTWARD": "W",
}


def _normalize_boundary_side(raw: Any) -> str | None:
    """Coerce a free-text boundary side to one of N/S/E/W (None if unparseable).

    Accepts strict single letters, full words ("south"), and phrases the LLM
    emits ("from the south", "the southern edge", "south-facing"): scans tokens
    for the first recognizable cardinal so "FROM THE SOUTH" -> "S". Returns None
    when no cardinal is found, so the caller drops the field and the demo default
    applies rather than failing the run.
    """
    if raw is None:
        return None
    s = str(raw).strip().upper()
    if not s:
        return None
    if s in _SIDE_WORD_TO_CARDINAL:
        return _SIDE_WORD_TO_CARDINAL[s]
    # Split phrases / hyphenated forms into word tokens: "FROM THE SOUTH",
    # "SOUTH-FACING", "S/SW" -> first recognizable cardinal wins.
    flattened = s
    for sep in ("-", "_", "/", ",", "."):
        flattened = flattened.replace(sep, " ")
    for tok in flattened.split():
        if tok in _SIDE_WORD_TO_CARDINAL:
            return _SIDE_WORD_TO_CARDINAL[tok]
    # Last resort: a leading cardinal letter (e.g. "SSW" -> "S").
    if s[0] in ("N", "S", "E", "W"):
        return s[0]
    return None


_RUN_SWAN_METADATA = AtomicToolMetadata(
    name="run_swan_waves",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
)


@register_tool(
    _RUN_SWAN_METADATA,
    # readOnlyHint=False (runs a solver writing output COG artifacts),
    # openWorldHint=False (Batch worker + intra-cloud object store),
    # destructiveHint=False (writes go to a new runs/ prefix),
    # idempotentHint=False (each call mints a new run_id + COG keys).
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def run_swan_waves(
    bbox: tuple[float, float, float, float] | list[float] | str | None = None,
    mode: str = "stationary",
    boundary_hs_m: float | None = None,
    boundary_tp_s: float | None = None,
    boundary_dir_deg: float | None = None,
    boundary_spread_deg: float | None = None,
    boundary_side: str | None = None,
    wind_uri: str | None = None,
    n_dir: int = 36,
    n_freq: int = 32,
    freq_low_hz: float = 0.04,
    freq_high_hz: float = 1.0,
    sim_duration_s: float = 10800.0,
    time_step_s: float = 600.0,
    output_frames: int = 24,
    friction: bool = True,
    breaking: bool = True,
    triads: bool = True,
    compute_class: str = "standard",
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> WaveFieldLayerURI | dict[str, Any]:
    """Run a STANDALONE SWAN nearshore spectral wave-field simulation over an AOI.

    Use this when: the user wants the DEFENSIBLE nearshore wave field
    itself -- significant wave heights/periods/direction, engineering-grade
    wave climate, overtopping inputs, buoy validation -- or wants to
    COMPARE SWAN against the existing SFINCS+SnapWave output on the same
    case. Solves the 3rd-gen spectral action-balance equation over real
    bathymetry. Do NOT use for: compound-flood/surge inundation depth
    (``run_model_flood_scenario`` -- SFINCS's fast in-model SnapWave path;
    SWAN is not a cheaper compound-flood solver); tsunami/dam-break
    (``run_geoclaw_inundation``); urban/pluvial drainage
    (``run_swmm_urban_flood``).

    Params:
        bbox: computational-domain AOI, EPSG:4326.
        mode: ``"stationary"`` (default, fast storm-peak field) or
            ``"nonstationary"`` (time-series animation); synonyms normalized.
        boundary_hs_m/boundary_tp_s/boundary_dir_deg/boundary_spread_deg:
            offshore boundary sea state; unset synthesizes a demo storm.
        boundary_side: forcing side {"N","S","E","W"}; auto-chosen if unset.
        wind_uri: optional ERA5 wind grid; enables GEN3 wind-sea growth.
        n_dir/n_freq/freq_low_hz/freq_high_hz: spectral discretization
            (defaults 36/32/0.04/1.0).
        sim_duration_s/time_step_s/output_frames: nonstationary run
            controls (defaults 10800/600/24).
        friction/breaking/triads: physics toggles (all default True).
        compute_class: default "standard".

    Returns:
        On success: ``WaveFieldLayerURI`` -- peak Hs COG + out-of-band
        per-timestep animation, with ``max_hs_m``, ``mean_tp_s``,
        ``mean_dir_deg``, ``wave_area_km2``.
        On failure: ``{"status": "error", "error_code", "error_message"}``
        -- ``SWAN_OUTPUT_EMPTY`` when nothing computed, never a silent
        empty layer. Not cached (``cacheable=False``).
    """
    # --- Validate + coerce into the SwanRunArgs contract --------------------
    if bbox is None:
        return {
            "status": "error",
            "error_code": "SWAN_PARAMS_INCOMPLETE",
            "error_message": (
                "run_swan_waves requires a bbox "
                "(min_lon, min_lat, max_lon, max_lat) in EPSG:4326."
            ),
        }
    coerced = coerce_bbox_value(bbox)
    if coerced is None:
        return {
            "status": "error",
            "error_code": "SWAN_PARAMS_INVALID",
            "error_message": (
                f"invalid bbox (expected 4 numbers min_lon,min_lat,max_lon,max_lat): "
                f"{bbox!r}"
            ),
        }
    try:
        # Assemble the optional parametric boundary only when the LLM supplied at
        # least one boundary field; otherwise leave it None so the composer
        # synthesizes a demo boundary from the AOI.
        boundary: SwanWaveBoundary | None = None
        if any(
            v is not None
            for v in (
                boundary_hs_m,
                boundary_tp_s,
                boundary_dir_deg,
                boundary_spread_deg,
                boundary_side,
            )
        ):
            bkwargs: dict[str, Any] = {}
            if boundary_hs_m is not None:
                bkwargs["hs_m"] = float(boundary_hs_m)
            if boundary_tp_s is not None:
                bkwargs["tp_s"] = float(boundary_tp_s)
            if boundary_dir_deg is not None:
                bkwargs["dir_deg"] = float(boundary_dir_deg)
            if boundary_spread_deg is not None:
                bkwargs["spread_deg"] = float(boundary_spread_deg)
            if boundary_side is not None:
                norm_side = _normalize_boundary_side(boundary_side)
                # Drop an unparseable side so the demo default applies instead of
                # failing SwanWaveBoundary validation (the transient red card).
                if norm_side is not None:
                    bkwargs["side"] = norm_side
            boundary = SwanWaveBoundary(**bkwargs)

        kwargs: dict[str, Any] = dict(
            bbox=tuple(coerced),  # type: ignore[arg-type]
            mode=mode,
            n_dir=int(n_dir),
            n_freq=int(n_freq),
            freq_low_hz=float(freq_low_hz),
            freq_high_hz=float(freq_high_hz),
            sim_duration_s=float(sim_duration_s),
            time_step_s=float(time_step_s),
            output_frames=int(output_frames),
            friction=bool(friction),
            breaking=bool(breaking),
            triads=bool(triads),
            compute_class=str(compute_class),
        )
        if boundary is not None:
            kwargs["boundary"] = boundary
        if wind_uri:
            kwargs["wind_uri"] = str(wind_uri)
        run_args = SwanRunArgs(**kwargs)
    except Exception as exc:  # noqa: BLE001 -- pydantic ValidationError or coercion
        return {
            "status": "error",
            "error_code": "SWAN_PARAMS_INVALID",
            "error_message": f"invalid SWAN run arguments: {exc}",
        }

    logger.info(
        "run_swan_waves bbox=%s mode=%s n_dir=%d n_freq=%d wind=%s",
        run_args.bbox,
        run_args.mode,
        run_args.n_dir,
        run_args.n_freq,
        bool(run_args.wind_uri),
    )

    try:
        peak = await model_wave_scenario(
            run_args,
            compute_class=compute_class,
        )
        logger.info(
            "run_swan_waves complete layer_id=%s mode=%s max_hs_m=%.4g "
            "mean_tp_s=%.4g mean_dir_deg=%.1f wave_area_km2=%.6g uri=%s",
            peak.layer_id,
            peak.mode,
            peak.max_hs_m,
            peak.mean_tp_s,
            peak.mean_dir_deg,
            peak.wave_area_km2,
            peak.uri,
        )
        return peak
    except asyncio.CancelledError:
        raise
    except (
        SwanWorkflowError,
        PostprocessSwanError,
        SwanComposerError,
    ) as exc:
        logger.warning("run_swan_waves failed: %s (%s)", exc.error_code, exc)
        return {
            "status": "error",
            "error_code": exc.error_code,
            "error_message": str(exc),
        }
    except Exception as exc:  # noqa: BLE001 -- defensive catch-all
        logger.exception("run_swan_waves unexpected failure")
        return {
            "status": "error",
            "error_code": "SWAN_INTERNAL_ERROR",
            "error_message": str(exc),
        }
