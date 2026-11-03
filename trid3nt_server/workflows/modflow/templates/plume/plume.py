"""Engine template ``modflow_plume`` - where a dissolved release goes: a conservative solute carried by the infiltrating water of a source polygon through a steady unconfined flow."""

from __future__ import annotations

import sys

from trid3nt_contracts.tool_registry import AtomicToolMetadata
from trid3nt_server.inputs.input import Input
from trid3nt_server.inputs.slots import BED, DOMAIN, OBSERVE
from trid3nt_server.tools.mesh.tool import mesh_op, tool
from trid3nt_server.workflows.modflow.modules import GWF
from trid3nt_server.workflows.modflow.workflow import ModflowWorkflow
from trid3nt_server.workflows.runtime import register_workflow
from trid3nt_server.workflows.runtime.primitives import mass_balance, series

__all__ = ["CAPTIONS", "INPUTS", "MESH", "OUTPUTS", "STEERING", "modflow_plume"]


class INPUTS:
    drain_conductance = Input()
    lake_bottom = Input().stated()
    lake_leakance = Input().stated()
    river_bottom = Input().stated()
    river_leakance = Input().stated()
    stream_width = Input().stated()
    stream_bed_thickness = Input().stated()
    stream_bed_k = Input().stated()
    stream_manning = Input().stated()
    source_rate = Input()
    source_concentration = Input()
    background_concentration = Input()
    porosity = Input()
    longitudinal_dispersivity = Input()
    transverse_dispersivity = Input()
    snapshot = Input()
    solute = Input()
    until = Input()
    time_steps = Input()
    layers = Input().stated()

    domain = Input(DOMAIN).need("hydrography", kind="basin")
    source = Input(desc=(
        "The polygon the effluent infiltrates over, as a drawn shape or a "
        "layer: the beds of the release. The run places the source on the "
        "cells whose centres it holds.")).need(geometry="polygon")
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
        "at the land surface, or where stream_width is stated a routed stream whose slope is the flowline's own; "
        "unfilled, the source publishing the flowline network here.")).need(
        "hydrography", of="flowline network", geometry="polyline")
    waterbodies = Input(desc=(
        "The lakes, ponds and reservoirs of the domain as polygons; unfilled, the source publishing "
        "waterbodies here, and absent where none does. Each one holding a cell centre of the mesh is a lake "
        "that exchanges water with the aquifer, and where stream_width is stated and streams carries the tags "
        "mark_lake_outlets writes also discharges into the stream line leaving it; one smaller than the mesh is "
        "skipped and said.")).need(
        "hydrography", of="waterbodies", geometry="polygon").optional_need()
    river_areas = Input(desc=(
        "The rivers wide enough to have two banks, as the polygons NHD maps their water surface in; unfilled, the "
        "source publishing river areas here, and absent where none does. Every solved cell a polygon overlaps is a "
        "river cell, exchanging with the aquifer over the river's measured area in it, in place of the drain of a "
        "flowline running through it; a cell whose DEM pixels show no flat water level is left out and said.")).need(
        "hydrography", of="river areas", geometry="polygon").optional_need()
    observe = Input(OBSERVE, desc=(
        "The measured concentrations of the solute at wells, at the snapshot: "
        "every well reporting it, each compared with the model at its own "
        "cell; unfilled, the measured water quality here.")).need(
        "water quality sample", of="CONCENTRATION", when="snapshot", every=True,
        measured="solute")


class STEERING(GWF):
    DISV_LENGTH_UNITS = "meters"
    TDIS_TIME_UNITS = "days"

    # an unconfined water table is the question
    NPF_ICELLTYPE = 1
    NPF_SAVE_FLOWS = True
    # Newton-Raphson over measured terrain converges only under the complex solver preset
    NAM_NEWTONOPTIONS = "NEWTON"
    IMS_COMPLEXITY = "COMPLEX"

    OC_HEAD_FILERECORD = "gwf.hds"
    OC_BUDGET_FILERECORD = "gwf.cbc"
    OC_SAVERECORD = [["HEAD", "LAST"], ["BUDGET", "LAST"]]

    # The clock: one period from the run's start to `until`, over `time_steps` equal steps.
    Periods = {"until": "until", "steps": "time_steps"}
    Aquifer = {"saturated_thickness": "saturated_thickness",
               "conductivity": {"layers": "conductivity_layers", "surfaces": "layer_surfaces"},
               "transmissivity": "transmissivity",
               "water_table": "water_table",
               "water_table_depth": "water_table_depth",
               "layers": "layers"}
    # No storage package: the flow is the steady one every period re-solves. The effluent is recharge over the source, on top of the natural recharge, and each carries its own concentration.
    Recharge = [{"geometry": "domain", "layer": 1, "rate": "recharge",
                 "concentration": "background_concentration"},
                {"geometry": "source", "layer": 1, "rate": "source_rate",
                 "concentration": "source_concentration"}]
    # The domain's outer cells hold the fetched water table, the head the model starts from: the aquifer continues past the domain, so a closed edge would pile the recharge up.
    ConstantHead = {"geometry": {"cells": "edge"}, "layer": "all", "head": {"cells": "start"}}
    Drains = {"geometry": "streams", "unless": "stream_width", "layer": 1, "elevation": "bed",
              "conductance": "drain_conductance"}
    # where a width is stated the lines are routed streams at the same land surface, in place of the drains
    Streams = {"geometry": "streams", "when": "stream_width", "layer": 1, "name": "streams", "width": "stream_width",
               "slope": {"field": "slope"}, "bed_top": "bed", "bed_thickness": "stream_bed_thickness",
               "bed_conductivity": "stream_bed_k", "manning": "stream_manning"}
    # a lake stands at the flat water surface the bed paints inside its polygon; its bed leaks both ways
    # where a width is stated, a lake drains into the line a flowline layer tagged by mark_lake_outlets leaves it by
    Lakes = {"geometry": "waterbodies", "stage": {"surface": "bed"}, "bottom": "lake_bottom",
             "leakance": "lake_leakance", "name": "lakes",
             "into": {"streams": "streams", "from_key": "permanent_identifier", "to_key": "receives_from_waterbody",
                      "when": "stream_width"}}
    # a river stands at the flat water surface the bed paints inside its polygon, and a drain in a cell it holds is not written
    Rivers = {"geometry": "river_areas", "flowlines": "streams", "layer": 1, "stage": {"surface": "bed"},
              "conductance": {"leakance": "river_leakance"}, "rbot": "river_bottom"}

    # A total variation diminishing scheme: the upstream one smears a plume front across several cells
    Transport = {"slots": {
        "NAM_SAVE_FLOWS": True,
        "IC_STRT": "background_concentration",
        "MST_POROSITY": "porosity",
        "DSP_ALH": "longitudinal_dispersivity",
        "DSP_ATH1": "transverse_dispersivity",
        "ADV_SCHEME": "tvd",
        "IMS_COMPLEXITY": "COMPLEX",
        "OC_CONCENTRATION_FILERECORD": "gwt.ucn",
        "OC_BUDGET_FILERECORD": "gwt.cbc",
        "OC_SAVERECORD": [["CONCENTRATION", "ALL"], ["BUDGET", "LAST"]]}}


MESH = tool.build_mesh(
    mesher="triangle",
    kind="unstructured_tri",
    extent="domain",
    resolution_m="mesh_resolution_m",
    ops=[mesh_op("set_bed", source="bed")],
)

OUTPUTS = [
    mass_balance().chart(),
    series("CONCENTRATION", at="observe", module="gwt").chart(),
]
CAPTIONS = {"mass_balance": "water budget",
            "CONCENTRATION": "the solute at the measured wells"}

_METADATA = AtomicToolMetadata(
    name="modflow_plume",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    engine="modflow",
    tier="template",
)

REVIEW_TITLE = "Review the aquifer, the release and the transport"

modflow_plume = register_workflow(ModflowWorkflow, _METADATA, sys.modules[__name__])
