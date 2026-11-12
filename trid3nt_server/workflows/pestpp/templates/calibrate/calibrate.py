"""Engine template ``calibrate`` - a finished run's parameters tuned against what was measured where it was solved."""

from __future__ import annotations

import sys
from types import MappingProxyType

from trid3nt_contracts.tool_registry import AtomicToolMetadata
from trid3nt_server.inputs.input import Input
from trid3nt_server.inputs.slots import OBSERVE, RUN
from trid3nt_server.workflows.pestpp.workflow import ENGINES, PestppWorkflow
from trid3nt_server.workflows.runtime import register_workflow

__all__ = ["CAPTIONS", "INPUTS", "calibrate"]


class INPUTS:
    run = Input(RUN, desc=(
        "The finished run whose parameters are tuned: its run id, or the handle the case shows for one of its "
        "layers; unstated, this case's finished run, the newest offered where it holds several. Its deck, mesh "
        "and frame are the study's model."), coercion=MappingProxyType({"engines": ENGINES}))
    parameters = Input()
    tune = Input().stated()
    observe = Input(OBSERVE, desc=(
        "The wells the run is held to: a point layer of wells reporting their water level; unfilled, every "
        "measured groundwater level the source reports over the run's ground. A depth below the land surface "
        "is read as a head at the well's cell, an elevation only on the run's own frame.")).need(
        "groundwater level", geometry="point", every=True)


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
