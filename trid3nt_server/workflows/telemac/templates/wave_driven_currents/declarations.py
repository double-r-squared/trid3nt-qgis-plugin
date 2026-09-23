"""The CONTRACT of the wave-driven-current question: its declared params and prose."""

from __future__ import annotations

from trid3nt_server.inputs import Point
from trid3nt_server.workflows.runtime import Accepts, Param, doors, lever

__all__ = ["ACCEPTS", "DEFAULT_OPEN_DEPTH_M", "DOC", "PARAMS"]

#: What a wave-driven-current run can be HANDED. The current and the waves are
#: solved over the same body of water on the same triangulation - that is what
#: same-mesh coupling means - so a supplied mesh is the whole of both.
ACCEPTS = Accepts(mesh=("unstructured_tri",))

#: How deep a boundary stretch has to reach for the library to designate it the
#: OPEN edge the sea state is imposed across. EVERY stretch that reaches it
#: opens: a coastal window is open on its seaward side and along both ends, and
#: the library's own default is set for a shelf-scale domain rather than for a
#: window whose deepest node is a few tens of metres down.
DEFAULT_OPEN_DEPTH_M: float = -12.0


class PARAMS:
    """What only a wave-driven-current question asks: where offshore the sea
    state driving the coast is measured, and where inshore the current is read.
    The window, the coast, the bed, the tide, the moment and the granularity are
    the runtime's own slots and levers, and the clock, the spectral grid, the
    breaking and the friction are keywords the two decks state by their own
    names - the wave deck's under ``tomawac:``."""

    seed = Param(
        door=doors.USER, optional=True, consequence="aoi", type=Point,
        derived_when_absent=(
            "the buoy nearest the centre of the water the window was cut to "
            "is the one the wave deck's open edge is forced at"),
        desc="Where OFFSHORE the incoming sea state is measured, as a Point: "
             "the pick's {coordinates, name} verbatim, a (lon, lat) pair, "
             "'lat,lon', a point layer. Geocode a place name first. The nearest "
             "buoy to it is the record the wave boundary is forced at, so put "
             "it on the water the swell arrives across")
    station = Param(
        door=doors.USER, user_lever=True, consequence="scenario", type=Point,
        desc="Where INSHORE the current is read over time, as a Point: the "
             "pick's {coordinates, name} verbatim, a (lon, lat) pair, "
             "'lat,lon', a point layer. Geocode a place name first. The speed "
             "and the wave height are charted at the node of the mesh it "
             "settles onto, so put it in the surf zone you are asking about")

    # The runtime's own granularity lever, restated ONLY for its default and its
    # floor. The current is driven by the GRADIENT of the radiation stress
    # across the surf zone, so the edge has to resolve that band; the floor is
    # the finest edge this deck's own stated time step stays stable at.
    mesh_resolution_m = lever(
        "mesh_resolution_m", default=40.0, bounds=(20.0, 5000.0),
        desc="Target element edge length the water is triangulated at; the "
             "longshore current is driven across the surf zone, so what this "
             "has to resolve is the width of the breaking band")

    open_depth_threshold_m = Param(
        door=doors.SCENARIO, default=DEFAULT_OPEN_DEPTH_M,
        bounds=(-200.0, -1.0), units="m", consequence="physics",
        desc="How deep a boundary stretch must reach for it to be designated "
             "the OPEN edge the measured sea state enters through; every "
             "stretch that reaches it opens, and a window where none does has "
             "no edge for the spectrum and refuses")


DOC = dict(
    summary="WAVE-DRIVEN CURRENTS: the current breaking waves drive along the "
            "shore - how fast and which way.",
    routing=(
        "THE tool for \"how strong is the longshore current here\", \"which way "
        "does the surf push along this beach\", \"what current do these waves "
        "set up\". TELEMAC-2D over the water the window is cut to at the "
        "coastline, COUPLED to TOMAWAC on the same mesh: the waves break, the "
        "gradient of their radiation stress enters the momentum equation, and "
        "the current that results is the answer. The wave edge is forced at a "
        "sea state a buoy MEASURED; the water stands at a gauge's tide. Both "
        "fields on one mesh, animated, the speed and the wave height charted "
        "where the ask points. Deck opinions, by keyword: TIME STEP, DURATION, "
        "COUPLING PERIOD FOR TOMAWAC, and the wave deck's under `tomawac:`. "
        "Supply the window, `station`, `event_time`."
    ),
    not_for=(
        "the waves alone, with no current solved "
        "(`tomawac_nearshore_waves`); agitation behind a breakwater "
        "(`artemis_harbor_agitation`); a current with no waves in it"
    ),
    params=PARAMS,
    controls=(
        ("input_mode",
         '"user_gated" presents the resolved sea state, the tide and the mesh '
         'for review/edit before the solve and WAITS; "auto" (session default) '
         "proceeds with every assumption labeled. Not a physical value."),
        ("restart_clean",
         "True discards any ledger left under this same invocation and re-runs "
         "every step from the top. Nothing a FAILED attempt left behind is ever "
         "replayed and a run that completed is never replayed either, so a fresh "
         "invocation always re-solves against live upstream data."),
    ),
    returns=(
        "On success the run's record (a `LayerURI`): every variable "
        "TELEMAC-2D wrote and every variable TOMAWAC wrote, styled on one "
        "mesh layer and animated, plus the current speed and the wave "
        "height charted at the station. A coast the swell reaches head-on "
        "charts a small longshore speed, which is what that place did in "
        "that hour. On failure a dict with `status=\"error\"` + `error_code`."
    ),
)
