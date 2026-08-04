"""``model_asr_scenario``  -  MODFLOW aquifer-storage-&-recovery (ASR) composer.

The end-to-end higher-order workflow for the MODFLOW ``ASR``
archetype: it turns a place (or AOI point) + an ASR well (location + injection /
recovery rates + cycle schedule) into a rendered ASR head layer  -  the cyclic
inject-rise / recover-fall sawtooth at the well and the recovery efficiency (the
fraction of injected water recovered). It mirrors the chain shape of
``model_sustainable_yield_scenario`` (sibling well-driven archetype): a single
WEL well flips sign per the inject/recover schedule over a transient run, and the
seasonal head series + the budget-split efficiency ARE the deliverable.

Canonical real-world pipeline mirrored here (an aquifer storage & recovery
operational / recovery-efficiency analysis):

    resolve the AOI point (geocode a place, or take an explicit lat/lon)
        -> the user supplies the ASR well + injection + recovery rates (NEVER
           fabricated  -  a missing well/rate is a typed USER_INPUT_REQUIRED failure)
        -> assemble MODFLOWRunArgs(archetype="ASR", well, inject, recover, ...)
        -> run_modflow_archetype_job (GWF transient seasonal-WEL deck -> mf6 -> ASR)
        -> ASRLayerURI (recovery_efficiency + head_timeseries sawtooth)

Invariants (same set as model_sustainable_yield_scenario):
- **1 / 2 / 8: preserve** (typed numbers, deterministic composition, cancellable).
- **9. No fabricated model inputs.** An ``ASR`` run with no well location OR no
  injection / recovery rate returns a typed ``USER_INPUT_REQUIRED`` failed
  envelope rather than inventing a well  -  the honesty floor: a "modeled" envelope
  with empty layers never reads ok.
- **10. Minimal parameter surface: preserves.** Intent (place + well + rates +
  cycle schedule) is exposed; the grid + demo aquifer K / Sy are derived defaults.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import Field

from trid3nt_contracts.common import GraceModel, SyntheticInput
from trid3nt_contracts.modflow_contracts import (
    ASRLayerURI,
    DEFAULT_AQUIFER_K_MS,
    MODFLOWRunArgs,
)
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.emission.pipeline_emitter import begin_substeps, current_emitter, emit_chart_payloads
from trid3nt_server.agent.tools import register_tool
from trid3nt_server.agent.workflows.modflow._template_card import TemplateCard
from trid3nt_server.agent.workflows.modflow.sustainable_yield.sustainable_yield import (
    _aquifer_overrides,
    _coerce_optional_latlon,
    _resolve_aoi_point,
    _run_archetype,
)

logger = logging.getLogger("trid3nt_server.agent.workflows.modflow.asr.asr")

__all__ = [
    "ASRResult",
    "model_asr_scenario",
    "modflow_asr",
    "ASRScenarioError",
    "ASRInputError",
]


# --------------------------------------------------------------------------- #
# Result envelope
# --------------------------------------------------------------------------- #


class ASRResult(GraceModel):
    """Return type for ``model_asr_scenario``.

    Bundles the ASR layer + the derived args + a narration summary dict.
    Invariant 1: every narrated number is a typed field  -  ``asr_layer`` carries
    ``recovery_efficiency`` + the well-head ``head_timeseries`` sawtooth.
    """

    schema_version: str = "v1"

    asr_layer: ASRLayerURI
    derived_params: dict[str, Any] = Field(default_factory=dict)
    summary: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Typed errors
# --------------------------------------------------------------------------- #


class ASRScenarioError(RuntimeError):
    """Base class for ``model_asr_scenario`` failures."""

    error_code: str = "ASR_SCENARIO_ERROR"
    retryable: bool = False


class ASRInputError(ASRScenarioError):
    """Caller supplied invalid / missing AOI or well input (honesty gate)."""

    error_code = "ASR_INPUT_INVALID"


# --------------------------------------------------------------------------- #
# Engine-output chart (mirror the head-decline emit): ASR seasonal-head line.
# --------------------------------------------------------------------------- #


async def _emit_asr_chart(layer: ASRLayerURI) -> None:
    """Side-emit the ASR well-head inject/recover sawtooth line (no-op safe).

    Builds a head-vs-time line from the typed ``ASRLayerURI.head_timeseries``
    (real solver output  -  the well head over the inject/recover cycle). The
    builder emits nothing for an absent / single-point series (the honesty floor).
    """
    from trid3nt_server.agent.tools.processing.charts_common import build_head_series_chart

    series = getattr(layer, "head_timeseries", None)
    if not series:
        return
    chart = build_head_series_chart(
        head_timeseries=list(series),
        title="ASR well head over inject/recover cycle",
        y_title="well head (m)",
        caption_label="inject-rise / recover-fall sawtooth at the ASR well",
        source_layer_uri=getattr(layer, "uri", None),
    )
    await emit_chart_payloads(chart)


# --------------------------------------------------------------------------- #
# The composer
# --------------------------------------------------------------------------- #


async def model_asr_scenario(
    location: str | None = None,
    aoi_latlon: tuple[float, float] | None = None,
    *,
    well_location_latlon: tuple[float, float] | None = None,
    injection_rate_m3_day: float | None = None,
    recovery_rate_m3_day: float | None = None,
    injection_months: int | None = None,
    recovery_months: int | None = None,
    n_cycles: int | None = None,
    aquifer_k_ms: float | None = None,
    porosity: float | None = None,
    aquifer_sy: float | None = None,
    compute_class: str = "standard",
    pipeline_emitter: Any | None = None,
) -> ASRResult:
    """Compose place/AOI + an ASR well -> MODFLOW inject/recover -> ASRLayerURI.

    Args:
        location: a place name (geocoded). Supply this OR ``aoi_latlon``.
        aoi_latlon: an explicit ``(lat, lon)`` AOI point.
        well_location_latlon: the ASR well ``(lat, lon)``. REQUIRED  -  a missing
            well is a typed USER_INPUT_REQUIRED failure (never invented).
        injection_rate_m3_day: injection rate as a POSITIVE magnitude (m^3/day).
            REQUIRED  -  the adapter applies the MF6 WEL sign (inject = +).
        recovery_rate_m3_day: recovery (extraction) rate as a POSITIVE magnitude
            (m^3/day). REQUIRED  -  the adapter applies the WEL sign (recover = -).
        injection_months / recovery_months / n_cycles: cycle schedule controls.
            Demo defaults applied by the adapter when None.
        aquifer_k_ms / porosity / aquifer_sy: optional demo-aquifer overrides.
        compute_class: FR-CE-3 compute class.
        pipeline_emitter: optional PipelineEmitter for live progress cards.

    Returns:
        ``ASRResult`` with the ``ASRLayerURI`` + derived args + a narration
        summary dict.

    Raises:
        ASRInputError: missing/invalid AOI, well, or rates (the honesty gate).
        ASRScenarioError: a required step (geocode / solver) failed.
        Propagates ``asyncio.CancelledError`` (Invariant 8).
    """
    # --- Honesty gate (Invariant 9): never fabricate the well / rates ----------
    if (
        well_location_latlon is None
        or injection_rate_m3_day is None
        or recovery_rate_m3_day is None
    ):
        raise ASRInputError(
            "ASR requires a well location (well_location_latlon) AND both an "
            "injection rate (injection_rate_m3_day) and a recovery rate "
            "(recovery_rate_m3_day). These are user inputs and are never invented; "
            "ask the user for the ASR well + the inject / recover rates."
        )

    _planned = 1
    has_loc = bool(location and location.strip())
    if has_loc:
        _planned += 1
    begin_substeps(current_emitter(), _planned)

    lat, lon, location_name = await _resolve_aoi_point(
        location, aoi_latlon, pipeline_emitter=pipeline_emitter
    )

    try:
        wlat = float(well_location_latlon[0])
        wlon = float(well_location_latlon[1])
    except Exception as exc:  # noqa: BLE001
        raise ASRInputError(
            f"invalid well_location_latlon (expected (lat, lon)): {exc}"
        ) from exc

    # Both rates are passed as POSITIVE magnitudes; the adapter applies the MF6
    # WEL sign (inject = +, recover = -). Normalize to magnitude here defensively.
    inj = abs(float(injection_rate_m3_day))
    rec = abs(float(recovery_rate_m3_day))

    try:
        run_args = MODFLOWRunArgs(
            spill_location_latlon=(lat, lon),
            contaminant="n/a",
            release_rate_kg_s=1.0,
            duration_days=1.0,
            archetype="ASR",
            well_location_latlon=(wlat, wlon),
            injection_rate_m3_day=inj,
            recovery_rate_m3_day=rec,
            injection_months=injection_months,
            recovery_months=recovery_months,
            n_cycles=n_cycles,
            **_aquifer_overrides(aquifer_k_ms, porosity, aquifer_sy, None),
        )
    except Exception as exc:  # noqa: BLE001
        raise ASRInputError(f"invalid ASR run arguments: {exc}") from exc

    layer = await _run_archetype(
        run_args,
        compute_class=compute_class,
        pipeline_emitter=pipeline_emitter,
        tool_label=f"Model aquifer storage & recovery [inject {inj:g} / recover {rec:g} m3/day]",
        expected_type=ASRLayerURI,
        error_code="ASR_RUN_FAILED",
        scenario_error=ASRScenarioError,
    )

    # provenance-chain wave: structure the demo-aquifer caveat. Each aquifer knob
    # the caller left unset fell to a demo default, NOT site hydrogeology; a knob
    # the user supplied is user-provenance. Attach onto the layer so the narration
    # seam renders it (replaces relying on the free-text caveat alone).
    provenance: list[SyntheticInput] = []
    for _pname, _val, _units in (
        ("aquifer_k_ms", aquifer_k_ms, "m/s"),
        ("porosity", porosity, None),
        ("aquifer_sy", aquifer_sy, None),
    ):
        provenance.append(SyntheticInput(
            param=_pname,
            value=(round(float(_val), 6) if _val is not None else None),
            units=_units,
            basis="user" if _val is not None else "default_demo",
            note=None if _val is not None else "demo aquifer default, not site hydrogeology",
        ))
    for _pname, _val in (
        ("injection_months", injection_months),
        ("recovery_months", recovery_months),
        ("n_cycles", n_cycles),
    ):
        if _val is None:
            provenance.append(SyntheticInput(
                param=_pname, basis="default_demo", note="demo cycle-schedule default",
            ))
    layer = layer.model_copy(update={"synthetic_inputs": provenance})

    # Mirror the head-decline emit: side-emit the well-head sawtooth line chart
    # from the typed ASRLayerURI.head_timeseries (real solver output).
    await _emit_asr_chart(layer)

    derived = {
        "location_name": location_name,
        "aoi_latlon": [lat, lon],
        "well_location_latlon": [wlat, wlon],
        "injection_rate_m3_day": inj,
        "recovery_rate_m3_day": rec,
        "injection_months": injection_months,
        "recovery_months": recovery_months,
        "n_cycles": n_cycles,
    }
    summary = {
        "location_name": location_name,
        "recovery_efficiency": layer.recovery_efficiency,
        "well_location_latlon": [wlat, wlon],
        "injection_rate_m3_day": inj,
        "recovery_rate_m3_day": rec,
        "head_series_steps": (
            len(layer.head_timeseries) if layer.head_timeseries else 0
        ),
        "demo_aquifer_caveat": (
            f"Aquifer K={DEFAULT_AQUIFER_K_MS:g} m/s, the specific yield, and the "
            "cycle schedule are demo defaults, not site-specific hydrogeology."
        ),
    }
    logger.info(
        "ASR scenario complete location=%r recovery_efficiency=%s head_steps=%d",
        location_name,
        layer.recovery_efficiency,
        len(layer.head_timeseries) if layer.head_timeseries else 0,
    )
    return ASRResult(asr_layer=layer, derived_params=derived, summary=summary)


# --------------------------------------------------------------------------- #
# LLM-exposed thin atomic-tool wrapper (workflow_dispatch source class)
# --------------------------------------------------------------------------- #


TEMPLATE_CARD = TemplateCard(
    question='aquifer storage & recovery (ASR): seasonal inject/recover at a well + recovery efficiency',
    required_inputs=['location (or aoi_latlon)', 'well_location_latlon'],
    knobs='injection_months, recovery_months, n_cycles, aquifer_k_ms, porosity, aquifer_sy',
)


_METADATA = AtomicToolMetadata(
    name="modflow_asr",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="modflow",
    tier="template",
)


@register_tool(
    _METADATA,
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def modflow_asr(
    location: str | None = None,
    aoi_latlon: tuple[float, float] | list[float] | None = None,
    well_location_latlon: tuple[float, float] | list[float] | None = None,
    injection_rate_m3_day: float | None = None,
    recovery_rate_m3_day: float | None = None,
    injection_months: int | None = None,
    recovery_months: int | None = None,
    n_cycles: int | None = None,
    aquifer_k_ms: float | None = None,
    porosity: float | None = None,
    aquifer_sy: float | None = None,
    compute_class: str = "standard",
    # absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Model aquifer storage & recovery (ASR): seasonal inject/recover at a well.

    Fidelity: MODFLOW 6 local planning-grade groundwater envelope (aquifer
    K/porosity default to narrated demo values unless supplied), not a
    calibrated regulatory delineation. Off-scope: surface-water inundation
    flooding -> sfincs_flood; urban storm-sewer / pipe-network flooding ->
    swmm_urban_flood.

    Builds a transient MODFLOW 6 groundwater-flow model with a single ASR well
    that INJECTS water for the injection months then RECOVERS (extracts) it for
    the recovery months, repeated for the requested cycles, runs it, and produces
    an ASR layer: the inject-rise / recover-fall head sawtooth at the well and the
    recovery efficiency (the fraction of injected water recovered). Use this to
    assess ASR feasibility / recovery efficiency / seasonal water banking.

    Use this when:
        - The user asks about aquifer storage & recovery, seasonal inject-then-
          recover water banking, or ASR recovery efficiency.

    Do NOT use this for:
        - A steady recharge-basin mound (use ``modflow_managed_recharge``).
        - A pumping-well drawdown cone (use ``modflow_sustainable_yield``).
        - A contaminant spill plume (use ``modflow_contaminant_plume``).

    Params:
        location: place name (geocoded). Supply this OR ``aoi_latlon``.
        aoi_latlon: explicit ``(lat, lon)`` AOI point.
        well_location_latlon: the ASR well ``(lat, lon)``. REQUIRED  -  never
            invented; ask the user if absent.
        injection_rate_m3_day: injection rate, POSITIVE magnitude (m^3/day).
            REQUIRED.
        recovery_rate_m3_day: recovery rate, POSITIVE magnitude (m^3/day). REQUIRED.
        injection_months / recovery_months / n_cycles: cycle schedule controls.
        aquifer_k_ms / porosity / aquifer_sy: optional demo-aquifer overrides.
        compute_class: FR-CE-3 compute class. Default ``"standard"``.

    Returns:
        On success: an ``ASRResult`` JSON dict with the ``asr_layer`` (an
        ``ASRLayerURI`` carrying ``recovery_efficiency`` + ``head_timeseries``  -
        the agent narrates these typed numbers), the ``derived_params``, and the
        ``summary``. On a recoverable failure (incl. a missing well / rate) the
        tool returns a typed error the agent narrates honestly  -  it never
        fabricates a well.

    FR-DC-6: ``cacheable=False`` + ``ttl_class="live-no-cache"`` +
    ``source_class="workflow_dispatch"``  -  the cache shim is NOT invoked.
    """
    aoi = _coerce_optional_latlon(aoi_latlon)
    well = _coerce_optional_latlon(well_location_latlon)
    try:
        result = await model_asr_scenario(
            location=location,
            aoi_latlon=aoi,
            well_location_latlon=well,
            injection_rate_m3_day=(
                float(injection_rate_m3_day)
                if injection_rate_m3_day is not None
                else None
            ),
            recovery_rate_m3_day=(
                float(recovery_rate_m3_day)
                if recovery_rate_m3_day is not None
                else None
            ),
            injection_months=(
                int(injection_months) if injection_months is not None else None
            ),
            recovery_months=(
                int(recovery_months) if recovery_months is not None else None
            ),
            n_cycles=int(n_cycles) if n_cycles is not None else None,
            aquifer_k_ms=aquifer_k_ms,
            porosity=porosity,
            aquifer_sy=aquifer_sy,
            compute_class=compute_class,
            pipeline_emitter=None,
        )
    except ASRInputError as exc:
        return {
            "status": "error",
            "error_code": "USER_INPUT_REQUIRED",
            "error_message": str(exc),
        }
    except ASRScenarioError as exc:
        return {
            "status": "error",
            "error_code": getattr(exc, "error_code", "ASR_SCENARIO_ERROR"),
            "error_message": str(exc),
        }
    return result.model_dump(mode="json")
