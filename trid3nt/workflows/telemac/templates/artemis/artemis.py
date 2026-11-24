"""The bare artemis declaration."""

import sys

from trid3nt.workflows.runtime.bare import NATIVE_EPSG, NATIVE_FILES, register_bare
from trid3nt.workflows.telemac.bare import NATIVE_STEERING, BareTelemacWorkflow
from trid3nt.workflows.telemac.modules import ART


class INPUTS:
    steering = NATIVE_STEERING
    files = NATIVE_FILES
    epsg = NATIVE_EPSG

STEERING = ART

artemis = register_bare(BareTelemacWorkflow, "artemis", sys.modules[__name__])
