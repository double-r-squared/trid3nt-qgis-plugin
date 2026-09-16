"""The CONTRACT of ``telemac_channel_dredging``: what only a maintenance dredge asks."""

from __future__ import annotations

from trid3nt_server.inputs import Point
from trid3nt_server.workflows.runtime import Accepts, Param, doors, lever
from trid3nt_server.workflows.telemac.modules.gaia import GRAIN_UM_MAX, GRAIN_UM_MIN

__all__ = ["ACCEPTS", "DOC", "PARAMS"]

#: What a dredge can be HANDED. The bed moves on the same triangulation the
#: hydrodynamics runs on, so a lattice is refused at the door; the two areas
#: declare the shape they take on their own DATA rows.
ACCEPTS = Accepts(mesh=("unstructured_tri",))


class PARAMS:
    """What only a maintenance-dredge question asks."""
    # -- the water this dredge works in ------------------------------------- #
    seed_point = Param(
        door=doors.QUESTION, optional=True, consequence="aoi", type=Point,
        derived_when_absent=(
            "nothing: the dredge's reference surface is a band of cross-"
            "sections stationed down the channel from this point, so a run "
            "without one refuses by naming it"),
        desc="A point ON the channel the dredge works in, as the pick's "
             "{coordinates, name} verbatim, a (lon, lat) pair, 'lat,lon' or a "
             "point layer. Geocode a place name first; the channel is fetched "
             "downstream of it and the levels every dredging action reads are "
             "stationed along the centerline that comes back with it")
    sim_duration_s = lever(
        "sim_duration_s", bounds=(600.0, 604800.0),
        desc="Simulated physical time the dredge campaign runs over; the "
             "morphological factor is what makes a short window produce a "
             "readable bed change")

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
        "depth, the spoil placed in a disposal area, and the bed's response "
        "to both. TELEMAC-2D coupled with GAIA, the dredger driven by "
        "NESTOR so what it moves is in the bed's own mass balance. The channel "
        "is fetched downstream of a point on it; the bed is the published USACE "
        "survey where one covers it and the terrain elsewhere. Returns the "
        "bed-evolution map and animation plus the dredged and dumped volumes. "
        "Supply `seed_point` and the two areas as polygons."
    ),
    not_for=(
        "a bed that scours and re-deposits on its own, with no dredger "
        "(`telemac_bed_scour`); a SUSPENDED plume settling onto an inert "
        "bed (`telemac_sediment_plume`); a dye or contaminant plume "
        "(`telemac_dye_release`); an OIL slick (`telemac_oil_spill`)"
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
        "modules wrote, styled on one mesh layer, animated where it varies, "
        "the bed evolution in metres among them (deposition positive, the "
        "dredged cut negative), and no read the template placed. Its `answer` "
        "carries `dug_volume_m3` / "
        "`dumped_volume_m3` (the engine's own report lines, summed over the "
        "maintenance passes it printed) beside `dredge_report`, which states "
        "why they read as they do and says so when a pass did not finish inside "
        "the run's clock, `dredged_bed_change_m` and "
        "`dumped_bed_change_m` (the bed change inside each area) and "
        "`net_bed_mass_kg`; narrate those typed numbers. On failure a dict with "
        "`status=\"error\"` + `error_code`."
    ),
)
