"""``modflow_wellhead_protection`` - MODFLOW EPA wellhead-protection-area template.

A separate template from ``modflow_capture_zone`` because capture_zone and
wellhead_protection answer DIFFERENT questions - general zone-of-contribution
vs EPA fixed-travel-time WHPA tiers - even though they share the
the composer composer + ``CaptureZoneLayerURI`` carrier.

This template reuses the ``modflow_capture_zone`` composer with
``archetype="wellhead_protection"`` (EPA WHPA framing + default [2, 5, 10] yr
travel-time tiers per SDWA Section 1428 / EPA 440/6-87-010).

Tagged ``engine="modflow"``, ``tier="template"``: EXCLUDED from the default
retrieval pool, surfaced only by the ``run_modflow`` door's gate expansion.
"""

from __future__ import annotations

from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.data import register_tool
from trid3nt_server.workflows.modflow._template_card import TemplateCard
from trid3nt_server.workflows.modflow.capture_zone.capture_zone import (
    CaptureZoneInputError,
    CaptureZoneScenarioError,
    _coerce_optional_latlon,
    model_capture_zone_scenario,
)

__all__ = ["modflow_wellhead_protection", "TEMPLATE_CARD"]


TEMPLATE_CARD = TemplateCard(
    question=(
        "an EPA-style wellhead protection area (WHPA) for a drinking-water well "
        "(fixed-travel-time tiers)"
    ),
    required_inputs=["location (or aoi_latlon)", "well_location_latlon"],
    knobs="travel_time_years (default [2, 5, 10]), n_particles, aquifer_k_ms, porosity",
)


_WHPA_METADATA = AtomicToolMetadata(
    name="modflow_wellhead_protection",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="modflow",
    tier="template",
)


@register_tool(
    _WHPA_METADATA,
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def modflow_wellhead_protection(
    location: str | None = None,
    aoi_latlon: tuple[float, float] | list[float] | None = None,
    well_location_latlon: tuple[float, float] | list[float] | None = None,
    travel_time_years: list[float] | None = None,
    n_particles: int = 16,
    aquifer_k_ms: float | None = None,
    porosity: float | None = None,
    # multi-well WELLFIELD + transient + NHD RIV boundaries.
    wells: list[Any] | None = None,
    transient: bool = False,
    sim_years: float | None = None,
    n_periods: int | None = None,
    use_nhd_river_boundaries: bool = False,
    compute_class: str = "standard",
    # absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Delineate an EPA-style wellhead protection area (WHPA) for a pumping well.

    Fidelity: MODFLOW 6 local planning-grade groundwater envelope (aquifer
    K/porosity are SoilGrids-derived at the AOI or refused when unavailable, law 9), not a
    calibrated regulatory delineation. Off-scope: surface-water inundation
    flooding -> sfincs_flood; urban storm-sewer / pipe-network flooding ->
    swmm_urban_flood.

    Identical machinery to ``modflow_capture_zone`` but uses EPA WHPA
    fixed-travel-time framing and default tiers of [2, 5, 10] years (the EPA
    wellhead protection program under SDWA Section 1428; fixed-travel-time
    delineation per EPA 440/6-87-010 -- the 2-year IMMEDIATE zone, the 5-year
    INTERMEDIATE zone, and the 10-year LONG-TERM zone). Produces a
    ``CaptureZoneLayerURI`` (violet protection-zone vector polygon).

    Use this when:
        - The user explicitly asks for a wellhead protection area, WHPA, source
          water protection zone, or EPA fixed-travel-time protection zone.
        - The user mentions regulatory compliance under the Safe Drinking Water
          Act (SDWA) Wellhead Protection Program.

    Do NOT use this for:
        - A general zone-of-contribution / capture zone without WHPA framing
          (use ``modflow_capture_zone``).
        - A drawdown cone (use ``modflow_sustainable_yield``).

    PRECISION CAVEAT: the polygon is a planning envelope computed from
    SoilGrids-derived (or caller-supplied) aquifer parameters, refusing when no
    real source can serve them (law 9), NOT a regulatory WHPA delineation. Always
    narrate this.

    Params:
        location: place name (geocoded). Supply this OR ``aoi_latlon``.
        aoi_latlon: explicit ``(lat, lon)`` AOI point.
        well_location_latlon: the pumping-well ``(lat, lon)``. REQUIRED -- never
            invented; ask the user if absent (Invariant 9).
        travel_time_years: list of isochrone cutoffs in years. Default [2, 5, 10].
        n_particles: particles released around the well screen (default 16).
        aquifer_k_ms / porosity: optional overrides; else SoilGrids-derived at the AOI or refused (law 9).
        compute_class: compute class. Default ``'standard'``. PRT
            archetypes run LOCAL-ONLY (fast; Batch is not used).

    Returns:
        On success: a ``CaptureZoneResult`` JSON dict with the
        ``capture_zone_layer`` (a ``CaptureZoneLayerURI`` carrying
        ``capture_zone_area_km2`` + ``travel_time_years`` + per-tier
        ``isochrone_areas_km2`` + ``particle_count``). On a recoverable failure
        (incl. a missing well) the tool returns a typed error the agent narrates
        honestly -- it never fabricates a well.

    ``cacheable=False`` + ``ttl_class="live-no-cache"`` +
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
            wells=wells,
            transient=bool(transient),
            sim_years=sim_years,
            n_periods=n_periods,
            use_nhd_river_boundaries=bool(use_nhd_river_boundaries),
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
