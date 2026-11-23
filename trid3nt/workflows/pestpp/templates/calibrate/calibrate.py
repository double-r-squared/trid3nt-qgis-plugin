"""Engine template ``calibrate`` - a finished run's parameters tuned against what was measured where it was solved."""

from __future__ import annotations

import sys
from types import MappingProxyType

from trid3nt_contracts.tool_registry import AtomicToolMetadata
from trid3nt.inputs.input import Input
from trid3nt.inputs.slots import LEVEL, OBSERVE, RUN
from trid3nt.workflows.pestpp.workflow import ENGINES, PestppWorkflow
from trid3nt.workflows.runtime import register_workflow

__all__ = ["CAPTIONS", "INPUTS", "calibrate"]


class INPUTS:
    run = Input(RUN, desc=(
        "The finished run whose parameters are tuned: its run id, or the handle the case shows for one of its "
        "layers; unstated, this case's finished run, the newest offered where it holds several. Its deck, mesh "
        "and frame are the study's model."), coercion=MappingProxyType({"engines": ENGINES}))
    parameters = Input()
    tune = Input().stated()
    stages = Input(type=list[str], desc=(
        "Stages in order: screen, calibrate, resolve. Omit a stage to skip it. Resolve requires prior calibration. "
        "Unstated, screen only when tune is smaller than the number named, then calibrate and resolve.")).stated()
    parameter_data = Input(type=dict, desc=(
        "PEST++ parameter table values by authored PARNME. Columns: PARNME, PARTRANS, PARCHGLIM, PARVAL1, "
        "PARLBND, PARUBND, PARGP, SCALE, OFFSET, DERCOM, PARTIED. Native values in PEST coordinates.")).stated()
    method = Input()
    sigma = Input().stated()
    balance = Input().stated()
    observe = Input(OBSERVE, desc=(
        "The wells the run is held to: a point layer of wells reporting their water level; unfilled, every "
        "measured groundwater level the source reports over the run's ground. A depth below the land surface "
        "is read as a head at the well's cell, an elevation only on the run's own frame; a MODFLOW run's.")).need(
        "groundwater level", geometry="point", every=True)
    gauges = Input(LEVEL, desc=(
        "The gauges a TELEMAC run is held to: a point layer of gauges reporting their water level; unfilled, "
        "every gauge the source reports over the run's ground. Each reading is lifted onto the run's own frame "
        "and held against the free surface at the run's end, at the node the run's point read lands the gauge "
        "on; a gauge the mesh does not hold is left out and said.")).need(
        "water level series", geometry="point", every=True)


CAPTIONS = {"phi": "objective function per iteration",
            "fit": "simulated against observed",
            "residuals": "simulated minus observed",
            "parameters": "calibrated multipliers"}

_METADATA = AtomicToolMetadata(
    name="calibrate",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    engine="pestpp",
    tier="template",
)

calibrate = register_workflow(PestppWorkflow, _METADATA, sys.modules[__name__])
"""Engine template ``calibrate``."""
