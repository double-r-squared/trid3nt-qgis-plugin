"""``model_river_seepage_scenario`` — MODFLOW river-seepage composer (J9).

The end-to-end higher-order workflow for the sprint-17 MODFLOW river-seepage
North Star: it turns a place + a contaminant + a release into a rendered
gaining/losing river-seepage layer (where the river leaks into the aquifer vs
draws baseflow out of it) plus the contaminant plume that entered with the
seepage. It is the river-coupled analogue of
``model_groundwater_contamination_scenario`` (the point-spill Case 2 composer)
and mirrors its chain shape.

Canonical real-world pipeline mirrored here (MODFLOW 6 RIV + GWT, from the
USGS modflow6-examples ex-gwf-sfr-p01 / Prudic stream-aquifer tradition,
reduced to the simplest RIV head-dependent boundary for v0.1):

    geocode location -> bbox around the spill point
        -> fetch_river_geometry (NHDPlus HR / OSM Overpass) to get the reach
        -> fetch_dem (USGS 3DEP) to sample streambed elevation (optional;
           the engine falls back to demo streambed values when absent)
        -> derive RIV args (river_geometry_uri + along-river source toggle)
        -> run_river_seepage_job (deck build -> mf6 -> postprocess -> publish)
        -> SeepageLayerURI (gaining/losing) + PlumeLayerURI (the solute)

Invariants:
- **1. Determinism boundary: preserves.** Every narrated number comes from a
  typed field — the derived-args dict (plain arithmetic / lookups) and the
  ``SeepageLayerURI`` scalars. No LLM call anywhere in this module.
- **2. Deterministic workflows: preserves.** Straight-line Python composition
  over registered atomic tools + the river-seepage bridge tool; typed-exception
  handling at the boundary.
- **8. Cancellation is first-class: preserves.** Every ``await`` is a
  cancel-propagation site; ``asyncio.CancelledError`` bubbles untouched.
- **10. Minimal parameter surface: preserves.** The signature exposes intent
  (the place + contaminant + release); the bbox, river geometry, DEM and the
  demo-aquifer K / porosity are derived, not user-supplied.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from pydantic import Field

from trid3nt_contracts.common import GraceModel
from trid3nt_contracts.modflow_contracts import (
    DEFAULT_AQUIFER_K_MS,
    DEFAULT_POROSITY,
    PlumeLayerURI,
    SeepageLayerURI,
)
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from ..pipeline_emitter import begin_substeps, current_emitter, substep
from ..tools import TOOL_REGISTRY, register_tool

logger = logging.getLogger("trid3nt_server.workflows.model_river_seepage_scenario")

__all__ = [
    "RiverSeepageResult",
    "model_river_seepage_scenario",
    "run_model_river_seepage_scenario",
    "RiverSeepageScenarioError",
    "RiverSeepageScenarioInputError",
    "DEFAULT_AOI_HALF_DEG",
]

#: Half-width (degrees) of the bbox drawn around the spill point to fetch the
#: river flowline + DEM. ~0.012 deg ~= 1.3 km, comfortably inside the
#: gwt_adapter 2 km demo domain (DOMAIN_HALF_WIDTH_M=1000 m) so the fetched
#: reach lands on the model grid.
DEFAULT_AOI_HALF_DEG: float = 0.012


# --------------------------------------------------------------------------- #
# Result envelope (agent-local; mirrors Case2Result)
# --------------------------------------------------------------------------- #


class RiverSeepageResult(GraceModel):
    """Return type for ``model_river_seepage_scenario`` (sprint-17 J9).

    Bundles the river-seepage composer output: the published seepage layer (the
    gaining/losing river<->aquifer exchange) + the contaminant plume + the
    derived args + a narration summary dict.

    Invariant 1 (Determinism boundary): every narrated number is a typed field.
    ``seepage_layer`` carries the leakage scalars; ``plume_layer`` (when present)
    carries the plume scalars; ``summary`` mirrors them — all computed.
    """

    schema_version: str = "v1"

    seepage_layer: SeepageLayerURI
    plume_layer: PlumeLayerURI | None = None
    derived_params: dict[str, Any] = Field(default_factory=dict)
    summary: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Typed errors
# --------------------------------------------------------------------------- #


class RiverSeepageScenarioError(RuntimeError):
    """Base class for ``model_river_seepage_scenario`` failures."""

    error_code: str = "RIVER_SEEPAGE_SCENARIO_ERROR"
    retryable: bool = False


class RiverSeepageScenarioInputError(RiverSeepageScenarioError):
    """Caller supplied neither a location string nor a spill point."""

    error_code = "RIVER_SEEPAGE_SCENARIO_INPUT_INVALID"


# --------------------------------------------------------------------------- #
# Registry helper
# --------------------------------------------------------------------------- #


def _registry_fn(name: str) -> Any:
    """Resolve ``name`` to the registered tool callable (registry seam)."""
    entry = TOOL_REGISTRY.get(name)
    if entry is None:
        raise RiverSeepageScenarioError(
            f"required atomic tool {name!r} is not registered "
            f"(known tools: {sorted(TOOL_REGISTRY)[:8]}...)"
        )
    return entry.fn


def _bbox_around(lat: float, lon: float, half_deg: float) -> tuple[float, float, float, float]:
    """Return a (min_lon, min_lat, max_lon, max_lat) bbox around a point."""
    return (lon - half_deg, lat - half_deg, lon + half_deg, lat + half_deg)


def _layer_uri_field(result: Any, field: str) -> Any:
    """Pull a field off a LayerURI-or-dict tool result (fetcher tolerance)."""
    if result is None:
        return None
    if hasattr(result, field):
        return getattr(result, field)
    if isinstance(result, dict):
        return result.get(field)
    return None


# --------------------------------------------------------------------------- #
# The composer
# --------------------------------------------------------------------------- #


async def model_river_seepage_scenario(
    location: str | None = None,
    spill_location_latlon: tuple[float, float] | None = None,
    contaminant: str = "TCE",
    release_rate_kg_s: float = 0.01,
    duration_days: float = 30.0,
    *,
    aoi_half_deg: float = DEFAULT_AOI_HALF_DEG,
    fetch_dem_for_streambed: bool = False,
    along_river_source: bool = True,
    aquifer_k_ms: float | None = None,
    porosity: float | None = None,
    compute_class: str = "standard",
    pipeline_emitter: Any | None = None,
) -> RiverSeepageResult:
    """Compose place/spill -> river geometry -> MODFLOW RIV+SRC -> seepage layer.

    Args:
        location: a place name (geocoded to the spill point). Supply this OR
            ``spill_location_latlon`` — exactly one.
        spill_location_latlon: an explicit ``(lat, lon)`` spill point.
        contaminant: contaminant name (conservative tracer; default ``"TCE"``).
        release_rate_kg_s: contaminant mass-release rate, kg/s (> 0).
        duration_days: release + transport duration, days (> 0).
        aoi_half_deg: half-width (deg) of the bbox fetched around the point.
        fetch_dem_for_streambed: when True also fetch a DEM (the engine can
            sample streambed elevation from it). The v0.1 RIV demo runs fine
            without it (demo streambed defaults), so it is OFF by default to
            keep the chain lean.
        along_river_source: place the contaminant SRC along the reach (True,
            the seepage source) vs at the spill point (False).
        aquifer_k_ms / porosity: optional demo-aquifer overrides.
        compute_class: FR-CE-3 compute class.
        pipeline_emitter: optional PipelineEmitter for live progress cards.

    Returns:
        ``RiverSeepageResult`` with the ``SeepageLayerURI`` + optional
        ``PlumeLayerURI`` + the derived-args dict + the narration summary dict.

    Raises:
        RiverSeepageScenarioInputError: neither / both of location /
            spill_location_latlon supplied.
        RiverSeepageScenarioError: a required step (geocode / river fetch /
            solver) failed.
        Propagates ``asyncio.CancelledError`` (Invariant 8).
    """
    has_loc = bool(location and location.strip())
    has_point = spill_location_latlon is not None
    if has_loc == has_point:  # both or neither
        raise RiverSeepageScenarioInputError(
            "supply exactly one of location or spill_location_latlon "
            f"(got location={has_loc}, spill_location_latlon={has_point})."
        )

    # task-168: declare the planned internal-tool count up front so the parent
    # workflow card's live breadcrumb can render "k/total". The plan is the
    # user-meaningful atomic-tool calls only: geocode (only when a place string
    # was supplied) + fetch_river_geometry (always) + fetch_dem (only when
    # streambed sampling is requested) + run_river_seepage_job (always). Each
    # ``substep(...)`` below is a NO-OP when no emitter is bound (the verify/CI
    # direct-call path), so the count is harmless there.
    _planned_substeps = 2  # fetch_river_geometry + run_river_seepage_job
    if has_loc:
        _planned_substeps += 1  # geocode_location
    if fetch_dem_for_streambed:
        _planned_substeps += 1  # fetch_dem
    begin_substeps(current_emitter(), _planned_substeps)

    # --- Stage 1: resolve the spill point ---
    if has_point:
        lat, lon = float(spill_location_latlon[0]), float(spill_location_latlon[1])  # type: ignore[index]
        location_name = location or f"({lat:.4f}, {lon:.4f})"
    else:
        geocode_fn = _registry_fn("geocode_location")
        async with substep(current_emitter(), "geocode_location"):
            geo = await _maybe_emit(
                pipeline_emitter,
                name=f"Geocode: {location}",
                tool_name="geocode_location",
                invoke=lambda: geocode_fn(location),
            )
        glat = geo.get("latitude") if isinstance(geo, dict) else None
        glon = geo.get("longitude") if isinstance(geo, dict) else None
        if glat is None or glon is None:
            raise RiverSeepageScenarioError(
                f"geocode_location({location!r}) returned no centroid lat/lon."
            )
        lat, lon = float(glat), float(glon)
        location_name = str(location)

    bbox = _bbox_around(lat, lon, aoi_half_deg)

    # --- Stage 2: fetch the river flowline ---
    fetch_river_fn = _registry_fn("fetch_river_geometry")
    async with substep(current_emitter(), "fetch_river_geometry"):
        river_layer = await _maybe_emit(
            pipeline_emitter,
            name="Fetch river geometry",
            tool_name="fetch_river_geometry",
            invoke=lambda: fetch_river_fn(bbox=bbox),
        )
    river_uri = _layer_uri_field(river_layer, "uri")
    if not river_uri:
        raise RiverSeepageScenarioError(
            "fetch_river_geometry returned no river flowline for the AOI; "
            "cannot drape a RIV boundary (no river near the spill point)."
        )

    # --- Stage 3: (optional) fetch a DEM for streambed sampling ---
    dem_uri: str | None = None
    if fetch_dem_for_streambed:
        try:
            fetch_dem_fn = _registry_fn("fetch_dem")
            async with substep(current_emitter(), "fetch_dem"):
                dem_layer = await _maybe_emit(
                    pipeline_emitter,
                    name="Fetch DEM (streambed)",
                    tool_name="fetch_dem",
                    invoke=lambda: fetch_dem_fn(bbox=bbox),
                )
            dem_uri = _layer_uri_field(dem_layer, "uri")
        except Exception as exc:  # noqa: BLE001 — DEM is optional, demo streambed otherwise
            logger.warning("river-seepage DEM fetch skipped (non-fatal): %s", exc)

    # --- Stage 4: run the river-seepage solver -> seepage + plume ---
    run_fn = _registry_fn("run_river_seepage_job")
    # task-168: wrap the solve as a nested child row. The failed-but-RETURNED
    # validation lives INSIDE the substep so a non-SeepageLayerURI result raises
    # here, marking the CHILD red (honesty floor: a failed solve never reads
    # green) before the error re-raises through the composer's existing path. The
    # two-card solver observability (mint_dispatch_and_sim_cards) is untouched:
    # the bridge tool still owns the Dispatch + Batch-bound Sim cards; this child
    # row is an additional nested timeline entry, not a replacement.
    async with substep(current_emitter(), "run_river_seepage_job"):
        result = await _maybe_emit(
            pipeline_emitter,
            name=f"Model river seepage [{contaminant}]",
            tool_name="run_river_seepage_job",
            invoke=lambda: run_fn(
                spill_location_latlon=(lat, lon),
                contaminant=contaminant,
                release_rate_kg_s=release_rate_kg_s,
                duration_days=duration_days,
                river_geometry_uri=river_uri,
                along_river_source=along_river_source,
                aquifer_k_ms=aquifer_k_ms,
                porosity=porosity,
                compute_class=compute_class,
            ),
        )
        if not isinstance(result, SeepageLayerURI):
            error_code = "RIVER_SEEPAGE_RUN_FAILED"
            error_message = "river-seepage run did not produce a seepage layer"
            if isinstance(result, dict):
                error_code = result.get("error_code", error_code)
                error_message = result.get("error_message", error_message)
            raise RiverSeepageScenarioError(f"{error_code}: {error_message}")

    seepage = result
    derived = {
        "location_name": location_name,
        "spill_location_latlon": [lat, lon],
        "bbox": list(bbox),
        "river_geometry_uri": river_uri,
        "dem_uri": dem_uri,
        "contaminant": contaminant,
        "release_rate_kg_s": release_rate_kg_s,
        "duration_days": duration_days,
        "along_river_source": along_river_source,
    }
    summary = {
        "location_name": location_name,
        "contaminant": contaminant,
        "total_leakage_m3_day": seepage.total_leakage_m3_day,
        "gaining_m3_day": seepage.gaining_m3_day,
        "losing_m3_day": seepage.losing_m3_day,
        "river_cell_count": seepage.river_cell_count,
        "demo_aquifer_caveat": (
            f"Aquifer K={DEFAULT_AQUIFER_K_MS:g} m/s, porosity={DEFAULT_POROSITY:g} "
            "and the streambed conductance are demo defaults, not site-specific "
            "hydrogeology."
        ),
    }
    logger.info(
        "river-seepage scenario complete location=%r total_leakage_m3_day=%.6g "
        "gaining=%.6g losing=%.6g cells=%d",
        location_name,
        seepage.total_leakage_m3_day,
        seepage.gaining_m3_day,
        seepage.losing_m3_day,
        seepage.river_cell_count,
    )

    return RiverSeepageResult(
        seepage_layer=seepage,
        plume_layer=None,  # the plume is loaded as a context layer by the tool
        derived_params=derived,
        summary=summary,
    )


# --------------------------------------------------------------------------- #
# Pipeline-emitter helper (mirror model_groundwater_contamination_scenario)
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
            name=name,
            tool_name=tool_name,
            invoke=invoke,
        )
    result = invoke()
    if asyncio.iscoroutine(result):
        result = await result
    return result


# --------------------------------------------------------------------------- #
# LLM-exposed thin atomic-tool wrapper (workflow_dispatch source class)
# --------------------------------------------------------------------------- #


_RUN_RIVER_SEEPAGE_SCENARIO_METADATA = AtomicToolMetadata(
    name="run_model_river_seepage_scenario",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
)


@register_tool(
    _RUN_RIVER_SEEPAGE_SCENARIO_METADATA,
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def run_model_river_seepage_scenario(
    location: str | None = None,
    spill_location_latlon: tuple[float, float] | list[float] | None = None,
    contaminant: str = "TCE",
    release_rate_kg_s: float = 0.01,
    duration_days: float = 30.0,
    along_river_source: bool = True,
    fetch_dem_for_streambed: bool = False,
    aquifer_k_ms: float | None = None,
    porosity: float | None = None,
    compute_class: str = "standard",
    # job-0164: absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """GROUNDWATER <-> river seepage EXCHANGE: is a reach gaining or losing, how much leaks between aquifer and river.

    NOT for surface-water transport down the channel: "a dye plume travels
    downstream", "how far does the dye/contaminant travel down the river", "a
    spill moving down the river" is ``run_telemac`` (surface flow IN the river),
    NOT this tool. This tool models the GROUNDWATER <-> river EXCHANGE (how much
    water leaks between the aquifer and the river, gaining vs losing reaches), NOT
    a plume moving down the channel.

    It turns a place (or spill point) + a contaminant into a rendered
    gaining/losing river-seepage layer: it geocodes the place, fetches the river
    flowline, drapes it onto a MODFLOW 6 grid as a RIV head-dependent
    river<->aquifer flux boundary, runs the GWF + MF6-GWT solver with an
    along-river SRC source, and publishes a DIVERGING seepage layer (where the
    river leaks into the aquifer vs draws baseflow out).

    Use this when:
        - The user wants to see whether a river reach is GAINING (baseflow out of
          the aquifer) or LOSING (leaking into the aquifer), or how much the river
          exchanges with the aquifer (streambed seepage flux, gaining/losing).
        - A contaminant enters the GROUNDWATER ALONG a river / stream (it seeps
          into the aquifer, it does not ride the surface current downstream).
        - The user asks to model river-coupled groundwater / streambed seepage.

    Do NOT use this for (see the routing block above):
        - Surface-water dye / tracer transport down the channel — ``run_telemac``.
        - A point spill with NO river (use ``run_modflow_job`` /
          ``run_model_groundwater_contamination_scenario``).
        - Surface-water flooding (use ``run_model_flood_scenario`` — SFINCS).

    Params:
        location: place name (geocoded). Supply this OR ``spill_location_latlon``.
        spill_location_latlon: explicit ``(lat, lon)`` point.
        contaminant: contaminant name (default ``"TCE"``; conservative tracer).
        release_rate_kg_s: mass-release rate, kg/s (default 0.01).
        duration_days: release + transport duration, days (default 30).
        along_river_source: place the SRC along the reach (default True).
        fetch_dem_for_streambed: also fetch a DEM for streambed elevation
            (default False — demo streambed otherwise).
        aquifer_k_ms / porosity: optional demo-aquifer overrides.
        compute_class: FR-CE-3 compute class. Default ``"standard"``.

    Returns:
        A JSON dict (``RiverSeepageResult.model_dump(mode="json")``) with the
        ``seepage_layer`` (a ``SeepageLayerURI`` carrying ``total_leakage_m3_day``
        + ``gaining_m3_day`` + ``losing_m3_day`` + ``river_cell_count`` — the
        agent narrates these typed numbers), the ``derived_params``, and the
        ``summary`` narration dict. On a recoverable failure the tool raises a
        typed error the agent narrates honestly.

    FR-DC-6: ``cacheable=False`` + ``ttl_class="live-no-cache"`` +
    ``source_class="workflow_dispatch"`` — the cache shim is NOT invoked.
    """
    point: tuple[float, float] | None = None
    if spill_location_latlon is not None:
        try:
            from ..tool_arg_normalizer import coerce_latlon

            point = tuple(coerce_latlon(spill_location_latlon))  # type: ignore[assignment]
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "error",
                "error_code": "RIVER_SEEPAGE_SCENARIO_INPUT_INVALID",
                "error_message": f"invalid spill_location_latlon: {exc}",
            }
    try:
        result = await model_river_seepage_scenario(
            location=location,
            spill_location_latlon=point,
            contaminant=contaminant,
            release_rate_kg_s=float(release_rate_kg_s),
            duration_days=float(duration_days),
            along_river_source=bool(along_river_source),
            fetch_dem_for_streambed=bool(fetch_dem_for_streambed),
            aquifer_k_ms=aquifer_k_ms,
            porosity=porosity,
            compute_class=compute_class,
            pipeline_emitter=None,
        )
    except RiverSeepageScenarioError as exc:
        return {
            "status": "error",
            "error_code": getattr(exc, "error_code", "RIVER_SEEPAGE_SCENARIO_ERROR"),
            "error_message": str(exc),
        }
    return result.model_dump(mode="json")
