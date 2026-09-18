"""The CONTRACT of ``telemac_dye_release``: its declared params and its prose."""

from __future__ import annotations

from trid3nt_server.inputs import Point
from trid3nt_server.workflows.runtime import Accepts, Param, doors

__all__ = ["ACCEPTS", "DECAY_PRESETS", "DOC", "PARAMS"]

#: The first-order die-off a named substance runs under, as the degradation law
#: and its coefficient: 1 is a T90 in hours, 2 a rate per hour. Bacterial words
#: carry T90 ~ 2 h, the daylight freshwater fecal-coliform die-off - a narrated
#: literature default, never a measured observation. A key matches as a
#: substring of the named word.
DECAY_PRESETS: dict[str, dict[str, float]] = {
    "sewage": {"law": 1, "coef": 2.0},
    "e. coli": {"law": 1, "coef": 2.0},
    "e.coli": {"law": 1, "coef": 2.0},
    "e coli": {"law": 1, "coef": 2.0},
    "ecoli": {"law": 1, "coef": 2.0},
    "coliform": {"law": 1, "coef": 2.0},
    "coli": {"law": 1, "coef": 2.0},
    "bacteria": {"law": 1, "coef": 2.0},
    "bacterial": {"law": 1, "coef": 2.0},
    "effluent": {"law": 1, "coef": 2.0},
    "wastewater": {"law": 1, "coef": 2.0},
    "die-off": {"law": 1, "coef": 2.0},
    "decaying": {"law": 2, "coef": 0.35},
    "half-life": {"law": 2, "coef": 0.35},
}

#: What a run of this question can be HANDED. TELEMAC-2D solves on triangles, so
#: a triangulation is the whole of what the domain can be handed as a mesh; a
#: lattice is refused at the door rather than trusted into a run that assumes
#: edges. The release enters the water at a POINT - the one release geometry this
#: plume pipeline has been run against.
ACCEPTS = Accepts(mesh=("unstructured_tri",), release=("point",))


class PARAMS:
    """What only a conservative-plume question asks: where the release is, what
    is released and how much of it, and whether it decays. The domain, the bed,
    its boundary runs and the granularity are the runtime's own slots and
    levers, and the deck's clock, roughness, cadence, wind and rain are keywords
    the module's dictionary describes."""

    release = Param(
        door=doors.USER, optional=True, user_lever=True,
        consequence="scenario", type=Point,
        derived_when_absent=(
            "the release sits at spill_fraction along the domain the mesh was "
            "built over; the travelled distance is measured from there"),
        desc="Where the substance enters the water, as a Point: the pick's "
             "{coordinates, name} verbatim, a (lon, lat) pair, 'lat,lon', a "
             "point layer. Geocode a place name first. Its name becomes the "
             "tracer's name, "
             "and on a river with no domain supplied it is also the seed the "
             "reach is walked downstream from")

    # -- the release -------------------------------------------------------- #
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
    dye_concentration_mgl = Param(
        door=doors.SCENARIO, default=100.0,
        bounds=(0.0, 1.0e6), units="mg/L", consequence="scenario",
        desc="Source concentration of the released substance")

    # -- decay, the one optional coupling this question carries -------------- #
    decaying_substance = Param(
        door=doors.QUESTION, optional=True, consequence="scenario",
        derived_when_absent=(
            "the tracer is CONSERVATIVE - it dilutes and advects and nothing "
            "removes it"),
        desc="Name a substance whose tracer DECAYS - sewage | E. coli | coliform "
             "| bacteria | effluent | wastewater - and its narrated literature "
             "die-off is applied as a first-order sink on the plume")

    # -- the state this run opens at ----------------------------------------- #
    continue_from = Param(
        door=doors.USER, optional=True, type=str,
        consequence="numerical",
        derived_when_absent=(
            "the run starts from its own initial conditions - a constant depth "
            "at rest - rather than from another run's state"),
        desc="Continue a previous run: the URI of its restart_domain.slf, the "
             "state at its last instant, which becomes this run's initial "
             "state - so DURATION is the time added ON TOP of it and the "
             "same declared scenario carries on over the longer horizon (a "
             "release whose spill_duration_s has elapsed stays finished). The "
             "mesh must be the same one, and a run that couples WAQTEL refuses")


DOC = dict(
    summary="A DYE / TRACER / CONTAMINANT plume released into a body of surface water and carried by its flow.",
    routing=(
        "THE tool for \"a spill in the water - how far does it travel, how "
        "concentrated\": a dye / contaminant / pollutant / chemical plume carried "
        "by the current, sewage or E.coli effluent DECAYING as it goes (name it "
        "in `decaying_substance`). TELEMAC-2D over a reach walked from the "
        "release, a lake drawn on the canvas, or a supplied polygon: a finite "
        "pulse is carried by the flow and dilutes. Give `release` as a pick, a "
        "pick or a pair, or supply `domain`. Deck: DURATION 3600 s, no WIND and "
        "no RAIN OR EVAPORATION - set each by its keyword name."
    ),
    not_for=(
        "an OIL slick (`telemac_oil_spill`); bed SCOUR, deposition, grain "
        "sorting (`telemac_bed_scour`); a SUSPENDED sediment plume settling "
        "onto the bed (`telemac_sediment_plume`); dissolved-oxygen sag "
        "(`telemac_do_sag`); rainfall-runoff flood depth "
        "(`telemac_rain_on_grid`). Groundwater plumes, dam-break and tsunami "
        "run-up are not modeled here"
    ),
    params=PARAMS,
    controls=(
        ("input_mode",
         '"user_gated" presents the resolved scenario sheet for review/edit and asks '
         'for the release point on the canvas before the solve, and WAITS; "auto" '
         "(session default) proceeds with every assumption labeled. Not a physical value."),
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
        "modules wrote, styled on one mesh layer, animated where it varies, "
        "plus the dye series charted. Its `answer` carries "
        "`dye_cmax_mgl` / `dye_peak_time_s` / `plume_reach_m` / `active_frames`; "
        "narrate those typed numbers. On failure a dict with `status=\"error\"` + "
        "`error_code`."
    ),
)
