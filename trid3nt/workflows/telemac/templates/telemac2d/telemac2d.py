"""The bare telemac2d declaration."""

import sys

from trid3nt.workflows.runtime.bare import NATIVE_EPSG, NATIVE_FILES, register_bare
from trid3nt.workflows.telemac.bare import COUPLING, NATIVE_STEERING, BareTelemacWorkflow
from trid3nt.workflows.telemac.modules import T2D


class INPUTS:
    steering = NATIVE_STEERING
    files = NATIVE_FILES
    epsg = NATIVE_EPSG
    coupling = COUPLING

STEERING = T2D

telemac2d = register_bare(BareTelemacWorkflow, "telemac2d", sys.modules[__name__])
