"""Sequential data assimilation of measured heads across ordered cycle windows on a native MODFLOW 6 model."""

import sys

from trid3nt.inputs.input import Input
from trid3nt.workflows.pestpp.assimilation import AssimilateWorkflow
from trid3nt.workflows.pestpp.modules import PESTPP
from trid3nt.workflows.runtime.bare import register_bare


class INPUTS:
    files = Input(type=dict, desc=(
        "Pinned native MODFLOW 6 simulation files by deck filename including mfsim.nam, with the PEST++ "
        "template and instruction files the parameters and observations read and write."))
    model = Input(type=str, desc="The GWF model name in the MODFLOW 6 simulation.")
    cycles = Input(type=list[dict], desc=(
        "Ordered, non-overlapping cycle windows. Each states id (zero-based integer), period (one-based MODFLOW "
        "6 stress period) and optionally length in days, which must equal that TDIS period."))
    parameters = Input(type=list[dict], desc=(
        "Parameters by name with template, model_file, value, lower and upper bounds, group, transform "
        "(log, none or fixed), cycle (-1 or a cycle id) and source. A fixed cycle=-1 parameter with state true "
        "is a dynamic state and needs a linked observation."))
    observations = Input(type=list[dict], desc=(
        "Observations by name with instruction, model_file, group, value, weight, cycle (-1 or a cycle id) and "
        "source. Optional values and weights map cycle ids to per-cycle values; state_par_link names the state "
        "parameter a state observation evolves."))
    options = Input(type=dict, stated_only=True, desc=(
        "PEST++ DA controls by their own names, such as da_num_reals, noptmax, da_use_mda and ies_no_noise. "
        "The three cycle tables are the study's own."))
    command = Input(type=str, stated_only=True, desc=(
        "The trial command the study runs, mf6 unstated. State a Python wrapper where a trial applies its "
        "templates through a script before mf6."))


STEERING = PESTPP
assimilate = register_bare(AssimilateWorkflow, "assimilate", sys.modules[__name__])
