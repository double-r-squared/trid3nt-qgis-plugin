"""The CONTRACT of ``telemac_river_sediment_plume``: its declared params + prose.

One file over from the recipe. The rows every river run reads, and the rows every
point RELEASE reads, are the shared river part's; what is here is what only a
suspended-plume question asks.
"""

from __future__ import annotations

from trid3nt_server.workflows.runtime import Accepts, Param, doors
from trid3nt_server.workflows.telemac.helpers.substance import (
    GRAIN_UM_MAX,
    GRAIN_UM_MIN,
)

__all__ = ["ACCEPTS", "DOC", "PARAMS"]

#: What a suspended-plume run can be HANDED. The settling class rides the same
#: triangulation the hydrodynamics runs on, so a lattice is refused at the door.
ACCEPTS = Accepts(mesh=("unstructured_tri",), release=("point",))


class PARAMS:
    """What only a settling-plume question asks."""

    # -- the released class -------------------------------------------------- #
    grain_size_um = Param(
        door=doors.SCENARIO, default=200.0,
        bounds=(GRAIN_UM_MIN, GRAIN_UM_MAX), units="um", user_lever=True,
        consequence="scenario",
        desc="Median grain diameter d50 of the RELEASED class - ~200 um fine "
             "sand settles within a few km, ~20 um silt mostly stays suspended "
             "(all modeled non-cohesive)")
    sediment_concentration_mgl = Param(
        door=doors.SCENARIO, default=100.0,
        bounds=(0.0, 1.0e6), units="mg/L", consequence="scenario",
        desc="Concentration of the released suspended sediment; what deposits is "
             "measured against what this put in")
    reach_length_km = Param(
        door=doors.SCENARIO, default=6.0, bounds=(0.5, 15.0),
        units="km", consequence="aoi",
        desc="Modeled reach length downstream of the release; a longer reach is "
             "coarsened under the mesh node budget")
    sim_duration_s = Param(
        door=doors.SCENARIO, default=3600.0,
        bounds=(600.0, 14400.0), units="s", consequence="numerical",
        desc="Simulated physical time")

    # -- numerics + geometry (the advanced fold) ---------------------------- #
    mesh_resolution_m = Param(
        door=doors.SCENARIO, default=14.0, user_lever=True,
        bounds=(3.0, 5000.0), units="m", consequence="numerical",
        desc="Target element edge length the reach is triangulated at; peak "
             "concentration is a resolution-bound class and a coarse mesh reads "
             "it low")


DOC = dict(
    summary="A SUSPENDED SEDIMENT plume in a RIVER: it settles and deposits on the bed.",
    routing=(
        "THE tool for \"sediment / silt / a turbidity plume released into the "
        "river, where does it settle out\" - a slurry spill, a construction or "
        "dredging discharge, a prescribed upstream sediment supply depositing "
        "downstream. TELEMAC-2D coupled with GAIA over a REAL NHDPlus reach: ONE "
        "settling class over a bed with NO stock, so nothing erodes and only what "
        "was injected can deposit. Returns a peak suspended-concentration map, "
        "the deposition pattern and the time-stepped mesh. Supply `location` OR "
        "`bbox`."
    ),
    not_for=(
        "bed SCOUR, an erodible bed, grain sorting or dredging "
        "(`telemac_river_scour`); a conservative dye or contaminant plume "
        "(`telemac_river_dye`); an OIL slick (`telemac_river_oil_spill`); "
        "dissolved-oxygen sag (`telemac_do_sag`)"
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
        "On success a `TelemacSedimentLayerURI` - the peak suspended-concentration "
        "map plus the SELAFIN sibling the client animates. It carries "
        "`max_deposition_mm` / `deposited_mass_kg` / `deposit_fraction`; narrate "
        "those typed numbers. On failure a dict with `status=\"error\"` + "
        "`error_code`."
    ),
)
