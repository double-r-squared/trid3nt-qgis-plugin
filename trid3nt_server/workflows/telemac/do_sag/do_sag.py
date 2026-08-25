"""Engine template ``telemac_do_sag`` - TELEMAC-2D WAQTEL dissolved-oxygen sag.

Four declarations and a chart: PARAMS, DATA, ``plan(p, d, ops)``, the ANSWER
fields, and the chart function beside them. Everything else - normalizing the
wire args, resolving the doors, walking the plan, persisting the products - is
the skeleton (``workflows/lib/workflow.py``); the reach mechanism is the TELEMAC
facade (``workflows/telemac/workflow.py``). See
``docs/design/declarative-workflows.md``.

THE QUESTION: the DISSOLVED-OXYGEN SAG below a permitted discharge / WWTP outfall
in a river reach (the US TMDL / Clean Water Act permit question). Where does DO
bottom out downstream, and does it VIOLATE the water-quality standard? TELEMAC-2D
+ WAQTEL O2 - the Streeter-Phelps oxygen sag - over a real NHDPlus reach.
"""

from __future__ import annotations

from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata, ResolutionSpec

from trid3nt_server.workflows.lib import (
    Data,
    DrawGate,
    Fetch,
    Forcing,
    MeshPolicy,
    Param,
    Physics,
    Ref,
    RunMode,
    doors,
    register_workflow,
)
from trid3nt_server.workflows.shared.aoi import location_or_bbox
from trid3nt_server.workflows.telemac.steps import (
    ReviewResolvedInputs,
    WaqtelO2,
    event_time,
    lonlat_point,
)
from trid3nt_server.workflows.telemac.workflow import CorridorPolicy, TelemacWorkflow

__all__ = ["ANSWER", "DATA", "PARAMS", "build_sag_chart", "plan", "telemac_do_sag"]

_STEPS = "trid3nt_server.workflows.telemac.steps"


PARAMS: tuple[Param, ...] = (
    Param("location", door=doors.QUESTION, optional=True, consequence="aoi",
          desc="Place name near the discharge, geocoded to the reach"),
    Param("bbox", door=doors.USER, optional=True, consequence="aoi",
          type=tuple[float, float, float, float] | list[float] | str,
          desc="Explicit AOI (min_lon,min_lat,max_lon,max_lat) EPSG:4326, instead of a place"),
    Param("outfall_coords", door=doors.USER, optional=True, consequence="scenario",
          user_lever=True, type=tuple[float, float] | list[float],
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

    Param("do_saturation_mgl", door=doors.DERIVED,
          resolve=f"{_STEPS}.water_quality.do_saturation_mgl",
          user_lever=True, bounds=(0.0, 20.0), units="mg/L", consequence="scenario",
          desc="DO saturation Cs; derived from water temperature unless supplied"),
    Param("upstream_do_mgl", door=doors.DERIVED,
          resolve=f"{_STEPS}.water_quality.upstream_do_mgl",
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
    Param("event_time", door=doors.QUESTION, optional=True, consequence="scenario",
          derived_when_absent=(
              "the carrier discharge is read at the MOST RECENT published NWM "
              "cycle"),
          desc="The storm/event moment to read the carrier discharge cycle at - "
               "from phrasing like 'during last Tuesday's storm'; an ISO date "
               "or datetime (e.g. '2026-08-20' or '2026-08-20T06:00:00Z'). "
               "Unset reads the most recent published NWM cycle. The NWM PDS "
               "bucket retains only the last ~30 days of history; a deeper "
               "request refuses typed rather than silently reading a "
               "different cycle."),
    Param("compute_class", door=doors.CONSTANT, default="medium",
          consequence="numerical", desc="Solve sizing class"),
)


#: The reach's REFERENCE data - fetched fresh for the domain the geocode step
#: binds, never BYO. The carrier discharge is a STEP rather than Data: it reads
#: the resolved mid-reach seed, which is a step result and not something a
#: producer declaration can name.
DATA = (
    Data("rivers", Fetch.tool(f"{_STEPS}.reach.fetch_reach_flowline", prefetched=None)),
)


def plan(p, d, ops):  # noqa: ANN001, ANN201 - the declared plan value, per the design doc
    """The DO-sag recipe. Pure: constructs the plan value, executes nothing."""
    physics = Physics("waqtel_o2", do_sag_config=Ref("waqtel"),
                      reach_seed_coords=p.outfall_coords,
                      sim_duration_s=p.sim_duration_s)
    forcing = Forcing(carrier=Ref("reviewed_discharge"))
    mesh = ops.build_mesh(
        Ref("reach"),
        MeshPolicy(resolution=p.mesh_resolution, target_edge_m=p.mesh_resolution_m),
        corridor=CorridorPolicy(extent_km=p.reach_length_km,
                                width_m=p.channel_width_m,
                                boundary_source=p.bank_source))
    return [
        DrawGate(param="outfall_coords", geometry="point",
                 prompt="Click where the discharge enters the river"),
        *ops.acquire_domain(location=p.location, bbox=p.bbox, rivers=d.rivers,
                            discharge=p.discharge_m3s, event_time=p.event_time),
        WaqtelO2(discharge_bod_mgl=p.discharge_bod_mgl,
                 upstream_do_mgl=p.upstream_do_mgl,
                 do_saturation_mgl=p.do_saturation_mgl,
                 water_temp_c=p.water_temp_c, k1_per_day=p.k1_per_day,
                 k2_per_day=p.k2_per_day,
                 do_standard_mgl=p.do_standard_mgl).named("waqtel"),
        ReviewResolvedInputs(carrier_discharge=Ref("carrier_discharge"),
                             bank_source=p.bank_source, workflow=ops.name,
                             input_mode=RunMode).named("reviewed_discharge"),
        ops.author(mesh=mesh, physics=physics, forcing=forcing),
        ops.solver_spec(compute_class=p.compute_class),
        ops.read_results(Ref("solve"), physics=physics, forcing=forcing)
           .chart("do_sag_curve", builder=build_sag_chart),
    ]


#: The run's ANSWER, as the numbers a reader has to be able to check. Persisted
#: beside the chart spec so verification cites the run's own figures rather than
#: recomputing them from the raster.
ANSWER = ("do_min_mgl", "do_min_distance_m", "do_standard_mgl",
          "do_violates_standard", "do_upstream_mgl", "do_saturation_mgl",
          "bod_upstream_mgl", "sag_curve_distance_m", "sag_curve_do_mgl",
          "sag_curve_bod_mgl", "mesh_size_m")


def build_sag_chart(*, result: Any, params: Any) -> dict[str, Any] | None:
    """The DO-sag chart SPEC: DO + CBOD vs downstream distance, standard as a rule.

    Honest postprocess scalars off the published layer (the binned centerline
    curve), never a fabricated line; ``None`` when the curve is absent.
    """
    xs = getattr(result, "sag_curve_distance_m", None)
    do = getattr(result, "sag_curve_do_mgl", None)
    bod = getattr(result, "sag_curve_bod_mgl", None)
    if not xs or not do or len(xs) != len(do):
        return None
    std = float(getattr(result, "do_standard_mgl", None) or 5.0)

    from trid3nt_server.tools.processing.charts_common import build_chart_payload

    do_vals = [{"x_km": round(xs[i] / 1000.0, 4), "v": do[i], "series": "Dissolved O2"}
               for i in range(len(xs))]
    bod_vals = ([{"x_km": round(xs[i] / 1000.0, 4), "v": bod[i], "series": "CBOD"}
                 for i in range(len(xs))] if bod and len(bod) == len(xs) else [])
    vega_lite_spec = {
        "layer": [
            {"mark": {"type": "line", "point": False},
             "data": {"values": do_vals + bod_vals},
             "encoding": {
                 "x": {"field": "x_km", "type": "quantitative",
                       "title": "Downstream distance (km)"},
                 "y": {"field": "v", "type": "quantitative",
                       "title": "Concentration (mg/L)"},
                 "color": {"field": "series", "type": "nominal", "title": None}}},
            {"mark": {"type": "rule", "strokeDash": [6, 4], "color": "#c0392b"},
             "data": {"values": [{"y": std}]},
             "encoding": {"y": {"field": "y", "type": "quantitative"}}},
        ]
    }
    dmin = getattr(result, "do_min_mgl", None)
    dloc = getattr(result, "do_min_distance_m", None)
    verdict = "violates" if getattr(result, "do_violates_standard", False) else "meets"
    # With no location words the LAYER's own name is the title: it already reads
    # "Dissolved oxygen sag (<reach>)", so prefixing it would say it twice.
    where = params.get("location")
    title = (f"Dissolved-oxygen sag - {where}" if where
             else (getattr(result, "name", None) or "Dissolved-oxygen sag"))
    return build_chart_payload(
        vega_lite_spec=vega_lite_spec,
        title=title,
        caption=(
            f"Streeter-Phelps DO sag: minimum {dmin} mg/L at {dloc} m downstream "
            f"({verdict} the {std:g} mg/L standard, dashed). CBOD decay drives the "
            f"sag; reaeration recovers it. Screening/permit grade."
        ),
    )


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


telemac_do_sag = register_workflow(
    TelemacWorkflow, _TELEMAC_DO_SAG_METADATA, PARAMS, plan,
    data=DATA,
    answer=ANSWER,
    # The mesh row is present only when a sizing rule MOVED the user's explicit
    # edge length; on an honoured (or absent) override both fields read null.
    provenance=(("discharge_m3s", "discharge_note"),
                ("mesh_resolution_m", "mesh_resolution_note")),
    coerce=(
        location_or_bbox("telemac_do_sag", code_prefix="TELEMAC"),
        lonlat_point("outfall_coords", label="outfall_coords"),
        event_time(),
    ),
    doc=_DOC,
)
