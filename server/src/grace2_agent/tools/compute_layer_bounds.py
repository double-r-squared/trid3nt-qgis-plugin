"""Atomic tool ``compute_layer_bounds`` — fast layer-extent + fit-the-map primitive (NATE 2026-06-17).

ONE atomic tool that answers "what is this layer's geographic extent?" and
"fit/zoom/resize the map so all of <these features> are in view" WITHOUT the
Python sandbox.

    ``compute_layer_bounds(layer_uri, pad_fraction=0.0) → dict``

**Why this tool exists (the bug it fixes):**

Live finding (NATE 2026-06-17): when the user asked to "resize the bounding box
to encompass all the <features>", the agent reached for the PYTHON SANDBOX
(``code_exec_request``) to compute ``gdf.total_bounds`` — which is slow, gated
behind a user-confirm, frequently orphaned, and (worst of all) the computed
extent was never applied, so the AOI stayed a tiny box "around a random house".
The agent also wrongly claimed "I cannot pan/zoom your map" even though a
``zoom-to`` map-command (Map.tsx ``fitBounds``) has existed since job-0068.

This tool replaces both failure modes:

1. It computes the layer's EPSG:4326 bounding box deterministically with
   geopandas (vector) or rasterio (raster) — reusing the
   ``postprocess_pelicun._bbox_from_gdf`` reproject-to-4326 pattern. Sub-second,
   no LLM, no sandbox, no user-confirm gate.
2. It EMITS a ``map-command(zoom-to, bbox=<computed bbox>)`` so the VIEW
   actually fits all features. The emission goes through the same
   ``current_emitter()`` ContextVar + ``emit_map_command`` seam that
   ``model_flood_scenario`` (job-0160) uses for zoom-on-area-first, so it is
   server→client consistent with the existing zoom-to envelope.

**Auto-detection (vector vs raster):**

- Extensions ``.tif`` / ``.tiff`` / ``.vrt`` / ``.img`` / ``.nc`` → raster
  (opens via ``rasterio``).
- Extensions ``.fgb`` / ``.geojson`` / ``.json`` / ``.gpkg`` / ``.shp`` /
  ``.parquet`` → vector (opens via ``geopandas`` / ``pyogrio``).
- Unknown extension → rasterio is tried first; on failure, geopandas is tried.

**Cross-cutting invariants:**

- **Invariant 1 (Determinism boundary): preserves.** Pure geopandas/rasterio
  bbox extraction; no LLM, no estimate. The emitted bbox is workflow-attributed.
- **FR-DC-6 (cacheable): honors.** ``cacheable=False`` /
  ``ttl_class="live-no-cache"`` — the tool has a side effect (it drives the map
  view) and is sub-second, so caching is both wrong and pointless.
- **FR-AS-11 (typed errors): honors.** Every failure raises
  ``ComputeLayerBoundsError`` with a SCREAMING_SNAKE_CASE ``error_code``.
"""

from __future__ import annotations

import logging
import math
import os
import tempfile
from datetime import datetime, timezone
from typing import Any

from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool

__all__ = [
    "compute_layer_bounds",
    "ComputeLayerBoundsError",
]

logger = logging.getLogger("grace2_agent.tools.compute_layer_bounds")


# ---------------------------------------------------------------------------
# Error class (FR-AS-11 typed errors)
# ---------------------------------------------------------------------------


class ComputeLayerBoundsError(RuntimeError):
    """Raised when layer-bounds computation fails.

    ``error_code`` carries a SCREAMING_SNAKE_CASE code surfaced in the pipeline
    strip / function_response (FR-AS-11 typed-error requirement).

    Codes:
    - ``UNKNOWN_LAYER_URI`` — uri is neither a gs:///s3:// URI nor a readable
      local file.
    - ``DOWNLOAD_FAILED`` — object-store download for a gs:///s3:// URI failed.
    - ``RASTER_OPEN_FAILED`` — the raster could not be opened by rasterio.
    - ``VECTOR_OPEN_FAILED`` — the vector could not be opened by geopandas.
    - ``GEOPANDAS_UNAVAILABLE`` — geopandas / pyogrio not importable.
    - ``EMPTY_LAYER`` — the layer has no features / no valid extent.
    - ``DEGENERATE_BOUNDS`` — the computed bounds are non-finite (NaN/inf).
    """

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


# ---------------------------------------------------------------------------
# Metadata — NOT cacheable: this tool has a side effect (drives the map view)
# and is sub-second. live-no-cache is the FR-DC-6-consistent declaration.
# ---------------------------------------------------------------------------

_COMPUTE_LAYER_BOUNDS_METADATA = AtomicToolMetadata(
    name="compute_layer_bounds",
    ttl_class="live-no-cache",
    source_class=None,
    cacheable=False,
)

_RASTER_EXTENSIONS = {".tif", ".tiff", ".img", ".vrt", ".nc"}
_VECTOR_EXTENSIONS = {".fgb", ".geojson", ".json", ".gpkg", ".shp", ".gml", ".kml", ".parquet"}


# ---------------------------------------------------------------------------
# URI → local path materialization (gs:// / s3:// / local), mirrors the
# clip_vector_to_polygon pattern (boto3 for s3, GCS client for gs).
# ---------------------------------------------------------------------------


def _infer_suffix(uri: str) -> str:
    """Pick a temp-file suffix matching the URI extension so geopandas/rasterio
    auto-detect the driver from the path. Falls back to ``.bin``."""
    base = uri.split("?")[0].rstrip("/")
    lower = base.lower()
    for ext in (*_RASTER_EXTENSIONS, *_VECTOR_EXTENSIONS):
        if lower.endswith(ext):
            return ext
    return ".bin"


def _resolve_layer_to_local_path(
    uri: str, storage_client: object | None = None
) -> tuple[str, bool]:
    """Resolve ``uri`` to a local file path.

    Returns ``(path, is_temp)`` — caller deletes the path iff ``is_temp``.
    Supports ``s3://`` (boto3, EC2 instance-role — job-0289 lesson) and local
    paths. GCP is decommissioned, so ``storage_client`` is ignored. Raises
    ``ComputeLayerBoundsError`` on failure.
    """
    del storage_client  # GCP decommissioned — S3/local only.
    suffix = _infer_suffix(uri)

    if uri.startswith("s3://"):
        from .cache import read_object_bytes_s3

        try:
            data = read_object_bytes_s3(uri)
        except Exception as exc:  # noqa: BLE001
            raise ComputeLayerBoundsError(
                "DOWNLOAD_FAILED", f"S3 download failed for {uri!r}: {exc}"
            ) from exc
        with tempfile.NamedTemporaryFile(
            suffix=suffix, delete=False, prefix="grace2_bounds_"
        ) as tmp:
            tmp.write(data)
            return tmp.name, True

    if os.path.isfile(uri):
        return uri, False

    # Tolerant fallback: a TiTiler tile-template / display URL embeds the real
    # COG in its ``url=`` query param (mirrors pipeline_emitter._layer_identity_key
    # and uri_registry._is_tile_template). Recover it and read the COG directly so
    # an LLM that passed the display URL still gets a deterministic extent rather
    # than a hard UNKNOWN_LAYER_URI. Defense-in-depth behind the uri_registry
    # resolver fix (which normally substitutes the COG before dispatch).
    if uri.startswith(("http://", "https://")):
        from urllib.parse import parse_qs, unquote, urlparse

        cog = (parse_qs(urlparse(uri).query).get("url") or [None])[0]
        if cog:
            cog = unquote(cog)
            if cog.startswith("s3://"):
                # storage_client was already del'd above (GCP decommissioned —
                # ignored); recurse on the s3:// COG branch with None.
                return _resolve_layer_to_local_path(cog, None)

    raise ComputeLayerBoundsError(
        "UNKNOWN_LAYER_URI",
        f"layer URI {uri!r} is not an s3:// URI, a TiTiler tile template with an "
        f"s3:// url= param, or a readable local file.",
    )


# ---------------------------------------------------------------------------
# Layer-type detection + per-type bbox extraction (reproject → EPSG:4326)
# ---------------------------------------------------------------------------


def _detect_layer_type(uri: str) -> str | None:
    """Return ``"raster"``/``"vector"`` from the extension, or ``None`` if
    unknown (caller probes rasterio then geopandas)."""
    ext = os.path.splitext(uri.split("?")[0].rstrip("/"))[-1].lower()
    if ext in _RASTER_EXTENSIONS:
        return "raster"
    if ext in _VECTOR_EXTENSIONS:
        return "vector"
    return None


def _bounds_from_raster(path: str) -> tuple[float, float, float, float]:
    """Open a raster and return its (min_lon, min_lat, max_lon, max_lat),
    reprojecting the dataset bounds to EPSG:4326 when the CRS differs."""
    try:
        import rasterio  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover — rasterio is a hard dep
        raise ComputeLayerBoundsError(
            "RASTER_OPEN_FAILED", f"rasterio not available: {exc}"
        ) from exc
    try:
        with rasterio.open(path) as ds:
            b = ds.bounds
            crs = ds.crs
            if crs is not None and str(crs).upper() not in (
                "EPSG:4326",
                "WGS 84",
                "WGS84",
            ):
                from rasterio.warp import transform_bounds

                left, bottom, right, top = transform_bounds(
                    crs, "EPSG:4326", b.left, b.bottom, b.right, b.top
                )
            else:
                left, bottom, right, top = b.left, b.bottom, b.right, b.top
    except ComputeLayerBoundsError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ComputeLayerBoundsError(
            "RASTER_OPEN_FAILED", f"Could not open raster {path!r}: {exc}"
        ) from exc
    return (float(left), float(bottom), float(right), float(top))


def _bounds_from_vector(path: str) -> tuple[float, float, float, float]:
    """Open a vector and return its (min_lon, min_lat, max_lon, max_lat) via the
    ``postprocess_pelicun._bbox_from_gdf`` reproject-to-4326 pattern."""
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ComputeLayerBoundsError(
            "GEOPANDAS_UNAVAILABLE", f"geopandas / pyogrio not available: {exc}"
        ) from exc
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
    except Exception:  # noqa: BLE001 — retry without the explicit engine
        try:
            gdf = gpd.read_file(path)
        except Exception as exc:  # noqa: BLE001
            raise ComputeLayerBoundsError(
                "VECTOR_OPEN_FAILED", f"Could not open vector {path!r}: {exc}"
            ) from exc

    try:
        gdf = gdf[gdf.geometry.notna()]
    except Exception:  # noqa: BLE001
        pass
    if gdf is None or len(gdf) == 0:
        raise ComputeLayerBoundsError(
            "EMPTY_LAYER", f"vector layer {path!r} has no features with geometry."
        )

    # Reuse the postprocess_pelicun reproject-to-4326 convention.
    from .postprocess_pelicun import _bbox_from_gdf

    minx, miny, maxx, maxy = _bbox_from_gdf(gdf)
    return (float(minx), float(miny), float(maxx), float(maxy))


def _apply_pad(
    bbox: tuple[float, float, float, float], pad_fraction: float
) -> tuple[float, float, float, float]:
    """Pad a 4326 bbox by ``pad_fraction`` of its width/height on each side.

    A point/degenerate-line layer (zero width or height) gets a small absolute
    pad (~0.001 deg, ~100 m) so the resulting box is not a zero-area sliver the
    map cannot fit. Longitudes/latitudes are clamped to valid ranges.
    """
    minx, miny, maxx, maxy = bbox
    w = maxx - minx
    h = maxy - miny
    pad_x = w * pad_fraction if w > 0 else 0.001
    pad_y = h * pad_fraction if h > 0 else 0.001
    # Always give a degenerate (point) layer a minimum pad even at pad=0.
    if w == 0:
        pad_x = max(pad_x, 0.001)
    if h == 0:
        pad_y = max(pad_y, 0.001)
    minx -= pad_x
    maxx += pad_x
    miny -= pad_y
    maxy += pad_y
    minx = max(-180.0, min(180.0, minx))
    maxx = max(-180.0, min(180.0, maxx))
    miny = max(-90.0, min(90.0, miny))
    maxy = max(-90.0, min(90.0, maxy))
    return (minx, miny, maxx, maxy)


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


@register_tool(
    _COMPUTE_LAYER_BOUNDS_METADATA,
    # readOnlyHint=True (reads the input layer; no mutation beyond the
    # transient map-command verb), openWorldHint=False (pure local GDAL),
    # destructiveHint=False, idempotentHint=True (same layer → same bbox).
    read_only_hint=True,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
)
async def compute_layer_bounds(
    layer_uri: str,
    pad_fraction: float = 0.0,
    *,
    fit_map: bool = True,
    _storage_client: object | None = None,
    # job-0164: absorb any LLM-invented kwargs (also centralized at server.py
    # via tool_arg_normalizer, but kept here belt-and-suspenders).
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Get a layer's geographic extent AND fit/zoom/resize the map to it.

    USE THIS TO GET A LAYER'S GEOGRAPHIC EXTENT, or to FIT / ZOOM / RESIZE THE
    MAP to a layer so all of its features are in view. NEVER use the code
    sandbox (code_exec_request) for bounding-box / extent math — this tool is
    the dedicated, fast, deterministic path and it ALSO drives the map view.

    When the user asks to "resize the bounding box to encompass all the
    <features>", "fit the map to the layer", "zoom to all the points", "show me
    the whole extent", or anything that means fit-the-view-to-a-layer, call
    THIS tool with the layer's handle/uri. You CAN drive the map view — do NOT
    claim you cannot pan or zoom the map. The tool emits a ``zoom-to``
    map-command (the same one the web's fitBounds handler consumes) so the
    actual viewport fits all features.

    Parameters:
        layer_uri: the loaded vector or raster layer's handle / URI. PREFER the
            layer's ``layer_id`` handle from [Case state] / loaded_layers, NOT
            its display tile URL (the ``https://.../cog/tiles/...`` template) --
            the handle resolves deterministically to the data COG. Also accepts a
            gs:///s3:// object URI or a local path (the display tile URL is
            tolerated as a fallback, but the handle is the reliable form). Vector
            (.fgb/.geojson/.gpkg/.shp/.parquet) opens via geopandas; raster
            (.tif/.tiff/.vrt) opens via rasterio. The extent is reprojected to
            EPSG:4326.
        pad_fraction: optional fractional padding added on every side
            (0.0 = exact extent; 0.05 = 5% breathing room). Default 0.0.
        fit_map: when True (default), emit a ``zoom-to`` map-command so the view
            fits the computed bounds. Set False to compute the extent only
            (no camera move).

    Returns:
        {
          "min_lon": float, "min_lat": float, "max_lon": float, "max_lat": float,
          "bbox": [min_lon, min_lat, max_lon, max_lat],  # convenience tuple
          "layer_type": "vector" | "raster",
          "crs": "EPSG:4326",
          "pad_fraction": float,
          "map_fitted": bool,        # True iff a zoom-to map-command was emitted
          "layer_uri": str,          # provenance
          "computed_at": str,        # ISO 8601 timestamp
        }

    Raises:
        ComputeLayerBoundsError: typed (FR-AS-11) on unreadable URI, open
            failure, empty layer, or degenerate bounds.
    """
    computed_at = datetime.now(timezone.utc).isoformat()

    local_path, is_temp = _resolve_layer_to_local_path(layer_uri, _storage_client)
    try:
        layer_type = _detect_layer_type(local_path) or _detect_layer_type(layer_uri)
        if layer_type == "raster":
            raw_bbox = _bounds_from_raster(local_path)
        elif layer_type == "vector":
            raw_bbox = _bounds_from_vector(local_path)
        else:
            # Unknown extension — probe raster first, then vector.
            try:
                raw_bbox = _bounds_from_raster(local_path)
                layer_type = "raster"
            except ComputeLayerBoundsError:
                raw_bbox = _bounds_from_vector(local_path)
                layer_type = "vector"
    finally:
        if is_temp:
            try:
                os.unlink(local_path)
            except OSError:
                pass

    if not all(math.isfinite(v) for v in raw_bbox):
        raise ComputeLayerBoundsError(
            "DEGENERATE_BOUNDS",
            f"layer {layer_uri!r} produced non-finite bounds {raw_bbox!r}.",
        )

    bbox = _apply_pad(raw_bbox, max(0.0, float(pad_fraction)))
    min_lon, min_lat, max_lon, max_lat = bbox

    # --- Fit the map: emit a zoom-to map-command via the existing seam.
    # Mirrors model_flood_scenario (job-0160): read the active emitter from the
    # _CURRENT_EMITTER ContextVar (bound by PipelineEmitter.emit_tool_call) and
    # fire ``map-command(zoom-to)``. Outside an emit_tool_call scope (direct
    # call, smoke harness, unit test without an emitter) current_emitter()
    # returns None and we skip silently — emitting is a UX action, not a
    # correctness gate, and the bbox is still returned for the agent / server.
    map_fitted = False
    if fit_map:
        from ..pipeline_emitter import current_emitter

        emitter = current_emitter()
        if emitter is not None:
            try:
                await emitter.emit_map_command("zoom-to", {"bbox": list(bbox)})
                map_fitted = True
                logger.info(
                    "compute_layer_bounds: emitted zoom-to bbox=%s (layer_type=%s)",
                    bbox,
                    layer_type,
                )
            except Exception as exc:  # noqa: BLE001 — non-fatal UX hint
                logger.warning(
                    "compute_layer_bounds: zoom-to emit failed (non-fatal): %s", exc
                )

    return {
        "min_lon": min_lon,
        "min_lat": min_lat,
        "max_lon": max_lon,
        "max_lat": max_lat,
        "bbox": [min_lon, min_lat, max_lon, max_lat],
        "layer_type": layer_type,
        "crs": "EPSG:4326",
        "pad_fraction": max(0.0, float(pad_fraction)),
        "map_fitted": map_fitted,
        "layer_uri": layer_uri,
        "computed_at": computed_at,
    }
