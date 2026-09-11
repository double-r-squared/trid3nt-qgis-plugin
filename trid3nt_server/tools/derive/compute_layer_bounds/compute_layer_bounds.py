"""Atomic tool ``compute_layer_bounds`` - layer extent, and fit the map to it.

Extent math never goes through ``code_exec_request``: this is the deterministic
path, and it also emits the ``zoom-to`` that makes the view follow the answer.
"""
from __future__ import annotations

import logging
import math
import os
import tempfile
from datetime import datetime, timezone
from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.workflows.shared.geometry import source_uri

__all__ = [
    "compute_layer_bounds",
    "ComputeLayerBoundsError",
]

logger = logging.getLogger("trid3nt_server.tools.derive.compute_layer_bounds.compute_layer_bounds")


# ---------------------------------------------------------------------------
# Error class (typed errors)
# ---------------------------------------------------------------------------


# ``error_code`` is one of UNKNOWN_LAYER_URI, DOWNLOAD_FAILED,
# RASTER_OPEN_FAILED, VECTOR_OPEN_FAILED, GEOPANDAS_UNAVAILABLE, EMPTY_LAYER,
# DEGENERATE_BOUNDS.
class ComputeLayerBoundsError(RuntimeError):
    """Layer-bounds computation failed."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


# ---------------------------------------------------------------------------
# Metadata. Never cached: the tool drives the map view and is sub-second.
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
# URI to local path materialization
# ---------------------------------------------------------------------------


def _infer_suffix(uri: str) -> str:
    """A temp-file suffix matching the URI extension, so the driver is detected
    off the path; ``.bin`` when nothing matches.
    """
    base = uri.split("?")[0].rstrip("/")
    lower = base.lower()
    for ext in (*_RASTER_EXTENSIONS, *_VECTOR_EXTENSIONS):
        if lower.endswith(ext):
            return ext
    return ".bin"


def _resolve_layer_to_local_path(
    uri: str, storage_client: object | None = None
) -> tuple[str, bool]:
    """Resolve an ``s3://`` URI or a local path to ``(path, is_temp)``; the caller
    deletes the path iff ``is_temp``. ``storage_client`` is ignored.
    """
    del storage_client
    # A chained row hands over whatever the producing tool RETURNED, so a
    # LayerURI enters here exactly as a typed path does; refusing the object
    # while accepting the uri it carries would make a chain depend on the author
    # remembering to write ``.uri``.
    uri = str(source_uri(uri))
    suffix = _infer_suffix(uri)

    if uri.startswith("s3://"):
        from trid3nt_server.tools.cache import read_object_bytes_s3

        try:
            data = read_object_bytes_s3(uri)
        except Exception as exc:  # noqa: BLE001
            raise ComputeLayerBoundsError(
                "DOWNLOAD_FAILED", f"S3 download failed for {uri!r}: {exc}"
            ) from exc
        with tempfile.NamedTemporaryFile(
            suffix=suffix, delete=False, prefix="trid3nt_bounds_"
        ) as tmp:
            tmp.write(data)
            return tmp.name, True

    if os.path.isfile(uri):
        return uri, False

    raise ComputeLayerBoundsError(
        "UNKNOWN_LAYER_URI",
        f"layer URI {uri!r} is not an s3:// URI or a readable local file.",
    )


# ---------------------------------------------------------------------------
# Layer-type detection and per-type bbox extraction
# ---------------------------------------------------------------------------


def _detect_layer_type(uri: str) -> str | None:
    """``"raster"`` or ``"vector"`` from the extension, else None for the caller
    to probe rasterio and then geopandas.
    """
    ext = os.path.splitext(uri.split("?")[0].rstrip("/"))[-1].lower()
    if ext in _RASTER_EXTENSIONS:
        return "raster"
    if ext in _VECTOR_EXTENSIONS:
        return "vector"
    return None


def _bounds_from_raster(path: str) -> tuple[float, float, float, float]:
    """A raster's ``(min_lon, min_lat, max_lon, max_lat)``, reprojected to
    EPSG:4326 when its CRS differs.
    """
    try:
        import rasterio  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover -- rasterio is a hard dep
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
    """A vector's ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326."""
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ComputeLayerBoundsError(
            "GEOPANDAS_UNAVAILABLE", f"geopandas / pyogrio not available: {exc}"
        ) from exc
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
    except Exception:  # noqa: BLE001 -- retry without the explicit engine
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

    minx, miny, maxx, maxy = _bbox_from_gdf(gdf)
    return (float(minx), float(miny), float(maxx), float(maxy))


def _bbox_from_gdf(gdf: Any) -> tuple[float, float, float, float]:
    """``(minLon, minLat, maxLon, maxLat)`` from ``total_bounds``, reprojected when
    a differing CRS is set; a CRS of None or non-finite bounds take the world.
    """
    try:
        crs = getattr(gdf, "crs", None)
        if crs is not None and str(crs).upper() not in (
            "EPSG:4326",
            "EPSG: 4326",
            "WGS 84",
            "WGS84",
        ):
            try:
                gdf_4326 = gdf.to_crs("EPSG:4326")
            except Exception:  # noqa: BLE001 -- fall back to raw bounds
                gdf_4326 = gdf
        else:
            gdf_4326 = gdf
        bounds = gdf_4326.total_bounds  # [minx, miny, maxx, maxy]
        minx, miny, maxx, maxy = (
            float(bounds[0]),
            float(bounds[1]),
            float(bounds[2]),
            float(bounds[3]),
        )
        # A single-point or degenerate layer can report NaN bounds.
        if not all(math.isfinite(v) for v in (minx, miny, maxx, maxy)):
            return (-180.0, -90.0, 180.0, 90.0)
        return (minx, miny, maxx, maxy)
    except Exception as exc:  # noqa: BLE001
        logger.warning("bbox extraction failed: %s; defaulting to world bbox", exc)
        return (-180.0, -90.0, 180.0, 90.0)


def _apply_pad(
    bbox: tuple[float, float, float, float], pad_fraction: float
) -> tuple[float, float, float, float]:
    """Pad a 4326 bbox by ``pad_fraction`` per side, clamped; a zero-width or
    zero-height layer takes an absolute 0.001 deg so it is never a sliver.
    """
    minx, miny, maxx, maxy = bbox
    w = maxx - minx
    h = maxy - miny
    pad_x = w * pad_fraction if w > 0 else 0.001
    pad_y = h * pad_fraction if h > 0 else 0.001
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


#: The pad is capped at a quarter turn per axis: at high latitude a metre pad is
#: a large number of degrees of longitude, and past this it is the whole parallel.
_PAD_MAX_DEG = 90.0


def _apply_pad_m(
    bbox: tuple[float, float, float, float], pad_m: float
) -> tuple[float, float, float, float]:
    """Pad a 4326 bbox by ``pad_m`` METRES on each side."""
    # A distance, not a fraction: what a query window must reach past its
    # subject is a distance, and a channel a kilometre off the centreline is the
    # same kilometre whether the reach is one km long or fifty. The degrees that
    # distance buys are walked geodesically west and north, so the longitude pad
    # widens with latitude as it should.
    from pyproj import Geod

    minx, miny, maxx, maxy = bbox
    if pad_m <= 0.0:
        return bbox
    geod = Geod(ellps="WGS84")
    mid_lat = 0.5 * (miny + maxy)
    west_lon, _, _ = geod.fwd(minx, mid_lat, 270.0, pad_m)
    _, south_lat, _ = geod.fwd(minx, miny, 180.0, pad_m)
    dx = min(_PAD_MAX_DEG, abs(minx - west_lon))
    dy = min(_PAD_MAX_DEG, abs(miny - south_lat))
    return (max(-180.0, minx - dx), max(-90.0, miny - dy),
            min(180.0, maxx + dx), min(90.0, maxy + dy))


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


@register_tool(
    _COMPUTE_LAYER_BOUNDS_METADATA,
    read_only_hint=True,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
)
async def compute_layer_bounds(
    layer_uri: str,
    pad_fraction: float = 0.0,
    pad_m: float = 0.0,
    *,
    fit_map: bool = True,
    _storage_client: object | None = None,
    # absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Get a layer's geographic extent AND fit/zoom/resize the map to it.

    Use when the user asks to fit the map to a layer, zoom to all the points,
    resize the bbox to encompass features, or wants a layer's extent. NEVER use
    ``code_exec_request`` for extent math - this is the deterministic path and
    it moves the view too.

    Params:
        layer_uri: the layer's ``layer_id`` handle, or an s3:// URI or local
            path. The extent is reprojected to EPSG:4326.
        pad_fraction: fractional pad per side; 0.0 (default) is exact.
        pad_m: pad per side in METRES, for a query window that must reach a
            stated distance past the layer. Applied after ``pad_fraction``.
        fit_map: True (default) also emits the zoom-to.

    Returns the four EPSG:4326 corners, the bbox list, the layer type and
    whether the map was fitted.
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

    bbox = _apply_pad_m(_apply_pad(raw_bbox, max(0.0, float(pad_fraction))),
                        max(0.0, float(pad_m)))
    min_lon, min_lat, max_lon, max_lat = bbox

    # Emitting is a UX action, not a correctness gate: outside an emitter scope
    # there is nothing to fire at, and the bbox is still returned.
    map_fitted = False
    if fit_map:
        from trid3nt_server.emission.pipeline_emitter import current_emitter

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
            except Exception as exc:  # noqa: BLE001 -- non-fatal UX hint
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
        "pad_m": max(0.0, float(pad_m)),
        "map_fitted": map_fitted,
        "layer_uri": layer_uri,
        "computed_at": computed_at,
    }
