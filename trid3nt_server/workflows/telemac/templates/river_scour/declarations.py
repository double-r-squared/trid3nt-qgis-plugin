"""The CONTRACT of ``telemac_river_scour``: its declared params and its prose.

One file over from the recipe. The rows every river run reads, and the rows every
point RELEASE reads, are the shared river part's; what is here is what only a
mobile-bed question asks.
"""

from __future__ import annotations

from trid3nt_server.workflows.runtime import Accepts, Param, doors
from trid3nt_server.workflows.telemac.helpers.substance import (
    GRAIN_UM_MAX,
    GRAIN_UM_MIN,
)

__all__ = ["ACCEPTS", "DOC", "PARAMS"]

#: What a mobile-bed run can be HANDED. The bed evolves on the same triangulation
#: the hydrodynamics runs on, so a lattice is refused at the door.
ACCEPTS = Accepts(mesh=("unstructured_tri",), release=("point",))


class PARAMS:
    """What only a scouring-bed question asks."""

    # -- the bed ------------------------------------------------------------ #
    grain_size_um = Param(
        door=doors.SCENARIO, default=200.0,
        bounds=(GRAIN_UM_MIN, GRAIN_UM_MAX), units="um", user_lever=True,
        consequence="scenario",
        desc="Median grain diameter d50 of the bed - ~200 um fine sand, ~20 um "
             "silt, ~8 um mud (all modeled non-cohesive); read only when no "
             "gradation is given")
    bed_thickness_m = Param(
        door=doors.SCENARIO, default=5.0, bounds=(0.05, 50.0),
        units="m", consequence="scenario",
        desc="Depth of the erodible sediment stock the bed can scour into")
    bedload_formula = Param(
        door=doors.SCENARIO, default=1, type=int, consequence="numerical",
        desc="GAIA bed-load law: 1=Meyer-Peter-Mueller, 2=Einstein-Brown, "
             "7=van Rijn")
    morphological_factor = Param(
        door=doors.SCENARIO, default=10.0, bounds=(1.0, 100.0),
        user_lever=True, consequence="numerical",
        desc="Amplifies bed change per hydraulic step so a short hydrograph "
             "yields a readable depth; a speed-up lever, not a rate")
    sediment_gradation = Param(
        door=doors.USER, optional=True, consequence="scenario",
        type=list | str,
        derived_when_absent=(
            "the bed is ONE class at grain_size_um, which is uniform by "
            "construction and cannot sort"),
        desc="Multi-class GRADED sediment: a preset name (graded_sand | "
             "poorly_sorted | sand_gravel_bimodal | fine_coarse_sand) or a list "
             "of [d50_um, fraction] pairs; a mixture sorts under a hiding factor")
    tracer_concentration_mgl = Param(
        door=doors.SCENARIO, default=100.0,
        bounds=(0.0, 1.0e6), units="mg/L", consequence="scenario",
        desc="Concentration of the marker tracer released at the source, which "
             "is what the deposited fraction is measured against")
    reach_length_km = Param(
        door=doors.SCENARIO, default=6.0, bounds=(0.5, 15.0),
        units="km", consequence="aoi",
        desc="Modeled reach length downstream of the release; a longer reach is "
             "coarsened under the mesh node budget")
    sim_duration_s = Param(
        door=doors.SCENARIO, default=3600.0,
        bounds=(600.0, 14400.0), units="s", consequence="numerical",
        desc="Simulated physical time; the morphological factor is what makes a "
             "short window produce a readable bed change")

    # -- channel maintenance dredging (NESTOR) ------------------------------ #
    dredging = Param(
        door=doors.SCENARIO, default=False, type=bool, consequence="scenario",
        desc="Arm the NESTOR channel-maintenance dig/dump rule on top of the "
             "mobile bed")
    dredge_mode = Param(
        door=doors.SCENARIO, default="scheduled", consequence="scenario",
        desc="Dredging rule: scheduled (remove a target volume over a window) | "
             "criterion (dig only where the bed silts within tolerance of grade)")
    dredge_volume_m3 = Param(
        door=doors.SCENARIO, default=4000.0, bounds=(1.0, 1.0e7),
        units="m^3", consequence="scenario",
        desc="Scheduled-mode target dredged volume")
    dredge_crit_depth_m = Param(
        door=doors.SCENARIO, default=0.3, bounds=(0.01, 20.0),
        units="m", consequence="scenario",
        desc="Criterion-mode siltation tolerance above the design grade")
    dredge_dig_depth_m = Param(
        door=doors.SCENARIO, default=1.5, bounds=(0.05, 30.0),
        units="m", consequence="scenario",
        desc="Criterion-mode dig target below the design grade")
    dredge_disposal = Param(
        door=doors.SCENARIO, default=False, type=bool, consequence="scenario",
        desc="Also place the dug spoil in a downstream disposal zone")
    dredge_bank_offset_m = Param(
        door=doors.SCENARIO, default=5.0,
        bounds=(0.0, 200.0), units="m", user_lever=True, consequence="scenario",
        desc="Bank setback the dig field is held back from the mapped water's "
             "edge, so the cut does not undercut the bank it is dug beside. It "
             "is also what excludes a stretch too narrow to dredge: narrower "
             "than twice the setback and no field survives there")

    # -- numerics + geometry (the advanced fold) ---------------------------- #
    mesh_resolution_m = Param(
        door=doors.SCENARIO, default=14.0, user_lever=True,
        bounds=(3.0, 5000.0), units="m", consequence="numerical",
        desc="Target element edge length the reach is triangulated at; scour "
             "depth is a resolution-bound class and a coarse mesh reads it low")


DOC = dict(
    summary="Bed SCOUR and DEPOSITION in a river reach: a mobile bed under a flow.",
    routing=(
        "THE tool for \"where does the bed scour and where does it re-deposit\" - "
        "erodible-bed morphodynamics below a dam, weir or bridge contraction, "
        "bedload transport and bed evolution under a flood, how a GRADED grain "
        "mixture sorts and armors, and channel-maintenance DREDGING against "
        "siltation (NESTOR dig/dump). TELEMAC-2D coupled with GAIA over a REAL "
        "NHDPlus reach with real NHDArea banks. Returns a bed-evolution map plus "
        "the time-stepped mesh. Supply `location` OR `bbox`."
    ),
    not_for=(
        "a SUSPENDED sediment plume settling onto an inert bed "
        "(`telemac_river_sediment_plume`); a conservative dye or contaminant "
        "plume (`telemac_river_dye`); an OIL slick "
        "(`telemac_river_oil_spill`); dissolved-oxygen sag (`telemac_do_sag`); "
        "rainfall-runoff flood depth (`telemac_rain_on_grid`)"
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
        "On success a `TelemacSedimentLayerURI` - the bed-evolution map plus the "
        "SELAFIN sibling the client animates. It carries `max_scour_mm` / "
        "`max_deposition_mm` / `deposited_mass_kg` / `deposit_fraction` and, on a "
        "graded bed, `sediment_surface_d50_range_um`; narrate those typed "
        "numbers. On failure a dict with `status=\"error\"` + `error_code`."
    ),
)
