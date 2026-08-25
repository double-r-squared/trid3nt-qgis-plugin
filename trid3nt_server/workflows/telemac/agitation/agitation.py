"""Engine template ``artemis_harbor_agitation`` - ARTEMIS harbour wave agitation.

Four declarations and a chart: PARAMS, DATA, ``plan(p, d, ops)``, the ANSWER
fields, and the chart function beside them. Everything else - normalizing the
wire args, resolving the doors, walking the plan, persisting the products - is
the skeleton (``workflows/lib/workflow.py``); the agitation mechanism is the
TELEMAC facade's open-water front (``steps/open_water.py`` +
``steps/agitation.py``). See ``docs/design/declarative-workflows.md``.

THE QUESTION: how much does swell amplify inside a harbour, and does the
breakwater shelter the berths. ARTEMIS is the phase-RESOLVING elliptic mild-slope
(Berkhoff) solver - the complement to TOMAWAC's phase-AVERAGED spectral tier -
so the answer is a steady-state agitation coefficient Kd = Hs/H0 in which
diffraction fringes, standing waves and resonance are visible rather than
averaged away. Three question classes:

  * ``diffraction`` - a breakwater shelters a berthing area, and on a real Great
                      Lakes harbour the ACTUAL surveyed structure is meshed.
  * ``resonance``   - a narrow-mouth basin amplifies swell at its seiche periods.
  * ``shoal``       - a nearshore reef refracts and FOCUSES waves down-wave.
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
from trid3nt_server.workflows.telemac.agitation.agitation_mode import agitation_mode
from trid3nt_server.workflows.telemac.steps import compute_class
from trid3nt_server.workflows.telemac.workflow import TelemacWorkflow

__all__ = ["ANSWER", "DATA", "PARAMS", "artemis_harbor_agitation",
           "build_agitation_chart", "plan"]

#: A harbour approach is small: ~0.06 deg (~6 km) around a geocoded quay is the
#: open-water box the sheltering question lives in.
_HARBOR_HALF_DEG = 0.06


PARAMS: tuple[Param, ...] = (
    # -- the question ------------------------------------------------------- #
    Param("location", door=doors.QUESTION, optional=True, consequence="aoi",
          desc="Harbour or coastal place near the AOI (e.g. 'Marquette, Michigan'), "
               "geocoded"),
    Param("bbox", door=doors.USER, optional=True, consequence="aoi",
          type=tuple[float, float, float, float] | list[float] | str,
          desc="Explicit AOI (min_lon,min_lat,max_lon,max_lat) EPSG:4326 - the "
               "open-water harbour approach for the real-bathymetry path"),
    Param("wave_mode", door=doors.QUESTION, default="diffraction",
          consequence="scenario",
          desc="Which agitation question: diffraction (a breakwater shelters the "
               "berths) | resonance (a narrow-mouth basin rings at its seiche "
               "periods) | shoal (a reef refracts and focuses waves)"),

    # -- the incident wave -------------------------------------------------- #
    Param("wave_period_s", door=doors.SCENARIO, default=8.0, bounds=(1.0, 300.0),
          units="s", consequence="physics",
          desc="Incident monochromatic wave period - a PRESCRIBED demo forcing, "
               "since no wave-forcing fetcher exists yet"),
    Param("wave_height_m", door=doors.SCENARIO, default=1.0, bounds=(0.01, 10.0),
          units="m", consequence="physics",
          desc="Incident wave height H0 at the open boundary; Kd is measured "
               "against it, so it sets the scale of every narrated height"),
    Param("wave_direction_deg", door=doors.SCENARIO, default=90.0,
          bounds=(0.0, 360.0), units="deg", consequence="scenario",
          desc="Incident wave direction in the TRIG convention (0 = +X east, "
               "90 = +Y north) - not the compass bearing"),
    Param("reflection_coef", door=doors.SCENARIO, default=1.0, bounds=(0.0, 1.0),
          consequence="physics",
          desc="Structure / quay-wall reflection coefficient: 1 fully reflecting "
               "(a vertical quay), 0 fully absorbing (a rubble slope)"),

    # -- the structure ------------------------------------------------------ #
    Param("breakwater", door=doors.USER, optional=True, consequence="scenario",
          type=tuple[float, float, float, float] | list[float],
          derived_when_absent=(
              "the ACTUAL surveyed breakwater is fetched from OpenStreetMap and "
              "meshed as a thin solid barrier; if OSM has none, a LABELED "
              "schematic segment stands in and says so"),
          desc="Pin the barrier as a segment (lon0, lat0, lon1, lat1) EPSG:4326; "
               "supplying it suppresses the OSM lookup"),

    # -- the domain --------------------------------------------------------- #
    Param("bathy_source", door=doors.SCENARIO, default="auto",
          consequence="physics",
          desc="Bed source: auto (a Great Lakes DIFFRACTION AOI samples the real "
               "NOAA lake-datum bathymetry, everything else runs the analytic "
               "domain) | noaa_greatlakes | idealized"),
    Param("target_resolution_m", door=doors.USER, optional=True, user_lever=True,
          bounds=(20.0, 2000.0), units="m", consequence="numerical",
          derived_when_absent=(
              "the grid is laid at the labeled default spacing - 40 m over a real "
              "harbour, 8 m in the analytic domain"),
          desc="Explicit grid node spacing; a phase-resolving solve needs several "
               "nodes per WAVELENGTH, so this is much finer than a spectral run"),
    Param("compute_class", door=doors.CONSTANT, default="medium",
          consequence="numerical", desc="Solve sizing class"),
)


#: NO declared Data. The harbour bed is sampled INSIDE the solver container and
#: the surveyed structure is a bare-Overpass way-geometry fetch the deck makes
#: only when it is going to mesh one - declaring it would fetch OSM for every
#: analytic run too. Both are on the in-worker-fetch migration queue.
DATA = ()


def plan(p, d, ops):  # noqa: ANN001, ANN201 - the declared plan value, per the design doc
    """The harbour-agitation recipe. Pure: constructs the plan value, executes nothing.

    The form gate comes FIRST because the incident wave is PRESCRIBED: Kd is a
    ratio against H0, so the height and period on that card scale every number the
    run goes on to produce.
    """
    physics = Physics("harbor_agitation",
                      wave_mode=p.wave_mode,
                      wave_period_s=p.wave_period_s,
                      wave_direction_deg=p.wave_direction_deg,
                      wave_height_m=p.wave_height_m,
                      reflection_coef=p.reflection_coef,
                      breakwater=p.breakwater,
                      bathy_source=p.bathy_source)
    mesh = ops.build_mesh(Ref("aoi"),
                          MeshPolicy(resolution=None,
                                     target_edge_m=p.target_resolution_m))
    return [
        FormGate(title="Review the incident wave and the structure"),
        *ops.acquire_domain(location=p.location, bbox=p.bbox, shape="open_water",
                            aoi_half_deg=_HARBOR_HALF_DEG, aoi_name="aoi",
                            code_prefix="ARTEMIS"),
        ops.author(mesh=mesh, physics=physics, forcing=Forcing()),
        ops.solver_spec(compute_class=p.compute_class, physics=physics),
        ops.read_results(Ref("solve"), physics=physics, forcing=Forcing())
           .chart("harbor_agitation", builder=build_agitation_chart),
    ]


#: The run's ANSWER, as the numbers a reader has to be able to check.
ANSWER = ("kd_max", "hs_max_m", "kd_sheltered", "kd_exposed", "resonant_period_s",
          "response_at_resonance", "response_off_resonance", "wave_mode",
          "wave_period_s", "mesh_size_m", "agitation_curve_m",
          "agitation_curve_kd", "agitation_curve_kind")


#: What the curve's axes ARE, per question class. Each mode sweeps a different
#: independent variable, so a single axis label would be wrong for two of the
#: three: a resonance run plots amplification against incident PERIOD, not Kd
#: against distance.
_CURVE_AXIS: dict[str, tuple[str, str]] = {
    "diffraction_transect": ("Distance along the transect (m)",
                             "Agitation coefficient Kd = Hs/H0"),
    "resonance_sweep": ("Incident wave period (s)",
                        "Basin response (amplification of H0)"),
    "shoal_axis_transect": ("Distance along the shoal axis (m)",
                            "Agitation coefficient Kd = Hs/H0"),
}
_CURVE_AXIS_DEFAULT = ("Along the measured curve",
                       "Agitation coefficient Kd = Hs/H0")


def build_agitation_chart(*, result: Any, params: Any) -> dict[str, Any] | None:
    """The agitation chart SPEC: Kd across the field, as the worker measured it.

    The curve is the RUN's own, carried on the layer, so the chart and the narrated
    sheltered/exposed pair are one measurement rather than two resamplings that
    nearly agree. ``None`` when the run measured no curve - which is the honest
    "there is nothing to plot".
    """
    xs = getattr(result, "agitation_curve_m", None)
    kd = getattr(result, "agitation_curve_kd", None)
    if not xs or not kd or len(xs) != len(kd):
        return None
    from trid3nt_server.tools.processing.charts_common import build_chart_payload

    kind = str(getattr(result, "agitation_curve_kind", None) or "transect")
    x_title, y_title = _CURVE_AXIS.get(kind, _CURVE_AXIS_DEFAULT)
    sheltered = getattr(result, "kd_sheltered", None)
    exposed = getattr(result, "kd_exposed", None)
    period = getattr(result, "wave_period_s", None)
    where = params.get("location")
    title = (f"Harbour agitation Kd - {where}" if where
             else (getattr(result, "name", None) or "Harbour agitation Kd"))
    return build_chart_payload(
        vega_lite_spec={
            "mark": {"type": "line", "point": False},
            "data": {"values": [{"x": float(xs[i]), "kd": float(kd[i])}
                                for i in range(len(xs))]},
            "encoding": {
                "x": {"field": "x", "type": "quantitative", "title": x_title},
                "y": {"field": "kd", "type": "quantitative", "title": y_title},
            },
        },
        title=title,
        caption=(
            (f"Forced by a prescribed {float(period):.3g} s incident wave: "
             if period is not None else "")
            + (f"Kd {float(exposed):.3g} on the exposed approach against "
               f"{float(sheltered):.3g} in the lee - the structure sheltered the "
               f"berths by a factor of {float(exposed) / float(sheltered):.3g}. "
               if sheltered and exposed else "")
            + "Phase-resolving screening, not a calibrated hindcast."
        ),
    )


_ARTEMIS_RES_SPEC = ResolutionSpec(
    param="target_resolution_m",
    unit="m",
    min_value=20.0,
    native_hint="NOAA Great Lakes lake-datum bathymetry (~90 m) / analytic domain",
    constraint_source="solver",
    rationale=(
        "target grid node spacing; a phase-resolving elliptic solve needs several "
        "nodes per wavelength, so 20 m is the floor the harbour grid authors and a "
        "wide AOI is coarsened under the node budget (self-labeled)"
    ),
)

_ARTEMIS_METADATA = AtomicToolMetadata(
    name="artemis_harbor_agitation",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="telemac",
    tier="template",
    resolution_specs=(_ARTEMIS_RES_SPEC,),
)


_DOC = dict(
    summary="The WAVE AGITATION (Kd = Hs/H0) inside a harbour or around a coastal structure.",
    routing=(
        "THE tool for \"how much does swell amplify inside this harbour\", \"wave "
        "agitation / tranquility in the basin\", \"does this breakwater shelter the "
        "berths\", \"harbour resonance / seiche\", \"diffraction behind a breakwater\", "
        "\"reef/shoal wave sheltering or focusing\". ARTEMIS phase-RESOLVING elliptic "
        "mild-slope (Berkhoff) - diffraction fringes, standing waves and resonance are "
        "the answer, not an average. THREE question classes via `wave_mode`: "
        "`diffraction` (default; on a real Great Lakes harbour the ACTUAL surveyed "
        "OSM breakwater is meshed), `resonance`, `shoal`. Returns a dimensionless "
        "agitation field. Supply a harbour `location` OR a `bbox`."
    ),
    not_for=(
        "the offshore SEA STATE or fetch-limited wind-wave growth "
        "(`tomawac_wave_field`, the phase-averaged tier); coastal storm-tide "
        "flooding (`coastal_tidal_surge`); inundation DEPTH (`sfincs_flood`); a "
        "river plume (`telemac_river_dye`)"
    ),
    params=PARAMS,
    controls=(
        ("input_mode",
         '"user_gated" presents the resolved incident wave and the structure for '
         'review/edit before the solve and WAITS; "auto" (session default) proceeds '
         "with every assumption labeled. Not a physical value."),
        ("restart_clean",
         "True discards the ledger a PREVIOUS FAILED attempt at this same invocation "
         "left behind and re-runs every step from the top. Default False resumes at "
         "the failed step."),
    ),
    returns=(
        "On success an `ArtemisAgitationLayerURI` (a `LayerURI` subtype) - the emitter "
        "loads the Kd COG and animates the ARTEMIS SELAFIN sibling. It carries "
        "`kd_max` / `kd_sheltered` / `kd_exposed` / `resonant_period_s` / "
        "`response_at_resonance` / `wave_mode`; narrate those typed numbers. On "
        "failure a dict with `status=\"error\"` + `error_code`."
    ),
)


artemis_harbor_agitation = register_workflow(
    TelemacWorkflow, _ARTEMIS_METADATA, PARAMS, plan,
    data=DATA,
    answer=ANSWER,
    provenance=(("wave_period_s", "wave_period_note"),
                ("breakwater", "breakwater_note"),
                ("target_resolution_m", "target_resolution_note")),
    coerce=(
        location_or_bbox("artemis_harbor_agitation", code_prefix="ARTEMIS",
                         hint="For a natural prompt like 'is the marina at <place> "
                              "sheltered', pass location='<place>'."),
        agitation_mode(),
        compute_class(),
    ),
    doc=_DOC,
)
