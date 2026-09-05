"""The CONTRACT of ``telemac_river_dye``: its declared params and its prose.

One file over from the recipe. ``river_dye.py`` reads on one page - the question,
the data, the plan, the answer, the chart - because the fifty rows that describe
every value it can take live here instead of in front of them.
"""

from __future__ import annotations

from trid3nt_server.workflows.runtime import Accepts, Param, doors

__all__ = ["ACCEPTS", "DOC", "PARAMS"]

#: What a reach run can be HANDED. TELEMAC-2D solves on triangles, so a
#: triangulation is the whole of what a reach corridor can be handed as a mesh; a
#: lattice is refused at the door rather than trusted into a run that assumes
#: edges. The release enters the water at a POINT - the one release geometry this
#: plume pipeline has been run against.
ACCEPTS = Accepts(mesh=("unstructured_tri",), release=("point",))


class PARAMS:
    """What only a conservative-plume question asks. The rows every river run
    reads, and the rows every point RELEASE reads, are the shared river part's.
    """

    # -- the scenario ------------------------------------------------------- #
    dye_concentration_mgl = Param(
        door=doors.SCENARIO, default=100.0,
        bounds=(0.0, 1.0e6), units="mg/L", consequence="scenario",
        desc="Source concentration of the released substance")
    reach_length_km = Param(
        door=doors.SCENARIO, default=6.0, bounds=(0.5, 15.0),
        units="km", consequence="aoi",
        desc="Modeled reach length downstream of the release; a longer reach is "
             "coarsened under the mesh node budget")
    sim_duration_s = Param(
        door=doors.SCENARIO, default=3600.0,
        bounds=(600.0, 14400.0), units="s", consequence="numerical",
        desc="Simulated physical time")

    # -- decay, the one optional coupling this question carries -------------- #
    decaying_substance = Param(
        door=doors.QUESTION, optional=True, consequence="scenario",
        derived_when_absent=(
            "the tracer is CONSERVATIVE - it dilutes and advects and nothing "
            "removes it"),
        desc="Name a substance whose tracer DECAYS - sewage | E. coli | coliform "
             "| bacteria | effluent | wastewater - and its narrated literature "
             "die-off is applied as a first-order sink on the plume")
    decay_half_life_hours = Param(
        door=doors.USER, optional=True, bounds=(0.1, 720.0),
        units="h", user_lever=True, consequence="scenario",
        desc="First-order half-life of the released substance; unset uses the "
             "narrated literature default for decaying_substance, and neither "
             "leaves the tracer conservative")
    decay_rate_per_day = Param(
        door=doors.USER, optional=True, bounds=(0.01, 100.0),
        units="1/day", user_lever=True, consequence="scenario",
        desc="Decay rate per day, as an alternative to the half-life")

    # -- numerics + geometry (the advanced fold) ---------------------------- #
    # THE granularity lever, and always an explicit sheet value: no sizing rung
    # derives an edge from the channel, so the number the run meshes at is either
    # the user's or the labeled default a review can see and change.
    mesh_resolution_m = Param(
        door=doors.SCENARIO, default=14.0, user_lever=True,
        bounds=(3.0, 5000.0), units="m", consequence="numerical",
        desc="Target element edge length the reach is triangulated at; peak "
             "concentration is a resolution-bound class and a coarse mesh reads "
             "it low")
    continue_from = Param(
        door=doors.USER, optional=True, type=str,
        consequence="numerical",
        derived_when_absent=(
            "the run starts from its own initial conditions - a constant depth "
            "at rest - rather than from another run's state"),
        desc="Continue a previous run: the URI of its restart_river.slf, the "
             "state at its last instant, which becomes this run's initial "
             "state - so sim_duration_s is the time added ON TOP of it and the "
             "same declared scenario carries on over the longer horizon (a "
             "release whose spill_duration_s has elapsed stays finished). The "
             "mesh must be the same one, and a run that couples WAQTEL refuses")


DOC = dict(
    summary="A DYE / TRACER / CONTAMINANT plume that TRAVELS DOWNSTREAM in a RIVER (surface water).",
    routing=(
        "THE tool for \"a spill in the river, how far downstream does it travel\" - a "
        "dye / contaminant / pollutant / chemical plume moving down the channel, "
        "sewage or E.coli effluent DECAYING downstream (name it in "
        "`decaying_substance`), and wind setup on a wide reach. TELEMAC-2D over a "
        "REAL NHDPlus reach with real NHDArea banks: a finite pulse is advected by "
        "the carrier discharge and dilutes. Returns a PEAK concentration map + the "
        "time-stepped mesh the client animates. Supply `location` OR `bbox`."
    ),
    not_for=(
        "an OIL slick (`telemac_river_oil_spill`); bed SCOUR, deposition, grain "
        "sorting or dredging (`telemac_river_scour`); a SUSPENDED sediment plume "
        "settling onto the bed (`telemac_river_sediment_plume`); "
        "dissolved-oxygen sag (`telemac_do_sag`); rainfall-runoff flood depth "
        "(`telemac_rain_on_grid`). Groundwater plumes, and dam-break or tsunami "
        "run-up, are not modeled here"
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
        "On success a `TelemacDyeLayerURI` (a `LayerURI` subtype) - the emitter loads "
        "the peak-concentration map and animates the SELAFIN sibling. It carries "
        "`dye_cmax_mgl` / `dye_peak_time_s` / `plume_reach_m` / `active_frames`; "
        "narrate those typed numbers. On failure a dict with `status=\"error\"` + "
        "`error_code`."
    ),
)
