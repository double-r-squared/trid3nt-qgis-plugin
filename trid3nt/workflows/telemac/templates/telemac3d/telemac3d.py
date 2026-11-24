"""The bare telemac3d declaration."""

import sys

from trid3nt.workflows.runtime.bare import NATIVE_EPSG, NATIVE_FILES, register_bare
from trid3nt.workflows.telemac.bare import COUPLING, NATIVE_STEERING, BareTelemacWorkflow
from trid3nt.workflows.telemac.modules import T3D


class INPUTS:
    steering = NATIVE_STEERING
    files = NATIVE_FILES
    epsg = NATIVE_EPSG
    coupling = COUPLING

STEERING = T3D

telemac3d = register_bare(BareTelemacWorkflow, "telemac3d", sys.modules[__name__])
