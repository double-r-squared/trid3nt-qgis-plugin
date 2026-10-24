"""Engine template ``geoclaw_storm_surge`` - a real hurricane's wind and pressure over the sea, and how high the water stands on the coast."""

from __future__ import annotations

import sys

from trid3nt_contracts.tool_registry import AtomicToolMetadata
from trid3nt_server.inputs import point_arg
from trid3nt_server.inputs.input import Input
from trid3nt_server.inputs.slots import BED, DOMAIN, EXTENT
from trid3nt_server.tools.mesh.tool import mesh_op, tool
from trid3nt_server.workflows.geoclaw.modules import GEO
from trid3nt_server.workflows.geoclaw.workflow import GeoclawWorkflow
from trid3nt_server.workflows.runtime import register_workflow

__all__ = ["CAPTIONS", "DEEP_BED", "INPUTS", "MESH", "OUTPUTS", "SEA_LEVEL_DATUM", "STEERING", "geoclaw_storm_surge"]

# The values below are those of Clawpack's storm-surge example for Hurricane Ike
# (geoclaw/examples/storm-surge/ike/setrun.py, v5.14.0) unless a comment says where else they come from.

#: The tidal datum the sea stands at when the storm is far away, measured at the seed on the run's frame (as the tsunami template's).
SEA_LEVEL_DATUM = "MHW"

#: The source of the coarse rung of the bed, under the fine one, over the whole grid.
DEEP_BED = "fetch_etopo"

#: The level-1 cell: a quarter of a degree (degree_factor 4 in the example's setrun), along a meridian (111,320 m per degree).
_LEVEL1_M = 0.25 * 111_320.0

#: One ratio per finer level: the first four of the example's refinement_ratios_x [2, 2, 2, 6, 16], five levels, the finest 0.25 / 48 degrees.
_RATIOS = [2, 2, 2, 6]
_FINEST = len(_RATIOS) + 1

#: The run opens three days before landfall and ends one day after it (clawdata.t0 = -days2seconds(3), tfinal = days2seconds(1)).
_T0 = -3.0 * 86400.0


class INPUTS:
    storm_name = Input(type=str, desc=(
        "The name of the storm, as the IBTrACS archive spells it: 'IKE', 'KATRINA'. With the season it picks the "
        "one best track whose wind and pressure drive the run."))
    season = Input(type=int, bounds=(1842, 2100), units="year", desc=(
        "The season (calendar year) of the storm; names are reused across years."))
    seed = Input(desc=(
        "A point on the water off the coast: where the vertical datum offset is measured and where the "
        "gauge stands, as a Point; the pick's {coordinates, name} verbatim, a (lon, lat) pair or 'lat,lon'. "
        "An offset of a tidal datum exists at the water only, and the point is never moved.")).stated()
    level1_cell_m = Input(
        type=float, default=_LEVEL1_M, units="m",
        desc=("The level-1 cell edge along a meridian, in metres; the example's quarter degree. "
              "The user's lever for how coarse the open Gulf is, and nothing refuses one."))
    deep_bed = Input(desc=(
        "The coarse surface under the fine bed, over the whole grid: a raster or a layer name on the "
        "run's vertical frame; unstated, ETOPO 2022 is fetched over the grid.")).stated()

    extent = Input(EXTENT)
    domain = Input(DOMAIN).need(at="seed")
    bed = Input(BED).need("bathymetry")


class STEERING(GEO):
    # the shallow-water numerics of the example's setrun (the limiter 'mc', source splitting 'godunov', extrapolating edges)
    CLAW_NUM_EQN = 3
    CLAW_NUM_AUX = 7  # 3 shallow, 1 friction, 3 storm fields
    CLAW_CAPA_INDEX = 2
    CLAW_NUM_WAVES = 3
    CLAW_LIMITER = [4, 4, 4]
    CLAW_USE_FWAVES = True
    CLAW_SOURCE_SPLIT = 1
    CLAW_DIMENSIONAL_SPLIT = 0  # unsplit
    CLAW_BC_LOWER = [1, 1]  # extrapolate
    CLAW_BC_UPPER = [1, 1]
    CLAW_DT_INITIAL = 0.016
    CLAW_CFL_DESIRED = 0.75
    CLAW_STEPS_MAX = 5000
    AMR_AUX_TYPE = ["center", "capacity", "yleft", "center", "center", "center", "center"]
    AMR_REGRID_INTERVAL = 3
    AMR_REGRID_BUFFER_WIDTH = 2
    AMR_VERBOSITY_REGRID = 0
    REFINEMENT_VARIABLE_DT_REFINEMENT_RATIOS = True
    REFINEMENT_WAVE_TOLERANCE = 1.0
    REFINEMENT_SPEED_TOLERANCE = [1.0, 2.0, 3.0, 4.0]

    # the sea in lon/lat metres with the Coriolis force, friction at every depth, over the example's physics
    GEO_COORDINATE_SYSTEM = 2
    GEO_GRAVITY = 9.81
    GEO_EARTH_RADIUS = 6367.5e3
    GEO_RHO = 1025.0
    GEO_RHO_AIR = 1.15
    GEO_AMBIENT_PRESSURE = 101.3e3
    GEO_CORIOLIS_FORCING = True
    GEO_FRICTION_FORCING = True
    GEO_FRICTION_DEPTH = 1.0e10
    GEO_DRY_TOLERANCE = 1.0e-2
    # Manning 0.022 where the bed is below zero and 0.030 above: the example's domain-wide friction region
    # (depths [inf, 0, -inf], coefficients [0.030, 0.022]) as the engine's depth law
    GEO_MANNING_COEFFICIENT = [0.022, 0.030]
    GEO_MANNING_BREAK = [0.0]

    # wind and pressure from the storm's track by the Holland (1980) model, the Garratt drag law, and the refinement
    # the example states for the wind (m/s) and for the distance from the eye (m)
    SURGE_WIND_FORCING = True
    SURGE_DRAG_LAW = 1
    SURGE_PRESSURE_FORCING = True
    SURGE_DISPLAY_LANDFALL_TIME = True
    SURGE_WIND_REFINE = [20.0, 40.0, 60.0]
    SURGE_R_REFINE = [60.0e3, 40.0e3, 20.0e3]
    SURGE_STORM_SPECIFICATION_TYPE = 1  # holland80

    # three days before landfall to one day after, the fixed grids alone written
    CLAW_T0 = _T0
    CLAW_TFINAL = 86400.0
    CLAW_NUM_OUTPUT_TIMES = 0
    CLAW_OUTPUT_T0 = False
    CLAW_OUTPUT_FORMAT = 2  # binary32
    FGMAX_NUM_FGMAX_VAL = 2

    Topography = [{"surface": "deep_bed", "offset": "deep_bed_offset"}, {"surface": "bed"}]
    Storm = {"storm_tracks": "storm_tracks", "storm_name": "storm_name", "season": "season"}
    # the products on the finest cells of the coast, a frame every three hours, the maxima watched from the first instant
    FixedGrids = {"extent": "domain", "interval": 10800.0, "tstart_max": _T0, "dt_check": 10.0,
                  "arrival_tol": 0.2, "interp_method": 0}


MESH = tool.build_mesh(
    mesher="claw_grid",
    kind="amr_grid",
    extent="extent",
    resolution_m="level1_cell_m",
    build={"refinement_ratios": _RATIOS},
    ops=[
        # every level but the finest may be flagged anywhere (by the wave, the speed, the wind and the distance from the eye),
        # and the coast is held at the finest from the first instant
        mesh_op("flagregion", name="Region_domain", minlevel=1, maxlevel=_FINEST - 1, t1=-1.0e9, t2=1.0e9, extent="extent"),
        mesh_op("flagregion", name="Region_coast", minlevel=_FINEST, maxlevel=_FINEST, t1=-1.0e9, t2=1.0e9, extent="domain"),
    ],
)

OUTPUTS: list = []
CAPTIONS: dict = {}

_METADATA = AtomicToolMetadata(
    name="geoclaw_storm_surge",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    engine="geoclaw",
    tier="template",
)

REVIEW_TITLE = "Review the grid, the storm and the sea level"

geoclaw_storm_surge = register_workflow(
    GeoclawWorkflow, _METADATA, sys.modules[__name__],
    coerce=(
        point_arg("seed", tool="geoclaw_storm_surge", prompt=None, code="GEOCLAW_PARAMS_INVALID"),
    ),
)
