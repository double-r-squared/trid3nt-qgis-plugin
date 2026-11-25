"""Several caller-stated objectives and the feasible non-dominated pumping decisions on a native MODFLOW 6 model."""

import sys

from trid3nt.inputs.input import Input
from trid3nt.workflows.pestpp.modules import PESTPP
from trid3nt.workflows.pestpp.tradeoff import TradeoffWorkflow
from trid3nt.workflows.runtime.bare import register_bare


class INPUTS:
    files = Input(type=dict, desc="Pinned native simulation files by deck filename, including mfsim.nam.")
    model = Input(type=str, desc="The GWF model name in the MODFLOW 6 simulation.")
    decisions = Input(type=list[dict], desc=(
        "Decisions by name with package, one-based period and native cellid, lower and upper rate bounds and source."))
    objectives = Input(type=list[dict], desc=(
        "Two or more native OBS forecasts with name, type, id, optional id2, package and model, one-based period "
        "and step, units and source. State each sense as min or max."))
    constraints = Input(type=list[dict], stated_only=True, desc=(
        "Optional native OBS forecasts with the same selectors and source as the objectives. State each sense as "
        "<= or >= and its limit in the forecast's units."))
    generations = Input(type=int, desc="Positive number of PESTPP-MOU generations (noptmax).")
    population = Input(type=int, desc="Positive PESTPP-MOU population size.")
    generator = Input(type=str, desc="PESTPP-MOU generator: de, pso, sbx, pm or simplex.")
    seed = Input(type=int, desc="Unsigned integer seed for PEST++'s random number generator (rand_seed).")


STEERING = PESTPP
tradeoff = register_bare(TradeoffWorkflow, "tradeoff", sys.modules[__name__])
