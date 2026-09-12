"""``list_run_frames`` - the ordered animation-frame COG URIs for one run layer.
The URIs are READ from the run's ``outputs.json``, never invented, and ordered by
the physical time ``t`` each entry carries. A run with no manifest or no matching
frames returns an HONEST empty result with a typed ``reason``, never a fabricated
list."""

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
    """Raised when the frame listing cannot be produced. ``error_code`` is
    ``MISSING_RUN_ID`` (none supplied) or ``MANIFEST_UNAVAILABLE`` (no manifest
    readable) - both are narrated as limitations, never filled in with frames."""

    error_code: str
    retryable: bool = False

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


class _RunIdShim:
    """Minimal ``run_result``-shaped object carrying just ``run_id``. Both manifest
    readers resolve a manifest from ``getattr(run_result, "run_id", None)``, so this
    is enough to reuse them without duplicating the object-path logic."""

    __slots__ = ("run_id",)

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id


def _norm(s: str) -> str:
    """Lowercase + collapse separators so "flood_depth" == "Flood depth"."""
    return "".join(ch for ch in s.lower() if ch.isalnum())


def _matches_layer(entry: Any, layer: str) -> bool:
    """True when a manifest entry belongs to the requested ``layer``, matched
    case- and separator-insensitively on the grouping ``name`` or the entry's
    identity token. A BLANK ``layer`` matches everything."""
    if not layer:
        return True
    want = _norm(layer)
    if not want:
        return True
    name = _norm(getattr(entry, "name", "") or "")
    stem = _norm(
        getattr(entry, "quantity", "") or getattr(entry, "layer_id_stem", "") or ""
    )
    return want in name or want in stem or name.startswith(want) or stem.startswith(want)


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
def list_run_frames(run_id: str, layer: str = "flood_depth") -> dict[str, Any]:
    """List the ordered animation-frame COG URIs for a completed run's layer.

    ROUTING: a PER-FRAME read over a time-stepped solve - a temporal glow over a
    flash sequence, a first/peak/last panel, a per-step max - where the ordered
    frame URIs feed a run_pyqgis snippet or a Processing algorithm per frame.
    NOT for a single non-animated layer (pass its URI straight on), NOT to fetch
    data, and NOT to render a standard scrubber - the frames the seam publishes
    are already grouped.

    `layer` is matched on the grouping name or the physical quantity; `""` lists ALL
    frame layers. Returns {run_id, layer, frame_count, frame_uris, frames:
    [{frame_no, cog_uri, name, t}]} in frame order, where `frame_no` is the 1-based
    ordinal and `t` is seconds from run start. An HONEST empty result - frame_count
    0 plus a `reason` - when the run has no manifest or no matching frames.
    """
    if not run_id or not str(run_id).strip():
        raise ListRunFramesError("MISSING_RUN_ID", "list_run_frames requires a run_id")

    # The reader NEVER raises: a None return is the honest "no manifest" path.
    from trid3nt_server.emission.outputs_seam import read_outputs_manifest

    shim = _RunIdShim(str(run_id))
    frames: list[dict[str, Any]] = []
    source = ""
    layer_total = 0

    outputs = read_outputs_manifest(shim)
    if outputs is not None:
        source = "outputs.json"
        layer_total = len(outputs.entries)
        # A frame is a raster entry carrying a physical time. Non-temporal
        # entries (the peak) have no ``t`` and are not part of a sequence.
        matched = [
            e
            for e in outputs.entries
            if e.kind == "raster" and e.t is not None and _matches_layer(e, layer)
        ]
        matched.sort(key=lambda e: float(e.t))
        frames = [
            {
                "frame_no": i,
                "cog_uri": e.uri,
                "name": e.name,
                "t": float(e.t),
            }
            for i, e in enumerate(matched, start=1)
            if e.uri
        ]

    frame_uris = [f["cog_uri"] for f in frames]
    logger.info(
        "list_run_frames: run_id=%s layer=%r frames=%d source=%s",
        run_id, layer, len(frame_uris), source or "none",
    )

    result: dict[str, Any] = {
        "run_id": str(run_id),
        "layer": layer,
        "frame_count": len(frame_uris),
        "frame_uris": frame_uris,
        "frames": frames,
    }
    if not frame_uris:
        result["reason"] = (
            "no outputs.json found for this run (the run "
            "may still be in flight, or have failed); no frames to list"
            if not source
            else (
                f"the run's {source} has no frames matching {layer!r} "
                f"(it has {layer_total} entr(y/ies); none was a temporal frame for "
                "this layer name). Pass layer='' to list all frames."
            )
        )
    return result
