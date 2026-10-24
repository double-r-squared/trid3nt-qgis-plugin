"""Engine template ``geoclaw_tsunami_inundation`` - a tsunami from a sea-floor rupture, across the sea to a coast, and how far inland it floods."""

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

__all__ = ["CAPTIONS", "DEEP_BED", "INPUTS", "MESH", "OUTPUTS", "RISE_SHAPE", "SEA_LEVEL_DATUM", "STEERING",
           "geoclaw_tsunami_inundation"]

# The values below are those of R. J. LeVeque's GeoClaw tsunami tutorial for Copalis Beach, WA
# (rjleveque.github.io/geoclaw_tsunami_tutorial, CopalisBeach/example2/setrun.py), the engine author's own case.

#: The tidal datum the tutorial's topography is shifted onto, so that its sea_level 0 is this datum (page "Make topofiles for Copalis Beach").
SEA_LEVEL_DATUM = "MHW"

#: The source of the coarse rung of the bed, under the fine one, over the whole grid (the tutorial's etopo22 30-arcsecond file).
DEEP_BED = "fetch_etopo"

#: How the sea floor rises in a kinematic rupture a run states: Clawpack's quadratic, the nearest of its two shapes to the finite-fault
#: file's asymmetric-cosine slip velocity (smooth, zero slope at both ends); a statement that names its own rise_shape keeps it.
RISE_SHAPE = "quadratic"

#: NOAA SIFT unit sources and the slip in metres on each: the tutorial's ASCE_SIFT_Region2 rupture (page "Make ASCE_SIFT_Region2 dtopo
#: file", the slips credited there to Yong Wei), built there with dtopotools.SiftFault.
_ASCE_SIFT_REGION2 = {"acsza57": 35.0, "acszb57": 35.0, "acsza58": 10.0, "acszb58": 33.0,
                      "acsza59": 10.0, "acszb59": 24.0, "acsza60": 30.0, "acszb60": 30.0}

#: The level-1 cell: 4 arcminutes, 15 cells per degree, along a meridian (111,320 m per degree).
_LEVEL1_M = 4.0 / 60.0 * 111_320.0

#: One ratio per finer level: 4' -> 2' -> 24" -> 12" -> 6" -> 3" -> 1", seven levels (the tutorial's list, whose seventh ratio its amr_levels_max leaves unused).
_RATIOS = [2, 5, 2, 2, 2, 3]
_FINEST = len(_RATIOS) + 1


class INPUTS:
    seed = Input(desc=(
        "A point on the water off the coast: where the vertical datum offsets are measured and where the "
        "gauge stands, as a Point; the pick's {coordinates, name} verbatim, a (lon, lat) pair or 'lat,lon'. "
        "An offset of a tidal datum exists at the water only, and the point is never moved.")).stated()
    level1_cell_m = Input(
        type=float, default=_LEVEL1_M, units="m",
        desc=("The level-1 cell edge along a meridian, in metres; the tutorial's 4 arcminutes. "
              "The user's lever for how coarse the open sea is, and nothing refuses one."))
    deep_bed = Input(desc=(
        "The coarse surface under the fine bed, over the whole grid: a raster or a layer name on the "
        "run's vertical frame; unstated, ETOPO 2022 is fetched over the grid.")).stated()

    initial_surface = Input(desc=(
        "A water surface the run opens at, as a raster of surface elevation on the run's vertical frame: "
        "a source stated by the sea surface it raises, beside the sea-floor rupture; unstated, the sea opens flat.")).stated()

    extent = Input(EXTENT)
    source = Input(EXTENT)
    approach = Input(EXTENT)
    domain = Input(DOMAIN).need(at="seed")
    bed = Input(BED).need("bathymetry")


class STEERING(GEO):
    # the shallow-water numerics of the tutorial's setrun
    CLAW_NUM_EQN = 3
    CLAW_NUM_AUX = 3
    CLAW_CAPA_INDEX = 2
    CLAW_NUM_WAVES = 3
    CLAW_LIMITER = [4, 4, 4]  # the MC limiter
    CLAW_USE_FWAVES = True
    CLAW_SOURCE_SPLIT = 1
    CLAW_DIMENSIONAL_SPLIT = 0  # unsplit
    CLAW_BC_LOWER = [1, 1]  # extrapolate
    CLAW_BC_UPPER = [1, 1]
    CLAW_DT_INITIAL = 0.2
    CLAW_CFL_DESIRED = 0.85
    CLAW_STEPS_MAX = 5000
    CLAW_VERBOSITY = 1
    AMR_AUX_TYPE = ["center", "capacity", "yleft"]
    AMR_REGRID_INTERVAL = 3
    AMR_REGRID_BUFFER_WIDTH = 2
    AMR_VERBOSITY_REGRID = 0
    REFINEMENT_VARIABLE_DT_REFINEMENT_RATIOS = True
    DTOPO_DT_MAX_DTOPO = 0.2
    QINIT_VARIABLE_ETA_INIT = True

    # the sea in lon/lat metres, without the Coriolis force, over the tutorial's friction and limits
    GEO_COORDINATE_SYSTEM = 2
    GEO_GRAVITY = 9.81
    GEO_CORIOLIS_FORCING = False
    GEO_FRICTION_DEPTH = 200.0
    GEO_SPEED_LIMIT = 20.0

    # one hour from the rupture, the fixed grids alone written
    CLAW_TFINAL = 3600.0
    CLAW_NUM_OUTPUT_TIMES = 0
    CLAW_OUTPUT_T0 = False
    CLAW_OUTPUT_FORMAT = 2  # binary32
    FGMAX_NUM_FGMAX_VAL = 2

    Topography = [{"surface": "deep_bed", "offset": "deep_bed_offset"}, {"surface": "bed"}]
    SiftFault = {"unit_sources": "sift_unit_sources", "slips": _ASCE_SIFT_REGION2,
                 "spacing": 10.0 / 3600.0, "buffer": 1.0, "times": [0.0]}
    InitialSurface = {"surface": "initial_surface"}
    # the products on the finest cells of the coast, a frame every two minutes, the maxima watched from the rupture
    FixedGrids = {"extent": "domain", "interval": 120.0, "tstart_max": 0.0, "dt_check": 10.0,
                  "arrival_tol": 0.2, "interp_method": 0}


MESH = tool.build_mesh(
    mesher="claw_grid",
    kind="amr_grid",
    extent="extent",
    resolution_m="level1_cell_m",
    build={"refinement_ratios": _RATIOS},
    ops=[
        # the whole grid to 2', the rupture's sea at level 4 while it moves, the sea to the coast to level 4
        # (the tutorial's Region_domain, Region_dtopo and Region_12sec), and the coast at the finest level from t = 0;
        # the source sea stays refined for the tutorial's 10 s of a static rupture, or until a stated kinematic rupture ends
        mesh_op("flagregion", name="Region_domain", minlevel=1, maxlevel=2, t1=0.0, t2=1.0e9, extent="extent"),
        mesh_op("flagregion", name="Region_dtopo", minlevel=4, maxlevel=4, t1=0.0, t2=10.0, extent="source",
                with_rupture=True),
        mesh_op("flagregion", name="Region_12sec", minlevel=3, maxlevel=4, t1=10.0, t2=1.0e9, extent="approach"),
        mesh_op("flagregion", name="Region_coast", minlevel=_FINEST, maxlevel=_FINEST, t1=0.0, t2=1.0e9,
                extent="domain"),
    ],
)

OUTPUTS: list = []
CAPTIONS: dict = {}

_METADATA = AtomicToolMetadata(
    name="geoclaw_tsunami_inundation",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    engine="geoclaw",
    tier="template",
)

REVIEW_TITLE = "Review the grid, the rupture and the sea level"

geoclaw_tsunami_inundation = register_workflow(
    GeoclawWorkflow, _METADATA, sys.modules[__name__],
    coerce=(
        point_arg("seed", tool="geoclaw_tsunami_inundation", prompt=None, code="GEOCLAW_PARAMS_INVALID"),
    ),
)
