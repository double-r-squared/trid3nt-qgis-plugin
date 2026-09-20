"""The CONTRACT of the nearshore-wave question: its declared params and prose."""

from __future__ import annotations

from trid3nt_server.inputs import Point
from trid3nt_server.workflows.runtime import Accepts, Param, doors, lever

__all__ = ["ACCEPTS", "DOC", "PARAMS"]

#: What a nearshore run can be HANDED. The wave field is solved over the whole
#: body of water between the shore and the offshore edge, so a triangulation is
#: the whole of it.
ACCEPTS = Accepts(mesh=("unstructured_tri",))


class PARAMS:
    """What only a nearshore-wave question asks: where offshore the sea state
    that forces the open edge is measured, and where inshore the waves are read.
    The window, the coast, the bed, the tide, the moment and the granularity are
    the runtime's own slots and levers, and the spectral grid, the clock, the
    cadence, the breaking and the bottom friction are keywords the dictionary
    describes and the deck states by their own names."""

    seed = Param(
        door=doors.USER, optional=True, consequence="aoi", type=Point,
        derived_when_absent=(
            "the buoy nearest the centre of the water the window was cut to "
            "is the one the open edge is forced at"),
        desc="Where OFFSHORE the incoming sea state is measured, as a Point: "
             "the pick's {coordinates, name} verbatim, a (lon, lat) pair, "
             "'lat,lon', a point layer. Geocode a place name first. The nearest "
             "buoy to it is the record the open boundary is forced at, so put "
             "it on the water the swell arrives across")
    station = Param(
        door=doors.USER, user_lever=True, consequence="scenario", type=Point,
        desc="Where INSHORE the waves are read over time, as a Point: the "
             "pick's {coordinates, name} verbatim, a (lon, lat) pair, "
             "'lat,lon', a point layer. Geocode a place name first. The height, "
             "the period and the direction are charted at the node of the mesh "
             "it settles onto, so put it on the water you are asking about")

    # The runtime's own granularity lever, restated ONLY for its default. What
    # this question is about happens where the depth falls - the shoaling, then
    # the breaking - so the edge has to resolve the slope of the bed between the
    # offshore edge and the shore rather than the planform of the coast.
    mesh_resolution_m = lever(
        "mesh_resolution_m", default=30.0,
        desc="Target element edge length the water is triangulated at; the "
             "waves shoal and then break where the DEPTH falls, so what this "
             "has to resolve is the slope of the bed across the surf zone")


DOC = dict(
    summary="NEARSHORE WAVES: what the offshore swell becomes at the shore - "
            "how high, how long, which way, and where it breaks.",
    routing=(
        "THE tool for \"how big are the waves at the beach\", \"what does this "
        "swell do as it comes in\", \"where does the surf break here\", \"wave "
        "height and period along this coast\". TOMAWAC, the spectral wave "
        "model, over the water the window is cut to at the coastline: the open "
        "edge forced at a sea state a buoy MEASURED, shoaling and refraction "
        "over the charted bed, breaking and bottom friction taking them down. "
        "Produces the height, the periods, the directions and the breaking "
        "band, animated, three of them charted where the ask points. Deck "
        "opinions, by keyword: TIME STEP, NUMBER OF TIME STEP, the spectral "
        "grid, DEPTH-INDUCED BREAKING DISSIPATION. Supply the window, "
        "`station`, and `event_time`."
    ),
    not_for=(
        "agitation behind a breakwater, which is phase-resolving "
        "(`artemis_harbor_agitation`); storm surge or the water level itself; "
        "the buoy record alone (`fetch_ndbc_buoys`)"
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
        "On success the run's record (an `AnswerLayerURI`): every variable "
        "TOMAWAC wrote, styled on one mesh layer and animated, plus the height, "
        "the period and the direction charted at the station. `answer` carries "
        "`hs_max_m` (the highest wave anywhere in the domain) / "
        "`hs_at_station_m` / `peak_period_at_station_s` / "
        "`direction_at_station_deg` (the bearing the waves run TOWARD) / "
        "`breaking_rate_max_per_s` and `breaker_dissipation_max_m2s` (how hard "
        "the surf zone is working - the BAND it works in is the picture) / "
        "`mesh_size_m`; narrate those typed numbers. A coast the swell reaches "
        "calmly answers a small height and no breaking, which is an honest "
        "answer about that place in that hour. On failure a dict with "
        "`status=\"error\"` + `error_code`."
    ),
)
