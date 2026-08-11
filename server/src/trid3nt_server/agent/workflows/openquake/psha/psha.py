"""Engine template ``openquake_psha`` - OpenQuake Engine probabilistic
seismic-hazard (PSHA) (engine-door refactor - OPENQUAKE slice; was
``run_seismic_hazard_psha``).

The LLM-facing exposure of the OpenQuake classical-PSHA engine (the platform's
seismic driver, pairing with the existing Pelicun impact path).
``openquake_psha(...)`` takes the ``OpenQuakeRunArgs`` parameters, runs the
deterministic assemble -> stage -> solve -> postprocess chain
(``model_openquake_psha`` below, in this module), and returns a
``SeismicHazardLayerURI`` the emitter loads onto the map (it subclasses
``LayerURI`` so the ``emit_tool_call`` ``add_loaded_layer`` gate fires).

This is the OpenQuake analogue of ``swmm_urban_flood`` (SWMM) /
``modflow_contaminant_plume`` (MODFLOW) / ``sfincs_flood`` (SFINCS) /
``geoclaw_inundation`` (GeoClaw). It is a registered engine TEMPLATE tagged
``engine="openquake", tier="template"`` - EXCLUDED from the default retrieval
pool and surfaced only by the ``run_openquake`` door's gate expansion
(SELECT-THEN-CALL). Like the other templates it declares ``cacheable=False`` +
``ttl_class="live-no-cache"`` + ``source_class="workflow_dispatch"`` (FR-DC-6 -
workflow exposure surface; never touches the cache shim). Confirmation before
consequence (Invariant 9 - a solver run) is enforced by the server confirmation
hook around this template (SOLVER_CONFIRM_TOOLS keys on ``openquake_psha``).

OpenQuake is CONTAINER-ONLY (the engine is RAM-hungry ~2 GB/thread and ships as
a containerized CLI), so unlike SWMM there is no in-process lane - the composer
always dispatches to a local Docker solver container via the generic run_solver
seam.

Determinism boundary (Invariant 1): every hazard number the agent narrates comes
from the typed ``SeismicHazardLayerURI.max_hazard_value`` / ``.hazard_area_km2`` /
``.return_period_years`` fields the postprocess computed - never free-generated.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import sys
from pathlib import Path
from typing import Any

from trid3nt_contracts import new_ulid
from trid3nt_contracts.execution import LayerURI, LegendClass, LegendKey
from trid3nt_contracts.openquake_contracts import (
    DEFAULT_SITE_GRID_SPACING_KM,
    OpenQuakeRunArgs,
    SeismicHazardLayerURI,
)
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_contracts.common import SyntheticInput

from trid3nt_server.agent.tools import register_tool
from trid3nt_server.agent.gates.input_review import gate_input_review
from trid3nt_server.agent.tool_arg_normalizer import coerce_bbox_value
from trid3nt_server.agent.workflows.openquake._template_card import TemplateCard
from trid3nt_server.agent.workflows.openquake.postprocess_openquake import (
    PostprocessOpenQuakeError,
    parse_hazard_curve_csv,
    parse_uhs_csv,
    postprocess_openquake,
)
from trid3nt_server.emission.layer_uri_emit import publish_input_layer
from trid3nt_server.emission.pipeline_emitter import (
    begin_substeps,
    current_emitter,
    emit_chart_payloads,
    substep,
)

logger = logging.getLogger("trid3nt_server.agent.workflows.openquake.psha.psha")

__all__ = [
    "openquake_psha",
    "RunOpenQuakeError",
    "model_openquake_psha",
    "OpenQuakeWorkflowError",
    "OPENQUAKE_SOLVER_NAME",
    "assemble_build_spec",
    "stage_openquake_build_spec",
    "resolve_fault_sources",
    "fault_records_to_feature_collection",
    "make_fault_sources_layer_uri",
    "FAULT_LINE_STYLE_PRESET",
    "REAL_FAULT_SITE_GRID_SPACING_KM",
    "openquake_local_spec",
    "register_openquake_solver",
]


class RunOpenQuakeError(RuntimeError):
    """Raised when the OpenQuake chain fails fatally before producing a layer.

    Carries the open-set ``error_code`` propagated from the failing stage so the
    agent emitter renders a typed error frame."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


#: Curated door-listing card (the run_openquake door prefers this over signature
#: derivation). One-line question + the real required input + a knobs summary.
TEMPLATE_CARD = TemplateCard(
    question=(
        "probabilistic seismic-hazard (PSHA) map - PGA / spectral-acceleration "
        "ground motion at a return period (e.g. 10% in 50 years) over an AOI; "
        "also the ground-motion INPUT to a Pelicun earthquake damage assessment"
    ),
    required_inputs=["bbox"],
    knobs=(
        "imt (PGA / PGV / SA(<period>)), poe, investigation_time_years, "
        "site_grid_spacing_km, max_distance_km, gmpe, a_value, b_value, "
        "min_magnitude, max_magnitude"
    ),
)


_OPENQUAKE_PSHA_METADATA = AtomicToolMetadata(
    name="openquake_psha",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="openquake",
    tier="template",
)


@register_tool(
    _OPENQUAKE_PSHA_METADATA,
    # readOnlyHint=False (runs a solver writing output COG artifacts),
    # openWorldHint=False (containerized OpenQuake CLI + intra-cloud object store),
    # destructiveHint=False (writes go to a new runs/ prefix),
    # idempotentHint=False (each call mints a new run_id + COG keys).
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def openquake_psha(
    bbox: tuple[float, float, float, float] | list[float] | str | None = None,
    imt: str = "PGA",
    poe: float = 0.10,
    investigation_time_years: float = 50.0,
    site_grid_spacing_km: float = 5.0,
    max_distance_km: float = 300.0,
    gmpe: str = "BooreAtkinson2008",
    a_value: float = 4.0,
    b_value: float = 1.0,
    min_magnitude: float = 5.0,
    max_magnitude: float = 7.5,
    vs30: float | None = None,
    vs30_compare: float | None = None,
    nehrp_amp_class: str | None = None,
    logic_tree: str = "single",
    secondary_poe: float | None = None,
    uniform_hazard_spectra: bool = False,
    compute_class: str = "standard",
    input_mode: str | None = None,
    # absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> SeismicHazardLayerURI | dict[str, Any]:
    """Run a probabilistic seismic-hazard (PSHA) calculation over an AOI.

    Fidelity: OpenQuake classical PSHA (a real active-fault source when GEM faults
    intersect the AOI, else a synthetic Gutenberg-Richter area source -- the
    returned source_model_kind must be narrated honestly; G-R recurrence + GMPE
    default to narrated demo values); planning-grade envelope, not a site-specific
    hazard study. Off-scope: landslide / ground-failure susceptibility ->
    landlab_susceptibility; structural damage / loss -> pelicun_damage_assessment.

    Use this when: the user asks for seismic/earthquake HAZARD, a probabilistic
    seismic-hazard map, PGA/spectral-acceleration map, or "10% in 50 years"
    (475-yr) ground motion -- also the canonical ground-motion INPUT to a
    Pelicun earthquake damage assessment. Builds a classical-PSHA OpenQuake deck
    over a site grid; when a REAL active fault (GEM Global Active Faults)
    intersects the AOI it builds a physics-based fault source (hazard peaks on
    the trace), else falls back to a synthetic Gutenberg-Richter area source --
    the returned ``source_model_kind`` ("real-fault"/"synthetic-area") must be
    narrated HONESTLY, never claim real faults on a synthetic fallback. Do NOT
    use for: surface-water/riverine/coastal flooding (``sfincs_flood``);
    urban/pluvial (``swmm_urban_flood``); groundwater
    (``modflow_contaminant_plume``); estimating building damage itself
    (``pelicun_damage_assessment`` -- this produces the Pelicun hazard INPUT,
    not the damage tool).

    Params:
        bbox: AOI, EPSG:4326; a regular site grid is laid over it.
        imt: ``"PGA"`` (default, g), ``"PGV"`` (cm/s), or ``"SA(<period>)"``.
        poe: probability of exceedance (0,1), default 0.10.
        investigation_time_years: PoE window, default 50.
        site_grid_spacing_km: default 5 (coarsened for wide AOIs --
            OpenQuake is RAM-hungry).
        max_distance_km: source-to-site integration distance, default 300.
        gmpe: ground-motion prediction equation, default
            "BooreAtkinson2008".
        a_value/b_value: demo Gutenberg-Richter recurrence, default 4.0/1.0.
        min_magnitude/max_magnitude: demo source range, default 5.0/7.5.
        vs30: reference site Vs30 (m/s). Unset -> the generic 760 rock demo
            default (labeled, not site-specific; no Vs30 fetcher yet).
        vs30_compare: optional SECOND reference Vs30 (m/s) for a site-response
            A/B. When set, a paired hazard-curve overlay is emitted comparing the
            run's Vs30 (rock default 760) against this softer/stiffer soil value
            at the AOI centroid on the same demo source -- the "how does my soil
            change the hazard" question. None => no comparison (unchanged).
        nehrp_amp_class: optional NEHRP site class ("C", "D", or "E") for a
            DISCRETE site-class amplification A/B -- a different mechanism from
            vs30_compare. Rather than sweeping a GMPE's continuous Vs30 term, a
            discrete AmplificationFunction table (published ASCE 7-22 Fpga site
            coefficient per class) is CONVOLVED into the hazard curve, and the
            unamplified 760 m/s reference rock is overlaid against the soft soil
            classes C/D/E at the AOI centroid, highlighting the named class -- the
            "how do NEHRP soil classes change the shaking vs rock" question. None
            => no amplification overlay.
        logic_tree: epistemic uncertainty mode. ``"single"`` (default) = one
            source model + one GMPE (a single hazard estimate). ``"source_models"``
            = two competing weighted source-model interpretations + 2 GMPEs
            (GEM LogicTreeCase1); ``"gr_uncertainty"`` = a Gutenberg-Richter
            a/b + maximum-magnitude epistemic branch tree + 2 GMPEs per tectonic
            region (GEM LogicTreeCase2, 324 realizations). The epistemic modes
            use a synthetic demo source (they bypass the real-fault path) and add
            the mean hazard curve + the 5/50/95 quantile spread across
            realizations. Use them for "how uncertain / what is the quantile
            spread / logic-tree / epistemic" questions.
        secondary_poe: optional second probability of exceedance for the hazard
            map (e.g. 0.02 == 2% in 50 years / 2475-yr) alongside the default
            ``poe`` (0.10 == 10%/475-yr). None => a single-PoE map.
        uniform_hazard_spectra: also export the Uniform Hazard Spectrum (spectral
            acceleration vs period at the target PoE) alongside the map + curve.
        compute_class: default "standard".
        input_mode: run-mode lever (ADR 0107). ``"user_gated"`` presents the
            reference Vs30 for review before the solve; ``"auto"`` (default)
            proceeds with it labeled.

    Returns:
        On success: ``SeismicHazardLayerURI`` with ``max_hazard_value``,
        ``hazard_area_km2``, ``return_period_years``, ``n_sites``,
        ``source_model_kind``, ``source_model_note``.
        On failure: ``{"status": "error", "error_code", "error_message"}``.
        Not cached (``cacheable=False``).

    FR-DC-6: ``cacheable=False``, ``ttl_class="live-no-cache"``,
    ``source_class="workflow_dispatch"`` -- cache shim not invoked.
    """
    if bbox is None:
        return {
            "status": "error",
            "error_code": "OQ_PARAMS_INCOMPLETE",
            "error_message": (
                "openquake_psha requires a bbox "
                "(min_lon, min_lat, max_lon, max_lat) in EPSG:4326."
            ),
        }
    coerced = coerce_bbox_value(bbox)
    if coerced is None:
        return {
            "status": "error",
            "error_code": "OQ_PARAMS_INVALID",
            "error_message": (
                f"invalid bbox (expected 4 numbers min_lon,min_lat,max_lon,max_lat): "
                f"{bbox!r}"
            ),
        }
    try:
        run_args = OpenQuakeRunArgs(
            bbox=tuple(coerced),  # type: ignore[arg-type]
            imt=str(imt),
            poe=float(poe),
            investigation_time_years=float(investigation_time_years),
            site_grid_spacing_km=float(site_grid_spacing_km),
            max_distance_km=float(max_distance_km),
            gmpe=str(gmpe),
            a_value=float(a_value),
            b_value=float(b_value),
            min_magnitude=float(min_magnitude),
            max_magnitude=float(max_magnitude),
            reference_vs30_ms=(float(vs30) if vs30 is not None else 760.0),
            logic_tree=str(logic_tree),
            secondary_poe=(float(secondary_poe) if secondary_poe is not None else None),
            uniform_hazard_spectra=bool(uniform_hazard_spectra),
        )
    except Exception as exc:  # noqa: BLE001 -- pydantic ValidationError or coercion
        return {
            "status": "error",
            "error_code": "OQ_PARAMS_INVALID",
            "error_message": f"invalid OpenQuake run arguments: {exc}",
        }

    # --- ADR 0107 two-mode input gate: the reference Vs30 is a physically
    # dominant site condition with no fetcher yet -- label the 760 rock default
    # (or a user value) and, in user_gated mode, present it for review before the
    # (consequential Batch) PSHA solve. auto (session default) + headless proceed.
    _vs30_user = vs30 is not None
    _vs30_prov = [SyntheticInput(
        param="vs30", value=round(float(run_args.reference_vs30_ms), 1),
        units="m/s", basis="user" if _vs30_user else "default_demo",
        note=(None if _vs30_user
              else "generic NEHRP B/C rock default; no Vs30 fetcher yet (not site-specific)"),
    )]
    _review = await gate_input_review(
        tool_name="openquake_psha", mode=input_mode,
        entries=_vs30_prov, params={"vs30": float(run_args.reference_vs30_ms)},
    )
    if _review.cancelled:
        return {
            "status": "error",
            "error_code": "USER_INPUT_CANCELLED",
            "error_message": f"openquake_psha {_review.cancel_reason}",
        }
    _vs30_prov = _review.entries
    _rv_vs30 = _review.params.get("vs30")
    if _rv_vs30 is not None and float(_rv_vs30) != float(run_args.reference_vs30_ms):
        run_args = run_args.model_copy(update={"reference_vs30_ms": float(_rv_vs30)})
        _vs30_prov = [SyntheticInput(
            param="vs30", value=round(float(_rv_vs30), 1), units="m/s",
            basis="user", note="user-revised at review")]

    logger.info(
        "openquake_psha bbox=%s imt=%s poe=%.4g inv_time=%.0fyr "
        "grid=%.1fkm gmpe=%s",
        run_args.bbox,
        run_args.imt,
        run_args.poe,
        run_args.investigation_time_years,
        run_args.site_grid_spacing_km,
        run_args.gmpe,
    )

    try:
        layer = await model_openquake_psha(
            run_args,
            compute_class=compute_class,
            vs30_compare=(float(vs30_compare) if vs30_compare is not None else None),
            nehrp_amp_class=(str(nehrp_amp_class) if nehrp_amp_class else None),
        )
        layer = layer.model_copy(update={"synthetic_inputs": _vs30_prov})
        logger.info(
            "openquake_psha complete layer_id=%s max_hazard=%.4g "
            "hazard_area_km2=%.6g return_period=%.0fyr uri=%s",
            layer.layer_id,
            layer.max_hazard_value,
            layer.hazard_area_km2,
            layer.return_period_years,
            layer.uri,
        )
        return layer
    except asyncio.CancelledError:
        raise
    except (OpenQuakeWorkflowError, PostprocessOpenQuakeError) as exc:
        logger.warning("openquake_psha failed: %s (%s)", exc.error_code, exc)
        return {
            "status": "error",
            "error_code": exc.error_code,
            "error_message": str(exc),
        }
    except Exception as exc:  # noqa: BLE001 -- defensive catch-all
        logger.exception("openquake_psha unexpected failure")
        return {
            "status": "error",
            "error_code": "OQ_INTERNAL_ERROR",
            "error_message": str(exc),
        }


# --------------------------------------------------------------------------- #
# assemble_build_spec -> resolve_fault_sources -> stage_openquake_build_spec
# -> run_solver -> postprocess_openquake: the composer end-to-end chain below,
# plus its pure helpers + the local-docker subprocess runner spec.
# --------------------------------------------------------------------------- #
#: The registry key + handle ``solver`` tag for the seismic-hazard engine.
OPENQUAKE_SOLVER_NAME: str = "openquake"

#: Logic-tree realization counts enumerated by each epistemic deck (deterministic
#: from the fixed branch structure the worker renders): source_models = 2 source
#: models x 2 GMPEs; gr_uncertainty = 3 a/b x 3 Mmax per source (2 sources) x 2
#: GMPEs per tectonic region (2 TRTs) = 324 (the published LogicTreeCase2 count).
_EPISTEMIC_N_REALIZATIONS: dict[str, int] = {
    "single": 0,
    "source_models": 4,
    "gr_uncertainty": 324,
}

#: One-line narration of the epistemic source model per logic-tree mode.
_EPISTEMIC_SOURCE_NOTE: dict[str, str] = {
    "source_models": (
        "Two competing weighted source-model interpretations + 2 GMPEs "
        "(GEM LogicTreeCase1 mechanism); the 5/50/95 band is the epistemic "
        "spread across 4 logic-tree realizations."
    ),
    "gr_uncertainty": (
        "Gutenberg-Richter a/b + maximum-magnitude epistemic branches over a "
        "two-source model x 2 GMPEs per tectonic region (GEM LogicTreeCase2 "
        "mechanism); the 5/50/95 band is the spread across 324 realizations."
    ),
}

#: a FINER default site-grid spacing for the real-fault case. The
#: synthetic area-source default (``DEFAULT_SITE_GRID_SPACING_KM`` == 5 km) is a
#: coarse uniform smear; a real-fault hazard map should resolve the sharp
#: gradient AROUND the fault trace, so we drop to 2 km when faults drive the
#: source model AND the caller left the (coarse) default in place. An explicit
#: finer request from the user still wins.
REAL_FAULT_SITE_GRID_SPACING_KM: float = 2.0


class OpenQuakeWorkflowError(RuntimeError):
    """Raised on any build-spec staging / dispatch / postprocess failure.

    Carries an open-set A.6 ``error_code`` so the agent emitter renders a typed
    error frame. Codes:

    - ``OQ_PARAMS_INVALID`` -- the run args could not be coerced.
    - ``OQ_STAGING_FAILED`` -- the build_spec could not be staged to S3.
    - ``OQ_SOLVE_FAILED`` -- the Batch solve did not complete.
    - ``OQ_BATCH_OUTPUT_MISSING`` -- a completed run produced no hazard-map CSV.
    """

    error_code: str = "OPENQUAKE_WORKFLOW_FAILED"

    def __init__(
        self,
        error_code: str,
        *,
        message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message or error_code)
        self.error_code = error_code
        self.details: dict[str, Any] = dict(details or {})


# --------------------------------------------------------------------------- #
# build_spec assembly (PURE -- unit-tested in isolation).
# --------------------------------------------------------------------------- #
def assemble_build_spec(
    run_args: OpenQuakeRunArgs,
    *,
    fault_sources: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Map ``OpenQuakeRunArgs`` -> the build_spec dict the worker reads.

    Pure (no I/O) so the composer arg-assembly unit-tests in isolation. The
    build_spec is exactly the shape ``job_ini.render_openquake_deck`` consumes
    (bbox + IMT + poe + grid spacing + max distance + GMPE + the G-R source
    params) plus the output globs for the worker's upload step.

    real-fault wiring: when ``fault_sources`` (the records
    ``fetch_fault_sources`` emits for the AOI) is a NON-empty list, it is attached
    to the build_spec under ``"fault_sources"`` so the worker's
    ``render_openquake_deck`` builds a physics-based ``simpleFaultSource`` model
    (hazard PEAKING ON the trace) instead of the synthetic AOI area source. In
    that case the site grid is ALSO refined to ``REAL_FAULT_SITE_GRID_SPACING_KM``
    (2 km) -- BUT ONLY when the caller left the coarse synthetic default
    (``DEFAULT_SITE_GRID_SPACING_KM`` == 5 km) in place; an explicit user request
    for a different spacing is honored unchanged. ADDITIVE: ``fault_sources=None``
    (or an empty list) renders a byte-identical synthetic build_spec, so a run
    with no faults in the AOI behaves exactly like before.

    levers STEP 3: the validated ``advanced_physics`` (truncation_level /
    rupture_mesh_spacing_km / width_of_mfd_bin / area_source_discretization_km)
    is MERGED into the build_spec, and ``uniform_hazard_spectra`` is flipped on
    (the classical run already exports hazard curves; UHS needs the flag). None
    => no keys merged => byte-identical job.ini. Invalid keys raise a typed
    ``OpenQuakeWorkflowError("OQ_PHYSICS_INVALID")``.
    """
    from trid3nt_server.agent.workflows.shared.physics_registry import (
        PhysicsRegistryError,
        validate_and_resolve_physics,
    )

    try:
        resolved = validate_and_resolve_physics(
            "openquake", getattr(run_args, "advanced_physics", None)
        )
    except PhysicsRegistryError as exc:
        raise OpenQuakeWorkflowError(
            "OQ_PHYSICS_INVALID",
            message=f"invalid advanced_physics: {exc}",
            details={"engine": "openquake", "key": getattr(exc, "key", None)},
        ) from exc

    have_faults = bool(fault_sources)

    # Real-fault case: refine the (coarse synthetic-default) site grid so the map
    # resolves the sharp gradient around the trace -- but never override an
    # explicit user request.
    grid_km = float(run_args.site_grid_spacing_km)
    if have_faults and grid_km == float(DEFAULT_SITE_GRID_SPACING_KM):
        grid_km = REAL_FAULT_SITE_GRID_SPACING_KM

    spec: dict[str, Any] = {
        "bbox": list(run_args.bbox),
        "imt": run_args.imt,
        "poe": float(run_args.poe),
        "investigation_time_years": float(run_args.investigation_time_years),
        "site_grid_spacing_km": grid_km,
        "max_distance_km": float(run_args.max_distance_km),
        "gmpe": run_args.gmpe,
        # ADR 0107: the reference Vs30 (default 760 rock) rides to the worker
        # deck; a user-supplied value overrides the demo default byte-for-byte.
        "reference_vs30_value": float(getattr(run_args, "reference_vs30_ms", 760.0)),
        "a_value": float(run_args.a_value),
        "b_value": float(run_args.b_value),
        "min_magnitude": float(run_args.min_magnitude),
        "max_magnitude": float(run_args.max_magnitude),
        # The OpenQuake CSV exports land under output/; capture them + the
        # rendered deck for provenance.
        "outputs": ["output/*.csv", "*.csv"],
    }
    # Real-fault source model: hand the worker the fetched fault records so it
    # builds simpleFaultSources. Absent/empty => synthetic area source (default).
    if have_faults:
        spec["fault_sources"] = [dict(rec) for rec in fault_sources]  # type: ignore[union-attr]

    # Epistemic logic-tree mode (GEM LogicTreeCase1/Case2): the worker deck
    # renderer branches on this key to emit the competing-source-model /
    # a-b-Mmax-uncertainty multi-branch trees + the 5/50/95 quantile spread. The
    # default "single" leaves the classical single-branch deck byte-identical.
    logic_tree = str(getattr(run_args, "logic_tree", "single"))
    if logic_tree != "single":
        spec["logic_tree"] = logic_tree

    # row-3 multi-return-period: a second PoE (e.g. 0.02 == 2% in 50yr) exports
    # the hazard map at BOTH PoEs; None => the single-PoE map (unchanged).
    secondary_poe = getattr(run_args, "secondary_poe", None)
    if secondary_poe is not None:
        spec["poes"] = [float(run_args.poe), float(secondary_poe)]

    # row-3 UHS: export the Uniform Hazard Spectrum alongside the map/curve.
    if bool(getattr(run_args, "uniform_hazard_spectra", False)):
        spec["uniform_hazard_spectra"] = True

    # Merge validated physics overrides (the worker render_job_ini reads them).
    spec.update(resolved)
    # levers STEP 3: request UHS export when the registry-quantities flag is on
    # (default OFF -> byte-identical classical job.ini). The agent reads the
    # exported UHS + hazard-curve CSVs into ScalarField metrics in
    # publish_openquake_quantities.
    if os.environ.get("TRID3NT_OPENQUAKE_REGISTRY_QUANTITIES", "").lower() in (
        "1", "true", "on", "yes"
    ):
        spec["uniform_hazard_spectra"] = True
    return spec


# --------------------------------------------------------------------------- #
# real-fault source resolution (the SYNC fetch wrapper).
#
# Calls the ``fetch_fault_sources`` atomic tool for the AOI and returns
# ``(fault_records, narration_note)``. This is a SYNC function (it does network
# I/O via the cache shim) -> the composer runs it OFF the asyncio loop with
# ``asyncio.to_thread`` (the no-sync-blocking norm). The honesty floor lives
# HERE + in the composer: a fetch that returns 0 faults (open ocean, stable
# craton, upstream wobble) yields an EMPTY list -> the composer narrates
# "synthetic-area" and never claims real faults.
# --------------------------------------------------------------------------- #
def resolve_fault_sources(
    bbox: list[float] | tuple[float, float, float, float],
) -> tuple[list[dict[str, Any]], str]:
    """Fetch real active-fault sources for ``bbox`` (sync; run off the loop).

    Returns ``(fault_records, note)``:

      - ``fault_records``: the list ``fetch_fault_sources`` emits (possibly empty).
        Pass straight to ``assemble_build_spec(fault_sources=...)``.
      - ``note``: a short human-readable line for the layer narration.

    NEVER raises for the "no faults / fetch failed" case -- a missing fault source
    is an HONEST fallback to the synthetic area source, not a workflow error (the
    data-source fallback norm). A genuine upstream failure with no cache is logged
    and degraded to the empty-faults synthetic path (we still want a hazard map).
    Only the caller's malformed bbox would surface upstream (already validated by
    ``OpenQuakeRunArgs``), so in practice this always returns cleanly.
    """
    # fetch_fault_sources is now spec-driven (ADR 0081): resolve the router closure
    # off the registry seam (TOOL_REGISTRY[name].fn) and catch the router's typed
    # FetchError base -- byte-identical A.6 codes (FAULT_SOURCES_*), zero twin import.
    from trid3nt_server.agent.tools import TOOL_REGISTRY
    from trid3nt_server.agent.tools.fetchers._fetch_common import FetchError

    fetch_fault_sources = TOOL_REGISTRY["fetch_fault_sources"].fn

    try:
        result = fetch_fault_sources(bbox=list(bbox))
    except FetchError as exc:
        logger.warning(
            "resolve_fault_sources: fault fetch failed bbox=%s (%s); "
            "falling back to the synthetic area source",
            list(bbox),
            exc,
        )
        return [], (
            "Real active-fault sources were unavailable for this AOI "
            f"({exc.error_code}); used the synthetic area source instead."
        )

    # HONESTY FLOOR: the fetcher already drops degenerate traces (it requires a
    # non-collinear/non-coincident >=2-distinct-point trace + slip>0), which is
    # the only realistic way a fetched fault could pass here yet fail the worker's
    # length/moment-balance render gate. So a non-empty list here == faults the
    # worker WILL render into simpleFaultSources -> the real-fault stamp matches
    # what the engine runs. (We do NOT import the worker's job_ini agent-side: it
    # is not in the agent bundle, and an ImportError would wrongly force the
    # synthetic fallback on the deployed agent.)
    # ``fetch_fault_sources`` now returns a ``FaultSourcesResult``
    # (a renderable ``LayerURI`` subclass) on a NON-empty fetch and a plain dict
    # on the empty degrade -- read the records + note off EITHER shape.
    if isinstance(result, dict):
        faults = list(result.get("faults") or [])
        fetch_note = result.get("note")
    else:
        faults = list(getattr(result, "faults", None) or [])
        fetch_note = getattr(result, "note", None)
    if faults:
        names = ", ".join(
            str(f.get("name") or "fault") for f in faults[:4]
        )
        more = "" if len(faults) <= 4 else f", +{len(faults) - 4} more"
        note = (
            f"Hazard built from {len(faults)} real GEM active-fault source"
            f"{'s' if len(faults) != 1 else ''} ({names}{more}); the hazard "
            "peaks on the actual fault traces."
        )
        return faults, note

    # Empty AOI -> honest synthetic fallback. Surface the fetcher's typed note
    # (read off either shape above).
    note = (
        str(fetch_note)
        if fetch_note
        else (
            "No mapped active fault intersects this AOI; used the synthetic "
            "area source."
        )
    )
    return [], note


# --------------------------------------------------------------------------- #
# surface the resolved fault traces as a renderable INPUT layer.
#
# The fault sources are resolved in-memory (lon/lat traces) and baked into the
# OpenQuake XML, then DISCARDED -- no artifact was kept, so the user could never
# SEE the fault lines the hazard peaks on. We now serialize the records to a
# GeoJSON FeatureCollection of LineStrings (carrying name / slip-rate / slip-type
# for click-inspect), upload it next to the run, and emit it as a role="input"
# vector so the fault traces render under the hazard COG.
# --------------------------------------------------------------------------- #

#: Style-preset label for the surfaced fault-trace vector. Semantic name (future-
#: proof for a dedicated web/QGIS preset); today the web renders an unknown LINE
#: preset in its geometry-family colour, so the traces draw as a distinct line.
FAULT_LINE_STYLE_PRESET = "fault_line"


def fault_records_to_feature_collection(
    fault_records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Serialize resolved fault records to a GeoJSON ``FeatureCollection``.

    Each record's ``geometry`` is the flattened ``[[lon, lat], ...]`` trace the
    fetcher produced (``trace_coords`` already collapses MultiLineString to one
    ordered vertex list). A record becomes a ``LineString`` feature carrying the
    click-inspect properties ``name`` / ``net_slip_rate_mm_yr`` / ``slip_type``
    (plus ``catalog_name`` when present). Records with fewer than 2 vertices are
    SKIPPED (a degenerate trace is not a drawable line) -- this mirrors the
    fetcher's own >=2-distinct-vertex gate, so in practice every resolved record
    yields a feature.

    Pure dict work (no I/O, no reproject -- the traces are already EPSG:4326
    lon/lat). Returns a valid (possibly empty) FeatureCollection.
    """
    features: list[dict[str, Any]] = []
    for rec in fault_records or []:
        coords = rec.get("geometry") or []
        # Coerce to a clean [[lon, lat], ...] list of >=2 vertices.
        line: list[list[float]] = []
        for p in coords:
            if isinstance(p, (list, tuple)) and len(p) >= 2:
                line.append([float(p[0]), float(p[1])])
        if len(line) < 2:
            continue
        props: dict[str, Any] = {
            "name": str(rec.get("name") or "fault"),
            "net_slip_rate_mm_yr": rec.get("net_slip_rate_mm_yr"),
            "slip_type": rec.get("slip_type"),
        }
        if rec.get("catalog_name"):
            props["catalog_name"] = str(rec.get("catalog_name"))
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": line},
                "properties": props,
            }
        )
    return {"type": "FeatureCollection", "features": features}


def make_fault_sources_layer_uri(
    fault_records: list[dict[str, Any]],
    *,
    run_id: str,
    runs_bucket: str | None = None,
) -> LayerURI | None:
    """Build the fault-trace ``FeatureCollection`` + UPLOAD it to S3 -> LayerURI.

    Mirrors :func:`make_swmm_mesh_layer_uri`: serialize the records, upload to the
    DURABLE runs bucket at ``s3://<runs_bucket>/<run_id>/fault_sources.geojson``
    (so ``add_loaded_layer`` can re-inline the s3:// vector on every reconnect,
    exactly like the mesh), and return a ``role="input"`` vector ``LayerURI`` with
    ``bbox=None`` (an input must not emit a competing zoom-to). Carries a
    categorical ``LegendKey`` so the surfaced traces get a legend swatch.

    Returns ``None`` (best-effort, never fatal) when there are no drawable
    features OR the S3 upload fails. SYNC compute + boto3 upload -- the caller
    wraps it in ``asyncio.to_thread`` (never run sync boto3 on the asyncio loop).
    """
    fc = fault_records_to_feature_collection(fault_records)
    n_features = len(fc.get("features") or [])
    if n_features <= 0:
        logger.info(
            "make_fault_sources_layer_uri: no drawable fault traces -> no input "
            "layer (run_id=%s)",
            run_id,
        )
        return None

    # Upload to the DURABLE runs bucket via the SHARED solver S3 seam (the SAME
    # boto3 instance-role + bucket convention the mesh layer + every run artifact
    # uses). A put failure -> the input is simply absent, never breaks the solve.
    try:
        from trid3nt_server.agent.tools.simulation.solver.solver import _get_runs_bucket, _get_s3_client

        bucket = runs_bucket or _get_runs_bucket()
        key = f"{run_id}/fault_sources.geojson"
        _get_s3_client().put_object(
            Bucket=bucket,
            Key=key,
            Body=json.dumps(fc).encode("utf-8"),
            ContentType="application/geo+json",
        )
        s3_uri = f"s3://{bucket}/{key}"
    except Exception as exc:  # noqa: BLE001 - best-effort; S3 put failure non-fatal
        logger.warning(
            "make_fault_sources_layer_uri: fault_sources.geojson S3 upload failed "
            "(non-fatal, fault input absent; run_id=%s): %s",
            run_id,
            exc,
        )
        return None

    plural = "trace" if n_features == 1 else "traces"
    return LayerURI(
        layer_id=f"fault-sources-{run_id}",
        name=f"Active fault {plural} ({n_features})",
        layer_type="vector",
        uri=s3_uri,
        style_preset=FAULT_LINE_STYLE_PRESET,
        role="input",
        bbox=None,
        legend=LegendKey(
            kind="categorical",
            classes=[
                LegendClass(value="fault", color="#FF6A00", label="Active fault trace")
            ],
            label="Active faults (GEM)",
        ),
    )


# --------------------------------------------------------------------------- #
# build_spec staging (S3) -- mirror of stage_swmm_manifest.
# --------------------------------------------------------------------------- #
def stage_openquake_build_spec(
    run_args: OpenQuakeRunArgs,
    run_id: str,
    *,
    fault_sources: list[dict[str, Any]] | None = None,
) -> str:
    """Upload the build_spec JSON to S3; return its ``s3://`` URI.

    Mirrors ``run_swmm.stage_swmm_manifest`` EXACTLY (no new client): uses the
    same ``cache.storage_scheme()`` scheme + the same ``solver._get_s3_client()``
    boto3 client + the same ``TRID3NT_CACHE_BUCKET`` staging bucket. Feed the
    returned URI STRAIGHT to ``run_solver(solver='openquake',
    model_setup_uri=<this>, ...)``.

    ``fault_sources`` (when non-empty) is threaded into
    ``assemble_build_spec`` so the staged build_spec carries the real-fault source
    model. ``None`` => synthetic area source (unchanged).

    Raises:
        OpenQuakeWorkflowError("OQ_STAGING_FAILED"): the upload could not complete.
    """
    from trid3nt_server.agent.tools.cache import CACHE_BUCKET, storage_scheme
    from trid3nt_server.agent.tools.simulation.solver.solver import _get_s3_client

    scheme = storage_scheme()  # "s3" on AWS
    cache_bucket = os.environ.get("TRID3NT_CACHE_BUCKET") or CACHE_BUCKET
    prefix = f"cache/static-30d/openquake_setup/{run_id}/"
    spec_key = f"{prefix}build_spec.json"
    spec_uri = f"{scheme}://{cache_bucket}/{spec_key}"

    build_spec = assemble_build_spec(run_args, fault_sources=fault_sources)
    try:
        s3 = _get_s3_client()
        s3.put_object(
            Bucket=cache_bucket,
            Key=spec_key,
            Body=json.dumps(build_spec, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
    except Exception as exc:  # noqa: BLE001
        raise OpenQuakeWorkflowError(
            "OQ_STAGING_FAILED",
            message=f"failed to stage OpenQuake build_spec to {spec_uri}: {exc}",
            details={"run_id": run_id, "build_spec_uri": spec_uri},
        ) from exc

    logger.info("stage_openquake_build_spec run_id=%s -> %s", run_id, spec_uri)
    return spec_uri


# --------------------------------------------------------------------------- #
# Batch hazard-map download -- mirror of _download_batch_swmm_outputs.
# --------------------------------------------------------------------------- #
def _pick_hazard_map_uri(output_uris: list[str]) -> str | None:
    """Pick the hazard-MAP CSV from the uploaded output URIs (agent-side mirror
    of the worker's ``resolve_hazard_map_csv``, so the agent never imports the
    worker package). Prefer a ``hazard_map`` CSV, fall back to any ``hazard``
    CSV, else None."""
    csvs = [u for u in output_uris if u.lower().endswith(".csv")]
    for u in csvs:
        base = u.rsplit("/", 1)[-1].lower()
        if "hazard_map" in base or "hazard-map" in base:
            return u
    for u in csvs:
        if "hazard" in u.rsplit("/", 1)[-1].lower():
            return u
    return None


def _download_batch_hazard_csv(run_result: Any, run_id: str) -> str:
    """Download the exported hazard-MAP CSV produced by the Batch worker.

    The OpenQuake Batch worker uploads the engine's CSV exports under
    ``s3://<runs_bucket>/<run_id>/output/`` and records the hazard-map URI in
    completion.json (``hazard_map_uri``, with the full ``output_uris`` list as a
    fallback). We re-read completion.json (small, already on S3) to find the
    hazard-map key, download it via the SAME boto3 client the solver dispatch
    uses, and return the local CSV TEXT.

    Raises:
        OpenQuakeWorkflowError("OQ_BATCH_OUTPUT_MISSING"): the completed run did
            not produce a downloadable hazard-map CSV.
    """
    from trid3nt_server.agent.tools.simulation.solver.solver import (
        _get_runs_bucket,
        _get_s3_client,
        _split_object_uri,
        _try_get_completion_s3,
    )

    runs_bucket = _get_runs_bucket()
    s3 = _get_s3_client()

    manifest = _try_get_completion_s3(runs_bucket, run_id)
    hazard_uri: str | None = None
    if isinstance(manifest, dict):
        hazard_uri = manifest.get("hazard_map_uri") or _pick_hazard_map_uri(
            [str(u) for u in (manifest.get("output_uris") or [])]
        )

    if not hazard_uri:
        raise OpenQuakeWorkflowError(
            "OQ_BATCH_OUTPUT_MISSING",
            message=(
                "OpenQuake Batch solve completed but produced no hazard-map CSV "
                f"(runs_bucket={runs_bucket} run_id={run_id})"
            ),
            details={"run_id": run_id, "output_uri": getattr(run_result, "output_uri", None)},
        )

    try:
        _scheme, _bucket, key = _split_object_uri(hazard_uri)
    except Exception as exc:  # noqa: BLE001
        raise OpenQuakeWorkflowError(
            "OQ_BATCH_OUTPUT_MISSING",
            message=f"hazard_map_uri unparseable: {hazard_uri!r}: {exc}",
            details={"run_id": run_id},
        ) from exc

    try:
        resp = s3.get_object(Bucket=runs_bucket, Key=key)
        return resp["Body"].read().decode("utf-8")
    except Exception as exc:  # noqa: BLE001
        raise OpenQuakeWorkflowError(
            "OQ_BATCH_OUTPUT_MISSING",
            message=f"hazard-map CSV download failed s3://{runs_bucket}/{key}: {exc}",
            details={"run_id": run_id},
        ) from exc


# --------------------------------------------------------------------------- #
# download the NON-RASTER curve products (hazard CURVE + UHS) for the
# chart producers. Best-effort: these are charts, not the headline layer - a
# missing/unreadable curve CSV yields no chart (NOT a workflow failure).
# --------------------------------------------------------------------------- #
def _pick_csv_by_token(output_uris: list[str], *tokens: str) -> str | None:
    """Pick the first CSV whose basename contains ANY of ``tokens`` (lowercased).

    OpenQuake exports ``hazard_curve-mean-<IMT>_*.csv`` and (when UHS is on)
    ``hazard_uhs-mean_*.csv`` alongside the map CSV; we select by filename token.
    """
    for u in output_uris:
        base = u.rsplit("/", 1)[-1].lower()
        if base.endswith(".csv") and any(t in base for t in tokens):
            return u
    return None


def _download_batch_curve_csvs(
    run_id: str,
) -> tuple[str | None, str | None]:
    """Download the hazard-CURVE + UHS CSV TEXT from the Batch run (best-effort).

    Re-reads completion.json's ``output_uris`` (the same manifest
    ``_download_batch_hazard_csv`` reads), selects the curve / UHS CSVs by
    filename token, and downloads them via the SAME boto3 client. Returns
    ``(hazard_curve_text|None, uhs_text|None)`` - a None entry means the product
    was not exported / not readable (no chart for it). NEVER raises: a curve
    download wobble must not fail the hazard run (the map layer already landed)."""
    try:
        from trid3nt_server.agent.tools.simulation.solver.solver import (
            _get_runs_bucket,
            _get_s3_client,
            _split_object_uri,
            _try_get_completion_s3,
        )

        runs_bucket = _get_runs_bucket()
        s3 = _get_s3_client()
        manifest = _try_get_completion_s3(runs_bucket, run_id)
        if not isinstance(manifest, dict):
            return None, None
        output_uris = [str(u) for u in (manifest.get("output_uris") or [])]
        curve_uri = _pick_csv_by_token(output_uris, "hazard_curve", "hazard-curve")
        uhs_uri = _pick_csv_by_token(output_uris, "hazard_uhs", "hazard-uhs", "_uhs")

        def _get_text(uri: str | None) -> str | None:
            if not uri:
                return None
            try:
                _scheme, _bucket, key = _split_object_uri(uri)
                resp = s3.get_object(Bucket=runs_bucket, Key=key)
                return resp["Body"].read().decode("utf-8")
            except Exception as exc:  # noqa: BLE001
                logger.warning("curve CSV download failed %s: %s", uri, exc)
                return None

        return _get_text(curve_uri), _get_text(uhs_uri)
    except Exception as exc:  # noqa: BLE001 - charts are non-fatal
        logger.warning("curve/UHS CSV resolution failed run_id=%s: %s", run_id, exc)
        return None, None


def _download_batch_quantile_curve_csvs(
    run_id: str,
) -> dict[str, str]:
    """Download the 5/50/95 quantile hazard-CURVE CSV TEXT (best-effort).

    Epistemic logic-tree runs export ``quantile_curve-{0.05,0.5,0.95}-<IMT>_*.csv``
    alongside the mean curve (same wide ``poe-<iml>`` format). Returns a dict
    keyed ``"0.05"`` / ``"0.5"`` / ``"0.95"`` -> CSV text for whichever quantiles
    exported. NEVER raises (charts are non-fatal): an empty dict yields no band."""
    out: dict[str, str] = {}
    try:
        from trid3nt_server.agent.tools.simulation.solver.solver import (
            _get_runs_bucket,
            _get_s3_client,
            _split_object_uri,
            _try_get_completion_s3,
        )

        runs_bucket = _get_runs_bucket()
        s3 = _get_s3_client()
        manifest = _try_get_completion_s3(runs_bucket, run_id)
        if not isinstance(manifest, dict):
            return out
        output_uris = [str(u) for u in (manifest.get("output_uris") or [])]
        for q in ("0.05", "0.5", "0.95"):
            uri = _pick_csv_by_token(output_uris, f"quantile_curve-{q}-")
            if not uri:
                continue
            try:
                _scheme, _bucket, key = _split_object_uri(uri)
                resp = s3.get_object(Bucket=runs_bucket, Key=key)
                out[q] = resp["Body"].read().decode("utf-8")
            except Exception as exc:  # noqa: BLE001
                logger.warning("quantile CSV download failed %s: %s", uri, exc)
        return out
    except Exception as exc:  # noqa: BLE001 - charts are non-fatal
        logger.warning("quantile CSV resolution failed run_id=%s: %s", run_id, exc)
        return out


async def _emit_oq_curve_charts(
    run_id: str,
    *,
    imt: str,
    poe: float,
    investigation_time_years: float,
    source_layer_uri: str | None,
    logic_tree: str = "single",
) -> None:
    """Build + side-emit the hazard-curve (and UHS) charts (best-effort, no-op safe).

    Downloads the curve / UHS CSVs off the loop, parses them with the EXISTING
    ``parse_hazard_curve_csv`` / ``parse_uhs_csv`` (real engine output, no LLM),
    builds Vega-Lite line charts via ``chart_tools``, and emits them through the
    live pipeline emitter. Each builder returns None (emits nothing) when its
    series is absent - so a classical-only run (no UHS) emits only the curve.

    For an epistemic logic-tree run (``logic_tree`` != "single") the mean curve is
    upgraded to a 5/50/95 quantile-BAND chart (the epistemic spread across
    realizations) when the ``quantile_curve-*`` exports are present."""
    from trid3nt_server.agent.tools.processing.charts_common import (
        build_hazard_curve_chart,
        build_hazard_quantile_band_chart,
        build_uhs_chart,
    )

    curve_text, uhs_text = await asyncio.to_thread(
        _download_batch_curve_csvs, run_id
    )
    quantile_texts: dict[str, str] = {}
    if logic_tree != "single":
        quantile_texts = await asyncio.to_thread(
            _download_batch_quantile_curve_csvs, run_id
        )

    charts: list[dict[str, Any]] = []
    if curve_text:
        curve = parse_hazard_curve_csv(curve_text)
        imls = curve.get("hazard_curve_imls_g") or []
        mean_poe = curve.get("hazard_curve_mean_poe") or []
        band_chart = None
        if quantile_texts.get("0.05") and quantile_texts.get("0.95"):
            q = {
                k: (parse_hazard_curve_csv(v).get("hazard_curve_mean_poe") or [])
                for k, v in quantile_texts.items()
            }
            band_chart = build_hazard_quantile_band_chart(
                imls_g=imls,
                mean_poe=mean_poe,
                q05_poe=q.get("0.05") or [],
                q50_poe=q.get("0.5") or mean_poe,
                q95_poe=q.get("0.95") or [],
                imt=imt,
                investigation_time_years=investigation_time_years,
                n_realizations=_EPISTEMIC_N_REALIZATIONS.get(logic_tree),
                logic_tree_label=logic_tree,
                source_layer_uri=source_layer_uri,
            )
        if band_chart is not None:
            charts.append(band_chart)
        else:
            chart = build_hazard_curve_chart(
                imls_g=imls,
                mean_poe=mean_poe,
                imt=imt,
                investigation_time_years=investigation_time_years,
                n_sites=curve.get("hazard_curve_n_sites"),
                source_layer_uri=source_layer_uri,
            )
            if chart is not None:
                charts.append(chart)
    if uhs_text:
        uhs = parse_uhs_csv(uhs_text, poe=poe)
        chart = build_uhs_chart(
            periods_s=uhs.get("uhs_periods_s") or [],
            mean_sa_g=uhs.get("uhs_mean_sa_g") or [],
            poe=poe,
            n_sites=uhs.get("uhs_n_sites"),
            source_layer_uri=source_layer_uri,
        )
        if chart is not None:
            charts.append(chart)

    if charts:
        await emit_chart_payloads(charts)


# --------------------------------------------------------------------------- #
# Vs30 site-response A/B overlay (row-4 fold): two single-site classical hazard
# curves on the same demo area source, differing ONLY in reference Vs30 (the
# run's rock value vs a softer/stiffer comparison soil), overlaid in ONE figure.
# Runs the installed oq locally (the offline lane, off the loop); best-effort -
# a failure to build the A/B never fails the primary hazard map.
# --------------------------------------------------------------------------- #
async def _emit_vs30_ab_chart(
    run_args: OpenQuakeRunArgs,
    *,
    vs30_rock: float,
    vs30_compare: float,
    source_layer_uri: str | None,
) -> None:
    """Emit the Vs30 rock-vs-soil hazard-curve A/B overlay (best-effort, no-op safe)."""
    try:
        from trid3nt_server.agent.tools.processing.charts_common import (
            build_chart_payload,
        )
        from trid3nt_server.agent.workflows.openquake._local_oq import (
            aoi_centroid,
            render_area_source_model_xml,
            render_classical_point_job_ini,
            render_trivial_gmpe_logic_tree_xml,
            render_trivial_source_logic_tree_xml,
            run_oq_local,
        )

        bbox = tuple(run_args.bbox)
        lon, lat = aoi_centroid(bbox)  # type: ignore[arg-type]
        source_xml = render_area_source_model_xml(
            bbox, a_value=float(run_args.a_value), b_value=float(run_args.b_value),  # type: ignore[arg-type]
            min_magnitude=float(run_args.min_magnitude),
            max_magnitude=float(run_args.max_magnitude),
        )
        slt = render_trivial_source_logic_tree_xml()
        glt = render_trivial_gmpe_logic_tree_xml(str(run_args.gmpe))

        def _curve_for(vs30: float) -> dict[str, list[float]]:
            files = {
                "source_model.xml": source_xml,
                "source_model_logic_tree.xml": slt,
                "gmpe_logic_tree.xml": glt,
                "job.ini": render_classical_point_job_ini(
                    site_lon=lon, site_lat=lat, imt=str(run_args.imt),
                    investigation_time_years=float(run_args.investigation_time_years),
                    max_distance_km=float(run_args.max_distance_km),
                    reference_vs30=float(vs30),
                    description=f"Vs30 site-response A/B ({vs30:g} m/s)",
                ),
            }
            outdir = run_oq_local(files, label="vs30ab")
            curves = sorted(outdir.glob("hazard_curve-mean*.csv"))
            if not curves:
                return {"imls": [], "poe": []}
            parsed = parse_hazard_curve_csv(curves[0].read_text(encoding="utf-8"))
            return {
                "imls": list(parsed.get("hazard_curve_imls_g") or []),
                "poe": list(parsed.get("hazard_curve_mean_poe") or []),
            }

        rock = await asyncio.to_thread(_curve_for, vs30_rock)
        soil = await asyncio.to_thread(_curve_for, vs30_compare)

        rows: list[dict[str, Any]] = []
        for label_vs30, curve in (
            (f"Vs30 {vs30_rock:g} m/s", rock),
            (f"Vs30 {vs30_compare:g} m/s", soil),
        ):
            for x, p in zip(curve.get("imls") or [], curve.get("poe") or []):
                if float(x) > 0.0 and float(p) > 0.0:
                    rows.append({"iml": float(x), "poe": float(p), "site": label_vs30})
        if len({r["site"] for r in rows}) < 2:
            return

        # amplitude ratio at the run PoE (soil / rock IML at the target PoE),
        # read off the two curves for the caption strip.
        def _iml_at(curve: dict[str, list[float]], poe: float) -> float | None:
            xs, ps = curve.get("imls") or [], curve.get("poe") or []
            best = None
            for x, p in zip(xs, ps):
                if p and p > 0 and x and x > 0:
                    if best is None or abs(p - poe) < best[0]:
                        best = (abs(p - poe), x)
            return best[1] if best else None

        rock_iml = _iml_at(rock, float(run_args.poe))
        soil_iml = _iml_at(soil, float(run_args.poe))
        ratio_txt = ""
        if rock_iml and soil_iml and rock_iml > 0:
            ratio_txt = (
                f" Near {run_args.poe * 100:g}% PoE the softer/stiffer site's "
                f"{run_args.imt} is ~{soil_iml / rock_iml:.2f}x the rock value."
            )

        inv = int(round(float(run_args.investigation_time_years))) or 50
        softer = vs30_compare < vs30_rock
        spec = {
            "data": {"values": rows},
            "mark": {"type": "line", "point": True, "tooltip": True},
            "encoding": {
                "x": {"field": "iml", "type": "quantitative",
                      "scale": {"type": "log"}, "title": f"{run_args.imt} (g)"},
                "y": {"field": "poe", "type": "quantitative",
                      "scale": {"type": "log"}, "title": f"Mean PoE in {inv}yr"},
                "color": {"field": "site", "type": "nominal", "title": "Site condition",
                          "scale": {"range": ["#1f5fbf", "#e07a00"]}},
            },
            "width": "container",
        }
        payload = build_chart_payload(
            vega_lite_spec=spec,
            title=f"Vs30 site-response A/B - {run_args.imt} hazard curve",
            caption=(
                f"Classical {run_args.imt} hazard curve at the AOI centroid for "
                f"reference Vs30 {vs30_rock:g} m/s (rock) vs {vs30_compare:g} m/s "
                f"({'softer soil' if softer else 'stiffer site'}) on the same demo "
                f"source over {inv} yr.{ratio_txt}"
            ),
            source_layer_uri=source_layer_uri,
        )
        await emit_chart_payloads(payload)
    except Exception as exc:  # noqa: BLE001 - the A/B is best-effort
        logger.warning("vs30 A/B chart emit failed (non-fatal): %s", exc)


async def _emit_nehrp_amp_chart(
    run_args: OpenQuakeRunArgs,
    *,
    highlight_class: str,
    source_layer_uri: str | None,
) -> None:
    """Emit the discrete NEHRP site-class amplification overlay (best-effort).

    Distinct mechanism from the ``vs30_compare`` A/B (which sweeps a GMPE's
    continuous Vs30 term): here a discrete AmplificationFunction table
    (``amplification.csv``, published ASCE 7-22 Fpga per NEHRP class) is convolved
    into the hazard curve via ``amplification_method = convolution``. Overlays the
    unamplified 760 m/s reference rock against the soft soil classes C/D/E at the
    AOI centroid on the same demo source, and reports the amplification factor for
    the highlighted class in the caption strip.
    """
    try:
        from trid3nt_server.agent.tools.processing.charts_common import (
            build_chart_payload,
        )
        from trid3nt_server.agent.workflows.openquake._local_oq import (
            NEHRP_FPGA,
            NEHRP_VS30,
            aoi_centroid,
            render_amplification_csv,
            render_area_source_model_xml,
            render_classical_amp_job_ini,
            render_site_model_csv,
            render_trivial_gmpe_logic_tree_xml,
            render_trivial_source_logic_tree_xml,
            run_oq_local,
        )

        bbox = tuple(run_args.bbox)
        lon, lat = aoi_centroid(bbox)  # type: ignore[arg-type]
        imt = str(run_args.imt)
        source_xml = render_area_source_model_xml(
            bbox, a_value=float(run_args.a_value), b_value=float(run_args.b_value),  # type: ignore[arg-type]
            min_magnitude=float(run_args.min_magnitude),
            max_magnitude=float(run_args.max_magnitude),
        )
        slt = render_trivial_source_logic_tree_xml()
        glt = render_trivial_gmpe_logic_tree_xml(str(run_args.gmpe))

        def _curve_for(ampcode: str, vs30: float, factor: float) -> dict[str, list[float]]:
            files = {
                "source_model.xml": source_xml,
                "source_model_logic_tree.xml": slt,
                "gmpe_logic_tree.xml": glt,
                "site_model.csv": render_site_model_csv(
                    site_lon=lon, site_lat=lat, vs30=vs30, ampcode=ampcode),
                "amplification.csv": render_amplification_csv(
                    ampcode=ampcode, factor=factor, imt=imt),
                "job.ini": render_classical_amp_job_ini(
                    imt=imt,
                    investigation_time_years=float(run_args.investigation_time_years),
                    max_distance_km=float(run_args.max_distance_km),
                    description=f"NEHRP {ampcode} amplification A/B",
                ),
            }
            outdir = run_oq_local(files, label="nehrpamp")
            curve_files = sorted(outdir.glob("hazard_curve-mean*.csv"))
            if not curve_files:
                return {"imls": [], "poe": []}
            parsed = parse_hazard_curve_csv(curve_files[0].read_text(encoding="utf-8"))
            return {
                "imls": list(parsed.get("hazard_curve_imls_g") or []),
                "poe": list(parsed.get("hazard_curve_mean_poe") or []),
            }

        # Unamplified 760 rock reference (factor 1.0) + soft classes C/D/E, all
        # through the identical convolution deck so curves differ ONLY by the
        # discrete site-class factor.
        classes = [("Rock ref (Vs30 760)", "R", 760.0, 1.0)]
        for cls in ("C", "D", "E"):
            classes.append(
                (f"NEHRP {cls} (Fpga {NEHRP_FPGA[cls]:g})", cls, NEHRP_VS30[cls],
                 NEHRP_FPGA[cls]))

        curves: dict[str, dict[str, list[float]]] = {}
        for label, code, vs30, factor in classes:
            curves[label] = await asyncio.to_thread(_curve_for, code, vs30, factor)

        rows: list[dict[str, Any]] = []
        for label, curve in curves.items():
            for x, p in zip(curve.get("imls") or [], curve.get("poe") or []):
                if float(x) > 0.0 and float(p) > 0.0:
                    rows.append({"iml": float(x), "poe": float(p), "site": label})
        if len({r["site"] for r in rows}) < 2:
            return

        # Amplification factor for the highlighted class: PoE ratio (soil / rock)
        # at the target-PoE IML, read off both curves for the caption strip.
        ref = curves.get("Rock ref (Vs30 760)", {"imls": [], "poe": []})
        hl_label = next(
            (lbl for lbl in curves if lbl.startswith(f"NEHRP {highlight_class} ")),
            None)

        def _poe_at_iml(curve: dict[str, list[float]], target_iml: float) -> float | None:
            best = None
            for x, p in zip(curve.get("imls") or [], curve.get("poe") or []):
                if best is None or abs(x - target_iml) < best[0]:
                    best = (abs(x - target_iml), p)
            return best[1] if best else None

        def _iml_at_poe(curve: dict[str, list[float]], target_poe: float) -> float | None:
            best = None
            for x, p in zip(curve.get("imls") or [], curve.get("poe") or []):
                if p and p > 0 and x and x > 0:
                    if best is None or abs(p - target_poe) < best[0]:
                        best = (abs(p - target_poe), x)
            return best[1] if best else None

        amp_txt = ""
        if hl_label and ref.get("imls"):
            ref_iml = _iml_at_poe(ref, float(run_args.poe))
            hl_iml = _iml_at_poe(curves[hl_label], float(run_args.poe))
            if ref_iml and hl_iml and ref_iml > 0:
                amp_txt = (
                    f" At {run_args.poe * 100:g}% PoE the NEHRP {highlight_class} "
                    f"{imt} is ~{hl_iml / ref_iml:.2f}x the reference-rock value."
                )
            else:
                mid = 0.284  # a representative PGA-scale probe on the ladder
                rp, hp = _poe_at_iml(ref, mid), _poe_at_iml(curves[hl_label], mid)
                if rp and hp and rp > 0:
                    amp_txt = (
                        f" At {mid:g} g the NEHRP {highlight_class} exceedance "
                        f"probability is ~{hp / rp:.2f}x the reference rock.")

        inv = int(round(float(run_args.investigation_time_years))) or 50
        spec = {
            "data": {"values": rows},
            "mark": {"type": "line", "point": True, "tooltip": True},
            "encoding": {
                "x": {"field": "iml", "type": "quantitative",
                      "scale": {"type": "log"}, "title": f"{imt} (g)"},
                "y": {"field": "poe", "type": "quantitative",
                      "scale": {"type": "log"}, "title": f"Mean PoE in {inv}yr"},
                "color": {"field": "site", "type": "nominal", "title": "Site class",
                          "scale": {"range": ["#444444", "#1f8f4e", "#e07a00",
                                              "#c0212f"]}},
            },
            "width": "container",
        }
        payload = build_chart_payload(
            vega_lite_spec=spec,
            title=f"NEHRP site-class amplification - {imt} hazard curve",
            caption=(
                f"Classical {imt} hazard curve at the AOI centroid convolved with a "
                f"discrete NEHRP site-class amplification table (ASCE 7-22 Fpga, "
                f"vs30_ref 760 m/s) over {inv} yr: unamplified reference rock vs "
                f"soft soil classes C/D/E on the same demo source. Deterministic "
                f"median site coefficient (sigma 0).{amp_txt}"
            ),
            source_layer_uri=source_layer_uri,
        )
        await emit_chart_payloads(payload)
    except Exception as exc:  # noqa: BLE001 - the amplification A/B is best-effort
        logger.warning("NEHRP amplification chart emit failed (non-fatal): %s", exc)


# --------------------------------------------------------------------------- #
# Composer.
# --------------------------------------------------------------------------- #
async def model_openquake_psha(
    run_args: OpenQuakeRunArgs,
    *,
    compute_class: str = "standard",
    vs30_compare: float | None = None,
    nehrp_amp_class: str | None = None,
) -> SeismicHazardLayerURI:
    """Run a classical-PSHA OpenQuake hazard calculation end-to-end on AWS Batch.

    Stages a build_spec, dispatches the OpenQuake Batch worker through the
    generic run_solver / wait_for_completion seam, downloads the exported
    hazard-map CSV, and postprocesses it into a published ``SeismicHazardLayerURI``.

    Args:
        run_args: the validated ``OpenQuakeRunArgs``.
        compute_class: FR-CE-3 compute class for the Batch sizing bucket.
            Default ``"standard"`` (OpenQuake is RAM-hungry, so it should size up
            for a larger site grid).

    Returns:
        ``SeismicHazardLayerURI`` (a ``LayerURI`` subtype) -- the emitter appends
        it to ``session-state.loaded_layers`` and the map renders the hazard COG.

    Raises:
        OpenQuakeWorkflowError: any staging / dispatch / postprocess step failed.
    """
    from trid3nt_server.agent.tools.simulation.solver.solver import run_solver, wait_for_completion

    run_id = new_ulid()
    logger.info(
        "model_openquake_psha run_id=%s bbox=%s imt=%s poe=%.4g "
        "inv_time=%.0fyr grid=%.1fkm gmpe=%s compute_class=%s",
        run_id,
        run_args.bbox,
        run_args.imt,
        run_args.poe,
        run_args.investigation_time_years,
        run_args.site_grid_spacing_km,
        run_args.gmpe,
        compute_class,
    )

    # Declare the planned child count up front so the parent card's live
    # breadcrumb can render "k/5" (faults -> build_spec -> solve -> download ->
    # publish). No-op when no emitter is bound (verify/CI direct-call path).
    begin_substeps(current_emitter(), 5)

    # 0): resolve REAL active-fault sources for the AOI (sync fetch off
    #    the loop). Non-empty => build the fault source model + narrate
    #    "real-fault"; empty => honest synthetic-area fallback. NEVER fails the
    #    run (resolve_fault_sources degrades to [] on any fetch error).
    # Epistemic logic-tree modes use a synthetic multi-branch demo source (the
    # LogicTreeCase1/Case2 mechanism), so they SKIP the real-fault fetch -- the
    # question is source-parameter epistemic uncertainty, not fault-trace hazard.
    _logic_tree = str(getattr(run_args, "logic_tree", "single"))
    async with substep(current_emitter(), "resolve_fault_sources"):
        if _logic_tree == "single":
            fault_sources, source_model_note = await asyncio.to_thread(
                resolve_fault_sources, list(run_args.bbox)
            )
        else:
            fault_sources = []
            source_model_note = _EPISTEMIC_SOURCE_NOTE.get(
                _logic_tree, "Epistemic logic-tree hazard over a synthetic demo source."
            )
    used_real_faults = bool(fault_sources)
    source_model_kind = "real-fault" if used_real_faults else "synthetic-area"
    logger.info(
        "model_openquake_psha run_id=%s source_model_kind=%s "
        "(fault_count=%d): %s",
        run_id,
        source_model_kind,
        len(fault_sources),
        source_model_note,
    )

    # 0.5): surface the resolved fault traces as a renderable INPUT
    #      layer so the user can SEE the fault lines the hazard peaks on.
    #      GATED on ``used_real_faults`` (no traces -> nothing to draw). The serialize +
    #      S3 upload is SYNC boto3, OFFLOADED off the loop; the emit is BEST-
    #      EFFORT (publish_input_layer never raises) so a failure to surface the
    #      faults can NEVER fail the solve. role="input" + bbox=None: the traces
    #      render under the hazard COG and do not fight the AOI camera.
    # GATED additionally on an emitter being bound: there is no point serializing
    # + uploading the fault GeoJSON when nothing can surface it (the verify/CI
    # direct-call path), and skipping it keeps that path free of boto3.
    if used_real_faults and current_emitter() is not None:
        try:
            fault_layer = await asyncio.to_thread(
                make_fault_sources_layer_uri, fault_sources, run_id=run_id
            )
            await publish_input_layer(current_emitter(), fault_layer)
        except Exception as exc:  # noqa: BLE001 - input surfacing is NEVER fatal
            logger.warning(
                "model_openquake_psha: fault-trace input surface failed "
                "(non-fatal) run_id=%s: %s",
                run_id,
                exc,
            )

    # 1) Stage the build_spec (sync boto3 off the loop). Thread the resolved
    #    fault sources so a real-fault AOI stages the simpleFaultSource model.
    async with substep(current_emitter(), "stage_openquake_build_spec"):
        build_spec_uri = await asyncio.to_thread(
            lambda: stage_openquake_build_spec(
                run_args, run_id, fault_sources=fault_sources or None
            )
        )

    # 2) Dispatch through the generic run_solver / wait_for_completion seam.
    #    Surface the dispatch + Batch wait as a single "Solved (Batch ...)" child
    #    row; the live Batch readout stays owned by the two-card Sim observability
    #    inside run_solver / wait_for_completion (mint_dispatch_and_sim_cards).
    async with substep(current_emitter(), "run_solver"):
        handle = run_solver(
            solver=OPENQUAKE_SOLVER_NAME,
            model_setup_uri=build_spec_uri,
            compute_class=compute_class,
        )
        run_result = await wait_for_completion(handle)

        # Honesty floor: a non-complete Batch result raises INSIDE the substep so
        # the solve child reads red (failed), not a silent green. The raise re-
        # raises through the substep wrapper unchanged (caller control flow).
        if run_result.status != "complete":
            raise OpenQuakeWorkflowError(
                "OQ_SOLVE_FAILED",
                message=(
                    "OpenQuake Batch solve did not complete "
                    f"(status={run_result.status}, error_code={run_result.error_code}): "
                    f"{run_result.error_message or run_result.cancellation_reason or ''}"
                ),
                details={
                    "run_id": run_id,
                    "output_uri": run_result.output_uri,
                },
            )

    # --- Register-only branch (worker postprocess offload) -------------------
    # If the worker wrote a publish_manifest.json (schema_version==1), read +
    # schema-gate it and SHORT-CIRCUIT the on-box heavy tail (no CSV download,
    # no postprocess_openquake). Degrades cleanly to legacy path when absent.
    from trid3nt_server.agent.workflows.shared.register_published_manifest import (
        read_publish_manifest,
        register_manifest_layers,
    )

    batch_run_id = getattr(run_result, "run_id", None) or run_id
    _oq_manifest = await asyncio.to_thread(read_publish_manifest, run_result)
    if _oq_manifest is not None:
        logger.info(
            "model_openquake_psha: REGISTER-ONLY path (worker postprocess "
            "offload) run_id=%s engine=%s layers=%d",
            batch_run_id, _oq_manifest.engine, len(_oq_manifest.layers),
        )
        async with substep(current_emitter(), "publish_layer"):
            _oq_reg = register_manifest_layers(
                _oq_manifest, run_id=batch_run_id,
                bbox=tuple(run_args.bbox) if run_args.bbox else None,
            )
        _oq_primary_layers = [lyr for lyr in _oq_reg.layers if lyr.role == "primary"]
        if not _oq_primary_layers:
            raise OpenQuakeWorkflowError(
                "OQ_NO_LAYERS",
                message="worker publish_manifest produced no primary layer (empty solve?)",
            )
        _oq_prim = _oq_primary_layers[0]
        _oq_m = _oq_reg.metrics
        _poe = float(run_args.poe)
        _inv = float(run_args.investigation_time_years)
        try:
            _rp = -_inv / math.log(1.0 - _poe)
        except (ValueError, ZeroDivisionError):
            _rp = 0.0
        _oq_layer = SeismicHazardLayerURI(
            uri=_oq_prim.uri,
            layer_type=_oq_prim.layer_type,
            layer_id=_oq_prim.layer_id,
            name=_oq_prim.name,
            style_preset=_oq_prim.style_preset,
            bbox=tuple(run_args.bbox) if run_args.bbox else None,
            role=_oq_prim.role,
            imt=run_args.imt,
            poe=_poe,
            investigation_time_years=_inv,
            return_period_years=_rp,
            max_hazard_value=float(_oq_m.get("max_pga_g", 0.0)),
            hazard_area_km2=float(_oq_m.get("hazard_area_km2", 0.0)),
            n_sites=int(_oq_m.get("n_sites", 0)),
            source_model_kind=source_model_kind,
            source_model_note=source_model_note,
            logic_tree=_logic_tree,
            n_realizations=_EPISTEMIC_N_REALIZATIONS.get(_logic_tree, 0),
        )
        # epistemic 5/50/95 quantile-band + UHS charts (best-effort, non-fatal).
        await _emit_oq_curve_charts(
            batch_run_id, imt=run_args.imt, poe=float(run_args.poe),
            investigation_time_years=float(run_args.investigation_time_years),
            source_layer_uri=_oq_layer.uri, logic_tree=_logic_tree,
        )
        if vs30_compare is not None:
            await _emit_vs30_ab_chart(
                run_args, vs30_rock=float(run_args.reference_vs30_ms),
                vs30_compare=float(vs30_compare), source_layer_uri=_oq_layer.uri,
            )
        if nehrp_amp_class is not None:
            await _emit_nehrp_amp_chart(
                run_args, highlight_class=str(nehrp_amp_class).upper(),
                source_layer_uri=_oq_layer.uri,
            )
        return _oq_layer

    # 3) Download the hazard-map CSV from the worker's run_id prefix (the Batch
    #    dispatch mints a fresh run_id; the worker writes under run_result.run_id,
    #    NOT the composer's run_id -- mirror the SWMM/SFINCS Batch lesson).
    async with substep(current_emitter(), "_download_batch_hazard_csv"):
        hazard_csv_text = await asyncio.to_thread(
            _download_batch_hazard_csv, run_result, batch_run_id
        )

    # 4) Postprocess: rasterize site values -> hazard COG + publish.
    try:
        async with substep(current_emitter(), "postprocess_openquake"):
            layer = await asyncio.to_thread(
                postprocess_openquake,
                hazard_csv_text,
                run_id=batch_run_id,
                imt=run_args.imt,
                poe=float(run_args.poe),
                investigation_time_years=float(run_args.investigation_time_years),
            )
    except PostprocessOpenQuakeError as exc:
        raise OpenQuakeWorkflowError(
            exc.error_code,
            message=str(exc),
            details={"run_id": batch_run_id, **getattr(exc, "details", {})},
        ) from exc

    # honesty floor: stamp the source-model provenance onto the typed
    # layer so the agent narrates real-vs-fallback from a TYPED field, never a
    # free claim. ``source_model_kind`` reflects the path THIS run actually took
    # (postprocess builds the layer with the synthetic-area default; we flip it to
    # real-fault ONLY when fault sources were actually staged).
    layer = layer.model_copy(
        update={
            "source_model_kind": source_model_kind,
            "source_model_note": source_model_note,
            "logic_tree": _logic_tree,
            "n_realizations": _EPISTEMIC_N_REALIZATIONS.get(_logic_tree, 0),
        }
    )

    # wire the NON-RASTER PSHA products (hazard CURVE + UHS) to charts.
    # The documented FOLLOW-UP (postprocess_openquake.py:524-531): the chart
    # producers consume the parsed curve arrays, not a layer URI. Best-effort -
    # a missing curve emits no chart (the honesty floor); never fails the run.
    await _emit_oq_curve_charts(
        batch_run_id,
        imt=run_args.imt,
        poe=float(run_args.poe),
        investigation_time_years=float(run_args.investigation_time_years),
        source_layer_uri=layer.uri,
        logic_tree=_logic_tree,
    )

    # row-4 Vs30 site-response A/B overlay (best-effort, non-fatal).
    if vs30_compare is not None:
        await _emit_vs30_ab_chart(
            run_args, vs30_rock=float(run_args.reference_vs30_ms),
            vs30_compare=float(vs30_compare), source_layer_uri=layer.uri,
        )
    # discrete NEHRP site-class amplification overlay (best-effort, non-fatal).
    if nehrp_amp_class is not None:
        await _emit_nehrp_amp_chart(
            run_args, highlight_class=str(nehrp_amp_class).upper(),
            source_layer_uri=layer.uri,
        )

    logger.info(
        "model_openquake_psha complete run_id=%s layer_id=%s "
        "source_model_kind=%s max_hazard=%.6g hazard_area_km2=%.6g n_sites=%d "
        "uri=%s",
        batch_run_id,
        layer.layer_id,
        layer.source_model_kind,
        layer.max_hazard_value,
        layer.hazard_area_km2,
        layer.n_sites,
        layer.uri,
    )
    return layer


# --------------------------------------------------------------------------- #
# OpenQuake LocalSolverSpec -- subprocess runner for the local-docker backend.
#
# exec_kind="exec": OpenQuake ships as a containerized CLI; there is no PyPI
# package that exposes a stable in-process API. For the offline / local build we
# run the worker entrypoint as a subprocess: ``sys.executable -m
# services.workers.openquake.entrypoint --run-id <id> --manifest-uri <uri>``
# with the repo root on PYTHONPATH. The worker renders the deck via
# ``job_ini.render_openquake_deck``, runs ``oq engine --run job.ini``, and writes
# completion.json + uploads output CSVs.
#
# IMPORTANT: the ``oq`` CLI binary must be on PATH in the subprocess environment
# (installed via the ``local-engines`` extra: ``pip install openquake.engine``).
# The spec is INERT unless TRID3NT_SOLVER_BACKEND=local-docker; the cloud Batch
# path (run_solver with aws-batch backend) is unchanged.
# --------------------------------------------------------------------------- #

#: Path to the thin OpenQuake subprocess shim (mirrors run_inp.py for SWMM).
_OQ_RUN_OQ = (
    Path(__file__).resolve().parents[7]
    / "services" / "workers" / "openquake" / "run_oq.py"
)

#: Repo root so the subprocess can resolve ``services.workers.*`` imports.
_TRID3NT_REPO_ROOT_OQ = str(Path(__file__).resolve().parents[7])


def openquake_local_spec() -> Any:
    """Build the OpenQuake ``LocalSolverSpec`` for the local-docker backend.

    Runs the thin CLI shim ``services/workers/openquake/run_oq.py`` as a
    subprocess in the current venv. The shim reads manifest.json from the
    rundir, renders the OpenQuake deck, and drives ``oq engine --run job.ini``.
    The SUPERVISOR (not the shim) writes completion.json and uploads output.

    The repo root is prepended to PYTHONPATH so the shim's
    ``from services.workers.*`` imports resolve.

    The ``oq`` CLI binary must be on PATH (installed via ``openquake.engine``
    in the local-engines optional extra). If absent, the shim exits 127 and
    the completion records status="error".

    This spec is the SUBPROCESS RUNNER for the offline / local build. The
    cloud Batch path is unchanged.
    """
    from trid3nt_server.agent.tools.simulation.solver.solver import LOCAL_EXEC_WORKFLOW_NAME, LocalSolverSpec

    repo_root = _TRID3NT_REPO_ROOT_OQ
    # Prepend the repo root to PYTHONPATH so the shim can import
    # ``services.workers.openquake.*`` and ``services.workers._raster_postprocess``.
    existing_pypath = os.environ.get("PYTHONPATH", "")
    new_pypath = f"{repo_root}:{existing_pypath}" if existing_pypath else repo_root

    def build_argv(run_id: str, rundir: Path, args: list[str]) -> list[str]:
        del args, run_id  # manifest.json is written to rundir by the supervisor
        return [
            sys.executable,
            str(_OQ_RUN_OQ),
            "--manifest",
            "manifest.json",
        ]

    def classify_exit(
        rundir: Path, exit_code: int
    ) -> "tuple[str, int, str | None, dict]":
        if exit_code != 0:
            return "error", exit_code, f"openquake worker exited with code {exit_code}", {}
        return "ok", 0, None, {}

    return LocalSolverSpec(
        solver=OPENQUAKE_SOLVER_NAME,
        workflow_name=LOCAL_EXEC_WORKFLOW_NAME,
        args_key="oq_args",
        build_argv=build_argv,
        stdout_name="oq.stdout",
        stderr_name="oq.stderr",
        stdout_uri_field="oq_stdout_uri",
        stderr_uri_field="oq_stderr_uri",
        exec_kind="exec",
        classify_exit=classify_exit,
        env_overrides={"PYTHONPATH": new_pypath},
    )


def register_openquake_solver() -> None:
    """Register ``'openquake'`` local spec in ``tools.simulation.solver.LOCAL_SOLVER_SPEC_REGISTRY``.

    Mirrors ``run_swmm.register_swmm_solver``. Idempotent -- safe to call at
    import. The factory (``openquake_local_spec``) is only called at dispatch
    time to avoid any circular-import hazard.
    """
    from trid3nt_server.agent.tools.simulation.solver.solver import register_local_solver_spec

    register_local_solver_spec(OPENQUAKE_SOLVER_NAME, openquake_local_spec)


# Register at import so ``run_solver(solver='openquake')`` with
# TRID3NT_SOLVER_BACKEND=local-docker dispatches to the subprocess spec.
register_openquake_solver()
