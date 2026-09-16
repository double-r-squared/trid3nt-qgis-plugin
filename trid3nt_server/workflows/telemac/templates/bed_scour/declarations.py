"""The CONTRACT of ``telemac_bed_scour``: what only a mobile-bed question asks."""

from __future__ import annotations

from trid3nt_server.inputs import Point
from trid3nt_server.workflows.runtime import Accepts, Param, doors, lever
from trid3nt_server.workflows.telemac.modules.gaia import GRAIN_UM_MAX, GRAIN_UM_MIN

__all__ = ["ACCEPTS", "DOC", "GRADATION_PRESETS", "PARAMS"]

#: Named gradations (d50 in microns, initial fraction) a mixture can be asked for
#: by name - honest demo mixes, never a measured site sieve curve. The fractions
#: are renormalized on use.
GRADATION_PRESETS: dict[str, list[list[float]]] = {
    "graded_sand": [[100.0, 0.34], [400.0, 0.33], [1000.0, 0.33]],
    "poorly_sorted": [[80.0, 0.4], [300.0, 0.3], [1200.0, 0.3]],
    "sand_gravel_bimodal": [[200.0, 0.5], [1800.0, 0.5]],
    "fine_coarse_sand": [[120.0, 0.5], [800.0, 0.5]],
}

#: What a mobile-bed run can be HANDED. The bed evolves on the same triangulation
#: the hydrodynamics runs on, so a lattice is refused at the door.
ACCEPTS = Accepts(mesh=("unstructured_tri",), release=("point",))


class PARAMS:
    """What only a scouring-bed question asks: the sediment the bed is made of,
    the marker the bed change is watched against, and the flow that moves both.

    The domain, the bed elevation, the boundary runs and the granularity are the
    runtime's own slots and levers; the deck's roughness and its output cadence
    are keywords the module's dictionary describes."""

    release = Param(
        door=doors.USER, optional=True, user_lever=True,
        consequence="scenario", type=Point,
        derived_when_absent=(
            "the marker sits at spill_fraction along the domain the mesh was "
            "built over"),
        desc="Where the marker enters the water, as a Point: the pick's "
             "{coordinates, name} verbatim, a (lon, lat) pair, 'lat,lon', a "
             "point layer, or a place name. Its name becomes the marker's name, "
             "and on a river with no domain supplied it is also the seed the "
             "reach is walked downstream from")

    # -- the marker the bed change is watched against ----------------------- #
    spill_fraction = Param(
        door=doors.SCENARIO, default=0.25, bounds=(0.05, 0.9),
        consequence="scenario",
        desc="Along-domain release position, 0=inflow..1=outflow; the source "
             "must sit strictly INSIDE the domain, never on a boundary")
    spill_duration_s = Param(
        door=doors.SCENARIO, default=300.0,
        bounds=(1.0, 86400.0), units="s", consequence="scenario",
        desc="Finite pulse injection window")
    source_q_m3s = Param(
        door=doors.SCENARIO, default=8.0, bounds=(0.5, 30.0),
        units="m^3/s", consequence="scenario",
        desc="Point-source discharge of the release itself, small against the "
             "carrier flow")
    tracer_concentration_mgl = Param(
        door=doors.SCENARIO, default=100.0,
        bounds=(0.0, 1.0e6), units="mg/L", consequence="scenario",
        desc="Concentration of the marker tracer released at the source, which "
             "is what the deposited fraction is measured against")

    # -- the forcings this question adds ------------------------------------ #
    wind_speed_mps = Param(
        door=doors.SCENARIO, default=0.0, bounds=(0.0, 60.0),
        units="m/s", consequence="scenario",
        desc="Sustained wind driving a surface wind-stress term; 0 = no wind")
    wind_direction_deg = Param(
        door=doors.SCENARIO, default=0.0, bounds=(0.0, 360.0),
        units="deg", consequence="scenario",
        desc="Compass bearing the wind blows FROM (0=N, 90=E); only read when "
             "wind_speed_mps > 0")
    # ONE signed rate, because the keyword it writes is signed: a net loss is a
    # negative rain rather than a second value subtracted from this one.
    rainfall_mm_per_day = Param(
        door=doors.USER, optional=True, bounds=(-50.0, 2000.0),
        units="mm/day", consequence="scenario",
        desc="NET distributed rainfall applied at every wet node, independent of "
             "the inflow hydrograph; negative is evaporation")

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

    # -- the clock ---------------------------------------------------------- #
    sim_duration_s = lever(
        "sim_duration_s", bounds=(600.0, 14400.0),
        desc="Simulated physical time; the morphological factor is what makes a "
             "short window produce a readable bed change")


DOC = dict(
    summary="Bed SCOUR and DEPOSITION under a body of water: a mobile bed under a flow.",
    routing=(
        "THE tool for \"where does the bed scour and where does it re-deposit\" - "
        "erodible-bed morphodynamics below a dam, weir or bridge contraction, "
        "bedload transport and bed evolution under a flood, and how a GRADED grain "
        "mixture sorts and armors. TELEMAC-2D coupled with GAIA over the domain "
        "the run solves on - a reach walked from the release point, an estuary "
        "drawn on the canvas, or a supplied polygon - its bed painted from a "
        "published channel survey where one covers it. Give `release` as a place, "
        "a pick or a pair, or supply `domain`."
    ),
    not_for=(
        "a SUSPENDED sediment plume settling onto an inert bed "
        "(`telemac_sediment_plume`); a conservative dye or contaminant "
        "plume (`telemac_dye_release`); an OIL slick "
        "(`telemac_oil_spill`); a maintenance DREDGE of a fairway "
        "(`telemac_channel_dredging`); dissolved-oxygen sag (`telemac_do_sag`); "
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
        "On success the run's record (an `AnswerLayerURI`): every variable its "
        "modules wrote, styled on one mesh layer, animated where it varies, "
        "the bed evolution in metres among them (deposition positive, scour "
        "negative), plus the marker series charted. Its `answer` carries "
        "`bed_evolution_max_m` / "
        "`bed_evolution_min_m` / `net_bed_mass_kg` / `surface_d50_spread_m` (the "
        "bed's sorting signature, zero on a single class) / "
        "`marker_cmax_mgl`; narrate those typed numbers. On failure a dict with `status=\"error\"` + "
        "`error_code`."
    ),
)
