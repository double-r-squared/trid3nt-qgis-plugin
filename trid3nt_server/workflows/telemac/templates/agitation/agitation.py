"""Engine template ``artemis_harbor_agitation`` - does the structure shelter the water."""

from __future__ import annotations

import sys

from trid3nt_contracts.tool_registry import AtomicToolMetadata
from trid3nt_server.inputs.slots import BED, DOMAIN, EXTENT
from trid3nt_server.tools.mesh.recipe import GRADE, RESOLUTION
from trid3nt_server.inputs.input import Input
from trid3nt_server.workflows.runtime import (
    register_workflow,
)
from trid3nt_server.tools.mesh.tool import mesh_op, tool
from trid3nt_server.workflows.telemac.authoring.accepted_mesh import (
    HARBOUR_GEOMETRY,
)
from trid3nt_server.workflows.runtime.primitives import profile
from trid3nt_server.workflows.telemac.modules.artemis import (
    ART, BOUNDARY_FILENAME, IncidentWave,
)
from trid3nt_server.workflows.telemac.workflow import Measured, TelemacWorkflow

__all__ = ["CAPTIONS", "MESH", "OUTPUTS", "INPUTS", "STEERING",
           "artemis_harbor_agitation"]


_RESULT = "res_agitation.slf"
_STEERING_FILE = "art_agitation.cas"


class INPUTS:
    wave_height_m = Input()
    reflection_coef = Input()

    mesh_resolution_m = RESOLUTION
    mesh_grade = GRADE
    barrier_width_m = Input()
    transect_length_m = Input()
    open_depth_threshold_m = Input()

    extent = Input(EXTENT, desc=(
        "NOT needed when `domain=` is filled - this is the other way to say "
        "where the water is. A rectangle over the harbour as a LAYER (a uri or "
        "a file, not four numbers): the mapped coastline divides it and the "
        "water it leaves IS the domain. A box a coastline way crosses without "
        "closing divides nothing and refuses by name rather than meshing a "
        "shape nobody cut.")).need(geometry="rectangle")
    domain = Input(DOMAIN, desc=(
        "A harbour approach, a marina basin, or any water a structure shelters. "
        "Its own edge is the SHORELINE the mesh is sized against, so a rough "
        "outline is enough; unfilled, the extent above is cut instead.")).need(
        "hydrography", kind="coastline", geometry="polyline")
    bed = Input(BED, desc=(
        "That producer is the measured bathymetry under the domain - the "
        "surveyed sea floor the wave refracts over, matched to this water and "
        "laid rung by rung. Hand it your own survey raster, a layer of "
        "soundings, or a depth in metres for a basin nobody has sounded.")).need(
        "bathymetry")
    structure = Input(desc=(
        "REQUIRED. The barrier the question is about, as a polyline LAYER (the "
        "uri or handle from fetch_osm_features feature='breakwaters', or any "
        "line layer the user has) or a drawn/typed line as [[lon, lat], ...]. "
        "Producer-less BY DESIGN - this tool will never go and find a structure "
        "you did not name. It is cut out of the water domain at "
        "`barrier_width_m` and its faces reflect `reflection_coef` of the "
        "incident energy.")).need(geometry="polyline")


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

RESULTS = (_RESULT,)

STEERING_FILE = _STEERING_FILE
PREFIX = "artemis"

REVIEW_TITLE = "Review the incident wave, the structure and the mesh"

artemis_harbor_agitation = register_workflow(
    TelemacWorkflow, _ARTEMIS_METADATA, sys.modules[__name__],
    sensitivity=(("agitation_coefficient", "peak"),),
)
