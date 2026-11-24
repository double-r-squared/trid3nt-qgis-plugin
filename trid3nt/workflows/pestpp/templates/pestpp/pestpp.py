"""The bare PEST++ declaration."""

import sys

from trid3nt.inputs.input import Input
from trid3nt.workflows.pestpp.bare import BarePestppWorkflow
from trid3nt.workflows.pestpp.modules import PESTPP
from trid3nt.workflows.runtime.bare import register_bare


class INPUTS:
    tool = Input(type=str, desc="The PEST++ executable suffix.")
    trial = Input(type=str, desc="Installed trial adapter, modflow or telemac.")
    trial_steering = Input(type=str, desc="Staged TELEMAC-2D steering filename for the trial.").stated()
    files = Input(type=dict, desc="Native trial files by their deck names and staged URIs.")
    parameters = Input(type=list[dict], desc="Parameter table with PARNME, PARVAL1, PARLBND, PARUBND, PARGP, PARTRANS, PARCHGLIM, SCALE and OFFSET.")
    observations = Input(type=list[dict], desc="Observation table with OBSNME, OBSVAL, WEIGHT and OBGNME.")
    templates = Input(type=dict, desc="Template-to-model input file pairs.")
    instructions = Input(type=dict, desc="Instruction-to-model output file pairs.")


STEERING = PESTPP
pestpp = register_bare(BarePestppWorkflow, "pestpp", sys.modules[__name__])
