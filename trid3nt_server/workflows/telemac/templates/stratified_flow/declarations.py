"""The CONTRACT of ``telemac3d_stratified_flow``: its declared params and its prose."""

from __future__ import annotations

from trid3nt_server.inputs import Point
from trid3nt_server.workflows.runtime import Param, doors

__all__ = ["DOC", "PARAMS"]


class PARAMS:
    """What only a vertical-structure question asks: the column it opens with,
    the wind that decides whether it survives, and which body of water it is.

    The domain, the bed and the granularity are the runtime's own slots and
    levers; the deck's roughness, its tracer scheme and its output cadence are
    keywords the module's dictionary describes."""

    # -- which body of water -------------------------------------------------- #
    seed = Param(
        door=doors.USER, optional=True, user_lever=True,
        consequence="scenario", type=Point,
        derived_when_absent=(
            "the domain polygon supplied on the call is the body of water the "
            "run solves over"),
        desc="A point ON or beside the body of water this question is about, "
             "as a Point: the pick's {coordinates, name} verbatim, a (lon, lat) "
             "pair, 'lat,lon', a point layer, or a place name. The mapped "
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
    wind_speed_mps = Param(
        door=doors.SCENARIO, default=0.0, bounds=(0.0, 40.0),
        units="m/s", consequence="physics",
        desc="Sustained wind speed; 0 is CALM - the half of the pair in which the "
             "thermocline persists - and a nonzero value both mixes the column "
             "and drives the surface-downwind / return-flow-at-depth circulation "
             "reported beside the temperature")
    wind_direction_deg = Param(
        door=doors.SCENARIO, default=270.0,
        bounds=(0.0, 360.0), units="deg", consequence="scenario",
        desc="Compass bearing the wind blows FROM (0=N, 90=E, 270=W)")

    # -- how finely the column and the water body are resolved ---------------- #
    levels = Param(
        door=doors.SCENARIO, default=13, bounds=(5.0, 30.0),
        type=int, user_lever=True, consequence="numerical",
        desc="Number of vertical sigma levels - the degree of freedom a 2D model "
             "does not have, so it is THE resolution lever here; too few for the "
             "declared thermocline is a refusal, not a coarser answer")
    # The runtime's own lever, restated ONLY for its default: 14 m over a lake is
    # a mesh nothing this question asks needs, because a vertical-structure
    # question is resolution-bound in the VERTICAL.
    mesh_resolution_m = Param(
        door=doors.SCENARIO, default=120.0, bounds=(20.0, 5000.0),
        units="m", consequence="numerical", user_lever=True,
        desc="Target triangle edge the water body's interior is meshed at. The "
             "horizontal spends its budget on COVERING the body rather than on "
             "detail; the 3D node count is this mesh's nodes times the levels")

    # -- the window (the advanced fold) --------------------------------------- #
    # CONSTANT, not SCENARIO: the window is a settling time, not a scenario. The
    # answer is the column's SETTLED state, so this is "long enough". The user
    # keeps the lever.
    sim_duration_s = Param(
        door=doors.CONSTANT, default=18000.0, bounds=(3600.0, 86400.0),
        units="s", consequence="numerical",
        desc="Simulated duration - long enough for the column to settle or mix")


DOC = dict(
    summary="The 3D VERTICAL STRUCTURE of a body of water a 2D depth-averaged "
            "model cannot resolve.",
    routing=(
        "THE tool for \"does this lake stratify or turn over\", \"thermal "
        "stratification / thermocline\", \"wind-driven vertical "
        "circulation\", \"surface-vs-bottom currents\". TELEMAC-3D "
        "baroclinic coupling over sigma layers on the CLOSED body of water "
        "it solves over - a lake, a reservoir, a pond, a pit - which names "
        "no liquid boundary. ONE run gives both the temperature difference "
        "that SURVIVES and the surface-downwind / return-flow-at-depth "
        "velocities the same wind drives. Name or point at the body and "
        "`seed` finds its mapped outline; `domain` takes a polygon drawn or "
        "held, and `bed` a survey raster, soundings, or a depth in metres."
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
         '"user_gated" presents the resolved column, the wind and the authored '
         'mesh for review/edit before the solve and WAITS; "auto" (session '
         "default) proceeds with every assumption labeled. Not a physical value."),
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
        "plus the temperature column at the deepest node charted against the "
        "prescribed initial column. Its `answer` carries `stratification_dt` "
        "(the surviving top-to-bottom difference) against `stratification_dt_init`, "
        "the depth-weighted `column_mean_final_c` / `column_mean_init_c` whose "
        "drift is the numerical error bar on the mixing, `column_depth_m`, and the "
        "`u_surface` / `u_bottom` / `depth_avg_u` triple the same wind drove; "
        "narrate those typed numbers. The run exchanges NO heat with the "
        "atmosphere, so a falling surface temperature is downward MIXING and never "
        "the water cooling - narrate it that way. On failure a dict with "
        "`status=\"error\"` + `error_code`."
    ),
)
