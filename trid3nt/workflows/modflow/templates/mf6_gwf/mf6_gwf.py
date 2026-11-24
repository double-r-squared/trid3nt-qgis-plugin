"""The bare MODFLOW groundwater flow declaration."""

import sys

from trid3nt.workflows.modflow.bare import NATIVE_SIMULATION, BareModflowWorkflow
from trid3nt.workflows.modflow.modules import GWF
from trid3nt.workflows.runtime.bare import NATIVE_EPSG, register_bare


class INPUTS:
    simulation = NATIVE_SIMULATION
    epsg = NATIVE_EPSG


STEERING = GWF
mf6_gwf = register_bare(BareModflowWorkflow, "mf6_gwf", sys.modules[__name__])
