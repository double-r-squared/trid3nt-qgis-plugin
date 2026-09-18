"""The CONTRACT of ``telemac_bed_scour``: what only a mobile-bed question asks."""

from __future__ import annotations

from trid3nt_server.inputs import Point
from trid3nt_server.workflows.runtime import Accepts, Param, doors

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
    """What only a scouring-bed question asks: where the marker enters the water,
    the pulse it is released as, and the GRADATION a mixed bed sorts under.

    The domain, the bed elevation, the boundary runs and the granularity are the
    runtime's own slots and levers; the clock, the roughness, the cadence and the
    bed's own sediment constants are keywords the module's dictionary describes."""

    release = Param(
        door=doors.USER, optional=True, user_lever=True,
        consequence="scenario", type=Point,
        derived_when_absent=(
            "the marker sits at spill_fraction along the domain the mesh was "
            "built over"),
        desc="Where the marker enters the water, as a Point: the pick's "
             "{coordinates, name} verbatim, a (lon, lat) pair, 'lat,lon', a "
             "point layer. Geocode a place name first. Its name becomes the "
             "marker's name, "
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

    # -- the bed ------------------------------------------------------------ #
    # The class diameter, the erodible stock, the transport law and the
    # morphological factor are keywords GAIA's dictionary carries and the deck
    # states by name. What is left here is the GRADATION the dictionary has no
    # keyword for: a named mixture, or the pairs one is written as.
    sediment_gradation = Param(
        door=doors.USER, optional=True, consequence="scenario",
        type=list | str,
        derived_when_absent=(
            "the bed is ONE class at the CLASSES SEDIMENT DIAMETERS the deck "
            "states, which is uniform by construction and cannot sort"),
        desc="Multi-class GRADED sediment: a preset name (graded_sand | "
             "poorly_sorted | sand_gravel_bimodal | fine_coarse_sand) or a list "
             "of [d50_um, fraction] pairs; a mixture sorts under a hiding factor")


DOC = dict(
    summary="Bed SCOUR and DEPOSITION: a mobile bed under moving water.",
    routing=(
        "THE tool for \"where does the bed scour and where does it re-deposit\" - "
        "erodible-bed morphodynamics below a dam, weir or bridge, bedload "
        "under a flood, and how a GRADED mixture sorts and armors. "
        "TELEMAC-2D + GAIA over a reach walked from the release point, an "
        "estuary you draw, or a polygon you supply, its bed painted from a "
        "published channel survey where one covers it. Deck opinions, by "
        "keyword: DURATION 3600 s, MORPHOLOGICAL FACTOR 10, CLASSES SEDIMENT "
        "DIAMETERS 2e-4 m, LAYERS INITIAL THICKNESS 5 m, BED-LOAD TRANSPORT "
        "FORMULA FOR ALL SANDS 1. Give `release`, or supply `domain`."
    ),
    not_for=(
        "a SUSPENDED plume settling onto an inert bed "
        "(`telemac_sediment_plume`); a conservative dye or contaminant plume "
        "(`telemac_dye_release`); an OIL slick (`telemac_oil_spill`); a "
        "maintenance DREDGE (`telemac_channel_dredging`); dissolved-oxygen sag "
        "(`telemac_do_sag`); rainfall-runoff flood depth "
        "(`telemac_rain_on_grid`)"
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
