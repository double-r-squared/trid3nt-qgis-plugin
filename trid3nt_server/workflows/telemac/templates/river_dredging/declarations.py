"""The CONTRACT of ``telemac_river_dredging``: what only a maintenance dredge asks."""

from __future__ import annotations

from trid3nt_server.workflows.runtime import Accepts, Param, doors
from trid3nt_server.workflows.telemac.modules.gaia import GRAIN_UM_MAX, GRAIN_UM_MIN

__all__ = ["ACCEPTS", "DOC", "PARAMS"]

#: What a dredge can be HANDED. The bed moves on the same triangulation the
#: hydrodynamics runs on, so a lattice is refused at the door; the two areas
#: declare the shape they take on their own DATA rows.
ACCEPTS = Accepts(mesh=("unstructured_tri",))


class PARAMS:
    """What only a maintenance-dredge question asks."""
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
        desc="Steady upstream discharge the reach is dredged under - the flow "
             "that shoals the channel and carries the disturbed material")
    event_time = Param(
        door=doors.QUESTION, optional=True, consequence="scenario",
        derived_when_absent=(
            "the carrier discharge is read at the MOST RECENT published NWM "
            "cycle"),
        desc="The moment to read the discharge cycle at - from phrasing like "
             "'at last week's flow'; an ISO date or datetime. The NWM PDS "
             "bucket retains only the last ~30 days of history; a deeper "
             "request refuses typed rather than silently reading a different "
             "cycle.")
    friction_coefficient = Param(
        door=doors.USER, optional=True, bounds=(10.0, 90.0),
        user_lever=True, consequence="numerical",
        derived_when_absent=(
            "the reach is solved at Strickler 33.0, which is also the "
            "roughness its outflow stage is derived as a normal depth at"),
        desc="Bed roughness under friction_law")
    friction_law = Param(
        door=doors.USER, optional=True, consequence="numerical", type=int,
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
        door=doors.SCENARIO, default=2.0, bounds=(0.5, 15.0),
        units="km", consequence="aoi",
        desc="Modeled reach length downstream of the seed; a longer reach is "
             "coarsened under the mesh node budget")
    sim_duration_s = Param(
        door=doors.SCENARIO, default=3600.0,
        bounds=(600.0, 604800.0), units="s", consequence="numerical",
        desc="Simulated physical time the dredge campaign runs over; the "
             "morphological factor is what makes a short window produce a "
             "readable bed change")
    mesh_resolution_m = Param(
        door=doors.SCENARIO, default=14.0, user_lever=True,
        bounds=(3.0, 5000.0), units="m", consequence="numerical",
        desc="Target element edge length the reach is triangulated at; the "
             "dredged volume is the sum over the nodes inside the field, so a "
             "coarse mesh resolves a narrow fairway badly")

    # -- the fields --------------------------------------------------------- #
    time_origin = Param(
        door=doors.SCENARIO, default=[2000, 1, 1, 0, 0, 0], type=list,
        consequence="scenario",
        desc="The calendar instant the run's clock starts at, as [year, month, "
             "day, hour, minute, second] - the origin every dredge schedule is "
             "dated against and the one the deck states")

    # -- the grade ---------------------------------------------------------- #
    design_depth_m = Param(
        door=doors.SCENARIO, default=3.0, bounds=(0.1, 40.0), units="m",
        user_lever=True, consequence="scenario",
        desc="Depth the fairway is dredged TO, under the reference water "
             "surface the run opens at - the design draught plus its overdepth")
    trigger_depth_m = Param(
        door=doors.SCENARIO, default=3.0, bounds=(0.1, 40.0), units="m",
        user_lever=True, consequence="scenario",
        desc="Depth at which a node is dredged: the bed is worked wherever it "
             "sits shallower than this under the reference surface. Equal to "
             "design_depth_m keeps the channel exactly at grade; SMALLER than "
             "it lets the channel shoal before the dredger returns, and a "
             "trigger DEEPER than the grade would mark a node for a cut that "
             "is above its own bed")

    # -- the schedule ------------------------------------------------------- #
    # THE CAMPAIGN'S CLOCK IS THE BED'S, not the solver's. The engine divides
    # every stated time by the deck's morphological factor and multiplies the
    # rates by it, so a schedule fits a run when it lands inside
    # sim_duration_s * morphological_factor. A pass also runs until it has cut
    # to grade rather than until dredge_end_s: the end only stops the NEXT pass
    # from starting, which is the engine's own behaviour and the reason a pass
    # that cannot finish inside the run reports no volume at all.
    dredge_start_s = Param(
        door=doors.SCENARIO, default=0.0, bounds=(0.0, 6048000.0), units="s",
        consequence="scenario",
        desc="When the first dredging pass begins, in seconds of the BED's own "
             "clock - the run's morphological time, sim_duration_s x "
             "morphological_factor")
    dredge_end_s = Param(
        door=doors.SCENARIO, default=3000.0, bounds=(1.0, 6048000.0), units="s",
        consequence="scenario",
        desc="When the campaign stops, on the same bed clock: no further pass "
             "begins after it, and a pass already cutting runs on until it "
             "reaches grade")
    dredge_repeat_s = Param(
        door=doors.SCENARIO, default=1800.0, bounds=(1.0, 6048000.0), units="s",
        consequence="scenario",
        desc="Maintenance interval on the bed clock: how long after a pass "
             "starts the next one begins, if the channel has shoaled past "
             "trigger_depth_m again")

    # -- the rates ---------------------------------------------------------- #
    dig_rate_m_per_s = Param(
        door=doors.SCENARIO, default=0.002, bounds=(1.0e-6, 1.0), units="m/s",
        user_lever=True, consequence="scenario",
        desc="How fast the dredger lowers the bed, as metres of bed per second "
             "of SOLVER time at a working node - the plant's capacity, not a "
             "physical rate. A pass reports its volume only once it has reached "
             "grade, so this and the cut it has to make are what decide whether "
             "the run sees a completed pass at all")
    dump_rate_m_per_s = Param(
        door=doors.SCENARIO, default=0.002, bounds=(1.0e-6, 1.0), units="m/s",
        user_lever=True, consequence="scenario",
        desc="How fast the spoil is laid into the dump area, as metres of bed "
             "per second of solver time at a receiving node; a pass is not "
             "finished until its spoil is placed")
    min_volume_m3 = Param(
        door=doors.SCENARIO, default=0.0, bounds=(0.0, 1.0e7), units="m^3",
        consequence="scenario",
        desc="Least volume worth moving around a node before it is dredged at "
             "all; 0 works every node past the trigger")

    # -- the bed ------------------------------------------------------------ #
    grain_size_um = Param(
        door=doors.SCENARIO, default=200.0,
        bounds=(GRAIN_UM_MIN, GRAIN_UM_MAX), units="um", user_lever=True,
        consequence="scenario",
        desc="Median grain diameter d50 of the bed the channel shoals with - "
             "~200 um fine sand, ~20 um silt (all modeled non-cohesive)")
    bed_thickness_m = Param(
        door=doors.SCENARIO, default=5.0, bounds=(0.05, 50.0),
        units="m", consequence="scenario",
        desc="Depth of the erodible sediment stock the dredger can cut into")
    bedload_formula = Param(
        door=doors.SCENARIO, default=1, type=int, consequence="numerical",
        desc="GAIA bed-load law: 1=Meyer-Peter-Mueller, 2=Einstein-Brown, "
             "7=van Rijn")
    morphological_factor = Param(
        door=doors.SCENARIO, default=10.0, bounds=(1.0, 100.0),
        user_lever=True, consequence="numerical",
        desc="Amplifies bed change per hydraulic step so a short campaign "
             "yields a readable depth; a speed-up lever, not a rate")


DOC = dict(
    summary="MAINTENANCE DREDGING of a navigation channel: how much comes out, "
            "and what the bed does.",
    routing=(
        "THE tool for \"dredge this channel and tell me the volume\" - a "
        "maintenance dredge of a fairway or berth pocket held at a design "
        "depth, the spoil placed in a named disposal area, and the bed's "
        "response to both. TELEMAC-2D coupled with GAIA over a REAL NHDPlus "
        "reach, the dredger driven by NESTOR on the sediment deck so what it "
        "moves is in the bed's own mass balance. Returns the bed-evolution map "
        "and animation plus the dredged and dumped volumes. Supply `location` "
        "OR `bbox`, and the two areas as polygons."
    ),
    not_for=(
        "where the bed scours and re-deposits on its own, with no dredger "
        "(`telemac_river_scour`); a SUSPENDED sediment plume settling onto an "
        "inert bed (`telemac_river_sediment_plume`); a conservative dye or "
        "contaminant plume (`telemac_river_dye`); an OIL slick "
        "(`telemac_river_oil_spill`); dissolved-oxygen sag (`telemac_do_sag`)"
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
        "On success the bed-evolution layer (a `LayerURI`, metres, deposition "
        "positive and the dredged cut negative) - the emitter loads the map and "
        "animates the bed beside it - whose `answer` carries `dug_volume_m3` / "
        "`dumped_volume_m3` (the engine's own report lines, summed over the "
        "maintenance passes it printed) beside `dredge_report`, which states "
        "why they read as they do and says so when a pass did not finish inside "
        "the run's clock, `dredged_bed_change_m` and "
        "`dumped_bed_change_m` (the bed change inside each area) and "
        "`net_bed_mass_kg`; narrate those typed numbers. On failure a dict with "
        "`status=\"error\"` + `error_code`."
    ),
)
