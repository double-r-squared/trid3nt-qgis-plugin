"""Bounded deterministic management optimization on a native MODFLOW 6 model."""

import sys

from trid3nt.inputs.input import Input
from trid3nt.workflows.pestpp.modules import PESTPP
from trid3nt.workflows.pestpp.optimization import OptimizeWorkflow
from trid3nt.workflows.runtime.bare import register_bare


class INPUTS:
    files = Input(type=dict, desc="Pinned native simulation files by deck filename, including mfsim.nam.")
    model = Input(type=str, desc="The GWF model name in the MODFLOW 6 simulation.")
    decisions = Input(type=list[dict], desc=(
        "Decisions by name with package, one-based period and native cellid, lower and upper rate bounds and source."))
    objective = Input(type=dict, desc=(
        "One native OBS forecast with name, type, id, optional id2, package and model, one-based period and step, "
        "units and source. State sense as min or max."))
    constraints = Input(type=list[dict], desc=(
        "Native OBS forecasts with the same selectors and source as the objective. State sense as <= or >= "
        "and limit in the forecast's units."))
    iterations = Input(type=int, desc="Positive number of PESTPP-OPT iterations.")


STEERING = PESTPP
optimize = register_bare(OptimizeWorkflow, "optimize", sys.modules[__name__])
