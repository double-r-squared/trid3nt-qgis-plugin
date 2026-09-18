"""The CONTRACT of ``telemac_sediment_plume``: what only a settling plume asks."""

from __future__ import annotations

from trid3nt_server.inputs import Point
from trid3nt_server.workflows.runtime import Accepts, Param, doors

__all__ = ["ACCEPTS", "DOC", "PARAMS"]

_HELPERS = "trid3nt_server.workflows.telemac.helpers"

#: What a suspended-plume run can be HANDED. The settling class rides the same
#: triangulation the hydrodynamics runs on, so a lattice is refused at the door.
ACCEPTS = Accepts(mesh=("unstructured_tri",), release=("point",))


class PARAMS:
    """What only a settling-plume question asks: where the sediment enters the
    water and how concentrated the release is. The domain, the bed, its boundary
    runs and the granularity are the runtime's own slots and levers, and the
    deck's clock, roughness, cadence, sediment class, transport and advection,
    wind and rain are keywords the module's dictionary describes."""

    release = Param(
        door=doors.USER, optional=True, user_lever=True,
        consequence="scenario", type=Point,
        derived_when_absent=(
            "the release sits at spill_fraction along the domain the mesh was "
            "built over; the plume's travel is measured from there"),
        desc="Where the sediment enters the water, as a Point: the pick's "
             "{coordinates, name} verbatim, a (lon, lat) pair, 'lat,lon', a "
             "point layer. Geocode a place name first. Its name becomes the "
             "marker's name, "
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

    # -- the released class -------------------------------------------------- #
    sediment_concentration_mgl = Param(
        door=doors.SCENARIO, default=100.0,
        bounds=(0.0, 1.0e6), units="mg/L", consequence="scenario",
        desc="Concentration of the released suspended sediment; what deposits is "
             "measured against what this put in")
    injected_mass_kg = Param(
        door=doors.DERIVED,
        resolve=f"{_HELPERS}.released_mass.injected_mass_kg",
        bounds=(0.0, 1.0e9), units="kg", consequence="scenario",
        desc="The mass the pulse released - source_q_m3s x "
             "sediment_concentration_mgl x spill_duration_s - which the deposited "
             "fraction is measured against")


DOC = dict(
    summary="A SUSPENDED SEDIMENT plume in a body of water: it settles and deposits on the bed.",
    routing=(
        "THE tool for \"sediment / silt / a turbidity plume released into the "
        "water, where does it settle out\" - a slurry spill, a construction or "
        "dredging discharge, an upstream sediment supply. TELEMAC-2D + GAIA "
        "over a reach walked from the release, a lake or harbour drawn on the "
        "canvas, or a supplied polygon: ONE settling class over a bed with NO "
        "stock, so nothing erodes and only what was injected deposits. Give "
        "`release` as a pick or a pair, or supply `domain`. Deck: "
        "DURATION 3600 s, CLASSES SEDIMENT DIAMETERS 2.0e-4 m, SUSPENSION "
        "TRANSPORT FORMULA FOR ALL SANDS 3, SCHEME FOR ADVECTION OF SUSPENDED "
        "SEDIMENTS 1, no WIND and no RAIN OR EVAPORATION - set each by its "
        "keyword name."
    ),
    not_for=(
        "bed SCOUR or an erodible bed (`telemac_bed_scour`); a conservative dye "
        "or contaminant plume (`telemac_dye_release`); an OIL slick "
        "(`telemac_oil_spill`); dissolved-oxygen sag (`telemac_do_sag`)"
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
        "the bed evolution among them, plus the suspended-sediment series "
        "charted. Its `answer` carries `suspended_cmax` / "
        "`plume_reach_m` / `bed_evolution_max_m` / `net_bed_mass_kg` / "
        "`deposit_fraction` (the deposited mass over the injected mass); narrate "
        "those typed numbers. On failure a dict with `status=\"error\"` + "
        "`error_code`."
    ),
)
