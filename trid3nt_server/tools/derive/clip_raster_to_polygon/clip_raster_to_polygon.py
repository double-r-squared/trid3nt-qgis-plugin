"""Atomic tool ``clip_raster_to_polygon`` - clip a raster to a polygon OR a bbox.

Both paths run one in-process ``rasterio.mask`` call; the clip geometry is
reprojected to the raster CRS first and the output keeps it unless retargeted.
"""
from __future__ import annotations

import logging
import os
import tempfile
from typing import Any

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.cache import CACHE_BUCKET, read_through

__all__ = [
    "clip_raster_to_polygon",
    "ClipRasterPolygonError",
]

logger = logging.getLogger("trid3nt_server.tools.derive.clip_raster_to_polygon.clip_raster_to_polygon")



# ``error_code`` is the caller's typed switch and is one of:
#   RASTER_OPEN_FAILED, RASTER_DOWNLOAD_FAILED, UNKNOWN_RASTER_URI,
#   POLYGON_OPEN_FAILED, POLYGON_DOWNLOAD_FAILED, UNKNOWN_POLYGON_URI,
#   POLYGON_FILTER_EMPTY, POLYGON_REPROJECT_FAILED, MASK_FAILED,
#   INVALID_CLIP_INPUT, BBOX_REPROJECT_FAILED.
class ClipRasterPolygonError(RuntimeError):
    """A clip input could not be fetched, opened, filtered, reprojected or masked."""

    error_code: str
    retryable: bool = True

    def __init__(self, error_code: str, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable



_METADATA = AtomicToolMetadata(
    name="clip_raster_to_polygon",
    ttl_class="static-30d",
    source_class="clip_raster_polygon",
    cacheable=True,
)




def _get_source_crs(raster_uri: str) -> Any:
    """The raster's CRS; ``s3://`` bytes are staged and opened in-memory. An
    unrecognised URI or an unopenable raster raises ``ClipRasterPolygonError``.
    """
    try:
        import rasterio  # type: ignore[import-not-found]

        # boto3 owns the credential chain here, not GDAL's /vsis3/.
        if raster_uri.startswith("s3://"):
            from rasterio.io import MemoryFile
            from trid3nt_server.tools.cache import read_object_bytes_s3
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
    """Raster bytes from an ``s3://`` URI or a local file; ``storage_client`` is
    ignored and anything else raises ``ClipRasterPolygonError``.
    """
    del storage_client
    if raster_uri.startswith("s3://"):
        from trid3nt_server.tools.cache import read_object_bytes_s3
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
    """``(bytes, suffix)`` for an ``s3://`` URI or a local file; the suffix is the
    extension pyogrio needs to pick a driver off the materialized temp file.
    """
    del storage_client
    if polygon_uri.startswith("s3://"):
        from trid3nt_server.tools.cache import read_object_bytes_s3
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




def _load_polygon_geom(
    polygon_uri: str,
    feature_filter: dict[str, Any] | None,
    target_crs: Any,
    storage_client: Any | None,
) -> list[Any]:
    """Load, filter and reproject the polygon: one shapely geometry per surviving
    feature, in ``target_crs``, which ``rasterio.mask`` takes as one union mask.
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
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False, prefix="trid3nt_poly_") as tmp:
            tmp_path = tmp.name
            tmp.write(poly_bytes)

        try:
            gdf = gpd.read_file(tmp_path, engine="pyogrio")
        except Exception as exc:  # noqa: BLE001
            raise ClipRasterPolygonError(
                "POLYGON_OPEN_FAILED",
                f"geopandas could not read polygon_uri {polygon_uri!r}: {exc}",
            ) from exc

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

        if gdf.crs is None:
            raise ClipRasterPolygonError(
                "POLYGON_REPROJECT_FAILED",
                f"polygon_uri {polygon_uri!r} has no CRS metadata; cannot reproject safely.",
                retryable=False,
            )

        # A raster with no CRS means broken metadata; assuming EPSG:4326 here
        # lets the mask raise a clearer error than a None comparison would.
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




def _bbox_to_geoms(
    bbox: tuple[float, float, float, float],
    bbox_crs: str,
    target_crs: Any,
) -> list[Any]:
    """One rectangular polygon for ``bbox`` (west, south, east, north) in
    ``bbox_crs``, reprojected to ``target_crs`` so the mask runs in the raster grid.
    """
    try:
        west, south, east, north = (float(v) for v in bbox)
    except (TypeError, ValueError) as exc:
        raise ClipRasterPolygonError(
            "INVALID_CLIP_INPUT",
            f"bbox must be a 4-tuple (west, south, east, north); got {bbox!r}",
            retryable=False,
        ) from exc

    rect = {
        "type": "Polygon",
        "coordinates": [[
            [west, south], [east, south], [east, north],
            [west, north], [west, south],
        ]],
    }

    try:
        from rasterio.crs import CRS  # type: ignore[import-not-found]
        from rasterio.warp import transform_geom  # type: ignore[import-not-found]

        src_crs = CRS.from_user_input(bbox_crs)
        dst_crs = target_crs if target_crs is not None else CRS.from_epsg(4326)
        if src_crs != dst_crs:
            rect = transform_geom(src_crs, dst_crs, rect)
    except ClipRasterPolygonError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ClipRasterPolygonError(
            "BBOX_REPROJECT_FAILED",
            f"could not reproject bbox {bbox!r} from {bbox_crs} to the raster CRS: {exc}",
        ) from exc
    return [rect]




def _mask_and_write(
    raster_bytes: bytes,
    geoms: list[Any],
    nodata_outside: float | None,
    target_crs: str | None = None,
) -> bytes:
    """Mask raster bytes to ``geoms`` and return LZW GeoTIFF bytes; ``crop=True``
    shrinks the extent to the geometry, ``target_crs`` reprojects the result.
    """
    import rasterio  # type: ignore[import-not-found]
    from rasterio.mask import mask as rio_mask  # type: ignore[import-not-found]

    in_tmp: str | None = None
    out_tmp: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".tif", delete=False, prefix="trid3nt_clip_in_") as in_f:
            in_tmp = in_f.name
            in_f.write(raster_bytes)

        with tempfile.NamedTemporaryFile(suffix=".tif", delete=False, prefix="trid3nt_clip_out_") as out_f:
            out_tmp = out_f.name
        os.unlink(out_tmp)

        try:
            with rasterio.open(in_tmp) as src:
                src_nodata = src.nodata
                effective_nodata = nodata_outside if nodata_outside is not None else src_nodata
                # crop=True needs a nodata value to fill outside pixels, so an
                # unset one falls back to NaN for floats and 0 for integers.
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
                        "rasterio.mask produced an empty array -- polygon may not "
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
                src_crs = src.crs

                if target_crs is not None:
                    out_image, out_meta = _reproject_masked(
                        out_image, out_meta, src_crs, target_crs, effective_nodata
                    )

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


def _reproject_masked(
    out_image: Any,
    out_meta: dict[str, Any],
    src_crs: Any,
    target_crs: str,
    nodata: float | int,
) -> tuple[Any, dict[str, Any]]:
    """Reproject a masked array to ``target_crs``, returning ``(image, meta)``;
    a no-op returning the inputs when it already equals the source CRS.
    """
    import numpy as np  # type: ignore[import-not-found]
    from rasterio.crs import CRS  # type: ignore[import-not-found]
    from rasterio.warp import Resampling, calculate_default_transform, reproject

    try:
        dst_crs = CRS.from_user_input(target_crs)
        if src_crs is not None and dst_crs == src_crs:
            return out_image, out_meta
        height, width = out_image.shape[1], out_image.shape[2]
        left = out_meta["transform"].c
        top = out_meta["transform"].f
        right = left + out_meta["transform"].a * width
        bottom = top + out_meta["transform"].e * height
        dst_transform, dst_w, dst_h = calculate_default_transform(
            src_crs, dst_crs, width, height, left, bottom, right, top
        )
        dst_image = np.full((out_image.shape[0], dst_h, dst_w), nodata, dtype=out_image.dtype)
        for b in range(out_image.shape[0]):
            reproject(
                source=out_image[b],
                destination=dst_image[b],
                src_transform=out_meta["transform"],
                src_crs=src_crs,
                dst_transform=dst_transform,
                dst_crs=dst_crs,
                src_nodata=nodata,
                dst_nodata=nodata,
                resampling=Resampling.nearest,
            )
        new_meta = dict(out_meta)
        new_meta.update({
            "crs": dst_crs,
            "transform": dst_transform,
            "width": dst_w,
            "height": dst_h,
        })
        return dst_image, new_meta
    except Exception as exc:  # noqa: BLE001
        raise ClipRasterPolygonError(
            "BBOX_REPROJECT_FAILED",
            f"could not reproject clipped raster to {target_crs}: {exc}",
        ) from exc




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
    polygon_uri: str | None = None,
    feature_filter: dict[str, Any] | None = None,
    nodata_outside: float | None = None,
    bbox: tuple[float, float, float, float] | None = None,
    bbox_crs: str = "EPSG:4326",
    target_crs: str | None = None,
    *,
    _storage_client: Any | None = None,
    _bucket: str | None = None,
    # absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> LayerURI:
    """Clip a raster to a polygon OR a rectangular bounding box.

    Use when a raster is larger than the analysis area: mask a flood / slope /
    DEM raster to a named place (state, county, watershed, parcel) before
    aggregation, or crop to a rectangle, reprojecting in the same pass. Pass
    EITHER ``polygon_uri`` OR ``bbox``. Do NOT use for vector-to-vector clips.

    Params:
        raster_uri: source raster (``s3://`` or local path).
        polygon_uri: polygon vector (FlatGeobuf / GeoJSON / GPKG / SHP).
        feature_filter: ``{"property": name, "value": val}`` selects features
            before the clip; else every feature dissolves into one mask.
        nodata_outside: value outside the clip; defaults to the source nodata.
        bbox: ``(west, south, east, north)`` in ``bbox_crs`` (EPSG:4326).
        target_crs: output CRS; else the source CRS is preserved.

    The clip geometry is reprojected to the raster CRS first. I/O, filter,
    reprojection and non-intersecting clips raise ClipRasterPolygonError.
    """
    effective_bucket = _bucket or CACHE_BUCKET

    use_bbox = bbox is not None
    if use_bbox == (polygon_uri is not None):
        raise ClipRasterPolygonError(
            "INVALID_CLIP_INPUT",
            "Provide EXACTLY one of polygon_uri (arbitrary polygon) or bbox "
            f"(rectangle); got polygon_uri={polygon_uri!r}, bbox={bbox!r}.",
            retryable=False,
        )

    source_crs = _get_source_crs(raster_uri)

    def _fetch() -> bytes:
        if use_bbox:
            geoms = _bbox_to_geoms(bbox, bbox_crs, source_crs)
        else:
            geoms = _load_polygon_geom(
                polygon_uri=polygon_uri,
                feature_filter=feature_filter,
                target_crs=source_crs,
                storage_client=_storage_client,
            )
        raster_bytes = _download_raster_bytes(raster_uri, _storage_client)
        return _mask_and_write(raster_bytes, geoms, nodata_outside, target_crs)

    # Every parameter that moves output pixels is in the key; None values are
    # omitted by _canonicalize_params.
    params: dict[str, Any] = {
        "raster_uri": raster_uri,
        "polygon_uri": polygon_uri,
        "feature_filter": feature_filter,
        "nodata_outside": nodata_outside,
        "bbox": [round(float(v), 6) for v in bbox] if use_bbox else None,
        "bbox_crs": bbox_crs if use_bbox else None,
        "target_crs": target_crs,
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

    raster_key = raster_uri.rstrip("/").rsplit("/", 1)[-1].replace(".tif", "")
    crs_suffix = ""
    if target_crs:
        crs_suffix = "-" + target_crs.replace("EPSG:", "epsg").replace(":", "-")

    if use_bbox:
        layer_id = f"clip-bbox-{raster_key}{crs_suffix}"
        name = f"Clipped raster [{target_crs or bbox_crs}]"
    else:
        polygon_key = os.path.splitext(polygon_uri.rstrip("/").rsplit("/", 1)[-1])[0]
        filter_suffix = ""
        if feature_filter is not None:
            # Compact suffix: {property: NAME, value: Washington} -> "-Washington"
            val = feature_filter.get("value")
            if val is not None:
                filter_suffix = "-" + str(val).replace(" ", "_")[:32]
        layer_id = f"clip-poly-{raster_key}-{polygon_key}{filter_suffix}{crs_suffix}"
        name = f"Clipped raster (polygon mask){filter_suffix}"

    return LayerURI(
        layer_id=layer_id,
        name=name,
        layer_type="raster",
        uri=result.uri,
        style={"kind": "continuous", "ramp": "gray", "units": "m", "label": "Elevation"},
        role="context",
        units=None,
        bbox=None,
    )
