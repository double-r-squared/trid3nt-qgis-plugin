"""Engine template ``coastal_tidal_surge`` - TELEMAC-2D coastal tidal/surge
inundation.

Four declarations and a chart: PARAMS, DATA, ``plan(p, d, ops)``, the ANSWER
fields, and the chart function beside them. Everything else - normalizing the
wire args, resolving the doors, walking the plan, persisting the products - is
the skeleton (``workflows/lib/workflow.py``); the coastal mechanism is the
TELEMAC facade's open-water front (``workflows/telemac/steps/open_water.py`` +
``steps/coastal.py``). See ``docs/design/declarative-workflows.md``.

THE QUESTION: how far does an OBSERVED or PREDICTED coastal water-level series
FLOOD a stretch of coast. A regular UTM grid over a coastal AOI with real NOAA
DEM_all topobathy at the nodes, ONE seaward liquid boundary driven in time by a
NOAA CO-OPS series through the LIQUID BOUNDARIES FILE (SL(1)); SAINT-VENANT +
TIDAL FLATS wetting/drying floods the low coast as the boundary stage rises. The
discriminant: a storm-surge series (``series_type="observed"``) floods far more
land than the calm astronomical tide (``"prediction"``) over the SAME domain.
"""

from __future__ import annotations

from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata, ResolutionSpec

from trid3nt_server.workflows.lib import (
    Data,
    Fetch,
    Forcing,
    FormGate,
    MeshPolicy,
    Param,
    ParamRef,
    Physics,
    Ref,
    doors,
    register_workflow,
)
from trid3nt_server.workflows.shared.aoi import location_or_bbox
from trid3nt_server.workflows.telemac.coastal_tidal_surge.series_type import (
    series_type,
)
from trid3nt_server.workflows.telemac.steps import compute_class
from trid3nt_server.workflows.telemac.workflow import TelemacWorkflow

__all__ = ["ANSWER", "DATA", "PARAMS", "build_stage_chart", "coastal_tidal_surge",
           "plan"]

_SHARED = "trid3nt_server.workflows.shared"

#: The AOI half-width (deg) a geocoded coastal place is squared off to. ~0.06 deg
#: (~6 km) spans a shoreline with open water on one side and low land on the other,
#: which is the domain shape this question needs.
_COAST_HALF_DEG = 0.06


PARAMS: tuple[Param, ...] = (
    # -- the question ------------------------------------------------------- #
    Param("location", door=doors.QUESTION, optional=True, consequence="aoi",
          desc="Coastal place near the AOI, geocoded to a shoreline-spanning extent"),
    Param("bbox", door=doors.USER, optional=True, consequence="aoi",
          type=tuple[float, float, float, float] | list[float] | str,
          desc="Explicit AOI (min_lon,min_lat,max_lon,max_lat) EPSG:4326 spanning the "
               "shoreline - open water on one side, low land on the other"),
    Param("series_type", door=doors.QUESTION, default="observed",
          consequence="scenario",
          desc="Which water-level record drives the boundary: observed (the storm-surge "
               "record) | prediction (the astronomical tide, the calm-tide control that "
               "isolates the surge)"),

    # -- the gauge series --------------------------------------------------- #
    Param("station", door=doors.USER, optional=True, consequence="physics",
          derived_when_absent=(
              "the CO-OPS station nearest the AOI centre drives the boundary"),
          desc="NOAA CO-OPS station id (e.g. '8728690'); unset uses the nearest "
               "in-AOI gauge"),
    Param("start_date", door=doors.QUESTION, optional=True, consequence="scenario",
          desc="ISO YYYY-MM-DD start of the gauge window - the storm the question "
               "is about"),
    Param("end_date", door=doors.QUESTION, optional=True, consequence="scenario",
          desc="ISO YYYY-MM-DD end of the gauge window"),
    Param("datum_offset_m", door=doors.SCENARIO, default=0.0, bounds=(-10.0, 10.0),
          units="m", consequence="physics",
          desc="Metres ADDED to every series value to reconcile the tide datum (MLLW) "
               "with the DEM datum (DEM_all ~ MSL); 0 uses the series as reported"),

    # -- the domain --------------------------------------------------------- #
    Param("target_resolution_m", door=doors.USER, optional=True, user_lever=True,
          bounds=(20.0, 5000.0), units="m", consequence="numerical",
          derived_when_absent="the grid is laid at the labeled 180 m default spacing",
          desc="Explicit grid node spacing; the coastal grid floor is 20 m and a wide "
               "AOI is coarsened under the node budget"),
    Param("ocean_edge", door=doors.USER, optional=True, consequence="numerical",
          derived_when_absent=(
              "the seaward boundary is placed on the DEEPEST-mean bbox edge"),
          desc="Which bbox edge carries the seaward liquid boundary: N | S | E | W; "
               "unset picks the deepest edge"),
    Param("bathy_source", door=doors.SCENARIO, default="noaa_demall",
          consequence="physics",
          desc="Bed source: noaa_demall (real topobathy) | synthetic (an analytic "
               "plane beach - the deterministic offline path, not a real coast)"),

    # -- numerics (the advanced fold) --------------------------------------- #
    Param("duration_hours", door=doors.USER, optional=True, bounds=(0.1, 720.0),
          units="h", consequence="numerical",
          derived_when_absent="the simulated window is the fetched series' own span",
          desc="Simulated window; unset runs the whole gauge series"),
    Param("time_step_s", door=doors.CONSTANT, default=20.0, bounds=(1.0, 600.0),
          units="s", consequence="numerical", desc="Solver time step"),
    Param("output_interval_min", door=doors.USER, optional=True, bounds=(0.1, 1440.0),
          units="min", consequence="numerical",
          desc="Result-writing cadence; unset keeps the deck's own graphic period"),
    Param("compute_class", door=doors.CONSTANT, default="medium",
          consequence="numerical", desc="Solve sizing class"),
)


#: The boundary FORCING - the gauge record, fetched fresh over the domain the AOI
#: step binds. Reference data: a water-level record is the world's, never BYO'd.
#: It reads the DOMAIN for where to look and the params for which series, which
#: station and which window.
DATA = (
    Data("tides", Fetch.tool(f"{_SHARED}.tide_series.resolve_tide_series",
                             series_type=ParamRef("series_type"),
                             station=ParamRef("station"),
                             start_date=ParamRef("start_date"),
                             end_date=ParamRef("end_date"))),
)


def plan(p, d, ops):  # noqa: ANN001, ANN201 - the declared plan value, per the design doc
    """The coastal tidal/surge recipe. Pure: constructs the plan value, executes nothing.

    The form gate comes FIRST so the fetch and the solve both run on the approved
    sheet: the window and the datum offset are exactly the values a reviewer would
    want to change, and a series fetched before the review would have been fetched
    for the window the review replaced.
    """
    physics = Physics("coastal_surge",
                      datum_offset_m=p.datum_offset_m, ocean_edge=p.ocean_edge,
                      duration_hours=p.duration_hours, time_step_s=p.time_step_s,
                      bathy_source=p.bathy_source,
                      output_interval_min=p.output_interval_min)
    forcing = Forcing(water_level=d.tides)
    mesh = ops.build_mesh(Ref("aoi"),
                          MeshPolicy(resolution=None,
                                     target_edge_m=p.target_resolution_m))
    return [
        FormGate(title="Review the coastal tide/surge scenario"),
        *ops.acquire_domain(location=p.location, bbox=p.bbox, shape="open_water",
                            aoi_half_deg=_COAST_HALF_DEG, aoi_name="coast",
                            code_prefix="COASTAL"),
        ops.author(mesh=mesh, physics=physics, forcing=forcing),
        ops.solver_spec(compute_class=p.compute_class, physics=physics),
        ops.read_results(Ref("solve"), physics=physics, forcing=forcing)
           .chart("coastal_stage_vs_inundation", builder=build_stage_chart),
    ]


#: The run's ANSWER, as the numbers a reader has to be able to check. Persisted
#: beside the chart spec so verification cites the run's own figures rather than
#: recomputing them from the raster.
ANSWER = ("peak_depth_m", "flooded_land_km2", "wet_area_km2", "peak_wl_m",
          "sl_peak_m", "series_type", "series_datum", "datum_offset_m",
          "station_id", "station_name", "ocean_edge", "mesh_size_m")


def build_stage_chart(*, result: Any, params: Any) -> dict[str, Any] | None:
    """The storm-tide chart SPEC: the boundary crest against what it flooded.

    Three measured bars, all off the published layer - the peak boundary stage the
    run was DRIVEN with, the peak free-surface level it REACHED, and the deepest
    inundation it produced - so the reader can see the forcing and the response in
    the same frame. ``None`` when the run measured no boundary stage, which is the
    honest "there is no chart to draw".
    """
    sl_peak = getattr(result, "sl_peak_m", None)
    peak_depth = getattr(result, "peak_depth_m", None)
    if sl_peak is None or peak_depth is None:
        return None
    from trid3nt_server.tools.processing.charts_common import build_chart_payload

    peak_wl = getattr(result, "peak_wl_m", None)
    bars = [{"quantity": "Boundary stage (peak)", "m": float(sl_peak)}]
    if peak_wl is not None:
        bars.append({"quantity": "Water level (peak)", "m": float(peak_wl)})
    bars.append({"quantity": "Inundation depth (peak)", "m": float(peak_depth)})

    kind = str(getattr(result, "series_type", None) or "observed")
    datum = getattr(result, "series_datum", None) or "MLLW"
    flooded = getattr(result, "flooded_land_km2", None)
    # With no location words the LAYER's own name is the title: it already reads
    # "Peak inundation depth (<coast>)", so prefixing it would say it twice.
    where = params.get("location")
    title = (f"Coastal {kind} tide - {where}" if where
             else (getattr(result, "name", None) or f"Coastal {kind} tide"))
    return build_chart_payload(
        vega_lite_spec={
            "mark": {"type": "bar"},
            "data": {"values": bars},
            "encoding": {
                "y": {"field": "quantity", "type": "nominal", "title": None,
                      "sort": None},
                "x": {"field": "m", "type": "quantitative", "title": "Metres"},
            },
        },
        title=title,
        caption=(
            f"Driven by the {kind} CO-OPS series ({datum} datum): peak boundary "
            f"stage {float(sl_peak):.3g} m, deepest inundation "
            f"{float(peak_depth):.3g} m"
            + (f", {float(flooded):.3g} km2 of land newly flooded"
               if flooded is not None else "")
            + ". Planning-grade screening, not a calibrated hindcast."
        ),
    )


_COASTAL_RES_SPEC = ResolutionSpec(
    param="target_resolution_m",
    unit="m",
    min_value=20.0,
    native_hint="NOAA DEM_all topobathy (~30-90 m coastal) / grid node spacing",
    constraint_source="solver",
    rationale=(
        "target grid node spacing; the coastal grid floor is 20 m, a wide bbox is "
        "coarsened under the node budget (self-labeled); a planning-grade "
        "inundation screening field gains nothing finer than the topobathy"
    ),
)

_COASTAL_METADATA = AtomicToolMetadata(
    name="coastal_tidal_surge",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="telemac",
    tier="template",
    resolution_specs=(_COASTAL_RES_SPEC,),
)


_DOC = dict(
    summary="How far an OBSERVED or PREDICTED coastal water-level series FLOODS this coast.",
    routing=(
        "THE tool for \"how far does the storm surge flood inland\", \"map the coastal "
        "inundation from this tide-gauge record\", \"which low land does the storm tide "
        "reach\", \"surge vs calm-tide flooded area at this coast\". TELEMAC-2D shallow "
        "water with TIDAL FLATS wetting/drying over real NOAA DEM_all topobathy, one "
        "seaward liquid boundary driven in time by a NOAA CO-OPS series. TWO question "
        "classes via `series_type`: `observed` (the storm-surge record floods the low "
        "coast) and `prediction` (the astronomical tide over the SAME domain - the "
        "control isolating the surge). Produces a peak-inundation-DEPTH map + the "
        "newly-flooded land area. Supply a coastal `location` OR a `bbox` spanning "
        "the shoreline."
    ),
    not_for=(
        "a spectral WAVE-HEIGHT field (`tomawac_wave_field`); harbour agitation "
        "(`artemis_harbor_agitation`); a river dye/contaminant plume "
        "(`telemac_river_dye`); regional compound-flood screening (`sfincs_flood`)"
    ),
    params=PARAMS,
    controls=(
        ("input_mode",
         '"user_gated" presents the resolved window, station and datum offset for '
         'review/edit before the solve and WAITS; "auto" (session default) proceeds '
         "with every assumption labeled. Not a physical value."),
        ("restart_clean",
         "True discards the ledger a PREVIOUS FAILED attempt at this same invocation "
         "left behind and re-runs every step from the top. Default False resumes at "
         "the failed step. A run that completed is marked complete and is never "
         "replayed, so a fresh invocation always re-solves against live upstream data."),
    ),
    returns=(
        "On success a `TelemacCoastalLayerURI` (a `LayerURI` subtype) - the emitter "
        "loads the peak-inundation-depth COG and animates the coastal SELAFIN sibling. "
        "It carries `peak_depth_m` / `flooded_land_km2` / `wet_area_km2` / `sl_peak_m` "
        "/ `series_type`; narrate those typed numbers. On failure a dict with "
        "`status=\"error\"` + `error_code`."
    ),
)


coastal_tidal_surge = register_workflow(
    TelemacWorkflow, _COASTAL_METADATA, PARAMS, plan,
    data=DATA,
    answer=ANSWER,
    provenance=(("datum_offset_m", "datum_offset_note"),
                ("series_type", "series_type_note")),
    coerce=(
        location_or_bbox("coastal_tidal_surge", code_prefix="COASTAL",
                         hint="For a natural prompt like 'storm surge flooding near "
                              "<place>', pass location='<place>'."),
        series_type(),
        compute_class(),
    ),
    doc=_DOC,
)
