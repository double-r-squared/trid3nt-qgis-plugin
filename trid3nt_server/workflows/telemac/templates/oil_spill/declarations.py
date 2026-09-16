"""The CONTRACT of ``telemac_oil_spill``: what only an oil question asks."""

from __future__ import annotations

from typing import Any

from trid3nt_server.inputs import Point
from trid3nt_server.workflows.runtime import Accepts, Param, doors

__all__ = ["ACCEPTS", "DOC", "OIL_PRESETS", "PARAMS"]

#: The oil presets the deck carries, in the module reader's own terms: the
#: (mass fraction, boiling point) components, the aromatic rows with their
#: solubility and dissolution and volatilisation rates, then the density,
#: viscosity, spreading volume, ambient temperature and spreading law. The
#: fractions sum to one per preset.
OIL_PRESETS: dict[str, dict[str, Any]] = {
    "light_crude": dict(
        compo=[(0.5, 645.0), (0.3, 830.0)],
        hap=[(0.2, 673.0, 0.018, 1.0e-5, 5.0e-5)],
        rho=850.0, eta=1.0e-5, voldev=20.0, tamb=288.0, etal=1),
    "diesel": dict(
        compo=[(0.6, 560.0), (0.25, 700.0)],
        hap=[(0.15, 610.0, 0.005, 1.0e-5, 8.0e-5)],
        rho=840.0, eta=4.0e-6, voldev=10.0, tamb=288.0, etal=1),
    "heavy_fuel": dict(
        compo=[(0.75, 900.0), (0.2, 1050.0)],
        hap=[(0.05, 800.0, 0.001, 5.0e-6, 1.0e-5)],
        rho=960.0, eta=5.0e-4, voldev=30.0, tamb=288.0, etal=1),
}

#: What an oil run can be HANDED. TELEMAC-2D solves on triangles, so a
#: triangulation is the whole of what the domain can be handed as a mesh. The
#: release enters the water at a POINT - floats released in shallow margins or
#: against a wall are dropped by the module, so the point is snapped for
#: clearance before it is compiled into the release routine.
ACCEPTS = Accepts(mesh=("unstructured_tri",), release=("point",))


class PARAMS:
    """What only an oil-slick question asks: where the oil enters the water,
    which oil it is, how much of it dissolves, how the slick is drawn, and the
    flow that carries it. The domain, the bed, its boundary runs and the
    granularity are the runtime's own slots and levers, and the deck's
    roughness and cadence are keywords the module's dictionary describes."""

    release = Param(
        door=doors.USER, optional=True, user_lever=True,
        consequence="scenario", type=Point,
        derived_when_absent=(
            "the release sits at spill_fraction along the domain the mesh was "
            "built over; the slick's drift is measured from there"),
        desc="Where the oil enters the water, as a Point: the pick's "
             "{coordinates, name} verbatim, a (lon, lat) pair, 'lat,lon', a "
             "point layer, or a place name. Its name becomes the tracer's name, "
             "and on a river with no domain supplied it is also the seed the "
             "reach is walked downstream from")
    discharge_m3s = Param(
        door=doors.USER, optional=True, units="m^3/s",
        bounds=(0.01, 1.0e5), consequence="physics", user_lever=True,
        derived_when_absent=(
            "the steady carrier discharge is read from the NOAA National Water "
            "Model over the domain; no NWM coverage refuses typed rather than "
            "falling back to a constant"),
        desc="Steady CARRIER discharge across the inflow boundary run - the "
             "flow that dilutes and transports the release")

    # -- the release -------------------------------------------------------- #
    spill_fraction = Param(
        door=doors.SCENARIO, default=0.25, bounds=(0.05, 0.9),
        consequence="scenario",
        desc="Along-domain release position, 0=inflow..1=outflow; the source "
             "must sit strictly INSIDE the domain, never on a boundary")
    spill_duration_s = Param(
        door=doors.SCENARIO, default=300.0,
        bounds=(1.0, 86400.0), units="s", consequence="scenario",
        desc="Finite pulse injection window")
    source_q_m3s = Param(
        door=doors.SCENARIO, default=8.0, bounds=(0.5, 30.0),
        units="m^3/s", consequence="scenario",
        desc="Point-source discharge of the release itself, small against the "
             "carrier flow")
    oil_concentration_mgl = Param(
        door=doors.SCENARIO, default=100.0,
        bounds=(0.0, 1.0e6), units="mg/L", consequence="scenario",
        desc="Concentration of the DISSOLVED fraction released with the slick, "
             "carried as the water's tracer")

    # -- which oil ---------------------------------------------------------- #
    oil_type = Param(
        door=doors.QUESTION, default="light_crude", consequence="scenario",
        desc="Which preset was spilled: light_crude | diesel | heavy_fuel - the "
             "module's own composition, density and viscosity. Crude runs as "
             "light_crude, gasoline and petrol as diesel, bunker as heavy_fuel")

    # -- the slick ---------------------------------------------------------- #
    n_drogues = Param(
        door=doors.SCENARIO, default=100, bounds=(1.0, 20000.0), type=int,
        user_lever=True, consequence="scenario",
        desc="How many floating particles the module tracks; the slick is drawn "
             "from their positions, so a coarse count draws a coarse slick")
    drogues_period_s = Param(
        door=doors.SCENARIO, default=60.0, bounds=(1.0, 3600.0), units="s",
        consequence="numerical",
        desc="How often the particle positions are written; it converts to a "
             "count of solver steps at the run's own timestep")
    oil_release_step = Param(
        door=doors.SCENARIO, default=600, bounds=(1.0, 1.0e6), type=int,
        consequence="scenario",
        desc="The solver step the floats are released at, compiled into the "
             "module's own release routine; it lets the flow field establish "
             "before the slick is put on it")

    # -- the forcings this question adds ------------------------------------ #
    wind_speed_mps = Param(
        door=doors.SCENARIO, default=0.0, bounds=(0.0, 60.0),
        units="m/s", consequence="scenario",
        desc="Sustained wind driving a surface wind-stress term; 0 = no wind")
    wind_direction_deg = Param(
        door=doors.SCENARIO, default=0.0, bounds=(0.0, 360.0),
        units="deg", consequence="scenario",
        desc="Compass bearing the wind blows FROM (0=N, 90=E); only read when "
             "wind_speed_mps > 0")
    # ONE signed rate, because the keyword it writes is signed: a net loss is a
    # negative rain rather than a second value subtracted from this one.
    rainfall_mm_per_day = Param(
        door=doors.USER, optional=True, bounds=(-50.0, 2000.0),
        units="mm/day", consequence="scenario",
        desc="NET distributed rainfall applied at every wet node, independent "
             "of the inflow hydrograph; negative is evaporation")

    # -- the clock ---------------------------------------------------------- #
    sim_duration_s = Param(
        door=doors.SCENARIO, default=3600.0,
        bounds=(600.0, 14400.0), units="s", consequence="numerical",
        desc="Simulated physical time")


DOC = dict(
    summary="An OIL SLICK released onto a body of surface water: floating particles plus the dissolved fraction.",
    routing=(
        "THE tool for \"an oil spill - where does the slick go\": a barge, "
        "pipeline, terminal or vessel release of crude, diesel, gasoline or "
        "heavy fuel onto water. TELEMAC-2D over the domain the run solves on - "
        "a reach walked from the release point, a harbour or lake drawn on the "
        "canvas, or a supplied polygon - with the engine's own oil-spill module "
        "riding on the solve: floating particles are tracked and drawn as the "
        "slick, and the dissolved fraction is advected as the water's tracer. "
        "Give `release` as a place, a pick or a pair, or supply `domain`."
    ),
    not_for=(
        "a conservative dye or contaminant plume with no slick "
        "(`telemac_dye_release`); bed SCOUR (`telemac_bed_scour`); a "
        "SUSPENDED sediment plume (`telemac_sediment_plume`); "
        "dissolved-oxygen sag (`telemac_do_sag`). Weathering, evaporation and "
        "beaching are the module's own and are not calibrated here"
    ),
    params=PARAMS,
    controls=(
        ("input_mode",
         '"user_gated" presents the filled sheet for review/edit before the solve '
         'and WAITS; "auto" (session default) proceeds with every assumption '
         "labeled. Not a physical value."),
        ("restart_clean",
         "True discards any ledger left under this same invocation and re-runs "
         "every step from the top."),
    ),
    returns=(
        "On success the run's record (an `AnswerLayerURI`): every variable its "
        "module wrote, styled on one mesh layer, animated where it varies, "
        "plus the dissolved-oil series charted and the slick track from the "
        "floats. Its `answer` carries `oil_cmax_mgl` / "
        "`oil_peak_time_s` / `plume_reach_m` / `slick_drift_m` / "
        "`floats_released` / `floats_remaining`; narrate those typed numbers. "
        "On failure a dict with `status=\"error\"` + `error_code`."
    ),
)
