"""The CONTRACT of ``telemac_river_eutrophication``: its declared params and its prose."""

from __future__ import annotations

from trid3nt_server.inputs import Point
from trid3nt_server.workflows.runtime import Accepts, Param, doors

__all__ = ["ACCEPTS", "DOC", "PARAMS"]

#: What an enrichment run can be HANDED. TELEMAC-2D solves on triangles, so a
#: triangulation is the whole of what an enriched reach can be handed as a mesh;
#: nothing is released into the water, so there is no release geometry.
ACCEPTS = Accepts(mesh=("unstructured_tri",))

_HELPERS = "trid3nt_server.workflows.telemac.helpers"

#: Where an observed nutrient, oxygen or water-temperature number for a US reach
#: comes from, named on every stated concentration below. It returns SAMPLE SITES
#: with their latest value, not a reach-wide field, so what this template solves
#: from is a value the user states against that record - never one it invented,
#: and never one it interpolated off the sites without being asked to.
_OBSERVED = "fetch_usgs_water_quality"


class PARAMS:
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
        desc="Steady upstream discharge - the flow that carries the nutrients "
             "through the reach and sets how long the water has to grow algae in")
    event_time = Param(
        door=doors.QUESTION, optional=True, consequence="scenario",
        derived_when_absent=(
            "the carrier discharge is read at the MOST RECENT published NWM "
            "cycle"),
        desc="The moment to read the carrier discharge cycle at - from phrasing "
             "like 'during last week's low flow'; an ISO date or datetime. The "
             "NWM PDS bucket retains only the last ~30 days of history; a deeper "
             "request refuses typed rather than silently reading a different cycle.")
    # The friction the reach is solved at when the ask states none is named on
    # the row because the outflow stage is a normal depth AT this roughness: a
    # stage derived at one number under a deck written at another is a level the
    # run never sits at. The assembler reads the same number.
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

    station = Param(
        door=doors.USER, optional=True, consequence="scenario",
        user_lever=True, type=Point,
        derived_when_absent=(
            "the two history charts are read as the reach-wide maximum at each "
            "instant rather than at one place"),
        desc="Where on the reach to watch the biomass and the oxygen over time, "
             "as a Point: the pick's {coordinates, name} verbatim, a (lon, lat) "
             "pair, 'lat,lon', a point layer, or a place name. The answers are "
             "all along-reach and do not move with it")

    reach_length_km = Param(
        door=doors.SCENARIO, default=12.0, bounds=(0.5, 15.0),
        units="km", consequence="aoi",
        desc="Modeled reach length downstream of the seed. It is also the LENGTH "
             "OF ONE PASS: the water grows algae for as long as it takes to "
             "travel this far, so a longer reach is a longer growing time")

    initial_phyto_ug_l = Param(
        door=doors.SCENARIO, default=2.0, bounds=(0.0, 500.0),
        units="ug/L", consequence="scenario",
        desc="Phytoplankton biomass in the water entering and filling the reach - "
             "the standing crop the pass grows FROM, and what the growth ratio "
             f"in the answer is held to. Stated, not fetched: `{_OBSERVED}` "
             "returns chlorophyll and nutrient sample SITES, and turning "
             "scattered sites into a reach-wide field is not a step this "
             "template takes on its own")
    initial_po4_mgl = Param(
        door=doors.SCENARIO, default=0.05, bounds=(0.0, 20.0),
        units="mg/L", consequence="scenario",
        desc="Dissolved orthophosphate entering and filling the reach - with "
             "nitrate this is what limits how much grows over the pass. Stated against "
             f"the observed record from `{_OBSERVED}` (characteristic "
             "'Phosphorus'), never fetched into the deck")
    initial_por_mgl = Param(
        door=doors.SCENARIO, default=0.02, bounds=(0.0, 20.0),
        units="mg/L", consequence="scenario",
        desc="Non-assimilable organic phosphorus - the phosphorus locked in dead "
             "material, released back to phosphate as it breaks down. Stated")
    initial_no3_mgl = Param(
        door=doors.SCENARIO, default=1.0, bounds=(0.0, 100.0),
        units="mg/L", consequence="scenario",
        desc="Dissolved nitrate entering and filling the reach - the other "
             "nutrient the growth is limited by. Stated against the observed "
             f"record from `{_OBSERVED}` (characteristic 'Nitrate')")
    initial_nor_mgl = Param(
        door=doors.SCENARIO, default=0.5, bounds=(0.0, 100.0),
        units="mg/L", consequence="scenario",
        desc="Non-assimilable organic nitrogen - the nitrogen locked in dead "
             "material, released back to nitrate as it breaks down. Stated")
    initial_nh4_mgl = Param(
        door=doors.SCENARIO, default=0.05, bounds=(0.0, 50.0),
        units="mg/L", consequence="scenario",
        desc="Ammonium entering and filling the reach; nitrifying it is one of "
             "the things that consumes oxygen. Stated")
    initial_organic_load_mgl = Param(
        door=doors.SCENARIO, default=2.0, bounds=(0.0, 500.0),
        units="mg/L", consequence="scenario",
        desc="Carbonaceous organic load (BOD) entering and filling the reach - "
             "the oxygen demand the water arrives with, before any the bloom "
             "creates for itself. Stated")

    water_temp_c = Param(
        door=doors.SCENARIO, default=22.0, bounds=(0.0, 40.0),
        units="C", consequence="scenario", user_lever=True,
        desc="Water temperature over the window. It sets the growth, mortality "
             "and nitrification rates AND the oxygen saturation the reach is "
             "measured against, so it is the single strongest lever here. Stated "
             f"against the observed record from `{_OBSERVED}` (characteristic "
             "'Temperature, water'), which this run fetches over the reach and "
             "publishes beside the answer; a run coupled to the thermal process "
             "reads a computed temperature field instead and ignores this number")
    sunshine_w_m2 = Param(
        door=doors.SCENARIO, default=100.0, bounds=(0.0, 400.0),
        units="W/m^2", consequence="scenario", user_lever=True,
        desc="Solar flux at the water surface, averaged over the window. Light "
             "is what drives the growth: at zero nothing grows at all. Stated, "
             "because the engine reads this from its own keyword and from "
             "nowhere else - the atmospheric data file's radiation columns feed "
             "the thermal heat budget and never this term")
    secchi_depth_m = Param(
        door=doors.USER, optional=True, bounds=(0.01, 30.0),
        units="m", consequence="scenario", user_lever=True,
        derived_when_absent="the engine's own clarity stands",
        desc="Secchi depth - how far light reaches down the water column. A "
             "turbid reach shades its own algae and blooms less")

    do_saturation_mgl = Param(
        door=doors.DERIVED,
        resolve=f"{_HELPERS}.water_quality.do_saturation_mgl",
        user_lever=True, bounds=(0.0, 20.0), units="mg/L", consequence="scenario",
        desc="DO saturation Cs at the stated water temperature; the ceiling the "
             "oxygen is measured against")
    initial_do_mgl = Param(
        door=doors.DERIVED,
        resolve=f"{_HELPERS}.water_quality.upstream_do_mgl",
        user_lever=True, bounds=(0.0, 20.0), units="mg/L", consequence="scenario",
        desc="Dissolved oxygen in the water entering and filling the reach; "
             "derived as saturation unless supplied")
    do_standard_mgl = Param(
        door=doors.SCENARIO, default=5.0, bounds=(0.0, 15.0),
        units="mg/L", consequence="scenario",
        desc="The DO water-quality standard the reach is judged against; 5 is a "
             "common warm-water aquatic-life criterion")

    sim_duration_s = Param(
        door=doors.SCENARIO, default=172800.0,
        bounds=(3600.0, 2.592e7), units="s", consequence="numerical",
        user_lever=True,
        desc="Simulated time. It has to cover several travel times through the "
             "reach before the along-reach answer has settled - the default is "
             "two days against a pass measured in hours. A river flushes far too "
             "fast to hold a seasonal bloom; ask a lake for that")
    # THE granularity lever, and always an explicit sheet value: no sizing rung
    # derives an edge from the channel, so the number the run meshes at is either
    # the user's or the labeled default a review can see and change. Coarser than
    # a plume template asks for, because this question is reach-scale chemistry
    # over a long window rather than a local feature at one instant.
    mesh_resolution_m = Param(
        door=doors.SCENARIO, default=25.0, user_lever=True,
        bounds=(3.0, 5000.0), units="m", consequence="numerical",
        desc="Target element edge length the reach is triangulated at; it also "
             "sets the CFL time step, so it is what decides whether a long "
             "window finishes")

DOC = dict(
    summary="NUTRIENT ENRICHMENT down a river reach: algal growth, nutrient "
            "drawdown and the oxygen response over one pass.",
    routing=(
        "THE tool for \"what does this reach do to its nutrient load\", \"how much "
        "algae grows down this river\", \"eutrophication of this stream\", \"does "
        "the algal growth pull the dissolved oxygen down\". Solves TELEMAC-2D + "
        "WAQTEL EUTRO over a REAL NHDPlus reach: phytoplankton growing on STATED "
        "nitrate and phosphate under stated light and temperature, dying back, and "
        "the oxygen budget that growth, that decay and the nitrification drive. "
        "Answers the LONGITUDINAL change over one pass - biomass, nutrients and "
        "oxygen down the reach against the water that entered. Supply `location` "
        "OR `bbox`."
    ),
    not_for=(
        "the oxygen sag below a WASTEWATER DISCHARGE (`telemac_do_sag`); a "
        "conservative dye or contaminant plume that only dilutes "
        "(`telemac_river_dye`); a SEASONAL bloom in standing water, which a river "
        "cannot hold - it flushes in hours. Nutrients are STATED, never fetched"
    ),
    params=PARAMS,
    controls=(
        ("input_mode",
         '"user_gated" presents the resolved carrier discharge and every stated '
         'concentration for review/edit before the solve and WAITS; "auto" '
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
        "On success the run's record (an `AnswerLayerURI`): every variable its "
        "modules wrote - the eight tracers the eutrophication process appends "
        "among them - styled on one mesh layer, animated where it varies, plus "
        "the biomass and the oxygen down the reach as charts and their history "
        "where the station was picked. `answer` carries `phyto_max_ug_l` / "
        "`phyto_max_distance_m` / `phyto_growth_ratio` (how many times the "
        "entering standing crop multiplied over one pass) / `no3_remaining_ratio` "
        "and `po4_remaining_ratio` (the fraction of the entering nitrate and "
        "phosphate still in the water at the drawn-down end) / `do_min_mgl` / "
        "`do_min_distance_m` / `do_below_standard` (vs `do_standard_mgl`) / "
        "`pass_velocity_mps` (the speed the pass was made at); narrate those "
        "typed numbers. On failure a dict with `status=\"error\"` + `error_code`."
    ),
)
