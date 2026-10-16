"""Engine template ``modflow_water_table`` - the steady water table of an unconfined aquifer under recharge, drained by its streams."""

from __future__ import annotations

import sys

from trid3nt_contracts.tool_registry import AtomicToolMetadata
from trid3nt_server.inputs import point_arg
from trid3nt_server.inputs.input import Input
from trid3nt_server.inputs.slots import BED, DOMAIN
from trid3nt_server.tools.mesh.tool import mesh_op, tool
from trid3nt_server.workflows.modflow.modules import GWF
from trid3nt_server.workflows.modflow.workflow import ModflowWorkflow
from trid3nt_server.workflows.runtime import register_workflow
from trid3nt_server.workflows.runtime.primitives import mass_balance

__all__ = ["CAPTIONS", "INPUTS", "MESH", "OUTPUTS", "STEERING", "modflow_water_table"]


class INPUTS:
    drain_conductance = Input()
    well = Input().stated()
    well_rate = Input().stated()

    domain = Input(DOMAIN).need("hydrography", kind="basin")
    bed = Input(BED, desc=(
        "The land surface: the terrain the mesh paints, which the aquifer's top "
        "is and the streams drain at. A DEM, or a depth in metres below the "
        "model's zero for a flat surface.")).need("terrain")
    thickness = Input(desc=(
        "How thick the aquifer is under the land surface, in metres: a number, "
        "or a raster publishing its unit.")).need(geometry="raster")
    conductivity = Input(desc=(
        "The aquifer's horizontal hydraulic conductivity, in metres per day: a "
        "number, or a raster publishing its unit.")).need(geometry="raster")
    water_table_depth = Input(desc=(
        "How far below the land surface the water table stands before the "
        "solve, in metres: a number, or a raster publishing its unit.")).need(
        geometry="raster")
    recharge = Input(desc=(
        "What reaches the water table over the domain, in metres per day: a "
        "number, or a raster publishing its unit (mm/yr is read onto the "
        "model's own).")).need(geometry="raster")
    streams = Input().need("hydrography", geometry="polyline")


class STEERING(GWF):
    DISV_LENGTH_UNITS = "meters"
    TDIS_TIME_UNITS = "days"

    # one steady period: no storage package, so the length marches nothing
    Periods = [[1.0, 1, 1.0]]

    # an unconfined water table is the question
    NPF_ICELLTYPE = 1
    NPF_SAVE_FLOWS = True
    # Under the standard formulation a convertible cell an iterate drops below its bottom goes dry and never rewets, and a
    # steady water table dries whole; Newton-Raphson needs the moderate solver's BiCGSTAB to converge
    NAM_NEWTONOPTIONS = "NEWTON"
    IMS_COMPLEXITY = "MODERATE"

    OC_HEAD_FILERECORD = "gwf.hds"
    OC_BUDGET_FILERECORD = "gwf.cbc"
    OC_SAVERECORD = [["HEAD", "ALL"], ["BUDGET", "ALL"]]

    Aquifer = {"thickness": "thickness", "conductivity": "conductivity",
               "water_table_depth": "water_table_depth"}
    Recharge = {"geometry": "domain", "layer": 1, "rate": "recharge"}
    # at the land surface: a stream drains the aquifer wherever the head stands above its bed
    Drains = {"geometry": "streams", "layer": 1, "elevation": "bed",
              "conductance": "drain_conductance"}
    Wells = {"geometry": "well", "layer": 1, "rate": "well_rate"}


MESH = tool.build_mesh(
    mesher="triangle",
    kind="unstructured_tri",
    extent="domain",
    resolution_m="mesh_resolution_m",
    ops=[mesh_op("set_bed", source="bed")],
)

OUTPUTS = [
    mass_balance().chart(),
]
CAPTIONS = {"mass_balance": "water budget"}

_METADATA = AtomicToolMetadata(
    name="modflow_water_table",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    engine="modflow",
    tier="template",
)

REVIEW_TITLE = "Review the aquifer, its recharge and its drains"

modflow_water_table = register_workflow(
    ModflowWorkflow, _METADATA, sys.modules[__name__],
    coerce=(
        point_arg("well", tool="modflow_water_table", prompt=None,
                  code="MODFLOW_PARAMS_INVALID"),
    ),
)
