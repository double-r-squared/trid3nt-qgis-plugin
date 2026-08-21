"""Engine template ``swan_wave_field`` - SWAN (Simulating WAves Nearshore)
spectral nearshore wave engine (engine-door refactor - SWAN slice; was
``run_swan_waves``).

The LLM-facing exposure of the SWAN third-generation spectral wave engine. SWAN is
the ADDITIVE comparison engine: it runs STANDALONE over a coastal AOI and produces
its OWN engineering-grade wave field (significant wave height Hs, peak period Tp,
mean direction Dir) so a user can COMPARE SWAN against the existing SFINCS+SnapWave
output on the SAME case. ``swan_wave_field(...)`` takes the ``SwanRunArgs`` grid /
boundary fields, runs the deterministic fetch -> stage -> Batch-solve ->
postprocess chain (``model_swan_wave_field`` below, in this module), and returns
a ``WaveFieldLayerURI`` the emitter loads onto the map (it subclasses
``LayerURI`` so the ``emit_tool_call`` ``add_loaded_layer`` gate fires).

This is the SWAN analogue of ``geoclaw_inundation`` (GeoClaw) /
``swmm_urban_flood`` (SWMM) / ``sfincs_flood`` (SFINCS). It is a registered engine
TEMPLATE tagged ``engine="swan", tier="template"`` - EXCLUDED from the default
retrieval pool and surfaced only by the ``run_swan`` door's gate expansion
(SELECT-THEN-CALL). Like the other templates it declares ``cacheable=False`` +
``ttl_class="live-no-cache"`` + ``source_class="workflow_dispatch"`` (
workflow exposure surface; never touches the cache shim).

SWAN is BATCH-ONLY (the GPL Fortran lives in the worker container image, never in
the agent venv), so this always dispatches to AWS Batch.

ROUTING GUIDANCE (the engine-spike crux): SWAN is the DEFENSIBLE nearshore wave
field (full 2D spectra, wind-sea growth, swell, engineering-grade Hs/Tp/Dir for
buoy validation / overtopping inputs / "show the incoming waves").
SFINCS+SnapWave (``sfincs_flood``) is the FAST compound-flood setup path (one
combined solve). Route SWAN when the user wants a defensible wave field to
COMPARE; route SFINCS for fast inundation. SWAN does NOT replace SFINCS.

Determinism boundary (Invariant 1): every wave number the agent narrates comes
from the typed ``WaveFieldLayerURI.max_hs_m`` / ``.mean_tp_s`` / ``.mean_dir_deg``
/ ``.wave_area_km2`` fields the postprocess computed - never free-generated.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Any

from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.swan_contracts import (
    SWAN_WAVE_HEIGHT_STYLE_PRESET,
    SwanRunArgs,
    SwanWaveBoundary,
    WaveFieldLayerURI,
)
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.data import register_tool
from trid3nt_server.data.tool_arg_normalizer import coerce_bbox_value
from trid3nt_server.data.publish_layer.publish_layer import PublishLayerError, publish_layer
from trid3nt_server.workflows.swan._template_card import TemplateCard
from trid3nt_server.workflows.swan.postprocess_swan import PostprocessSwanError, postprocess_swan
from trid3nt_server.workflows.shared.register_published_manifest import (
    read_publish_manifest,
    register_swan_wave_layers,
)
from trid3nt_server.emission.outputs_seam import (
    build_layers_from_outputs,
    read_outputs_manifest,
)
from trid3nt_server.workflows.swan.run_swan import (
    SWAN_SOLVER_NAME,
    SwanWorkflowError,
    stage_swan_manifest,
)
from trid3nt_server.workflows.shared.solve_progress import drive_live_solve_progress
from trid3nt_server.emission.layer_uri_emit import emit_layer_uri
from trid3nt_server.fallbacks import persist_run_activations
def fetch_topobathy(bbox: Any = None, **kwargs: Any):
    """Registry-closure indirection for the folded ``fetch_topobathy``:
    a module-level, patchable shim (the swan-chain tests patch this attribute). The
    promoted router closure is ``**kwargs``-only, so the positional ``bbox`` is
    mapped to the keyword the closure expects."""
    from trid3nt_server.data import TOOL_REGISTRY

    if bbox is not None:
        kwargs["bbox"] = bbox
    return TOOL_REGISTRY["fetch_topobathy"].fn(**kwargs)


from trid3nt_server.emission.pipeline_emitter import (
    begin_substeps,
    current_emitter,
    mint_dispatch_and_sim_cards,
    route_sim_terminal,
    substep,
)

logger = logging.getLogger(
    "trid3nt_server.workflows.swan.wave_field.wave_field"
)

__all__ = [
    "swan_wave_field",
    "RunSwanError",
    "model_swan_wave_field",
    "SwanComposerError",
]


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


def build_storm_hydrograph(
    baseline_hs_m: float,
    peak_hs_m: float,
    tp_s: float,
    dir_deg: float,
    spread_deg: float,
    sim_duration_s: float,
    peak_hour: float | None,
    n_points: int = 9,
) -> list[tuple[float, float, float, float, float]]:
    """Build a time-varying storm boundary series (build to a peak, then decay).

    Returns ``[(t_sec, hs_m, tp_s, dir_deg, spread_deg), ...]`` - a triangular
    Hs envelope from ``baseline_hs_m`` up to ``peak_hs_m`` at ``peak_hour`` then
    back to baseline at the end. Tp grows modestly with Hs (a longer-period sea
    at the peak). Pure - unit-testable, no network."""
    dur = float(sim_duration_s)
    peak_t = (float(peak_hour) * 3600.0 if peak_hour is not None else dur / 2.0)
    peak_t = min(max(peak_t, dur * 0.05), dur * 0.95)
    base = max(float(baseline_hs_m), 0.1)
    peak = max(float(peak_hs_m), base)
    ts = [dur * k / (n_points - 1) for k in range(n_points)]
    rows: list[tuple[float, float, float, float, float]] = []
    for t in ts:
        if t <= peak_t:
            frac = t / peak_t if peak_t > 0 else 1.0
        else:
            frac = (dur - t) / (dur - peak_t) if dur > peak_t else 0.0
        hs = base + (peak - base) * max(0.0, min(1.0, frac))
        # Tp scales gently with the sea state (peak sea ~ +25% period).
        tp = float(tp_s) * (1.0 + 0.25 * (hs - base) / max(peak - base, 1e-6))
        rows.append((round(t, 1), round(hs, 3), round(tp, 3),
                     float(dir_deg), float(spread_deg)))
    return rows


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


#: Curated door-listing card (the run_swan door prefers this over signature
#: derivation). One-line question + the real required input + a knobs summary.
TEMPLATE_CARD = TemplateCard(
    question=(
        "the DEFENSIBLE nearshore spectral wave field - significant wave height "
        "Hs / peak period Tp / mean direction Dir (SWAN 3rd-gen spectral, "
        "standalone or to COMPARE against SFINCS+SnapWave)"
    ),
    required_inputs=["bbox"],
    knobs=(
        "mode (stationary / nonstationary), boundary_hs_m, boundary_tp_s, "
        "boundary_dir_deg, boundary_spread_deg, boundary_side, wind_uri, "
        "n_dir, n_freq, freq_low_hz, freq_high_hz, sim_duration_s, time_step_s, "
        "output_frames, storm_peak_hs_m, storm_peak_hour, friction, breaking, triads"
    ),
)


_SWAN_WAVE_FIELD_METADATA = AtomicToolMetadata(
    name="swan_wave_field",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="swan",
    tier="template",
)


@register_tool(
    _SWAN_WAVE_FIELD_METADATA,
    # readOnlyHint=False (runs a solver writing output COG artifacts),
    # openWorldHint=False (Batch worker + intra-cloud object store),
    # destructiveHint=False (writes go to a new runs/ prefix),
    # idempotentHint=False (each call mints a new run_id + COG keys).
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def swan_wave_field(
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
    storm_peak_hs_m: float | None = None,
    storm_peak_hour: float | None = None,
    friction: bool = True,
    breaking: bool = True,
    triads: bool = True,
    gen_formulation: str = "westhuysen",
    whitecapping: str | None = None,
    quad_iquad: int | None = None,
    breaking_alpha: float = 1.0,
    breaking_gamma: float = 0.73,
    friction_cfjon: float = 0.067,
    triad_biphase: str | None = None,
    triad_urcrit: float = 0.63,
    triad_lpar: float = 0.0,
    compute_class: str = "standard",
    # absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> WaveFieldLayerURI | dict[str, Any]:
    """Run a STANDALONE SWAN nearshore spectral wave-field simulation over an AOI.

    Fidelity: SWAN phase-averaged spectral wave field (Hs / Tp / Dir over real
    nearshore bathymetry; requires real below-datum bathymetry); engineering /
    planning-grade wave field, not an inundation solver. Off-scope: compound-flood
    / surge / pluvial / riverine inundation depth -> sfincs_flood; tsunami /
    dam-break run-up -> geoclaw_inundation; urban storm-sewer -> swmm_urban_flood.

    Use this when: the user wants the DEFENSIBLE nearshore wave field itself --
    significant wave heights/periods/direction, engineering-grade wave climate,
    overtopping inputs, buoy validation -- or wants to COMPARE SWAN against the
    existing SFINCS+SnapWave output on the same case. Solves the 3rd-gen spectral
    action-balance equation over real bathymetry. Do NOT use for: compound-flood
    /surge inundation depth (``sfincs_flood`` -- SFINCS's fast in-model SnapWave
    path; SWAN is not a cheaper compound-flood solver); tsunami/dam-break
    (``geoclaw_inundation``); urban/pluvial drainage (``swmm_urban_flood``).

    Params:
        bbox: computational-domain AOI, EPSG:4326.
        mode: ``"stationary"`` (default, fast storm-peak field) or
            ``"nonstationary"`` (time-series animation); synonyms normalized.
        boundary_hs_m/boundary_tp_s/boundary_dir_deg/boundary_spread_deg:
            offshore boundary sea state; unset synthesizes a demo storm.
        boundary_side: forcing side {"N","S","E","W"}; auto-chosen if unset.
        storm_peak_hs_m: OPTIONAL peak offshore Hs (m) of a TIME-VARYING storm
            (nonstationary only). When set, the offshore boundary BUILDS from the
            baseline ``boundary_hs_m`` up to this peak at ``storm_peak_hour`` then
            DECAYS back over ``sim_duration_s`` - a passing 24-48 h storm whose
            nearshore wave field genuinely evolves (vs a constant-boundary
            spin-up). Forces ``mode="nonstationary"``.
        storm_peak_hour: OPTIONAL hour (from run start) of the storm peak.
            Default = the middle of the run. Only used with ``storm_peak_hs_m``.
        wind_uri: optional ERA5 wind grid; enables GEN3 wind-sea growth.
        n_dir/n_freq/freq_low_hz/freq_high_hz: spectral discretization
            (defaults 36/32/0.04/1.0).
        sim_duration_s/time_step_s/output_frames: nonstationary run
            controls (defaults 10800/600/24). ``output_frames`` is the deck-side
            cadence lever (evenly spaced across the sim window; the universal
            ``output_interval_min`` vocabulary maps as
            ``round(sim_duration_min / output_interval_min)``). Every
            solver-written snapshot is published (never subsampled).
        friction/breaking/triads: physics toggles (all default True).
        gen_formulation/whitecapping/quad_iquad/breaking_gamma/friction_cfjon/
            triad_biphase: explicit physics-scheme knobs (defaults reproduce the
            operational deck). For an A-vs-B sensitivity comparison across a
            physics axis use ``swan_physics_sensitivity_sweep`` instead.
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
                "swan_wave_field requires a bbox "
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

        # TIME-VARYING storm: a storm_peak_hs_m forces nonstationary mode and
        # builds the offshore boundary hydrograph (build-peak-decay). Requires a
        # resolved boundary (baseline Hs + Tp/dir/spread come from it).
        storm_series = None
        if storm_peak_hs_m is not None:
            mode = "nonstationary"
            _b = boundary
            base_hs = float(_b.hs_m) if _b is not None else (
                float(boundary_hs_m) if boundary_hs_m is not None else 1.0)
            base_tp = float(_b.tp_s) if _b is not None else (
                float(boundary_tp_s) if boundary_tp_s is not None else 10.0)
            base_dir = float(_b.dir_deg) if _b is not None else (
                float(boundary_dir_deg) if boundary_dir_deg is not None else 180.0)
            base_spread = float(_b.spread_deg) if _b is not None else (
                float(boundary_spread_deg) if boundary_spread_deg is not None else 25.0)
            storm_series = build_storm_hydrograph(
                base_hs, float(storm_peak_hs_m), base_tp, base_dir, base_spread,
                float(sim_duration_s), storm_peak_hour,
            )

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
            gen_formulation=str(gen_formulation),
            whitecapping=whitecapping,
            quad_iquad=quad_iquad,
            breaking_alpha=float(breaking_alpha),
            breaking_gamma=float(breaking_gamma),
            friction_cfjon=float(friction_cfjon),
            triad_biphase=triad_biphase,
            triad_urcrit=float(triad_urcrit),
            triad_lpar=float(triad_lpar),
            compute_class=str(compute_class),
        )
        if boundary is not None:
            kwargs["boundary"] = boundary
        if wind_uri:
            kwargs["wind_uri"] = str(wind_uri)
        if storm_series is not None:
            kwargs["storm_boundary_timeseries"] = storm_series
        run_args = SwanRunArgs(**kwargs)
    except Exception as exc:  # noqa: BLE001 -- pydantic ValidationError or coercion
        return {
            "status": "error",
            "error_code": "SWAN_PARAMS_INVALID",
            "error_message": f"invalid SWAN run arguments: {exc}",
        }

    logger.info(
        "swan_wave_field bbox=%s mode=%s n_dir=%d n_freq=%d wind=%s",
        run_args.bbox,
        run_args.mode,
        run_args.n_dir,
        run_args.n_freq,
        bool(run_args.wind_uri),
    )

    try:
        peak = await model_swan_wave_field(
            run_args,
            compute_class=compute_class,
        )
        logger.info(
            "swan_wave_field complete layer_id=%s mode=%s max_hs_m=%.4g "
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
        logger.warning("swan_wave_field failed: %s (%s)", exc.error_code, exc)
        return {
            "status": "error",
            "error_code": exc.error_code,
            "error_message": str(exc),
        }
    except Exception as exc:  # noqa: BLE001 -- defensive catch-all
        logger.exception("swan_wave_field unexpected failure")
        return {
            "status": "error",
            "error_code": "SWAN_INTERNAL_ERROR",
            "error_message": str(exc),
        }


# --------------------------------------------------------------------------- #
# The composer. A
# deterministic orchestrator-style chain (Invariant 2 - no LLM in the chain):
#   fetch a topo/bathy DEM (fetch_topobathy seamless land+bathy)
#     -> resolve a parametric offshore wave boundary (demo synthesis, or the
#        caller-supplied SwanRunArgs.boundary)
#     -> stage build_spec manifest + DEM reference to S3 (run_swan)
#     -> run_solver('swan', ...) -> wait_for_completion (AWS Batch, the SAME
#        generic dispatch seam SFINCS/GeoClaw use)
#     -> a present publish_manifest.json short-circuits into the REGISTER-ONLY
#        path (worker-side postprocess offload); otherwise download the SWAN
#        swan_out.mat output and run postprocess_swan on-box (rasterize the Hs
#        field -> peak primary Hs COG + per-frame Hs COGs)
#     -> publish the peak primary + emit the frames out-of-band (the Phase-1
#        scrubber animation group).
# SWAN is BATCH-ONLY (the GPL Fortran lives in the worker container image,
# never in the agent venv), so this always dispatches to AWS Batch.
# Determinism boundary (Invariant 1): every wave number the agent narrates
# comes from the typed WaveFieldLayerURI fields the postprocess computed -
# never free-generated.
# --------------------------------------------------------------------------- #

#: SWAN solve ETA heuristic (s) per mesh cell -- a coarse perf hint for the live
#: progress heartbeat (Invariant 1: a hint, never a narrated number). A full
#: spectral solve is pricier per cell than a shallow-water step.
_SWAN_SEC_PER_CELL: float = 0.08


class SwanComposerError(RuntimeError):
    """Raised on a fatal composer failure (carries an open-set ``error_code``)."""

    error_code: str = "SWAN_COMPOSER_FAILED"

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


# --------------------------------------------------------------------------- #
# Bathy DEM acquisition (topobathy seamless -> fetch_dem fallback).
# --------------------------------------------------------------------------- #
#: The bathymetry rungs a SWAN wave field tolerates. CUDEM's 1/9" collection
#: covers only part of the US coast, and where it stops the 3DEP land leg paints
#: flat 0 m ocean -- a fake landmass SWAN excludes from its grid. The global
#: ETOPO relief is coarse but REAL below-waterline bed, so the gap is filled from
#: it, loudly labeled, rather than refused or faked.
_SWAN_BATHY_FALLBACK = ("etopo_bathy_base",)


def _fetch_bathy_for_swan(
    bbox: tuple[float, float, float, float],
    *,
    activation_sink: list[Any] | None = None,
) -> str:
    """Fetch a topo/bathy DEM for the AOI and return its ``s3://`` URI.

    SWAN needs a SEAMLESS land+bathymetry DEM (the bed for depth-induced shoaling /
    breaking): try ``fetch_topobathy`` first (the seamless coastal DEM -- the right
    substrate for a nearshore wave field).

    ``activation_sink`` collects the fallback-ladder rows the fetch reported, so
    the composer can stamp what actually painted the bed onto its own result.

    REQUIRES REAL BATHYMETRY: a coastal wave model run on a LAND-ONLY DEM (all
    positive NAVD88 elevations) renders an ALL-DRY SWAN bottom grid (every cell
    above the still-water level -> depth < DEPMIN -> inactive), so SWAN "prepares
    computation", runs zero sweeps, and writes no swan_out.mat. ``fetch_topobathy``
    degrades to a land-only 3DEP fallback and signals it via
    ``bathymetry_present=False``; we REJECT a bathymetry-absent result up front
    with an honest typed error rather than launch a guaranteed no-op solve. A
    direct ``fetch_dem`` (3DEP, land-only) fallback is intentionally NOT used
    here: it can never carry below-sea-level depths, so it would always produce
    an all-dry deck for a coastal AOI.

    Returns the DEM cache/runs ``s3://`` URI (staged BY REFERENCE -- the worker
    downloads it directly). Raises ``SwanComposerError`` when the fetch fails OR
    when the result carries no bathymetry (the data-source fallback norm: primary
    -> honest typed error, never a silent all-dry dead-end).
    """
    def _attr(layer: Any, name: str) -> Any:
        if isinstance(layer, dict):
            return layer.get(name)
        return getattr(layer, name, None)

    try:
        layer = fetch_topobathy(bbox, fallback=_SWAN_BATHY_FALLBACK)
    except Exception as exc:  # noqa: BLE001
        raise SwanComposerError(
            "SWAN_DEM_FETCH_FAILED",
            f"fetch_topobathy failed for bbox {bbox}: {exc}",
        ) from exc

    if activation_sink is not None:
        activation_sink.extend(_attr(layer, "fallbacks") or [])

    uri = _attr(layer, "uri")
    if not uri:
        raise SwanComposerError(
            "SWAN_DEM_FETCH_FAILED",
            f"fetch_topobathy returned no uri for bbox {bbox}",
        )

    # REQUIRE real bathymetry. ``bathymetry_present`` is False when CUDEM had no
    # coverage and fetch_topobathy degraded to a LAND-ONLY 3DEP surface -- which
    # has NO below-datum sea cells, so the SWAN bottom grid would be entirely dry
    # and SWAN would no-op silently. Default True so a plain ``LayerURI`` (no flag,
    # e.g. a test stub) is accepted.
    bathy_present = _attr(layer, "bathymetry_present")
    if bathy_present is False:
        warning = _attr(layer, "fallback_warning")
        raise SwanComposerError(
            "SWAN_NO_BATHYMETRY",
            f"fetch_topobathy returned a LAND-ONLY DEM for bbox {bbox} "
            f"(bathymetry_present=False); a coastal SWAN run needs real "
            f"below-datum bathymetry or the computational grid is all-dry and "
            f"SWAN no-ops (empty solve). "
            + (f"({warning})" if warning else "No CUDEM coastal coverage for this AOI."),
        )

    return str(uri)


def _record_swan_batch_solve_telemetry(
    *,
    run_result: Any,
    handle: Any,
    staging: Any,
    compute_class: str,
    session_id: str | None = None,
    case_id: str | None = None,
) -> dict | None:
    """Record ONE SOLVE row for the SWAN Batch lane (mirrors the GeoClaw/SFINCS
    telemetry sibling). Best-effort; returns the recorded row or ``None``."""
    from trid3nt_server.telemetry import record_solve_telemetry

    meta = getattr(run_result, "batch_compute_meta", None) or {}
    if not isinstance(meta, dict):
        meta = {}

    row: dict = {
        "run_id": getattr(run_result, "run_id", None) or staging.run_id,
        "solver": SWAN_SOLVER_NAME,
        "status": getattr(run_result, "status", None),
        "backend": str(getattr(handle, "workflow_name", "") or "unknown"),
        "compute_class": compute_class,
        "case_id": case_id,
        "session_id": session_id,
        "active_cell_count": int(getattr(staging, "n_active_cells", 0) or 0),
        "mode": staging.run_args.mode,
    }
    row.update(meta)
    return record_solve_telemetry(row)


def _stamp_swan_provenance(
    peak: WaveFieldLayerURI,
    run_args: SwanRunArgs,
    bathy_activation: list[Any] | None = None,
) -> WaveFieldLayerURI:
    """Surface SWAN's wave-physics calibration coefficients (law 9, audit row 34)
    and which rungs of the bathymetry ladder painted the bed.

    The coefficients are literature-canonical SWAN constants (single
    universally-accepted published values), so they carry a documented-default
    label and PROCEED (numerical consequence, no refuse) -- they are not invented
    site claims. The bed rows go through ``stamp_fallbacks``, the one merge seam,
    so a re-stamp cannot duplicate a row or double the narration.
    """
    from trid3nt_server.emission.layer_uri_emit import stamp_fallbacks

    entry = SyntheticInput(
        param="wave_physics_coefficients",
        value=(f"breaking_alpha={run_args.breaking_alpha:g}, "
               f"breaking_gamma={run_args.breaking_gamma:g}, "
               f"friction_cfjon={run_args.friction_cfjon:g}, "
               f"triads={'on' if run_args.triads else 'off'}"),
        basis="default_demo", consequence="numerical",
        note="literature-canonical SWAN calibration constants (depth-induced "
             "breaker index + JONSWAP bottom friction + triad closure); documented "
             "published values, not site-fit",
    )
    stamped = stamp_fallbacks(peak, bathy_activation)
    return stamped.model_copy(
        update={"synthetic_inputs": list(stamped.synthetic_inputs or []) + [entry]}
    )


async def model_swan_wave_field(
    run_args: SwanRunArgs,
    *,
    dem_uri: str | None = None,
    run_id: str | None = None,
    compute_class: str = "standard",
    cleanup_outputs: bool = True,
) -> WaveFieldLayerURI:
    """Compose the full standalone SWAN nearshore wave-field chain (Batch).

    Args:
        run_args: the validated ``SwanRunArgs`` (bbox + mode + boundary forcing).
        dem_uri: optional topo/bathy DEM ``s3://`` URI. When ``None`` the composer
            fetches it (``fetch_topobathy`` -> ``fetch_dem`` fallback). Tests pass
            a synthetic URI to skip the fetch.
        run_id: optional ULID; minted by the staging step if absent.
        compute_class: compute class for the Batch sizing.
        cleanup_outputs: when True, the downloaded output dir is removed after
            postprocess (the COGs were already uploaded).

    Returns:
        The PEAK ``WaveFieldLayerURI`` (role ``"primary"``, name ``"Peak wave
        height"``) carrying the four narration scalars + the echoed mode. Per-frame
        Hs layers are emitted out-of-band via the emitter.

    Raises:
        SwanComposerError / SwanWorkflowError / PostprocessSwanError on a fatal
        stage failure (the tool wrapper catches these and returns a typed error
        dict so the agent narrates honestly).
    """
    bbox = tuple(run_args.bbox)
    emitter = current_emitter()

    # --- Zoom-on-area-first: the map zooms before the solve runs. ---
    if emitter is not None:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(bbox)})
        except Exception as exc:  # noqa: BLE001 -- non-fatal UX hint
            logger.warning("model_swan_wave_field: zoom-to emit failed: %s", exc)

    # --- Sub-step plan: fetch DEM -> stage -> solve -> postprocess -> publish. --
    begin_substeps(emitter, 5)

    # --- Step 1: topo/bathy DEM (off-loop blocking I/O) ---------------------
    bathy_activation: list[Any] = []
    if dem_uri is None:
        async with substep(emitter, "fetch_topobathy"):
            resolved_dem_uri = await asyncio.to_thread(
                lambda: _fetch_bathy_for_swan(bbox, activation_sink=bathy_activation)
            )
    else:
        resolved_dem_uri = dem_uri
    logger.info("model_swan_wave_field: DEM=%s", resolved_dem_uri)

    # surface the fetched topo/bathy DEM (the seabed SWAN propagates
    # waves over) as a role=context input. Lights all 3 SWAN templates (wave_field
    # directly; the sweep + snapshot batch route through this composer). Rides the
    # fetched s3:// COG; best-effort -- never fails the solve.
    from trid3nt_server.emission.layer_uri_emit import publish_raster_input_cog
    await publish_raster_input_cog(
        emitter,
        cog_uri=resolved_dem_uri,
        layer_id=f"input-bathymetry-{run_id}",
        name="Input: topo/bathy DEM (seamless coastal, fetch_topobathy)",
        style_preset="continuous_dem",
        role="context",
    )

    # --- Step 2: stage the build_spec manifest + DEM reference --------------
    async with substep(emitter, "stage_swan_manifest"):
        staging = await asyncio.to_thread(
            stage_swan_manifest,
            run_args,
            dem_uri=resolved_dem_uri,
            run_id=run_id,
            wind_uri=run_args.wind_uri,
        )

    # ONE local compute environment: the solve runs on the host CPUs, so the
    # caller's compute_class flows through unchanged (no auto-scaling).
    import os as _os

    n_active = int(getattr(staging, "n_active_cells", 0) or 0)
    effective_compute_class = compute_class

    # --- Step 3: dispatch via the generic run_solver seam -------------------
    from trid3nt_server.data.simulation.solver.solver import (
        EmitterBinding,
        run_solver,
        set_emitter_binding,
        wait_for_completion,
    )

    handle = run_solver(
        solver=SWAN_SOLVER_NAME,
        model_setup_uri=staging.manifest_uri,
        compute_class=effective_compute_class,
    )

    # --- Two-card sim observability (dispatch card + Batch-bound sim card) --
    _sim_step_id = await mint_dispatch_and_sim_cards(
        emitter=emitter,
        solver=SWAN_SOLVER_NAME,
        handle=handle,
        compute_class=effective_compute_class,
    )
    if emitter is not None and _sim_step_id is not None:
        set_emitter_binding(EmitterBinding(emitter=emitter, step_id=_sim_step_id))

    _progress_task = asyncio.ensure_future(
        drive_live_solve_progress(
            emitter=current_emitter(),
            run_id=staging.run_id,
            solver=SWAN_SOLVER_NAME,
            grid_resolution_m=None,
            active_cell_count=n_active or None,
            vcpus=_os.cpu_count(),
            eta_seconds=(n_active * _SWAN_SEC_PER_CELL) if n_active else None,
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
                # Invariant 8: propagate the cancel; route it to the SIM card.
                logger.info("model_swan_wave_field cancelled while awaiting solver")
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
        # Child already marked red by the substep; fall through to the telemetry +
        # typed-error path (which records + raises SwanWorkflowError).
        pass

    await route_sim_terminal(emitter, _sim_step_id, run_result=run_result)

    # --- SOLVE telemetry (Batch instance + size + timing) ------------------
    try:
        _record_swan_batch_solve_telemetry(
            run_result=run_result,
            handle=handle,
            staging=staging,
            compute_class=effective_compute_class,
        )
    except Exception as exc:  # noqa: BLE001 -- never break the solve
        logger.warning("SWAN solve batch-compute telemetry failed (non-fatal): %s", exc)

    if run_result.status != "complete":
        raise SwanWorkflowError(
            "SWAN_RUN_FAILED",
            message=(
                "SWAN Batch solve did not complete "
                f"(status={run_result.status}, "
                f"error_code={getattr(run_result, 'error_code', None)}): "
                f"{getattr(run_result, 'error_message', '') or getattr(run_result, 'cancellation_reason', '') or ''}"
            ),
            details={
                "run_id": staging.run_id,
                "output_uri": getattr(run_result, "output_uri", None),
            },
        )

    # --- Postprocess-offload branch (Phase 4): worker-written manifest -------
    # When the SWAN Batch worker rebuilds with the raster-postprocess offload it
    # runs the heavy .mat -> COG conversion ITSELF (display-ready overview-bearing
    # COGs) and writes a thin typed publish_manifest.json (pointed to by
    # completion.json.publish_manifest_uri). ``read_publish_manifest`` reads +
    # SCHEMA-GATEs it; a present, schema_version==1 manifest activates the
    # REGISTER-ONLY path below - SHORT-CIRCUITing the on-box heavy tail entirely
    # (NO _download_batch_swan_outputs, NO postprocess_swan, NO _ensure_raster_
    # has_overviews). The publish-or-honest-drop gate (TRID3NT_TILE_SERVER_BASE) +
    # the render-chokepoint registration are preserved per layer.
    #
    # ONE-RELEASE SAFETY: manifest absent OR unknown schema_version ->
    # ``read_publish_manifest`` returns None and the EXISTING on-box path below
    # runs unchanged (the raw swan_out.mat is still uploaded). Clean if/else.
    # (The SWAN worker does NOT emit a manifest yet, so today this always falls
    # back; the branch is forward-ready for when the SWAN worker side lands.)
    # EMIT-ON-SOLVE SEAM (ADR 0281). When the rebuilt worker wrote an outputs.json
    # under the run prefix, the SEAM owns ALL publication (peak + animation frames)
    # -- proven byte-equivalent to the register path
    # (tests/test_swan_outputs_seam.py). ``publish_manifest`` is STILL read, but
    # ONLY for the top-level wave narration scalars (the flat outputs.json entries
    # carry no aggregates -- publish_manifest is the metrics carrier, not a second
    # publication). Absent outputs.json -> the legacy register-only publish_manifest
    # path runs byte-unchanged (one-release safety); absent both -> the on-box
    # swan_out.mat download path runs byte-unchanged. Clean seam-or-legacy fork.
    outputs_manifest = await asyncio.to_thread(read_outputs_manifest, run_result)
    manifest = await asyncio.to_thread(read_publish_manifest, run_result)
    if outputs_manifest is not None:
        logger.info(
            "model_swan_wave_field: SEAM path (outputs.json emit-on-solve) "
            "run_id=%s engine=%s entries=%d",
            staging.run_id, outputs_manifest.engine, len(outputs_manifest.entries),
        )
        async with substep(emitter, "publish_layer"):
            seam = await asyncio.to_thread(
                build_layers_from_outputs,
                outputs_manifest, run_id=staging.run_id, bbox=bbox,
            )
        if not seam.layers:
            raise SwanComposerError(
                "SWAN_NO_LAYERS",
                "outputs.json seam produced no renderable wave layers "
                "(honesty floor: cannot narrate an empty solve).",
            )
        _m = dict(manifest.metrics) if manifest is not None else {}
        _prim = next(l for l in seam.layers if l.role == "primary")
        _frame_seam = [l for l in seam.layers if l.role != "primary"]
        peak = WaveFieldLayerURI(
            layer_id=_prim.layer_id, name=_prim.name, layer_type=_prim.layer_type,
            uri=_prim.uri, style_preset=_prim.style_preset, role=_prim.role,
            units=_prim.units, bbox=_prim.bbox or bbox,
            max_hs_m=float(_m.get("max_hs_m", 0.0) or 0.0),
            mean_tp_s=float(_m.get("mean_tp_s", 0.0) or 0.0),
            mean_dir_deg=float(_m.get("mean_dir_deg", 0.0) or 0.0),
            wave_area_km2=float(_m.get("wave_area_km2", 0.0) or 0.0),
            mean_hs_m=float(_m.get("mean_hs_m", 0.0) or 0.0),
            mode=run_args.mode,  # type: ignore[arg-type]
        )
        # Wrap the seam's context frames into WaveFieldLayerURI (context scalars
        # 0.0 -- the peak drives narration) so ``_emit_frame_layers``' publish
        # chokepoint re-wrap has the typed wave fields.
        frame_layers = [
            WaveFieldLayerURI(
                layer_id=l.layer_id, name=l.name, layer_type=l.layer_type,
                uri=l.uri, style_preset=l.style_preset, role=l.role,
                units=l.units, bbox=l.bbox or bbox,
                max_hs_m=0.0, mean_tp_s=0.0, mean_dir_deg=0.0,
                wave_area_km2=0.0, mean_hs_m=0.0,
                mode=run_args.mode,  # type: ignore[arg-type]
            )
            for l in _frame_seam
        ]
        emitted_frames = await _emit_frame_layers(emitter, frame_layers, staging.run_id)
        logger.info(
            "model_swan_wave_field complete (seam) run_id=%s mode=%s max_hs_m=%.4g "
            "frames_emitted=%d/%d peak_uri=%s",
            staging.run_id, run_args.mode, peak.max_hs_m,
            emitted_frames, len(frame_layers), peak.uri,
        )
        if emitter is not None:
            try:
                await emitter.emit_map_command("zoom-to", {"bbox": list(bbox)})
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "model_swan_wave_field: seam zoom-to failed: %s", exc
                )
        await asyncio.to_thread(
            persist_run_activations, staging.run_id, bathy_activation,
            capability_note="topo-bathymetry bed for the SWAN wave grid",
        )
        return _stamp_swan_provenance(peak, run_args, bathy_activation)
    if manifest is not None:
        logger.info(
            "model_swan_wave_field: REGISTER-ONLY path (worker postprocess offload) "
            "run_id=%s engine=%s layers=%d",
            staging.run_id, manifest.engine, len(manifest.layers),
        )
        async with substep(emitter, "publish_layer"):
            wave_layers, _top_metrics, _dropped = await asyncio.to_thread(
                register_swan_wave_layers,
                manifest,
                run_id=staging.run_id,
                mode=run_args.mode,
                bbox=bbox,
            )
        if not wave_layers:
            raise SwanComposerError(
                "SWAN_NO_LAYERS",
                "publish manifest produced no renderable wave layers "
                "(no tile server configured, or empty manifest).",
            )
        peak = wave_layers[0]
        frame_layers = wave_layers[1:]
        emitted_frames = await _emit_frame_layers(
            emitter, frame_layers, staging.run_id
        )
        logger.info(
            "model_swan_wave_field complete (register-only) run_id=%s mode=%s "
            "max_hs_m=%.4g frames_emitted=%d/%d peak_uri=%s",
            staging.run_id, run_args.mode, peak.max_hs_m,
            emitted_frames, len(frame_layers), peak.uri,
        )
        if emitter is not None:
            try:
                await emitter.emit_map_command("zoom-to", {"bbox": list(bbox)})
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "model_swan_wave_field: register-only zoom-to failed: %s", exc
                )
        await asyncio.to_thread(
            persist_run_activations, staging.run_id, bathy_activation,
            capability_note="topo-bathymetry bed for the SWAN wave grid",
        )
        return _stamp_swan_provenance(peak, run_args, bathy_activation)

    # --- Step 4: download the Batch SWAN output (ON-BOX FALLBACK) ----------
    batch_run_id = getattr(run_result, "run_id", None) or staging.run_id
    out_dir = await asyncio.to_thread(_download_batch_swan_outputs, batch_run_id)

    try:
        # --- Step 5: postprocess (rasterize Hs -> peak + frames) -----------
        async with substep(emitter, "postprocess_swan"):
            layers, metrics = await asyncio.to_thread(
                postprocess_swan,
                out_dir,
                bbox,
                run_id=staging.run_id,
                mode=run_args.mode,
            )
    finally:
        if cleanup_outputs:
            _cleanup_dir(out_dir)

    if not layers:
        raise SwanComposerError(
            "SWAN_NO_LAYERS",
            "postprocess_swan produced no wave layers (empty solve?)",
        )

    raw_peak = layers[0]
    frame_layers = layers[1:]

    # --- Step 6: publish the PEAK COG through publish_layer (render chokepoint)
    async with substep(emitter, "publish_layer"):
        peak = await asyncio.to_thread(_publish_peak_layer, raw_peak, staging.run_id)

    # --- Step 6b: publish + emit the per-frame animation layers OUT-OF-BAND --
    emitted_frames = await _emit_frame_layers(emitter, frame_layers, staging.run_id)

    logger.info(
        "model_swan_wave_field complete run_id=%s mode=%s max_hs_m=%.4g "
        "mean_tp_s=%.4g mean_dir_deg=%.1f wave_area_km2=%.6g "
        "frames_emitted=%d/%d peak_uri=%s",
        staging.run_id,
        run_args.mode,
        peak.max_hs_m,
        peak.mean_tp_s,
        peak.mean_dir_deg,
        peak.wave_area_km2,
        emitted_frames,
        len(frame_layers),
        peak.uri,
    )

    # --- AUTHORITATIVE LAST zoom-to ----------------------------------------
    if emitter is not None:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(bbox)})
        except Exception as exc:  # noqa: BLE001
            logger.warning("model_swan_wave_field: authoritative zoom-to failed: %s", exc)

    await asyncio.to_thread(
        persist_run_activations, batch_run_id, bathy_activation,
        capability_note="topo-bathymetry bed for the SWAN wave grid",
    )
    return _stamp_swan_provenance(peak, run_args, bathy_activation)


def _publish_peak_layer(
    raw_peak: WaveFieldLayerURI, run_id: str
) -> WaveFieldLayerURI:
    """Publish the PEAK Hs COG through publish_layer (render chokepoint).

    Routes the raw s3:// peak COG through ``publish_layer`` and returns a NEW
    ``WaveFieldLayerURI`` carrying the published /tiles or WMS URL plus the
    narration scalars. On publish failure the raw peak is returned UNCHANGED: the
    dispatch-level ``emit_layer_uri`` guardrail then drops the dead raw-s3:// raster
    from the map (honest) while the typed metrics still narrate. Mirrors the
    GeoClaw/SFINCS primary-publish path.
    """
    if raw_peak.layer_type != "raster" or not (
        raw_peak.uri.startswith("gs://") or raw_peak.uri.startswith("s3://")
    ):
        return raw_peak
    layer_id_for_pub = f"swan-wave-height-peak-{run_id}"
    try:
        published_uri = publish_layer(
            layer_uri=raw_peak.uri,
            layer_id=layer_id_for_pub,
            style_preset=raw_peak.style_preset or SWAN_WAVE_HEIGHT_STYLE_PRESET,
        )
    except PublishLayerError as exc:
        logger.warning(
            "model_swan_wave_field: publish_layer FAILED for the peak layer_id=%s "
            "error_code=%s (%s) - returning the unpublished peak.",
            layer_id_for_pub,
            exc.error_code,
            exc,
        )
        return raw_peak
    return WaveFieldLayerURI(
        layer_id=layer_id_for_pub,
        name=raw_peak.name,
        layer_type=raw_peak.layer_type,
        uri=published_uri,
        style_preset=raw_peak.style_preset or SWAN_WAVE_HEIGHT_STYLE_PRESET,
        role=raw_peak.role,
        units=raw_peak.units,
        bbox=raw_peak.bbox,
        max_hs_m=raw_peak.max_hs_m,
        mean_tp_s=raw_peak.mean_tp_s,
        mean_dir_deg=raw_peak.mean_dir_deg,
        wave_area_km2=raw_peak.wave_area_km2,
        mean_hs_m=raw_peak.mean_hs_m,
        mode=raw_peak.mode,
    )


async def _emit_frame_layers(
    emitter: Any, frame_layers: list[WaveFieldLayerURI], run_id: str
) -> int:
    """Publish + emit per-frame Hs COGs out-of-band so the web scrubber forms.

    Each frame COG is routed through ``publish_layer`` (render chokepoint) so it
    carries a renderable URL before ``add_loaded_layer``. The "Wave height step N"
    name token is preserved so the web ``detectSequentialGroups`` groups them. A
    frame that fails to publish is HONESTLY DROPPED. Returns the number emitted
    (0 when no emitter is bound). Never raises (mirrors GeoClaw).
    """
    if not frame_layers or emitter is None:
        if frame_layers:
            logger.info(
                "model_swan_wave_field: %d animation frames available but no emitter "
                "bound - frames not emitted.",
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
                    style_preset=lyr.style_preset or SWAN_WAVE_HEIGHT_STYLE_PRESET,
                )
            except PublishLayerError as exc:
                logger.warning(
                    "model_swan_wave_field: publish_layer FAILED for frame "
                    "layer_id=%s error_code=%s (%s) - dropping this frame.",
                    lyr.layer_id,
                    exc.error_code,
                    exc,
                )
                continue
            emit_layer = WaveFieldLayerURI(
                layer_id=lyr.layer_id,
                name=lyr.name,
                layer_type=lyr.layer_type,
                uri=frame_uri,
                style_preset=lyr.style_preset or SWAN_WAVE_HEIGHT_STYLE_PRESET,
                role=lyr.role,
                units=lyr.units,
                bbox=lyr.bbox,
                max_hs_m=lyr.max_hs_m,
                mean_tp_s=lyr.mean_tp_s,
                mean_dir_deg=lyr.mean_dir_deg,
                wave_area_km2=lyr.wave_area_km2,
                mean_hs_m=lyr.mean_hs_m,
                mode=lyr.mode,
            )
        try:
            safe = emit_layer_uri(emit_layer)
            if safe is not None:
                await emitter.add_loaded_layer(safe)
                emitted += 1
        except Exception as exc:  # noqa: BLE001 -- never break the solve
            logger.warning(
                "model_swan_wave_field: frame add_loaded_layer failed for %s: %s",
                emit_layer.layer_id,
                exc,
            )
    if emitted:
        logger.info(
            "model_swan_wave_field: emitted %d/%d animation frames as a sequential "
            "group (run_id=%s)",
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


def _download_batch_swan_outputs(run_id: str) -> str:
    """Download the Batch SWAN output to a tmp dir for postprocess.

    The SWAN Batch worker uploads its ``swan_out.mat`` (+ PRINT / Errfile
    diagnostics) under ``s3://<runs_bucket>/<run_id>/`` and records their URIs in
    completion.json ``output_uris``. We re-read completion.json (small, already on
    S3) to find the output keys, download them via the SAME boto3 client the solver
    dispatch uses (no new client), and return the local dir holding the output.

    Raises:
        SwanWorkflowError("SWAN_BATCH_OUTPUT_MISSING"): the completed run produced
            no downloadable swan_out.mat (a 'complete' solve with no wave output is
            a real failure -- never a silent dead-end).
    """
    from trid3nt_server.data.simulation.solver.solver import (
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
            if base.endswith(".mat") or base in {"PRINT", "Errfile", "deck_manifest.json"}:
                keys.append(key)
    if not keys:
        # Defensive fallback: list the runs prefix for the .mat output.
        try:
            resp = s3.list_objects_v2(Bucket=runs_bucket, Prefix=f"{run_id}/")
            for obj in resp.get("Contents", []) or []:
                k = obj.get("Key", "")
                base = k.rsplit("/", 1)[-1]
                if base.endswith(".mat") or base in {"PRINT", "Errfile"}:
                    keys.append(k)
        except Exception as exc:  # noqa: BLE001
            logger.warning("SWAN output list fallback failed: %s", exc)

    tmp_dir = tempfile.mkdtemp(prefix=f"swan-batch-out-{run_id}-")
    out_sub = Path(tmp_dir)

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
                "SWAN Batch output download failed s3://%s/%s: %s",
                runs_bucket,
                key,
                exc,
            )

    has_mat = any(
        p.suffix.lower() == ".mat" for p in out_sub.iterdir() if p.is_file()
    )
    if not has_mat:
        _cleanup_dir(tmp_dir)
        raise SwanWorkflowError(
            "SWAN_BATCH_OUTPUT_MISSING",
            message=(
                f"SWAN Batch run {run_id} completed but produced no downloadable "
                f"swan_out.mat under s3://{runs_bucket}/{run_id}/ "
                f"(downloaded {downloaded} output objects)"
            ),
            details={"run_id": run_id, "runs_bucket": runs_bucket},
        )

    return tmp_dir
