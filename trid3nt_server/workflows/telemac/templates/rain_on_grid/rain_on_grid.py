"""Engine template ``telemac_rain_on_grid`` - a storm over the ground it falls on.

APPLICABILITY (Godara, Bruland and Alfredsen 2024, Front. Water 6:1384205):
single-storm flash floods in small steep catchments; infiltrated water is lost,
so there is no subsurface return flow and no baseflow."""

from __future__ import annotations

import sys

from trid3nt_contracts.tool_registry import AtomicToolMetadata
from trid3nt_server.inputs.slots import BED, DOMAIN, RAIN
from trid3nt_server.tools.mesh.recipe import MAX_EDGE, RESOLUTION
from trid3nt_server.inputs.input import Input
from trid3nt_server.workflows.runtime import (
    register_workflow, tool,
)
from trid3nt_server.tools.mesh.tool import mesh_op
from trid3nt_server.inputs import point_arg
from trid3nt_server.workflows.telemac.modules import T2D, series
from trid3nt_server.workflows.telemac.modules.telemac2d import (
    Infiltration, Rating, Storm,
)
from trid3nt_server.workflows.telemac.workflow import Measured, TelemacWorkflow

__all__ = ["CAPTIONS", "LANDCOVER_CN_MANNING",
           "LANDCOVER_UNMAPPED", "MESH", "OUTPUTS", "INPUTS", "STEERING",
           "telemac_rain_on_grid"]


LANDCOVER_CN_MANNING: dict[int, tuple[float, float, str]] = {
    11: (100.0, 0.040, "river/open-water"),
    12: (100.0, 0.040, "river/open-water"),
    21: (75.0, 0.050, "open-land"),
    22: (89.0, 0.100, "urban"),
    23: (89.0, 0.100, "urban"),
    24: (89.0, 0.100, "urban"),
    31: (85.0, 0.020, "bare-rock/scarce-veg"),
    41: (80.0, 0.200, "forest"),
    42: (80.0, 0.200, "forest"),
    43: (80.0, 0.200, "forest"),
    51: (75.0, 0.050, "open-land"),
    52: (75.0, 0.050, "open-land"),
    71: (75.0, 0.050, "open-land"),
    72: (75.0, 0.050, "open-land"),
    81: (80.0, 0.050, "open-land"),
    82: (80.0, 0.050, "open-land"),
    90: (90.0, 0.200, "marsh"),
    95: (90.0, 0.200, "marsh"),
}

LANDCOVER_UNMAPPED: tuple[float, float, str] = (75.0, 0.050, "open-land")


_GEOMETRY = "rog.slf"
_BOUNDARY = "rog.cli"
_RESULT = "r2d_rog.slf"

# half-width; the delineation refuses a basin the window clips
_BASIN_WINDOW_KM = 15.0

_MESH_GRADE = 0.20


class INPUTS:
    pour_point = Input()

    rain_series_mm = Input()
    design_storm_mm_per_day = Input()

    curve_number = Input()
    steep_slope_correction = Input()

    mesh_resolution_m = RESOLUTION
    mesh_max_edge_m = MAX_EDGE

    domain = Input(DOMAIN).need("hydrography", kind="basin", at="pour_point",
                                span_km=_BASIN_WINDOW_KM)
    bed = Input(BED).need("terrain")
    rivers = Input().need("hydrography", geometry="polyline")
    landcover = Input().need("land cover")
    rain = Input(RAIN).need("precipitation series")


MESH = tool.build_mesh(
    mesher="om2d",
    kind="unstructured_tri",
    extent="domain",
    resolution_m="mesh_resolution_m",
    ops=[
        mesh_op("distance_sizing_from_line_function", line_file="rivers",
                rate=_MESH_GRADE, max_edge_length="mesh_max_edge_m"),
        mesh_op("set_rim_size"),
        mesh_op("enforce_mesh_gradation", gradation=_MESH_GRADE),
        mesh_op("delete_boundary_faces"),
        mesh_op("delete_faces_connected_to_one_face"),
        mesh_op("make_mesh_boundaries_traversable"),
        mesh_op("fix_mesh", delete_unused=True),
        mesh_op("set_bed", source="bed", condition="pit_fill"),
        mesh_op("set_boundary_roles", runs="domain"),
    ],
)

_OUTLET = Measured(
    "outlet", kind="rating",
    reads={"landcover": "landcover", "mm_per_day": "design_storm_mm_per_day",
           "series": "rain_series_mm", "record": "rain"},
    asked={"roughness": LANDCOVER_CN_MANNING, "unmapped": LANDCOVER_UNMAPPED})


class STEERING(T2D):
    GEOMETRY_FILE = _GEOMETRY
    BOUNDARY_CONDITIONS_FILE = _BOUNDARY
    RESULTS_FILE = _RESULT
    # in solver steps, not seconds
    GRAPHIC_PRINTOUT_PERIOD = 900
    LISTING_PRINTOUT_PERIOD = 900
    DURATION = 43200.0
    TYPE_OF_ADVECTION = [1, 5]
    SUPG_OPTION = [0, 0]
    MASS_LUMPING_ON_H = 1.0
    CONTINUITY_CORRECTION = True
    SOLVER = 1
    SOLVER_ACCURACY = 1.0e-6
    MAXIMUM_NUMBER_OF_ITERATIONS_FOR_SOLVER = 200
    IMPLICITATION_FOR_DEPTH = 0.6
    IMPLICITATION_FOR_VELOCITY = 0.6
    # an overland sheet's surface follows the ground
    FREE_SURFACE_GRADIENT_COMPATIBILITY = 0.9
    MASS_BALANCE = True

    # Manning: the infiltration table's roughness column is n
    LAW_OF_BOTTOM_FRICTION = 4

    # SCS Curve Number
    RAINFALL_RUNOFF_MODEL = 1

    storm = Storm(mm_per_day="design_storm_mm_per_day",
                  hours=6.0,
                  series="rain_series_mm",
                  record="rain",
                  tracers=0)
    infiltration = Infiltration(
        landcover="landcover",
        table=LANDCOVER_CN_MANNING, unmapped=LANDCOVER_UNMAPPED,
        uniform_cn="curve_number",
        steep_slope_correction="steep_slope_correction",
        antecedent_moisture=2,
        initial_abstraction=1)

    rating = Rating(measured="outlet")


OUTPUTS = [
    series("FLUX", at="pour_point").chart(),
    series("FLUX", at="pour_point").station(),
]
CAPTIONS = {"FLUX": "outlet hydrograph"}

_METADATA = AtomicToolMetadata(
    name="telemac_rain_on_grid",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    engine="telemac",
    tier="template",
)

REVIEW_TITLE = "Review the storm, the catchment and the mesh band"

telemac_rain_on_grid = register_workflow(
    TelemacWorkflow, _METADATA, sys.modules[__name__],
    sensitivity=(("water_depth", "peak"),
                 ("outlet_hydrograph", "peak")),
    coerce=(
        point_arg("pour_point", tool="telemac_rain_on_grid",
                  prompt="Click the catchment outlet the runoff drains to",
                  code="TELEMAC_ROG_PARAMS_INVALID"),
    ),
)
