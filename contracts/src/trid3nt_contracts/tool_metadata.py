"""Tool docstring metadata conventions and ``tool_category`` vocabulary.

CONVENTION ONLY. This module documents the required docstring sections (FR-AS-3,
FR-TA-3) and the ``tool_category`` vocabulary used in the ``tool-call-start``
WebSocket message (Appendix A.4). ``agent`` owns the tool registry / ``FunctionTool``
code; ``schema`` owns these conventions and the message field they populate.

The constants here are importable so ``agent`` and ``testing`` can assert that a
registered tool's category is a known member and that its docstring carries the
required sections — without re-stating the vocabulary in two places.
"""

from __future__ import annotations

__all__ = [
    "REQUIRED_DOCSTRING_SECTIONS",
    "TOOL_CATEGORIES",
    "is_known_tool_category",
]


#: Required docstring sections for every registered tool (FR-AS-3).
#: The agent's registry should reject (or flag) a tool whose docstring is
#: missing any of these. ``testing`` asserts presence as a negative control.
REQUIRED_DOCSTRING_SECTIONS: tuple[str, ...] = (
    "summary",  # one-sentence summary (the first docstring line)
    "Use this when:",  # bullet list of trigger conditions
    "Do NOT use this for:",  # bullet list of incorrect uses
    "params",  # parameter descriptions
    "returns",  # return-type description
)


#: ``tool_category`` vocabulary for ``tool-call-start.tool_category`` (A.4).
#: Open enum (Decision G): a new engine may add a category without a breaking
#: change. Members mirror the FR-TA-2 tool groupings. The pipeline strip uses
#: the category to group/icon steps client-side.
TOOL_CATEGORIES: tuple[str, ...] = (
    "workflow",  # FR-TA-1 deterministic workflows
    "discovery",  # public hazard layer discovery (catalog search / fetch / summarize)
    "data-fetch",  # DEM, landcover, rivers, precip, streamflow, tracks, buildings
    "event-sourcing",  # news + agency feeds + generic web fetch
    "event-aggregation",  # aggregate_claims_across_sources
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

    Open-enum semantics: unknown categories are *allowed* on the wire (a new
    engine may add one), but the agent/testing layers can use this to flag a
    category that is not yet documented here so the vocabulary stays current.
    """
    return category in TOOL_CATEGORIES
