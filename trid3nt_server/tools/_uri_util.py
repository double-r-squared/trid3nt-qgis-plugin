"""Shared layer-uri helper for agent tools (no ``cases/`` platform import).

``agent/tools/*`` must not import the ``cases/`` platform package (layering:
``cases/`` sits above ``agent/tools/``, not below it), so the agent-side source
of ``_strip_query`` lives here.
"""

from __future__ import annotations

__all__ = ["_strip_query"]


def _strip_query(uri: str) -> str:
    return uri.split("?", 1)[0].rstrip("/")
