"""Register-only fast path for the worker-written ``publish_manifest.json``.

The worker writes display-ready COGs and a thin typed manifest; the agent only
REGISTERS - the bare ``cog_uri`` becomes the layer uri and no COG is downloaded.
"""

from __future__ import annotations

import logging
from typing import Any

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.publish_manifest import (
    PublishManifest,
    PublishManifestLayer,
    parse_publish_manifest,
)

from trid3nt_server.emission.publish import (
    _stash_legend_for_uri,
    legend_for_published_layer,
)
from trid3nt_server.emission.uri_registry import observe_published_layer

__all__ = [
    "read_publish_manifest",
    "register_manifest_layers",
    "ManifestRegisterResult",
    "RegisteredLayer",
]

logger = logging.getLogger("trid3nt_server.workflows.shared.register_published_manifest")


def read_publish_manifest(run_result: Any) -> PublishManifest | None:
    """Read + schema-gate the worker's ``publish_manifest.json`` for a run.
    Resolves the explicit ``completion.json.publish_manifest_uri`` rather than globbing,
    and NEVER raises: every failure degrades to ``None`` for the caller to branch on."""
    run_id = getattr(run_result, "run_id", None)
    if not run_id:
        return None
    try:
        from trid3nt_server.workflows.solver.solver import (
            _get_runs_bucket,
            _read_object_bytes,
            _try_get_completion_s3,
        )

        runs_bucket = _get_runs_bucket()
        completion = _try_get_completion_s3(runs_bucket, str(run_id))
    except Exception as exc:  # noqa: BLE001 - degrade to fallback
        logger.warning(
            "register_published_manifest: completion.json read failed run_id=%s "
            "(%s: %s) - falling back to on-box postprocess",
            run_id,
            type(exc).__name__,
            exc,
        )
        return None

    if not isinstance(completion, dict):
        return None
    manifest_uri = completion.get("publish_manifest_uri")
    if not manifest_uri:
        # Pre-rebuild worker image (no manifest pointer) - clean fallback.
        return None

    try:
        raw = _read_object_bytes(str(manifest_uri))
    except Exception as exc:  # noqa: BLE001 - degrade to fallback
        logger.warning(
            "register_published_manifest: manifest read failed uri=%s "
            "(%s: %s) - falling back to on-box postprocess",
            manifest_uri,
            type(exc).__name__,
            exc,
        )
        return None

    try:
        manifest = parse_publish_manifest(raw)
    except ValueError as exc:
        # Absent/unknown schema_version OR malformed body - clean fallback.
        logger.warning(
            "register_published_manifest: manifest schema-gate rejected uri=%s "
            "(%s) - falling back to on-box postprocess",
            manifest_uri,
            exc,
        )
        return None
    logger.info(
        "register_published_manifest: parsed manifest run_id=%s engine=%s "
        "status=%s layers=%d",
        run_id,
        manifest.engine,
        manifest.status,
        len(manifest.layers),
    )
    return manifest


class RegisteredLayer:
    """One manifest layer resolved to a renderable ``LayerURI`` (or dropped)."""

    __slots__ = ("layer", "dropped", "cog_uri", "stem")

    def __init__(
        self,
        *,
        layer: LayerURI | None,
        dropped: bool,
        cog_uri: str,
        stem: str,
    ) -> None:
        self.layer = layer
        self.dropped = dropped
        self.cog_uri = cog_uri
        self.stem = stem


class ManifestRegisterResult:
    """The register-only outcome consumed by the workflow tails.
    ``layers`` is in manifest order with the PEAK primary first; ``dropped_count`` is
    always 0 and ``tile_publish_available`` always True - there is no tile server."""

    __slots__ = ("layers", "metrics", "dropped_count", "tile_publish_available")

    def __init__(
        self,
        *,
        layers: list[LayerURI],
        metrics: dict[str, Any],
        dropped_count: int,
        tile_publish_available: bool,
    ) -> None:
        self.layers = layers
        self.metrics = metrics
        self.dropped_count = dropped_count
        self.tile_publish_available = tile_publish_available


def _register_one_layer(
    entry: PublishManifestLayer,
    *,
    run_id: str,
    bbox: tuple[float, float, float, float] | None,
) -> RegisteredLayer:
    """Resolve ONE manifest layer to a registered ``LayerURI``.
    The declared style row resolves against the manifest's own band stats, with NO
    COG read, and the returned uri is the raw ``cog_uri``."""
    stem = entry.layer_id_stem
    cog_uri = entry.cog_uri
    layer_id = f"{stem}-{run_id}"

    # The pipeline emitter's add_loaded_layer lifts the stash back out by
    # ``layer.uri``. ``raster_bytes=b""`` pins the register-only contract: NO
    # COG download here. Fail-open: a legend failure never blocks registration.
    bs = entry.band_stats
    try:
        _stash_legend_for_uri(cog_uri, legend_for_published_layer(
            entry.style, cog_uri, units=entry.units or None, raster_bytes=b"",
            band_stats=(bs.p2, bs.p98)))
    except Exception as exc:  # noqa: BLE001 - legend never blocks a register
        logger.debug("register_published_manifest legend stash skipped (%s: %s)",
                     type(exc).__name__, exc)

    # Register the published COG: with the TiTiler exit there is no
    # separate display face - the raw s3:// COG IS both the consumable DATA uri
    # and the envelope uri the plugin renders (mirrors publish_layer). A NO-OP
    # outside an active dispatch ContextVar - which is exactly why registration
    # stays agent-side (it cannot move to the worker; a missing registration
    # breaks the flood->Pelicun URI-handle resolution).
    observe_published_layer(layer_id, uri=cog_uri)

    # Per-layer bbox: prefer the manifest entry's, else the workflow's AOI bbox.
    entry_bbox: tuple[float, float, float, float] | None = None
    if entry.bbox and len(entry.bbox) == 4:
        entry_bbox = (
            float(entry.bbox[0]),
            float(entry.bbox[1]),
            float(entry.bbox[2]),
            float(entry.bbox[3]),
        )
    layer = LayerURI(
        layer_id=layer_id,
        name=entry.name,  # EXACT web grouping token - never rename.
        layer_type=entry.layer_type or "raster",
        uri=cog_uri,
        style=entry.style,
        role=entry.role or "primary",  # type: ignore[arg-type]
        units=entry.units or None,
        bbox=entry_bbox or bbox,
    )
    logger.info(
        "register_published_manifest: registered layer_id=%s name=%r cog_uri=%s",
        layer_id,
        entry.name,
        cog_uri,
    )
    return RegisteredLayer(layer=layer, dropped=False, cog_uri=cog_uri, stem=stem)


def register_manifest_layers(
    manifest: PublishManifest,
    *,
    run_id: str,
    bbox: tuple[float, float, float, float] | None = None,
) -> ManifestRegisterResult:
    """Register every manifest layer - raw ``s3://`` COG uri, observe, legend stash.
    Safe to run ON the loop: the worker already produced the COGs and band stats,
    so nothing here does heavy I/O and no layer is ever dropped."""
    layers: list[LayerURI] = []
    dropped = 0
    for entry in manifest.layers:
        reg = _register_one_layer(entry, run_id=run_id, bbox=bbox)
        if reg.dropped:
            dropped += 1
            continue
        if reg.layer is not None:
            layers.append(reg.layer)
    return ManifestRegisterResult(
        layers=layers,
        metrics=dict(manifest.metrics or {}),
        dropped_count=dropped,
        tile_publish_available=True,
    )
