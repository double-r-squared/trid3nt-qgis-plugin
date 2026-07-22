"""Atomic tool ``list_run_frames`` — ordered animation-frame COG URIs for a run.

This is the LLM-facing companion to the Python sandbox's multi-frame
``layer_refs`` extension (sandbox-staging). A time-stepped solve (SFINCS flood
depth per step, GLM lightning per minute, wave fields per step) writes a
``publish_manifest.json`` whose ``layers[]`` carry one ``cog_uri`` per frame plus
a ``frame_no``. To run a per-frame visualization in the sandbox (a gaussian glow
over a flash sequence, a first/peak/last panel, a temporal max), the agent needs
the ORDERED list of those frame COG URIs so it can hand them to
``code_exec_request(layer_refs={"frames": [<uri>, ...]})``.

``list_run_frames`` reads the run's manifest (the SAME schema-gated reader the
register-only fast path uses), filters the layers to the requested ``layer``
(matched on the web grouping ``name`` token OR the ``layer_id_stem``), drops the
non-frame aggregate layers (those carry ``frame_no = None`` — e.g. the PEAK depth
layer), orders the remainder by ``frame_no``, and returns the ordered
``cog_uri`` list.

Determinism (Invariant 1): the URIs are READ from the worker-written manifest,
never invented. Honesty floor (data-source-fallback norm): a run with no manifest
/ no matching frames returns an HONEST empty result with a typed ``reason`` — never
a fabricated frame list.

Caching: ``ttl_class="live-no-cache"`` — a run's manifest is read once per ask and
the result is small; the manifest itself is the source of truth, so caching the
listing is pointless (and a re-run could grow frames).
"""

from __future__ import annotations

import logging
from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from . import register_tool

__all__ = [
    "list_run_frames",
    "ListRunFramesError",
]

logger = logging.getLogger("trid3nt_server.tools.list_run_frames")


class ListRunFramesError(RuntimeError):
    """Raised when the frame listing cannot be produced (FR-AS-11 typed error).

    Codes:
    - ``MISSING_RUN_ID`` — no ``run_id`` was supplied.
    - ``MANIFEST_UNAVAILABLE`` — the run's ``publish_manifest.json`` could not be
      read or schema-gated (the agent narrates the limitation; it does NOT
      fabricate frames).
    """

    error_code: str
    retryable: bool = False

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


class _RunIdShim:
    """Minimal ``run_result``-shaped object carrying just ``run_id``.

    ``register_published_manifest.read_publish_manifest`` resolves a run's
    manifest from ``getattr(run_result, "run_id", None)``; this shim lets us reuse
    that exact reader (completion.json -> publish_manifest_uri -> schema-gated
    parse) without duplicating the S3 path logic."""

    __slots__ = ("run_id",)

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id


def _norm(s: str) -> str:
    """Lowercase + collapse separators so "flood_depth" == "Flood depth"."""
    return "".join(ch for ch in s.lower() if ch.isalnum())


def _matches_layer(entry: Any, layer: str) -> bool:
    """True when a manifest layer entry belongs to the requested ``layer``.

    Matched (case-insensitive, separator-insensitive — so ``"flood_depth"``
    matches ``"Flood depth step 3"``) on the web grouping ``name`` token (the value
    the user/agent most naturally names) OR the ``layer_id_stem``. A blank
    ``layer`` matches everything (return ALL frame layers). The frame-number
    suffix in the name is tolerated by substring matching on the normalized
    forms."""
    if not layer:
        return True
    want = _norm(layer)
    if not want:
        return True
    name = _norm(getattr(entry, "name", "") or "")
    stem = _norm(getattr(entry, "layer_id_stem", "") or "")
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

    Use this when: you want to run a PER-FRAME visualization over a time-stepped
    solve in the Python sandbox (``code_exec_request``) — a temporal glow over a
    GLM lightning sequence, a first/peak/last flood panel, a per-step max — and
    you need the ordered list of frame COG URIs to pass as a multi-frame
    ``layer_refs`` entry (``{"frames": [<uri>, ...]}``). The URIs come from the
    run's ``publish_manifest.json`` (one ``cog_uri`` per ``frame_no``).

    Do NOT use this for: a single (non-animated) layer — pass that layer's URI to
    ``code_exec_request`` directly. Do NOT use it to fetch new data or to render a
    standard scrubber (the web already groups sequential layers from the manifest).

    Args:
        run_id: The completed run's id (the solve whose frames you want).
        layer: The frame layer to list, matched on the web grouping name token
            (e.g. ``"flood_depth"``, ``"lightning"``) or the layer_id_stem.
            Defaults to ``"flood_depth"``. Pass ``""`` to list ALL frame layers.

    Returns:
        ``{run_id, layer, frame_count, frame_uris: [<s3://...>, ...], frames:
        [{frame_no, cog_uri, name}, ...]}`` ordered by ``frame_no``. An HONEST
        empty result (``frame_count=0`` + a ``reason``) when the run has no
        manifest or no matching frame layers — never a fabricated list. The
        ``frame_uris`` list is exactly what ``code_exec_request`` accepts as a
        list-valued ``layer_refs`` entry.
    """
    if not run_id or not str(run_id).strip():
        raise ListRunFramesError("MISSING_RUN_ID", "list_run_frames requires a run_id")

    # Reuse the schema-gated manifest reader (completion.json -> manifest_uri ->
    # typed parse). It NEVER raises — it returns None on any failure — so a
    # None here is the honest "no manifest" path, not a crash.
    from ..workflows.register_published_manifest import read_publish_manifest

    manifest = read_publish_manifest(_RunIdShim(str(run_id)))
    if manifest is None:
        logger.info("list_run_frames: no manifest run_id=%s", run_id)
        return {
            "run_id": str(run_id),
            "layer": layer,
            "frame_count": 0,
            "frame_uris": [],
            "frames": [],
            "reason": (
                "no publish_manifest.json found for this run (the run may predate "
                "the manifest, still be in flight, or have failed); no frames to list"
            ),
        }

    # Collect the FRAME layers (frame_no is not None) that match the requested
    # layer, ordered by frame_no. Aggregate/peak layers carry frame_no=None and
    # are excluded — they are not part of an animation sequence.
    matched = [
        entry
        for entry in manifest.layers
        if getattr(entry, "frame_no", None) is not None and _matches_layer(entry, layer)
    ]
    matched.sort(key=lambda e: int(getattr(e, "frame_no")))

    frames = [
        {
            "frame_no": int(getattr(e, "frame_no")),
            "cog_uri": getattr(e, "cog_uri", ""),
            "name": getattr(e, "name", ""),
        }
        for e in matched
        if getattr(e, "cog_uri", "")
    ]
    frame_uris = [f["cog_uri"] for f in frames]

    logger.info(
        "list_run_frames: run_id=%s layer=%r frames=%d", run_id, layer, len(frame_uris)
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
            f"the run manifest has no frame layers matching {layer!r} "
            f"(it has {len(manifest.layers)} layer(s); none carried a frame_no for "
            "this layer name). Pass layer='' to list all frame layers."
        )
    return result
