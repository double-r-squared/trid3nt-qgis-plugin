"""Engine template ``tomawac_wave_field`` - TOMAWAC spectral (phase-averaged) waves.

Four declarations and a chart: PARAMS, DATA, ``plan(p, d, ops)``, the ANSWER
fields, and the chart function beside them. Everything else - normalizing the
wire args, resolving the doors, walking the plan, persisting the products - is
the skeleton (``workflows/lib/workflow.py``); the wave mechanism is the TELEMAC
facade's open-water front (``steps/open_water.py`` + ``steps/wave.py``). See
``docs/design/declarative-workflows.md``.

THE QUESTION: how big do the waves get. TOMAWAC's third-generation wave-action
solver - the refinement-grade complement to SFINCS/SnapWave coastal screening -
over four question classes:

  * ``fetch_growth``    - fetch-limited wind-wave growth; Hs grows downwind, and
                          the upwind/downwind shore pair under the SAME storm is
                          what makes the answer checkable.
  * ``shoaling``        - an offshore swell steepens then depth-breaks up a beach.
  * ``bottom_friction`` - a shallow shelf dissipates wave energy.
  * ``wave_current``    - an opposing current amplifies Hs, a following one damps.

Two bed paths, chosen by where the AOI IS: a Great Lakes AOI samples the real
NOAA lake-datum bathymetry; anywhere else runs the geography-free idealized basin
that reproduces the official TOMAWAC verification physics, labeled as such.
"""

from __future__ import annotations

from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata, ResolutionSpec

from trid3nt_server.workflows.lib import (
    Forcing,
    FormGate,
    MeshPolicy,
    Param,
    Physics,
    Ref,
    doors,
    register_workflow,
)
from trid3nt_server.workflows.shared.aoi import location_or_bbox
from trid3nt_server.workflows.telemac.steps import compute_class
from trid3nt_server.workflows.telemac.wave_field.wave_mode import wave_mode
from trid3nt_server.workflows.telemac.workflow import TelemacWorkflow

__all__ = ["ANSWER", "DATA", "PARAMS", "build_fetch_chart", "plan",
           "tomawac_wave_field"]

#: A lake fetch runs ALONG the wind and is wider than it is tall, so a geocoded
#: place is squared off asymmetrically (~0.7 deg of longitude, ~0.4 of latitude).
#: A square box here would model a different fetch than the question asked about.
_LAKE_HALF_DEG = (0.7, 0.4)


PARAMS: tuple[Param, ...] = (
    # -- the question ------------------------------------------------------- #
    Param("location", door=doors.QUESTION, optional=True, consequence="aoi",
          desc="Lake or coastal place near the AOI (e.g. 'Lake Superior'), geocoded"),
    Param("bbox", door=doors.USER, optional=True, consequence="aoi",
          type=tuple[float, float, float, float] | list[float] | str,
          desc="Explicit AOI (min_lon,min_lat,max_lon,max_lat) EPSG:4326 - open water "
               "inside a lake for the real-bathymetry path"),
    Param("wave_mode", door=doors.QUESTION, default="fetch_growth",
          consequence="scenario",
          desc="Which wave question: fetch_growth (wind-wave growth across the fetch) "
               "| shoaling (swell steepens and depth-breaks) | bottom_friction (a "
               "shallow shelf dissipates energy) | wave_current (a current amplifies "
               "or damps the swell)"),

    # -- the storm ---------------------------------------------------------- #
    Param("wind_speed_mps", door=doors.SCENARIO, default=20.0, bounds=(0.0, 60.0),
          units="m/s", consequence="physics",
          desc="Sustained storm wind speed - a PRESCRIBED demo forcing, since no "
               "wave-forcing fetcher exists yet"),
    Param("wind_direction_deg", door=doors.SCENARIO, default=270.0,
          bounds=(0.0, 360.0), units="deg", consequence="physics",
          desc="Compass bearing the wind blows FROM (0=N, 90=E, 270=W); the fetch "
               "runs downwind of it"),
    Param("boundary_hs_m", door=doors.SCENARIO, default=1.5, bounds=(0.0, 20.0),
          units="m", consequence="scenario",
          desc="Incident swell significant wave height at the open boundary - the "
               "shoaling and wave_current question classes"),
    Param("boundary_period_s", door=doors.SCENARIO, default=10.0, bounds=(1.0, 30.0),
          units="s", consequence="scenario",
          desc="Incident swell peak period"),
    Param("current_speed_mps", door=doors.SCENARIO, default=-2.5,
          bounds=(-10.0, 10.0), units="m/s", consequence="scenario",
          desc="wave_current only - the current ramped across the domain; NEGATIVE "
               "opposes the swell (amplifies Hs), POSITIVE follows it (damps it)"),
    Param("bottom_friction", door=doors.USER, optional=True, type=bool,
          consequence="physics",
          derived_when_absent=(
              "bottom-friction dissipation arms itself for the bottom_friction "
              "question class and stays off for every other one"),
          desc="Force bottom-friction dissipation on or off"),

    # -- the domain --------------------------------------------------------- #
    Param("bathy_source", door=doors.SCENARIO, default="auto",
          consequence="physics",
          desc="Bed source: auto (a Great Lakes AOI samples the real NOAA lake-datum "
               "bathymetry, anywhere else runs the idealized basin) | noaa_greatlakes "
               "| idealized"),
    Param("target_resolution_m", door=doors.USER, optional=True, user_lever=True,
          bounds=(150.0, 20000.0), units="m", consequence="numerical",
          derived_when_absent=(
              "the grid is laid at the labeled default spacing - 2000 m over a real "
              "lake, 1500 m in the idealized basin"),
          desc="Explicit grid node spacing; 150 m is the finest the wave grid authors "
               "and a large lake is coarsened under the node budget"),

    # -- numerics (the advanced fold) --------------------------------------- #
    Param("sim_duration_hours", door=doors.SCENARIO, default=4.0, bounds=(1.0, 24.0),
          units="h", consequence="numerical",
          desc="Simulated storm duration - long enough for the sea to reach its "
               "fetch-limited steady state"),
    Param("compute_class", door=doors.CONSTANT, default="medium",
          consequence="numerical", desc="Solve sizing class"),
)


#: NO declared Data. The wave bed is sampled INSIDE the solver container from the
#: NOAA lake-datum grids, so there is no agent-side artifact to declare - which is
#: itself a queued item (the in-worker fetch migration), not a gap in this
#: declaration.
DATA = ()


def plan(p, d, ops):  # noqa: ANN001, ANN201 - the declared plan value, per the design doc
    """The spectral-wave recipe. Pure: constructs the plan value, executes nothing.

    The form gate comes FIRST because the storm is a PRESCRIBED value: the wind
    speed that sets the whole answer is a labeled default, and reviewing it after
    the solve would be reviewing a number that had already decided everything.
    """
    physics = Physics("wave_spectrum",
                      wave_mode=p.wave_mode,
                      wind_speed_mps=p.wind_speed_mps,
                      wind_direction_deg=p.wind_direction_deg,
                      boundary_hs_m=p.boundary_hs_m,
                      boundary_period_s=p.boundary_period_s,
                      current_speed_mps=p.current_speed_mps,
                      bottom_friction=p.bottom_friction,
                      sim_duration_hours=p.sim_duration_hours,
                      bathy_source=p.bathy_source)
    mesh = ops.build_mesh(Ref("aoi"),
                          MeshPolicy(resolution=None,
                                     target_edge_m=p.target_resolution_m))
    return [
        FormGate(title="Review the wave-field storm forcing"),
        *ops.acquire_domain(location=p.location, bbox=p.bbox, shape="open_water",
                            aoi_half_deg=_LAKE_HALF_DEG, aoi_name="aoi",
                            code_prefix="TOMAWAC"),
        ops.author(mesh=mesh, physics=physics, forcing=Forcing()),
        ops.solver_spec(compute_class=p.compute_class, physics=physics),
        ops.read_results(Ref("solve"), physics=physics, forcing=Forcing())
           .chart("wave_fetch_growth", builder=build_fetch_chart),
    ]


#: The run's ANSWER, as the numbers a reader has to be able to check.
ANSWER = ("hs_max_m", "hs_mean_m", "hs_upwind_m", "hs_downwind_m",
          "peak_period_max_s", "wave_mode", "wind_speed_mps", "mesh_size_m",
          "fetch_curve_km", "fetch_curve_hs_m")


def build_fetch_chart(*, result: Any, params: Any) -> dict[str, Any] | None:
    """The along-fetch growth chart SPEC: Hs against downwind distance.

    The curve is the WORKER's own measurement, carried out on the layer, so the
    chart and the narrated ``hs_downwind_m`` are the same numbers rather than two
    resamplings that nearly agree. ``None`` when the run measured no curve, which
    is the honest "there is nothing to plot" - a shoaling or wave-current run has
    no fetch axis to grow along.
    """
    xs = getattr(result, "fetch_curve_km", None)
    hs = getattr(result, "fetch_curve_hs_m", None)
    if not xs or not hs or len(xs) != len(hs):
        return None
    from trid3nt_server.tools.processing.charts_common import build_chart_payload

    upwind = getattr(result, "hs_upwind_m", None)
    downwind = getattr(result, "hs_downwind_m", None)
    wind = getattr(result, "wind_speed_mps", None)
    where = params.get("location")
    title = (f"Wave growth along the fetch - {where}" if where
             else (getattr(result, "name", None) or "Wave growth along the fetch"))
    return build_chart_payload(
        vega_lite_spec={
            "mark": {"type": "line", "point": False},
            "data": {"values": [{"x_km": float(xs[i]), "hs_m": float(hs[i])}
                                for i in range(len(xs))]},
            "encoding": {
                "x": {"field": "x_km", "type": "quantitative",
                      "title": "Downwind distance (km)"},
                "y": {"field": "hs_m", "type": "quantitative",
                      "title": "Significant wave height Hs (m)"},
            },
        },
        title=title,
        caption=(
            f"Fetch-limited growth under a prescribed {float(wind):.3g} m/s wind: "
            if wind is not None else "Fetch-limited growth: ")
        + (f"Hs {float(upwind):.3g} m at the upwind shore rising to "
           f"{float(downwind):.3g} m downwind. "
           if upwind is not None and downwind is not None else "")
        + "Spectral screening, not a calibrated hindcast.",
    )


_TOMAWAC_RES_SPEC = ResolutionSpec(
    param="target_resolution_m",
    unit="m",
    min_value=150.0,
    native_hint="NOAA Great Lakes lake-datum bathymetry (~90 m) / idealized grid",
    constraint_source="solver",
    rationale=(
        "target grid node spacing; GRID_H_FLOOR_M=150 m is the finest the wave "
        "grid authors, a large lake is coarsened under the GRID_NODE_CAP budget "
        "(self-labeled); a spectral screening field gains nothing finer"
    ),
)

_TOMAWAC_METADATA = AtomicToolMetadata(
    name="tomawac_wave_field",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="telemac",
    tier="template",
    resolution_specs=(_TOMAWAC_RES_SPEC,),
)


_DOC = dict(
    summary="The SPECTRAL WAVE FIELD (significant wave height Hs) a storm builds over a lake or coast.",
    routing=(
        "THE tool for \"how big do the waves get\", \"significant wave height\", \"wave "
        "field / sea state\", \"fetch-limited wave growth across the lake\", \"swell "
        "shoaling / breaking at the beach\", \"wave-current interaction\", \"wave energy "
        "dissipation on a shallow shelf\". TOMAWAC third-generation spectral wave-action "
        "solver - wind-wave generation (WAM cycle 4), shoaling/breaking, wave-current "
        "interaction, bottom friction. FOUR question classes via `wave_mode`: "
        "`fetch_growth` (default), `shoaling`, `bottom_friction`, `wave_current`. "
        "Produces an Hs field map + the along-fetch growth curve + the upwind/downwind "
        "shore pair. Supply a lake/coastal `location` OR a `bbox`."
    ),
    not_for=(
        "inundation DEPTH (`sfincs_flood` / `geoclaw_inundation`); coastal storm-tide "
        "flooding (`coastal_tidal_surge`); harbour agitation inside a breakwater "
        "(`artemis_harbor_agitation`); a river plume (`telemac_river_dye`)"
    ),
    params=PARAMS,
    controls=(
        ("input_mode",
         '"user_gated" presents the resolved storm forcing and bed source for '
         'review/edit before the solve and WAITS; "auto" (session default) proceeds '
         "with every assumption labeled. Not a physical value."),
        ("restart_clean",
         "True discards the ledger a PREVIOUS FAILED attempt at this same invocation "
         "left behind and re-runs every step from the top. Default False resumes at "
         "the failed step."),
    ),
    returns=(
        "On success a `TelemacWaveLayerURI` (a `LayerURI` subtype) - the emitter loads "
        "the Hs COG and animates the TOMAWAC SELAFIN sibling. It carries `hs_max_m` / "
        "`hs_mean_m` / `hs_upwind_m` / `hs_downwind_m` / `wave_mode`; narrate those "
        "typed numbers. On failure a dict with `status=\"error\"` + `error_code`."
    ),
)


tomawac_wave_field = register_workflow(
    TelemacWorkflow, _TOMAWAC_METADATA, PARAMS, plan,
    data=DATA,
    answer=ANSWER,
    provenance=(("wind_speed_mps", "wind_note"),
                ("bathy_source", "bathy_note")),
    coerce=(
        location_or_bbox("tomawac_wave_field", code_prefix="TOMAWAC",
                         hint="For a natural prompt like 'how big do the waves get "
                              "on <lake>', pass location='<lake>'."),
        wave_mode(),
        compute_class(),
    ),
    doc=_DOC,
)
