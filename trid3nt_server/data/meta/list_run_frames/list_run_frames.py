"""Atomic tool ``list_run_frames`` -- ordered animation-frame COG URIs for a run.

This is the LLM-facing companion to the Python sandbox's multi-frame
``layer_refs`` extension (sandbox-staging). A time-stepped solve (SFINCS flood
depth per step, GeoClaw tsunami depth, wave fields per step) writes an
``outputs.json`` under its run prefix -- the emit-on-solve manifest
(``trid3nt_contracts.outputs_manifest``) whose entries carry one ``uri`` per
frame plus the physical time ``t`` (seconds from run start; absent on a
non-temporal artifact like the PEAK layer). To run a per-frame visualization in
the sandbox (a gaussian glow over a flash sequence, a first/peak/last panel, a
temporal max), the agent needs the ORDERED list of those frame COG URIs so it
can hand them to ``code_exec_request(layer_refs={"frames": [<uri>, ...]})``.

``list_run_frames`` reads ``outputs.json`` FIRST (the seam's own contract, the
single frame source of truth), keeps the entries carrying a ``t`` that match the
requested ``layer`` (matched on the web grouping ``name`` token OR the physical
``quantity``), orders them by ``t``, and returns the ordered ``uri`` list.

LEGACY FALLBACK: a run that PREDATES ``outputs.json`` has only the worker's
``publish_manifest.json``, whose ``layers[]`` carried a per-frame ``frame_no``.
When no ``outputs.json`` is readable, the frame layers are read from there and
ordered by ``frame_no``. Runs written by a current worker carry NO frame entries
in ``publish_manifest.json`` at all -- it is the metrics carrier, not a second
frame stream.

Determinism (Invariant 1): the URIs are READ from the run's manifest, never
invented. Honesty floor (data-source-fallback norm): a run with no manifest / no
matching frames returns an HONEST empty result with a typed ``reason`` -- never a
fabricated frame list.

Caching: ``ttl_class="live-no-cache"`` -- a run's manifest is read once per ask and
the result is small; the manifest itself is the source of truth, so caching the
listing is pointless (and a re-run could grow frames).
"""

from __future__ import annotations

import logging
from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.data import register_tool

__all__ = [
    "list_run_frames",
    "ListRunFramesError",
]

logger = logging.getLogger("trid3nt_server.data.meta.list_run_frames.list_run_frames")


class ListRunFramesError(RuntimeError):
    """Raised when the frame listing cannot be produced (typed error).

    Codes:
    - ``MISSING_RUN_ID`` -- no ``run_id`` was supplied.
    - ``MANIFEST_UNAVAILABLE`` -- neither the run's ``outputs.json`` nor its legacy
      ``publish_manifest.json`` could be read or schema-gated (the agent narrates
      the limitation; it does NOT fabricate frames).
    """

    error_code: str
    retryable: bool = False

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


class _RunIdShim:
    """Minimal ``run_result``-shaped object carrying just ``run_id``.

    Both manifest readers (``outputs_seam.read_outputs_manifest`` and the legacy
    ``register_published_manifest.read_publish_manifest``) resolve a run's
    manifest from ``getattr(run_result, "run_id", None)``; this shim lets us reuse
    those exact readers without duplicating the S3 path logic."""

    __slots__ = ("run_id",)

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id


def _norm(s: str) -> str:
    """Lowercase + collapse separators so "flood_depth" == "Flood depth"."""
    return "".join(ch for ch in s.lower() if ch.isalnum())


def _matches_layer(entry: Any, layer: str) -> bool:
    """True when a manifest entry belongs to the requested ``layer``.

    Matched (case-insensitive, separator-insensitive -- so ``"flood_depth"``
    matches ``"Flood depth step 3"``) on the web grouping ``name`` token (the value
    the user/agent most naturally names) OR the entry's identity token: the
    physical ``quantity`` on an ``outputs.json`` entry, the ``layer_id_stem`` on a
    legacy ``publish_manifest`` layer. A blank ``layer`` matches everything
    (return ALL frames). The frame-number suffix in the name is tolerated by
    substring matching on the normalized forms."""
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

    Use this when: you want to run a PER-FRAME visualization over a time-stepped
    solve in the Python sandbox (``code_exec_request``) -- a temporal glow over a
    GLM lightning sequence, a first/peak/last flood panel, a per-step max -- and
    you need the ordered list of frame COG URIs to pass as a multi-frame
    ``layer_refs`` entry (``{"frames": [<uri>, ...]}``). The URIs come from the
    run's ``outputs.json`` (one entry per frame, ordered by the physical time
    ``t``); a run predating that manifest falls back to its legacy
    ``publish_manifest.json`` frame layers.

    Do NOT use this for: a single (non-animated) layer -- pass that layer's URI to
    ``code_exec_request`` directly. Do NOT use it to fetch new data or to render a
    standard scrubber (the plugin already groups the frames the seam publishes).

    Args:
        run_id: The completed run's id (the solve whose frames you want).
        layer: The frame layer to list, matched on the web grouping name token
            (e.g. ``"flood_depth"``, ``"wave_height"``) or the physical quantity.
            Defaults to ``"flood_depth"``. Pass ``""`` to list ALL frame layers.

    Returns:
        ``{run_id, layer, frame_count, frame_uris: [<s3://...>, ...], frames:
        [{frame_no, cog_uri, name, t}, ...]}`` in frame order (``frame_no`` is the
        1-based ordinal; ``t`` is seconds from run start, ``None`` on the legacy
        path). An HONEST empty result (``frame_count=0`` + a ``reason``) when the
        run has no manifest or no matching frames -- never a fabricated list. The
        ``frame_uris`` list is exactly what ``code_exec_request`` accepts as a
        list-valued ``layer_refs`` entry.
    """
    if not run_id or not str(run_id).strip():
        raise ListRunFramesError("MISSING_RUN_ID", "list_run_frames requires a run_id")

    # Both readers NEVER raise -- they return None on any failure -- so a None is
    # the honest "no manifest" path, not a crash.
    from trid3nt_server.emission.outputs_seam import read_outputs_manifest
    from trid3nt_server.workflows.shared.register_published_manifest import (
        read_publish_manifest,
    )

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

    if not frames:
        # LEGACY runs (pre-outputs.json): the worker's publish_manifest carried a
        # per-frame ``frame_no``. Current workers write NO frame entries there.
        manifest = read_publish_manifest(shim)
        if manifest is not None:
            legacy = [
                e
                for e in manifest.layers
                if getattr(e, "frame_no", None) is not None
                and _matches_layer(e, layer)
            ]
            legacy.sort(key=lambda e: int(getattr(e, "frame_no")))
            if legacy:
                source = "publish_manifest.json (legacy)"
                layer_total = len(manifest.layers)
                frames = [
                    {
                        "frame_no": int(getattr(e, "frame_no")),
                        "cog_uri": e.cog_uri,
                        "name": e.name,
                        "t": None,
                    }
                    for e in legacy
                    if e.cog_uri
                ]
            elif not source:
                source = "publish_manifest.json (legacy)"
                layer_total = len(manifest.layers)

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
            "no outputs.json or publish_manifest.json found for this run (the run "
            "may still be in flight, or have failed); no frames to list"
            if not source
            else (
                f"the run's {source} has no frames matching {layer!r} "
                f"(it has {layer_total} entr(y/ies); none was a temporal frame for "
                "this layer name). Pass layer='' to list all frames."
            )
        )
    return result
