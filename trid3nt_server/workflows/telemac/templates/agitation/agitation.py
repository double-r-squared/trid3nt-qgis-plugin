"""Engine template ``artemis_harbor_agitation`` - does the structure shelter the water."""

from __future__ import annotations

import sys

from trid3nt_contracts.tool_registry import AtomicToolMetadata
from trid3nt_server.workflows.runtime import (
    Data, register_workflow, Accepts, Param, doors, lever,
)
from trid3nt_server.tools.mesh.tool import mesh_op, tool
from trid3nt_server.workflows.telemac.authoring.accepted_mesh import (
    HARBOUR_GEOMETRY,
)
from trid3nt_server.workflows.telemac.modules.outputs import profile
from trid3nt_server.workflows.telemac.modules.artemis import (
    ART, BOUNDARY_FILENAME, IncidentWave,
)
from trid3nt_server.workflows.telemac.workflow import Measured, TelemacWorkflow

__all__ = ["ACCEPTS", "CAPTIONS", "DATA", "DEFAULT_BARRIER_WIDTH_M",
           "DEFAULT_GRADE", "DEFAULT_OPEN_DEPTH_M", "DOC", "MESH", "OUTPUTS",
           "PARAMS", "STEERING", "artemis_harbor_agitation"]


ACCEPTS = Accepts(mesh=("unstructured_tri",))

DEFAULT_GRADE: float = 0.2

# a mapped centreline bounds no area, so the mesher cuts it at this width
DEFAULT_BARRIER_WIDTH_M: float = 20.0

# every boundary stretch at least this deep opens
DEFAULT_OPEN_DEPTH_M: float = -12.0


class PARAMS:
    wave_height_m = Param(
        door=doors.SCENARIO, default=1.0, bounds=(0.01, 10.0),
        units="m", consequence="physics",
        desc="Incident wave height H0 on the designated liquid boundary; Kd is "
             "measured against it, so it sets the scale of every narrated height")
    reflection_coef = Param(
        door=doors.SCENARIO, default=0.5, bounds=(0.0, 1.0),
        consequence="physics",
        desc="The declared structure's reflection coefficient: 1 fully reflecting "
             "(a vertical quay), 0 fully absorbing (a rubble slope). Every other "
             "solid face is the absorbing shore")

    mesh_resolution_m = lever(
        "mesh_resolution_m",
        desc="Finest triangle edge, used at the shoreline and around the "
             "structure. THE granularity lever: a phase-resolving solve needs "
             "several nodes per WAVELENGTH and Kd peaks inside a diffraction "
             "fringe, so a coarse mesh reads the peaks low")
    mesh_grade = Param(
        door=doors.CONSTANT, default=DEFAULT_GRADE, bounds=(0.05, 0.5),
        consequence="numerical",
        desc="Mesh gradation: how fast the edge may grow from the structure band "
             "out to the open approach")
    barrier_width_m = Param(
        door=doors.SCENARIO, default=DEFAULT_BARRIER_WIDTH_M,
        bounds=(1.0, 200.0), units="m", consequence="numerical",
        desc="The width the mapped structure centreline is cut at; a survey maps "
             "a mound as a line and a line removes no water from the domain")
    transect_length_m = Param(
        door=doors.SCENARIO, default=1500.0, bounds=(100.0, 20000.0),
        units="m", consequence="scenario",
        desc="The whole length of the transect the agitation is read along: a "
             "straight line through the structure's centroid along the incident "
             "wave direction, half of it on the exposed side and half in the lee")
    open_depth_threshold_m = Param(
        door=doors.SCENARIO, default=DEFAULT_OPEN_DEPTH_M,
        bounds=(-200.0, -1.0), units="m", consequence="physics",
        desc="How deep a boundary stretch must reach for it to be designated the "
             "OPEN edge the incident wave enters through; every stretch that "
             "reaches it opens")
    cores = lever("cores")


_RESULT = "res_agitation.slf"
_STEERING_FILE = "art_agitation.cas"


class DATA:
    extent = Data.supplied(geometry="rectangle")
    domain = Data.need("hydrography", kind="coastline", geometry="polyline")
    bed = Data.need("bathymetry")
    structure = Data.supplied(geometry="polyline")
    mesh = Data.supplied(geometry="mesh").optional()


_FOOTPRINT = Measured(
    "footprint", kind="footprint",
    reads={"value": "structure", "width_m": "barrier_width_m"},
    asked={"asked": "the structure this question asks about",
           "code": "ARTEMIS_STRUCTURE_INVALID"})

_HARBOUR = Measured(
    "settled", kind="harbour",
    reads={"structure": "structure",
           "structure_width_m": "barrier_width_m",
           "wave_period_s": "WAVE_PERIOD",
           "wave_height_m": "wave_height_m",
           "wave_direction_deg": "DIRECTION_OF_WAVE_PROPAGATION",
           "reflection_coef": "reflection_coef",
           "open_depth_threshold_m": "open_depth_threshold_m"},
    asked={"result_basename": _RESULT, "deck": "artemis_harbor_agitation"})


class STEERING(ART):
    GEOMETRY_FILE = HARBOUR_GEOMETRY
    BOUNDARY_CONDITIONS_FILE = BOUNDARY_FILENAME
    RESULTS_FILE = _RESULT

    INITIAL_CONDITIONS = "CONSTANT ELEVATION"
    # an elliptic solve this size outruns the dictionary iteration ceiling
    MAXIMUM_NUMBER_OF_ITERATIONS_FOR_SOLVER = 4000

    WAVE_PERIOD = 8.0
    # trigonometric from +x, so 90 travels north; the transect is read along it
    DIRECTION_OF_WAVE_PROPAGATION = 90.0

    incident_wave = IncidentWave(measured="settled", height_m="wave_height_m",
                                 reflection_coef="reflection_coef")


MESH = tool.build_mesh(
    mesher="om2d",
    kind="unstructured_tri",
    extent="domain",
    resolution_m="mesh_resolution_m",
    ops=[
        mesh_op("set_obstacle", geometry="footprint"),
        mesh_op("feature_sizing_function"),
        mesh_op("set_rim_size"),
        mesh_op("enforce_mesh_gradation", gradation="mesh_grade"),
        mesh_op("delete_boundary_faces"),
        mesh_op("delete_faces_connected_to_one_face"),
        mesh_op("make_mesh_boundaries_traversable"),
        mesh_op("fix_mesh", delete_unused=True),
        mesh_op("set_bed", source="bed"),
        mesh_op("identify_ocean_boundary_sections",
                depth_threshold="open_depth_threshold_m"),
    ],
)

_TRANSECT = Measured(
    "transect", kind="transect",
    reads={"value": "structure",
           "bearing_deg": "DIRECTION_OF_WAVE_PROPAGATION",
           "length_m": "transect_length_m"},
    asked={"convention": "trig", "code": "ARTEMIS_STRUCTURE_INVALID"})

OUTPUTS = [
    profile("KD", along="transect", within_m="mesh_resolution_m").chart(),
]
CAPTIONS = {"KD": "agitation coefficient"}

_ARTEMIS_METADATA = AtomicToolMetadata(
    name="artemis_harbor_agitation",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    engine="telemac",
    tier="template",
)

MESH_ON = "domain"
SUPPLIED_MESH = "mesh"

RESULTS = (_RESULT,)

STEERING_FILE = _STEERING_FILE
PREFIX = "artemis"

REVIEW_TITLE = "Review the incident wave, the structure and the mesh"

DOC = dict(
    summary="The WAVE AGITATION (Kd = Hs/H0) a declared structure leaves inside a "
            "harbour, marina or sheltered basin.",
    routing=(
        "THE tool for \"does this breakwater shelter the berths\", \"how much does "
        "swell amplify inside this harbour\", \"wave agitation / tranquility in the "
        "basin\". ARTEMIS phase-RESOLVING elliptic mild-slope (Berkhoff) over a "
        "mesh cut from the real shoreline, the structure punched out and the "
        "seaward stretches open: fringes and standing waves are the answer, not "
        "an average. Wave: `WAVE PERIOD` 8 s, `DIRECTION OF WAVE PROPAGATION` 90 "
        "deg (trig from +x) - set either by name. THE STRUCTURE IS THE "
        "QUESTION and is REQUIRED: `structure=` a breakwater layer "
        "(`fetch_osm_features`) or a drawn line. Give `domain=` the water as "
        "an outline or a polygon layer, or `extent=` a rectangle the "
        "coastline cuts into water."
    ),
    not_for=(
        "the offshore SEA STATE or fetch-limited wind-wave growth; free-field "
        "agitation with no structure; coastal storm-tide flooding; a "
        "tracer released into a river channel"
    ),
    params=PARAMS,
    controls=(
        ("extent",
         "NOT needed when `domain=` is filled - this is the other way to say "
         "where the water is. A rectangle over the harbour as a LAYER (a uri or "
         "a file, not four numbers): the mapped coastline divides it and the "
         "water it leaves IS the domain. A box a coastline way crosses without "
         "closing divides nothing and refuses by name rather than meshing a "
         "shape nobody cut."),
        ("domain",
         "A harbour approach, a marina basin, or any water a structure shelters. "
         "Its own edge is the SHORELINE the mesh is sized against, so a rough "
         "outline is enough; unfilled, the extent above is cut instead."),
        ("bed",
         "That producer is the measured bathymetry under the domain - the "
         "surveyed sea floor the wave refracts over, matched to this water and "
         "laid rung by rung. Hand it your own survey raster, a layer of "
         "soundings, or a depth in metres for a basin nobody has sounded."),
        ("structure",
         "REQUIRED. The barrier the question is about, as a polyline LAYER (the "
         "uri or handle from fetch_osm_features feature='breakwaters', or any line layer the user "
         "has) or a drawn/typed line as [[lon, lat], ...]. Producer-less BY "
         "DESIGN - this tool will never go and find a structure you did not name. "
         "It is cut out of the water domain at `barrier_width_m` and its faces "
         "reflect `reflection_coef` of the incident energy."),
        ("input_mode",
         '"user_gated" presents the resolved incident wave, the structure and the '
         'authored mesh for review/edit before the solve and WAITS; "auto" '
         "(session default) proceeds with every assumption labeled. Not a "
         "physical value."),
        ("restart_clean",
         "True builds the mesh again even where one built from the same domain, "
         "bed, resolution and mesher is kept; unset, such a kept mesh is reused. "
         "Not a physical value."),
    ),
    returns=(
        "On success the run's record (a `LayerURI`): every variable its "
        "module wrote, styled on one mesh layer, animated where it varies, "
        "the derived agitation coefficient Kd = Hs/H0 among them, plus the "
        "Kd profile through the structure charted. The Kd field peaks on a "
        "standing wave against the domain's own open boundary as often as "
        "inside the harbour, so read the transect chart and the field "
        "behind the structure. On failure a dict with `status=\"error\"` + "
        "`error_code`."
    ),
)

artemis_harbor_agitation = register_workflow(
    TelemacWorkflow, _ARTEMIS_METADATA, sys.modules[__name__],
    levers=(),
    sensitivity=(("agitation_coefficient", "peak"),),
)
