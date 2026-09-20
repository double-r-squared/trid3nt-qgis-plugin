"""The CONTRACT of ``artemis_harbor_agitation``: its declared params and its prose."""

from __future__ import annotations

from trid3nt_server.workflows.runtime import Accepts, Param, doors, lever

__all__ = ["ACCEPTS", "DEFAULT_BARRIER_WIDTH_M", "DEFAULT_GRADE",
           "DEFAULT_OPEN_DEPTH_M", "DOC", "PARAMS"]

#: What may be SUPPLIED to this template instead of built. A triangulation is
#: the only mesh an elliptic mild-slope solve reads, and the boundary numbering
#: the incident wave is stamped onto is the pair writer's own.
ACCEPTS = Accepts(mesh=("unstructured_tri",))

#: How fast the edge may grow out of the structure band. The coarsest edge
#: defaults to ten times the finest inside the mesher, and this limits how fast
#: one becomes the other.
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
    # -- the world ---------------------------------------------------------- #
    # NOT params. The water body, what its nodes carry for elevation and the
    # thing that shelters are SLOTS (``DATA`` in agitation.py): the domain is a
    # polygon the user outlines or the coastline cut produces; the bed is a
    # surface, a survey or a depth; and the structure is a polyline the template
    # names no source for, because naming a default source for somebody's
    # breakwater is an opinion the question does not carry.

    # -- the incident wave -------------------------------------------------- #
    # Its PERIOD and its DIRECTION are keywords the deck states. What is left
    # here is the pair ARTEMIS reads out of the BOUNDARY CONDITIONS FILE per
    # node, which the dictionary carries no keyword for.
    wave_height_m = Param(
        door=doors.SCENARIO, default=1.0, bounds=(0.01, 10.0),
        units="m", consequence="physics",
        desc="Incident wave height H0 on the designated liquid boundary; Kd is "
             "measured against it, so it sets the scale of every narrated height")
    reflection_coef = Param(
        door=doors.SCENARIO, default=0.5, bounds=(0.0, 1.0),
        consequence="physics",
        desc="The declared structure's reflection coefficient: 1 fully reflecting "
             "(a vertical quay), 0 fully absorbing (a rubble slope). Every other "
             "solid face is the absorbing shore")

    # -- the domain (the granularity lever) --------------------------------- #
    # The runtime's own granularity lever, restated for the default and the floor
    # a phase-resolving solve needs: a harbour is read at the shoreline and
    # around the structure, an order finer than an open-water domain.
    mesh_resolution_m = lever(
        "mesh_resolution_m", default=8.0, bounds=(2.0, 500.0),
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
    transect_length_m = Param(
        door=doors.SCENARIO, default=1500.0, bounds=(100.0, 20000.0),
        units="m", consequence="scenario",
        desc="The whole length of the transect the agitation is read along: a "
             "straight line through the structure's centroid along the incident "
             "wave direction, half of it on the exposed side and half in the lee")
    open_depth_threshold_m = Param(
        door=doors.SCENARIO, default=DEFAULT_OPEN_DEPTH_M,
        bounds=(-200.0, -1.0), units="m", consequence="physics",
        desc="How deep a boundary stretch must reach for it to be designated the "
             "OPEN edge the incident wave enters through; every stretch that "
             "reaches it opens")
    compute_class = lever("compute_class")


DOC = dict(
    summary="The WAVE AGITATION (Kd = Hs/H0) a declared structure leaves inside a "
            "harbour, marina or sheltered basin.",
    routing=(
        "THE tool for \"does this breakwater shelter the berths\", \"how much does "
        "swell amplify inside this harbour\", \"wave agitation / tranquility in the "
        "basin\". ARTEMIS phase-RESOLVING elliptic mild-slope (Berkhoff) over a "
        "mesh cut from the real shoreline, the structure punched out and the "
        "seaward stretches open: fringes and standing waves are the answer, not "
        "an average. Wave: `WAVE PERIOD` 8 s, `DIRECTION OF WAVE PROPAGATION` 90 "
        "deg (trig from +x) - set either by name. THE STRUCTURE IS THE "
        "QUESTION and is REQUIRED: `structure=` a breakwater layer "
        "(`fetch_osm_breakwaters`) or a drawn line. Give `domain=` the water as "
        "an outline or a polygon layer, or `extent=` a rectangle the "
        "coastline cuts into water."
    ),
    not_for=(
        "the offshore SEA STATE or fetch-limited wind-wave growth; free-field "
        "agitation with no structure; coastal storm-tide flooding; a "
        "tracer released into a river channel"
    ),
    params=PARAMS,
    controls=(
        ("extent",
         "NOT needed when `domain=` is filled - this is the other way to say "
         "where the water is. A rectangle over the harbour as a LAYER (a uri or "
         "a file, not four numbers): the mapped coastline divides it and the "
         "water it leaves IS the domain. A box a coastline way crosses without "
         "closing divides nothing and refuses by name rather than meshing a "
         "shape nobody cut."),
        ("domain",
         "A harbour approach, a marina basin, or any water a structure shelters. "
         "Its own edge is the SHORELINE the mesh is sized against, so a rough "
         "outline is enough; unfilled, the extent above is cut instead."),
        ("bed",
         "That producer is NOAA CUDEM nearshore topobathy over the domain - the "
         "surveyed sea floor the wave refracts over. Hand it your own survey "
         "raster, a layer of soundings, or a depth in metres for a basin nobody "
         "has sounded."),
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
        "On success the run's record (an `AnswerLayerURI`): every variable its "
        "module wrote, styled on one mesh layer, animated where it varies, "
        "the derived agitation coefficient Kd = Hs/H0 among them, plus the Kd "
        "profile through the structure charted. Its `answer` carries `kd_max`, "
        "`kd_transect_min` and `kd_transect_max` (Kd along that transect - the "
        "lee against the exposed approach), `hs_max_m` and `mesh_size_m`; "
        "narrate those typed numbers. kd_max is often a standing wave against "
        "the domain's own open boundary rather than a harbour answer, so read "
        "the transect and the field behind the structure. On failure a dict "
        "with `status=\"error\"` + `error_code`."
    ),
)
