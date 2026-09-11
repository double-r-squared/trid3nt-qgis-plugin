"""The CONTRACT of ``telemac_river_oil_spill``: what only an oil question asks."""

from __future__ import annotations

from typing import Any

from trid3nt_server.workflows.inputs import Point
from trid3nt_server.workflows.runtime import Accepts, Param, doors

__all__ = ["ACCEPTS", "DOC", "OIL_PRESETS", "PARAMS"]

#: The oil presets the deck carries, in the module reader's own terms: the
#: (mass fraction, boiling point) components, the aromatic rows with their
#: solubility and dissolution and volatilisation rates, then the density,
#: viscosity, spreading volume, ambient temperature and spreading law. The
#: fractions sum to one per preset.
OIL_PRESETS: dict[str, dict[str, Any]] = {
    "light_crude": dict(
        compo=[(0.5, 645.0), (0.3, 830.0)],
        hap=[(0.2, 673.0, 0.018, 1.0e-5, 5.0e-5)],
        rho=850.0, eta=1.0e-5, voldev=20.0, tamb=288.0, etal=1),
    "diesel": dict(
        compo=[(0.6, 560.0), (0.25, 700.0)],
        hap=[(0.15, 610.0, 0.005, 1.0e-5, 8.0e-5)],
        rho=840.0, eta=4.0e-6, voldev=10.0, tamb=288.0, etal=1),
    "heavy_fuel": dict(
        compo=[(0.75, 900.0), (0.2, 1050.0)],
        hap=[(0.05, 800.0, 0.001, 5.0e-6, 1.0e-5)],
        rho=960.0, eta=5.0e-4, voldev=30.0, tamb=288.0, etal=1),
}

#: What an oil run can be HANDED. TELEMAC-2D solves on triangles, so a
#: triangulation is the whole of what a reach corridor can be handed as a mesh.
#: The release enters the water at a POINT - floats released in shallow margins
#: or against a wall are dropped by the module, so the point is snapped for
#: clearance before it is compiled into the release routine.
ACCEPTS = Accepts(mesh=("unstructured_tri",), release=("point",))


class PARAMS:
    """What only an oil-slick question asks."""
    release = Param(
        door=doors.USER, optional=True, user_lever=True,
        consequence="scenario", type=Point,
        derived_when_absent=(
            "the release sits at spill_fraction along the meshed reach; the "
            "downstream distance is measured from there"),
        desc="Where the substance enters the water, as a Point: the pick's "
             "{coordinates, name} verbatim, a (lon, lat) pair, 'lat,lon', a "
             "point layer, or a place name")

    # -- the scenario ------------------------------------------------------- #
    oil_type = Param(
        door=doors.QUESTION, default="light_crude", consequence="scenario",
        desc="Which preset was spilled: light_crude | diesel | heavy_fuel - the "
             "module's own composition, density and viscosity. Crude runs as "
             "light_crude, gasoline and petrol as diesel, bunker as heavy_fuel")
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
        "On success the peak dissolved-oil layer (a `LayerURI`) - the emitter "
        "loads the map, animates the result mesh beside it and draws the slick "
        "track from the floats - whose `answer` carries `oil_cmax_mgl` / "
        "`oil_peak_time_s` / `plume_reach_m` / `slick_drift_m` / "
        "`floats_released` / `floats_remaining`; narrate those typed numbers. "
        "On failure a dict with `status=\"error\"` + `error_code`."
    ),
)
