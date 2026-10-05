"""Engine template ``tomawac_wave_driven_currents`` - the current the waves drive."""

from __future__ import annotations

import sys

from trid3nt_contracts.tool_registry import AtomicToolMetadata
from trid3nt_server.inputs import point_arg, Point
from trid3nt_server.inputs.instant import event_time
from trid3nt_server.workflows.runtime import (
    Data, register_workflow, Accepts, Param, doors, lever,
)
from trid3nt_server.tools.mesh.tool import mesh_op, tool
from trid3nt_server.workflows.telemac.modules import T2D, WAC, series
from trid3nt_server.workflows.telemac.modules.telemac2d import Boundaries
from trid3nt_server.workflows.telemac.modules.tomawac import RESULT_FILENAME
from trid3nt_server.workflows.telemac.workflow import Placed, TelemacWorkflow

__all__ = ["ACCEPTS", "CAPTIONS", "DATA", "DEFAULT_OPEN_DEPTH_M", "DOC",
           "MESH", "OUTPUTS", "PARAMS", "STEERING",
           "tomawac_wave_driven_currents"]


ACCEPTS = Accepts(mesh=("unstructured_tri",))

# every boundary stretch at least this deep opens
DEFAULT_OPEN_DEPTH_M: float = -12.0


class PARAMS:
    seed = Param(
        door=doors.USER, optional=True, consequence="aoi", type=Point,
        derived_when_absent=(
            "the buoy nearest the centre of the water the window was cut to "
            "is the one the wave deck's open edge is forced at"),
        desc="Where OFFSHORE the incoming sea state is measured, as a Point: "
             "the pick's {coordinates, name} verbatim, a (lon, lat) pair, "
             "'lat,lon', a point layer. Geocode a place name first. The nearest "
             "buoy to it is the record the wave boundary is forced at, so put "
             "it on the water the swell arrives across")
    station = Param(
        door=doors.USER, user_lever=True, consequence="scenario", type=Point,
        desc="Where INSHORE the current is read over time, as a Point: the "
             "pick's {coordinates, name} verbatim, a (lon, lat) pair, "
             "'lat,lon', a point layer. Geocode a place name first. The speed "
             "and the wave height are charted at the node of the mesh it "
             "settles onto, so put it in the surf zone you are asking about")

    mesh_resolution_m = lever(
        "mesh_resolution_m",
        desc="Target element edge length the water is triangulated at; the "
             "longshore current is driven across the surf zone, so what this "
             "has to resolve is the width of the breaking band")

    open_depth_threshold_m = Param(
        door=doors.SCENARIO, default=DEFAULT_OPEN_DEPTH_M,
        bounds=(-200.0, -1.0), units="m", consequence="physics",
        desc="How deep a boundary stretch must reach for it to be designated "
             "the OPEN edge the measured sea state enters through; every "
             "stretch that reaches it opens, and a window where none does has "
             "no edge for the spectrum and refuses")


_GEOMETRY = "coast.slf"
_BOUNDARY = "coast.cli"
_RESULT = "t2d_coast.slf"
_STEERING_FILE = "t2d_wave_driven.cas"

_DURATION_S = 3600.0
# both decks march this clock; modules stepping apart are two runs on one mesh
_TIME_STEP_S = 1.0
# in host steps
_COUPLING_PERIOD = 60
_WAVE_TIME_STEP_S = _TIME_STEP_S * _COUPLING_PERIOD
_WAVE_STEPS = int(_DURATION_S / _WAVE_TIME_STEP_S)
_HOST_FRAMES = int(_DURATION_S / _TIME_STEP_S) // _WAVE_STEPS

# Nikuradse: the coefficient is a grain roughness in metres
_FRICTION_LAW = 5
_FRICTION_COEFFICIENT = 0.05

_STATION = Placed("station", point="station", label="Current station")


class DATA:
    extent = Data.supplied(geometry="rectangle")
    domain = Data.need("hydrography", kind="coastline", geometry="polyline")
    bed = Data.need("bathymetry")
    wave = Data.need("wave series", at="seed")
    level = Data.need("water level series")
    mesh = Data.supplied(geometry="mesh").optional()


class STEERING(T2D):
    GEOMETRY_FILE = _GEOMETRY
    BOUNDARY_CONDITIONS_FILE = _BOUNDARY
    RESULTS_FILE = _RESULT

    TIME_STEP = _TIME_STEP_S
    DURATION = _DURATION_S
    GRAPHIC_PRINTOUT_PERIOD = _HOST_FRAMES
    LISTING_PRINTOUT_PERIOD = _HOST_FRAMES

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

    # the swash dries and wets every wave
    TREATMENT_OF_NEGATIVE_DEPTHS = 1

    MASS_BALANCE = True

    boundaries = Boundaries(tracers=[])

    coupling = [WAC.wave(
        geometry=_GEOMETRY, boundary=_BOUNDARY,
        TIME_STEP=_WAVE_TIME_STEP_S,
        NUMBER_OF_TIME_STEP=_WAVE_STEPS,
        NUMBER_OF_DIRECTIONS=24,
        NUMBER_OF_FREQUENCIES=25,
        MINIMAL_FREQUENCY=0.04,
        TYPE_OF_BOUNDARY_DIRECTIONAL_SPECTRUM=6,
        DEPTH_INDUCED_BREAKING_DISSIPATION=1,
        BOTTOM_FRICTION_DISSIPATION=1)]

    COUPLING_PERIOD_FOR_TOMAWAC = _COUPLING_PERIOD


MESH = tool.build_mesh(
    mesher="om2d",
    kind="unstructured_tri",
    extent="domain",
    resolution_m="mesh_resolution_m",
    ops=[
        mesh_op("feature_sizing_function"),
        mesh_op("set_rim_size"),
        mesh_op("enforce_mesh_gradation"),
        mesh_op("delete_boundary_faces"),
        mesh_op("delete_faces_connected_to_one_face"),
        mesh_op("make_mesh_boundaries_traversable"),
        mesh_op("fix_mesh", delete_unused=True),
        mesh_op("set_bed", source="bed"),
        mesh_op("identify_ocean_boundary_sections",
                depth_threshold="open_depth_threshold_m"),
    ],
)

OUTPUTS = [
    series("M", at="station").chart(),
    series("HM0", at="station", module="tomawac").chart(),
]
CAPTIONS = {"M": "current speed", "U": "current along x", "V": "current along y",
            "HM0": "significant wave height", "BETA": "breaking rate",
            "wave": "a sea state",
            "level": "the tide the open edge holds, over the run's window"}

_METADATA = AtomicToolMetadata(
    name="tomawac_wave_driven_currents",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    engine="telemac",
    tier="template",
)

MESH_ON = "domain"
SUPPLIED_MESH = "mesh"

RESULTS = (_RESULT, RESULT_FILENAME)

STEERING_FILE = _STEERING_FILE
PREFIX = "t2d"

REVIEW_TITLE = "Review the sea state, the tide and the water they drive"

DOC = dict(
    summary="WAVE-DRIVEN CURRENTS: the current breaking waves drive along the "
            "shore - how fast and which way.",
    routing=(
        "THE tool for \"how strong is the longshore current here\", \"which way "
        "does the surf push along this beach\", \"what current do these waves "
        "set up\". TELEMAC-2D over the water the window is cut to at the "
        "coastline, COUPLED to TOMAWAC on the same mesh: the waves break, the "
        "gradient of their radiation stress enters the momentum equation, and "
        "the current that results is the answer. The wave edge is forced at a "
        "sea state a buoy MEASURED; the water stands at a gauge's tide. Both "
        "fields on one mesh, animated, the speed and the wave height charted "
        "where the ask points. Deck opinions, by keyword: TIME STEP, DURATION, "
        "COUPLING PERIOD FOR TOMAWAC, and the wave deck's under `tomawac:`. "
        "Supply the window, `station`, `event_time`."
    ),
    not_for=(
        "the waves alone, with no current solved "
        "(`tomawac_nearshore_waves`); agitation behind a breakwater "
        "(`artemis_harbor_agitation`); a current with no waves in it"
    ),
    params=PARAMS,
    controls=(
        ("input_mode",
         '"user_gated" presents the resolved sea state, the tide and the mesh '
         'for review/edit before the solve and WAITS; "auto" (session default) '
         "proceeds with every assumption labeled. Not a physical value."),
        ("restart_clean",
         "True builds the mesh again even where one built from the same domain, "
         "bed, resolution and mesher is kept; unset, such a kept mesh is reused. "
         "Not a physical value."),
    ),
    returns=(
        "On success the run's record (a `LayerURI`): every variable "
        "TELEMAC-2D wrote and every variable TOMAWAC wrote, styled on one "
        "mesh layer and animated, plus the current speed and the wave "
        "height charted at the station. A coast the swell reaches head-on "
        "charts a small longshore speed, which is what that place did in "
        "that hour. On failure a dict with `status=\"error\"` + `error_code`."
    ),
)

tomawac_wave_driven_currents = register_workflow(
    TelemacWorkflow, _METADATA, sys.modules[__name__],
    sensitivity=(("current_speed", "peak"),),
    coerce=(
        point_arg("seed", tool="tomawac_wave_driven_currents",
                  prompt="Click offshore, where the incoming sea state is "
                         "measured",
                  code="TELEMAC_PARAMS_INVALID"),
        point_arg("station", tool="tomawac_wave_driven_currents",
                  prompt="Click on the water where the current should be read",
                  code="TELEMAC_PARAMS_INVALID"),
        event_time(),
    ),
)
