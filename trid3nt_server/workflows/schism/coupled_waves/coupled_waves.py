"""Engine template ``schism_coupled_waves`` -- SCHISM+WWM two-way wave-current
coupling on the Duck NC FRF validation geometry (engine #12 second archetype,
0129).

The LLM-facing exposure of SCHISM's REFINEMENT-GRADE coupled wave-current solve:
the semi-implicit unstructured-grid hydro core two-way coupled to WWM-III spectral
waves with the GOTM k-epsilon turbulence closure (``itur=3`` -- the faithful
config the ``pschism_WWM_GOTM_TVD-VL`` build variant exists for). ONE bundled,
self-contained published case: the SCHISM verification Test_WWM_Duck deck (US Army
Corps Field Research Facility, Duck NC, an energetic 12 Oct 1994 nor'easter), whose
wave boundary forcing (a non-parametric spectrum from the 8m-array observations)
SHIPS WITH THE CASE -- no WW3/parametric build. The deliverable is the max
significant-wave-height (Hs) surface + a CROSS-SHORE Hs/Tp VERIFICATION against the
bundled pressure-transducer transect (the observed offshore-shoaling-breaking
transformation) -- the V&V chart is the acceptance artifact.

Determinism boundary (invariant 1): every Hs/Tp number the agent narrates comes
from the typed ``SchismWaveLayerURI`` fields the postprocess computed from
``sigWaveHeight`` / ``peakPeriod`` -- never free-generated. SCHISM is LOCAL-DOCKER
ONLY, so the composer dispatches through the generic run_solver seam (the ``wwm``
executable variant).
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from trid3nt_contracts import new_ulid
from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.schism_contracts import (
    SCHISM_INPUT_INVALID,
    SCHISM_SOLVE_FAILED,
    SchismWaveLayerURI,
)
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.data import register_tool
from trid3nt_server.gates.input_review import gate_input_review
from trid3nt_server.workflows.schism._template_card import TemplateCard

logger = logging.getLogger(
    "trid3nt_server.workflows.schism.coupled_waves.coupled_waves"
)

__all__ = [
    "schism_coupled_waves",
    "model_schism_coupled_waves",
    "SchismWaveScenarioError",
    "TEMPLATE_CARD",
]

#: The LOUD fidelity + published-fixture honesty floor stamped on every result.
_DUCK_NOTE: str = (
    "REFINEMENT-GRADE COUPLED WAVE-CURRENT hindcast on the Duck NC FRF VALIDATION "
    "geometry (SCHISM hydro core two-way coupled to WWM-III spectral waves + the "
    "GOTM k-epsilon closure, itur=3): a published SCHISM verification case (12 Oct "
    "1994 nor'easter), forced by the bundled 8m-array non-parametric wave spectrum. "
    "This is the FRF validation mesh with bundled field observations -- it proves "
    "the coupled solver reproduces the observed cross-shore wave transformation, "
    "NOT waves at an arbitrary AOI. For standalone spectral wave fields at another "
    "US coastal AOI use swan_wave_field; for fast flood screening use sfincs_flood."
)


#: The parametric-forcing fidelity note: the geometry is the Duck FRF
#: validation mesh but the boundary is a PRESCRIBED sea state, not the observed event.
_PARAMETRIC_NOTE_TMPL: str = (
    "REFINEMENT-GRADE COUPLED WAVE-CURRENT run on the Duck NC FRF mesh (SCHISM hydro "
    "core two-way coupled to WWM-III + GOTM itur=3), forced by a PRESCRIBED offshore "
    "parametric JONSWAP spectrum (Hs={hs_m:g} m, Tp={tp_s:g} s, dir={dir_deg:g} deg, "
    "spread={spread_deg:g} deg) -- NOT the observed 12 Oct 1994 event, so there is no "
    "field-gauge V&V here; the deliverable is the nearshore wave transformation + "
    "wave setup THIS offshore sea state drives on the FRF geometry. For the validated "
    "observed-event run omit the wave knobs. For standalone spectral waves at another "
    "US AOI use swan_wave_field; for fast flood screening use sfincs_flood."
)


def _forcing_note(parametric: bool, wave_forcing: dict[str, float] | None) -> str:
    """The honesty-floor note stamped on the result (observed vs parametric forcing)."""
    if parametric and wave_forcing:
        return _PARAMETRIC_NOTE_TMPL.format(**wave_forcing)
    return _DUCK_NOTE


class SchismWaveScenarioError(RuntimeError):
    """Raised when the coupled-wave chain fails fatally before producing a layer."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


#: Parametric JONSWAP defaults for the unset knobs (the Duck fixture's own values).
_PARAMETRIC_DEFAULTS = {"hs_m": 2.0, "tp_s": 12.0, "dir_deg": 80.0, "spread_deg": 30.0}


def _resolve_wave_forcing(
    hs: float | None, tp: float | None, direction: float | None, spread: float | None,
) -> dict[str, float] | None:
    """Return a parametric-JONSWAP forcing dict, or ``None`` for the observed spectrum.

    Setting ANY of the four knobs selects the prescribed parametric boundary; the
    unset knobs fall back to the Duck fixture's own JONSWAP values. Validates the
    physical ranges (raises ``ValueError`` on garbage)."""
    if hs is None and tp is None and direction is None and spread is None:
        return None
    out = dict(_PARAMETRIC_DEFAULTS)
    if hs is not None:
        hs = float(hs)
        if not (0.0 < hs <= 12.0):
            raise ValueError("significant_wave_height_m must be in (0, 12] m")
        out["hs_m"] = hs
    if tp is not None:
        tp = float(tp)
        if not (1.0 < tp <= 25.0):
            raise ValueError("peak_period_s must be in (1, 25] s")
        out["tp_s"] = tp
    if direction is not None:
        direction = float(direction)
        if not (0.0 <= direction < 360.0):
            raise ValueError("mean_direction_deg must be in [0, 360) degrees")
        out["dir_deg"] = direction
    if spread is not None:
        spread = float(spread)
        if not (0.0 < spread <= 90.0):
            raise ValueError("directional_spread must be in (0, 90] degrees")
        out["spread_deg"] = spread
    return out


TEMPLATE_CARD = TemplateCard(
    question=(
        "two-way COUPLED WAVE-CURRENT nearshore simulation (SCHISM + WWM-III spectral "
        "waves + GOTM turbulence): a max significant-wave-height (Hs) surface + a "
        "cross-shore Hs/Tp verification against field gauges, on the bundled Duck NC "
        "FRF published validation case (self-contained wave-spectrum forcing). Or "
        "PRESCRIBE an offshore parametric JONSWAP spectrum (Hs/Tp/direction/spread) "
        "to see the nearshore wave transformation + wave setup it drives"
    ),
    required_inputs=[],  # the bundled Duck case is fully self-contained
    knobs=(
        "sim_hours, input_mode, significant_wave_height_m, peak_period_s, "
        "mean_direction_deg, directional_spread"
    ),
)


_WAVE_METADATA = AtomicToolMetadata(
    name="schism_coupled_waves",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="schism",
    tier="template",
)


@register_tool(
    _WAVE_METADATA,
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def schism_coupled_waves(
    sim_hours: float = 4.0,
    input_mode: str | None = None,
    significant_wave_height_m: float | None = None,
    peak_period_s: float | None = None,
    mean_direction_deg: float | None = None,
    directional_spread: float | None = None,
    **_extra_ignored: Any,
) -> SchismWaveLayerURI | dict[str, Any]:
    """A REFINEMENT-GRADE two-way COUPLED WAVE-CURRENT nearshore simulation (SCHISM+WWM).

    Fidelity: SCHISM (the semi-implicit cross-scale unstructured-grid hydrodynamic
    core behind NOAA STOFS) two-way coupled to WWM-III spectral waves with the GOTM
    k-epsilon turbulence closure (itur=3) -- refinement-grade nearshore
    wave-current dynamics. Returns a MAX significant-wave-height (Hs) surface + the
    nearshore wave SETUP, plus (observed-forcing mode) a cross-shore Hs/Tp
    VERIFICATION vs bundled field gauges.

    THE tool for "run a coupled wave-current model", "SCHISM WWM coupled waves",
    "nearshore wave transformation / shoaling / breaking with SCHISM", "wave setup
    from an offshore wave spectrum", "wave-current interaction on an unstructured
    coastal mesh", "spectral waves coupled to a hydrodynamic model". The bundled
    case is the Duck NC FRF validation deck (12 Oct 1994 nor'easter); its observed
    wave-spectrum boundary ships with the case. TWO forcing modes on that geometry:

      * observed-spectrum (default, no wave knobs): the bundled non-parametric
        8m-array spectrum -- the validation case with the cross-shore gauge V&V.
      * PARAMETRIC JONSWAP (set any of the four wave knobs): a prescribed offshore
        JONSWAP boundary (Hs / Tp / direction / spread) -- answers "given THIS
        offshore sea state, what nearshore Hs + wave setup result?".

    Do NOT use this for:
        - STANDALONE spectral wave fields at an arbitrary US coastal AOI (no
          current coupling) -- use ``swan_wave_field`` (SWAN).
        - FAST arbitrary-AOI flood screening -- use ``sfincs_flood`` (SFINCS).
        - BAROTROPIC tides / max water-surface elevation with no waves -- use
          ``schism_tidal_hydro``.
        - Riverine flood (``hecras_riverine_flood``), urban drainage
          (``swmm_urban_flood``), or tsunami (``geoclaw_inundation``).

    Params:
        sim_hours: coupled-run length in hours within the bundled case window
            (default 4.0 -- the full published case; the wave field spins up within
            the first hour, so a shorter window is a cheaper smoke). Clamped
            (0.5, 4.0].
        input_mode: run-mode lever. ``"user_gated"`` reviews the coupled
            forcing + published-fixture basis before solving; ``"auto"`` (default)
            proceeds labeled.
        significant_wave_height_m: offshore boundary Hs (m). Setting ANY of the four
            wave knobs switches the WWM boundary to a PRESCRIBED parametric JONSWAP
            spectrum (default 2.0 m for the unset knobs). Clamped (0, 12].
        peak_period_s: offshore boundary peak period Tp (s) for the JONSWAP spectrum
            (default 12.0). Clamped (1, 25].
        mean_direction_deg: offshore mean wave direction (nautical degrees, default
            80.0 -- the Duck shore-normal). Clamped [0, 360).
        directional_spread: offshore directional spread (degrees, default 30.0).
            Clamped (0, 90].

    Returns:
        On success: ``SchismWaveLayerURI`` (``LayerURI`` subtype) -- the emitter
        loads the max-Hs COG beside the SCHISM+WWM mesh preview. Carries
        ``hs_max_m`` / ``hs_mean_m`` / ``tp_max_s`` / ``offshore_hs_m`` /
        ``wave_setup_m`` / ``n_nodes`` / ``sim_hours``, the boundary-forcing echoes
        ``forcing_mode`` / ``forced_hs_m`` / ``forced_tp_s`` / ``forced_dir_deg`` /
        ``forced_spread_deg``, and (observed-forcing mode) the cross-shore V&V
        fields ``vv_hs_rmse_m`` / ``vv_hs_bias_m`` / ``vv_hs_corr`` /
        ``vv_offshore_hs_{obs,mod}_m`` (narrate these typed numbers only --
        invariant 1).
        On failure: dict with ``status="error"`` + ``error_code`` + ``error_message``.

    ``cacheable=False``, ``ttl_class="live-no-cache"``,
    ``source_class="workflow_dispatch"``.
    """
    try:
        sim_hours = float(sim_hours)
    except (TypeError, ValueError):
        return {"status": "error", "error_code": SCHISM_INPUT_INVALID,
                "error_message": "sim_hours must be a number"}
    if not (0.5 <= sim_hours <= 4.0):
        return {"status": "error", "error_code": SCHISM_INPUT_INVALID,
                "error_message": "sim_hours in (0.5, 4.0] (the bundled Duck case window)"}

    try:
        wave_forcing = _resolve_wave_forcing(
            significant_wave_height_m, peak_period_s, mean_direction_deg, directional_spread,
        )
    except ValueError as exc:
        return {"status": "error", "error_code": SCHISM_INPUT_INVALID, "error_message": str(exc)}

    logger.info("schism_coupled_waves sim_hours=%.3g mode=%s forcing=%s",
                sim_hours, input_mode, wave_forcing)
    try:
        result = await model_schism_coupled_waves(
            sim_hours=sim_hours, input_mode=input_mode, wave_forcing=wave_forcing,
        )
        if isinstance(result, dict):
            return result
        logger.info(
            "schism_coupled_waves complete layer_id=%s hs_max=%.3g tp_max=%s vv_rmse=%s uri=%s",
            result.layer_id, result.hs_max_m, result.tp_max_s, result.vv_hs_rmse_m, result.uri,
        )
        return result
    except asyncio.CancelledError:
        raise
    except SchismWaveScenarioError as exc:
        logger.warning("schism_coupled_waves failed: %s (%s)", exc.error_code, exc)
        return {"status": "error", "error_code": exc.error_code, "error_message": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.exception("schism_coupled_waves unexpected failure")
        return {"status": "error", "error_code": "SCHISM_INTERNAL_ERROR", "error_message": str(exc)}


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
from trid3nt_server.data.publish_layer.publish_layer import (
    PublishLayerError,
    publish_layer,
)
from trid3nt_server.emission.layer_uri_emit import publish_input_layer
from trid3nt_server.workflows.schism import deck_authoring
from trid3nt_server.workflows.schism import postprocess_schism as pp
from trid3nt_server.workflows.schism.run_schism import SCHISM_WAVE_SOLVER_NAME


def _cache_bucket() -> str:
    b = (os.environ.get("TRID3NT_CACHE_BUCKET") or "").strip()
    if not b:
        raise SchismWaveScenarioError(
            SCHISM_SOLVE_FAILED, "TRID3NT_CACHE_BUCKET must be set to stage the SCHISM manifest."
        )
    return b


def _stage_wave_manifest(
    deck_files: list[Path], run_tag: str, *, ncompute: int, nscribe: int
) -> str:
    """Upload the staged Duck deck as manifest inputs[] (variant='wwm'); return its uri."""
    import json as _json
    from trid3nt_server.data.simulation.solver.solver import _get_s3_client

    cache_bucket = _cache_bucket()
    s3 = _get_s3_client()
    inputs = []
    for f in deck_files:
        key = f"schism/{run_tag}/{f.name}"
        with open(f, "rb") as fh:
            s3.put_object(Bucket=cache_bucket, Key=key, Body=fh.read())
        inputs.append({"gs_uri": f"s3://{cache_bucket}/{key}", "dest": f.name})
    manifest = {
        "variant": "wwm",
        "ncompute": int(ncompute),
        "nscribe": int(nscribe),
        "run_id": run_tag,
        "inputs": inputs,
        "schism_args": [],
        "outputs": ["outputs/*.nc", "schism_metrics.json"],
    }
    key = f"schism/{run_tag}/manifest.json"
    s3.put_object(Bucket=cache_bucket, Key=key,
                  Body=_json.dumps(manifest, indent=2).encode("utf-8"),
                  ContentType="application/json")
    return f"s3://{cache_bucket}/{key}"


def _download_run_output(run_id: str, rel_key: str) -> str | None:
    from trid3nt_server.data.simulation.solver.solver import (
        _get_runs_bucket, _get_s3_client,
    )
    try:
        s3 = _get_s3_client()
        obj = s3.get_object(Bucket=_get_runs_bucket(), Key=f"{run_id}/{rel_key}")
        tmp = tempfile.NamedTemporaryFile(suffix="_" + Path(rel_key).name, delete=False)
        tmp.write(obj["Body"].read())
        tmp.close()
        return tmp.name
    except Exception as exc:  # noqa: BLE001
        logger.info("schism waves: run output miss %s/%s: %s", run_id, rel_key, exc)
        return None


def _runs_uri(run_id: str, rel_key: str) -> str:
    from trid3nt_server.data.simulation.solver.solver import _get_runs_bucket
    return f"s3://{_get_runs_bucket()}/{run_id}/{rel_key}"


async def model_schism_coupled_waves(
    *, sim_hours: float, input_mode: str | None,
    wave_forcing: dict[str, float] | None = None,
) -> SchismWaveLayerURI | dict[str, Any]:
    """Stage the Duck deck -> input gate -> coupled solve -> Hs postprocess + V&V -> publish."""
    emitter = current_emitter()
    begin_substeps(emitter, 3)  # run_solver + postprocess + publish

    workdir = Path(tempfile.mkdtemp(prefix="schism-wwm-deck-"))
    parametric = wave_forcing is not None

    # --- Stage 1: stage the transformed Duck deck ----------------------------- #
    deck_files, ncompute, nscribe = deck_authoring.stage_wwm_duck_deck(
        workdir, sim_hours=sim_hours, wave_forcing=wave_forcing
    )
    if parametric:
        wave_boundary_entry = SyntheticInput(
            param="wave_boundary",
            value=(f"parametric JONSWAP Hs={wave_forcing['hs_m']:g} m "
                   f"Tp={wave_forcing['tp_s']:g} s dir={wave_forcing['dir_deg']:g} deg "
                   f"spread={wave_forcing['spread_deg']:g} deg"),
            basis="user",
            note="prescribed offshore JONSWAP spectrum on the Duck FRF geometry (WWM LBCWA=T)")
    else:
        wave_boundary_entry = SyntheticInput(
            param="wave_boundary", value="8m-array non-parametric spectrum (bundled)",
            basis="default_demo", consequence="scenario",
            real_source_if_any="DUCK94 8m-array observed wave spectra",
            note="the bundled DUCK94_wave_spectra_8m_array.nc spectral boundary")
    review_entries = [
        SyntheticInput(param="mesh_source", value="bundled_wwm_duck",
                       basis="default_demo", consequence="aoi",
                       note="SCHISM Test_WWM_Duck FRF validation mesh (33586 elements / 17054 nodes)"),
        wave_boundary_entry,
        SyntheticInput(param="turbulence_closure", value="GOTM k-epsilon (itur=3, KE/KC)",
                       basis="default_demo", consequence="numerical",
                       note="the faithful coupled config the WWM+GOTM binary exists for"),
        SyntheticInput(param="sim_hours", value=round(sim_hours, 3), units="h",
                       basis="user" if sim_hours != 4.0 else "default_demo", consequence="scenario",
                       note="coupled-run window within the 4-hour published case"),
    ]

    # --- Stage 2: the input-review gate ---------------------------- #
    review = await gate_input_review(
        tool_name="schism_coupled_waves", mode=input_mode, entries=review_entries,
        params={"sim_hours": sim_hours},
    )
    if not review.proceed:
        return {"status": "error", "error_code": "SCHISM_INPUT_REVIEW_CANCELLED",
                "error_message": review.cancel_reason or "input review not approved; the solver did not run"}

    # --- Stage 3: stage manifest + dispatch the coupled solve ----------------- #
    run_tag = new_ulid()
    manifest_uri = await asyncio.to_thread(
        _stage_wave_manifest, deck_files, run_tag, ncompute=ncompute, nscribe=nscribe
    )
    logger.info("model_schism_coupled_waves staged manifest run_tag=%s files=%d uri=%s",
                run_tag, len(deck_files), manifest_uri)

    from trid3nt_server.data.simulation.solver.solver import (
        run_solver, wait_for_completion,
    )
    handle = run_solver(solver=SCHISM_WAVE_SOLVER_NAME, model_setup_uri=manifest_uri,
                        compute_class="medium")
    sim_step_id = await mint_dispatch_and_sim_cards(
        emitter=emitter, solver=SCHISM_WAVE_SOLVER_NAME, handle=handle, compute_class="medium",
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
        raise SchismWaveScenarioError(
            SCHISM_SOLVE_FAILED,
            "SCHISM+WWM coupled solve did not complete "
            f"(status={getattr(run_result, 'status', None)}, "
            f"error_code={getattr(run_result, 'error_code', None)}): "
            f"{getattr(run_result, 'error_message', '') or getattr(run_result, 'cancellation_reason', '') or ''}",
        )
    batch_run_id = getattr(run_result, "run_id", None) or run_tag

    # --- Stage 4: download out2d, postprocess Hs/Tp --------------------------- #
    out2d_local = await asyncio.to_thread(_download_run_output, batch_run_id, "outputs/out2d_1.nc")
    if out2d_local is None:
        raise SchismWaveScenarioError(SCHISM_SOLVE_FAILED,
                                      "SCHISM+WWM completed but outputs/out2d_1.nc was not downloadable")
    out2d_uri = _runs_uri(batch_run_id, "outputs/out2d_1.nc")
    forcing_meta = (
        {"forcing_mode": "parametric_jonswap",
         "forced_hs_m": wave_forcing["hs_m"], "forced_tp_s": wave_forcing["tp_s"],
         "forced_dir_deg": wave_forcing["dir_deg"],
         "forced_spread_deg": wave_forcing["spread_deg"]}
        if parametric else {"forcing_mode": "observed_spectrum"}
    )
    try:
        async with substep(emitter, "postprocess_schism_waves"):
            layers, metrics = await asyncio.to_thread(
                pp.postprocess_schism_waves, out2d_local, out2d_uri, run_id=batch_run_id,
                sim_hours=sim_hours, fallback_note=_forcing_note(parametric, wave_forcing),
                forcing=forcing_meta,
            )
    except pp.PostprocessSchismError as exc:
        raise SchismWaveScenarioError(exc.error_code, str(exc)) from exc

    wave = layers[0]
    assert isinstance(wave, SchismWaveLayerURI)
    mesh_layer = layers[1] if len(layers) > 1 else None

    # --- Stage 4b: the cross-shore Hs/Tp V&V vs the bundled gauges ------------- #
    # The bundled gauges record the REAL 12 Oct 1994 event; a PRESCRIBED parametric
    # sea state does not reproduce that record, so the gauge V&V applies only to the
    # observed-spectrum forcing (comparing a synthetic forcing to the observed
    # transect would be a dishonest V&V).
    mat_path = deck_authoring.wwm_duck_fixture_dir() / "Data" / "timeseries_data_1010_to_1410_004Hz_025Hz.mat"
    vv = None
    if not parametric and mat_path.exists():
        vv = await asyncio.to_thread(pp.verify_cross_shore_waves, out2d_local, mat_path)
        if vv:
            wave = wave.model_copy(update={
                "vv_n_gauges": vv.get("n_gauges"),
                "vv_hs_rmse_m": vv.get("hs_rmse_m"),
                "vv_hs_bias_m": vv.get("hs_bias_m"),
                "vv_hs_corr": vv.get("hs_corr"),
                "vv_offshore_hs_obs_m": vv.get("offshore_hs_obs_m"),
                "vv_offshore_hs_mod_m": vv.get("offshore_hs_mod_m"),
                "vv_tp_rmse_s": vv.get("tp_rmse_s"),
            })
            logger.info(
                "Duck cross-shore V&V: n=%s Hs RMSE=%.3f m bias=%.3f m corr=%.3f offshore obs/mod=%.2f/%.2f",
                vv.get("n_gauges"), vv.get("hs_rmse_m"), vv.get("hs_bias_m"), vv.get("hs_corr"),
                vv.get("offshore_hs_obs_m") or float("nan"), vv.get("offshore_hs_mod_m") or float("nan"),
            )
    try:
        Path(out2d_local).unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass

    # --- Stage 5: publish the max-Hs COG (render chokepoint) ------------------ #
    async with substep(emitter, "publish_layer"):
        wave = await asyncio.to_thread(_publish_wave_layer, wave, review.entries)

    # --- Best-effort: the SCHISM+WWM mesh preview ----------------------------- #
    if mesh_layer is not None:
        try:
            await publish_input_layer(emitter, mesh_layer, role="context")
        except Exception as exc:  # noqa: BLE001
            logger.warning("schism wave mesh preview emit skipped: %s", exc)

    # --- Best-effort: the cross-shore Hs/Tp verification chart (the artifact) -- #
    if emitter is not None and vv and vv.get("gauges"):
        try:
            await _maybe_emit_cross_shore_chart(emitter, vv)
        except Exception as exc:  # noqa: BLE001
            logger.warning("schism cross-shore chart skipped: %s", exc)

    # --- AUTHORITATIVE LAST zoom-to -------------------------------------------- #
    if emitter is not None and wave.bbox:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(wave.bbox)})
        except Exception as exc:  # noqa: BLE001
            logger.warning("schism wave zoom-to failed: %s", exc)

    return wave


def _publish_wave_layer(
    wave: SchismWaveLayerURI, synthetic_inputs: list[SyntheticInput]
) -> SchismWaveLayerURI:
    """Publish the max-Hs COG through publish_layer + stamp provenance."""
    out = wave
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
        logger.warning("schism wave publish_layer FAILED layer_id=%s (%s) - returning raw COG",
                       out.layer_id, exc)
        return out


async def _maybe_emit_cross_shore_chart(emitter: Any, vv: dict[str, Any]) -> None:
    """The cross-shore Hs verification chart -- modeled vs measured gauges (THE artifact)."""
    if not hasattr(emitter, "emit_chart"):
        return
    from trid3nt_server.data.processing.charts_common import build_chart_payload

    values: list[dict[str, Any]] = []
    for g in vv["gauges"]:
        if g.get("hs_obs") is not None:
            values.append({"x_m": g["x"], "Hs_m": g["hs_obs"], "series": "measured (8m-array transect)"})
        if g.get("hs_mod") is not None:
            values.append({"x_m": g["x"], "Hs_m": g["hs_mod"], "series": "SCHISM+WWM coupled (itur=3)"})
    spec = {
        "data": {"values": values},
        "mark": {"type": "line", "point": True},
        "encoding": {
            "x": {"field": "x_m", "type": "quantitative", "title": "cross-shore position xFRF (m)"},
            "y": {"field": "Hs_m", "type": "quantitative", "title": "significant wave height Hs (m)"},
            "color": {"field": "series", "type": "nominal", "title": ""},
        },
    }
    rmse = vv.get("hs_rmse_m")
    corr = vv.get("hs_corr")
    caption = (
        "Cross-shore significant-wave-height transect: the SCHISM+WWM coupled model "
        f"(GOTM itur=3) vs the bundled Duck FRF pressure-transducer gauges "
        f"(Hs RMSE={rmse} m, correlation={corr} across {vv.get('n_gauges')} gauges). "
        "A hindcast comparison of the observed offshore-shoaling-breaking wave "
        "transformation -- the acceptance V&V."
    )
    payload = build_chart_payload(
        vega_lite_spec=spec,
        title="Duck FRF cross-shore Hs: SCHISM+WWM coupled vs field gauges",
        caption=caption,
    )
    await emitter.emit_chart(payload)
