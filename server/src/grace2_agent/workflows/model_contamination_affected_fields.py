"""``model_contamination_affected_fields`` — the which-farm-fields composer.

The contamination-plume x Fields-of-the-World demo composer (section-7 step S3
of reports/design/demo_spike_contamination_fotw.md): given a farmland AOI (a
place name or bbox) + a contaminant + release params (or a spill news article),
it places a spill UP-GRADIENT of the field cluster, runs the EXISTING MODFLOW
plume engine behind the existing solver-confirm gate, fetches the FTW / fiboa
agricultural field boundaries, and runs ``analyze_affected_fields`` to answer
"which farm fields does the plume reach, and how badly", ranked.

It is the structural sibling of ``model_groundwater_contamination_scenario``
(Case 2): the plume half (parameter assembly -> confirmation gate ->
``run_modflow_job``) is the same shape, and the analysis half mirrors
``compute_impact_envelope`` (per-feature -> ranked aggregate -> headline).

Chain:

    1. SCOPE + GEOCODE
       - Resolve the AOI: an explicit ``bbox`` is used directly; otherwise
         ``location_query`` is geocoded (``geocode_location``) to a bbox +
         centroid. The default demo AOI is US cropland (FTW headline coverage).
       - When ``article_text`` is supplied, the spill parameters (contaminant,
         release rate, duration, location) are EXTRACTED via the Case 2
         ``extract_spill_parameters`` and the field-cluster centroid drives the
         spill placement.

    2. UP-GRADIENT SPILL PLACEMENT
       - The demo deck drives a regional WEST -> EAST groundwater gradient via
         the CHD boundary, so "up-gradient of the fields" = WEST of the field
         cluster centroid. ``place_spill_up_gradient`` offsets the centroid west
         by a configurable distance (honoring the deck gradient direction). An
         explicit ``spill_location_latlon`` overrides the auto-placement
         (deterministic / testable; gotcha UP-GRADIENT PLACEMENT).

    3. CONFIRMATION BEFORE CONSEQUENCE (Invariant 9)
       - The MODFLOW run is gated behind the existing solver-confirm machinery
         (``confirmed`` injected True by the server gate only after the user
         approves the derived forcing). Without that injection AND without a
         proceeding ``confirmation_hook``, the gate FAILS CLOSED — no run.

    4. RUN PLUME
       - ``run_modflow_job`` builds the GWF+GWT deck, runs mf6 (Batch / local),
         and returns a ``PlumeLayerURI`` (concentration COG + max_concentration_mgl
         + plume_area_km2).

    5. FETCH FIELDS
       - ``fetch_field_boundaries`` returns the FTW / fiboa field polygons for
         the AOI (each carrying a ``crop_name``). Outside FTW coverage it raises
         ``FIELDS_NO_COVERAGE`` — surfaced HONESTLY, never fabricated.

    6. ANALYZE
       - ``analyze_affected_fields`` intersects the plume COG against each field,
         splits affected vs untouched at the plume detection threshold, ranks,
         and emits the per-field readout + headline.

    7. RETURN
       - An ``AffectedFieldsResult`` carrying the plume layer + the field
         boundaries layer + the ranked affected-field readout + a narration
         summary (every narrated number a typed field, Invariant 1).

Invariants:
- **1. Determinism boundary: preserves.** Every narrated number reads off the
  ``PlumeLayerURI`` scalars + the deterministic ``analyze_affected_fields``
  output; no LLM call anywhere in this module.
- **2. Deterministic workflows: preserves.** Straight-line Python over
  registered atomic tools + ``run_modflow_job``; typed-exception boundary.
- **8. Cancellation is first-class: preserves.** Every ``await`` propagates
  ``asyncio.CancelledError`` untouched.
- **9. Confirmation before consequence: preserves.** The MODFLOW run is gated;
  the gate fails closed (no hook + ``confirmed=False`` -> no run).
- **10. Minimal parameter surface: preserves.** The signature exposes intent
  (AOI + contaminant + release schedule, or an article); aquifer K / porosity
  are demo defaults from the contract; the spill point is auto-placed
  up-gradient unless explicitly supplied.

No-sync-blocking: the heavy steps (FTW parquet read, plume zonal scoring) run
in ``asyncio.to_thread`` via the registry callables so they never stall the WS
heartbeat (``feedback_no_sync_blocking_on_asyncio_loop``).
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any, Awaitable, Callable

from pydantic import Field

from grace2_contracts.common import GraceModel
from grace2_contracts.execution import LayerURI
from grace2_contracts.modflow_contracts import (
    DEFAULT_AQUIFER_K_MS,
    DEFAULT_POROSITY,
    MODFLOWRunArgs,
    PlumeLayerURI,
)
from grace2_contracts.payload_warning import PayloadWarningEnvelopePayload

from ..tools import TOOL_REGISTRY, register_tool
from grace2_contracts.tool_registry import AtomicToolMetadata

logger = logging.getLogger(
    "grace2_agent.workflows.model_contamination_affected_fields"
)

__all__ = [
    "AffectedFieldsResult",
    "model_contamination_affected_fields",
    "run_model_contamination_affected_fields",
    "place_spill_up_gradient",
    "resolve_aoi_bbox",
    "ContaminationAffectedFieldsError",
    "ContaminationAffectedFieldsInputError",
    "ContaminationAffectedFieldsGeocodeError",
    "ContaminationAffectedFieldsNoCoverageError",
    "ContaminationAffectedFieldsConfirmationDeniedError",
    "DEFAULT_UPGRADIENT_OFFSET_KM",
    "ConfirmationHook",
]


# --------------------------------------------------------------------------- #
# Up-gradient placement constant.
# --------------------------------------------------------------------------- #

#: How far WEST (up-gradient) of the field-cluster centroid to place the spill,
#: in kilometers, when the spill point is auto-placed. The demo deck drives a
#: regional WEST -> EAST gradient via CHD, so up-gradient = west. ~3 km puts the
#: source comfortably outside (and hydraulically upstream of) a county-scale
#: field AOI so the plume migrates INTO the field cluster.
DEFAULT_UPGRADIENT_OFFSET_KM: float = 3.0

#: One degree of latitude in km (constant); longitude degrees shrink by cos(lat).
_KM_PER_DEG_LAT: float = 111.32


# --------------------------------------------------------------------------- #
# Result envelope (agent-local, like Case2Result).
# --------------------------------------------------------------------------- #


class AffectedFieldsResult(GraceModel):
    """Return type for ``model_contamination_affected_fields``.

    Bundles the plume layer + the FTW field-boundary layer + the ranked
    affected-field readout + a narration summary. Invariant 1: every narrated
    number is a typed field (the ``PlumeLayerURI`` scalars + the deterministic
    ``analyze_affected_fields`` output).

    Fields:
        plume_layer: the ``PlumeLayerURI`` the MODFLOW run produced.
        fields_layer: the FTW / fiboa field-boundary ``LayerURI``.
        affected: the ``analyze_affected_fields`` result dict (ranked
            affected_fields + counts + headline).
        summary: narration dict ``{location_name, contaminant,
            n_fields_affected, affected_area_km2, worst_field, plume_area_km2,
            max_concentration_mgl, demo_aquifer_caveat}``.
        spill_location_latlon: the (lat, lon) point the spill was placed at.
        confirmation_envelope: the parameter-confirmation envelope that gated
            the run, serialized for the surface.
    """

    schema_version: str = "v1"

    plume_layer: PlumeLayerURI
    fields_layer: LayerURI
    affected: dict[str, Any] = Field(default_factory=dict)
    summary: dict[str, Any] = Field(default_factory=dict)
    spill_location_latlon: list[float] = Field(default_factory=list)
    confirmation_envelope: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Typed errors (FR-AS-11).
# --------------------------------------------------------------------------- #


class ContaminationAffectedFieldsError(RuntimeError):
    """Base class for ``model_contamination_affected_fields`` failures."""

    error_code: str = "CONTAMINATION_AFFECTED_FIELDS_ERROR"
    retryable: bool = False


class ContaminationAffectedFieldsInputError(ContaminationAffectedFieldsError):
    """Missing / invalid input (no AOI, no contaminant + no article, etc.)."""

    error_code = "CONTAMINATION_AFFECTED_FIELDS_INPUT_INVALID"


class ContaminationAffectedFieldsGeocodeError(ContaminationAffectedFieldsError):
    """``geocode_location`` returned no usable bbox / centroid for the AOI."""

    error_code = "CONTAMINATION_AFFECTED_FIELDS_GEOCODE_FAILED"


class ContaminationAffectedFieldsNoCoverageError(
    ContaminationAffectedFieldsError
):
    """No published FTW / fiboa field-boundary dataset covers the AOI.

    Surfaced HONESTLY (``feedback_data_source_fallback_norm``): the FTW corpus is
    regional (US / Japan / Denmark). Pick a US-cropland AOI (Ames, Iowa /
    Nebraska / Central Valley) so the demo stays inside coverage. Not retryable
    for the same out-of-coverage AOI.
    """

    error_code = "CONTAMINATION_AFFECTED_FIELDS_NO_COVERAGE"


class ContaminationAffectedFieldsConfirmationDeniedError(
    ContaminationAffectedFieldsError
):
    """The user declined / timed out at the parameter-confirmation gate.

    Confirmation fails closed (Invariant 9): no MODFLOW run proceeds.
    """

    error_code = "CONTAMINATION_AFFECTED_FIELDS_CONFIRMATION_DENIED"


# --------------------------------------------------------------------------- #
# Confirmation-hook seam (same shape as the Case 2 composer).
# --------------------------------------------------------------------------- #

ConfirmationHook = Callable[[PayloadWarningEnvelopePayload], Awaitable[bool]]


# --------------------------------------------------------------------------- #
# Registry seam.
# --------------------------------------------------------------------------- #


def _registry_fn(name: str) -> Any:
    """Resolve ``name`` to the registered tool callable (registry seam)."""
    entry = TOOL_REGISTRY.get(name)
    if entry is None:
        raise ContaminationAffectedFieldsError(
            f"required atomic tool {name!r} is not registered"
        )
    return entry.fn


# --------------------------------------------------------------------------- #
# AOI + up-gradient placement helpers (pure; unit-testable).
# --------------------------------------------------------------------------- #


def resolve_aoi_bbox(
    bbox: tuple[float, float, float, float] | list[float] | None,
    location_query: str | None,
) -> tuple[tuple[float, float, float, float], tuple[float, float]]:
    """Resolve the AOI to a ``(bbox, centroid_latlon)`` pair.

    An explicit ``bbox`` is used directly (its centroid derived); otherwise
    ``location_query`` is geocoded. Returns the WGS84 bbox
    ``(min_lon, min_lat, max_lon, max_lat)`` + the centroid ``(lat, lon)``.

    Raises:
        ContaminationAffectedFieldsInputError: neither bbox nor location_query.
        ContaminationAffectedFieldsGeocodeError: geocode returned no bbox.
    """
    if bbox is not None:
        if len(bbox) != 4:
            raise ContaminationAffectedFieldsInputError(
                f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
            )
        b = (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
        centroid = ((b[1] + b[3]) / 2.0, (b[0] + b[2]) / 2.0)  # (lat, lon)
        return b, centroid

    if not location_query or not str(location_query).strip():
        raise ContaminationAffectedFieldsInputError(
            "model_contamination_affected_fields requires either bbox or "
            "location_query (the farmland AOI)."
        )

    geocode_fn = _registry_fn("geocode_location")
    try:
        geo = geocode_fn(location_query)
    except Exception as exc:  # noqa: BLE001
        raise ContaminationAffectedFieldsGeocodeError(
            f"geocode_location({location_query!r}) failed: {exc}"
        ) from exc
    geo_bbox = geo.get("bbox") if isinstance(geo, dict) else None
    if not geo_bbox or len(geo_bbox) != 4:
        raise ContaminationAffectedFieldsGeocodeError(
            f"geocode_location({location_query!r}) returned no usable bbox: {geo!r}"
        )
    b = (
        float(geo_bbox[0]),
        float(geo_bbox[1]),
        float(geo_bbox[2]),
        float(geo_bbox[3]),
    )
    lat = geo.get("latitude")
    lon = geo.get("longitude")
    if lat is None or lon is None:
        centroid = ((b[1] + b[3]) / 2.0, (b[0] + b[2]) / 2.0)
    else:
        centroid = (float(lat), float(lon))
    return b, centroid


def place_spill_up_gradient(
    field_centroid_latlon: tuple[float, float],
    offset_km: float = DEFAULT_UPGRADIENT_OFFSET_KM,
) -> tuple[float, float]:
    """Place the spill UP-GRADIENT (WEST) of the field-cluster centroid.

    The demo deck drives a regional WEST -> EAST groundwater gradient via the
    CHD boundary, so up-gradient = west of the field centroid. Offsets the
    centroid west by ``offset_km`` (longitude degrees scaled by the centroid
    latitude's cosine) so the plume migrates eastward INTO the field cluster.

    Returns the spill point ``(lat, lon)`` (latitude unchanged; longitude moved
    west). Honors the deck gradient direction (gotcha UP-GRADIENT PLACEMENT).
    """
    lat, lon = float(field_centroid_latlon[0]), float(field_centroid_latlon[1])
    cos_lat = max(math.cos(math.radians(lat)), 1e-6)  # guard the poles
    deg_per_km_lon = 1.0 / (_KM_PER_DEG_LAT * cos_lat)
    spill_lon = lon - offset_km * deg_per_km_lon  # WEST = decreasing longitude
    # Clamp to the valid longitude range defensively.
    spill_lon = max(-180.0, min(180.0, spill_lon))
    return (lat, spill_lon)


# --------------------------------------------------------------------------- #
# Confirmation envelope + summary (deterministic).
# --------------------------------------------------------------------------- #


def _build_confirmation_envelope(
    run_args: MODFLOWRunArgs, location_name: str
) -> PayloadWarningEnvelopePayload:
    """Compose the parameter-confirmation envelope (payload-warning pattern)."""
    from grace2_contracts import new_ulid

    lat, lon = run_args.spill_location_latlon
    caveat = (
        f"Demo aquifer parameterization (K={run_args.aquifer_k_ms:g} m/s, "
        f"porosity={run_args.porosity:g}) with a regional west->east gradient "
        f"(spill placed up-gradient, west of the fields) — NOT site-specific "
        f"hydrogeology. Confirm to run the MODFLOW plume for "
        f"{run_args.contaminant} near {location_name} and intersect it against "
        f"the farm-field boundaries."
    )
    return PayloadWarningEnvelopePayload(
        warning_id=new_ulid(),
        tool_name="run_modflow_job",
        tool_args={
            "contaminant": run_args.contaminant,
            "location_name": location_name,
            "spill_location_latlon": [lat, lon],
            "release_rate_kg_s": run_args.release_rate_kg_s,
            "duration_days": run_args.duration_days,
            "aquifer_k_ms": run_args.aquifer_k_ms,
            "porosity": run_args.porosity,
        },
        estimated_mb=0.0,
        threshold_mb=0.0,
        recommendation=caveat[:512],
        options=["proceed", "cancel"],
    )


def _build_summary(
    location_name: str,
    contaminant: str,
    plume: PlumeLayerURI,
    affected: dict[str, Any],
) -> dict[str, Any]:
    """Build the narration summary dict (every number a typed field)."""
    return {
        "location_name": location_name,
        "contaminant": contaminant,
        "n_fields_total": affected.get("n_fields_total"),
        "n_fields_affected": affected.get("n_fields_affected"),
        "affected_area_km2": affected.get("affected_area_km2"),
        "worst_field": affected.get("worst_field"),
        "headline": affected.get("headline"),
        "plume_area_km2": plume.plume_area_km2,
        "max_concentration_mgl": plume.max_concentration_mgl,
        "demo_aquifer_caveat": (
            f"Aquifer K={DEFAULT_AQUIFER_K_MS:g} m/s, porosity={DEFAULT_POROSITY:g} "
            "are demo defaults with a demo west->east regional gradient, not "
            "site-specific hydrogeology."
        ),
    }


# --------------------------------------------------------------------------- #
# Pipeline-emitter helper (mirror the Case 2 composer).
# --------------------------------------------------------------------------- #


async def _maybe_emit(
    emitter: Any | None,
    *,
    name: str,
    tool_name: str,
    invoke: Any,
) -> Any:
    """Run ``invoke()`` through ``emitter.emit_tool_call`` if given, else direct."""
    if emitter is not None:
        return await emitter.emit_tool_call(
            name=name, tool_name=tool_name, invoke=invoke
        )
    result = invoke()
    if asyncio.iscoroutine(result):
        result = await result
    return result


# --------------------------------------------------------------------------- #
# The composer.
# --------------------------------------------------------------------------- #


async def model_contamination_affected_fields(
    location_query: str | None = None,
    bbox: tuple[float, float, float, float] | list[float] | None = None,
    contaminant: str = "trichloroethylene",
    release_rate_kg_s: float = 0.05,
    duration_days: float = 1.0,
    article_text: str | None = None,
    spill_location_latlon: tuple[float, float] | list[float] | None = None,
    upgradient_offset_km: float = DEFAULT_UPGRADIENT_OFFSET_KM,
    threshold_mgl: float | None = None,
    rank_by: str = "peak",
    *,
    confirmed: bool = False,
    confirmation_hook: ConfirmationHook | None = None,
    aquifer_k_ms: float | None = None,
    porosity: float | None = None,
    compute_class: str = "standard",
    pipeline_emitter: Any | None = None,
) -> AffectedFieldsResult:
    """Compose AOI -> up-gradient spill -> MODFLOW plume -> FTW fields -> analysis.

    See the module docstring for the full chain. Exactly one AOI source is
    required (``bbox`` OR ``location_query``). When ``article_text`` is supplied
    the contaminant + release schedule + location are EXTRACTED via the Case 2
    extractor (overriding the explicit contaminant / release args).

    Raises:
        ContaminationAffectedFieldsInputError: no AOI / invalid args.
        ContaminationAffectedFieldsGeocodeError: AOI geocode failed.
        ContaminationAffectedFieldsNoCoverageError: AOI outside FTW coverage.
        ContaminationAffectedFieldsConfirmationDeniedError: gate not approved.
        Propagates ``asyncio.CancelledError`` from any await (Invariant 8).
    """
    # --- Stage 0: optional article extraction (overrides explicit params) -- #
    location_name = location_query or "the farmland AOI"
    if article_text and article_text.strip():
        from .model_groundwater_contamination_scenario import (
            extract_spill_parameters,
        )

        derived = await _maybe_emit(
            pipeline_emitter,
            name="Extract spill parameters",
            tool_name="aggregate_claims_across_sources",
            invoke=lambda: extract_spill_parameters(
                str(article_text), geocode=False
            ),
        )
        contaminant = derived["contaminant"]
        release_rate_kg_s = derived["release_rate_kg_s"]
        duration_days = derived["duration_days"]
        if not location_query and derived.get("location_name"):
            location_query = derived["location_name"]
            location_name = location_query

    # --- Stage 1: resolve the AOI bbox + field-cluster centroid ----------- #
    resolved_bbox, centroid = resolve_aoi_bbox(bbox, location_query)
    if location_query:
        location_name = location_query

    # --- Stage 2: place the spill up-gradient (or honor explicit point) ---- #
    if spill_location_latlon is not None:
        spill_pt = (
            float(spill_location_latlon[0]),
            float(spill_location_latlon[1]),
        )
    else:
        spill_pt = place_spill_up_gradient(centroid, upgradient_offset_km)

    # --- assemble + validate the forcing contract ------------------------- #
    kwargs: dict[str, Any] = dict(
        spill_location_latlon=spill_pt,
        contaminant=contaminant,
        release_rate_kg_s=float(release_rate_kg_s),
        duration_days=float(duration_days),
    )
    if aquifer_k_ms is not None:
        kwargs["aquifer_k_ms"] = float(aquifer_k_ms)
    if porosity is not None:
        kwargs["porosity"] = float(porosity)
    try:
        run_args = MODFLOWRunArgs(**kwargs)
    except Exception as exc:  # noqa: BLE001 — pydantic ValidationError
        raise ContaminationAffectedFieldsInputError(
            f"derived parameters failed MODFLOWRunArgs validation: {exc}"
        ) from exc

    # --- Stage 3: CONFIRMATION BEFORE CONSEQUENCE (Invariant 9) ----------- #
    envelope = _build_confirmation_envelope(run_args, location_name)
    if not confirmed:
        proceed = False
        if confirmation_hook is not None:
            proceed = bool(await confirmation_hook(envelope))
        if not proceed:
            logger.info(
                "affected-fields confirmation denied / no hook; MODFLOW run NOT "
                "started (fail-closed) aoi=%r",
                location_name,
            )
            raise ContaminationAffectedFieldsConfirmationDeniedError(
                "MODFLOW run not started: the parameter-confirmation gate was "
                "not approved (declined, timed out, or no confirmation channel)."
            )

    # --- Stage 4: run the MODFLOW plume ----------------------------------- #
    run_modflow_fn = _registry_fn("run_modflow_job")
    plume_result = await _maybe_emit(
        pipeline_emitter,
        name=f"Model groundwater plume [{contaminant}]",
        tool_name="run_modflow_job",
        invoke=lambda: run_modflow_fn(
            spill_location_latlon=run_args.spill_location_latlon,
            contaminant=run_args.contaminant,
            release_rate_kg_s=run_args.release_rate_kg_s,
            duration_days=run_args.duration_days,
            aquifer_k_ms=run_args.aquifer_k_ms,
            porosity=run_args.porosity,
            compute_class=compute_class,
        ),
    )
    if not isinstance(plume_result, PlumeLayerURI):
        error_code = "MODFLOW_RUN_FAILED"
        error_message = "MODFLOW run did not produce a plume layer"
        if isinstance(plume_result, dict):
            error_code = plume_result.get("error_code", error_code)
            error_message = plume_result.get("error_message", error_message)
        raise ContaminationAffectedFieldsError(f"{error_code}: {error_message}")
    plume = plume_result

    # --- Stage 5: fetch the FTW field boundaries (honest no-coverage) ----- #
    fetch_fields_fn = _registry_fn("fetch_field_boundaries")
    try:
        fields_layer = await _maybe_emit(
            pipeline_emitter,
            name="Fetch farm-field boundaries (Fields of The World)",
            tool_name="fetch_field_boundaries",
            invoke=lambda: fetch_fields_fn(bbox=resolved_bbox),
        )
    except Exception as exc:  # noqa: BLE001
        # Surface FTW no-coverage honestly (never fabricate fields).
        if getattr(exc, "error_code", "") == "FIELDS_NO_COVERAGE":
            raise ContaminationAffectedFieldsNoCoverageError(
                f"no published farm-field boundaries cover {location_name} "
                f"(bbox {resolved_bbox}). Fields of The World coverage is "
                "regional (US / Japan / Denmark); pick a US-cropland AOI "
                f"(e.g. Ames, Iowa). Underlying: {exc}"
            ) from exc
        raise ContaminationAffectedFieldsError(
            f"fetch_field_boundaries failed: {exc}"
        ) from exc
    fields_uri = getattr(fields_layer, "uri", None)
    if not fields_uri:
        raise ContaminationAffectedFieldsError(
            "fetch_field_boundaries returned no layer URI."
        )

    # --- Stage 6: analyze the affected fields ----------------------------- #
    analyze_fn = _registry_fn("analyze_affected_fields")
    affected = await _maybe_emit(
        pipeline_emitter,
        name="Analyze affected farm fields",
        tool_name="analyze_affected_fields",
        invoke=lambda: analyze_fn(
            plume_layer_uri=plume.uri,
            fields_layer_uri=fields_uri,
            threshold_mgl=threshold_mgl,
            rank_by=rank_by,
        ),
    )

    summary = _build_summary(location_name, contaminant, plume, affected)
    logger.info(
        "affected-fields complete aoi=%r n_affected=%s/%s plume_area_km2=%.6g",
        location_name,
        affected.get("n_fields_affected"),
        affected.get("n_fields_total"),
        plume.plume_area_km2,
    )

    return AffectedFieldsResult(
        plume_layer=plume,
        fields_layer=fields_layer,
        affected=affected,
        summary=summary,
        spill_location_latlon=[spill_pt[0], spill_pt[1]],
        confirmation_envelope=envelope.model_dump(mode="json"),
    )


# --------------------------------------------------------------------------- #
# LLM-exposed thin atomic-tool wrapper (workflow_dispatch source class).
# --------------------------------------------------------------------------- #


_RUN_AFFECTED_FIELDS_METADATA = AtomicToolMetadata(
    name="run_model_contamination_affected_fields",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    supports_global_query=False,
)


@register_tool(
    _RUN_AFFECTED_FIELDS_METADATA,
    # readOnlyHint=False (submits a solver run), openWorldHint=False (intra-AWS /
    # local mf6 + public FTW fetch declared by the sub-tool), destructiveHint=False
    # (additive writes only), idempotentHint=False (each call mints a new run).
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def run_model_contamination_affected_fields(
    location_query: str | None = None,
    bbox: tuple[float, float, float, float] | list[float] | None = None,
    contaminant: str = "trichloroethylene",
    release_rate_kg_s: float = 0.05,
    duration_days: float = 1.0,
    article_text: str | None = None,
    spill_location_latlon: tuple[float, float] | list[float] | None = None,
    upgradient_offset_km: float = DEFAULT_UPGRADIENT_OFFSET_KM,
    threshold_mgl: float | None = None,
    rank_by: str = "peak",
    aquifer_k_ms: float | None = None,
    porosity: float | None = None,
    compute_class: str = "standard",
    # job-0241: server-managed confirmation flag — the solver-confirm gate strips
    # any LLM-supplied value and injects True only after the user approves.
    confirmed: bool = False,
    # job-0164: absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Which farm fields a modeled contaminant plume reaches — end to end.

    Models a groundwater contaminant spill UP-GRADIENT of a farmland area, runs
    the MODFLOW plume, fetches the agricultural field boundaries (Fields of The
    World / fiboa), and reports WHICH farm fields the plume reaches and HOW
    BADLY — each affected field's peak + mean concentration and affected area,
    ranked, plus a headline. The single-call demo behind "model a spill near
    <farmland> and tell me which fields it reaches".

    Use this when:
        - The user wants to model a contaminant / solvent spill near farmland
          and find out which agricultural fields it affects (ranked, with crops).
        - A US-cropland AOI is named (Ames, Iowa / Nebraska / Central Valley) —
          the Fields of The World coverage region.

    Do NOT use this for:
        - Surface-water flooding over fields (use the flood composers / SFINCS).
        - A plume with no farm-field question (use
          ``run_model_groundwater_contamination_scenario`` — that stops at the
          plume) or a generic raster-over-polygon summary
          (``compute_zonal_statistics``).
        - Areas outside FTW coverage — the tool returns an honest
          ``CONTAMINATION_AFFECTED_FIELDS_NO_COVERAGE`` error rather than guessing.

    Params:
        location_query: the farmland AOI as a place name (geocoded). Supply this
            OR ``bbox``. Prefer a US-cropland area (e.g. "Ames, Iowa").
        bbox: ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326. Supply this
            OR ``location_query``.
        contaminant: contaminant name (default "trichloroethylene"). Ignored when
            ``article_text`` is supplied (extracted from the article instead).
        release_rate_kg_s: mass release rate, kg/s (default 0.05).
        duration_days: release duration, days (default 1.0).
        article_text: optional spill news article — when supplied, the
            contaminant + release schedule + location are extracted from it.
        spill_location_latlon: explicit spill point ``(lat, lon)`` — overrides
            the automatic up-gradient (west-of-the-fields) placement.
        upgradient_offset_km: how far west of the field centroid to place the
            auto spill (default 3 km; the demo deck's gradient runs west->east).
        threshold_mgl: concentration above which a field counts as affected;
            defaults to the plume detection floor.
        rank_by: "peak" (default) or "area".
        aquifer_k_ms / porosity: optional demo-aquifer overrides (narrated as
            demo defaults).
        compute_class: FR-CE-3 compute class. Default "standard".

    Returns:
        A JSON dict (``AffectedFieldsResult.model_dump(mode="json")``) with the
        ``plume_layer`` (``PlumeLayerURI`` — ``max_concentration_mgl`` +
        ``plume_area_km2``), the ``fields_layer`` (FTW boundaries), the
        ``affected`` readout (ranked ``affected_fields`` + counts + ``headline``),
        the ``summary`` narration dict, the placed ``spill_location_latlon``, and
        the ``confirmation_envelope`` that gated the run. The agent narrates the
        typed numbers (n affected, affected area, worst-field peak mg/L), never
        invents them (Invariant 1). On a recoverable failure the tool raises a
        typed error the agent narrates honestly.

    Confirmation-before-consequence (Invariant 9): the MODFLOW run is gated
    behind the server's solver-confirm gate (``server.SOLVER_CONFIRM_TOOLS``);
    without that injected approval the wrapper fails closed.

    Cross-tool dependencies (step chain): ``geocode_location`` (AOI),
    ``run_modflow_job`` (plume), ``fetch_field_boundaries`` (FTW fields),
    ``analyze_affected_fields`` (the which-field intersection + ranking).
    """
    result = await model_contamination_affected_fields(
        location_query=location_query,
        bbox=bbox,
        contaminant=contaminant,
        release_rate_kg_s=release_rate_kg_s,
        duration_days=duration_days,
        article_text=article_text,
        spill_location_latlon=spill_location_latlon,
        upgradient_offset_km=upgradient_offset_km,
        threshold_mgl=threshold_mgl,
        rank_by=rank_by,
        confirmed=confirmed,
        aquifer_k_ms=aquifer_k_ms,
        porosity=porosity,
        compute_class=compute_class,
        pipeline_emitter=None,
    )
    return result.model_dump(mode="json")
