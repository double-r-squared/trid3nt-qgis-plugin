"""``model_mine_dewatering_scenario``  -  MODFLOW mine-pit-dewatering composer.

The end-to-end higher-order workflow for the sprint-18 Wave-1 MODFLOW
``mine_dewatering`` archetype: it turns a place (or AOI point) + a pit footprint
polygon into a rendered dewatering-rate layer  -  the per-cell drain outflow over
the pit and the total pump-to-dewater rate the pit needs to stay dry. It mirrors
the chain shape of ``model_sustainable_yield_scenario`` (sibling GWF-only
archetype): the pit footprint is draped as a DRN drain ring (steady GWF,
unconfined water table), and the DRN budget term IS the dewatering rate.

Canonical real-world pipeline mirrored here (an open-pit mine inflow / dewatering
estimate, the standard MODFLOW open-pit-dewatering analysis):

    resolve the AOI point (geocode a place, or take an explicit lat/lon)
        -> the user supplies the pit footprint polygon (NEVER fabricated  -  a
           missing pit is a typed USER_INPUT_REQUIRED failure)
        -> assemble MODFLOWRunArgs(archetype="mine_dewatering", pit, drain, ...)
        -> run_modflow_archetype_job (GWF steady DRN deck -> mf6 -> dewatering)
        -> DewaterLayerURI (dewatering_rate_m3_day + drain_cell_count)

Invariants (same set as model_sustainable_yield_scenario):
- **1 / 2 / 8: preserve** (typed numbers, deterministic composition, cancellable).
- **9. No fabricated model inputs.** A ``mine_dewatering`` run with no pit
  footprint returns a typed ``USER_INPUT_REQUIRED`` failed envelope rather than
  inventing a pit  -  the honesty floor: a "modeled" envelope with empty layers
  never reads ok.
- **10. Minimal parameter surface: preserves.** Intent (place + pit + target
  dewatered elevation) is exposed; the grid + demo aquifer K are derived.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import Field

from grace2_contracts.common import GraceModel
from grace2_contracts.modflow_contracts import (
    DEFAULT_AQUIFER_K_MS,
    DewaterLayerURI,
    MODFLOWRunArgs,
)
from grace2_contracts.tool_registry import AtomicToolMetadata

from ..pipeline_emitter import begin_substeps, current_emitter
from ..tools import register_tool
# Reuse the shared archetype-run + AOI-resolve + emit helpers from the
# sustainable_yield composer (one implementation, three composers).
from .model_sustainable_yield_scenario import (
    _aquifer_overrides,
    _coerce_optional_latlon,
    _resolve_aoi_point,
    _run_archetype,
)

logger = logging.getLogger(
    "grace2_agent.workflows.model_mine_dewatering_scenario"
)

__all__ = [
    "MineDewateringResult",
    "model_mine_dewatering_scenario",
    "run_model_mine_dewatering_scenario",
    "MineDewateringScenarioError",
    "MineDewateringInputError",
]


# --------------------------------------------------------------------------- #
# Result envelope
# --------------------------------------------------------------------------- #


class MineDewateringResult(GraceModel):
    """Return type for ``model_mine_dewatering_scenario`` (sprint-18 Wave-1).

    Bundles the dewatering layer + the derived args + a narration summary dict.
    Invariant 1: every narrated number is a typed field  -  ``dewater_layer``
    carries ``dewatering_rate_m3_day`` + ``drain_cell_count``.
    """

    schema_version: str = "v1"

    dewater_layer: DewaterLayerURI
    derived_params: dict[str, Any] = Field(default_factory=dict)
    summary: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Typed errors
# --------------------------------------------------------------------------- #


class MineDewateringScenarioError(RuntimeError):
    """Base class for ``model_mine_dewatering_scenario`` failures."""

    error_code: str = "MINE_DEWATERING_SCENARIO_ERROR"
    retryable: bool = False


class MineDewateringInputError(MineDewateringScenarioError):
    """Caller supplied invalid / missing AOI or pit input (honesty gate)."""

    error_code = "MINE_DEWATERING_INPUT_INVALID"


# --------------------------------------------------------------------------- #
# The composer
# --------------------------------------------------------------------------- #


def _normalize_pit_footprint(
    pit_footprint_lonlat: Any,
) -> list[tuple[float, float]]:
    """Coerce a pit footprint into an ordered ``[(lon, lat), ...]`` vertex list.

    Accepts a list of ``(lon, lat)`` / ``[lon, lat]`` pairs, or a GeoJSON-style
    ``{"type": "Polygon", "coordinates": [[[lon, lat], ...]]}`` /
    ``{"type": "Feature", "geometry": {...}}`` dict. Raises a typed input error
    when no usable ring is found.
    """
    coords: Any = pit_footprint_lonlat
    if isinstance(coords, dict):
        geom = coords.get("geometry", coords)
        gtype = str(geom.get("type", "")).lower()
        raw = geom.get("coordinates")
        if gtype == "polygon" and raw:
            ring = raw[0]
        elif gtype == "multipolygon" and raw:
            ring = raw[0][0]
        elif gtype in {"linestring", "multipoint"} and raw:
            ring = raw
        else:
            raise MineDewateringInputError(
                f"unsupported pit footprint geometry type {gtype!r}"
            )
        coords = ring
    try:
        verts = [(float(pt[0]), float(pt[1])) for pt in coords]
    except Exception as exc:  # noqa: BLE001
        raise MineDewateringInputError(
            f"invalid pit_footprint_lonlat (expected [(lon, lat), ...]): {exc}"
        ) from exc
    if len(verts) < 1:
        raise MineDewateringInputError(
            "pit_footprint_lonlat is empty; supply the pit outline vertices."
        )
    return verts


async def model_mine_dewatering_scenario(
    location: str | None = None,
    aoi_latlon: tuple[float, float] | None = None,
    *,
    pit_footprint_lonlat: Any | None = None,
    drain_elevation_m: float | None = None,
    drain_conductance_m2_day: float | None = None,
    well_pumping_rate_m3_day: float | None = None,
    aquifer_k_ms: float | None = None,
    porosity: float | None = None,
    compute_class: str = "standard",
    pipeline_emitter: Any | None = None,
) -> MineDewateringResult:
    """Compose place/AOI + a pit footprint -> MODFLOW dewatering -> DewaterLayerURI.

    Args:
        location: a place name (geocoded). Supply this OR ``aoi_latlon``.
        aoi_latlon: an explicit ``(lat, lon)`` AOI point.
        pit_footprint_lonlat: the pit outline as ``[(lon, lat), ...]`` (or a
            GeoJSON polygon). REQUIRED  -  a missing pit is a typed
            USER_INPUT_REQUIRED failure (never invented).
        drain_elevation_m: the target dewatered head (m, deck datum). Demo
            default applied by the adapter when None.
        drain_conductance_m2_day: per-cell DRN conductance (m^2/day). Demo
            default applied when None.
        well_pumping_rate_m3_day: optional supplemental sump WEL (m^3/day).
        aquifer_k_ms / porosity: optional demo-aquifer overrides.
        compute_class: FR-CE-3 compute class.
        pipeline_emitter: optional PipelineEmitter for live progress cards.

    Returns:
        ``MineDewateringResult`` with the ``DewaterLayerURI`` + derived args + a
        narration summary dict.

    Raises:
        MineDewateringInputError: missing/invalid AOI or pit (the honesty gate).
        MineDewateringScenarioError: a required step (geocode / solver) failed.
        Propagates ``asyncio.CancelledError`` (Invariant 8).
    """
    # --- Honesty gate (Invariant 9): never fabricate the pit -------------------
    if pit_footprint_lonlat is None:
        raise MineDewateringInputError(
            "mine_dewatering requires a pit footprint (pit_footprint_lonlat). "
            "The pit outline is a user input and is never invented; ask the user "
            "to draw / supply the pit polygon."
        )
    pit_verts = _normalize_pit_footprint(pit_footprint_lonlat)

    _planned = 1
    has_loc = bool(location and location.strip())
    if has_loc:
        _planned += 1
    begin_substeps(current_emitter(), _planned)

    lat, lon, location_name = await _resolve_aoi_point(
        location, aoi_latlon, pipeline_emitter=pipeline_emitter
    )

    try:
        run_args = MODFLOWRunArgs(
            spill_location_latlon=(lat, lon),
            contaminant="n/a",
            release_rate_kg_s=1.0,
            duration_days=1.0,
            archetype="mine_dewatering",
            pit_footprint_lonlat=pit_verts,
            drain_elevation_m=drain_elevation_m,
            drain_conductance_m2_day=drain_conductance_m2_day,
            well_pumping_rate_m3_day=well_pumping_rate_m3_day,
            **_aquifer_overrides(aquifer_k_ms, porosity, None, None),
        )
    except Exception as exc:  # noqa: BLE001
        raise MineDewateringInputError(
            f"invalid mine_dewatering run arguments: {exc}"
        ) from exc

    layer = await _run_archetype(
        run_args,
        compute_class=compute_class,
        pipeline_emitter=pipeline_emitter,
        tool_label=f"Model mine dewatering [{len(pit_verts)} pit vertices]",
        expected_type=DewaterLayerURI,
        error_code="MINE_DEWATERING_RUN_FAILED",
        scenario_error=MineDewateringScenarioError,
    )

    derived = {
        "location_name": location_name,
        "aoi_latlon": [lat, lon],
        "pit_vertex_count": len(pit_verts),
        "drain_elevation_m": drain_elevation_m,
        "drain_conductance_m2_day": drain_conductance_m2_day,
        "well_pumping_rate_m3_day": well_pumping_rate_m3_day,
    }
    summary = {
        "location_name": location_name,
        "dewatering_rate_m3_day": layer.dewatering_rate_m3_day,
        "drain_cell_count": layer.drain_cell_count,
        "demo_aquifer_caveat": (
            f"Aquifer K={DEFAULT_AQUIFER_K_MS:g} m/s and the drain conductance / "
            "elevation are demo defaults, not site-specific hydrogeology."
        ),
    }
    logger.info(
        "mine_dewatering scenario complete location=%r dewatering_rate_m3_day=%.6g "
        "drain_cells=%d",
        location_name,
        layer.dewatering_rate_m3_day,
        layer.drain_cell_count,
    )
    return MineDewateringResult(
        dewater_layer=layer, derived_params=derived, summary=summary
    )


# --------------------------------------------------------------------------- #
# LLM-exposed thin atomic-tool wrapper (workflow_dispatch source class)
# --------------------------------------------------------------------------- #


_METADATA = AtomicToolMetadata(
    name="run_model_mine_dewatering_scenario",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
)


@register_tool(
    _METADATA,
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def run_model_mine_dewatering_scenario(
    location: str | None = None,
    aoi_latlon: tuple[float, float] | list[float] | None = None,
    pit_footprint_lonlat: Any | None = None,
    drain_elevation_m: float | None = None,
    drain_conductance_m2_day: float | None = None,
    well_pumping_rate_m3_day: float | None = None,
    aquifer_k_ms: float | None = None,
    porosity: float | None = None,
    compute_class: str = "standard",
    # job-0164: absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Model an open-pit mine's dewatering rate (groundwater inflow to the pit).

    Builds a steady MODFLOW 6 groundwater-flow model with an unconfined water
    table and a DRN drain over the user-supplied pit footprint, runs it, and
    produces a DEWATERING-RATE layer: the per-cell drain outflow over the pit and
    the TOTAL pump-to-dewater rate the pit needs to stay dry. Use this to estimate
    open-pit groundwater inflow / required dewatering capacity.

    Use this when:
        - The user asks how much water a mine pit must pump to stay dewatered, the
          groundwater inflow to an open pit, or required dewatering capacity.

    Do NOT use this for:
        - A pumping-well drawdown cone (use ``run_model_sustainable_yield_scenario``).
        - A contaminant spill plume (use ``run_modflow_job``).
        - Surface-water flooding (use ``run_model_flood_scenario``  -  SFINCS).

    Params:
        location: place name (geocoded). Supply this OR ``aoi_latlon``.
        aoi_latlon: explicit ``(lat, lon)`` AOI point.
        pit_footprint_lonlat: the pit outline as ``[(lon, lat), ...]`` or a
            GeoJSON polygon. REQUIRED  -  never invented; ask the user if absent.
        drain_elevation_m: the target dewatered head (m). Demo default if None.
        drain_conductance_m2_day: per-cell DRN conductance. Demo default if None.
        well_pumping_rate_m3_day: optional supplemental sump WEL (m^3/day).
        aquifer_k_ms / porosity: optional demo-aquifer overrides.
        compute_class: FR-CE-3 compute class. Default ``"standard"``.

    Returns:
        On success: a ``MineDewateringResult`` JSON dict with the
        ``dewater_layer`` (a ``DewaterLayerURI`` carrying ``dewatering_rate_m3_day``
        + ``drain_cell_count``  -  the agent narrates these typed numbers), the
        ``derived_params``, and the ``summary``. On a recoverable failure (incl.
        a missing pit) the tool returns a typed error the agent narrates honestly
         -  it never fabricates a pit.

    FR-DC-6: ``cacheable=False`` + ``ttl_class="live-no-cache"`` +
    ``source_class="workflow_dispatch"``  -  the cache shim is NOT invoked.
    """
    aoi = _coerce_optional_latlon(aoi_latlon)
    try:
        result = await model_mine_dewatering_scenario(
            location=location,
            aoi_latlon=aoi,
            pit_footprint_lonlat=pit_footprint_lonlat,
            drain_elevation_m=(
                float(drain_elevation_m) if drain_elevation_m is not None else None
            ),
            drain_conductance_m2_day=(
                float(drain_conductance_m2_day)
                if drain_conductance_m2_day is not None
                else None
            ),
            well_pumping_rate_m3_day=(
                float(well_pumping_rate_m3_day)
                if well_pumping_rate_m3_day is not None
                else None
            ),
            aquifer_k_ms=aquifer_k_ms,
            porosity=porosity,
            compute_class=compute_class,
            pipeline_emitter=None,
        )
    except MineDewateringInputError as exc:
        return {
            "status": "error",
            "error_code": "USER_INPUT_REQUIRED",
            "error_message": str(exc),
        }
    except MineDewateringScenarioError as exc:
        return {
            "status": "error",
            "error_code": getattr(exc, "error_code", "MINE_DEWATERING_SCENARIO_ERROR"),
            "error_message": str(exc),
        }
    return result.model_dump(mode="json")
