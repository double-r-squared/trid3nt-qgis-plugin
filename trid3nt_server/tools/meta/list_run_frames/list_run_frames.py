"""``list_run_frames`` - the layers a completed run put on the map, by uri.
The uris are READ off the run's own record, never invented: the publish stage
writes every layer it emitted onto the run journal, which outlives the artifacts
and the session. A run with no record, or none matching, returns an HONEST empty
result with a typed ``reason``, never a fabricated list."""

from __future__ import annotations

import logging
from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool

__all__ = [
    "list_run_frames",
    "ListRunFramesError",
]

logger = logging.getLogger("trid3nt_server.tools.meta.list_run_frames.list_run_frames")


class ListRunFramesError(RuntimeError):
    """Raised when the listing cannot be produced. ``error_code`` is
    ``MISSING_RUN_ID`` (none supplied) - narrated as a limitation, never filled
    in with outputs."""

    error_code: str
    retryable: bool = False

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def _norm(s: str) -> str:
    """Lowercase + collapse separators so "flood_depth" == "Flood depth"."""
    return "".join(ch for ch in s.lower() if ch.isalnum())


def _matches_layer(row: dict[str, Any], layer: str) -> bool:
    """True when a recorded output belongs to the requested ``layer``, matched
    case- and separator-insensitively on the layer's name or its quantity. A
    BLANK ``layer`` matches everything."""
    want = _norm(layer or "")
    if not want:
        return True
    name = _norm(str(row.get("name") or ""))
    quantity = _norm(str(row.get("quantity") or ""))
    return (want in name or want in quantity
            or name.startswith(want) or quantity.startswith(want))


@register_tool(
    AtomicToolMetadata(
        name="list_run_frames",
        ttl_class="live-no-cache",
        cacheable=False,
    ),
    read_only_hint=True,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
)
def list_run_frames(run_id: str, layer: str = "") -> dict[str, Any]:
    """List the object-store uris of the layers a completed run put on the map.

    ROUTING: when a snippet or a Processing algorithm has to open a run's own
    products by path - the results mesh a field was solved on, a track, a
    station - and the caller has the run id rather than the layer handles. NOT
    for a layer already on the canvas (name it), NOT to fetch data, and NOT to
    play a time series - the mesh layer's dataset groups already carry every
    written instant under the temporal controller.

    `layer` is matched on the layer's name or its physical quantity; `""` (the
    default) lists every output. Returns {run_id, layer, output_count, outputs:
    [{uri, name, quantity, layer_type, units}]} in the order the run published
    them. An HONEST empty result - output_count 0 plus a `reason` - when the run
    has no record or none of its outputs matches.
    """
    if not run_id or not str(run_id).strip():
        raise ListRunFramesError("MISSING_RUN_ID", "list_run_frames requires a run_id")

    from trid3nt_server.render.outputs_seam import run_outputs

    published = run_outputs(str(run_id))
    matched = [row for row in published if _matches_layer(row, layer) and row.get("uri")]
    outputs = [{"uri": str(row.get("uri")), "name": str(row.get("name") or ""),
                "quantity": row.get("quantity"),
                "layer_type": row.get("layer_type"),
                "units": row.get("units")}
               for row in matched]
    logger.info("list_run_frames: run_id=%s layer=%r outputs=%d of %d recorded",
                run_id, layer, len(outputs), len(published))

    result: dict[str, Any] = {
        "run_id": str(run_id),
        "layer": layer,
        "output_count": len(outputs),
        "outputs": outputs,
    }
    if not outputs:
        result["reason"] = (
            "the run journal carries no record for this run (it may still be in "
            "flight, or have failed); no outputs to list"
            if not published else
            f"the run published {len(published)} layer(s), none matching "
            f"{layer!r}. Pass layer='' to list them all.")
    return result
