"""Shared layer-uri helpers for agent tools (no ``cases/`` platform import).

``agent/tools/*`` must not import the ``cases/`` platform package (layering:
``cases/`` sits above ``agent/tools/``, not below it). ``_strip_query`` and
``_unwrap_tile_template`` are needed by several tools that resolve a case
layer's ``uri`` to its underlying file, so they live here as the agent-side
source; ``cases/hydrate_case_layers.py`` keeps its own copy rather than
importing this module (same layering reason, opposite direction).
"""

from __future__ import annotations

from urllib.parse import parse_qs, unquote, urlparse

__all__ = ["_strip_query", "_unwrap_tile_template"]


def _strip_query(uri: str) -> str:
    return uri.split("?", 1)[0].rstrip("/")


def _unwrap_tile_template(uri: str) -> str:
    """If ``uri`` is a TiTiler tile TEMPLATE (``/cog/tiles/`` display URL),
    return the underlying COG from its percent-encoded ``url=`` query param;
    otherwise return ``uri`` unchanged."""
    if "/cog/tiles/" not in uri:
        return uri
    cog = (parse_qs(urlparse(uri).query).get("url") or [None])[0]
    if cog:
        return unquote(cog)
    return uri
