"""Atomic tool ``clip_raster_to_polygon`` — clip a raster to an arbitrary polygon (job-0106).

Sibling to ``clip_raster_to_bbox`` (job-0085) but accepts an arbitrary vector
polygon instead of a rectangular bbox. This is the enabler for the "in [place]"
geographic-clipping pattern (per feedback-geographic-clipping-pattern memory
rule). Typical composition::

    boundaries_uri = fetch_administrative_boundaries(level='state', bbox=...)
    clipped_uri = clip_raster_to_polygon(
        precip_uri,
        boundaries_uri,
        feature_filter={"property": "name", "value": "Washington"},
    )

The result is a clipped GeoTIFF stored under the FR-DC-3 cache shim at::

    s3://trid3nt-cache/cache/static-30d/clip_raster_polygon/<key>.tif

**Implementation flow (cache miss):**

1. Detect source CRS with ``rasterio.open(raster_uri).crs``.
2. Read polygon(s) via ``geopandas.read_file`` (supports FlatGeobuf, GeoJSON,
   shapefiles, GeoParquet, etc.).
3. Apply ``feature_filter`` (property+value) to select matching features.
4. Reproject polygon geometry to raster CRS via
   ``rasterio.warp.transform_geom`` if CRS mismatched.
5. Download source raster bytes (gs:// or local), write to a temp file.
6. ``rasterio.mask.mask(raster, [polygon_geom], crop=True, nodata=...)``.
7. Write masked array back to a LZW-compressed GeoTIFF.
8. ``read_through`` writes bytes to the cache bucket.

**Cache key** is derived from ``(raster_uri, polygon_uri, feature_filter,
nodata_outside)`` — all four parameters materially affect the output pixels.

**Cross-cutting invariants:**

- **Invariant 2 (Deterministic workflows): preserves.** Zero LLM calls.
- **FR-DC-6 (cacheable): honors.** ``cacheable=True``,
  ``ttl_class="static-30d"``, ``source_class="clip_raster_polygon"`` — clip of
  a static raster + static polygon is stable.
- **NFR-R-1 (resilience): preserves.** Failures surface as
  ``ClipRasterPolygonError`` (typed, never unhandled exception).
- **CRS hygiene end-to-end:** polygon is reprojected to the raster's native
  CRS before masking; output preserves the source raster CRS.
"""

from __future__ import annotations

import logging
import os
import tempfile
from typing import Any

from grace2_contracts.execution import LayerURI
from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from .cache import CACHE_BUCKET, read_through

__all__ = [
    "clip_raster_to_polygon",
    "ClipRasterPolygonError",
]

logger = logging.getLogger("grace2_agent.tools.clip_raster_to_polygon")

# ---------------------------------------------------------------------------
# Error class
# ---------------------------------------------------------------------------


class ClipRasterPolygonError(RuntimeError):
    """Raised when polygon-clip fails or inputs cannot be fetched/opened.

    ``error_code`` carries a SCREAMING_SNAKE_CASE code surfaced in the
    pipeline strip (NFR-R-1 typed-error requirement).

    Codes:
    - ``RASTER_OPEN_FAILED`` — could not open raster_uri with rasterio.
    - ``RASTER_DOWNLOAD_FAILED`` — GCS download for raster URI failed.
    - ``UNKNOWN_RASTER_URI`` — raster_uri neither gs:// URI nor readable file.
    - ``POLYGON_OPEN_FAILED`` — could not read polygon_uri with geopandas.
    - ``POLYGON_DOWNLOAD_FAILED`` — GCS download for polygon URI failed.
    - ``UNKNOWN_POLYGON_URI`` — polygon_uri neither gs:// URI nor readable file.
    - ``POLYGON_FILTER_EMPTY`` — feature_filter matched zero features.
    - ``POLYGON_REPROJECT_FAILED`` — CRS reprojection of the polygon failed.
    - ``MASK_FAILED`` — rasterio.mask.mask raised or produced empty output.
    """

    error_code: str
    retryable: bool = True

    def __init__(self, error_code: str, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable


# ---------------------------------------------------------------------------
# Tool metadata
# ---------------------------------------------------------------------------

_METADATA = AtomicToolMetadata(
    name="clip_raster_to_polygon",
    ttl_class="static-30d",
    source_class="clip_raster_polygon",
    cacheable=True,
)


# ---------------------------------------------------------------------------
# Raster I/O helpers (mirrors clip_raster_to_bbox sibling pattern)
# ---------------------------------------------------------------------------


def _get_source_crs(raster_uri: str) -> Any:
    """Open the raster with rasterio and return its CRS.

    For ``s3://`` URIs the bytes are staged via the shared boto3 reader and
    opened in-memory.

    Raises:
        ClipRasterPolygonError: if the URI is unrecognised or rasterio cannot
            open it.
    """
    try:
        import rasterio  # type: ignore[import-not-found]

        # sprint-14-aws (job-0293b): s3:// header-read.
        if raster_uri.startswith("s3://"):
            # sprint-14-aws (job-0293c): GDAL's /vsis3/ credential chain does
            # not resolve the EC2 instance role in this env (boto3 does) —
            # observed live: "does not exist" on an existing object. Stage the
            # bytes via the shared boto3 reader and open in-memory.
            from rasterio.io import MemoryFile
            from .cache import read_object_bytes_s3
            with MemoryFile(read_object_bytes_s3(raster_uri)) as mf:
                with mf.open() as src:
                    return src.crs
        elif os.path.isfile(raster_uri):
            with rasterio.open(raster_uri) as src:
                return src.crs
        else:
            raise ClipRasterPolygonError(
                "UNKNOWN_RASTER_URI",
                f"raster_uri {raster_uri!r} is not an s3:// URI and is not a "
                "readable local file. Provide an s3:// URI or an absolute local path.",
                retryable=False,
            )
    except ClipRasterPolygonError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ClipRasterPolygonError(
            "RASTER_OPEN_FAILED",
            f"rasterio could not open {raster_uri!r}: {exc}",
        ) from exc


def _download_raster_bytes(raster_uri: str, storage_client: Any | None = None) -> bytes:
    """Download raster bytes from an ``s3://`` URI or read from a local file.

    GCP is decommissioned: object-store reads route through boto3 (S3).
    ``storage_client`` is retained for backward-compatible call signatures
    but is ignored.
    """
    del storage_client  # GCP decommissioned — S3/local only.
    # sprint-14-aws (job-0290b): s3:// staging via the shared boto3 reader.
    if raster_uri.startswith("s3://"):
        from .cache import read_object_bytes_s3
        try:
            return read_object_bytes_s3(raster_uri)
        except Exception as exc:  # noqa: BLE001
            raise ClipRasterPolygonError(
                "RASTER_DOWNLOAD_FAILED",
                f"S3 download failed for {raster_uri!r}: {exc}",
            ) from exc
    if not os.path.isfile(raster_uri):
        raise ClipRasterPolygonError(
            "UNKNOWN_RASTER_URI",
            f"raster_uri {raster_uri!r} is not an s3:// URI and is not a "
            "readable local file.",
            retryable=False,
        )
    try:
        with open(raster_uri, "rb") as f:
            return f.read()
    except OSError as exc:
        raise ClipRasterPolygonError(
            "RASTER_DOWNLOAD_FAILED",
            f"Could not read local raster path {raster_uri!r}: {exc}",
        ) from exc


def _download_polygon_bytes(polygon_uri: str, storage_client: Any | None = None) -> tuple[bytes, str]:
    """Download polygon bytes from an ``s3://`` URI or read from a local file.

    GCP is decommissioned: object-store reads route through boto3 (S3).
    ``storage_client`` is retained for backward-compatible call signatures
    but is ignored.

    Returns:
        (bytes, suffix) where ``suffix`` is the file extension (e.g. ``.fgb``,
        ``.geojson``) used so geopandas/pyogrio picks the right driver when
        reading from the materialized temp file.
    """
    del storage_client  # GCP decommissioned — S3/local only.
    # sprint-14-aws (job-0290b): s3:// staging via the shared boto3 reader.
    if polygon_uri.startswith("s3://"):
        from .cache import read_object_bytes_s3
        _name = polygon_uri.rstrip("/").rsplit("/", 1)[-1]
        _suffix = ("." + _name.rsplit(".", 1)[-1]) if "." in _name else ".fgb"
        try:
            return read_object_bytes_s3(polygon_uri), _suffix
        except Exception as exc:  # noqa: BLE001
            raise ClipRasterPolygonError(
                "POLYGON_DOWNLOAD_FAILED",
                f"S3 download failed for {polygon_uri!r}: {exc}",
            ) from exc
    if not os.path.isfile(polygon_uri):
        raise ClipRasterPolygonError(
            "UNKNOWN_POLYGON_URI",
            f"polygon_uri {polygon_uri!r} is not an s3:// URI and is not a "
            "readable local file.",
            retryable=False,
        )
    try:
        with open(polygon_uri, "rb") as f:
            data = f.read()
    except OSError as exc:
        raise ClipRasterPolygonError(
            "POLYGON_DOWNLOAD_FAILED",
            f"Could not read local polygon path {polygon_uri!r}: {exc}",
        ) from exc
    suffix = os.path.splitext(polygon_uri)[1] or ".fgb"
    return data, suffix

    suffix = os.path.splitext(blob_path)[1] or ".fgb"
    return data, suffix


# ---------------------------------------------------------------------------
# Polygon load + filter + reproject helpers
# ---------------------------------------------------------------------------


def _load_polygon_geom(
    polygon_uri: str,
    feature_filter: dict[str, Any] | None,
    target_crs: Any,
    storage_client: Any | None,
) -> list[Any]:
    """Load polygon vector, apply ``feature_filter``, reproject to ``target_crs``.

    Returns:
        A list of shapely geometries (in ``target_crs``) suitable for
        ``rasterio.mask.mask``. Multi-feature inputs yield one shapely geometry
        per feature; the mask is the union of all of them.

    Raises:
        ClipRasterPolygonError: on read / filter / reproject failure.
    """
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ClipRasterPolygonError(
            "POLYGON_OPEN_FAILED",
            f"geopandas not available: {exc}",
        ) from exc

    poly_bytes, suffix = _download_polygon_bytes(polygon_uri, storage_client)

    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False, prefix="grace2_poly_") as tmp:
            tmp_path = tmp.name
            tmp.write(poly_bytes)

        try:
            gdf = gpd.read_file(tmp_path, engine="pyogrio")
        except Exception as exc:  # noqa: BLE001
            raise ClipRasterPolygonError(
                "POLYGON_OPEN_FAILED",
                f"geopandas could not read polygon_uri {polygon_uri!r}: {exc}",
            ) from exc

        # Apply feature_filter if given. Schema: {"property": <name>, "value": <val>}
        if feature_filter is not None:
            prop = feature_filter.get("property")
            value = feature_filter.get("value")
            if prop is None:
                raise ClipRasterPolygonError(
                    "POLYGON_FILTER_EMPTY",
                    f"feature_filter is missing 'property' key: {feature_filter!r}",
                    retryable=False,
                )
            if prop not in gdf.columns:
                raise ClipRasterPolygonError(
                    "POLYGON_FILTER_EMPTY",
                    f"feature_filter property {prop!r} not found in polygon attributes; "
                    f"available columns: {list(gdf.columns)}",
                    retryable=False,
                )
            gdf = gdf[gdf[prop] == value]
            if gdf.empty:
                raise ClipRasterPolygonError(
                    "POLYGON_FILTER_EMPTY",
                    f"feature_filter {feature_filter!r} matched 0 features in {polygon_uri!r}",
                    retryable=False,
                )

        # Reproject to target CRS (raster's native CRS) if necessary.
        if gdf.crs is None:
            raise ClipRasterPolygonError(
                "POLYGON_REPROJECT_FAILED",
                f"polygon_uri {polygon_uri!r} has no CRS metadata; cannot reproject safely.",
                retryable=False,
            )

        # Compare CRSs. If raster CRS is None (rare; usually means broken raster
        # metadata), assume EPSG:4326 lat/lon and let mask raise a clearer error.
        target_crs_obj = target_crs
        try:
            if target_crs_obj is None:
                from rasterio.crs import CRS as _CRS  # type: ignore[import-not-found]

                target_crs_obj = _CRS.from_epsg(4326)
            same_crs = gdf.crs == target_crs_obj
        except Exception:  # noqa: BLE001
            same_crs = False

        if not same_crs:
            try:
                gdf = gdf.to_crs(target_crs_obj)
            except Exception as exc:  # noqa: BLE001
                raise ClipRasterPolygonError(
                    "POLYGON_REPROJECT_FAILED",
                    f"polygon reprojection to {target_crs_obj} failed: {exc}",
                ) from exc

        # Return one geometry per feature.
        geoms = [geom for geom in gdf.geometry if geom is not None and not geom.is_empty]
        if not geoms:
            raise ClipRasterPolygonError(
                "POLYGON_FILTER_EMPTY",
                f"polygon_uri {polygon_uri!r} yielded zero non-empty geometries after filter/reproject.",
                retryable=False,
            )
        return geoms

    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Mask + write GeoTIFF
# ---------------------------------------------------------------------------


def _mask_and_write(
    raster_bytes: bytes,
    geoms: list[Any],
    nodata_outside: float | None,
) -> bytes:
    """Mask raster bytes with polygon geometry/ies; return GeoTIFF bytes.

    Uses ``rasterio.mask.mask(crop=True)`` so the output extent shrinks to the
    polygon bounding box. Output is LZW-compressed GeoTIFF preserving source CRS.

    Raises:
        ClipRasterPolygonError(MASK_FAILED) if masking raises or yields empty output.
    """
    import rasterio  # type: ignore[import-not-found]
    from rasterio.mask import mask as rio_mask  # type: ignore[import-not-found]

    in_tmp: str | None = None
    out_tmp: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".tif", delete=False, prefix="grace2_clip_in_") as in_f:
            in_tmp = in_f.name
            in_f.write(raster_bytes)

        with tempfile.NamedTemporaryFile(suffix=".tif", delete=False, prefix="grace2_clip_out_") as out_f:
            out_tmp = out_f.name
        # Remove placeholder so rasterio can create fresh.
        os.unlink(out_tmp)

        try:
            with rasterio.open(in_tmp) as src:
                src_nodata = src.nodata
                effective_nodata = nodata_outside if nodata_outside is not None else src_nodata
                # rasterio.mask requires nodata for crop=True to fill outside pixels;
                # fall back to 0 if both are None and the dtype is integer-like.
                if effective_nodata is None:
                    if src.dtypes[0].startswith("float"):
                        effective_nodata = float("nan")
                    else:
                        effective_nodata = 0

                out_image, out_transform = rio_mask(
                    src,
                    geoms,
                    crop=True,
                    nodata=effective_nodata,
                    filled=True,
                    all_touched=False,
                )

                if out_image.size == 0:
                    raise ClipRasterPolygonError(
                        "MASK_FAILED",
                        "rasterio.mask produced an empty array — polygon may not "
                        "intersect raster extent.",
                        retryable=False,
                    )

                out_meta = src.meta.copy()
                out_meta.update({
                    "driver": "GTiff",
                    "height": out_image.shape[1],
                    "width": out_image.shape[2],
                    "transform": out_transform,
                    "nodata": effective_nodata,
                    "compress": "LZW",
                })

                with rasterio.open(out_tmp, "w", **out_meta) as dst:
                    dst.write(out_image)
        except ClipRasterPolygonError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ClipRasterPolygonError(
                "MASK_FAILED",
                f"rasterio.mask failed: {exc}",
            ) from exc

        with open(out_tmp, "rb") as f:
            return f.read()
    finally:
        for path in (in_tmp, out_tmp):
            if path is not None:
                try:
                    os.unlink(path)
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    # Annotations: readOnlyHint=True (reads input raster/vector; writes cache
    # artifact only via the read-through shim), openWorldHint=False (all
    # computation is local GDAL/numpy; no external API calls),
    # destructiveHint=False, idempotentHint=True (deterministic transform;
    # same inputs always produce the same output pixels).
)
def clip_raster_to_polygon(
    raster_uri: str,
    polygon_uri: str,
    feature_filter: dict[str, Any] | None = None,
    nodata_outside: float | None = None,
    *,
    _storage_client: Any | None = None,
    _bucket: str | None = None,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Clip a raster to an arbitrary polygon (vs ``clip_raster_to_bbox`` which only does rectangles).

    Uses ``rasterio.mask.mask(crop=True)`` to mask a raster to one or more
    polygon features, with optional attribute-based feature selection and
    automatic CRS reprojection of the polygon to the raster CRS. Returns a
    new ``LayerURI`` for the masked raster, cached for 30 days.

    When to use:
        - User asks for analysis "in [named place]" (state, county, watershed,
          protected area, parcel) and the place is a non-rectangular polygon.
        - Masking a flood, slope, or DEM raster to a WDPA protected-area boundary
          or TIGER county outline before aggregation.
        - Preparing a raster zone input for ``compute_zonal_statistics`` by
          restricting to an exact administrative or ecological boundary.
        - Case 1 flood-habitat workflow: clip flood-depth COG to WDPA polygons.

    When NOT to use:
        - Rectangular bbox clips (use ``clip_raster_to_bbox`` — faster, no vector read).
        - Vector-to-vector clips (use ``clip_vector_to_polygon``).
        - Reprojection without spatial masking (use ``clip_raster_to_bbox`` with
          ``target_crs``).
        - Clipping based on complex attribute logic (pre-filter the vector first).

    Params:
        raster_uri: source raster URI — ``gs://`` GCS path or absolute local
            file path. Must be a GeoTIFF or any GDAL-readable raster format.
        polygon_uri: source polygon URI — ``gs://`` GCS path or absolute local
            file path. Must be a GDAL/OGR-readable vector format (FlatGeobuf,
            GeoJSON, GeoPackage, Shapefile, etc.) containing one or more
            polygon features with explicit CRS metadata.
        feature_filter: optional dict ``{"property": "<attribute_name>",
            "value": <expected_value>}`` — if the vector has multiple polygons,
            select only matching features BEFORE clip. Use to pick e.g. one
            state by name out of a TIGER state FlatGeobuf. If None, the union
            of ALL features in the vector is used as the mask.
        nodata_outside: value to assign to pixels outside the polygon. If None,
            uses the source raster's existing nodata value (or 0 for integer
            dtypes / NaN for float dtypes if no source nodata is defined).

    Returns:
        A ``LayerURI`` pointing at a masked GeoTIFF in the cache bucket::

            s3://trid3nt-cache/cache/static-30d/clip_raster_polygon/<key>.tif

        Output CRS matches the source raster's CRS. Output extent is the
        polygon's bounding box (``crop=True`` in ``rasterio.mask.mask``).

    LLM guidance:
        - The polygon is reprojected to the raster's CRS automatically; you do
          NOT need to pre-reproject. Just pass the LayerURI from any polygon
          fetcher.
        - feature_filter works on attribute equality only — for complex
          attribute logic (regex, range), filter the vector first with
          ``qgis_process``.
        - Cache key includes (raster_uri, polygon_uri, feature_filter,
          nodata_outside); same inputs return the same cached clip across runs.

    FR-CE-8: Results are routed through ``read_through`` so repeat calls with
    the same parameters return the cached clip without re-running rasterio.mask.
    TTL is 30 days.

    Cross-tool dependencies:
        Upstream (consumes):
        - ``fetch_dem`` / ``fetch_landcover`` / ``compute_slope`` / ``compute_hillshade`` /
          ``compute_colored_relief`` / ``compute_impervious_surface`` — supply the
          ``raster_uri`` input.
        - ``fetch_administrative_boundaries`` / ``fetch_wdpa_protected_areas`` —
          supply the ``polygon_uri`` mask input.
        - Flood-depth COG from ``postprocess_flood`` (via ``run_model_flood_scenario``)
          — primary raster input for Case 1 flood-habitat analysis.
        Downstream (feeds):
        - ``compute_zonal_statistics`` — pass the clipped ``LayerURI`` as
          ``value_raster_uri`` to aggregate within the polygon boundary.
        - ``run_model_flood_habitat_scenario`` — calls this internally to clip
          flood and species-layer rasters to WDPA polygon extents.
        - ``publish_layer`` — publish the clipped raster to QGIS Server.

    Raises:
        ClipRasterPolygonError: with one of the documented error codes if
        raster/polygon I/O fails, the feature_filter matches no features, CRS
        reprojection fails, or the polygon does not intersect the raster.
    """
    effective_bucket = _bucket or CACHE_BUCKET

    # 1. Detect source CRS so we know what to reproject the polygon to.
    source_crs = _get_source_crs(raster_uri)

    def _fetch() -> bytes:
        # 2. Load + filter + reproject polygon geometry to raster CRS.
        geoms = _load_polygon_geom(
            polygon_uri=polygon_uri,
            feature_filter=feature_filter,
            target_crs=source_crs,
            storage_client=_storage_client,
        )
        # 3. Download raster bytes.
        raster_bytes = _download_raster_bytes(raster_uri, _storage_client)
        # 4. Mask + write GeoTIFF.
        return _mask_and_write(raster_bytes, geoms, nodata_outside)

    # Cache key on (raster_uri, polygon_uri, feature_filter, nodata_outside).
    # None values are omitted by _canonicalize_params (cache.py rule).
    params: dict[str, Any] = {
        "raster_uri": raster_uri,
        "polygon_uri": polygon_uri,
        "feature_filter": feature_filter,
        "nodata_outside": nodata_outside,
    }

    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="tif",
        fetch_fn=_fetch,
        bucket=effective_bucket,
        storage_client=_storage_client,
    )
    assert result.uri is not None, "clip_raster_to_polygon is cacheable; uri must be set"

    # Build a stable layer_id from raster + polygon keys.
    raster_key = raster_uri.rstrip("/").rsplit("/", 1)[-1].replace(".tif", "")
    polygon_key = polygon_uri.rstrip("/").rsplit("/", 1)[-1]
    polygon_key = os.path.splitext(polygon_key)[0]

    filter_suffix = ""
    if feature_filter is not None:
        # Compact suffix: feature_filter={property: NAME, value: Washington} -> "-Washington"
        val = feature_filter.get("value")
        if val is not None:
            filter_suffix = "-" + str(val).replace(" ", "_")[:32]

    layer_id = f"clip-poly-{raster_key}-{polygon_key}{filter_suffix}"

    name = f"Clipped raster (polygon mask){filter_suffix}"

    return LayerURI(
        layer_id=layer_id,
        name=name,
        layer_type="raster",
        uri=result.uri,
        style_preset="continuous_dem",  # default; caller can override at the map layer
        role="context",
        units=None,
        bbox=None,
    )
