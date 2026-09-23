"""The CONTRACT of ``telemac3d_stratified_flow``: its declared params and its prose."""

from __future__ import annotations

from trid3nt_server.inputs import Point
from trid3nt_server.workflows.runtime import Param, doors, lever

__all__ = ["DOC", "PARAMS"]


class PARAMS:
    """What only a vertical-structure question asks: the column it opens with,
    and which body of water it is.

    The domain, the bed and the granularity are the runtime's own slots and
    levers; the deck's clock, its plane count, its wind, its roughness, its
    tracer scheme and its output cadence are keywords the module's dictionary
    describes."""

    # -- which body of water -------------------------------------------------- #
    seed = Param(
        door=doors.USER, optional=True, user_lever=True,
        consequence="scenario", type=Point,
        derived_when_absent=(
            "the domain polygon supplied on the call is the body of water the "
            "run solves over"),
        desc="A point ON or beside the body of water this question is about, "
             "as a Point: the pick's {coordinates, name} verbatim, a (lon, lat) "
             "pair, 'lat,lon' or a point layer (geocode a place name first). "
             "The mapped "
             "outline that point names becomes the domain; a domain supplied "
             "directly supersedes it, and a body nobody mapped is drawn")

    # -- the column ---------------------------------------------------------- #
    warm_temp_c = Param(
        door=doors.SCENARIO, default=25.0, bounds=(-2.0, 40.0),
        units="C", consequence="physics",
        desc="Epilimnion (warm surface layer) temperature the column OPENS at. "
             "The run exchanges no heat with the atmosphere, so what happens to "
             "this difference is the whole answer")
    cold_temp_c = Param(
        door=doors.SCENARIO, default=15.0, bounds=(-2.0, 40.0),
        units="C", consequence="physics",
        desc="Hypolimnion (cold bottom layer) temperature; the initial "
             "top-to-bottom difference is what the run either keeps or mixes away")
    thermocline_depth_m = Param(
        door=doors.SCENARIO, default=8.0,
        bounds=(0.5, 200.0), units="m", consequence="physics",
        desc="Depth of the thermocline below the free surface. The vertical grid "
             "is planned to HOLD it and REFUSES when no admissible sigma stretch "
             "over the domain's deepest column can")

    # -- how finely the water body is resolved ------------------------------- #
    # The runtime's own lever, restated ONLY for its default: 14 m over a lake is
    # a mesh nothing this question asks needs, because a vertical-structure
    # question is resolution-bound in the VERTICAL.
    mesh_resolution_m = lever(
        "mesh_resolution_m", default=120.0, bounds=(20.0, 5000.0),
        desc="Target triangle edge the water body's interior is meshed at. The "
             "horizontal spends its budget on COVERING the body rather than on "
             "detail; the 3D node count is this mesh's nodes times the planes "
             "the deck states")


DOC = dict(
    summary="The 3D VERTICAL STRUCTURE of a body of water a 2D depth-averaged "
            "model cannot resolve.",
    routing=(
        "THE tool for \"does this lake stratify or turn over\", \"thermal "
        "stratification / thermocline\", \"wind-driven vertical "
        "circulation\". TELEMAC-3D baroclinic coupling over sigma layers on a "
        "CLOSED body of water - a lake, a reservoir, a pond, a pit - naming no "
        "liquid boundary. ONE run gives both the temperature difference that "
        "SURVIVES and the opposed surface / bottom velocities a stated wind "
        "drives. `seed` names or points at the body and finds its mapped "
        "outline; `domain` takes a polygon, `bed` a survey raster, soundings "
        "or a depth in metres. Deck: DURATION 18000 s, NUMBER OF HORIZONTAL "
        "LEVELS 13, calm; set each by its keyword name."
    ),
    not_for=(
        "a 2D depth-averaged plume or transport question; inundation "
        "DEPTH; coastal storm-tide flooding; harbour wave agitation "
        "(`artemis_harbor_agitation`); a salinity intrusion up an estuary, "
        "which needs a tidal liquid boundary a closed body has none of"
    ),
    params=PARAMS,
    controls=(
        ("input_mode",
         '"user_gated" presents the resolved column, the deck it is solved '
         'under and the authored mesh for review/edit before the solve and '
         'WAITS; "auto" (session default) proceeds with every assumption '
         "labeled. Not a physical value."),
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
        "module wrote, styled on one mesh layer, animated where it varies, "
        "plus the temperature column at the deepest node charted against "
        "the prescribed initial column. The run exchanges NO heat with the "
        "atmosphere, so a falling surface temperature is downward MIXING "
        "and never the water cooling - narrate it that way. On failure a "
        "dict with `status=\"error\"` + `error_code`."
    ),
)
