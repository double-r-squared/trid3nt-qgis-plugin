"""The CONTRACT of ``telemac_micropollutant_release``: its declared params and its prose."""

from __future__ import annotations

from trid3nt_server.inputs import Point
from trid3nt_server.workflows.runtime import Accepts, Param, doors

__all__ = ["ACCEPTS", "DOC", "PARAMS"]

#: What a run of this question can be HANDED. TELEMAC-2D solves on triangles, so
#: a triangulation is the whole of what the domain can be handed as a mesh. The
#: substance enters the water at a POINT, which is the one release geometry this
#: pipeline has been run against.
ACCEPTS = Accepts(mesh=("unstructured_tri",), release=("point",))


class PARAMS:
    """What only a sorbing-substance question asks: where and for how long it
    enters, and where downstream the history is read. The domain, the bed, its
    boundary runs and the granularity are the runtime's own slots and levers,
    and the clock, the roughness, the cadence, the source keywords and the
    sorption constants are keywords the module's dictionary describes."""

    # -- the release -------------------------------------------------------- #
    release = Param(
        door=doors.USER, optional=True, user_lever=True,
        consequence="scenario", type=Point,
        derived_when_absent=(
            "the release sits at release_fraction along the domain the mesh was "
            "built over; the travelled distance is measured from there"),
        desc="Where the substance enters the water, as a Point: the pick's "
             "{coordinates, name} verbatim, a (lon, lat) pair, 'lat,lon', a "
             "point layer. Geocode a place name first. On a river with no domain "
             "it is also the seed the reach is walked downstream from")
    release_fraction = Param(
        door=doors.SCENARIO, default=0.1, bounds=(0.05, 0.9),
        consequence="scenario",
        desc="Along-domain release position, 0=inflow..1=outflow; the source "
             "must sit strictly INSIDE the domain, never on a boundary. It sits "
             "near the top so the substance has water left to sorb and settle in")
    release_duration_s = Param(
        door=doors.SCENARIO, default=3600.0,
        bounds=(1.0, 86400.0), units="s", consequence="scenario",
        desc="Finite injection window; the substance is released over it and the "
             "rest of the run is what happens to what was released")

    # -- where the history is read ------------------------------------------- #
    monitoring_point = Param(
        door=doors.USER, optional=True, user_lever=True,
        consequence="scenario", type=Point,
        derived_when_absent=(
            "the history is read at monitoring_fraction along the domain the "
            "mesh was built over"),
        desc="Where the dissolved history is read, as a Point: the pick's "
             "{coordinates, name} verbatim, a (lon, lat) pair, 'lat,lon', a "
             "point layer. Geocode a place name first")
    monitoring_fraction = Param(
        door=doors.SCENARIO, default=0.85, bounds=(0.1, 0.95),
        consequence="scenario",
        desc="Along-domain position the history is read at when no point was "
             "given, 0=inflow..1=outflow")


DOC = dict(
    summary="A SORBING substance released into water: how much stays DISSOLVED and how much ends up ON THE BED.",
    routing=(
        "THE tool for \"where does this pollutant END UP\" - a metal, a PCB, a "
        "pesticide, anything that ATTACHES TO SEDIMENT: how much travels "
        "dissolved and how much rides the sediment onto the bed. "
        "TELEMAC-2D + WAQTEL micropol over a reach walked from the release "
        "point, a basin or harbour on the canvas, or a supplied polygon: "
        "a finite point release, the water's own suspended sediment as the "
        "sorbent. Deck opinion: DURATION 172800 s, two "
        "days against a partition that equilibrates in hours; WAQTEL's own "
        "sorption and decay constants stand. Give `release` as a pick "
        "or a pair, or supply `domain`."
    ),
    not_for=(
        "a CONSERVATIVE tracer that only dilutes (`telemac_dye_release`); an OIL "
        "slick (`telemac_oil_spill`); bed SCOUR (`telemac_bed_scour`); a "
        "suspended SEDIMENT plume carrying nothing (`telemac_sediment_plume`); "
        "a dissolved-oxygen sag (`telemac_do_sag`); groundwater contamination"
    ),
    params=PARAMS,
    controls=(
        ("input_mode",
         '"user_gated" presents the resolved scenario sheet for review/edit and asks '
         'for the release point and the monitoring point on the canvas before the '
         'solve, and WAITS; "auto" (session default) proceeds with every assumption '
         "labeled. Not a physical value."),
        ("restart_clean",
         "True discards any ledger left under this same invocation and re-runs "
         "every step from the top. Nothing a FAILED attempt left behind is ever "
         "replayed and a run that completed is never replayed either, so a fresh "
         "invocation always re-solves against live upstream data; what this flag "
         "clears is the records a process that died without unwinding left on "
         "disk."),
    ),
    returns=(
        "On success the run's record (a `LayerURI`): every variable its "
        "modules wrote, styled on one mesh layer, animated where it varies, "
        "plus the dissolved history at the monitoring point charted. On "
        "failure a dict with `status=\"error\"` + `error_code`."
    ),
)
