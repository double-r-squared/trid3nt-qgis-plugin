"""``model_saltwater_intrusion_scenario``  -  MODFLOW Wave-5 BUY saltwater-intrusion
composer.

The end-to-end higher-order workflow for the sprint-18 Wave-5 MODFLOW
``saltwater_intrusion`` archetype: it turns a coastal AOI point + a user-supplied
cross-section transect (two lat/lon endpoints, A=seaward -> B=inland) into a
rendered saltwater-wedge cross-section chart + a map transect line + toe point.

Canonical real-world pipeline mirrored here (a Henry-style variable-density
saltwater intrusion analysis -- the standard MODFLOW BUY / VDF coastal
groundwater study):

    resolve the AOI point (geocode a place, or take an explicit lat/lon)
        -> the user supplies the coastal transect (A=seaward, B=inland
           lat/lon endpoints, or a coastal point + bearing; NEVER fabricated --
           a missing transect is a typed InputError, Invariant 9)
        -> assemble MODFLOWRunArgs(archetype='saltwater_intrusion',
                                   coastal_transect_latlon=(...),
                                   seawater_salinity_ppt=35.0, ...)
        -> run_modflow_archetype_job (GWF+GWT BUY variable-density deck ->
           mf6 LOCAL -> postprocess_saltwater_intrusion)
        -> SaltwaterWedgeLayerURI (vector: transect LINE + toe POINT in FGB)
           + cross-section heatmap chart via _chart_payload

PRIMARY product: a Vega-Lite cross-section heatmap chart (x = distance inland m,
y = depth m, colour = salinity ppt, + 50%-isochlor toe rule). The chart is built
by ``postprocess_saltwater_intrusion`` (no second UCN read here) and stashed as
``_chart_payload`` on the returned ``SaltwaterWedgeLayerURI``; the composer reads
it via ``getattr(layer, '_chart_payload', None)`` and emits it through
``emit_chart_payloads``.

MAP element (thin): a FlatGeobuf VECTOR with the coastal transect LINE (A->B) and
the 50%-isochlor toe POINT, rendered via the inline-GeoJSON path with the
``saltwater_intrusion`` style preset (teal #1ABC9C).

Invariants:
- **1 / 2 / 8: preserve** (typed numbers, deterministic composition, cancellable).
- **9. No fabricated model inputs.** A ``saltwater_intrusion`` run with no coastal
  transect returns a typed ``USER_INPUT_REQUIRED`` failure -- a coastline can
  NEVER be fabricated. The transect is a user input: the user must supply either
  two lat/lon endpoints or a coastal point + bearing.
- **10. Minimal parameter surface: preserves.** Intent (transect + optional
  salinity / vertical layers) is exposed; the grid dimensions and Henry-style
  aquifer defaults are derived, not user-supplied.

PRECISION CAVEAT (Invariant 1): the cross-section is a DEMO Henry-style
variable-density simulation on a 100-column x nlay-layer structured grid with
demo aquifer K = 1e-4 m/s and default porosity. It is a qualitative illustration
of wedge dynamics, not a site-calibrated intrusion depth forecast. The agent must
narrate this caveat when presenting the chart.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import Field

from grace2_contracts.common import GraceModel
from grace2_contracts.modflow_contracts import (
    DEFAULT_AQUIFER_K_MS,
    DEFAULT_POROSITY,
    MODFLOWRunArgs,
    SaltwaterWedgeLayerURI,
)
from grace2_contracts.tool_registry import AtomicToolMetadata

from ..pipeline_emitter import begin_substeps, current_emitter, emit_chart_payloads
from ..tools import register_tool
# Reuse the shared archetype-run + AOI-resolve helpers from the sustainable_yield
# composer (one implementation, all archetypes).
from .model_sustainable_yield_scenario import (
    _aquifer_overrides,
    _coerce_optional_latlon,
    _resolve_aoi_point,
    _run_archetype,
)

logger = logging.getLogger("grace2_agent.workflows.model_saltwater_intrusion_scenario")

__all__ = [
    "SaltwaterIntrusionResult",
    "model_saltwater_intrusion_scenario",
    "run_model_saltwater_intrusion_scenario",
    "SaltwaterIntrusionScenarioError",
    "SaltwaterIntrusionInputError",
]


# --------------------------------------------------------------------------- #
# Result envelope
# --------------------------------------------------------------------------- #


class SaltwaterIntrusionResult(GraceModel):
    """Return type for ``model_saltwater_intrusion_scenario`` (sprint-18 Wave-5).

    Bundles the saltwater-wedge vector layer + the derived args + a narration
    summary dict. Invariant 1: every narrated number is a typed field --
    ``intrusion_layer`` carries ``intrusion_length_m``, ``toe_distance_m``,
    ``seaward_salinity_ppt``, and ``transect_endpoints``.
    """

    schema_version: str = "v1"

    intrusion_layer: SaltwaterWedgeLayerURI
    derived_params: dict[str, Any] = Field(default_factory=dict)
    summary: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Typed errors
# --------------------------------------------------------------------------- #


class SaltwaterIntrusionScenarioError(RuntimeError):
    """Base class for ``model_saltwater_intrusion_scenario`` failures."""

    error_code: str = "SALTWATER_INTRUSION_SCENARIO_ERROR"
    retryable: bool = False


class SaltwaterIntrusionInputError(SaltwaterIntrusionScenarioError):
    """Caller supplied invalid / missing transect or AOI input (honesty gate).

    Invariant 9: the coastal transect is NEVER fabricated. A
    ``saltwater_intrusion`` run with no transect endpoints raises this error so
    the agent asks the user for the real coastline coordinates.
    """

    error_code = "SALTWATER_INTRUSION_INPUT_INVALID"


# --------------------------------------------------------------------------- #
# The composer
# --------------------------------------------------------------------------- #


async def model_saltwater_intrusion_scenario(
    location: str | None = None,
    aoi_latlon: tuple[float, float] | None = None,
    *,
    coastal_transect_latlon: tuple[
        tuple[float, float], tuple[float, float]
    ] | None = None,
    seawater_salinity_ppt: float = 35.0,
    n_vertical_layers: int = 20,
    freshwater_inflow_m3_day: float | None = None,
    aquifer_k_ms: float | None = None,
    porosity: float | None = None,
    compute_class: str = "standard",
    pipeline_emitter: Any | None = None,
) -> SaltwaterIntrusionResult:
    """Compose coastal AOI + transect -> MODFLOW BUY -> SaltwaterWedgeLayerURI.

    Args:
        location: a place name (geocoded). Supply this OR ``aoi_latlon``.
        aoi_latlon: an explicit ``(lat, lon)`` AOI point.
        coastal_transect_latlon: two ``(lat, lon)`` endpoints defining the
            cross-section axis: A = seaward end (ocean side), B = inland end.
            REQUIRED -- a missing transect is a typed USER_INPUT_REQUIRED failure
            (Invariant 9). The coastline can NEVER be fabricated; ask the user.
        seawater_salinity_ppt: salinity at the seaward GHB+AUX boundary (ppt).
            Default 35.0 (open ocean). Lower values model estuarine / brackish
            conditions.
        n_vertical_layers: number of vertical model layers (nlay). Default 20;
            bounds [4, 80]. More layers resolve the density interface more sharply
            at the cost of slightly longer runtime.
        freshwater_inflow_m3_day: freshwater inflow at the inland WEL+AUX
            boundary, m^3/day. When None the adapter auto-derives a
            Henry-representative flux from the transect geometry + aquifer K.
        aquifer_k_ms / porosity: optional demo-aquifer overrides.
        compute_class: FR-CE-3 compute class. NOTE: saltwater_intrusion is
            LOCAL-ONLY (the Henry demo grid is small + fast; Batch is not used).
        pipeline_emitter: optional PipelineEmitter for live progress cards.

    Returns:
        ``SaltwaterIntrusionResult`` with the ``SaltwaterWedgeLayerURI`` (a
        vector layer carrying ``intrusion_length_m`` + ``toe_distance_m`` +
        ``seaward_salinity_ppt`` + ``transect_endpoints``) + derived args +
        a narration summary. The cross-section heatmap chart is emitted as a
        side effect via ``emit_chart_payloads``.

    Raises:
        SaltwaterIntrusionInputError: missing/invalid transect or AOI (Invariant 9
            gate -- the coastline is never invented).
        SaltwaterIntrusionScenarioError: a required step (geocode / solver) failed.
        Propagates ``asyncio.CancelledError`` (Invariant 8).
    """
    # --- Honesty gate (Invariant 9): NEVER fabricate the coastal transect ------
    if coastal_transect_latlon is None:
        raise SaltwaterIntrusionInputError(
            "saltwater_intrusion requires a coastal transect "
            "(coastal_transect_latlon): two (lat, lon) endpoints A=seaward, "
            "B=inland that define the cross-section axis. The transect is a "
            "user input and is NEVER invented; ask the user to supply or draw "
            "the coastal cross-section line."
        )

    # Validate and unpack the transect.
    try:
        (lat_a, lon_a), (lat_b, lon_b) = (
            (float(coastal_transect_latlon[0][0]), float(coastal_transect_latlon[0][1])),
            (float(coastal_transect_latlon[1][0]), float(coastal_transect_latlon[1][1])),
        )
    except Exception as exc:  # noqa: BLE001
        raise SaltwaterIntrusionInputError(
            f"invalid coastal_transect_latlon (expected "
            f"((lat_a, lon_a), (lat_b, lon_b))): {exc}"
        ) from exc

    # Validate scalar fields.
    try:
        salinity = float(seawater_salinity_ppt)
        if salinity <= 0.0:
            raise ValueError("seawater_salinity_ppt must be > 0")
        nlay = int(n_vertical_layers)
        if not (4 <= nlay <= 80):
            raise ValueError("n_vertical_layers must be in [4, 80]")
    except SaltwaterIntrusionInputError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SaltwaterIntrusionInputError(
            f"invalid saltwater_intrusion parameter: {exc}"
        ) from exc

    # task-168: declare the planned internal-tool count up front.
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
            contaminant="n/a",        # GWF+GWT BUY archetype: no external solute
            release_rate_kg_s=1.0,    # ignored when archetype is set
            duration_days=1.0,        # ignored when archetype is set
            archetype="saltwater_intrusion",
            coastal_transect_latlon=((lat_a, lon_a), (lat_b, lon_b)),
            seawater_salinity_ppt=salinity,
            n_vertical_layers=nlay,
            freshwater_inflow_m3_day=freshwater_inflow_m3_day,
            **_aquifer_overrides(aquifer_k_ms, porosity, None, None),
        )
    except Exception as exc:  # noqa: BLE001  -  pydantic ValidationError
        raise SaltwaterIntrusionInputError(
            f"invalid saltwater_intrusion run arguments: {exc}"
        ) from exc

    label = (
        f"Model saltwater intrusion wedge "
        f"[{salinity:g} ppt seawater, {nlay} layers]"
    )
    layer = await _run_archetype(
        run_args,
        compute_class=compute_class,
        pipeline_emitter=pipeline_emitter,
        tool_label=label,
        expected_type=SaltwaterWedgeLayerURI,
        error_code="SALTWATER_INTRUSION_RUN_FAILED",
        scenario_error=SaltwaterIntrusionScenarioError,
    )

    # Emit the cross-section heatmap chart that postprocess built and stashed as
    # ``_chart_payload`` on the layer (best-effort; no-op when None).
    chart = getattr(layer, "_chart_payload", None)
    if chart is not None:
        await emit_chart_payloads(chart)

    intrusion_m = getattr(layer, "intrusion_length_m", 0.0)
    toe_m = getattr(layer, "toe_distance_m", 0.0)
    transect_eps = getattr(layer, "transect_endpoints", ((lat_a, lon_a), (lat_b, lon_b)))

    derived = {
        "location_name": location_name,
        "aoi_latlon": [lat, lon],
        "coastal_transect_latlon": [[lat_a, lon_a], [lat_b, lon_b]],
        "seawater_salinity_ppt": salinity,
        "n_vertical_layers": nlay,
        "freshwater_inflow_m3_day": freshwater_inflow_m3_day,
    }
    summary = {
        "location_name": location_name,
        "intrusion_length_m": intrusion_m,
        "toe_distance_m": toe_m,
        "seaward_salinity_ppt": layer.seaward_salinity_ppt,
        "transect_endpoints": [list(transect_eps[0]), list(transect_eps[1])],
        "demo_aquifer_caveat": (
            f"Aquifer K={DEFAULT_AQUIFER_K_MS:g} m/s and porosity="
            f"{DEFAULT_POROSITY:g} are demo defaults, not site-specific "
            "hydrogeology. The cross-section is a qualitative Henry-style "
            "variable-density simulation on a 100-column structured grid -- "
            "treat it as a planning-level wedge illustration, not a calibrated "
            "intrusion depth forecast."
        ),
    }
    logger.info(
        "saltwater_intrusion scenario complete location=%r intrusion_length_m=%.3g "
        "seaward_salinity_ppt=%.3g",
        location_name,
        intrusion_m,
        layer.seaward_salinity_ppt,
    )
    return SaltwaterIntrusionResult(
        intrusion_layer=layer, derived_params=derived, summary=summary
    )


# --------------------------------------------------------------------------- #
# LLM-exposed thin atomic-tool wrapper (workflow_dispatch source class)
# --------------------------------------------------------------------------- #


_METADATA = AtomicToolMetadata(
    name="run_model_saltwater_intrusion_scenario",
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
async def run_model_saltwater_intrusion_scenario(
    location: str | None = None,
    aoi_latlon: tuple[float, float] | list[float] | None = None,
    coastal_transect_latlon: Any | None = None,
    seawater_salinity_ppt: float = 35.0,
    n_vertical_layers: int = 20,
    freshwater_inflow_m3_day: float | None = None,
    aquifer_k_ms: float | None = None,
    porosity: float | None = None,
    compute_class: str = "standard",
    # job-0164: absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Model a coastal saltwater intrusion wedge (Henry-style variable-density BUY).

    Builds a MODFLOW 6 GWF+GWT BUY (variable-density) vertical cross-section
    model along a user-supplied coastal transect, runs it, and produces:

      * A Vega-Lite CROSS-SECTION HEATMAP chart (x = distance inland m,
        y = depth m, colour = salinity ppt, + 50%-isochlor toe rule) -- the
        primary physical deliverable.
      * A VECTOR MAP layer: a FlatGeobuf transect LINE (A=seaward -> B=inland)
        + a toe POINT at the 50%-isochlor penetration depth, rendered teal
        (#1ABC9C) via the ``saltwater_intrusion`` style preset.
      * HEADLINE SCALAR: ``intrusion_length_m`` -- bottom-layer 50%-isochlor
        toe penetration from the seaward boundary, m. Narrate this as the key
        physical result.

    Use this when:
        - The user asks about saltwater intrusion, seawater wedge, coastal
          groundwater salinisation, or freshwater/saltwater interface depth.
        - The user wants to see how far inland the saltwater wedge penetrates.

    Do NOT use this for:
        - Surface coastal flooding (use ``run_model_flood_scenario`` / SFINCS).
        - A pumping-well drawdown (use ``run_model_sustainable_yield_scenario``).
        - Contaminant plume transport (use ``run_modflow_job``).

    PRECISION CAVEAT: this is a demo Henry-style variable-density simulation
    (100-column structured cross-section, demo aquifer K=1e-4 m/s). Narrate it
    as a qualitative wedge illustration, NOT a calibrated intrusion forecast.

    Params:
        location: place name (geocoded). Supply this OR ``aoi_latlon``.
        aoi_latlon: explicit ``(lat, lon)`` AOI point.
        coastal_transect_latlon: ``[[lat_a, lon_a], [lat_b, lon_b]]`` (A=seaward,
            B=inland). REQUIRED -- never invented; ask the user if absent (Invariant 9).
        seawater_salinity_ppt: boundary salinity at the seaward end, ppt. Default
            35.0 (open ocean). Lower for estuarine / brackish conditions.
        n_vertical_layers: number of vertical model layers (default 20; range 4..80).
        freshwater_inflow_m3_day: inland freshwater inflow, m^3/day. None -> adapter
            auto-derives from geometry + aquifer K.
        aquifer_k_ms / porosity: optional demo-aquifer overrides.
        compute_class: FR-CE-3 compute class. Default ``"standard"``. This archetype
            runs LOCAL-ONLY (the Henry demo grid is small + fast; Batch is not used).

    Returns:
        On success: a ``SaltwaterIntrusionResult`` JSON dict with the
        ``intrusion_layer`` (a ``SaltwaterWedgeLayerURI`` carrying
        ``intrusion_length_m`` + ``toe_distance_m`` + ``seaward_salinity_ppt`` +
        ``transect_endpoints``), the ``derived_params``, and the ``summary``.
        On a recoverable failure (incl. a missing transect) the tool returns a
        typed error the agent narrates honestly -- it never fabricates a transect.

    FR-DC-6: ``cacheable=False`` + ``ttl_class="live-no-cache"`` +
    ``source_class="workflow_dispatch"``  -  the cache shim is NOT invoked.
    """
    aoi = _coerce_optional_latlon(aoi_latlon)

    # Coerce the coastal transect: accept list-of-lists or tuple-of-tuples.
    transect: tuple[tuple[float, float], tuple[float, float]] | None = None
    if coastal_transect_latlon is not None:
        try:
            pts = list(coastal_transect_latlon)
            transect = (
                (float(pts[0][0]), float(pts[0][1])),
                (float(pts[1][0]), float(pts[1][1])),
            )
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "error",
                "error_code": "USER_INPUT_REQUIRED",
                "error_message": (
                    f"coastal_transect_latlon must be a list/tuple of two "
                    f"[[lat_a, lon_a], [lat_b, lon_b]] endpoints: {exc}"
                ),
            }

    try:
        result = await model_saltwater_intrusion_scenario(
            location=location,
            aoi_latlon=aoi,
            coastal_transect_latlon=transect,
            seawater_salinity_ppt=float(seawater_salinity_ppt),
            n_vertical_layers=int(n_vertical_layers),
            freshwater_inflow_m3_day=(
                float(freshwater_inflow_m3_day)
                if freshwater_inflow_m3_day is not None
                else None
            ),
            aquifer_k_ms=aquifer_k_ms,
            porosity=porosity,
            compute_class=compute_class,
            pipeline_emitter=None,
        )
    except SaltwaterIntrusionInputError as exc:
        return {
            "status": "error",
            "error_code": "USER_INPUT_REQUIRED",
            "error_message": str(exc),
        }
    except SaltwaterIntrusionScenarioError as exc:
        return {
            "status": "error",
            "error_code": getattr(exc, "error_code", "SALTWATER_INTRUSION_SCENARIO_ERROR"),
            "error_message": str(exc),
        }
    return result.model_dump(mode="json")
