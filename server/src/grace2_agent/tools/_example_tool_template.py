"""``example_bbox_area`` -- COPY-ME template for a new atomic tool.

This is a COMPLETE, WORKING, minimal registered tool you copy to start your own.
It is deliberately trivial (a dependency-free planar area estimate for a bbox) so
the mechanics are not buried under real data-fetch code. To turn it into a real
tool: copy this file, rename it, replace the body, and follow the TODO markers.

Read alongside ``docs/authoring/writing-a-tool.md`` -- that guide walks every
seam this file touches (metadata fields, the docstring rule, the corpus, the
retrieval check, the test).

INERT BY DEFAULT: the ``@register_tool`` call at the bottom is gated behind the
``GRACE2_ENABLE_EXAMPLE_TOOL`` env flag so this example NEVER pollutes the
production tool catalog. A REAL tool does NOT gate -- it decorates the function
unconditionally (see the TODO at the gate). Enable it for a demo / the visibility
check with ``GRACE2_ENABLE_EXAMPLE_TOOL=1``.

ASCII only. No emojis, no typographic dashes.
"""

from __future__ import annotations

import math
import os
from typing import Any

from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool

__all__ = ["example_bbox_area"]

# ---------------------------------------------------------------------------
# 1. The metadata (grace2_contracts.tool_registry.AtomicToolMetadata).
#
# Every atomic tool declares one at module load. The cross-field validator runs
# at construction, so a misconfigured tool fails fast at IMPORT time (before it
# is ever on the wire). See docs/authoring/writing-a-tool.md section B for every
# field. This example is a pure deterministic compute, not a fetcher:
#   - cacheable=False  -> it does not write the object-store cache, so
#   - ttl_class="live-no-cache" (the validator REQUIRES this pairing when
#     cacheable=False), and
#   - source_class=None (no cache-bucket prefix is needed when nothing is cached).
# A network FETCHER instead sets cacheable=True + ttl_class="static-30d" (or
# semi-static-7d / dynamic-1h) + a source_class prefix like "dem".
# ---------------------------------------------------------------------------
_METADATA = AtomicToolMetadata(
    name="example_bbox_area",  # TODO: rename to your tool's function name (== registry key)
    ttl_class="live-no-cache",  # TODO: a fetcher uses "static-30d" / "semi-static-7d" / "dynamic-1h"
    source_class=None,  # TODO: a cacheable tool needs a non-empty prefix, e.g. "dem"
    cacheable=False,  # TODO: True for a network fetcher whose bytes you want cached
    supports_global_query=False,  # this tool requires a bbox; bbox=None is a hard error below
    # MCP annotation hints -- safe defaults for a read-only, in-process compute:
    read_only_hint=True,  # False for writers (publish_layer, run_solver, ...)
    open_world_hint=False,  # True for anything hitting an EXTERNAL endpoint (all fetch_*)
    destructive_hint=False,  # True only for irreversible mutation (publish_layer)
    idempotent_hint=True,  # False for dispatchers / emitters / interactive tools
    # auto_publish only matters when a tool returns a raster LayerURI carrying a
    # raw s3:///gs:// uri; a dict-returning compute leaves it at its default.
)


# ---------------------------------------------------------------------------
# 2. The tool function.
#
# Signature conventions:
#   - plain typed params the LLM fills from the docstring + type hints;
#   - a trailing ``**_extra_ignored: Any`` to tolerate LLM over-supply
#     (underscore-prefixed params are STRIPPED from the LLM-facing schema);
#   - sync ``def`` is fine for a cheap in-process compute. Use ``async def`` for
#     engine/composer tools or anything doing loop-blocking I/O (the
#     no-sync-blocking-on-the-asyncio-loop rule); heavy sync fetchers are wrapped
#     server-side via _ALWAYS_OFFLOAD_SYNC_TOOLS instead.
#
# The docstring is LOAD-BEARING: the adapter builds the LLM tool declaration from
# the SIGNATURE + this docstring, and Bedrock TRUNCATES the description to 1000
# chars. FRONT-LOAD the routing block (What / When to use / When NOT) so it
# survives the cut. See docs/authoring/writing-a-tool.md section E.
#
# TODO: in a REAL tool, drop the ``if _ENABLED`` gate below and decorate the
# function directly:  @register_tool(_METADATA, open_world_hint=True)
# ---------------------------------------------------------------------------
def example_bbox_area(
    bbox: tuple[float, float, float, float],
    label: str = "area of interest",
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Estimate the ground area of a bounding box in square kilometres.

    **What it does:** Computes an approximate planar ground area for a lon/lat
    bounding box using an equirectangular (cosine-latitude) approximation. This
    is a COPY-ME EXAMPLE tool that ships with the repo to demonstrate the atomic
    tool authoring pattern; it does no network I/O and mutates nothing.

    **When to use:**
    - "roughly how many square kilometres does this box cover?"
    - a quick area sanity-check on a drawn or derived area of interest.

    **When NOT to use:**
    - For an authoritative area of an admin polygon -> fetch the boundary and use
      a projected-CRS area calculation, not this planar estimate.
    - For anything you would report to a user as exact -- this is an example.

    **Parameters:**
    - ``bbox`` (tuple): ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326.
      Required. Example (Lee County, FL): ``(-82.2, 26.2, -81.5, 26.9)``.
    - ``label`` (str, optional): a human-readable name echoed back in the result.

    **Returns:** a plain dict ``{"area_km2", "width_km", "height_km", "bbox",
    "label", "method"}``. A tool that produces a MAP LAYER instead returns a
    ``grace2_contracts.execution.LayerURI`` (see ``fetch_noaa_slr_confidence`` for
    the canonical raster example).

    **Cross-tool dependencies:** none -- this is a self-contained primitive.
    """
    # --- Error convention: raise a typed, honest failure. ---
    # A real tool imports ToolInputError from grace2_contracts.tool_registry (or
    # .errors) and raises it with a stable code; the server renders it as the
    # {status:error, error_code, retryable, message} envelope and the LLM retries
    # via function_response. Here we keep the dependency surface minimal and raise
    # a plain ValueError to illustrate the fail-fast shape.
    if bbox is None or len(bbox) != 4:
        raise ValueError(
            "example_bbox_area requires bbox=(min_lon, min_lat, max_lon, max_lat)"
        )
    min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox)
    if not (max_lon > min_lon and max_lat > min_lat):
        raise ValueError(
            "example_bbox_area: bbox must be non-degenerate with max > min on both axes"
        )

    km_per_deg_lat = 111.32
    mid_lat_rad = math.radians((min_lat + max_lat) / 2.0)
    km_per_deg_lon = 111.32 * math.cos(mid_lat_rad)

    width_km = (max_lon - min_lon) * km_per_deg_lon
    height_km = (max_lat - min_lat) * km_per_deg_lat
    area_km2 = abs(width_km * height_km)

    return {
        "label": label,
        "bbox": [min_lon, min_lat, max_lon, max_lat],
        "width_km": round(width_km, 3),
        "height_km": round(height_km, 3),
        "area_km2": round(area_km2, 3),
        "method": "equirectangular cosine-latitude approximation (example tool)",
    }


# ---------------------------------------------------------------------------
# 3. Registration.
#
# In a REAL tool this is a single line directly above the function:
#     @register_tool(_METADATA, open_world_hint=True)
#     def my_tool(...): ...
# Decorator kwargs (open_world_hint, read_only_hint, supports_global_query,
# payload_mb_estimator_name, ...) override the metadata and RE-VALIDATE fail-fast.
#
# This example instead registers CONDITIONALLY so it stays out of the production
# catalog by default. ``register_tool(...)`` returns a decorator; we apply it to
# the function only when the flag is set. Duplicate names raise
# ToolRegistrationError at import time, so a copied file must use a fresh name.
#
# TODO: in your real tool DELETE this block and use the plain @register_tool
# decorator shown above.
# ---------------------------------------------------------------------------
_ENABLED = os.environ.get("GRACE2_ENABLE_EXAMPLE_TOOL", "").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)

if _ENABLED:  # pragma: no cover - exercised only under the demo/visibility flag
    example_bbox_area = register_tool(_METADATA)(example_bbox_area)
