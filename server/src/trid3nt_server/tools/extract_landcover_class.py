"""Atomic tool ``extract_landcover_class`` — NLCD binary-mask extractor (job-0094, FR-TA-2, FR-CE-8, FR-DC).

This module registers one atomic tool that filters an NLCD landcover raster to a
set of requested class codes and returns a binary mask raster:

    ``extract_landcover_class(landcover_uri, classes, bbox=None) → LayerURI``

The result is a single-band uint8 GeoTIFF where pixels matching any of the
requested NLCD class codes are 1, other valid pixels are 0, and nodata pixels
are preserved as 255. The output is LZW-compressed and is stored under the
FR-DC-3 cache shim at:

    ``s3://trid3nt-cache/cache/static-30d/landcover_class/<key>.tif``

**Typical use cases:**

- Building a "water" mask from NLCD class 11 (Open Water) for zonal statistics
  inside ``compute_zonal_statistics`` (use the mask as ``zone_input``).
- Forest extent mask from NLCD classes 41 (Deciduous), 42 (Evergreen),
  43 (Mixed) for habitat / fuel-availability analysis.
- Developed extent mask from NLCD classes 21-24 for exposure analysis.

**NLCD class codes (most common, NLCD 2021 / Annual NLCD Collection 1.0):**

- 11 = Open Water
- 12 = Perennial Ice/Snow
- 21-24 = Developed (Open, Low, Medium, High intensity)
- 31 = Barren Land
- 41-43 = Forest (Deciduous, Evergreen, Mixed)
- 52 = Shrub/Scrub
- 71-74 = Herbaceous (Grassland, Sedge, Lichens, Moss)
- 81-82 = Planted/Cultivated (Pasture, Cropland)
- 90-95 = Wetlands (Woody, Emergent Herbaceous)

**bbox window-read:** when ``bbox`` is provided, the tool reads only the window
of the source raster that intersects the bbox (rasterio ``window=from_bounds``).
This avoids loading large national rasters into memory when only a small AOI is
needed. The output raster covers exactly the bbox (clipped to the source
extent); when ``bbox`` is None the entire source raster is processed.

**Cache key** is derived from ``(landcover_uri, sorted(classes), bbox_rounded_6dp,
year="2021")`` — all four parameters materially affect the output pixels. The
``year`` tag pins to NLCD 2021 (the default vintage for ``fetch_landcover``)
and is reserved for a future-vintage opt-in.

**Cross-cutting invariants:**

- **Invariant 1 (Determinism boundary): preserves.** Tool returns a typed
  ``LayerURI`` with provenance metadata; no LLM-generated numbers.
- **Invariant 2 (Deterministic workflows): preserves.** Pure rasterio + numpy
  pipeline, no LLM calls, deterministic given inputs.
- **FR-DC-6 (cacheable): honors.** ``cacheable=True``, ``ttl_class="static-30d"``,
  ``source_class="landcover_class"`` — a binary mask of a static NLCD COG is
  stable for the 30-day window.
- **NFR-R-1 (resilience): preserves.** Read / parse failures surface as
  ``LandcoverClassError`` (typed; never an unhandled exception).
- **Job-0086 codified lesson (geographic correctness):** the live test asserts
  that the mask aligns with the known geography of the source raster — a pixel
  classified as Open Water (NLCD 11) must remain 1 in the mask after
  extraction, and a Developed (NLCD 21-24) pixel must become 0 if those classes
  aren't requested. The mask is verified against ``np.isin`` of the source array
  in the same projection, so an in-memory axis mirror or transform bug shows up
  as a count mismatch rather than passing silently.
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

from . import register_tool
from .cache import CACHE_BUCKET, read_through

__all__ = [
    "extract_landcover_class",
    "LandcoverClassError",
]

logger = logging.getLogger("trid3nt_server.tools.extract_landcover_class")


# ---------------------------------------------------------------------------
# Error class
# ---------------------------------------------------------------------------


class LandcoverClassError(RuntimeError):
    """Raised when extract_landcover_class cannot read / process the input.

    ``error_code`` carries a SCREAMING_SNAKE_CASE code surfaced in the
    pipeline strip (NFR-R-1 typed-error requirement).

    Codes:
    - ``CLASSES_EMPTY`` — ``classes`` argument was empty.
    - ``CLASSES_INVALID`` — a class code is out of NLCD uint8 range (0-254).
    - ``BBOX_INVALID`` — bbox is malformed (wrong arity, non-finite, degenerate).
    - ``RASTER_OPEN_FAILED`` — rasterio cannot open the landcover raster.
    - ``WINDOW_EMPTY`` — the requested bbox does not intersect the source raster.
    - ``WRITE_FAILED`` — rasterio could not write the output GeoTIFF.
    """

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
# reserved for nodata; if the caller asks to extract 255 we refuse — it would
# silently merge with nodata.
_NLCD_MAX_CLASS = 254


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_classes(classes: list[int]) -> list[int]:
    """Validate + normalize the requested class codes.

    Returns the sorted, de-duplicated class list. Raises
    ``LandcoverClassError`` if empty or out of range.
    """
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
    """Round bbox coordinates to 6 decimal places (~0.1m) for cache-key stability."""
    return tuple(round(v, 6) for v in bbox)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Source-raster opener (s3:// via boto3 stage-then-open, local paths native)
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _open_source(landcover_uri: str) -> Any:
    """Open the landcover raster with rasterio for read (context manager).

    GCP is decommissioned: ``s3://`` URIs are staged via boto3 and opened
    in-memory; local paths open natively.

    job-0305: this is a CONTEXT MANAGER (was a plain return-the-dataset
    function). For the s3:// in-memory path the prior code did
    ``MemoryFile(...).open()`` and returned the dataset, ORPHANING the
    MemoryFile — Python could GC it (freeing the /vsimem/ buffer) mid-read, so
    reads returned valid pixels PLUS uninitialized garbage. Yielding the
    dataset from inside a nested ``with MemoryFile(...)`` pins the buffer for
    the dataset's whole lifetime (the same bug + fix as the NLCD validation
    gate in sfincs_builder). The sole caller already uses ``with``.
    """
    # sprint-14-aws (job-0293b): s3:// reads via boto3 stage-then-open —
    # GDAL's /vsis3/ creds don't resolve the EC2 instance role in this env
    # (see clip modules), so we open from staged bytes in-memory. NOTE: only
    # the OPEN is wrapped in the RASTER_OPEN_FAILED try; ``yield src`` (the
    # caller's block) runs in a separate try/finally so a downstream error is
    # never mis-attributed to the open, and the MemoryFile is always closed.
    if landcover_uri.startswith("s3://"):
        from rasterio.io import MemoryFile
        from .cache import read_object_bytes_s3
        try:
            mf = MemoryFile(read_object_bytes_s3(landcover_uri))
            dataset = mf.open()
        except Exception as exc:  # noqa: BLE001
            raise LandcoverClassError(
                "RASTER_OPEN_FAILED",
                f"rasterio could not open {landcover_uri!r}: {exc}",
            ) from exc
        # ``with mf`` pins the /vsimem/ buffer; ``dataset`` closes on exit. The
        # yield is OUTSIDE the open's try/except so a caller-side error is never
        # mis-attributed to the open.
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
    """Read the source raster (optionally windowed), build the binary mask,
    write LZW-compressed COG-style GeoTIFF, return its bytes.
    """
    with _open_source(landcover_uri) as src:
        src_nodata = src.nodata
        src_crs = src.crs

        # Resolve the read window.
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

            # Clip the requested window to the source extent. rasterio's
            # ``Window`` does not provide an "intersection" helper but
            # ``round_lengths`` + clipping the offsets does the right thing.
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
        # raster is too small for tiling — we accept that fallback.
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
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """NLCD landcover-class binary mask extractor.

    Reads an NLCD landcover GeoTIFF (typical: USGS NLCD 2021 CONUS, or any
    NLCD-coded raster produced by ``fetch_landcover``), filters to the requested
    integer class codes, and returns a binary raster mask: ``1`` where any pixel
    matches one of the requested classes, ``0`` for other valid pixels, ``255``
    preserved as nodata. The result is a single-band uint8 LZW-compressed
    GeoTIFF in the FR-DC cache and is map-renderable as well as suitable as a
    ``zone_input`` for ``compute_zonal_statistics``.

    Use this when: the agent has an NLCD landcover raster (e.g. from
    ``fetch_landcover``) and needs a single-class or multi-class binary mask
    to drive downstream analysis — examples include:

      - Building a "water" mask from class 11 to compute zonal statistics of
        flood depth over open-water pixels only.
      - A forest mask from classes 41/42/43 for habitat / fuel-availability
        analysis.
      - A developed mask from classes 21-24 for population / building
        exposure.
      - A wetland mask from classes 90/95 for tidal-flood inundation analysis.

    Do NOT use this for: extracting from non-NLCD landcover (ESA WorldCover
    has different class codes — see OQ-0094-WORLDCOVER for the future
    extension); RGB / palette-indexed NLCD displays (use ``fetch_landcover``
    with the WCS path so canonical NLCD codes are returned, not palette
    indices — see job-0044's OQ-42-NLCD-WMS-PALETTE-ENCODING); per-pixel
    statistics across multiple classes simultaneously (use
    ``compute_zonal_statistics`` directly on the NLCD raster with the
    landcover raster as the value layer).

    Params:
        landcover_uri: source NLCD GeoTIFF URI — ``gs://`` GCS path or
            absolute local file path. Must be NLCD-coded (canonical class
            integers, not palette indices). The output of
            ``fetch_landcover(...)["raster_uri"]`` satisfies this.
        classes: list of NLCD integer class codes to extract. At least one
            code required. Most common (NLCD 2021):

              11 = Open Water
              21,22,23,24 = Developed (Open, Low, Medium, High intensity)
              31 = Barren Land
              41,42,43 = Forest (Deciduous, Evergreen, Mixed)
              52 = Shrub/Scrub
              71-74 = Herbaceous
              81,82 = Pasture / Cropland
              90,95 = Wetlands (Woody, Emergent Herbaceous)

            Duplicates are de-duplicated; order does not affect the output.
            ``255`` is rejected (reserved for the output nodata sentinel).
        bbox: optional ``(min_lon, min_lat, max_lon, max_lat)`` clip window in
            the source raster's CRS units (typically EPSG:4326 lon/lat).
            When provided, only the windowed slice is read — the output covers
            only the bbox-intersection with the source raster. When ``None``
            (default), the entire source raster is processed.

    Returns:
        A ``LayerURI`` pointing at the binary-mask GeoTIFF in the cache
        bucket:
        ``s3://trid3nt-cache/cache/static-30d/landcover_class/<key>.tif``
        - ``layer_type="raster"``
        - ``role="context"`` (mask is contextual, not a primary hazard layer)
        - ``style_preset="categorical_landcover"`` for client rendering
        - ``units=None`` (binary mask is unitless)

    LLM guidance:
        - When a question mentions a specific landcover term ("water",
          "forest", "wetland", "developed", "cropland"), map it to the NLCD
          code(s) above and pass the integer list — do not invent new codes.
        - Multi-class extracts (e.g. all forest classes) collapse to one
          binary mask: pass ``[41, 42, 43]``, not three separate calls.
        - The output is appropriate as a ``zone_input`` in
          ``compute_zonal_statistics`` to compute "value-in-class" aggregates.

    FR-CE-8: Results are routed through ``read_through`` so repeat calls with
    the same ``(landcover_uri, classes, bbox)`` triple reuse the cached mask
    without re-reading or re-computing. TTL is 30 days (NLCD vintages are
    stable for years).

    Raises:
        LandcoverClassError: with a typed ``error_code`` if classes is empty
            or out of range, the bbox is malformed, the source raster
            cannot be opened, the bbox does not intersect the source, or
            writing the output GeoTIFF fails.
    """
    effective_bucket = _bucket or CACHE_BUCKET

    # Argument validation up-front so bad calls fail BEFORE the cache lookup.
    classes_sorted = _validate_classes(classes)
    _validate_bbox(bbox)

    # Quantize bbox to 6dp for cache-key stability (cache.py convention).
    bbox_rounded = _round_bbox(bbox) if bbox is not None else None

    # Cache key on (landcover_uri, sorted classes, bbox, year tag). The year
    # tag pins to NLCD 2021 — when the engine bumps the default vintage the
    # cache key naturally changes, avoiding silent staleness.
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

    # Build a stable, readable layer_id.
    src_key = landcover_uri.rstrip("/").rsplit("/", 1)[-1].replace(".tif", "")
    classes_tag = "-".join(str(c) for c in classes_sorted)
    layer_id = f"landcover-class-{src_key}-{classes_tag}"

    # Name surfaces in the LayerPanel; keep it short but informative.
    if len(classes_sorted) == 1:
        cls_label = f"NLCD {classes_sorted[0]}"
    else:
        cls_label = f"NLCD [{','.join(str(c) for c in classes_sorted)}]"
    name = f"Landcover mask — {cls_label}"

    return LayerURI(
        layer_id=layer_id,
        name=name,
        layer_type="raster",
        uri=result.uri,
        style_preset="categorical_landcover",
        role="context",
        units=None,
        bbox=bbox_rounded if bbox_rounded is not None else None,
    )
