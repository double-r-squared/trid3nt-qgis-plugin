"""Engine template ``telemac_rain_on_soil`` - a storm over the ground it falls on, infiltrating by the soil's texture.

APPLICABILITY (Godara, Bruland and Alfredsen 2024, Front. Water 6:1384205):
single-storm flash floods in small steep catchments; infiltrated water is lost,
so there is no subsurface return flow and no baseflow."""

from __future__ import annotations

import sys

from trid3nt_contracts.tool_registry import AtomicToolMetadata
from trid3nt.inputs.slots import BED, DOMAIN, RAIN, SOIL
from trid3nt.tools.mesh.recipe import MAX_EDGE, RESOLUTION
from trid3nt.inputs.input import Input
from trid3nt.workflows.runtime import register_workflow
from trid3nt.inputs import point_arg
from trid3nt.workflows.telemac.modules import T2D, series
from trid3nt.workflows.telemac.modules.telemac2d import (
    GreenAmpt, Rating, Storm,
)
from trid3nt.workflows.telemac.templates.rain_on_grid.rain_on_grid import (
    LANDCOVER_CN_MANNING, LANDCOVER_UNMAPPED, MESH,
)
from trid3nt.workflows.telemac.workflow import Measured, TelemacWorkflow

__all__ = ["CAPTIONS", "MESH", "OUTPUTS", "INPUTS", "STEERING", "telemac_rain_on_soil"]


_GEOMETRY = "rog.slf"
_BOUNDARY = "rog.cli"
_RESULT = "r2d_rog.slf"

# half-width; the delineation refuses a basin the window clips
_BASIN_WINDOW_KM = 15.0


class INPUTS:
    pour_point = Input()

    rain_series_mm = Input().stated()
    design_storm_mm_per_day = Input().stated()

    mesh_resolution_m = RESOLUTION
    mesh_max_edge_m = MAX_EDGE

    domain = Input(DOMAIN).need("hydrography", kind="basin", at="pour_point",
                                span_km=_BASIN_WINDOW_KM)
    bed = Input(BED).need("terrain").steps("merge_surfaces", "interpolate_surface")
    rivers = Input().need("hydrography", geometry="polyline")
    landcover = Input().need("land cover")
    soil = Input(SOIL).need("soil", of="sand")
    rain = Input(RAIN).need("precipitation series").optional_need()


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

    # Manning: the land-cover table's roughness column is n
    LAW_OF_BOTTOM_FRICTION = 4

    # Green-Ampt
    RAINFALL_RUNOFF_MODEL = 3

    storm = Storm(mm_per_day="design_storm_mm_per_day",
                  hours=6.0,
                  series="rain_series_mm",
                  record="rain",
                  tracers=0)
    green_ampt = GreenAmpt(soil="soil", landcover="landcover",
                           table=LANDCOVER_CN_MANNING, unmapped=LANDCOVER_UNMAPPED)

    rating = Rating(measured="outlet")


OUTPUTS = [
    series("FLUX", at="pour_point").chart(),
    series("FLUX", at="pour_point").station(),
]
CAPTIONS = {"FLUX": "outlet hydrograph"}

_METADATA = AtomicToolMetadata(
    name="telemac_rain_on_soil",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    engine="telemac",
    tier="template",
)

REVIEW_TITLE = "Review the storm, the soil, the catchment and the mesh band"

telemac_rain_on_soil = register_workflow(
    TelemacWorkflow, _METADATA, sys.modules[__name__],
    sensitivity=(("water_depth", "peak"),
                 ("outlet_hydrograph", "peak")),
    coerce=(
        point_arg("pour_point", tool="telemac_rain_on_soil",
                  prompt="Click the catchment outlet the runoff drains to",
                  code="TELEMAC_ROS_PARAMS_INVALID"),
    ),
)
