"""Tool docstring sections and the ``tool_category`` vocabulary.

Constants only, so the vocabulary is asserted against rather than restated.
``tool_category`` is an OPEN enum: a value absent from it is still legal on
the wire.
"""

from __future__ import annotations

__all__ = [
    "REQUIRED_DOCSTRING_SECTIONS",
    "TOOL_CATEGORIES",
    "is_known_tool_category",
]


#: Required docstring sections for every registered tool. A registered tool
#: whose docstring is missing any of these is malformed.
REQUIRED_DOCSTRING_SECTIONS: tuple[str, ...] = (
    "summary",  # one-sentence summary (the first docstring line)
    "Use this when:",  # bullet list of trigger conditions
    "Do NOT use this for:",  # bullet list of incorrect uses
    "params",  # parameter descriptions
    "returns",  # return-type description
)


#: ``tool_category`` vocabulary for the ``tool-call-start`` field of the same
#: name. Open enum - a new engine may add a category without a breaking change,
#: so a receiver must tolerate a value that is not a member here.
TOOL_CATEGORIES: tuple[str, ...] = (
    "workflow",  # deterministic workflows
    "discovery",  # public hazard layer discovery (catalog search / fetch / summarize)
    "data-fetch",  # DEM, landcover, rivers, precip, streamflow, tracks, buildings
    "event-sourcing",  # news + agency feeds + generic web fetch
    "geocoding",  # place name -> bbox
    "mongodb",  # MCP-served document/vector/insert operations
    "qgis",  # PyQGIS worker operations + algorithm discovery
    "model-setup",  # build_sfincs_model
    "model-execution",  # run_solver / wait_for_completion / postprocess
    "client-control",  # zoom_to / set_layer_opacity / start_animation
    "user-input",  # request_spatial_input / _disambiguation / _clarification
)


def is_known_tool_category(category: str) -> bool:
    """Return True if ``category`` is a documented ``tool_category`` member.
    False is not invalid - the enum is open and an undocumented category is
    still legal on the wire."""
    return category in TOOL_CATEGORIES
