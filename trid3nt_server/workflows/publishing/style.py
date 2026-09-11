"""The styling seam a solved raster product goes through before it is returned.

Nothing here is field-specific: the caller supplies the style row and the update
mapping, and gets the same typed layer back, published."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger("trid3nt_server.workflows.publishing.style")

__all__ = ["publish_product_layer"]


async def publish_product_layer(raw: Any, *, style: dict,
                                update: dict[str, Any]) -> Any:
    """Style ``raw``'s COG through ``publish_layer`` and fold ``update`` onto it.
    A layer whose ``uri`` is not an object-store URI is only enriched, and the
    layer's OWN ``style`` row wins over the caller's default."""
    from trid3nt_server.emission.publish import (
        PublishLayerError,
        publish_layer,
    )

    if not str(getattr(raw, "uri", "")).startswith(("s3://", "gs://")):
        return raw.model_copy(update=update)
    try:
        published_uri = await asyncio.to_thread(
            publish_layer, layer_uri=raw.uri, layer_id=raw.layer_id,
            style=raw.style or style)
    except PublishLayerError as exc:
        # FAILURE NEVER RETRACTS: the raw layer comes back enriched but unpublished.
        # Its object-store COG still lets the case find the solver's own output, and
        # dropping a solved result over a styling miss would retract the run.
        logger.warning("publish_layer failed (%s) - the unpublished COG is returned",
                       exc)
        return raw.model_copy(update=update)
    return raw.model_copy(update={"uri": published_uri, **update})
