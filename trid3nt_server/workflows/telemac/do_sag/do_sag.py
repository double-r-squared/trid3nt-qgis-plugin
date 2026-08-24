"""Engine template ``telemac_do_sag`` - TELEMAC-2D WAQTEL dissolved-oxygen sag.

Declared as PARAMS + ``plan(p, d)``: the tool body resolves the doors, validates
the plan, and hands it to the interpreter. See
``docs/design/declarative-workflows.md``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from trid3nt_contracts.telemac_contracts import TelemacDoLayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata, ResolutionSpec

from trid3nt_server.data import register_tool
from trid3nt_server.data.tool_arg_normalizer import coerce_bbox_value
from trid3nt_server.declarative import (
    DeclarativeError,
    DrawGate,
    Param,
    RunMode,
    Workflow,
    doors,
    interpret,
    merge_provenance,
    render_docstring,
    resolve_params,
)
from trid3nt_server.workflows.telemac._template_card import TemplateCard
from trid3nt_server.workflows.telemac.run_products import persist_run_products
from trid3nt_server.workflows.telemac.do_sag.steps import (
    OutfallCoordsInvalidError,
    ReachSolve,
    coerce_outfall_point,
)

logger = logging.getLogger("trid3nt_server.workflows.telemac.do_sag.do_sag")

__all__ = ["DATA", "PARAMS", "plan", "telemac_do_sag"]

_STEPS = "trid3nt_server.workflows.telemac.do_sag.steps"

QUESTION = (
    "the DISSOLVED-OXYGEN SAG below a permitted discharge / WWTP outfall in a "
    "river reach (US TMDL / Clean Water Act permit): where does DO bottom out "
    "downstream and does it VIOLATE the water-quality standard? (TELEMAC-2D "
    "WAQTEL O2 / Streeter-Phelps oxygen sag over a real reach)"
)

TEMPLATE_CARD = TemplateCard(
    question=QUESTION,
    required_inputs=["location OR bbox"],
    knobs=(
        "discharge_bod_mgl, upstream_do_mgl, water_temp_c, do_standard_mgl, "
        "k1_per_day, k2_per_day, reach_length_km, discharge_m3s, mesh_resolution, "
        "bank_source, outfall_coords"
    ),
)


PARAMS: tuple[Param, ...] = (
    Param("location", door=doors.QUESTION, optional=True, consequence="aoi",
          desc="Place name near the discharge, geocoded to the reach"),
    Param("bbox", door=doors.USER, optional=True, consequence="aoi",
          desc="Explicit AOI (min_lon,min_lat,max_lon,max_lat) EPSG:4326, instead of a place"),
    Param("outfall_coords", door=doors.USER, optional=True, consequence="scenario",
          user_lever=True,
          derived_when_absent=(
              "the release is seeded at the reach point the pipeline derives "
              "(mid-reach on the fetched flowline, else the geocoded centroid); the "
              "sag distance is measured downstream from there"),
          desc="Where the discharge enters the water, (lon, lat); unset seeds the "
               "reach at the derived reach point"),

    Param("discharge_bod_mgl", door=doors.SCENARIO, default=20.0,
          bounds=(0.1, 5000.0), units="mg/L", consequence="scenario",
          desc="Fully-mixed ultimate carbonaceous BOD at the top of the reach - "
               "the pollutant source-term question"),
    Param("water_temp_c", door=doors.SCENARIO, default=20.0, bounds=(0.0, 40.0),
          units="C", consequence="scenario",
          desc="Water temperature, which sets the DO saturation the deficit is "
               "measured against; 20 C is the standard Streeter-Phelps condition"),
    Param("do_standard_mgl", door=doors.SCENARIO, default=5.0, bounds=(0.0, 15.0),
          units="mg/L", consequence="scenario",
          desc="The DO water-quality standard the sag is judged against; 5 is a "
               "common warm-water aquatic-life criterion"),
    Param("k1_per_day", door=doors.SCENARIO, default=0.3, bounds=(0.01, 20.0),
          units="1/day", consequence="numerical",
          desc="CBOD deoxygenation rate - a documented rate coefficient"),
    Param("k2_per_day", door=doors.SCENARIO, default=0.9, bounds=(0.01, 50.0),
          units="1/day", consequence="numerical",
          desc="Surface reaeration rate - a documented rate coefficient"),
    Param("reach_length_km", door=doors.SCENARIO, default=12.0, bounds=(0.5, 15.0),
          units="km", consequence="aoi",
          desc="Modeled reach length downstream of the discharge; the sag critical "
               "point is often several km down"),

    Param("do_saturation_mgl", door=doors.DERIVED, resolve=f"{_STEPS}.do_saturation_mgl",
          user_lever=True, bounds=(0.0, 20.0), units="mg/L", consequence="scenario",
          desc="DO saturation Cs; derived from water temperature unless supplied"),
    Param("upstream_do_mgl", door=doors.DERIVED, resolve=f"{_STEPS}.upstream_do_mgl",
          user_lever=True, bounds=(0.0, 20.0), units="mg/L", consequence="scenario",
          desc="DO carried in at the top of the reach; derived as saturation unless supplied"),

    Param("channel_width_m", door=doors.CONSTANT, default=60.0, bounds=(1.0, 5000.0),
          units="m", consequence="numerical",
          desc="Modeled channel width, used for the mesh node estimate and for the "
               "assumed ribbon when bank_source is not nhd_area"),
    Param("sim_duration_s", door=doors.CONSTANT, default=10800.0,
          bounds=(60.0, 864000.0), units="s", consequence="numerical",
          desc="Simulated time to reach the steady-state sag"),
    Param("mesh_resolution", door=doors.CONSTANT, default="auto",
          consequence="numerical",
          desc="Mesh sizing mode: auto | fine | coarse"),
    Param("mesh_resolution_m", door=doors.USER, optional=True, user_lever=True,
          bounds=(3.0, 5000.0), units="m", consequence="numerical",
          desc="Explicit target element edge length, overriding the sizing mode"),
    Param("bank_source", door=doors.CONSTANT, default="nhd_area",
          consequence="scenario",
          desc="Bank geometry source: nhd_area (real polygons, else a typed refusal) "
               "| constant_ribbon (assumed width)"),
    Param("discharge_m3s", door=doors.USER, optional=True, units="m^3/s",
          bounds=(0.01, 1.0e5), consequence="physics", user_lever=True,
          desc="Steady carrier discharge; unset resolves from the NOAA National "
               "Water Model at the reach"),
    Param("compute_class", door=doors.CONSTANT, default="medium",
          consequence="numerical", desc="Solve sizing class"),
)

#: The reach pipeline is ONE composite step in v1; its internal fetches surface
#: through the emit-on-fetch seam. They become declared Data when river_dye is
#: migrated.
DATA: tuple = ()


def plan(p, d):  # noqa: ANN001, ANN201 - the declared plan value, per the design doc
    """The DO-sag recipe. Pure: constructs the plan value, executes nothing."""
    return Workflow("telemac_do_sag", engine="telemac2d")[
        DrawGate(param="outfall_coords", geometry="point",
                 prompt="Click where the discharge enters the river"),
        ReachSolve.telemac_waqtel_o2(
            location=p.location, bbox=p.bbox, outfall_coords=p.outfall_coords,
            discharge_bod_mgl=p.discharge_bod_mgl,
            upstream_do_mgl=p.upstream_do_mgl,
            do_saturation_mgl=p.do_saturation_mgl,
            water_temp_c=p.water_temp_c, do_standard_mgl=p.do_standard_mgl,
            k1_per_day=p.k1_per_day, k2_per_day=p.k2_per_day,
            reach_length_km=p.reach_length_km, channel_width_m=p.channel_width_m,
            sim_duration_s=p.sim_duration_s, discharge_m3s=p.discharge_m3s,
            mesh_resolution=p.mesh_resolution, mesh_resolution_m=p.mesh_resolution_m,
            bank_source=p.bank_source, compute_class=p.compute_class,
            input_mode=RunMode,
        ).named("do_field")
         .chart("do_sag_curve", builder=f"{_STEPS}.build_sag_chart"),
    ]


_TELEMAC_DO_SAG_RES_SPEC = ResolutionSpec(
    param="mesh_resolution_m",
    unit="m",
    min_value=3.0,
    native_hint="NHD channel geometry + 3DEP terrain; edge sized from reach width",
    constraint_source="solver",
    rationale=(
        "explicit target edge length; 3 m is the absolute finest the TELEMAC mesh "
        "builder authors, a long reach is coarsened under the node budget "
        "(self-labeled); no fixed coarse ceiling"
    ),
)

_TELEMAC_DO_SAG_METADATA = AtomicToolMetadata(
    name="telemac_do_sag",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="telemac",
    tier="template",
    resolution_specs=(_TELEMAC_DO_SAG_RES_SPEC,),
)


@register_tool(
    _TELEMAC_DO_SAG_METADATA,
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def telemac_do_sag(
    location: str | None = None,
    bbox: tuple[float, float, float, float] | list[float] | str | None = None,
    outfall_coords: tuple[float, float] | list[float] | None = None,
    discharge_bod_mgl: float | None = None,
    upstream_do_mgl: float | None = None,
    water_temp_c: float | None = None,
    do_saturation_mgl: float | None = None,
    do_standard_mgl: float | None = None,
    k1_per_day: float | None = None,
    k2_per_day: float | None = None,
    reach_length_km: float | None = None,
    channel_width_m: float | None = None,
    sim_duration_s: float | None = None,
    discharge_m3s: float | None = None,
    mesh_resolution: str | None = None,
    mesh_resolution_m: float | None = None,
    bank_source: str | None = None,
    compute_class: str | None = None,
    input_mode: str | None = None,
    restart_clean: bool = False,
    **_extra_ignored: Any,
) -> TelemacDoLayerURI | dict[str, Any]:
    supplied, err = _normalize(locals())
    if err is not None:
        return err
    try:
        p = await resolve_params(PARAMS, supplied)
        result = await interpret(
            plan(p, None), p, PARAMS, DATA,
            input_mode=input_mode, resume=not restart_clean,
        )
    except asyncio.CancelledError:
        raise
    except DeclarativeError as exc:
        logger.warning("telemac_do_sag %s: %s", exc.error_code, exc)
        return {"status": "error", "error_code": exc.error_code,
                "error_message": _with_notes(exc)}
    except Exception as exc:  # noqa: BLE001
        if getattr(exc, "retryable", False):
            # The banks/reach gates carry .suggestions the adapter harvests off the
            # RAISED exception, so the model can retry with corrected args.
            raise
        logger.exception("telemac_do_sag unexpected failure")
        return {"status": "error", "error_code": "TELEMAC_INTERNAL_ERROR",
                "error_message": _with_notes(exc)}

    layer = result.value
    update: dict[str, Any] = {
        "synthetic_inputs": merge_provenance(layer.synthetic_inputs or [],
                                             result.entries),
    }
    if result.notes:
        parts = [layer.fallback_note] if layer.fallback_note else []
        parts += [f"NOTE: {n}" for n in result.notes]
        update["fallback_note"] = " ".join(parts)
    layer = layer.model_copy(update=update)
    await persist_run_products(
        getattr(layer, "run_id", None),
        charts=result.charts, metrics=_physical_answer(layer),
    )
    logger.info(
        "telemac_do_sag complete layer_id=%s do_min=%.3g mg/L at %sm violates=%s "
        "executed=%s replayed=%s notes=%s",
        layer.layer_id, layer.do_min_mgl, layer.do_min_distance_m,
        layer.do_violates_standard, result.executed, result.replayed, result.notes,
    )
    return layer


def _physical_answer(layer: TelemacDoLayerURI) -> dict[str, Any]:
    """The run's ANSWER, as the numbers a reader has to be able to check.

    Persisted beside the chart spec so verification cites the run's own figures
    rather than recomputing them from the raster.
    """
    return {
        "do_min_mgl": layer.do_min_mgl,
        "do_min_distance_m": layer.do_min_distance_m,
        "do_standard_mgl": layer.do_standard_mgl,
        "do_violates_standard": layer.do_violates_standard,
        "do_upstream_mgl": layer.do_upstream_mgl,
        "do_saturation_mgl": layer.do_saturation_mgl,
        "bod_upstream_mgl": layer.bod_upstream_mgl,
        "sag_curve_distance_m": layer.sag_curve_distance_m,
        "sag_curve_do_mgl": layer.sag_curve_do_mgl,
        "sag_curve_bod_mgl": layer.sag_curve_bod_mgl,
        "mesh_size_m": layer.mesh_size_m,
        "layer_uri": layer.uri,
    }


def _with_notes(exc: BaseException) -> str:
    """The failure, plus whatever auxiliary products the run also lost on the way."""
    notes = getattr(exc, "__notes__", ()) or ()
    return " ".join([str(exc), *notes])


def _normalize(args: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Coerce the wire args to the door-1 sheet: exactly one of location or bbox."""
    try:
        outfall = coerce_outfall_point(args.get("outfall_coords"))
    except OutfallCoordsInvalidError as exc:
        return {}, {"status": "error", "error_code": "TELEMAC_PARAMS_INVALID",
                    "error_message": str(exc)}

    location, bbox = args.get("location"), args.get("bbox")
    coerced: tuple[float, float, float, float] | None = None
    if bbox is not None:
        cb = coerce_bbox_value(bbox)
        if cb is None:
            if isinstance(bbox, str) and any(c.isalpha() for c in bbox) \
                    and not (location and str(location).strip()):
                location, bbox = bbox, None
            else:
                return {}, {"status": "error", "error_code": "TELEMAC_PARAMS_INVALID",
                            "error_message": f"invalid bbox: {bbox!r}"}
        else:
            coerced = tuple(cb)  # type: ignore[assignment]

    has_loc = bool(location and str(location).strip())
    if not has_loc and coerced is None:
        return {}, {"status": "error", "error_code": "TELEMAC_PARAMS_INCOMPLETE",
                    "error_message": ("telemac_do_sag needs a place `location` "
                                      "(geocoded) or an explicit `bbox` AOI.")}
    if has_loc:
        coerced = None  # location wins

    declared = {p.name for p in PARAMS}
    supplied = {k: v for k, v in args.items() if k in declared and v is not None}
    supplied["location"] = location if has_loc else None
    supplied["bbox"] = coerced
    supplied["outfall_coords"] = outfall
    return {k: v for k, v in supplied.items() if v is not None}, None


_DOC = dict(
    summary="DISSOLVED-OXYGEN SAG below a discharge in a river (US TMDL / permit question).",
    routing=(
        "THE tool for \"where does dissolved oxygen bottom out below this discharge\", "
        "\"will the DO sag violate the standard\", \"Streeter-Phelps oxygen sag\", \"BOD "
        "loading / oxygen demand downstream of a WWTP / outfall\", \"DO TMDL for this "
        "reach\". Solves TELEMAC-2D + WAQTEL O2 over a REAL NHDPlus reach modeled "
        "STARTING at the fully-mixed discharge: the mixed carbonaceous BOD + DO enter "
        "at the top of the reach, CBOD decays downstream (deoxygenation k1) consuming "
        "oxygen, and surface reaeration (k2) recovers it. Produces a DISSOLVED-O2 field "
        "map + the along-reach DO-sag curve + the sag-minimum location/value. Supply a "
        "place `location` (geocoded) OR an explicit `bbox`."
    ),
    not_for=(
        "a conservative dye/tracer/contaminant plume that only dilutes "
        "(`telemac_river_dye`); groundwater plumes (`modflow_*`); flood depth "
        "(`sfincs_flood` / `hecras_riverine_flood`)"
    ),
    params=PARAMS,
    controls=(
        ("input_mode",
         '"user_gated" presents the resolved carrier discharge and bank source for '
         'review/edit before the solve and WAITS; "auto" (session default) proceeds '
         "with every assumption labeled. Not a physical value."),
        ("restart_clean",
         "True discards the ledger a PREVIOUS FAILED attempt at this same invocation "
         "left behind and re-runs every step from the top. Default False resumes at "
         "the failed step. A run that completed is marked complete and is never "
         "replayed, so a fresh invocation always re-solves against live upstream "
         "data."),
    ),
    returns=(
        "On success a `TelemacDoLayerURI` (a `LayerURI` subtype) - the emitter loads "
        "the DISSOLVED-O2 field map and animates the SELAFIN sibling. It carries "
        "`do_min_mgl` / `do_min_distance_m` / `do_violates_standard` + `sag_curve_*`; "
        "narrate those typed numbers. On failure a dict with `status=\"error\"` + "
        "`error_code`."
    ),
)

#: The full sheet is what the MODEL needs (it fills the params); the routing view
#: is what a surface that only helps someone CHOOSE the tool needs, and it fits
#: the truncation budget by construction.
telemac_do_sag.__doc__ = render_docstring(**_DOC)
telemac_do_sag.routing_doc = render_docstring(**_DOC, view="routing")
