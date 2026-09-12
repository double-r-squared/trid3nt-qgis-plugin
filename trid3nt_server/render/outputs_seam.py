"""A finished run's OUTPUTS: what it put on the map, read back off its record.

There is one registry, not two: the run journal's ``outputs`` field is written
by the publish stage as the layers are emitted, and a reader that names a run id
reads it from there. The artifacts a row points at are delete-on-whim and the
session that saw the layers ends; the record outlives both."""

from __future__ import annotations

import logging
from typing import Any

__all__ = ["quantity_label", "run_outputs"]

logger = logging.getLogger("trid3nt_server.render.outputs_seam")


def run_outputs(run_id: str) -> list[dict[str, Any]]:
    """Every layer the named run published, oldest first; ``[]`` when its record
    carries none. Never raises: a run nobody journalled is answered as empty."""
    from trid3nt_server.workflows.runtime.journal import run_outputs as recorded

    if not run_id:
        return []
    try:
        published = recorded(str(run_id))
    except Exception as exc:  # noqa: BLE001 - an unreadable record answers empty
        logger.warning("outputs_seam: run %s has no readable record (%s: %s)",
                       run_id, type(exc).__name__, exc)
        return []
    logger.info("outputs_seam: run %s published %d layer(s)", run_id,
                len(published))
    return published


def quantity_label(quantity: str) -> str:
    """``flood_depth`` -> ``Flood depth``: the quantity, said out loud.

    No lookup table: the producer's own field name is the label's only source.
    """
    words = (quantity or "").strip().replace("-", " ").replace("_", " ").strip()
    return words[:1].upper() + words[1:] if words else "Value"
