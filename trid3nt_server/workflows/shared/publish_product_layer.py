"""The ONE styling seam a solved raster product goes through before it is returned.

An engine's postprocess hands back a RAW layer whose ``uri`` is an object-store
COG. Putting that on the canvas means one call to ``publish_layer`` for the style
and one ``model_copy`` to fold the run's narration (scalars, honesty note,
provenance rows) onto the typed layer. Eight engine families were each carrying
their own copy of those four lines.

Nothing here is peak-specific: the field may be a peak envelope, a final-frame
sea state, a steady agitation coefficient or a bottom sigma plane. What the
caller supplies is the style preset and the update mapping; what it gets back is
the same typed layer, published.

FAILURE NEVER RETRACTS: on a publish failure the RAW layer is returned enriched
but unpublished. Its object-store COG still lets the case find the SELAFIN
sibling, and dropping a solved result over a styling miss would be the
failure-retracts-something anti-pattern.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger("trid3nt_server.workflows.shared.publish_product_layer")

__all__ = ["publish_product_layer"]


async def publish_product_layer(raw: Any, *, style_preset: str,
                                update: dict[str, Any]) -> Any:
    """Style ``raw``'s COG through ``publish_layer`` and fold ``update`` onto it.

    A layer whose ``uri`` is not an object-store URI has nothing to style, so it
    is only enriched. The layer's OWN ``style_preset`` wins over the caller's
    default when the postprocess already chose one.
    """
    from trid3nt_server.tools.publish_layer.publish_layer import (
        PublishLayerError,
        publish_layer,
    )

    if not str(getattr(raw, "uri", "")).startswith(("s3://", "gs://")):
        return raw.model_copy(update=update)
    try:
        published_uri = await asyncio.to_thread(
            publish_layer, layer_uri=raw.uri, layer_id=raw.layer_id,
            style_preset=raw.style_preset or style_preset)
    except PublishLayerError as exc:
        logger.warning("publish_layer failed (%s) - the unpublished COG is returned",
                       exc)
        return raw.model_copy(update=update)
    return raw.model_copy(update={"uri": published_uri, **update})
