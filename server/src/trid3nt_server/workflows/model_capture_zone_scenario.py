"""``model_capture_zone_scenario``  -  MODFLOW Wave-4 PRT capture-zone composer.

The end-to-end higher-order workflow for the sprint-18 Wave-4 MODFLOW
``capture_zone`` and ``wellhead_protection`` archetypes: it turns a place (or
AOI point) + a pumping well location into a rendered capture-zone polygon  -
the zone of contribution delineated by backward particle tracking (MF6 PRT).

Canonical real-world pipeline mirrored here (a wellhead protection area /
zone-of-contribution delineation, the MODFLOW analogue of the EPA WHPA /
ZONEBUDGET approach):

    resolve the AOI point (geocode a place, or take an explicit lat/lon)
        -> the user supplies the well location (NEVER fabricated  -  a missing
           well is a typed USER_INPUT_REQUIRED failure, Invariant 9)
        -> assemble MODFLOWRunArgs(archetype='capture_zone', well, tiers, ...)
        -> run_modflow_archetype_job:
             GWF steady flow solve -> mf6
             -> gwt_adapter.build_and_run_prt_from_gwf (PRT backward tracking)
             -> postprocess_capture_zone (convex-hull isochrones + FlatGeobuf)
        -> CaptureZoneLayerURI (vector polygon + per-tier isochrone areas)

The difference between the two archetypes is framing and default travel-time
tiers only:

    ``capture_zone``       - general zone-of-contribution; defaults [1, 5, 10] yr
    ``wellhead_protection`` - EPA-style fixed-travel-time; defaults [2, 5, 10] yr
                             (EPA WHPA fixed-travel-time approach; SDWA Section
                             1428 / EPA 440/6-87-010 delineation guidance)

Both produce a ``CaptureZoneLayerURI`` (layer_type='vector'), which renders
client-side via the inline-GeoJSON path and the ``presetColorFor('capture_zone')``
violet branch in ``vector_rendering.ts``.

Invariants:
- **1 / 2 / 8: preserve** (typed numbers, deterministic composition, cancellable).
- **9. No fabricated model inputs.** A capture-zone run with no well location
  returns a typed ``USER_INPUT_REQUIRED`` failure -- the CONVEX HULL of
  backtracked pathlines is a physical delineation, not a guess; a missing well
  is never invented.
- **10. Minimal parameter surface: preserves.** The signature exposes intent (the
  place + the well + optional tiers / particle count); the grid, demo aquifer
  K / Sy, and PRT parameters are derived defaults, not user-supplied.

PRECISION CAVEAT (Invariant 1): the polygon is the CONVEX HULL of discrete
backtracked pathlines on a structured 100 m rectilinear grid with DEMO aquifer
parameters, NOT a calibrated regulatory wellhead protection area. The agent must
narrate this caveat when presenting the layer (FR-AS-7).
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import Field

from trid3nt_contracts.common import GraceModel
from trid3nt_contracts.modflow_contracts import (
    CaptureZoneLayerURI,
    DEFAULT_AQUIFER_K_MS,
    DEFAULT_POROSITY,
    MODFLOWRunArgs,
)
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from ..pipeline_emitter import begin_substeps, current_emitter
from ..tools import register_tool
# Reuse the shared archetype-run + AOI-resolve helpers from the sustainable_yield
# composer (one implementation, all archetypes).
from .model_sustainable_yield_scenario import (
    _aquifer_overrides,
    _coerce_optional_latlon,
    _resolve_aoi_point,
    _run_archetype,
)

logger = logging.getLogger("trid3nt_server.workflows.model_capture_zone_scenario")

__all__ = [
    "CaptureZoneResult",
    "model_capture_zone_scenario",
    "run_model_capture_zone_scenario",
    "run_model_wellhead_protection_scenario",
    "CaptureZoneScenarioError",
    "CaptureZoneInputError",
]

#: Default travel-time isochrone tiers (years) for ``capture_zone``.
#: One, five, and ten years is the common municipal-well zone-of-contribution
#: analysis period (e.g. USEPA Source Water Protection guidance).
CAPTURE_ZONE_DEFAULT_TIERS: list[float] = [1.0, 5.0, 10.0]

#: Default travel-time isochrone tiers (years) for ``wellhead_protection``.
#: Two, five, and ten years align with the EPA WHPA fixed-travel-time approach
#: (SDWA Section 1428 wellhead protection program; delineation methods per EPA
#: 440/6-87-010; the 2-year tier is the IMMEDIATE zone).
WELLHEAD_PROTECTION_DEFAULT_TIERS: list[float] = [2.0, 5.0, 10.0]


# --------------------------------------------------------------------------- #
# Result envelope
# --------------------------------------------------------------------------- #


class CaptureZoneResult(GraceModel):
    """Return type for ``model_capture_zone_scenario`` (sprint-18 Wave-4).

    Bundles the capture-zone vector layer + the derived args + a narration
    summary dict. Invariant 1: every narrated number is a typed field  -
    ``capture_zone_layer`` carries ``capture_zone_area_km2``,
    ``travel_time_years``, ``isochrone_areas_km2``, and ``particle_count``.
    """

    schema_version: str = "v1"

    capture_zone_layer: CaptureZoneLayerURI
    derived_params: dict[str, Any] = Field(default_factory=dict)
    summary: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Typed errors
# --------------------------------------------------------------------------- #


class CaptureZoneScenarioError(RuntimeError):
    """Base class for ``model_capture_zone_scenario`` failures."""

    error_code: str = "CAPTURE_ZONE_SCENARIO_ERROR"
    retryable: bool = False


class CaptureZoneInputError(CaptureZoneScenarioError):
    """Caller supplied invalid / missing well or AOI input (honesty gate).

    Invariant 9: the well location is NEVER fabricated. A ``capture_zone`` run
    with no well location raises this error so the agent asks the user for the
    real well coordinates.
    """

    error_code = "CAPTURE_ZONE_INPUT_INVALID"


# --------------------------------------------------------------------------- #
# The composer
# --------------------------------------------------------------------------- #


async def model_capture_zone_scenario(
    location: str | None = None,
    aoi_latlon: tuple[float, float] | None = None,
    *,
    well_location_latlon: tuple[float, float] | None = None,
    travel_time_years: list[float] | None = None,
    n_particles: int = 16,
    archetype: str = "capture_zone",
    aquifer_k_ms: float | None = None,
    porosity: float | None = None,
    compute_class: str = "standard",
    pipeline_emitter: Any | None = None,
) -> CaptureZoneResult:
    """Compose place/AOI + a pumping well -> MODFLOW PRT -> CaptureZoneLayerURI.

    Args:
        location: a place name (geocoded). Supply this OR ``aoi_latlon``.
        aoi_latlon: an explicit ``(lat, lon)`` AOI point.
        well_location_latlon: the pumping-well ``(lat, lon)``. REQUIRED  -  a
            missing well is a typed USER_INPUT_REQUIRED failure (never invented).
            Invariant 9: the CONVEX HULL of backtracked pathlines is a physical
            delineation computed by MF6 PRT; no coordinate is fabricated.
        travel_time_years: list of travel-time isochrone cutoffs, years. Each
            value defines one nested isochrone tier of the capture zone (particles
            that reach the well within this time bound define that zone). When None
            the archetype-specific default is used:
                ``capture_zone``        -> [1.0, 5.0, 10.0]
                ``wellhead_protection`` -> [2.0, 5.0, 10.0] (EPA WHPA tiers)
        n_particles: number of particles released around the pumping-well screen
            per PRT solve (default 16; range 4..256). More particles improve
            capture-zone shape fidelity at the cost of slightly longer runtime.
        archetype: ``'capture_zone'`` (zone-of-contribution) or
            ``'wellhead_protection'`` (EPA fixed-travel-time framing). The
            difference is framing and default tiers only; both produce the same
            carrier.
        aquifer_k_ms / porosity: optional demo-aquifer overrides (narrated as
            demo defaults, not site-specific hydrogeology).
        compute_class: FR-CE-3 compute class. NOTE: PRT archetypes are
            LOCAL-ONLY (fast; the Batch path is never used).
        pipeline_emitter: optional PipelineEmitter for live progress cards.

    Returns:
        ``CaptureZoneResult`` with the ``CaptureZoneLayerURI`` (a vector polygon
        carrying per-tier isochrone areas) + derived args + a narration summary.

    Raises:
        CaptureZoneInputError: missing/invalid AOI or well (Invariant 9 gate).
        CaptureZoneScenarioError: a required step (geocode / solver) failed.
        Propagates ``asyncio.CancelledError`` (Invariant 8).
    """
    if archetype not in ("capture_zone", "wellhead_protection"):
        raise CaptureZoneInputError(
            f"model_capture_zone_scenario: archetype must be 'capture_zone' or "
            f"'wellhead_protection'; got {archetype!r}."
        )

    # --- Honesty gate (Invariant 9): never fabricate the well -----------------
    if well_location_latlon is None:
        raise CaptureZoneInputError(
            f"{archetype} requires a pumping-well location (well_location_latlon). "
            "The well coordinates are a user input and are NEVER invented; ask the "
            "user to supply the pumping-well lat/lon. The capture-zone polygon is "
            "computed by MF6 backward particle tracking from the real well cell."
        )

    # Apply archetype-specific default tiers when the caller did not supply them.
    if travel_time_years is None:
        if archetype == "wellhead_protection":
            tiers = list(WELLHEAD_PROTECTION_DEFAULT_TIERS)
        else:
            tiers = list(CAPTURE_ZONE_DEFAULT_TIERS)
    else:
        tiers = [float(t) for t in travel_time_years if t > 0]
        if not tiers:
            raise CaptureZoneInputError(
                "travel_time_years must contain at least one positive value; "
                f"got {travel_time_years!r}."
            )

    # task-168: declare the planned internal-tool count up front: geocode (only
    # when a place string was supplied) + run_modflow_archetype_job (always).
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
        raise CaptureZoneInputError(
            f"invalid well_location_latlon (expected (lat, lon)): {exc}"
        ) from exc

    try:
        run_args = MODFLOWRunArgs(
            spill_location_latlon=(lat, lon),
            contaminant="n/a",       # GWF-only archetype: no solute (placeholder)
            release_rate_kg_s=1.0,   # ignored when archetype is set
            duration_days=1.0,       # ignored when archetype is set
            archetype=archetype,
            well_location_latlon=(wlat, wlon),
            capture_zone_travel_time_years=tiers,
            n_particles=int(n_particles),
            **_aquifer_overrides(aquifer_k_ms, porosity, None, None),
        )
    except Exception as exc:  # noqa: BLE001  -  pydantic ValidationError
        raise CaptureZoneInputError(
            f"invalid {archetype} run arguments: {exc}"
        ) from exc

    label = (
        f"Model {'wellhead protection area' if archetype == 'wellhead_protection' else 'capture zone'} "
        f"[{len(tiers)} tier(s), {n_particles} particles]"
    )
    layer = await _run_archetype(
        run_args,
        compute_class=compute_class,
        pipeline_emitter=pipeline_emitter,
        tool_label=label,
        expected_type=CaptureZoneLayerURI,
        error_code=f"{archetype.upper()}_RUN_FAILED",
        scenario_error=CaptureZoneScenarioError,
    )

    derived = {
        "location_name": location_name,
        "aoi_latlon": [lat, lon],
        "well_location_latlon": [wlat, wlon],
        "archetype": archetype,
        "travel_time_years": tiers,
        "n_particles": n_particles,
    }
    iso_areas = getattr(layer, "isochrone_areas_km2", {})
    summary = {
        "location_name": location_name,
        "archetype": archetype,
        "well_location_latlon": [wlat, wlon],
        "capture_zone_area_km2": layer.capture_zone_area_km2,
        "travel_time_years": layer.travel_time_years,
        "isochrone_areas_km2": iso_areas,
        "particle_count": layer.particle_count,
        "demo_aquifer_caveat": (
            f"Aquifer K={DEFAULT_AQUIFER_K_MS:g} m/s and porosity={DEFAULT_POROSITY:g} "
            "are demo defaults, not site-specific hydrogeology. The polygon is the "
            "CONVEX HULL of discrete backtracked pathlines on a structured 100 m "
            "rectilinear grid -- treat it as a planning-level envelope, not a "
            "legally defensible wellhead protection area."
        ),
    }
    logger.info(
        "%s scenario complete location=%r capture_zone_area_km2=%.6g tiers=%s",
        archetype,
        location_name,
        layer.capture_zone_area_km2,
        layer.travel_time_years,
    )
    return CaptureZoneResult(
        capture_zone_layer=layer, derived_params=derived, summary=summary
    )


# --------------------------------------------------------------------------- #
# LLM-exposed thin atomic-tool wrappers (workflow_dispatch source class)
# --------------------------------------------------------------------------- #


_CAPTURE_ZONE_METADATA = AtomicToolMetadata(
    name="run_model_capture_zone_scenario",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
)


@register_tool(
    _CAPTURE_ZONE_METADATA,
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def run_model_capture_zone_scenario(
    location: str | None = None,
    aoi_latlon: tuple[float, float] | list[float] | None = None,
    well_location_latlon: tuple[float, float] | list[float] | None = None,
    travel_time_years: list[float] | None = None,
    n_particles: int = 16,
    aquifer_k_ms: float | None = None,
    porosity: float | None = None,
    compute_class: str = "standard",
    # job-0164: absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Delineate the capture zone (zone of contribution) for a pumping well.

    Builds a MODFLOW 6 steady groundwater-flow model, then runs an MF6 PRT
    (Particle Tracking) backward-tracking solve that releases particles around
    the pumping-well screen and tracks them up-gradient to their capture origin.
    The convex hull of all backtracked pathlines at each requested travel-time
    threshold is the capture-zone isochrone for that tier. Produces a VECTOR
    polygon layer on the map (violet protection-zone colour).

    Use this when:
        - The user asks for the capture zone, zone of contribution, zone of
          influence, or zone of transport for a pumping well.
        - The user asks how far back in time the water in a well came from.

    Do NOT use this for:
        - A wellhead PROTECTION area with EPA WHPA framing (use
          ``run_model_wellhead_protection_scenario``).
        - A pumping-well DRAWDOWN cone (use ``run_model_sustainable_yield_scenario``).
        - A contaminant spill plume (use ``run_modflow_job``).

    PRECISION CAVEAT: the polygon is the CONVEX HULL of discrete backtracked
    pathlines on a structured 100 m rectilinear grid with DEMO aquifer parameters
    (K=1e-4 m/s, porosity=0.3), NOT a calibrated regulatory wellhead protection
    area. Always narrate this caveat.

    Params:
        location: place name (geocoded). Supply this OR ``aoi_latlon``.
        aoi_latlon: explicit ``(lat, lon)`` AOI point.
        well_location_latlon: the pumping-well ``(lat, lon)``. REQUIRED -- never
            invented; ask the user if absent (Invariant 9).
        travel_time_years: list of isochrone cutoffs in years. Default [1, 5, 10].
        n_particles: particles released around the well screen (default 16; range
            4..256). More = denser pathline fan = more representative shape.
        aquifer_k_ms / porosity: optional demo-aquifer overrides.
        compute_class: FR-CE-3 compute class. Default ``'standard'``. PRT
            archetypes run LOCAL-ONLY (fast; Batch is not used).

    Returns:
        On success: a ``CaptureZoneResult`` JSON dict with the
        ``capture_zone_layer`` (a ``CaptureZoneLayerURI`` carrying
        ``capture_zone_area_km2`` + ``travel_time_years`` + per-tier
        ``isochrone_areas_km2`` + ``particle_count``). On a recoverable failure
        (incl. a missing well) the tool returns a typed error the agent narrates
        honestly -- it never fabricates a well.

    FR-DC-6: ``cacheable=False`` + ``ttl_class="live-no-cache"`` +
    ``source_class="workflow_dispatch"``  -  the cache shim is NOT invoked.
    """
    aoi = _coerce_optional_latlon(aoi_latlon)
    well = _coerce_optional_latlon(well_location_latlon)
    try:
        result = await model_capture_zone_scenario(
            location=location,
            aoi_latlon=aoi,
            well_location_latlon=well,
            travel_time_years=(
                [float(t) for t in travel_time_years] if travel_time_years else None
            ),
            n_particles=int(n_particles),
            archetype="capture_zone",
            aquifer_k_ms=aquifer_k_ms,
            porosity=porosity,
            compute_class=compute_class,
            pipeline_emitter=None,
        )
    except CaptureZoneInputError as exc:
        return {
            "status": "error",
            "error_code": "USER_INPUT_REQUIRED",
            "error_message": str(exc),
        }
    except CaptureZoneScenarioError as exc:
        return {
            "status": "error",
            "error_code": getattr(exc, "error_code", "CAPTURE_ZONE_SCENARIO_ERROR"),
            "error_message": str(exc),
        }
    return result.model_dump(mode="json")


_WHPA_METADATA = AtomicToolMetadata(
    name="run_model_wellhead_protection_scenario",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
)


@register_tool(
    _WHPA_METADATA,
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def run_model_wellhead_protection_scenario(
    location: str | None = None,
    aoi_latlon: tuple[float, float] | list[float] | None = None,
    well_location_latlon: tuple[float, float] | list[float] | None = None,
    travel_time_years: list[float] | None = None,
    n_particles: int = 16,
    aquifer_k_ms: float | None = None,
    porosity: float | None = None,
    compute_class: str = "standard",
    # job-0164: absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Delineate an EPA-style wellhead protection area (WHPA) for a pumping well.

    Identical to ``run_model_capture_zone_scenario`` but uses EPA WHPA
    fixed-travel-time framing and default tiers of [2, 5, 10] years (the EPA
    wellhead protection program under SDWA Section 1428; fixed-travel-time
    delineation per EPA 440/6-87-010 -- the 2-year IMMEDIATE zone, the 5-year
    INTERMEDIATE zone, and the 10-year LONG-TERM zone). Both tools produce the
    same ``CaptureZoneLayerURI`` carrier; the framing and defaults differ.

    Use this when:
        - The user explicitly asks for a wellhead protection area, WHPA, source
          water protection zone, or EPA fixed-travel-time protection zone.
        - The user mentions regulatory compliance under the Safe Drinking Water
          Act (SDWA) Wellhead Protection Program.

    Do NOT use this for:
        - A general zone-of-contribution / capture zone without WHPA framing
          (use ``run_model_capture_zone_scenario``).
        - A drawdown cone (use ``run_model_sustainable_yield_scenario``).

    PRECISION CAVEAT: the polygon is a demo planning envelope computed from DEMO
    aquifer parameters, NOT a regulatory WHPA delineation. Always narrate this.

    Params, Returns: identical to ``run_model_capture_zone_scenario`` (default
    travel_time_years=[2, 5, 10]).

    FR-DC-6: ``cacheable=False`` + ``ttl_class="live-no-cache"`` +
    ``source_class="workflow_dispatch"``  -  the cache shim is NOT invoked.
    """
    aoi = _coerce_optional_latlon(aoi_latlon)
    well = _coerce_optional_latlon(well_location_latlon)
    try:
        result = await model_capture_zone_scenario(
            location=location,
            aoi_latlon=aoi,
            well_location_latlon=well,
            travel_time_years=(
                [float(t) for t in travel_time_years] if travel_time_years else None
            ),
            n_particles=int(n_particles),
            archetype="wellhead_protection",
            aquifer_k_ms=aquifer_k_ms,
            porosity=porosity,
            compute_class=compute_class,
            pipeline_emitter=None,
        )
    except CaptureZoneInputError as exc:
        return {
            "status": "error",
            "error_code": "USER_INPUT_REQUIRED",
            "error_message": str(exc),
        }
    except CaptureZoneScenarioError as exc:
        return {
            "status": "error",
            "error_code": getattr(exc, "error_code", "CAPTURE_ZONE_SCENARIO_ERROR"),
            "error_message": str(exc),
        }
    return result.model_dump(mode="json")
