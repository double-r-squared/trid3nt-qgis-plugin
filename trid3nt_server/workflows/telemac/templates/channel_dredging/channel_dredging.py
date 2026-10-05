"""Engine template ``telemac_channel_dredging`` - a maintenance dredge of a channel."""

from __future__ import annotations

import sys

from trid3nt_contracts.tool_registry import AtomicToolMetadata
from trid3nt_server.workflows.runtime import (
    Data, register_workflow, Accepts, Param, doors,
)
from trid3nt_server.inputs import point_arg, Point
from trid3nt_server.inputs.instant import event_time
from trid3nt_server.workflows.telemac.modules import GAIA, T2D, mass_balance
from trid3nt_server.workflows.telemac.modules.gaia import (
    Dig, Dredging, RESULT_FILENAME,
)
from trid3nt_server.workflows.telemac.modules.telemac2d import Boundaries
from trid3nt_server.workflows.telemac.workflow import Measured, TelemacWorkflow

__all__ = ["ACCEPTS", "CAPTIONS", "DATA", "DOC", "OUTPUTS", "PARAMS",
           "STEERING", "telemac_channel_dredging"]


ACCEPTS = Accepts(mesh=("unstructured_tri",))


class PARAMS:
    seed_point = Param(
        door=doors.QUESTION, optional=True, consequence="aoi", type=Point,
        derived_when_absent=(
            "nothing: the dredge's reference surface is a band of cross-"
            "sections stationed down the channel from this point, so a run "
            "without one refuses by naming it"),
        desc="A point ON the channel the dredge works in, as the pick's "
             "{coordinates, name} verbatim, a (lon, lat) pair, 'lat,lon' or a "
             "point layer. Geocode a place name first; the channel is fetched "
             "downstream of it and the levels every dredging action reads are "
             "stationed along the centerline that comes back with it")

    design_depth_m = Param(
        door=doors.SCENARIO, default=3.0, bounds=(0.1, 40.0), units="m",
        user_lever=True, consequence="scenario",
        desc="Depth the fairway is dredged TO, under the reference water "
             "surface the run opens at - the design draught plus its overdepth")
    trigger_depth_m = Param(
        door=doors.SCENARIO, default=3.0, bounds=(0.1, 40.0), units="m",
        user_lever=True, consequence="scenario",
        desc="Depth at which a node is dredged: the bed is worked wherever it "
             "sits shallower than this under the reference surface. Equal to "
             "design_depth_m keeps the channel exactly at grade; SMALLER than "
             "it lets the channel shoal before the dredger returns, and a "
             "trigger DEEPER than the grade would mark a node for a cut that "
             "is above its own bed")

    dredge_start_s = Param(
        door=doors.SCENARIO, default=0.0, bounds=(0.0, 6048000.0), units="s",
        consequence="scenario",
        desc="When the first dredging pass begins, in seconds of the BED's own "
             "clock - the run's morphological time, DURATION x MORPHOLOGICAL "
             "FACTOR")
    dredge_end_s = Param(
        door=doors.SCENARIO, default=3000.0, bounds=(1.0, 6048000.0), units="s",
        consequence="scenario",
        desc="When the campaign stops, on the same bed clock: no further pass "
             "begins after it, and a pass already cutting runs on until it "
             "reaches grade")
    dredge_repeat_s = Param(
        door=doors.SCENARIO, default=1800.0, bounds=(1.0, 6048000.0), units="s",
        consequence="scenario",
        desc="Maintenance interval on the bed clock: how long after a pass "
             "starts the next one begins, if the channel has shoaled past "
             "trigger_depth_m again")

    dig_rate_m_per_s = Param(
        door=doors.SCENARIO, default=0.002, bounds=(1.0e-6, 1.0), units="m/s",
        user_lever=True, consequence="scenario",
        desc="How fast the dredger lowers the bed, as metres of bed per second "
             "of SOLVER time at a working node - the plant's capacity, not a "
             "physical rate. A pass reports its volume only once it has reached "
             "grade, so this and the cut it has to make are what decide whether "
             "the run sees a completed pass at all")
    dump_rate_m_per_s = Param(
        door=doors.SCENARIO, default=0.002, bounds=(1.0e-6, 1.0), units="m/s",
        user_lever=True, consequence="scenario",
        desc="How fast the spoil is laid into the dump area, as metres of bed "
             "per second of solver time at a receiving node; a pass is not "
             "finished until its spoil is placed")
    min_volume_m3 = Param(
        door=doors.SCENARIO, default=0.0, bounds=(0.0, 1.0e7), units="m^3",
        consequence="scenario",
        desc="Least volume worth moving around a node before it is dredged at "
             "all; 0 works every node past the trigger")


_GEOMETRY = "channel.slf"
_BOUNDARY = "channel.cli"
_RESULT = "r2d_channel.slf"

_REACH_LENGTH_KM = 2.0

# Strickler; the outflow stage is derived at this same roughness
_FRICTION_LAW = 3
_FRICTION_COEFFICIENT = 33.0

_BED_STOCK_M = 5.0

# NESTOR dates every action against this origin
_TIME_ORIGIN = [2000, 1, 1, 0, 0, 0]

# levels read off the authored profile file, the opening water surface
_REFERENCE_LEVEL = "SECTIONS"


class DATA:
    domain = Data.need("hydrography", at="seed_point",
                       span_km=_REACH_LENGTH_KM)
    line = Data.supplied(geometry="polyline")
    bed = Data.need("channel survey")
    discharge = Data.need("discharge series").context(
        "the National Water Model published no streamflow over this domain "
        "at that cycle")
    dredge_area = Data.supplied(geometry="polygon")
    dump_area = Data.supplied(geometry="polygon")
    level = Data.need("water level series").optional()


_DREDGE = Measured(
    "dredge", kind="dredge",
    reads={"areas": {"dredge_area": "dredge_area", "dump_area": "dump_area"},
           "grade_depth_m": "design_depth_m"},
    asked={"dug_area": "dredge_area", "stock_m": _BED_STOCK_M})


class STEERING(T2D):
    GEOMETRY_FILE = _GEOMETRY
    BOUNDARY_CONDITIONS_FILE = _BOUNDARY
    RESULTS_FILE = _RESULT
    LISTING_PRINTOUT_PERIOD = 500

    LAW_OF_BOTTOM_FRICTION = _FRICTION_LAW
    FRICTION_COEFFICIENT = _FRICTION_COEFFICIENT

    TYPE_OF_ADVECTION = [1, 5]
    SUPG_OPTION = [0, 0]
    MASS_LUMPING_ON_H = 1.0
    CONTINUITY_CORRECTION = True
    SOLVER = 1
    SOLVER_ACCURACY = 1.0e-6
    MAXIMUM_NUMBER_OF_ITERATIONS_FOR_SOLVER = 500
    IMPLICITATION_FOR_DEPTH = 0.6
    IMPLICITATION_FOR_VELOCITY = 0.6

    MASS_BALANCE = True

    # in solver steps, not seconds
    GRAPHIC_PRINTOUT_PERIOD = 100

    # hydraulic seconds; the bed clock is this times the morphological factor
    DURATION = 3600.0

    ORIGINAL_DATE_OF_TIME = _TIME_ORIGIN[:3]
    ORIGINAL_HOUR_OF_TIME = _TIME_ORIGIN[3:]

    boundaries = Boundaries(tracers=[])

    coupling = [GAIA.bed(
        geometry=_GEOMETRY, boundary=_BOUNDARY,
        MASS_BALANCE=True,
        CLASSES_SEDIMENT_DIAMETERS=[2.0e-4],
        LAYERS_INITIAL_THICKNESS=[_BED_STOCK_M],
        MORPHOLOGICAL_FACTOR=10.0,
        dredging=Dredging(
            measured="dredge",
            actions=[Dig(field="dredge_area",
                         level=_REFERENCE_LEVEL,
                         start="dredge_start_s", end="dredge_end_s",
                         repeat="dredge_repeat_s",
                         rate="dig_rate_m_per_s",
                         depth="design_depth_m",
                         crit_depth="trigger_depth_m",
                         min_volume="min_volume_m3",
                         dump="dump_area",
                         dump_rate="dump_rate_m_per_s")],
            origin=_TIME_ORIGIN))]


OUTPUTS = [mass_balance(module="gaia").note()]
CAPTIONS = {"discharge": "a streamflow", "level": "a water level",
            "mass_balance": "the sediment closure and the dredged and dumped volumes"}

_METADATA = AtomicToolMetadata(
    name="telemac_channel_dredging",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    engine="telemac",
    tier="template",
)

RESULTS = (_RESULT, RESULT_FILENAME)

REVIEW_TITLE = "Review the dredge, the bed and the mesh"

DOC = dict(
    summary="MAINTENANCE DREDGING of a navigation channel: how much comes out, "
            "and what the bed does.",
    routing=(
        "THE tool for \"dredge this channel and tell me the volume\" - a "
        "maintenance dredge of a fairway or berth pocket held at a design "
        "depth, the spoil placed in a disposal area. TELEMAC-2D coupled with "
        "GAIA, the dredger driven by NESTOR so what it moves is in the bed's "
        "mass balance. The channel is fetched downstream of a point on it; "
        "its bed is the published USACE survey where one covers it, terrain "
        "elsewhere. DURATION, MORPHOLOGICAL FACTOR, CLASSES SEDIMENT "
        "DIAMETERS and LAYERS INITIAL THICKNESS are the deck's own, set by "
        "name. Returns the bed evolution and the dredged and dumped volumes; "
        "supply `seed_point` and the two areas as polygons."
    ),
    not_for=(
        "a bed that scours and re-deposits on its own, with no dredger "
        "(`telemac_bed_scour`); a SUSPENDED plume settling onto an inert bed "
        "(`telemac_sediment_plume`); a dye or contaminant plume "
        "(`telemac_dye_release`); an OIL slick (`telemac_oil_spill`)"
    ),
    params=PARAMS,
    controls=(
        ("input_mode",
         '"user_gated" presents the filled sheet for review/edit before the solve '
         'and WAITS; "auto" (session default) proceeds with every assumption '
         "labeled. Not a physical value."),
        ("restart_clean",
         "True builds the mesh again even where one built from the same domain, "
         "bed, resolution and mesher is kept; unset, such a kept mesh is reused. "
         "Not a physical value."),
    ),
    returns=(
        "On success the run's record (a `LayerURI`): every variable its "
        "modules wrote, styled on one mesh layer, animated where it varies, "
        "the bed evolution in metres among them (deposition positive, the "
        "dredged cut negative), and no read the template placed. On failure "
        "a dict with `status=\"error\"` + `error_code`."
    ),
)

telemac_channel_dredging = register_workflow(
    TelemacWorkflow, _METADATA,
    sys.modules[__name__],
    sensitivity=(("cumul_bed_evol", "peak"),),
    coerce=(
        point_arg("seed_point", tool="telemac_channel_dredging",
                  prompt="Click on the channel this dredge works in",
                  code="TELEMAC_PARAMS_INVALID"),
        event_time(),
    ),
)
