"""The CONTRACT of ``telemac_do_sag``: its declared params and its prose."""

from __future__ import annotations

from trid3nt_server.inputs import Point
from trid3nt_server.workflows.runtime import Accepts, Param, doors

__all__ = ["ACCEPTS", "DOC", "PARAMS"]

#: What an outfall run can be HANDED. TELEMAC-2D solves on triangles, so a
#: triangulation is the whole of what this question can be handed as a mesh; the
#: discharge enters the water at a POINT, which is the one release geometry the
#: sag pipeline has been run against.
ACCEPTS = Accepts(mesh=("unstructured_tri",), release=("point",))


class PARAMS:
    """What only an oxygen-sag question asks: where the discharge enters, what it
    carries, and the standard the sag is judged against. The domain, the bed, its
    boundary runs and the granularity are the runtime's own slots and levers, and
    the clock, the roughness, the cadence and the O2 kinetics are keywords the
    module's dictionary describes and the deck states by their own names."""

    # -- the discharge ------------------------------------------------------- #
    outfall_coords = Param(
        door=doors.USER, optional=True, consequence="scenario",
        user_lever=True, type=Point,
        derived_when_absent=(
            "with a domain supplied the outfall sits near the top of its "
            "centerline and the sag distance is measured downstream from there; "
            "with no domain either, the run refuses rather than inventing where "
            "a permitted discharge is"),
        desc="Where the discharge enters the water, as a Point: the pick's "
             "{coordinates, name} verbatim, a (lon, lat) pair, 'lat,lon', a "
             "point layer. Geocode a place name first. On a river with no domain "
             "supplied it is also the seed the reach is walked downstream from")
    effluent_bod_mgl = Param(
        door=doors.SCENARIO, default=250.0,
        bounds=(0.1, 5000.0), units="mg/L", consequence="scenario",
        desc="Ultimate carbonaceous BOD IN THE DISCHARGE ITSELF - what leaves the "
             "outfall pipe, before any dilution; the mixed load the water carries "
             "is what the solve computes from this and the carrier flow")
    effluent_q_m3s = Param(
        door=doors.SCENARIO, default=1.0,
        bounds=(0.0001, 1000.0), units="m^3/s", consequence="scenario",
        desc="Discharge rate at the outfall - with the carrier flow this sets the "
             "dilution, and so how much of the effluent load the water carries")
    effluent_do_mgl = Param(
        door=doors.SCENARIO, default=2.0,
        bounds=(0.0, 20.0), units="mg/L", consequence="scenario",
        desc="Dissolved oxygen in the discharge itself; a treated effluent arrives "
             "oxygen-poor, which is the initial deficit the sag starts from")

    # -- what the sag is judged against -------------------------------------- #
    do_standard_mgl = Param(
        door=doors.SCENARIO, default=5.0, bounds=(0.0, 15.0),
        units="mg/L", consequence="scenario",
        desc="The DO water-quality standard the sag is judged against; 5 is a "
             "common warm-water aquatic-life criterion")


DOC = dict(
    summary="DISSOLVED-OXYGEN SAG below a discharge (US TMDL / permit question).",
    routing=(
        "THE tool for \"where does dissolved oxygen bottom out below this discharge\", "
        "\"will the DO sag violate the standard\", \"Streeter-Phelps oxygen sag\", \"BOD "
        "loading downstream of a WWTP / outfall\". TELEMAC-2D + WAQTEL O2 over a reach "
        "walked downstream from the outfall, or a polygon you supply: clean "
        "water in at the inflow, the DISCHARGE a continuous source of organic load "
        "and low oxygen, CBOD decaying and reaeration recovering downstream. Produces "
        "the along-channel oxygen profile against the closed form. Deck opinions, by "
        "keyword: CONSTANT OF DEGRADATION OF ORGANIC LOAD K1, K2 REAERATION "
        "COEFFICIENT, O2 SATURATION DENSITY OF WATER (CS), WATER TEMPERATURE, DURATION. "
        "Give `outfall_coords` or `domain`."
    ),
    not_for=(
        "a conservative dye/tracer plume that only dilutes "
        "(`telemac_dye_release`); rainfall-runoff flood depth "
        "(`telemac_rain_on_grid`); a closed body with no through-flow, which "
        "has no sag"
    ),
    params=PARAMS,
    controls=(
        ("input_mode",
         '"user_gated" presents the resolved carrier discharge and bed source for '
         'review/edit before the solve and WAITS; "auto" (session default) proceeds '
         "with every assumption labeled. Not a physical value."),
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
        "modules wrote, styled on one mesh layer, animated where it varies, "
        "plus the oxygen profile on its Streeter-Phelps curve. `answer` carries "
        "`do_min_mgl` / `do_below_standard` (vs `do_standard_mgl`) / "
        "`do_min_distance_m` / `bod_mixed_mgl` / `mean_velocity_mps`; narrate "
        "those numbers. On failure a dict with `status=\"error\"` + `error_code`."
    ),
)
