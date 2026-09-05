"""The CONTRACT of ``telemac_river_oil_spill``: its declared params and its prose.

One file over from the recipe. The rows every river run reads, and the rows every
point RELEASE reads, are the shared river part's; what is here is what only an
oil question asks.
"""

from __future__ import annotations

from trid3nt_server.workflows.runtime import Accepts, Param, doors

__all__ = ["ACCEPTS", "DOC", "PARAMS"]

#: What an oil run can be HANDED. TELEMAC-2D solves on triangles, so a
#: triangulation is the whole of what a reach corridor can be handed as a mesh.
#: The release enters the water at a POINT - floats released in shallow margins
#: or against a wall are dropped by the module, so the point is snapped for
#: clearance before it is compiled into the release routine.
ACCEPTS = Accepts(mesh=("unstructured_tri",), release=("point",))


class PARAMS:
    """What only an oil-slick question asks."""

    # -- the scenario ------------------------------------------------------- #
    oil_type = Param(
        door=doors.QUESTION, default="crude", consequence="scenario",
        desc="What was spilled - crude | diesel | gasoline | heavy fuel | bunker "
             "- which picks the module's own composition, density and viscosity "
             "preset")
    oil_concentration_mgl = Param(
        door=doors.SCENARIO, default=100.0,
        bounds=(0.0, 1.0e6), units="mg/L", consequence="scenario",
        desc="Concentration of the DISSOLVED fraction released with the slick, "
             "carried as the reach's tracer")
    reach_length_km = Param(
        door=doors.SCENARIO, default=6.0, bounds=(0.5, 15.0),
        units="km", consequence="aoi",
        desc="Modeled reach length downstream of the release; a longer reach is "
             "coarsened under the mesh node budget")
    sim_duration_s = Param(
        door=doors.SCENARIO, default=3600.0,
        bounds=(600.0, 14400.0), units="s", consequence="numerical",
        desc="Simulated physical time")

    # -- the slick ---------------------------------------------------------- #
    n_drogues = Param(
        door=doors.SCENARIO, default=100, bounds=(1.0, 20000.0), type=int,
        user_lever=True, consequence="scenario",
        desc="How many floating particles the module tracks; the slick is drawn "
             "from their positions, so a coarse count draws a coarse slick")
    drogues_period_s = Param(
        door=doors.SCENARIO, default=60.0, bounds=(1.0, 3600.0), units="s",
        consequence="numerical",
        desc="How often the particle positions are written; it converts to a "
             "count of solver steps at the run's own timestep")
    oil_release_step = Param(
        door=doors.SCENARIO, default=600, bounds=(1.0, 1.0e6), type=int,
        consequence="scenario",
        desc="The solver step the floats are released at, compiled into the "
             "module's own release routine; it lets the flow field establish "
             "before the slick is put on it")

    # -- numerics + geometry (the advanced fold) ---------------------------- #
    # THE granularity lever, and always an explicit sheet value: no sizing rung
    # derives an edge from the channel, so the number the run meshes at is either
    # the user's or the labeled default a review can see and change.
    mesh_resolution_m = Param(
        door=doors.SCENARIO, default=14.0, user_lever=True,
        bounds=(3.0, 5000.0), units="m", consequence="numerical",
        desc="Target element edge length the reach is triangulated at; where the "
             "slick reaches is a front location and moves with the elements that "
             "carry it")


DOC = dict(
    summary="An OIL SLICK released into a RIVER: floating particles plus the dissolved fraction.",
    routing=(
        "THE tool for \"an oil spill on the river, where does the slick go\" - a "
        "barge or pipeline release of crude, diesel, gasoline or heavy fuel into a "
        "river reach. TELEMAC-2D over a REAL NHDPlus reach with real NHDArea banks, "
        "with the engine's own oil-spill module riding on the solve: floating "
        "particles are tracked and drawn as the slick, and the dissolved fraction "
        "is advected as the reach's tracer. Supply `location` OR `bbox`."
    ),
    not_for=(
        "a conservative dye or contaminant plume with no slick "
        "(`telemac_river_dye`); bed SCOUR or dredging (`telemac_river_scour`); a "
        "SUSPENDED sediment plume (`telemac_river_sediment_plume`); "
        "dissolved-oxygen sag (`telemac_do_sag`). Weathering, evaporation and "
        "beaching are the module's own and are not calibrated here"
    ),
    params=PARAMS,
    controls=(
        ("input_mode",
         '"user_gated" presents the filled sheet for review/edit before the solve '
         'and WAITS; "auto" (session default) proceeds with every assumption '
         "labeled. Not a physical value."),
        ("restart_clean",
         "True discards any ledger left under this same invocation and re-runs "
         "every step from the top."),
    ),
    returns=(
        "On success a `TelemacDyeLayerURI` - the emitter loads the peak "
        "dissolved-fraction map, animates the SELAFIN sibling and draws the slick "
        "from the particle track. Narrate the typed numbers it carries. On failure "
        "a dict with `status=\"error\"` + `error_code`."
    ),
)
