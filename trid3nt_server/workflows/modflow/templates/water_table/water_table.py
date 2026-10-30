"""Engine template ``modflow_water_table`` - the steady water table of an unconfined aquifer under recharge, drained by its streams."""

from __future__ import annotations

import sys

from trid3nt_contracts.tool_registry import AtomicToolMetadata
from trid3nt_server.inputs import point_arg
from trid3nt_server.inputs.input import Input
from trid3nt_server.inputs.slots import BED, DOMAIN, OBSERVE
from trid3nt_server.tools.mesh.tool import mesh_op, tool
from trid3nt_server.workflows.modflow.modules import GWF
from trid3nt_server.workflows.modflow.workflow import ModflowWorkflow
from trid3nt_server.workflows.runtime import register_workflow
from trid3nt_server.workflows.runtime.primitives import mass_balance, series

__all__ = ["CAPTIONS", "INPUTS", "MESH", "OUTPUTS", "STEERING", "modflow_water_table"]


class INPUTS:
    drain_conductance = Input()
    lake_bottom = Input().stated()
    lake_leakance = Input().stated()
    well = Input().stated()
    well_rate = Input().stated()

    domain = Input(DOMAIN).need("hydrography", kind="basin")
    bed = Input(BED, desc=(
        "The land surface: the terrain the mesh paints, which the aquifer's top "
        "is and the streams drain at. A DEM, or a depth in metres below the "
        "model's zero for a flat surface.")).need("terrain")
    saturated_thickness = Input(desc=(
        "How thick the saturated aquifer is, its base that far below the water "
        "table: a number in metres, or a raster publishing its unit; unfilled, "
        "the source publishing aquifer_saturated_thickness here.")).need(
        "aquifer property", of="aquifer_saturated_thickness")
    conductivity_layers = Input(desc=(
        "The aquifer's conductivity by depth, one raster of metres per day for each layer of "
        "the published USGS model whose grid holds the domain; unfilled, the source publishing "
        "aquifer_hydraulic_conductivity here, and absent where none does. Where it fills, "
        "transmissivity is not asked for; a transmissivity you state is used in its place.")).need(
        "aquifer property", of="aquifer_hydraulic_conductivity").optional_need(instead_of="transmissivity")
    layer_surfaces = Input(desc=(
        "The top of the published model's first layer and the bottom of each layer, one raster of "
        "metres each, which place the conductivity by depth; unfilled, the source publishing "
        "aquifer_layer_elevation here, and absent where none does.")).need(
        "aquifer property", of="aquifer_layer_elevation").optional_need(instead_of="transmissivity")
    transmissivity = Input(desc=(
        "The aquifer's transmissivity: a number in square metres per day, or a "
        "raster publishing its unit; unfilled, the source publishing "
        "aquifer_transmissivity here. Not asked for where the conductivity by depth fills.")).need(
        "aquifer property", of="aquifer_transmissivity")
    water_table = Input(desc=(
        "Where the water table stands before the solve, as an elevation on the "
        "run's vertical frame: a number in metres, or a raster publishing its unit "
        "and datum; unfilled, the source publishing water_table_elevation here, "
        "and absent where none measures every cell of the domain. Where it fills, "
        "the depth is not asked for; a depth you state is used in its place.")).need(
        "groundwater level", of="water_table_elevation").optional_need(instead_of="water_table_depth")
    water_table_depth = Input(desc=(
        "How far below the land surface the water table stands before the "
        "solve: a number in metres, or a raster publishing its unit; unfilled, "
        "the source publishing water_table_depth here. Not asked for where the "
        "water table elevation fills.")).need(
        "groundwater level", of="water_table_depth")
    recharge = Input(desc=(
        "What reaches the water table over the domain: a number in metres per "
        "day, or a raster publishing its unit; unfilled, the source publishing "
        "groundwater_recharge here.")).need(
        "aquifer property", of="groundwater_recharge")
    streams = Input(desc=(
        "The streams that drain the aquifer: the NHDPlus flowline network over the domain, each line a drain "
        "at the land surface; unfilled, the source publishing the flowline network here.")).need(
        "hydrography", of="flowline network", geometry="polyline")
    waterbodies = Input(desc=(
        "The lakes, ponds and reservoirs of the domain as polygons; unfilled, the source publishing "
        "waterbodies here, and absent where none does. Each one holding a cell centre of the mesh is a lake "
        "that exchanges water with the aquifer only; one smaller than the mesh is skipped and said.")).need(
        "hydrography", of="waterbodies", geometry="polygon").optional_need()
    observe = Input(OBSERVE, desc=(
        "A well's measured water level the solved water table is checked "
        "against at the well's own cell: a point layer of wells reporting it; "
        "unfilled, the measured groundwater levels here.")).need(
        "groundwater level", geometry="point")


class STEERING(GWF):
    DISV_LENGTH_UNITS = "meters"
    TDIS_TIME_UNITS = "days"

    # one steady period: no storage package, so the length marches nothing
    Periods = [[1.0, 1, 1.0]]

    # an unconfined water table is the question
    NPF_ICELLTYPE = 1
    NPF_SAVE_FLOWS = True
    # Under the standard formulation a convertible cell an iterate drops below its bottom goes dry and never rewets, and a
    # steady water table dries whole; Newton-Raphson over measured terrain converges only under the complex solver preset
    NAM_NEWTONOPTIONS = "NEWTON"
    IMS_COMPLEXITY = "COMPLEX"

    OC_HEAD_FILERECORD = "gwf.hds"
    OC_BUDGET_FILERECORD = "gwf.cbc"
    OC_SAVERECORD = [["HEAD", "ALL"], ["BUDGET", "ALL"]]

    Aquifer = {"saturated_thickness": "saturated_thickness",
               "conductivity": {"layers": "conductivity_layers", "surfaces": "layer_surfaces"},
               "transmissivity": "transmissivity",
               "water_table": "water_table",
               "water_table_depth": "water_table_depth"}
    Recharge = {"geometry": "domain", "layer": 1, "rate": "recharge"}
    # at the land surface: a stream drains the aquifer wherever the head stands above its bed
    Drains = {"geometry": "streams", "layer": 1, "elevation": "bed",
              "conductance": "drain_conductance"}
    # a lake stands at the flat water surface the bed paints inside its polygon; its bed leaks both ways
    Lakes = {"geometry": "waterbodies", "stage": {"surface": "bed"}, "bottom": "lake_bottom",
             "leakance": "lake_leakance"}
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
    series("HEAD", at="observe").note(),
]
CAPTIONS = {"mass_balance": "water budget",
            "HEAD": "the water table at the observed well"}

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
