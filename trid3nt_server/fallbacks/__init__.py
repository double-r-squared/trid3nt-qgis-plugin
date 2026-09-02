"""Declared-degradation machinery: ladders as data + the one walker.

Capability-neutral by design -- fetchers, mesh builders and worker legs all
declare their ladders here and share one execution + recording path.
"""

from .ladder import (
    BELOW_PRIMARY_CLASSES,
    DEGRADATION_CLASSES,
    REFUSE,
    Consequence,
    Ladder,
    Rung,
    get_ladder,
    register_ladder,
    register_ladder_selector,
    registered_ladders,
    resolve_ladder,
)
from .persist import persist_run_activations
from .walker import (
    LADDER_ERROR_CODE,
    Activation,
    LadderGap,
    LadderRefused,
    RungRecord,
    walk_ladder,
)

__all__ = [
    "Consequence",
    "BELOW_PRIMARY_CLASSES",
    "DEGRADATION_CLASSES",
    "Rung",
    "REFUSE",
    "Ladder",
    "register_ladder",
    "register_ladder_selector",
    "resolve_ladder",
    "get_ladder",
    "registered_ladders",
    "LADDER_ERROR_CODE",
    "LadderGap",
    "LadderRefused",
    "RungRecord",
    "Activation",
    "walk_ladder",
    "persist_run_activations",
]
