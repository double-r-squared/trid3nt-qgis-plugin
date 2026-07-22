"""Register-only fast path for the worker-written ``publish_manifest.json``.

SFINCS postprocess offload (Phase 4 - agent thin-out). The Batch worker now runs
the heavy NetCDF/.mat -> COG conversion ON THE WORKER (it has the raw output
local, the geo stack in-image, and a big box), writes display-ready
overview-bearing COGs to deterministic keys, and writes a thin typed
``publish_manifest.json`` alongside ``completion.json`` (pointed to by
``completion.json.publish_manifest_uri``).

The agent then collapses to REGISTER-ONLY:
  1. Read ``completion.json.publish_manifest_uri`` (this module, ``read_publish_manifest``).
  2. For each manifest layer: emit the bare ``cog_uri`` AS the layer uri
     (TiTiler exit / QGIS-native swap - the plugin reads the raw ``s3://`` COG
     via /vsicurl/, mirroring ``publish_layer``'s raw-cog return), resolve the
     style params from the agent-owned registry + ``band_stats`` (NO COG
     download) purely to STASH the data-driven legend keyed by that ``cog_uri``
     (``_stash_legend_for_uri``; the pipeline emitter lifts it by ``layer.uri``),
     mint ``layer_id = f"{layer_id_stem}-{run_id}"``, call
     ``observe_published_layer``, build a ``LayerURI``.
  3. SHORT-CIRCUIT the on-box heavy path: NO ``_resolve_run_output_to_local``,
     NO ``postprocess_flood``/``postprocess_waves``, NO
     ``_ensure_raster_has_overviews`` (``has_overviews`` is true).

TiTiler exit: the former ``TRID3NT_TILE_SERVER_BASE`` publish-or-honest-drop gate
is GONE - no tile server is needed to display a raster anymore, so no manifest
layer is dropped for lack of one (``dropped_count`` stays for the result shape
but is always 0 on this path).

FALLBACK (one-release safety): when the manifest is ABSENT or carries an UNKNOWN
schema_version, ``read_publish_manifest`` returns ``None`` and the caller runs
the EXISTING on-box postprocess path unchanged (the raw ``sfincs_map.nc`` is
still uploaded). This is a clean if/else so nothing breaks before the worker
images rebuild.
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

from ..tools.publish_layer import (
    _stash_legend_for_uri,
    legend_for_published_layer,
    style_params_from_band_stats,
)
from ..uri_registry import observe_published_layer

__all__ = [
    "read_publish_manifest",
    "register_manifest_layers",
    "register_swan_wave_layers",
    "ManifestRegisterResult",
    "RegisteredLayer",
]

logger = logging.getLogger("trid3nt_server.workflows.register_published_manifest")


def read_publish_manifest(run_result: Any) -> PublishManifest | None:
    """Read + schema-gate the worker's ``publish_manifest.json`` for a run.

    Resolves ``completion.json.publish_manifest_uri`` (the explicit pointer the
    worker writes so the agent never globs) and parses it through the typed,
    schema-gated reader.

    Returns ``None`` (the FALLBACK trigger - the caller runs the legacy on-box
    path) when:
      - the completion manifest cannot be read,
      - it carries no ``publish_manifest_uri`` (pre-rebuild worker image),
      - the manifest object cannot be read, OR
      - the manifest body is malformed / carries an UNKNOWN schema_version.

    NEVER raises - any failure degrades to ``None`` (one-release safety).
    """
    run_id = getattr(run_result, "run_id", None)
    if not run_id:
        return None
    try:
        from ..tools.solver import (
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

    ``layers`` parallels ``postprocess_flood``'s return: ``layers[0]`` is the
    PEAK primary, ``layers[1:]`` are the frame/context layers. ``metrics`` is
    the manifest's top-level peak aggregates (the ``FloodMetrics`` source).
    TiTiler exit: rasters emit as their raw ``s3://`` COG uri (plugin
    /vsicurl/), so the no-tile-server drop path is gone -- ``dropped_count`` is
    always 0 and ``tile_publish_available`` is always True (both kept for the
    callers' result shape).
    """

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

    Mirrors the on-box ``publish_layer`` s3 branch byte-for-byte in shape
    (TiTiler exit): resolve style params (from band_stats, NO COG read), stash
    the data-driven LEGEND keyed by the raw ``cog_uri``, register the COG via
    ``observe_published_layer``, return a ``LayerURI`` carrying the raw
    ``cog_uri`` as ``uri`` (the plugin reads it via /vsicurl/).
    """
    stem = entry.layer_id_stem
    cog_uri = entry.cog_uri
    layer_id = f"{stem}-{run_id}"

    bs = entry.band_stats
    style_params = style_params_from_band_stats(
        entry.style_preset,
        is_categorical=bs.is_categorical,
        is_rgba=bs.is_rgba,
        p2=bs.p2,
        p98=bs.p98,
        layer_uri=cog_uri,
    )

    # DATA-DRIVEN LEGEND (mirror of publish_layer's raw-cog exit): derive the
    # render KEY from the SAME resolved style_params and stash it keyed by the
    # ENVELOPE uri - the raw s3:// COG this layer emits. The pipeline emitter's
    # add_loaded_layer lifts it back out by ``layer.uri``. ``raster_bytes=b""``
    # pins the register-only fast-path contract: NO COG download here - an
    # empty style_params (categorical/RGBA register-only layer) therefore
    # stashes None and falls back to the plugin's style_preset/default
    # rendering, exactly as the legacy template seam did. Fail-open: a legend
    # failure never blocks registration.
    try:
        legend = None
        if style_params:
            legend = legend_for_published_layer(
                entry.style_preset,
                cog_uri,
                style_params,
                units=entry.units or None,
                raster_bytes=b"",
            )
        _stash_legend_for_uri(cog_uri, legend)
    except Exception as exc:  # noqa: BLE001 - legend never blocks a register
        logger.debug(
            "register_published_manifest legend stash skipped (%s: %s)",
            type(exc).__name__,
            exc,
        )

    # Register the published COG (job-0304): with the TiTiler exit there is no
    # separate display face - the raw s3:// COG IS both the consumable DATA uri
    # and the envelope uri the plugin renders (mirrors publish_layer). A NO-OP
    # outside an active dispatch ContextVar - which is exactly why registration
    # stays agent-side (it cannot move to the worker; a missing registration
    # breaks the flood->Pelicun URI-handle resolution).
    observe_published_layer(layer_id, gcs_uri=cog_uri)

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
        style_preset=entry.style_preset,
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


def register_swan_wave_layers(
    manifest: PublishManifest,
    *,
    run_id: str,
    mode: str,
    bbox: tuple[float, float, float, float] | None = None,
) -> tuple[list[Any], dict[str, Any], int]:
    """Register manifest wave layers as ``WaveFieldLayerURI`` rows (SWAN path).

    The SWAN standalone composer returns a typed ``WaveFieldLayerURI`` carrying
    the four narration scalars (``max_hs_m`` / ``mean_tp_s`` / ``mean_dir_deg`` /
    ``wave_area_km2``) that the manifest stores in each layer's ``metrics``. This
    builds those typed rows over the register-only raw ``s3://`` COG uris
    (TiTiler exit; no COG conversion).

    Returns ``(wave_layers, top_metrics, dropped_count)`` where ``wave_layers[0]``
    is the PEAK (role ``"primary"``). ``WaveFieldLayerURI`` is imported lazily so
    the generic register module stays SWAN-agnostic for the depth path.
    """
    from trid3nt_contracts.swan_contracts import WaveFieldLayerURI

    wave_layers: list[Any] = []
    dropped = 0
    for entry in manifest.layers:
        reg = _register_one_layer(entry, run_id=run_id, bbox=bbox)
        if reg.dropped or reg.layer is None:
            dropped += 1
            continue
        base = reg.layer
        m = entry.metrics or {}
        wave_layers.append(
            WaveFieldLayerURI(
                layer_id=base.layer_id,
                name=base.name,
                layer_type=base.layer_type,
                uri=base.uri,
                style_preset=base.style_preset,
                role=base.role,
                units=base.units,
                bbox=base.bbox,
                max_hs_m=float(m.get("max_hs_m", 0.0) or 0.0),
                mean_tp_s=float(m.get("mean_tp_s", 0.0) or 0.0),
                mean_dir_deg=float(m.get("mean_dir_deg", 0.0) or 0.0),
                wave_area_km2=float(m.get("wave_area_km2", 0.0) or 0.0),
                mode=mode,  # type: ignore[arg-type]
            )
        )
    return wave_layers, dict(manifest.metrics or {}), dropped


def register_manifest_layers(
    manifest: PublishManifest,
    *,
    run_id: str,
    bbox: tuple[float, float, float, float] | None = None,
) -> ManifestRegisterResult:
    """Register every manifest layer (raw ``s3://`` COG uri + observe + legend
    stash), no COG conversion.

    Pure given the active dispatch registry; runs on the loop (it does NO heavy
    I/O - the worker already produced the COGs + band_stats). TiTiler exit: no
    tile server is required to display, so no layer is dropped for lack of one.
    """
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
