"""The CONTRACT of ``telemac_micropollutant_release``: its declared params and its prose."""

from __future__ import annotations

from trid3nt_server.inputs import Point
from trid3nt_server.workflows.runtime import Accepts, Param, doors, lever

__all__ = ["ACCEPTS", "DOC", "PARAMS"]

#: What a run of this question can be HANDED. TELEMAC-2D solves on triangles, so
#: a triangulation is the whole of what the domain can be handed as a mesh. The
#: substance enters the water at a POINT, which is the one release geometry this
#: pipeline has been run against.
ACCEPTS = Accepts(mesh=("unstructured_tri",), release=("point",))


class PARAMS:
    """What only a sorbing-substance question asks: where it enters and how much,
    the sediment it partitions onto, how hard it holds on and how long it lasts,
    and where downstream the history is read. The domain, the bed, its boundary
    runs and the granularity are the runtime's own slots and levers, and the
    deck's roughness and cadence are keywords the module's dictionary describes."""

    # -- the release -------------------------------------------------------- #
    release = Param(
        door=doors.USER, optional=True, user_lever=True,
        consequence="scenario", type=Point,
        derived_when_absent=(
            "the release sits at release_fraction along the domain the mesh was "
            "built over; the travelled distance is measured from there"),
        desc="Where the substance enters the water, as a Point: the pick's "
             "{coordinates, name} verbatim, a (lon, lat) pair, 'lat,lon', a "
             "point layer, or a place name. On a river with no domain supplied "
             "it is also the seed the reach is walked downstream from")
    release_fraction = Param(
        door=doors.SCENARIO, default=0.1, bounds=(0.05, 0.9),
        consequence="scenario",
        desc="Along-domain release position, 0=inflow..1=outflow; the source "
             "must sit strictly INSIDE the domain, never on a boundary. It sits "
             "near the top so the substance has water left to sorb and settle in")
    release_duration_s = Param(
        door=doors.SCENARIO, default=3600.0,
        bounds=(1.0, 86400.0), units="s", consequence="scenario",
        desc="Finite injection window; the substance is released over it and the "
             "rest of the run is what happens to what was released")
    source_q_m3s = Param(
        door=doors.SCENARIO, default=1.0, bounds=(0.0001, 1000.0),
        units="m^3/s", consequence="scenario",
        desc="Discharge of the release itself - with the carrier flow this sets "
             "the dilution the water receives")
    source_concentration_mgl = Param(
        door=doors.SCENARIO, default=100.0,
        bounds=(0.0, 1.0e6), units="mg/L", consequence="scenario",
        desc="DISSOLVED concentration of the substance in the release itself, "
             "before any dilution; everything that ends up on sediment gets "
             "there by sorption during the run")

    # -- the sediment the substance partitions onto -------------------------- #
    ambient_spm_kg_m3 = Param(
        door=doors.SCENARIO, default=0.03, bounds=(0.0, 10.0),
        units="kg/m^3", consequence="physics", user_lever=True,
        desc="Suspended sediment the water already carries, in and at the top of "
             "the domain, in KILOGRAMS PER CUBIC METRE - the class the sorption "
             "coefficient's own m^3/kg is read against, so 30 mg/L is 0.03. It "
             "is the SORBENT: with none, the substance stays dissolved and "
             "nothing reaches the bed. Nothing fetches suspended sediment, so "
             "this is a STATED condition, not a measured one - state the gauged "
             "value where there is one")

    # -- how the substance behaves ------------------------------------------- #
    decay_constant_per_s = Param(
        door=doors.USER, optional=True, bounds=(0.0, 1.0),
        units="1/s", user_lever=True, consequence="physics",
        derived_when_absent=(
            "the engine's own exponential desintegration constant, 1.13e-7 1/s "
            "- a 71-day half-life - stands; state 0 for a substance that does "
            "not break down at all"),
        desc="First-order decay constant of the substance, applied at the same "
             "rate to all three of its phases - dissolved, on suspended sediment "
             "and on bed sediment. A half-life h in days is ln(2)/(86400 h)")
    settling_velocity_mps = Param(
        door=doors.USER, optional=True, bounds=(0.0, 0.1),
        units="m/s", user_lever=True, consequence="physics",
        derived_when_absent=(
            "the engine's own sediment settling velocity, 6e-6 m/s, stands"),
        desc="Settling velocity of the suspended sediment - what carries the "
             "sorbed substance down onto the bed")
    distribution_coefficient_m3kg = Param(
        door=doors.USER, optional=True, bounds=(0.0, 1.0e6),
        units="m^3/kg", user_lever=True, consequence="physics",
        derived_when_absent=(
            "the engine's own coefficient of distribution, 1775 m3/kg, stands"),
        desc="Kd, the sorption equilibrium: how strongly the substance partitions "
             "onto sediment rather than staying dissolved. A larger Kd puts more "
             "of it on the bed")
    desorption_constant_per_s = Param(
        door=doors.USER, optional=True, bounds=(0.0, 1.0),
        units="1/s", user_lever=True, consequence="physics",
        derived_when_absent=(
            "the engine's own constant of desorption kinetic, 2.5e-7 1/s, stands"),
        desc="How fast the substance comes back off the sediment - with Kd it "
             "sets how quickly the partition reaches equilibrium")

    # -- where the history is read ------------------------------------------- #
    monitoring_point = Param(
        door=doors.USER, optional=True, user_lever=True,
        consequence="scenario", type=Point,
        derived_when_absent=(
            "the history is read at monitoring_fraction along the domain the "
            "mesh was built over"),
        desc="Where the dissolved history is read, as a Point: the pick's "
             "{coordinates, name} verbatim, a (lon, lat) pair, 'lat,lon', a "
             "point layer, or a place name")
    monitoring_fraction = Param(
        door=doors.SCENARIO, default=0.85, bounds=(0.1, 0.95),
        consequence="scenario",
        desc="Along-domain position the history is read at when no point was "
             "given, 0=inflow..1=outflow")

    # -- the clock ----------------------------------------------------------- #
    sim_duration_s = lever(
        "sim_duration_s", default=172800.0, bounds=(3600.0, 864000.0),
        desc="Simulated time. Sorption equilibrates in hours and settling takes "
             "longer than that, so a window of a few hours reports a partition "
             "that has not happened yet; two days is what the default covers")


DOC = dict(
    summary="A SORBING substance released into water: how much stays DISSOLVED and how much ends up ON THE BED.",
    routing=(
        "THE tool for \"where does this pollutant END UP\" - a metal, a PCB, a "
        "pesticide, any substance that ATTACHES TO SEDIMENT: how much travels "
        "dissolved and how much rides the suspended sediment onto the bed. "
        "TELEMAC-2D + WAQTEL micropol over the domain the run solves on - a "
        "reach walked from the release point, a basin or harbour drawn on the "
        "canvas, or a supplied polygon. A finite point release, the water's own "
        "suspended sediment as the sorbent, settling onto the bed, an optional "
        "half-life. Give `release` as a place, a pick or a pair, or supply "
        "`domain`."
    ),
    not_for=(
        "a CONSERVATIVE tracer that only dilutes (`telemac_dye_release`); an OIL "
        "slick (`telemac_oil_spill`); bed SCOUR and grain sorting "
        "(`telemac_bed_scour`); a suspended SEDIMENT plume carrying nothing "
        "(`telemac_sediment_plume`); dissolved-oxygen sag "
        "(`telemac_do_sag`). Groundwater contamination is not modeled here"
    ),
    params=PARAMS,
    controls=(
        ("input_mode",
         '"user_gated" presents the resolved scenario sheet for review/edit and asks '
         'for the release point and the monitoring point on the canvas before the '
         'solve, and WAITS; "auto" (session default) proceeds with every assumption '
         "labeled. Not a physical value."),
        ("restart_clean",
         "True discards any ledger left under this same invocation and re-runs "
         "every step from the top. Nothing a FAILED attempt left behind is ever "
         "replayed and a run that completed is never replayed either, so a fresh "
         "invocation always re-solves against live upstream data; what this flag "
         "clears is the work a derived rerun inherited, or records a process that "
         "died without unwinding left on disk."),
    ),
    returns=(
        "On success the run's record (an `AnswerLayerURI`): every variable its "
        "modules wrote, styled on one mesh layer, animated where it varies, plus "
        "the dissolved history at the monitoring point charted. Its `answer` "
        "carries `dissolved_cmax_mgl` / `dissolved_peak_time_s` at the "
        "monitoring point, `dissolved_travel_m`, and the partition at the last "
        "instant as `dissolved_final_mean_mgl` / "
        "`suspended_sorbed_final_mean_mgl` / `bed_sorbed_final_mean_g_m2` - the "
        "bed phase is what settled onto a square metre, not a concentration in "
        "the water - and `sorbed_over_dissolved` as how much rides the suspended "
        "sediment for every unit still dissolved; narrate those typed numbers. "
        "On failure a dict with "
        "`status=\"error\"` + `error_code`."
    ),
)
