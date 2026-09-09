"""The CONTRACT of ``artemis_harbor_agitation``: its declared params and its prose."""

from __future__ import annotations

from trid3nt_server.workflows.runtime import Accepts, Param, doors

__all__ = ["ACCEPTS", "DEFAULT_BARRIER_WIDTH_M", "DEFAULT_GRADE",
           "DEFAULT_MIN_EDGE_M", "DEFAULT_OPEN_DEPTH_M", "DOC",
           "HARBOR_HALF_DEG", "PARAMS"]

#: What may be SUPPLIED to this template instead of built. A triangulation is
#: the only mesh an elliptic mild-slope solve reads, and the boundary numbering
#: the incident wave is stamped onto is the pair writer's own.
ACCEPTS = Accepts(mesh=("unstructured_tri",))

#: A harbour approach is small: ~0.06 deg (~6 km) around a geocoded quay is the
#: open-water box the sheltering question lives in.
HARBOR_HALF_DEG = 0.06

#: The finest triangle edge, where the shoreline and the structure are. The
#: coarsest defaults to ten times it inside the mesher and the gradation op limits
#: how fast one becomes the other.
DEFAULT_MIN_EDGE_M: float = 8.0
DEFAULT_GRADE: float = 0.2

#: The WIDTH the mapped structure centreline is given, in metres. A survey maps a
#: rubble-mound breakwater as a LINE and a line bounds no area, so subtracting it
#: from the water removes nothing and the triangulation closes straight over it.
#: The declared width is what the mesher can actually punch out - a labeled
#: modelling choice, in the range a harbour-of-refuge mound occupies at the
#: waterline.
DEFAULT_BARRIER_WIDTH_M: float = 20.0

#: How deep a boundary stretch must be for the mesher's library to read it as
#: open. EVERY stretch that reaches it opens: a harbour with two mouths forced at
#: one of them is a harbour the swell can only enter through half of.
DEFAULT_OPEN_DEPTH_M: float = -12.0


class PARAMS:
    # -- the question ------------------------------------------------------- #
    location = Param(
        door=doors.QUESTION, optional=True, consequence="aoi",
        desc="Harbour or coastal place near the AOI (e.g. 'Point Judith, Rhode "
             "Island'), geocoded")
    bbox = Param(
        door=doors.USER, optional=True, consequence="aoi",
        type=tuple[float, float, float, float] | list[float] | str,
        desc="Explicit AOI (min_lon,min_lat,max_lon,max_lat) EPSG:4326 - the "
             "harbour approach the domain is cut from the shoreline inside")

    # -- the structure -----------------------------------------------------  #
    # NOT a Param. The thing that shelters is a SUPPLIED SLOT (``DATA`` in
    # agitation.py): the template says it accepts a polyline and says nothing
    # about where one comes from, because naming a default source for somebody's
    # breakwater is an opinion the question does not carry.

    # -- the incident wave -------------------------------------------------- #
    wave_period_s = Param(
        door=doors.SCENARIO, default=8.0, bounds=(1.0, 300.0),
        units="s", consequence="physics",
        desc="Incident monochromatic wave period - a PRESCRIBED demo forcing, "
             "since no wave-forcing fetcher exists yet")
    wave_height_m = Param(
        door=doors.SCENARIO, default=1.0, bounds=(0.01, 10.0),
        units="m", consequence="physics",
        desc="Incident wave height H0 on the designated liquid boundary; Kd is "
             "measured against it, so it sets the scale of every narrated height")
    wave_direction_deg = Param(
        door=doors.SCENARIO, default=90.0,
        bounds=(0.0, 360.0), units="deg", consequence="scenario",
        desc="Incident wave direction in the TRIG convention (0 = +X east, "
             "90 = +Y north) - not the compass bearing")
    reflection_coef = Param(
        door=doors.SCENARIO, default=0.5, bounds=(0.0, 1.0),
        consequence="physics",
        desc="The declared structure's reflection coefficient: 1 fully reflecting "
             "(a vertical quay), 0 fully absorbing (a rubble slope). Every other "
             "solid face is the absorbing shore")

    # -- the domain (the granularity lever) --------------------------------- #
    mesh_min_edge_m = Param(
        door=doors.SCENARIO, default=DEFAULT_MIN_EDGE_M,
        bounds=(2.0, 500.0), units="m", user_lever=True, consequence="numerical",
        desc="Finest triangle edge, used at the shoreline and around the "
             "structure. THE granularity lever: a phase-resolving solve needs "
             "several nodes per WAVELENGTH and Kd peaks inside a diffraction "
             "fringe, so a coarse mesh reads the peaks low")
    mesh_grade = Param(
        door=doors.CONSTANT, default=DEFAULT_GRADE, bounds=(0.05, 0.5),
        consequence="numerical",
        desc="Mesh gradation: how fast the edge may grow from the structure band "
             "out to the open approach")
    barrier_width_m = Param(
        door=doors.SCENARIO, default=DEFAULT_BARRIER_WIDTH_M,
        bounds=(1.0, 200.0), units="m", consequence="numerical",
        desc="The width the mapped structure centreline is cut at; a survey maps "
             "a mound as a line and a line removes no water from the domain")
    open_depth_threshold_m = Param(
        door=doors.SCENARIO, default=DEFAULT_OPEN_DEPTH_M,
        bounds=(-200.0, -1.0), units="m", consequence="physics",
        desc="How deep a boundary stretch must reach for it to be designated the "
             "OPEN edge the incident wave enters through; every stretch that "
             "reaches it opens")
    compute_class = Param(
        door=doors.CONSTANT, default="medium",
        consequence="numerical", desc="Solve sizing class")


DOC = dict(
    summary="The WAVE AGITATION (Kd = Hs/H0) a declared structure leaves inside a "
            "harbour.",
    routing=(
        "THE tool for \"does this breakwater shelter the berths\", \"how much does "
        "swell amplify inside this harbour\", \"wave agitation / tranquility in the "
        "basin\", \"diffraction behind a breakwater\". ARTEMIS phase-RESOLVING "
        "elliptic mild-slope (Berkhoff) over a mesh cut from the real shoreline "
        "with the structure punched out conformally and the seaward stretches "
        "designated open: diffraction fringes and standing waves are the answer, "
        "not an average. THE STRUCTURE IS THE QUESTION and is REQUIRED: pass "
        "`structure=` a breakwater layer (`fetch_osm_breakwaters`) or a drawn "
        "line. Supply a harbour `location` or `bbox`."
    ),
    not_for=(
        "the offshore SEA STATE or fetch-limited wind-wave growth; free-field "
        "agitation with no structure in it; coastal storm-tide flooding; a river "
        "plume (`telemac_river_dye`)"
    ),
    params=PARAMS,
    controls=(
        ("structure",
         "REQUIRED. The barrier the question is about, as a polyline LAYER (the "
         "uri or handle from fetch_osm_breakwaters, or any line layer the user "
         "has) or a drawn/typed line as [[lon, lat], ...]. Producer-less BY "
         "DESIGN - this tool will never go and find a structure you did not name. "
         "It is cut out of the water domain at `barrier_width_m` and its faces "
         "reflect `reflection_coef` of the incident energy."),
        ("input_mode",
         '"user_gated" presents the resolved incident wave, the structure and the '
         'authored mesh for review/edit before the solve and WAITS; "auto" '
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
        "On success an `ArtemisAgitationLayerURI` (a `LayerURI` subtype) - the "
        "emitter loads the Kd COG and animates the ARTEMIS SELAFIN sibling. It "
        "carries `kd_max` / `kd_sheltered` / `kd_exposed` and the transect through "
        "the structure's own shadow strip; narrate those typed numbers. On failure "
        "a dict with `status=\"error\"` + `error_code`."
    ),
)
