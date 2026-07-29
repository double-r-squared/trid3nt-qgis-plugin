"""raster-cog executor (contract sec 2.1).

Reads a gridded source to a CRS-tagged single-band COG. Three sub-modes keyed by
``ingest.access``:
  - ``opendap``       xarray subset + time_reduce collapse  (gridmet)
  - ``direct_window`` httpx-transport windowed read of a known COG/VRT (ADR-0044)
  - ``stac_search``   pystac-client search + windowed reproject (esri, single tile)

Emits ``nodata=nan``, north-up (no lat sortby -- the gridmet orientation lesson
is a ``normalize.orientation`` directive), CRS re-asserted post-astype. The pure
serializer ``array_to_cog_bytes`` is offline-testable with a synthetic array; the
network sub-modes route through ``fetch_source_array`` which tests monkeypatch.
"""

from __future__ import annotations

import logging
import os
import tempfile
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..errors import router_empty_error, router_upstream_error

logger = logging.getLogger(
    "trid3nt_server.agent.tools.fetchers._router.executors.raster_cog"
)

__all__ = ["array_to_cog_bytes", "fetch_source_array", "stac_to_mosaic", "execute"]


def array_to_cog_bytes(
    array: Any,
    transform: Any,
    crs: Any,
    *,
    nodata: float = float("nan"),
    dtype: str = "float32",
    colormap: dict | None = None,
) -> bytes:
    """Serialize a 2D (or single-band 3D) array to COG bytes (pure, offline).

    North-up is the caller's responsibility (the transform already carries a
    negative y-step). CRS is re-asserted on the profile after the astype so the
    geographic-correctness gate never sees a dropped CRS (codified lesson).

    ``colormap`` (a GDAL ``{class_code: (r,g,b,a)}`` table) bakes a categorical
    palette into band 1 with ``ColorInterp.palette`` -- byte-identical to the
    esri_landcover twin's ``_write_palette_cog`` so ``publish_layer`` colorizes
    from the embedded table (categorical passthrough, no rescale).
    """
    import numpy as np
    import rasterio

    arr = np.asarray(array, dtype=dtype)
    if arr.ndim == 2:
        arr = arr[np.newaxis, :, :]
    if arr.ndim != 3:
        raise ValueError(f"array must be 2D or single-band 3D; got shape {arr.shape}")
    count, height, width = arr.shape

    def _write_palette(dst: Any) -> None:
        if colormap is None:
            return
        dst.write_colormap(1, colormap)
        try:
            from rasterio.enums import ColorInterp

            interp = list(dst.colorinterp)
            interp[0] = ColorInterp.palette
            dst.colorinterp = tuple(interp)
        except Exception:  # noqa: BLE001 -- colorinterp set is best-effort
            pass

    out_fd, out_path = tempfile.mkstemp(suffix=".tif", prefix="trid3nt_router_cog_")
    os.close(out_fd)
    try:
        base_profile = {
            "dtype": dtype,
            "count": count,
            "height": height,
            "width": width,
            "crs": crs,
            "transform": transform,
            "nodata": nodata,
            "compress": "DEFLATE",
        }
        try:
            with rasterio.open(
                out_path, "w", driver="COG", blocksize=256, **base_profile
            ) as dst:
                dst.write(arr)
                _write_palette(dst)
        except Exception:  # noqa: BLE001 -- COG driver may be unavailable; GTiff tiled
            with rasterio.open(
                out_path,
                "w",
                driver="GTiff",
                tiled=True,
                blockxsize=256,
                blockysize=256,
                **base_profile,
            ) as dst:
                dst.write(arr)
                _write_palette(dst)
        with open(out_path, "rb") as f:
            return f.read()
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# Source-array fetch (network). Tests monkeypatch this dispatcher.
# --------------------------------------------------------------------------- #


def fetch_source_array(spec: SourceSpec, params: dict[str, Any]) -> tuple[Any, Any, Any]:
    """Return ``(array_2d, affine_transform, crs)`` for the requested extent.

    Dispatches on ``ingest.access`` (default ``opendap`` for a raster spec). Each
    sub-mode raises :class:`RouterUpstreamError` on open/read failure and
    :class:`RouterEmptyError` when the window has no finite pixels.
    """
    access = (spec.ingest or {}).get("access", "opendap")
    if access == "opendap":
        return _opendap_to_array(spec, params)
    if access == "direct_window":
        return _direct_window_to_array(spec, params)
    if access == "stac_search":
        return _stac_to_array(spec, params)
    raise router_upstream_error(
        spec.error_code_prefix, f"unknown raster access mode {access!r}"
    )


def _opendap_to_array(spec: SourceSpec, params: dict[str, Any]) -> tuple[Any, Any, Any]:
    """OPeNDAP/THREDDS netCDF subset + time-mean collapse (gridmet reference)."""
    import numpy as np
    import rasterio.transform as rtransform

    ingest = spec.ingest or {}
    variable = params.get("variable")
    bbox = params["bbox"]
    d0 = params.get("start_date")
    d1 = params.get("end_date")

    dap_tmpl = ingest.get("dap_url_template")
    endpoint = spec.endpoints.get("data") or next(iter(spec.endpoints.values()))
    base = (endpoint.url or endpoint.url_template or "").rstrip("/")
    if dap_tmpl:
        dap_url = f"{base}/{dap_tmpl.format(variable=variable)}"
    else:
        dap_url = base

    try:
        import xarray as xr  # noqa: F401
    except ImportError as exc:  # pragma: no cover
        raise router_upstream_error(spec.error_code_prefix, f"xarray unavailable: {exc}")

    try:
        ds = xr.open_dataset(dap_url, chunks=None)
    except Exception as exc:  # noqa: BLE001
        raise router_upstream_error(
            spec.error_code_prefix, f"could not open OPeNDAP {dap_url}: {exc}"
        )
    try:
        data_vars = [v for v in ds.data_vars if v not in ds.coords]
        if not data_vars:
            raise router_upstream_error(spec.error_code_prefix, "source carried no data variables")
        da = ds[data_vars[0]]
        time_dim = next((d for d in da.dims if d in ("day", "time")), None)
        lat_dim = next((d for d in da.dims if d in ("lat", "latitude", "y")), None)
        lon_dim = next((d for d in da.dims if d in ("lon", "longitude", "x")), None)
        west, south, east, north = bbox
        if time_dim is not None and d0 and d1:
            t = da[time_dim].values
            if np.issubdtype(t.dtype, np.datetime64):
                da = da.sel({time_dim: slice(np.datetime64(d0), np.datetime64(d1))})
        lats = da[lat_dim].values
        if lats[0] > lats[-1]:
            da = da.sel({lat_dim: slice(north, south)})
        else:
            da = da.sel({lat_dim: slice(south, north)})
        da = da.sel({lon_dim: slice(west, east)})
        if da.size == 0 or any(s == 0 for s in da.shape):
            raise router_empty_error(spec.error_code_prefix, f"bbox={bbox} produced an empty window", spec.empty_error_suffix)
        if time_dim is not None and time_dim in da.dims:
            da = da.mean(dim=time_dim, skipna=True)
        arr = np.asarray(da.values, dtype="float32")
        if not np.isfinite(arr).any():
            raise router_empty_error(spec.error_code_prefix, f"bbox={bbox} produced no finite pixels", spec.empty_error_suffix)
        lons = da[lon_dim].values
        lat_vals = da[lat_dim].values
        transform = rtransform.from_bounds(
            float(lons.min()), float(lat_vals.min()),
            float(lons.max()), float(lat_vals.max()),
            arr.shape[1], arr.shape[0],
        )
        return arr, transform, spec.normalize.crs
    finally:
        try:
            ds.close()
        except Exception:  # noqa: BLE001
            pass


def _direct_window_to_array(spec: SourceSpec, params: dict[str, Any]) -> tuple[Any, Any, Any]:
    """Windowed read of a known COG/VRT through the httpx transport (direct-window).

    Reads via the coalescing/parallel range opener (ADR-0044) -- GDAL never
    networks -- so the transport surfaces a typed status: a missing object (404 /
    S3 NoSuchKey) maps to the typed EMPTY frame (the twins' no-coverage semantics),
    403/AccessDenied to an auth-class upstream error, 429/5xx to a retryable
    upstream error. This is where the old ``/vsicurl/`` path lost the 404->EMPTY
    split (GDAL discarded the status, so every failure read as UPSTREAM_ERROR).
    """
    import numpy as np
    from rasterio.windows import from_bounds as window_from_bounds

    from ..transport import (
        TransportAuthError,
        TransportNotFound,
        open_windowed_cog,
    )

    bbox = params["bbox"]
    endpoint = spec.endpoints.get("data") or next(iter(spec.endpoints.values()))
    url = endpoint.url or endpoint.url_template or ""
    if url.startswith("/vsicurl/"):
        url = url[len("/vsicurl/"):]
    try:
        with open_windowed_cog(url) as src:
            win = window_from_bounds(*bbox, transform=src.transform)
            arr = src.read(1, window=win)
            transform = src.window_transform(win)
            crs = src.crs
    except TransportNotFound as exc:
        raise router_empty_error(
            spec.error_code_prefix,
            f"direct-window object not found (bbox={bbox}): {exc}",
            spec.empty_error_suffix,
        )
    except TransportAuthError as exc:
        err = router_upstream_error(spec.error_code_prefix, f"direct-window access denied: {exc}")
        err.retryable = False
        raise err
    except Exception as exc:  # noqa: BLE001 -- retryable upstream (timeout/5xx/read)
        raise router_upstream_error(spec.error_code_prefix, f"direct-window read failed: {exc}")
    if arr.size == 0:
        raise router_empty_error(spec.error_code_prefix, f"bbox={bbox} produced an empty window", spec.empty_error_suffix)
    return np.asarray(arr, dtype="float32"), transform, crs


def _bbox_intersects(item_bbox: Any, bbox: tuple[float, float, float, float]) -> bool:
    """True iff ``item_bbox`` (min_lon,min_lat,max_lon,max_lat) overlaps ``bbox``."""
    try:
        ib0, ib1, ib2, ib3 = (float(item_bbox[0]), float(item_bbox[1]),
                              float(item_bbox[2]), float(item_bbox[3]))
    except (TypeError, ValueError, IndexError):
        return False
    return not (ib2 < bbox[0] or ib0 > bbox[2] or ib3 < bbox[1] or ib1 > bbox[3])


def _select_stac_items(spec: SourceSpec, params: dict[str, Any],
                       bbox: tuple[float, float, float, float]) -> tuple[list[Any], dict, str]:
    """Search PC STAC + narrow to items intersecting ``bbox`` in the request year.

    Reuses the esri twin ``_select_items`` semantics (bbox-intersect +
    start_datetime year filter). Search failure -> typed upstream; zero items ->
    typed empty (honest no-coverage).
    """
    ingest = spec.ingest or {}
    stac = ingest.get("stac", {})
    root = stac.get("root")
    collection = stac.get("collection")
    year = params.get("year")
    try:
        from pystac_client import Client
    except ImportError as exc:  # pragma: no cover
        raise router_upstream_error(spec.error_code_prefix, f"pystac-client unavailable: {exc}")
    dt_range = f"{year}-01-01/{year}-12-31" if year is not None else None
    try:
        client = Client.open(root)
        search = client.search(collections=[collection], bbox=list(bbox),
                               datetime=dt_range, limit=100)
        all_items = list(search.items())
    except Exception as exc:  # noqa: BLE001 -- translate any pystac/http error
        raise router_upstream_error(
            spec.error_code_prefix,
            f"STAC search failed (collection={collection!r}, bbox={bbox}, year={year}): {exc}",
        )
    items = [it for it in all_items if _bbox_intersects(getattr(it, "bbox", None), bbox)]
    if year is not None:
        items = [
            it for it in items
            if str(getattr(it, "properties", {}).get("start_datetime", "")).startswith(str(year))
        ]
    if not items:
        raise router_empty_error(
            spec.error_code_prefix, f"no STAC item covers bbox={bbox} for year {year}",
            spec.empty_error_suffix,
        )
    return items, stac, str(collection)


def _read_tile_window(spec: SourceSpec, signed_href: str,
                      bbox: tuple[float, float, float, float],
                      width_px: int, height_px: int, nodata: int) -> tuple[Any, dict | None]:
    """Warp+window-read a tile's band-1 categorical data to EPSG:4326 nearest.

    Reuses the esri twin ``_read_tile_window`` verbatim (reproject, nearest,
    nodata read-back). Opens local paths directly (cached/test COGs) and https
    hrefs through GDAL ``/vsicurl/``. Returns ``(uint8 (H,W), colormap|None)``.
    """
    import numpy as np
    import rasterio
    from rasterio.warp import reproject, Resampling

    from ...imagery import _pc_stac

    if os.path.exists(signed_href):
        path = signed_href
    elif signed_href.startswith(("http://", "https://")):
        path = "/vsicurl/" + signed_href
    else:
        path = signed_href
    try:
        with rasterio.Env(**_pc_stac.VSICURL_ENV_KW):
            with rasterio.open(path) as src:
                try:
                    colormap = src.colormap(1)
                except (ValueError, KeyError):
                    colormap = None
                dst_transform = rasterio.transform.from_bounds(
                    bbox[0], bbox[1], bbox[2], bbox[3], width_px, height_px
                )
                dst = np.zeros((height_px, width_px), dtype="uint8")
                reproject(
                    source=rasterio.band(src, 1),
                    destination=dst,
                    src_transform=src.transform,
                    src_crs=src.crs,
                    dst_transform=dst_transform,
                    dst_crs="EPSG:4326",
                    resampling=Resampling.nearest,
                    src_nodata=src.nodata if src.nodata is not None else nodata,
                    dst_nodata=nodata,
                )
        return dst, colormap
    except Exception as exc:  # noqa: BLE001 -- translate any rasterio/GDAL error
        raise router_upstream_error(
            spec.error_code_prefix, f"STAC tile read failed (href={signed_href[:120]!r}): {exc}"
        )


def stac_to_mosaic(spec: SourceSpec, params: dict[str, Any]) -> tuple[Any, Any, Any, dict | None]:
    """STAC search -> SAS-sign -> reproject each item to EPSG:4326 nearest ->
    first-non-nodata uint8 mosaic + embedded palette (contract sec 2.1).

    Returns ``(mosaic_uint8 (H,W), transform, "EPSG:4326", colormap|None)``. This
    reuses the ``_pc_stac`` primitives (``sas_sign_href`` + ``bbox_pixel_dims``)
    and reproduces the esri twin ``_fetch_single_tile_mosaic`` byte-for-byte.
    """
    import numpy as np
    import rasterio

    from ...imagery import _pc_stac

    bbox = tuple(params["bbox"])
    ingest = spec.ingest or {}
    mosaic_cfg = ingest.get("mosaic", {})
    nodata = int(mosaic_cfg.get("nodata", 0))
    native_cell_m = float(ingest.get("native_cell_m", 10.0))

    items, stac, collection = _select_stac_items(spec, params, bbox)
    asset_key = stac.get("data_asset", "data")
    sign_mode = stac.get("sign")

    width_px, height_px = _pc_stac.bbox_pixel_dims(bbox, native_cell_m)
    dst_transform = rasterio.transform.from_bounds(
        bbox[0], bbox[1], bbox[2], bbox[3], width_px, height_px
    )
    mosaic = np.zeros((height_px, width_px), dtype="uint8")
    colormap: dict | None = None
    for item in items:
        assets = getattr(item, "assets", {}) or {}
        if asset_key not in assets:
            continue
        href = assets[asset_key].href
        if sign_mode == "sas":
            href = _pc_stac.sas_sign_href(href, collection)
        tile, tile_cmap = _read_tile_window(spec, href, bbox, width_px, height_px, nodata)
        if colormap is None and tile_cmap is not None:
            colormap = tile_cmap
        fill = (mosaic == nodata) & (tile != nodata)
        if fill.any():
            mosaic[fill] = tile[fill]

    if int((mosaic != nodata).sum()) == 0:
        raise router_empty_error(
            spec.error_code_prefix,
            f"io-lulc items intersected bbox={bbox} but the mosaic is entirely no-data",
            spec.empty_error_suffix,
        )
    return mosaic, dst_transform, "EPSG:4326", colormap


def _stac_to_array(spec: SourceSpec, params: dict[str, Any]) -> tuple[Any, Any, Any]:
    """STAC categorical mosaic as ``(array, transform, crs)`` -- colormap dropped
    to satisfy the ``fetch_source_array`` 3-tuple contract. The palette-preserving
    path is :func:`execute` (and the tiled-mosaic transform's categorical branch)."""
    arr, transform, crs, _cmap = stac_to_mosaic(spec, params)
    return arr, transform, crs


def execute(spec: SourceSpec, params: dict[str, Any]) -> bytes:
    """Fetch the source array and serialize to COG bytes (the ``fetch_fn`` body).

    The ``stac_search`` sub-mode produces a uint8 categorical mosaic and bakes the
    embedded palette in (esri_landcover parity); other sub-modes emit float32.
    """
    access = (spec.ingest or {}).get("access", "opendap")
    if access == "stac_search":
        ingest = spec.ingest or {}
        arr, transform, crs, colormap = stac_to_mosaic(spec, params)
        nodata = int((ingest.get("mosaic") or {}).get("nodata", 0))
        dtype = str(ingest.get("dtype", "uint8"))
        return array_to_cog_bytes(
            arr, transform, crs, nodata=nodata, dtype=dtype, colormap=colormap
        )
    arr, transform, crs = fetch_source_array(spec, params)
    return array_to_cog_bytes(arr, transform, crs)
