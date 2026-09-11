"""Atomic tool ``extract_landcover_class`` - NLCD binary-mask extractor.

The output is uint8: 1 where a pixel matches a requested class, 0 for any other
valid pixel, 255 for nodata. 255 is therefore reserved and cannot be extracted.
"""
from __future__ import annotations

import contextlib
import logging
import math
import os
import tempfile
from typing import Any

import numpy as np
import rasterio
from rasterio.windows import from_bounds, Window

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.cache import CACHE_BUCKET, read_through

__all__ = [
    "extract_landcover_class",
    "LandcoverClassError",
]

logger = logging.getLogger("trid3nt_server.tools.derive.extract_landcover_class.extract_landcover_class")


# ---------------------------------------------------------------------------
# Error class
# ---------------------------------------------------------------------------


# ``error_code`` is one of CLASSES_EMPTY, CLASSES_INVALID, BBOX_INVALID,
# RASTER_OPEN_FAILED, WINDOW_EMPTY, WRITE_FAILED.
class LandcoverClassError(RuntimeError):
    """The input could not be read or processed."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


# ---------------------------------------------------------------------------
# Tool metadata
# ---------------------------------------------------------------------------

_METADATA = AtomicToolMetadata(
    name="extract_landcover_class",
    ttl_class="static-30d",
    source_class="landcover_class",
    cacheable=True,
)


# Output sentinel: nodata pixels in the input map to 255 in the binary mask.
# 0 = not-in-classes, 1 = in-classes, 255 = nodata.
_NODATA_OUT = 255

# NLCD source classes can legally be 0-254 (uint8). The output sentinel 255 is
# reserved for nodata; if the caller asks to extract 255 we refuse -- it would
# silently merge with nodata.
_NLCD_MAX_CLASS = 254


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_classes(classes: list[int]) -> list[int]:
    """The sorted, de-duplicated class list; empty or out-of-range raises."""
    if not classes:
        raise LandcoverClassError(
            "CLASSES_EMPTY",
            "classes argument is empty; provide at least one NLCD class code "
            "(e.g. [11] for Open Water).",
        )
    sorted_classes = sorted({int(c) for c in classes})
    for c in sorted_classes:
        if c < 0 or c > _NLCD_MAX_CLASS:
            raise LandcoverClassError(
                "CLASSES_INVALID",
                f"class code {c} is out of valid NLCD range [0, {_NLCD_MAX_CLASS}]. "
                f"255 is reserved for the output nodata sentinel.",
            )
    return sorted_classes


def _validate_bbox(bbox: tuple[float, float, float, float] | None) -> None:
    """Raise ``LandcoverClassError`` if bbox is malformed (None is allowed)."""
    if bbox is None:
        return
    if len(bbox) != 4:
        raise LandcoverClassError(
            "BBOX_INVALID",
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}",
        )
    min_lon, min_lat, max_lon, max_lat = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise LandcoverClassError(
            "BBOX_INVALID",
            f"bbox contains non-finite values: {bbox!r}",
        )
    if min_lon >= max_lon or min_lat >= max_lat:
        raise LandcoverClassError(
            "BBOX_INVALID",
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}",
        )


def _round_bbox(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Round bbox coordinates to 6 decimal places for cache-key stability."""
    return tuple(round(v, 6) for v in bbox)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Source-raster opener
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _open_source(landcover_uri: str) -> Any:
    """Open the landcover raster for read: ``s3://`` bytes are staged and opened
    in-memory, a local path opens natively.
    """
    # This is a context manager because a MemoryFile whose dataset outlives it
    # is GC'd mid-read - the /vsimem/ buffer is freed and reads return valid
    # pixels PLUS uninitialized garbage. Yielding the dataset from inside the
    # ``with MemoryFile(...)`` pins the buffer for the dataset's whole lifetime.
    # boto3 stages the bytes because GDAL's own /vsis3/ credential chain does
    # not resolve the instance role in this environment.
    if landcover_uri.startswith("s3://"):
        from rasterio.io import MemoryFile
        from trid3nt_server.tools.cache import read_object_bytes_s3
        try:
            mf = MemoryFile(read_object_bytes_s3(landcover_uri))
            dataset = mf.open()
        except Exception as exc:  # noqa: BLE001
            raise LandcoverClassError(
                "RASTER_OPEN_FAILED",
                f"rasterio could not open {landcover_uri!r}: {exc}",
            ) from exc
        # The yield sits OUTSIDE the open's try/except, so a caller-side error
        # is never mis-attributed to the open.
        with mf, dataset as src:
            yield src
        return
    else:
        path = landcover_uri
    try:
        dataset = rasterio.open(path)
    except Exception as exc:  # noqa: BLE001
        raise LandcoverClassError(
            "RASTER_OPEN_FAILED",
            f"rasterio could not open {landcover_uri!r}: {exc}",
        ) from exc
    with dataset as src:
        yield src


# ---------------------------------------------------------------------------
# Core extraction
# ---------------------------------------------------------------------------


def _extract_mask_bytes(
    landcover_uri: str,
    classes_sorted: list[int],
    bbox: tuple[float, float, float, float] | None,
) -> bytes:
    """The binary mask as LZW GeoTIFF bytes, read from a bbox window when one is
    given, else from the whole source raster.
    """
    with _open_source(landcover_uri) as src:
        src_nodata = src.nodata
        src_crs = src.crs

        if bbox is None:
            window: Window = Window(0, 0, src.width, src.height)
            src_array = src.read(1, window=window)
            out_transform = src.transform
        else:
            try:
                window_full = from_bounds(
                    bbox[0], bbox[1], bbox[2], bbox[3], transform=src.transform
                )
            except Exception as exc:  # noqa: BLE001
                raise LandcoverClassError(
                    "BBOX_INVALID",
                    f"failed to compute window for bbox={bbox} "
                    f"src_bounds={src.bounds}: {exc}",
                ) from exc

            # rasterio's Window carries no intersection helper, so the offsets
            # are clipped to the source extent by hand.
            col_off = max(0, int(math.floor(window_full.col_off)))
            row_off = max(0, int(math.floor(window_full.row_off)))
            col_max = min(
                src.width,
                int(math.ceil(window_full.col_off + window_full.width)),
            )
            row_max = min(
                src.height,
                int(math.ceil(window_full.row_off + window_full.height)),
            )
            w = col_max - col_off
            h = row_max - row_off
            if w <= 0 or h <= 0:
                raise LandcoverClassError(
                    "WINDOW_EMPTY",
                    f"bbox={bbox} does not intersect source raster "
                    f"bounds={src.bounds}",
                )
            window = Window(col_off, row_off, w, h)
            src_array = src.read(1, window=window)
            # Derive the output transform from the windowed slice.
            out_transform = rasterio.windows.transform(window, src.transform)

    # ----- Build the binary mask -----
    # 1=match, 0=other, 255=nodata.
    # ``np.isin`` is the cleanest way to test membership against a small set.
    # For NLCD inputs (uint8) this is a single-pass O(N) operation.
    arr = np.asarray(src_array)
    classes_arr = np.array(classes_sorted, dtype=arr.dtype if arr.dtype.kind == "u" else np.int64)

    in_class = np.isin(arr, classes_arr)
    mask = np.where(in_class, 1, 0).astype(np.uint8)

    # Apply nodata preservation. NLCD source rasters typically have a uint8
    # nodata sentinel (often 255 itself). If src_nodata is None we treat every
    # pixel as valid (0 or 1 only).
    if src_nodata is not None:
        try:
            nodata_val = int(src_nodata)
        except (TypeError, ValueError):
            # Float nodata on a categorical raster is unusual but possible.
            # Fall back to NaN-style comparison if it's NaN, else int cast.
            if isinstance(src_nodata, float) and math.isnan(src_nodata):
                nodata_mask = np.zeros_like(arr, dtype=bool)
            else:
                nodata_val = int(src_nodata)
                nodata_mask = arr == nodata_val
        else:
            nodata_mask = arr == nodata_val
        mask = np.where(nodata_mask, _NODATA_OUT, mask).astype(np.uint8)

    height, width = mask.shape

    # ----- Write LZW-compressed COG-style GeoTIFF -----
    profile = {
        "driver": "GTiff",
        "dtype": "uint8",
        "width": width,
        "height": height,
        "count": 1,
        "crs": src_crs,
        "transform": out_transform,
        "nodata": _NODATA_OUT,
        "compress": "lzw",
        "tiled": True,
        # 256x256 blocksize for COG-friendly tiling; rasterio enforces a
        # multiple-of-16 constraint and falls back to a strip layout when the
        # raster is too small for tiling -- we accept that fallback.
        "blockxsize": 256,
        "blockysize": 256,
    }

    out_tmp: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".tif", delete=False, prefix="trid3nt_landcover_mask_"
        ) as f:
            out_tmp = f.name
        # If the output is smaller than the tile size, drop tiling.
        if width < 256 or height < 256:
            profile["tiled"] = False
            profile.pop("blockxsize", None)
            profile.pop("blockysize", None)
        with rasterio.open(out_tmp, "w", **profile) as dst:
            dst.write(mask, 1)
            dst.update_tags(
                source_uri=landcover_uri,
                classes=",".join(str(c) for c in classes_sorted),
                bbox=str(bbox) if bbox is not None else "full",
                nodata_value=str(_NODATA_OUT),
                tool="extract_landcover_class",
            )

        with open(out_tmp, "rb") as f:
            out_bytes = f.read()

        n_match = int(np.sum(mask == 1))
        n_other = int(np.sum(mask == 0))
        n_nd = int(np.sum(mask == _NODATA_OUT))
        logger.info(
            "extract_landcover_class: shape=%dx%d classes=%s match=%d other=%d nodata=%d bytes=%d",
            height,
            width,
            classes_sorted,
            n_match,
            n_other,
            n_nd,
            len(out_bytes),
        )
        return out_bytes
    except LandcoverClassError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise LandcoverClassError(
            "WRITE_FAILED",
            f"rasterio could not write output GeoTIFF: {exc}",
        ) from exc
    finally:
        if out_tmp is not None:
            try:
                os.unlink(out_tmp)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Registered atomic tool
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    # Annotations: readOnlyHint=True (reads input raster/vector; writes cache
    # artifact only via the read-through shim), openWorldHint=False (all
    # computation is local GDAL/numpy; no external API calls),
    # destructiveHint=False, idempotentHint=True (deterministic transform;
    # same inputs always produce the same output pixels).
)
def extract_landcover_class(
    landcover_uri: str,
    classes: list[int],
    bbox: tuple[float, float, float, float] | None = None,
    *,
    _storage_client: object | None = None,
    _bucket: str | None = None,
    # absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> LayerURI:
    """NLCD landcover-class binary mask extractor.

    Use when you have an NLCD landcover raster and need a binary mask: water
    from class 11 for zonal flood-depth stats, forest from 41/42/43 for habitat,
    developed from 21-24 for exposure. Not for non-NLCD land cover, whose codes
    differ, and not for per-pixel stats across classes.

    Params:
        landcover_uri: NLCD-coded GeoTIFF of canonical class integers, not
            palette indices - the ``fetch_landcover`` layer.
        classes: NLCD codes to extract, at least one. 11=Open Water,
            21-24=Developed, 31=Barren, 41/42/43=Forest, 52=Shrub, 71-74=
            Herbaceous, 81/82=Pasture/Cropland, 90/95=Wetlands. 255 is
            rejected: it is the nodata sentinel.
        bbox: clip window; None processes the full raster.

    Returns a uint8 mask - 1 match, 0 other, 255 nodata - usable as a zone mask.
    """
    effective_bucket = _bucket or CACHE_BUCKET

    # Validation runs before the cache lookup, so a bad call fails on its own
    # terms rather than on a key miss.
    classes_sorted = _validate_classes(classes)
    _validate_bbox(bbox)

    bbox_rounded = _round_bbox(bbox) if bbox is not None else None

    # The year tag pins the vintage, so bumping the default changes the key
    # rather than serving a stale mask under it.
    params: dict[str, object] = {
        "landcover_uri": landcover_uri,
        "classes": classes_sorted,
        "year": "2021",
    }
    if bbox_rounded is not None:
        params["bbox"] = list(bbox_rounded)

    def _fetch() -> bytes:
        return _extract_mask_bytes(landcover_uri, classes_sorted, bbox_rounded)

    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="tif",
        fetch_fn=_fetch,
        bucket=effective_bucket,
        storage_client=_storage_client,
    )
    assert result.uri is not None, (
        "extract_landcover_class is cacheable; uri must be set by read_through"
    )

    src_key = landcover_uri.rstrip("/").rsplit("/", 1)[-1].replace(".tif", "")
    classes_tag = "-".join(str(c) for c in classes_sorted)
    layer_id = f"landcover-class-{src_key}-{classes_tag}"

    if len(classes_sorted) == 1:
        cls_label = f"NLCD {classes_sorted[0]}"
    else:
        cls_label = f"NLCD [{','.join(str(c) for c in classes_sorted)}]"
    name = f"Landcover mask -- {cls_label}"

    return LayerURI(
        layer_id=layer_id,
        name=name,
        layer_type="raster",
        uri=result.uri,
        style={"kind": "classed", "label": "Land Cover"},
        role="context",
        units=None,
        bbox=bbox_rounded if bbox_rounded is not None else None,
    )
