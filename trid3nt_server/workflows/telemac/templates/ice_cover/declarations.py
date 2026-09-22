"""The CONTRACT of the ice-cover question: its declared params and prose."""

from __future__ import annotations

from trid3nt_server.inputs import Point
from trid3nt_server.workflows.runtime import Accepts, Param, doors, lever

__all__ = ["ACCEPTS", "DOC", "PARAMS"]

#: What a freeze-up run can be HANDED. KHIONE solves on its host's own nodes and
#: the cover is a SURFACE the whole domain carries, so a triangulation is the
#: whole of it.
ACCEPTS = Accepts(mesh=("unstructured_tri",))


class PARAMS:
    """What only a freeze-up question asks: which kind of water the seed is
    on and where to cut it when no domain is handed in, where the cover series
    is read, and how much cover counts as frozen. The domain, the bed, what the
    water opens at, the moment the cold snap is read at and the granularity are
    the runtime's own slots and levers, and the clock, the cadence, the friction
    and every ice process are keywords the dictionaries describe and the decks
    state by their own names."""

    seed = Param(
        door=doors.USER, optional=True, consequence="aoi", type=Point,
        derived_when_absent=(
            "nothing is fetched and the domain is the polygon the caller "
            "supplied or drew"),
        desc="Where on the water the modelled stretch STARTS, as a Point: the "
             "pick's {coordinates, name} verbatim, a (lon, lat) pair, 'lat,lon', "
             "a point layer. Geocode a place name first. It seeds the reach the "
             "domain is cut from; supply the domain polygon - a lake, a pond, a "
             "reservoir - instead and this is not read")
    station = Param(
        door=doors.USER, optional=True, consequence="scenario",
        user_lever=True, type=Point,
        derived_when_absent=(
            "the series is read at the point 98% of the way down the modelled "
            "domain, which is the water that has been under the cold longest"),
        desc="Where the ice cover and its thickness are read over time, as a "
             "Point: the pick's {coordinates, name} verbatim, a (lon, lat) pair, "
             "'lat,lon' or a point layer. Geocode a place name first")
    # A REPORTING threshold rather than a physical constant: how much of the
    # water surface has to be under ice before the ask calls the water frozen.
    # The measure it drives is a crossing time, so the number is the question's
    # own definition and nothing in the physics reads it.
    cover_threshold = Param(
        door=doors.QUESTION, optional=True, default=0.5,
        consequence="scenario", user_lever=True, type=float,
        desc="Fraction of the surface under ice, 0 to 1, that counts as frozen "
             "over - the answer reports the first moment the cover exceeds it, "
             "at the point and anywhere in the domain. A reporting threshold, "
             "not a physical constant: half the surface by default")

    # The runtime's own granularity lever, restated ONLY for its default: a
    # surface heat budget is divided by the local depth and watched over days,
    # and the edge also sets the CFL step, so the runtime's 14 m spends the
    # whole compute budget resolving a planform this question reads only at its
    # banks. Bounds and meaning are the lever's.
    mesh_resolution_m = lever(
        "mesh_resolution_m", default=20.0,
        desc="Target element edge length the domain is triangulated at; the "
             "budget that makes the ice is divided by the local DEPTH, and the "
             "border ice grows from the BANK, so what this has to resolve is "
             "how deep the water is and where its edge runs")


DOC = dict(
    summary="ICE COVER under a cold snap: when water freezes over, and how "
            "thick.",
    routing=(
        "THE tool for \"when does this water freeze over\", \"how thick does "
        "the ice get\", \"frazil and border ice\". KHIONE on "
        "TELEMAC-2D over the domain it is given, drawn or matched at `seed`: "
        "the heat budget under the hourly airport record over the run's "
        "window, the frazil it makes, and the cover that grows from it. "
        "Produces cover fraction and thickness, animated and charted. "
        "Deck opinions, by keyword: DURATION (seven days), GRAPHIC "
        "PRINTOUT PERIOD, LAW OF BOTTOM FRICTION, FRICTION COEFFICIENT; on the "
        "ice deck ATMOSPHERE-WATER EXCHANGE MODEL, DYNAMIC ICE COVER, MODEL FOR "
        "MASS EXCHANGE BETWEEN FRAZIL AND ICE COVER, BORDER ICE COVER. Supply "
        "the domain or `seed`, and `event_time`, the moment the snap opens "
        "at."
    ),
    not_for=(
        "how warm the water gets with no ice in the question "
        "(`telemac_water_temperature`); snow or ice on LAND; air temperature "
        "or a forecast, which the weather fetchers answer"
    ),
    params=PARAMS,
    controls=(
        ("input_mode",
         '"user_gated" presents the resolved discharge, the weather station and '
         'the opening water temperature for review/edit before the solve and '
         'WAITS; "auto" (session default) proceeds with every assumption '
         "labeled. Not a physical value."),
        ("restart_clean",
         "True discards any ledger left under this same invocation and re-runs "
         "every step from the top. Nothing a FAILED attempt left behind is ever "
         "replayed and a run that completed is never replayed either, so a fresh "
         "invocation always re-solves against live upstream data."),
    ),
    returns=(
        "On success the run's record (an `AnswerLayerURI`): every variable its "
        "modules wrote, styled on one mesh layer, animated where it varies, plus "
        "the cover and thickness series charted at the station. `answer` carries "
        "`freeze_time_s` (the first moment the cover at the point exceeds "
        "`cover_threshold`) / `domain_freeze_time_s` (the first moment anywhere "
        "in the domain does) / `peak_ice_thickness_m` / `final_cover_fraction`; "
        "narrate those typed numbers. A water that never reaches the threshold "
        "answers with no crossing time, and water that makes no ice at all "
        "answers zero thickness and zero cover - both are honest answers "
        "about that place in that week. On failure a dict with "
        "`status=\"error\"` + `error_code`."
    ),
)
