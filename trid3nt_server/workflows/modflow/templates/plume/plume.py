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
    transmissivity = Input(desc=(
        "The aquifer's transmissivity: a number in square metres per day, or a "
        "raster publishing its unit; unfilled, the source publishing "
        "aquifer_transmissivity here.")).need(
        "aquifer property", of="aquifer_transmissivity")
    water_table_depth = Input(desc=(
        "How far below the land surface the water table stands before the "
        "solve: a number in metres, or a raster publishing its unit; unfilled, "
        "the source publishing water_table_depth here.")).need(
        "groundwater level", of="water_table_depth")
    recharge = Input(desc=(
        "What reaches the water table over the domain: a number in metres per "
        "day, or a raster publishing its unit; unfilled, the source publishing "
        "groundwater_recharge here.")).need(
        "aquifer property", of="groundwater_recharge")
    streams = Input().need("hydrography", geometry="polyline")
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
               "transmissivity": "transmissivity",
               "water_table_depth": "water_table_depth",
               "layers": "layers"}
    # No storage package: the flow is the steady one every period re-solves. The effluent is recharge over the source, on top of the natural recharge, and each carries its own concentration.
    Recharge = [{"geometry": "domain", "layer": 1, "rate": "recharge",
                 "concentration": "background_concentration"},
                {"geometry": "source", "layer": 1, "rate": "source_rate",
                 "concentration": "source_concentration"}]
    # The domain's outer cells hold the fetched water table, the head the model starts from: the aquifer continues past the domain, so a closed edge would pile the recharge up.
    ConstantHead = {"geometry": {"cells": "edge"}, "layer": "all", "head": {"cells": "start"}}
    Drains = {"geometry": "streams", "layer": 1, "elevation": "bed",
              "conductance": "drain_conductance"}

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
