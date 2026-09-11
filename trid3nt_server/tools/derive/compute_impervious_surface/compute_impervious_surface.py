"""Atomic tool ``compute_impervious_surface`` - NLCD impervious-fraction raster.

Reads the NLCD Impervious Surface product (percent, scaled by 1/100) or the Land
Cover raster (through the developed classes), keeping the input CRS and grid.
"""
from __future__ import annotations
from typing import Any

import logging
import os
import tempfile

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.cache import CACHE_BUCKET, read_through

__all__ = [
    "compute_impervious_surface",
    "ImperviousSurfaceError",
    "DEVELOPED_CLASS_TO_IMPERVIOUS",
]

logger = logging.getLogger("trid3nt_server.tools.derive.compute_impervious_surface.compute_impervious_surface")

# ---------------------------------------------------------------------------
# Error class
# ---------------------------------------------------------------------------


# ``error_code`` is one of RASTER_OPEN_FAILED, RASTER_DOWNLOAD_FAILED,
# RASTER_WRITE_FAILED, UNKNOWN_RASTER_URI, BBOX_OUTSIDE_RASTER.
class ImperviousSurfaceError(RuntimeError):
    """Impervious-surface computation failed."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


# ---------------------------------------------------------------------------
# Tool metadata
# ---------------------------------------------------------------------------

_IMPERVIOUS_METADATA = AtomicToolMetadata(
    name="compute_impervious_surface",
    ttl_class="static-30d",
    source_class="impervious",
    cacheable=True,
)


# ---------------------------------------------------------------------------
# Developed-class to impervious-fraction mapping
# ---------------------------------------------------------------------------

#: USGS NLCD developed-density classes -> representative impervious fraction:
#: 21 Open Space (under 20% impervious), 22 Low (20-49%), 23 Medium (50-79%),
#: 24 High (80-100%), taking the USGS companion product's typical midpoints.
#: Every other NLCD class maps to 0.0.
DEVELOPED_CLASS_TO_IMPERVIOUS: dict[int, float] = {
    21: 0.0,
    22: 0.3,
    23: 0.6,
    24: 0.9,
}


# ---------------------------------------------------------------------------
# Object-store read helper
# ---------------------------------------------------------------------------


def _download_raster_bytes(uri: str, storage_client: object | None = None) -> bytes:
    """Raster bytes from an ``s3://`` URI or a local path; ``storage_client`` is
    ignored and any failure raises ``ImperviousSurfaceError``.
    """
    del storage_client
    if uri.startswith("s3://"):
        from trid3nt_server.tools.cache import read_object_bytes_s3
        try:
            return read_object_bytes_s3(uri)
        except Exception as exc:  # noqa: BLE001
            raise ImperviousSurfaceError(
                "RASTER_DOWNLOAD_FAILED",
                f"S3 download failed for {uri!r}: {exc}",
            ) from exc
    try:
        with open(uri, "rb") as f:
            return f.read()
    except OSError as exc:
        raise ImperviousSurfaceError(
            "RASTER_DOWNLOAD_FAILED",
            f"Could not read local raster path {uri!r}: {exc}",
        ) from exc


# ---------------------------------------------------------------------------
# Product-type detection
# ---------------------------------------------------------------------------


def _is_impervious_product(uri: str, tags: dict[str, object] | None) -> bool:
    """True when the input looks like the NLCD Impervious Surface product; False
    routes the caller through the landcover developed-class mapping instead.
    """
    # The signals are an "impervious" token anywhere in the URI, or a raster tag
    # whose key or value carries impervious / percent_developed_impervious.
    if "impervious" in uri.lower():
        return True
    if tags:
        for k, v in tags.items():
            if "impervious" in str(k).lower():
                return True
            sv = str(v).lower()
            if "impervious_surface" in sv or "percent_developed_impervious" in sv:
                return True
    return False


# ---------------------------------------------------------------------------
# Core computation
# ---------------------------------------------------------------------------


def _derive_impervious_from_landcover(
    landcover_array: object,  # numpy.ndarray (int)
    nodata: int | float | None,
) -> object:  # numpy.ndarray (float32)
    """NLCD class codes to a float32 impervious fraction in [0.0, 1.0], NaN
    wherever the input is nodata.
    """
    import numpy as np  # type: ignore[import-not-found]

    out = np.zeros(landcover_array.shape, dtype=np.float32)
    # Every non-developed class stays at 0.0: a forest is 0% impervious, and so
    # is a water body from a runoff-routing view - the water IS the destination.
    for class_code, fraction in DEVELOPED_CLASS_TO_IMPERVIOUS.items():
        out[landcover_array == class_code] = fraction

    if nodata is not None:
        out[landcover_array == nodata] = np.nan

    return out


def _scale_impervious_product(
    impervious_array: object,  # numpy.ndarray (int)
    nodata: int | float | None,
) -> object:  # numpy.ndarray (float32)
    """Percent-impervious values to a float32 fraction in [0.0, 1.0], NaN wherever
    the input is nodata.
    """
    import numpy as np  # type: ignore[import-not-found]

    out = impervious_array.astype(np.float32) / 100.0

    # A value outside the range is nodata or an upstream encoding anomaly;
    # clipping beats emitting a fraction above 1.0 for a consumer to misread.
    out = np.clip(out, 0.0, 1.0)

    if nodata is not None:
        out[impervious_array == nodata] = np.nan

    return out


def _compute_impervious_bytes(
    landcover_bytes: bytes,
    bbox: tuple[float, float, float, float] | None,
    force_impervious_product: bool | None = None,
) -> bytes:
    """Single-band Float32 GeoTIFF bytes, nodata NaN, CRS and transform inherited;
    a ``bbox`` that misses the raster raises BBOX_OUTSIDE_RASTER.
    """
    import numpy as np  # type: ignore[import-not-found]
    import rasterio  # type: ignore[import-not-found]
    from rasterio.io import MemoryFile  # type: ignore[import-not-found]
    from rasterio.warp import transform_bounds  # type: ignore[import-not-found]
    from rasterio.windows import from_bounds as window_from_bounds  # type: ignore[import-not-found]

    try:
        memfile = MemoryFile(landcover_bytes)
    except Exception as exc:  # noqa: BLE001
        raise ImperviousSurfaceError(
            "RASTER_OPEN_FAILED",
            f"rasterio MemoryFile open failed: {exc}",
        ) from exc

    try:
        with memfile.open() as src:
            src_crs = src.crs
            src_transform = src.transform
            src_nodata = src.nodata
            tags = src.tags()

            if force_impervious_product is not None:
                is_imp = bool(force_impervious_product)
            else:
                # No URI at this layer, so only the tags can be inspected; the
                # caller has already applied the filename signal.
                is_imp = _is_impervious_product("", tags)

            if bbox is not None:
                if src_crs is None:
                    raise ImperviousSurfaceError(
                        "RASTER_OPEN_FAILED",
                        "Input raster has no CRS; cannot transform bbox.",
                    )
                # bbox is EPSG:4326 per LayerURI / docstring convention.
                try:
                    west, south, east, north = transform_bounds(
                        "EPSG:4326", src_crs, *bbox, densify_pts=21
                    )
                except Exception as exc:  # noqa: BLE001
                    raise ImperviousSurfaceError(
                        "RASTER_OPEN_FAILED",
                        f"bbox transform_bounds failed: {exc}",
                    ) from exc

                try:
                    window = window_from_bounds(
                        west, south, east, north, transform=src_transform
                    )
                    # Intersect with the raster's own window.
                    raster_window = rasterio.windows.Window(
                        0, 0, src.width, src.height
                    )
                    # rasterio's intersection helper -- returns the overlap.
                    window = window.intersection(raster_window)
                except (ValueError, rasterio.windows.WindowError) as exc:
                    raise ImperviousSurfaceError(
                        "BBOX_OUTSIDE_RASTER",
                        f"bbox {bbox} does not intersect raster: {exc}",
                    ) from exc

                # Round window to integer pixels (rasterio convention).
                window = window.round_lengths().round_offsets()
                if window.width <= 0 or window.height <= 0:
                    raise ImperviousSurfaceError(
                        "BBOX_OUTSIDE_RASTER",
                        f"bbox {bbox} produced an empty window for the raster.",
                    )

                arr = src.read(1, window=window)
                out_transform = src.window_transform(window)
                out_width = int(window.width)
                out_height = int(window.height)
            else:
                arr = src.read(1)
                out_transform = src_transform
                out_width = src.width
                out_height = src.height

            # Compute output array.
            if is_imp:
                logger.info(
                    "compute_impervious_surface: scaling impervious product "
                    "(min=%s, max=%s, nodata=%s)",
                    arr.min(),
                    arr.max(),
                    src_nodata,
                )
                result = _scale_impervious_product(arr, src_nodata)
            else:
                logger.info(
                    "compute_impervious_surface: deriving from landcover "
                    "(unique classes=%s, nodata=%s)",
                    np.unique(arr).tolist()[:10],
                    src_nodata,
                )
                result = _derive_impervious_from_landcover(arr, src_nodata)

            # Sanity: result must be Float32 in [0.0, 1.0] or NaN.
            assert result.dtype == np.float32, (
                f"result dtype is {result.dtype}, expected float32"
            )

            # Write Float32 GeoTIFF to bytes.
            profile = {
                "driver": "GTiff",
                "dtype": "float32",
                "count": 1,
                "width": out_width,
                "height": out_height,
                "crs": src_crs,
                "transform": out_transform,
                "nodata": float("nan"),
                "compress": "deflate",
                "predictor": 3,  # float predictor for float32
                "tiled": True,
                "blockxsize": 256,
                "blockysize": 256,
            }

            try:
                with MemoryFile() as out_memfile:
                    with out_memfile.open(**profile) as dst:
                        dst.write(result, 1)
                        # Tag the output so consumers can identify it.
                        dst.update_tags(
                            trid3nt_tool="compute_impervious_surface",
                            trid3nt_source_path=(
                                "impervious_product" if is_imp else "landcover_derived"
                            ),
                            trid3nt_units="fraction",
                        )
                    return out_memfile.read()
            except Exception as exc:  # noqa: BLE001
                raise ImperviousSurfaceError(
                    "RASTER_WRITE_FAILED",
                    f"output GeoTIFF write failed: {exc}",
                ) from exc

    except ImperviousSurfaceError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ImperviousSurfaceError(
            "RASTER_OPEN_FAILED",
            f"unexpected error reading input raster: {exc}",
        ) from exc
    finally:
        try:
            memfile.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


@register_tool(
    _IMPERVIOUS_METADATA,
    # Annotations: readOnlyHint=True (reads input raster/vector; writes cache
    # artifact only via the read-through shim), openWorldHint=False (all
    # computation is local GDAL/numpy; no external API calls),
    # destructiveHint=False, idempotentHint=True (deterministic transform;
    # same inputs always produce the same output pixels).
)
def compute_impervious_surface(
    landcover_uri: str,
    bbox: tuple[float, float, float, float] | None = None,
    *,
    _storage_client: object | None = None,
    _bucket: str | None = None,
    # absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> LayerURI:
    """NLCD impervious-surface fraction computation.

    Use this when: urban hydrology/runoff analysis, hydraulic model
    infiltration parameterization, urban heat island / stormwater capacity, or a
    percent-developed visualization needs an impervious fraction (0.0-1.0).
    Path auto-selects: NLCD Impervious Surface product (0-100 int) scaled
    by 1/100, or NLCD landcover dev-class mapping (21->0.0, 22->0.3,
    23->0.6, 24->0.9). Do NOT use for: per-building impervious area
    (``fetch_buildings`` + geometry); non-CONUS coverage (NLCD is CONUS
    L48 only).

    Params:
        landcover_uri: NLCD landcover or NLCD Impervious Surface GeoTIFF
            (typically from ``fetch_landcover``); path auto-detected via
            filename/tags.
        bbox: optional (min_lon, min_lat, max_lon, max_lat) EPSG:4326 to
            window-read a large source raster; ``None`` processes the
            full input.

    Returns a single-band Float32 GeoTIFF in [0.0, 1.0] with NaN nodata, on the
    input's own CRS and grid.
    """
    effective_bucket = _bucket or CACHE_BUCKET

    if bbox is not None:
        if len(bbox) != 4:
            raise ImperviousSurfaceError(
                "BBOX_OUTSIDE_RASTER",
                f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}",
            )
        min_lon, min_lat, max_lon, max_lat = bbox
        if min_lon >= max_lon or min_lat >= max_lat:
            raise ImperviousSurfaceError(
                "BBOX_OUTSIDE_RASTER",
                f"bbox is degenerate (min must be < max on both axes): {bbox!r}",
            )

    # The URI token is the strongest product signal, so it is resolved here and
    # passed down as an override of the tag inspection.
    uri_says_impervious = "impervious" in landcover_uri.lower()

    def _fetch() -> bytes:
        raster_bytes = _download_raster_bytes(landcover_uri, _storage_client)
        return _compute_impervious_bytes(
            raster_bytes,
            bbox=bbox,
            force_impervious_product=(True if uri_says_impervious else None),
        )

    # Cache key on (landcover_uri, bbox).
    params: dict[str, object] = {
        "landcover_uri": landcover_uri,
    }
    if bbox is not None:
        # 6dp is about 0.1 m, enough for cache-key stability across jitter.
        params["bbox"] = [round(v, 6) for v in bbox]

    result = read_through(
        metadata=_IMPERVIOUS_METADATA,
        params=params,
        ext="tif",
        fetch_fn=_fetch,
        bucket=effective_bucket,
        storage_client=_storage_client,
    )
    assert result.uri is not None, (
        "compute_impervious_surface is cacheable; uri must be set by read_through"
    )

    # Build a stable layer_id from the input URI's last path component.
    input_key = landcover_uri.rstrip("/").rsplit("/", 1)[-1]
    for ext in (".tif", ".tiff", ".TIF", ".TIFF"):
        if input_key.endswith(ext):
            input_key = input_key[: -len(ext)]
            break
    bbox_suffix = ""
    if bbox is not None:
        bbox_suffix = f"-bbox{bbox[0]:.4f}-{bbox[1]:.4f}"

    return LayerURI(
        layer_id=f"impervious-{input_key}{bbox_suffix}",
        name="Impervious Surface Fraction (NLCD-derived)",
        layer_type="raster",
        uri=result.uri,
        style={"kind": "continuous", "ramp": "reds", "units": "%",
         "label": "Impervious surface",
         "scale": {"policy": "fixed", "range": [0, 100],
         "transform": "linear"}},
        role="context",
        units=None,
    )
