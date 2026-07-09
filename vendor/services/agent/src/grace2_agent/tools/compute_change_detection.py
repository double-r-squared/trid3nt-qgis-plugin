"""``compute_change_detection`` composer tool -- two-date Sentinel-2 index change.

Detects surface change between two dates over a bbox by differencing a
spectral index computed from Sentinel-2 L2A surface reflectance:

    index = NDVI = (NIR - Red) / (NIR + Red)          (default; vegetation)
    index = NDWI = (Green - NIR) / (Green + NIR)      (``index="ndwi"``; water)

    delta = index(date_b) - index(date_a)

Pixels where ``|delta| >= threshold`` (default 0.15) are classified as

    gain  -- delta >= +threshold  (greening / water gain)
    loss  -- delta <= -threshold  (vegetation loss / water loss)

and vectorized to change polygons (``rasterio.features.shapes``), returned as
a FlatGeobuf vector layer styled by gain/loss (a categorical ``LegendKey`` on
the per-feature ``change`` property) with per-class counts + areas riding on a
``ChangeDetectionLayerURI`` (the ``DebrisFlowLayerURI`` /
``SedimentYieldLayerURI`` house side-channel pattern).

Data source
===========

Sentinel-2 L2A via the Microsoft Planetary Computer STAC -- the EXACT search /
sign / windowed-read helpers ``compute_ndvi`` and ``fetch_sentinel2_truecolor``
use (``_pc_stac.search_least_cloudy_item`` + ``sas_sign_href`` +
``bbox_pixel_dims``; one least-cloudy scene per date window). Bands:

    ndvi: B04 (Red, 10 m) + B08 (NIR, 10 m)
    ndwi: B03 (Green, 10 m) + B08 (NIR, 10 m)

Offline / precomputed path: ``imagery_a_uri`` / ``imagery_b_uri`` accept
ALREADY-COMPUTED single-band index rasters (s3:// or local GeoTIFF). When both
are supplied no STAC call is made; raster B is resampled onto raster A's grid
(bilinear) before differencing. This is the offline-test seam and also lets a
caller difference indices produced elsewhere (e.g. two ``compute_ndvi`` runs).

Honesty (data-source fallback norm)
===================================

- No scene for a date window: typed ``ChangeDetectionNoImageryError``.
- Both dates read but NO pixel crosses the threshold: typed
  ``ChangeDetectionNoChangeError`` -- an honest "no significant change"
  narration, never an empty layer that reads as success.

AOI clamp: <= 0.2 degrees per side (CPU-bounded two-scene read + vectorize).

``cacheable=False`` (``ttl_class="live-no-cache"``): this is a modeling
composer, not a fetcher -- the artifact goes to the runs bucket (or
``_output_dir`` for offline tests), not the cache prefix.
"""

from __future__ import annotations

import logging
import math
import os
import tempfile
import uuid
from typing import Any

import numpy as np

from grace2_contracts.execution import LayerURI, LegendClass, LegendKey
from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from . import _pc_stac

__all__ = [
    "compute_change_detection",
    "ChangeDetectionLayerURI",
    "ChangeDetectionError",
    "ChangeDetectionInputError",
    "ChangeDetectionAoiTooLargeError",
    "ChangeDetectionNoImageryError",
    "ChangeDetectionNoChangeError",
    "ChangeDetectionUpstreamError",
    "CHANGE_CLASSES",
]

logger = logging.getLogger("grace2_agent.tools.compute_change_detection")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class ChangeDetectionError(RuntimeError):
    """Base class for compute_change_detection failures."""

    error_code: str = "CHANGE_DETECTION_ERROR"
    retryable: bool = True


class ChangeDetectionInputError(ChangeDetectionError):
    """Bad inputs (malformed bbox, bad threshold/index/dates, unreadable URI)."""

    error_code = "CHANGE_DETECTION_INPUT_INVALID"
    retryable = False


class ChangeDetectionAoiTooLargeError(ChangeDetectionInputError):
    """The AOI exceeds the CPU-bound clamp (> 0.2 degrees per side)."""

    error_code = "CHANGE_DETECTION_AOI_TOO_LARGE"
    retryable = False


class ChangeDetectionNoImageryError(ChangeDetectionError):
    """No Sentinel-2 scene covers the bbox in a date window (honest miss)."""

    error_code = "CHANGE_DETECTION_NO_IMAGERY"
    retryable = False


class ChangeDetectionNoChangeError(ChangeDetectionError):
    """Both dates were read but no pixel crosses the change threshold.

    Honest no-change signal -- the agent narrates "no significant change
    detected" rather than emitting an empty layer that reads as success.
    """

    error_code = "CHANGE_DETECTION_NO_CHANGE"
    retryable = False


class ChangeDetectionUpstreamError(ChangeDetectionError):
    """A STAC search / band read / vectorize / artifact write failed."""

    error_code = "CHANGE_DETECTION_UPSTREAM_ERROR"
    retryable = True


# ---------------------------------------------------------------------------
# Result type -- LayerURI subclass carrying the summary (house side-channel).
# ---------------------------------------------------------------------------


class ChangeDetectionLayerURI(LayerURI):
    """The change-polygon ``LayerURI`` plus assessment summary.

    Extra fields beyond ``LayerURI``:

    - ``gain_count`` / ``loss_count`` -- change polygons per class.
    - ``gain_area_m2`` / ``loss_area_m2`` -- total area per class.
    - ``index`` -- the spectral index differenced (``"ndvi"`` / ``"ndwi"``).
    - ``threshold`` -- the |delta| threshold actually used.
    - ``scene_a_id`` / ``scene_b_id`` -- Sentinel-2 scene ids (None on the
      precomputed ``imagery_*_uri`` path).
    - ``notes`` -- honest provenance + every fallback/simplification used.
    """

    gain_count: int = 0
    loss_count: int = 0
    gain_area_m2: float = 0.0
    loss_area_m2: float = 0.0
    index: str = "ndvi"
    threshold: float = 0.15
    scene_a_id: str | None = None
    scene_b_id: str | None = None
    notes: list[str] = []


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

_COLLECTION = "sentinel-2-l2a"
_NATIVE_CELL_M = 10.0

#: Bands per index: (band_1, band_2) with index = (b1 - b2) / (b1 + b2).
#: ndvi = (NIR - Red) / (NIR + Red); ndwi = (Green - NIR) / (Green + NIR).
_INDEX_BANDS: dict[str, tuple[str, str]] = {
    "ndvi": ("B08", "B04"),
    "ndwi": ("B03", "B08"),
}

#: Default |delta| threshold for a "real" change (index units, -2..2 range).
_DEFAULT_THRESHOLD = 0.15

#: Default cloud-cover ceiling (percent) per date-window scene search.
_DEFAULT_MAX_CLOUD = 30.0

#: Default minimum change-polygon area (m^2): drops single/few-pixel index
#: specks (one Sentinel-2 pixel is ~100 m^2; 1000 m^2 is ~10 pixels).
_DEFAULT_MIN_AREA_M2 = 1000.0

#: CPU-bound AOI clamp (degrees per side): two scenes are read + differenced
#: + vectorized in-process on the agent box.
_MAX_AOI_DEG = 0.2

#: Gain/loss render classes: (value, "#rrggbb", label). The categorical
#: ``LegendKey`` (value_field="change") is built from this SAME table so the
#: key always matches the paint (the data-driven vector legend seam).
CHANGE_CLASSES: tuple[tuple[str, str, str], ...] = (
    ("gain", "#1a9850", "Gain (index increase)"),
    ("loss", "#d73027", "Loss (index decrease)"),
)

_STYLE_PRESET = "change_detection"

_METADATA = AtomicToolMetadata(
    name="compute_change_detection",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
)


# ---------------------------------------------------------------------------
# Validation helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: Any) -> tuple[float, float, float, float]:
    if not isinstance(bbox, (tuple, list)) or len(bbox) != 4:
        raise ChangeDetectionInputError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    try:
        west, south, east, north = (float(v) for v in bbox)
    except (TypeError, ValueError) as exc:
        raise ChangeDetectionInputError(
            f"bbox contains non-numeric values: {bbox!r}"
        ) from exc
    if not all(math.isfinite(v) for v in (west, south, east, north)):
        raise ChangeDetectionInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= west <= 180.0 and -180.0 <= east <= 180.0):
        raise ChangeDetectionInputError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= south <= 90.0 and -90.0 <= north <= 90.0):
        raise ChangeDetectionInputError(f"bbox lat out of [-90,90]: {bbox!r}")
    if west >= east or south >= north:
        raise ChangeDetectionInputError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )
    if (east - west) > _MAX_AOI_DEG or (north - south) > _MAX_AOI_DEG:
        raise ChangeDetectionAoiTooLargeError(
            f"AOI {bbox!r} exceeds the compute_change_detection clamp of "
            f"{_MAX_AOI_DEG} degrees per side "
            f"(got {east - west:.3f} x {north - south:.3f} deg). Two 10 m "
            "Sentinel-2 scenes are read + differenced in-process; pick a "
            "site-scale AOI."
        )
    return (west, south, east, north)


def _validate_index(index: Any) -> str:
    idx = str(index or "ndvi").strip().lower()
    if idx not in _INDEX_BANDS:
        raise ChangeDetectionInputError(
            f"index must be one of {sorted(_INDEX_BANDS)}; got {index!r}"
        )
    return idx


def _validate_threshold(threshold: Any) -> float:
    try:
        thr = float(threshold)
    except (TypeError, ValueError) as exc:
        raise ChangeDetectionInputError(
            f"threshold must be numeric; got {threshold!r}"
        ) from exc
    if not math.isfinite(thr) or not (0.0 < thr <= 2.0):
        raise ChangeDetectionInputError(
            f"threshold must be a finite value in (0, 2]; got {threshold!r}"
        )
    return thr


# ---------------------------------------------------------------------------
# Input staging (mirrors compute_sediment_yield).
# ---------------------------------------------------------------------------


def _stage_uri_local(uri: str, tmpdir: str, label: str) -> str:
    """Return a local file path for ``uri`` (s3:// download or local path)."""
    if uri.startswith("s3://"):
        from .cache import read_object_bytes_s3

        name = uri.rstrip("/").rsplit("/", 1)[-1] or f"{label}.bin"
        local = os.path.join(tmpdir, f"{label}_{name}")
        try:
            data = read_object_bytes_s3(uri)
        except Exception as exc:  # noqa: BLE001
            raise ChangeDetectionUpstreamError(
                f"S3 download failed for {label} uri {uri!r}: {exc}"
            ) from exc
        with open(local, "wb") as f:
            f.write(data)
        return local
    if uri.startswith(("gs://", "http://", "https://")):
        raise ChangeDetectionInputError(
            f"{label} uri scheme not supported: {uri!r} (use s3:// or a local path)"
        )
    if not os.path.exists(uri):
        raise ChangeDetectionInputError(
            f"{label} uri points at a missing local file: {uri!r}"
        )
    return uri


# ---------------------------------------------------------------------------
# Index computation -- fetched (PC STAC) and precomputed paths.
# ---------------------------------------------------------------------------


def _read_band_window(
    signed_href: str,
    bbox: tuple[float, float, float, float],
    width_px: int,
    height_px: int,
) -> Any:
    """Read ``signed_href`` warped to EPSG:4326 windowed to ``bbox``.

    Returns a 2-D float32 numpy masked array -- the exact read path
    ``compute_ndvi`` / ``digitize_water_body`` use.
    """
    import rasterio
    from rasterio.warp import Resampling, reproject

    vsicurl = "/vsicurl/" + signed_href
    try:
        with rasterio.Env(**_pc_stac.VSICURL_ENV_KW):
            with rasterio.open(vsicurl) as src:
                dst_transform = rasterio.transform.from_bounds(
                    bbox[0], bbox[1], bbox[2], bbox[3], width_px, height_px
                )
                dst = np.zeros((height_px, width_px), dtype="float32")
                reproject(
                    source=rasterio.band(src, 1),
                    destination=dst,
                    src_transform=src.transform,
                    src_crs=src.crs,
                    dst_transform=dst_transform,
                    dst_crs="EPSG:4326",
                    resampling=Resampling.bilinear,
                    src_nodata=src.nodata if src.nodata is not None else 0,
                    dst_nodata=0,
                )
        return np.ma.masked_equal(dst.astype("float32"), 0.0)
    except ChangeDetectionError:
        raise
    except Exception as exc:  # noqa: BLE001 -- translate any rasterio/GDAL error
        raise ChangeDetectionUpstreamError(
            f"Sentinel-2 band read failed (href={signed_href[:120]!r}): {exc}"
        ) from exc


def _fetch_index_for_window(
    bbox: tuple[float, float, float, float],
    datetime_range: str,
    index: str,
    max_cloud_cover: float,
    width_px: int,
    height_px: int,
    label: str,
) -> tuple[np.ma.MaskedArray, str]:
    """Search PC STAC for the least-cloudy scene in the window; return the
    masked index array on the shared bbox grid + the scene id."""
    try:
        item = _pc_stac.search_least_cloudy_item(
            collection=_COLLECTION,
            bbox=bbox,
            datetime_range=datetime_range,
            max_cloud_cover=max_cloud_cover,
            sort_by_cloud=True,
        )
    except _pc_stac.PCStacNoItemsError as exc:
        raise ChangeDetectionNoImageryError(
            f"no Sentinel-2 imagery for {label} window {datetime_range} over "
            f"bbox={bbox} under {max_cloud_cover}% cloud cover: {exc}"
        ) from exc
    except _pc_stac.PCStacError as exc:
        raise ChangeDetectionUpstreamError(
            f"Sentinel-2 STAC search failed for {label} window: {exc}"
        ) from exc

    b1_key, b2_key = _INDEX_BANDS[index]
    assets = getattr(item, "assets", {}) or {}
    if b1_key not in assets or b2_key not in assets:
        raise ChangeDetectionUpstreamError(
            f"Sentinel-2 item {getattr(item, 'id', '?')} missing "
            f"{b1_key}/{b2_key} assets (have {sorted(assets)[:8]})"
        )

    b1_href = _pc_stac.sas_sign_href(assets[b1_key].href, _COLLECTION)
    b2_href = _pc_stac.sas_sign_href(assets[b2_key].href, _COLLECTION)
    b1 = _read_band_window(b1_href, bbox, width_px, height_px)
    b2 = _read_band_window(b2_href, bbox, width_px, height_px)

    b1_f = b1.astype("float32")
    b2_f = b2.astype("float32")
    denom = b1_f + b2_f
    with np.errstate(divide="ignore", invalid="ignore"):
        idx = (b1_f - b2_f) / denom
    idx = np.ma.masked_invalid(idx)
    idx = np.ma.masked_where(np.ma.getmaskarray(b1) | np.ma.getmaskarray(b2), idx)
    idx = np.ma.masked_where(np.abs(denom) < 1e-6, idx)
    if idx.count() == 0:
        raise ChangeDetectionNoImageryError(
            f"Sentinel-2 scene {getattr(item, 'id', '?')} produced an all-nodata "
            f"{index.upper()} over bbox={bbox} for the {label} window (scene does "
            "not actually cover the AOI)."
        )
    return idx, str(getattr(item, "id", "?"))


def _open_index_raster(path: str, label: str) -> tuple[np.ndarray, Any]:
    """Open band 1 of a precomputed index raster as float64 with nodata->NaN."""
    try:
        import rasterio
    except ImportError as exc:
        raise ChangeDetectionUpstreamError(f"rasterio unavailable: {exc}") from exc
    try:
        src = rasterio.open(path)
    except Exception as exc:  # noqa: BLE001
        raise ChangeDetectionInputError(
            f"could not open {label} index raster {path!r}: {exc}"
        ) from exc
    band = src.read(1).astype(np.float64)
    nodata = src.nodata
    if nodata is not None and math.isfinite(float(nodata)):
        band[band == float(nodata)] = np.nan
    return band, src


def _resample_onto(
    path: str, label: str, ref_src: Any
) -> np.ndarray:
    """Reproject/resample ``path`` band 1 onto ``ref_src``'s grid (bilinear).

    An input already on the exact reference grid passes through
    value-identical (the compute_sediment_yield same-grid shortcut).
    """
    import rasterio  # noqa: F401
    from rasterio.warp import Resampling, reproject

    band, src = _open_index_raster(path, label)
    try:
        same_grid = (
            src.crs == ref_src.crs
            and src.transform == ref_src.transform
            and src.shape == ref_src.shape
        )
        if same_grid:
            return band
        out = np.full(ref_src.shape, np.nan, dtype=np.float64)
        reproject(
            source=band,
            destination=out,
            src_transform=src.transform,
            src_crs=src.crs,
            dst_transform=ref_src.transform,
            dst_crs=ref_src.crs,
            src_nodata=np.nan,
            dst_nodata=np.nan,
            resampling=Resampling.bilinear,
        )
        return out
    except ChangeDetectionError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ChangeDetectionUpstreamError(
            f"resampling {label} index raster onto the date-A grid failed: {exc}"
        ) from exc
    finally:
        src.close()


# ---------------------------------------------------------------------------
# Vectorize + write.
# ---------------------------------------------------------------------------


def _vectorize_change(
    delta: np.ndarray,
    valid: np.ndarray,
    threshold: float,
    transform: Any,
    crs: Any,
    min_area_m2: float,
    bbox: tuple[float, float, float, float],
) -> Any:
    """Threshold ``delta`` into gain/loss polygons; return an EPSG:4326 gdf."""
    import rasterio
    from rasterio import features

    classified = np.zeros(delta.shape, dtype=np.uint8)
    classified[valid & (delta >= threshold)] = 1  # gain
    classified[valid & (delta <= -threshold)] = 2  # loss
    changed_px = int((classified > 0).sum())
    if changed_px == 0:
        raise ChangeDetectionNoChangeError(
            f"no pixel crosses |delta| >= {threshold} between the two dates over "
            f"bbox={bbox} (valid_px={int(valid.sum())}). No significant change "
            "detected; lower the threshold to surface subtler change."
        )

    try:
        from shapely.geometry import shape
        import geopandas as gpd

        records: list[dict[str, Any]] = []
        geoms: list[Any] = []
        for geom, val in features.shapes(
            classified, mask=classified > 0, transform=transform
        ):
            change = "gain" if int(val) == 1 else "loss"
            geoms.append(shape(geom))
            records.append({"change": change})
        gdf = gpd.GeoDataFrame(records, geometry=geoms, crs=crs)
    except ChangeDetectionError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ChangeDetectionUpstreamError(
            f"change-mask vectorization failed for bbox={bbox}: {exc}"
        ) from exc

    # Area in m^2 via Web-Mercator (adequate for a speck filter at AOI scale;
    # the digitize_water_body convention).
    gdf["area_m2"] = gdf.geometry.to_crs(3857).area
    if min_area_m2 > 0.0:
        gdf = gdf[gdf["area_m2"] >= min_area_m2].copy()
    if len(gdf) == 0:
        raise ChangeDetectionNoChangeError(
            f"all change polygons over bbox={bbox} were smaller than "
            f"min_area_m2={min_area_m2} m^2 (only index specks; no mappable "
            "change). Lower min_area_m2 to keep small patches."
        )
    if str(gdf.crs).upper() not in ("EPSG:4326",):
        gdf = gdf.to_crs(4326)
    return gdf


def _write_fgb_bytes(gdf: Any, tmpdir: str) -> bytes:
    path = os.path.join(tmpdir, "change_polygons.fgb")
    try:
        gdf.to_file(path, driver="FlatGeobuf", engine="pyogrio")
    except Exception as exc:  # noqa: BLE001
        raise ChangeDetectionUpstreamError(
            f"change-polygon FlatGeobuf write failed: {exc}"
        ) from exc
    with open(path, "rb") as f:
        return f.read()


def _write_output(payload: bytes, seed: str, output_dir: str | None) -> str:
    """Persist the FGB; return its URI (local for tests, runs bucket live)."""
    filename = f"change_detection_{seed}.fgb"
    if output_dir is not None:
        path = os.path.join(output_dir, filename)
        with open(path, "wb") as f:
            f.write(payload)
        return path
    try:
        from .solver import _get_runs_bucket, _get_s3_client

        bucket = _get_runs_bucket()
        key = f"change-detection-{seed}/{filename}"
        _get_s3_client().put_object(
            Bucket=bucket,
            Key=key,
            Body=payload,
            ContentType="application/octet-stream",
        )
        return f"s3://{bucket}/{key}"
    except Exception as exc:  # noqa: BLE001
        raise ChangeDetectionUpstreamError(
            f"failed to upload the change-polygon FGB to the runs bucket: {exc}"
        ) from exc


def _build_legend(index: str) -> LegendKey:
    """Categorical gain/loss legend built from the SAME class table the paint
    uses (value_field drives the client's data-driven vector fill)."""
    return LegendKey(
        kind="categorical",
        classes=[
            LegendClass(value=value, color=color, label=label)
            for value, color, label in CHANGE_CLASSES
        ],
        value_field="change",
        label=f"{index.upper()} change",
    )


# ---------------------------------------------------------------------------
# Registered tool.
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    # Annotations: fetches its own Sentinel-2 inputs (external PC STAC API)
    # unless both imagery_*_uri overrides are passed -- an input-fetching
    # composer like compute_sediment_yield, so open_world_hint=True is honest
    # (listed in test_tool_annotations._OPEN_WORLD_COMPUTE_EXCEPTIONS).
    open_world_hint=True,
)
def compute_change_detection(
    bbox: tuple[float, float, float, float],
    date_a_start: str | None = None,
    date_a_end: str | None = None,
    date_b_start: str | None = None,
    date_b_end: str | None = None,
    index: str = "ndvi",
    threshold: float = _DEFAULT_THRESHOLD,
    max_cloud_cover: float = _DEFAULT_MAX_CLOUD,
    min_area_m2: float = _DEFAULT_MIN_AREA_M2,
    imagery_a_uri: str | None = None,
    imagery_b_uri: str | None = None,
    *,
    _output_dir: str | None = None,
    # job-0164: absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> ChangeDetectionLayerURI:
    """Detect surface change between two dates by differencing Sentinel-2 NDVI/NDWI.

    Use this (not compute_ndvi, which is one date) when you want the DIFFERENCE between two dates.

    **What it does:** Fetches the least-cloudy Sentinel-2 L2A scene for EACH of
    two date windows over ``bbox`` (Microsoft Planetary Computer STAC -- the
    same search path as ``compute_ndvi``), computes the spectral index per date
    (NDVI by default; NDWI when ``index="ndwi"``), differences them
    (``delta = date_b - date_a``), thresholds ``|delta| >= threshold`` (default
    0.15) into gain / loss classes, vectorizes to change polygons, and returns
    a FlatGeobuf vector layer styled by gain (green) / loss (red) with
    per-class counts + areas.

    **When to use:**
    - "What changed here between 2020 and 2024?" -- deforestation / clearing,
      vegetation regrowth, crop conversion (``index="ndvi"``).
    - Reservoir draw-down / new open water / flood-scar mapping
      (``index="ndwi"``).
    - Post-event before/after screening (fire scar, storm blowdown) when a
      categorical change footprint is wanted rather than two rasters.

    **When NOT to use:**
    - A single-date vegetation map -- use ``compute_ndvi``.
    - Land-cover CLASS transitions (forest -> urban) -- compare
      ``fetch_esri_landcover_10m`` years instead; this tool differences a
      continuous index, not classes.
    - Sub-annual crop phenology: two dates in different seasons will read as
      "change" -- compare same-season windows.

    **Parameters:**
    - ``bbox``: ``(min_lon, min_lat, max_lon, max_lat)`` EPSG:4326, clamped to
      <= 0.2 degrees per side.
    - ``date_a_start`` / ``date_a_end``: ``"YYYY-MM-DD"`` window for the
      BEFORE date (required unless both ``imagery_*_uri`` are supplied).
    - ``date_b_start`` / ``date_b_end``: window for the AFTER date (same rule).
    - ``index``: ``"ndvi"`` (default, vegetation) or ``"ndwi"`` (open water).
    - ``threshold``: |delta| cutoff for a real change (default 0.15).
    - ``max_cloud_cover``: per-window scene cloud ceiling (default 30.0).
    - ``min_area_m2``: drop change polygons smaller than this (default 1000).
    - ``imagery_a_uri`` / ``imagery_b_uri``: PRECOMPUTED single-band index
      rasters (s3:// or local GeoTIFF). When both are given no STAC call is
      made; B is resampled onto A's grid before differencing.

    **Returns:** ``ChangeDetectionLayerURI`` -- a vector ``LayerURI``
    (FlatGeobuf, EPSG:4326; each feature carries ``change`` in
    ``{"gain","loss"}`` + ``area_m2``) with a categorical gain/loss legend
    (``value_field="change"``) plus ``gain_count`` / ``loss_count`` /
    ``gain_area_m2`` / ``loss_area_m2`` / ``scene_a_id`` / ``scene_b_id`` and
    honest ``notes``.

    **Errors (FR-AS-11):** ``ChangeDetectionNoImageryError`` (no scene in a
    window), ``ChangeDetectionNoChangeError`` (nothing crosses the threshold
    -- an HONEST no-change result, never an empty layer),
    ``ChangeDetectionAoiTooLargeError`` / ``ChangeDetectionInputError`` /
    ``ChangeDetectionUpstreamError``.
    """
    q_bbox = _validate_bbox(bbox)
    idx_name = _validate_index(index)
    thr = _validate_threshold(threshold)
    try:
        min_area = float(min_area_m2)
    except (TypeError, ValueError):
        min_area = _DEFAULT_MIN_AREA_M2
    if not math.isfinite(min_area) or min_area < 0.0:
        raise ChangeDetectionInputError(
            f"min_area_m2 must be a finite value >= 0; got {min_area_m2!r}"
        )

    notes: list[str] = []
    scene_a_id: str | None = None
    scene_b_id: str | None = None

    precomputed = imagery_a_uri is not None or imagery_b_uri is not None
    if precomputed and not (imagery_a_uri and imagery_b_uri):
        raise ChangeDetectionInputError(
            "imagery_a_uri and imagery_b_uri must BOTH be supplied on the "
            "precomputed-index path (got only one)."
        )

    with tempfile.TemporaryDirectory(prefix="grace2_change_") as tmpdir:
        if precomputed:
            # ---- Precomputed-index path (offline tests / external indices).
            import rasterio  # noqa: F401

            a_local = _stage_uri_local(imagery_a_uri, tmpdir, "index_a")
            b_local = _stage_uri_local(imagery_b_uri, tmpdir, "index_b")
            a_band, a_src = _open_index_raster(a_local, "imagery_a")
            try:
                b_band = _resample_onto(b_local, "imagery_b", a_src)
                valid = np.isfinite(a_band) & np.isfinite(b_band)
                if not valid.any():
                    raise ChangeDetectionInputError(
                        "the two precomputed index rasters share no valid "
                        "overlapping cells."
                    )
                delta = np.where(valid, b_band - a_band, np.nan)
                transform, crs = a_src.transform, a_src.crs
            finally:
                a_src.close()
            notes.append(
                f"Indices from caller-supplied precomputed rasters "
                f"(imagery_a_uri={imagery_a_uri}, imagery_b_uri={imagery_b_uri}); "
                "raster B resampled onto raster A's grid (bilinear). No "
                "Sentinel-2 fetch performed."
            )
        else:
            # ---- Fetched path (PC STAC, one scene per date window).
            if not (date_a_start and date_a_end and date_b_start and date_b_end):
                raise ChangeDetectionInputError(
                    "date_a_start/date_a_end and date_b_start/date_b_end are all "
                    "required when imagery_a_uri/imagery_b_uri are not supplied."
                )
            import rasterio

            window_a = f"{date_a_start}/{date_a_end}"
            window_b = f"{date_b_start}/{date_b_end}"
            try:
                max_cc = float(max_cloud_cover)
            except (TypeError, ValueError):
                max_cc = _DEFAULT_MAX_CLOUD
            width_px, height_px = _pc_stac.bbox_pixel_dims(q_bbox, _NATIVE_CELL_M)
            idx_a, scene_a_id = _fetch_index_for_window(
                q_bbox, window_a, idx_name, max_cc, width_px, height_px, "date_a"
            )
            idx_b, scene_b_id = _fetch_index_for_window(
                q_bbox, window_b, idx_name, max_cc, width_px, height_px, "date_b"
            )
            both_valid = ~(np.ma.getmaskarray(idx_a) | np.ma.getmaskarray(idx_b))
            if not both_valid.any():
                raise ChangeDetectionNoImageryError(
                    f"the {window_a} and {window_b} scenes share no valid "
                    f"overlapping pixels over bbox={q_bbox}."
                )
            delta = np.where(
                both_valid,
                idx_b.filled(np.nan) - idx_a.filled(np.nan),
                np.nan,
            )
            valid = both_valid
            transform = rasterio.transform.from_bounds(
                q_bbox[0], q_bbox[1], q_bbox[2], q_bbox[3], width_px, height_px
            )
            crs = "EPSG:4326"
            notes.append(
                f"{idx_name.upper()} per date from the least-cloudy Sentinel-2 "
                f"L2A scene per window (Microsoft Planetary Computer STAC): "
                f"date_a={window_a} scene={scene_a_id}, "
                f"date_b={window_b} scene={scene_b_id}, "
                f"cloud ceiling {max_cc:g}%."
            )
            notes.append(
                "Single-scene-per-date comparison: residual cloud/shadow or "
                "seasonal phenology between the two scenes can register as "
                "change; compare same-season windows for land-change analysis."
            )

        gdf = _vectorize_change(
            delta, valid, thr, transform, crs, min_area, q_bbox
        )
        payload = _write_fgb_bytes(gdf, tmpdir)

    gain = gdf[gdf["change"] == "gain"]
    loss = gdf[gdf["change"] == "loss"]
    gain_count, loss_count = int(len(gain)), int(len(loss))
    gain_area = float(gain["area_m2"].sum()) if gain_count else 0.0
    loss_area = float(loss["area_m2"].sum()) if loss_count else 0.0

    seed = uuid.uuid4().hex[:8]
    uri = _write_output(payload, seed, _output_dir)

    logger.info(
        "compute_change_detection: bbox=%s index=%s thr=%g -> gain=%d (%.0f m^2) "
        "loss=%d (%.0f m^2) scenes=(%s, %s)",
        q_bbox,
        idx_name,
        thr,
        gain_count,
        gain_area,
        loss_count,
        loss_area,
        scene_a_id,
        scene_b_id,
    )
    return ChangeDetectionLayerURI(
        layer_id=f"change-detection-{seed}",
        name=(
            f"{idx_name.upper()} change (|delta| >= {thr:g}) -- "
            f"bbox ({q_bbox[0]:.2f},{q_bbox[1]:.2f},{q_bbox[2]:.2f},{q_bbox[3]:.2f})"
        ),
        layer_type="vector",
        uri=uri,
        style_preset=_STYLE_PRESET,
        role="primary",
        units="m^2",
        bbox=q_bbox,
        legend=_build_legend(idx_name),
        gain_count=gain_count,
        loss_count=loss_count,
        gain_area_m2=round(gain_area, 1),
        loss_area_m2=round(loss_area, 1),
        index=idx_name,
        threshold=thr,
        scene_a_id=scene_a_id,
        scene_b_id=scene_b_id,
        notes=notes,
    )
