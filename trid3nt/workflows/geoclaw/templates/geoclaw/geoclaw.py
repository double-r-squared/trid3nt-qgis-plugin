"""The bare GeoClaw declaration."""

import sys

from trid3nt.workflows.geoclaw.bare import VERTICAL_FRAME, BareGeoclawWorkflow
from trid3nt.workflows.geoclaw.modules import GEO
from trid3nt.workflows.runtime.bare import NATIVE_FILES, register_bare


class INPUTS:
    files = NATIVE_FILES
    vertical_frame = VERTICAL_FRAME


STEERING = GEO
geoclaw = register_bare(BareGeoclawWorkflow, "geoclaw", sys.modules[__name__])
