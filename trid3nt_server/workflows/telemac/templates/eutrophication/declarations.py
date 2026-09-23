"""The CONTRACT of the eutrophication question: its declared params and its prose."""

from __future__ import annotations

from trid3nt_server.inputs import Point
from trid3nt_server.workflows.runtime import Accepts, Param, doors, lever

__all__ = ["ACCEPTS", "DOC", "PARAMS"]

#: What an enrichment run can be HANDED. TELEMAC-2D solves on triangles, so a
#: triangulation is the whole of what this question can be handed as a mesh;
#: nothing is released into the water, so there is no release geometry.
ACCEPTS = Accepts(mesh=("unstructured_tri",))


class PARAMS:
    seed = Param(
        door=doors.USER, optional=True, consequence="aoi", type=Point,
        derived_when_absent=(
            "nothing is fetched and the domain is the polygon the caller "
            "supplied or drew"),
        desc="Where on the channel the modelled stretch STARTS, as a Point: the "
             "pick's {coordinates, name} verbatim, a (lon, lat) pair, 'lat,lon', "
             "a point layer. Geocode a place name first. The stretch walked "
             "downstream of it is the water one pass is measured over; supply "
             "the domain polygon instead and this is not read")

    station = Param(
        door=doors.USER, optional=True, consequence="scenario",
        user_lever=True, type=Point,
        derived_when_absent=(
            "the two history charts are read as the domain-wide maximum at each "
            "instant rather than at one place"),
        desc="Where to watch the biomass and the oxygen over time, as a Point: "
             "the pick's {coordinates, name} verbatim, a (lon, lat) pair, "
             "'lat,lon' or a point layer (geocode a place name first). The "
             "profiles are all longitudinal and do not move with it")

    do_standard_mgl = Param(
        door=doors.SCENARIO, default=5.0, bounds=(0.0, 15.0),
        units="mg/L", consequence="scenario",
        desc="The DO water-quality standard the water is judged against; 5 is a "
             "common warm-water aquatic-life criterion. It never reaches the "
             "deck: no keyword names a standard, and the oxygen profile carries "
             "it as a reference line")

    # The runtime's own granularity lever, restated ONLY for its default: this
    # question is domain-scale chemistry over a long window rather than a local
    # feature at one instant, and the runtime's 14 m quarters the time step for
    # nothing it can resolve. Bounds and meaning are the lever's.
    mesh_resolution_m = lever(
        "mesh_resolution_m", default=25.0,
        desc="Target element edge length the domain is triangulated at; it also "
             "sets the CFL time step, so it is what decides whether a long "
             "window finishes")

DOC = dict(
    summary="NUTRIENT ENRICHMENT in a body of water: algal growth, nutrient "
            "drawdown and the oxygen response over one pass through it.",
    routing=(
        "THE tool for \"what does this water do to its nutrient load\", \"how much "
        "algae grows down this river\", \"eutrophication of this stream\", \"does "
        "the algal growth pull the oxygen down\". Solves TELEMAC-2D + WAQTEL "
        "EUTRO: phytoplankton growing on STATED nitrate and phosphate under stated "
        "light and temperature, dying back, and the oxygen budget that drives. "
        "Answers the LONGITUDINAL change over one pass. Supply the domain polygon, "
        "or `seed` a point on it. Opinions, as keywords: DURATION, INITIAL VALUES "
        "OF TRACERS (eight, in process order), WATER TEMPERATURE, SUNSHINE FLUX "
        "DENSITY ON WATER SURFACE, GRAPHIC PRINTOUT PERIOD."
    ),
    not_for=(
        "the oxygen sag below a WASTEWATER DISCHARGE (`telemac_do_sag`); a "
        "conservative plume that only dilutes (`telemac_dye_release`); a SEASONAL "
        "bloom in standing water - this measures ONE PASS. Nutrients are STATED"
    ),
    params=PARAMS,
    controls=(
        ("input_mode",
         '"user_gated" presents the resolved carrier discharge and the deck the '
         'run is written on for review/edit before the solve and WAITS; "auto" '
         "(session default) proceeds with every assumption labeled. Not a "
         "physical value."),
        ("restart_clean",
         "True discards any ledger left under this same invocation and re-runs "
         "every step from the top. Nothing a FAILED attempt left behind is ever "
         "replayed and a run that completed is never replayed either, so a fresh "
         "invocation always re-solves against live upstream data; what this flag "
         "clears is the work a derived rerun inherited, or records a process that "
         "died without unwinding left on disk."),
    ),
    returns=(
        "On success the run's record (a `LayerURI`): every variable its "
        "modules wrote - the eight tracers the eutrophication process "
        "appends among them - styled on one mesh layer, animated where it "
        "varies, plus the biomass and the oxygen along the water's path as "
        "charts and their history where the station was picked. On failure "
        "a dict with `status=\"error\"` + `error_code`."
    ),
)
