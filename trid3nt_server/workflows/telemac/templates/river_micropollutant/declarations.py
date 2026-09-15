"""The CONTRACT of ``telemac_river_micropollutant``: its declared params and its prose."""

from __future__ import annotations

from trid3nt_server.inputs import Point
from trid3nt_server.workflows.runtime import Accepts, Param, doors

__all__ = ["ACCEPTS", "DOC", "PARAMS"]

#: What a reach run can be HANDED. TELEMAC-2D solves on triangles, so a
#: triangulation is the whole of what a reach corridor can be handed as a mesh.
#: The substance enters the water at a POINT, which is the one release geometry
#: this pipeline has been run against.
ACCEPTS = Accepts(mesh=("unstructured_tri",), release=("point",))


class PARAMS:
    """What only a sorbing-substance question asks: where it enters and how
    much, the sediment it partitions onto, how hard it holds on and how long it
    lasts, and where downstream the history is read."""

    # -- the reach ---------------------------------------------------------- #
    location = Param(
        door=doors.QUESTION, optional=True, consequence="aoi",
        desc="Place name on the river, geocoded to the reach")
    bbox = Param(
        door=doors.USER, optional=True, consequence="aoi",
        type=tuple[float, float, float, float] | list[float] | str,
        desc="Explicit AOI (min_lon,min_lat,max_lon,max_lat) EPSG:4326, instead of a place")
    river_geometry_uri = Param(
        door=doors.USER, optional=True, consequence="aoi",
        derived_when_absent="the reach flowline is fetched fresh for the AOI",
        desc="Reuse an already-fetched river flowline for this reach instead of "
             "re-fetching it")
    discharge_m3s = Param(
        door=doors.USER, optional=True, units="m^3/s",
        bounds=(0.01, 1.0e5), consequence="physics", user_lever=True,
        derived_when_absent=(
            "the steady carrier discharge is resolved from the NOAA National "
            "Water Model at the reach; no NWM coverage refuses typed rather "
            "than falling back to a constant"),
        desc="Steady upstream CARRIER discharge - the river flow that dilutes "
             "and transports the release")
    event_time = Param(
        door=doors.QUESTION, optional=True, consequence="scenario",
        derived_when_absent=(
            "the carrier discharge is read at the MOST RECENT published NWM "
            "cycle"),
        desc="The storm/event moment to read the carrier discharge cycle at - "
             "from phrasing like 'during last Tuesday's storm'; an ISO date "
             "or datetime (e.g. '2026-08-20' or '2026-08-20T06:00:00Z'). "
             "Unset reads the most recent published NWM cycle. The NWM PDS "
             "bucket retains only the last ~30 days of history; a deeper "
             "request refuses typed rather than silently reading a "
             "different cycle.")
    # The friction the reach is solved at when the ask states none is named on
    # the row because the outflow stage is a normal depth AT this roughness: a
    # stage derived at one number under a deck written at another is a level
    # the run never sits at. The assembler reads the same number.
    friction_coefficient = Param(
        door=doors.USER, optional=True, bounds=(10.0, 90.0),
        user_lever=True, consequence="numerical",
        derived_when_absent=(
            "the reach is solved at Strickler 33.0, which is also the "
            "roughness its outflow stage is derived as a normal depth at"),
        desc="Bed roughness under friction_law")
    friction_law = Param(
        door=doors.USER, optional=True, consequence="numerical",
        type=int,
        derived_when_absent="the coefficient is read as a Strickler one",
        desc="Law interpreting friction_coefficient: 2=Chezy, 3=Strickler, "
             "4=Manning")
    output_interval_min = Param(
        door=doors.USER, optional=True, bounds=(0.1, 1440.0),
        units="min", consequence="numerical",
        desc="Result-writing cadence; unset keeps the steering file's own period")
    compute_class = Param(
        door=doors.CONSTANT, default="medium",
        consequence="numerical", desc="Solve sizing class")
    reach_length_km = Param(
        door=doors.SCENARIO, default=6.0, bounds=(0.5, 15.0),
        units="km", consequence="aoi",
        desc="Modeled reach length downstream of the release; a longer reach is "
             "coarsened under the mesh node budget")

    # -- the release -------------------------------------------------------- #
    release = Param(
        door=doors.USER, optional=True, user_lever=True,
        consequence="scenario", type=Point,
        derived_when_absent=(
            "the release sits at release_fraction along the meshed reach; the "
            "downstream distance is measured from there"),
        desc="Where the substance enters the water, as a Point: the pick's "
             "{coordinates, name} verbatim, a (lon, lat) pair, 'lat,lon', a "
             "point layer, or a place name")
    release_fraction = Param(
        door=doors.SCENARIO, default=0.1, bounds=(0.05, 0.9),
        consequence="scenario",
        desc="Along-reach release position, 0=upstream..1=downstream; the source "
             "must sit strictly INSIDE the reach, never on a boundary. It sits "
             "near the top so the substance has reach left to sorb and settle in")
    release_duration_s = Param(
        door=doors.SCENARIO, default=3600.0,
        bounds=(1.0, 86400.0), units="s", consequence="scenario",
        desc="Finite injection window; the substance is released over it and the "
             "rest of the run is what happens to what was released")
    source_q_m3s = Param(
        door=doors.SCENARIO, default=1.0, bounds=(0.0001, 1000.0),
        units="m^3/s", consequence="scenario",
        desc="Discharge of the release itself - with the carrier flow this sets "
             "the dilution the reach receives")
    source_concentration_mgl = Param(
        door=doors.SCENARIO, default=100.0,
        bounds=(0.0, 1.0e6), units="mg/L", consequence="scenario",
        desc="DISSOLVED concentration of the substance in the release itself, "
             "before any dilution; everything that ends up on sediment gets "
             "there by sorption during the run")

    # -- the sediment the substance partitions onto -------------------------- #
    ambient_spm_mgl = Param(
        door=doors.SCENARIO, default=30.0, bounds=(0.0, 10000.0),
        units="mg/L", consequence="physics", user_lever=True,
        desc="Suspended sediment the river already carries, in and at the top of "
             "the reach. It is the SORBENT: with none, the substance stays "
             "dissolved and nothing reaches the bed. Nothing fetches suspended "
             "sediment for a reach, so this is a STATED condition, not a "
             "measured one - state the gauged value if the reach has one")

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
            "the history is read at monitoring_fraction along the meshed reach"),
        desc="Where downstream the dissolved history is read, as a Point: the "
             "pick's {coordinates, name} verbatim, a (lon, lat) pair, 'lat,lon', "
             "a point layer, or a place name")
    monitoring_fraction = Param(
        door=doors.SCENARIO, default=0.85, bounds=(0.1, 0.95),
        consequence="scenario",
        desc="Along-reach position the history is read at when no point was "
             "given, 0=upstream..1=downstream")

    # -- the horizon and the edge -------------------------------------------- #
    sim_duration_s = Param(
        door=doors.SCENARIO, default=172800.0,
        bounds=(3600.0, 864000.0), units="s", consequence="numerical",
        user_lever=True,
        desc="Simulated time. Sorption equilibrates in hours and settling takes "
             "longer than that, so a window of a few hours reports a partition "
             "that has not happened yet; two days is what the default covers")
    # THE granularity lever, and always an explicit sheet value: no sizing rung
    # derives an edge from the channel, so the number the run meshes at is either
    # the user's or the labeled default a review can see and change.
    mesh_resolution_m = Param(
        door=doors.SCENARIO, default=14.0, user_lever=True,
        bounds=(3.0, 5000.0), units="m", consequence="numerical",
        desc="Target element edge length the reach is triangulated at; peak "
             "concentration is a resolution-bound class and a coarse mesh reads "
             "it low")


DOC = dict(
    summary="A SORBING substance in a RIVER: how much stays DISSOLVED and how much ends up ON THE BED.",
    routing=(
        "THE tool for \"where does this pollutant END UP\" - a metal, a PCB, a "
        "pesticide, any substance that ATTACHES TO SEDIMENT: how much travels "
        "dissolved, how much rides the suspended sediment, how much is left on "
        "the bed. TELEMAC-2D + WAQTEL micropol over a REAL NHDPlus reach: a "
        "finite release at a point, the river's own suspended sediment as the "
        "sorbent, sorption at a distribution coefficient, settling onto the bed, "
        "an optional half-life. Supply `location` OR `bbox`."
    ),
    not_for=(
        "a CONSERVATIVE tracer that only dilutes (`telemac_river_dye`, which also "
        "carries first-order die-off on the tracer itself); an OIL slick (`telemac_river_oil_spill`); "
        "bed SCOUR and grain sorting (`telemac_river_scour`); a suspended SEDIMENT "
        "plume with no substance on it (`telemac_river_sediment_plume`); "
        "dissolved-oxygen sag (`telemac_do_sag`). Groundwater contamination is not "
        "modeled here"
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
        "`suspended_sorbed_final_mean_mgl` / `bed_sorbed_final_mean_mgl`, with "
        "`bed_over_dissolved` as how much sits on the bed for every unit still "
        "dissolved; narrate those typed numbers. On failure a dict with "
        "`status=\"error\"` + `error_code`."
    ),
)
