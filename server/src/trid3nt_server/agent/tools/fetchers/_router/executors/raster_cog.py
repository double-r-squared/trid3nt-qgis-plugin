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

from ..errors import (
    RouterError,
    router_empty_error,
    router_input_error,
    router_upstream_error,
)

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
    if access == "stac_float":
        return _stac_float_to_array(spec, params)
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
    from rasterio.windows import Window
    from rasterio.windows import from_bounds as window_from_bounds

    from ..transport import (
        TransportAuthError,
        TransportNotFound,
        open_windowed_cog,
    )

    ingest = spec.ingest or {}
    bbox = params["bbox"]

    # url_by_param (wave-8, gcn250): a param value (enum) selects the object URL;
    # absent -> the single `data` endpoint URL (every prior direct_window spec).
    ubp = ingest.get("url_by_param")
    if ubp:
        url = (ubp.get("map") or {}).get(params.get(ubp.get("param")))
        if url is None:
            raise router_upstream_error(
                spec.error_code_prefix,
                f"no direct-window URL for {ubp.get('param')}={params.get(ubp.get('param'))!r}",
            )
    else:
        endpoint = spec.endpoints.get("data") or next(iter(spec.endpoints.values()))
        url = endpoint.url or endpoint.url_template or ""
    if url.startswith("/vsicurl/"):
        url = url[len("/vsicurl/"):]

    round_pixel = bool(ingest.get("round_pixel_window", False))
    try:
        with open_windowed_cog(url) as src:
            win = window_from_bounds(*bbox, transform=src.transform)
            if round_pixel:
                # gcn250 parity: outward-round to integer pixels, clip to extent.
                win = win.round_offsets(op="floor").round_lengths(op="ceil")
                win = win.intersection(Window(0, 0, src.width, src.height))
                if win.width <= 0 or win.height <= 0:
                    raise router_empty_error(
                        spec.error_code_prefix,
                        f"bbox={bbox} produces a zero-size window", spec.empty_error_suffix)
            arr = src.read(1, window=win)
            transform = src.window_transform(win)
            crs = src.crs
            src_nodata = src.nodata
    except RouterError:
        raise  # a typed router error (zero-size empty) propagates unwrapped
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

    # all-nodata coverage gate (wave-8, gcn250): a window entirely the source
    # nodata sentinel is honest no-coverage (over open water / off-disk), never a
    # fabricated layer. Absent `nodata_gate` -> no gate (every prior spec).
    if ingest.get("nodata_gate"):
        sentinel = src_nodata if src_nodata is not None else float(ingest.get("default_nodata", 255))
        if not bool((arr != sentinel).any()):
            raise router_empty_error(
                spec.error_code_prefix,
                f"bbox={bbox} produced no valid pixels (all-nodata window -- over "
                f"open water or outside coverage)", spec.empty_error_suffix)
    return np.asarray(arr, dtype="float32"), transform, crs


def _imageserver_size(bbox: tuple[float, float, float, float], ingest: dict[str, Any]) -> tuple[int, int]:
    """ImageServer ``size`` (width_px, height_px) for ``bbox`` at the native grid.

    Approximates m/degree at the bbox midpoint latitude (the standard ArcGIS
    ImageServer sizing the landfire/usfs twins used), rounds to the native cell,
    and clamps per axis. Declarative knobs: ``native_cell_m`` / ``px_min`` /
    ``px_max``.
    """
    import math

    cell_m = float(ingest.get("native_cell_m", 30.0))
    px_min = int(ingest.get("px_min", 16))
    px_max = int(ingest.get("px_max", 4096))
    min_lon, min_lat, max_lon, max_lat = bbox
    mid_lat = 0.5 * (min_lat + max_lat)
    m_per_deg_lon = 111_320.0 * math.cos(math.radians(mid_lat))
    width_m = (max_lon - min_lon) * m_per_deg_lon
    height_m = (max_lat - min_lat) * 111_320.0
    width_px = max(px_min, min(px_max, int(round(width_m / cell_m))))
    height_px = max(px_min, min(px_max, int(round(height_m / cell_m))))
    return width_px, height_px


def _imageserver_export_bytes(spec: SourceSpec, params: dict[str, Any]) -> bytes:
    """ArcGIS ImageServer ``exportImage`` REST fetch + all-nodata coverage gate.

    Returns the server's ready GeoTIFF body UNCHANGED (the twins did no
    reserialization -- the exportImage response IS the cached artifact), so the
    router's output is value-identical to the hand-written twin. The transport
    owns the socket (ADR-0044); GDAL only parses the returned bytes for the
    all-nodata gate. A JSON error envelope / non-TIFF body -> typed UPSTREAM;
    an all-nodata raster (bbox outside coverage) -> typed EMPTY.
    """
    import numpy as np
    import rasterio
    from rasterio.io import MemoryFile

    from ..transport import TransportError, get_bytes, get_client

    ingest = spec.ingest or {}
    img = ingest.get("imageserver", {})
    bbox = tuple(params["bbox"])

    # service name resolved from a request param (the layer -> ImageServer map).
    svc_cfg = img.get("service_by_param", {})
    svc_param = svc_cfg.get("param")
    svc_map = svc_cfg.get("map", {})
    service = svc_map.get(params.get(svc_param))
    if service is None:
        # A param outside the map is an input defect (mirrors the twin's layer
        # guard); the enum gate already rejected it, so this is defense-in-depth.
        raise router_upstream_error(
            spec.error_code_prefix, f"no ImageServer service for {svc_param}={params.get(svc_param)!r}"
        )

    endpoint = spec.endpoints.get("data") or next(iter(spec.endpoints.values()))
    base = (endpoint.url or endpoint.url_template or "").rstrip("/")
    url = f"{base}/{service}/ImageServer/exportImage"

    width_px, height_px = _imageserver_size(bbox, img)
    query = dict(img.get("export_query", {}))
    query["bbox"] = f"{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}"
    query["size"] = f"{width_px},{height_px}"

    ua = spec.auth.user_agent if spec.auth else "trid3nt_default"
    try:
        body, content_type, _ = get_bytes(
            get_client(), url, headers={"User-Agent": ua}, params=query
        )
    except TransportError as exc:
        raise router_upstream_error(
            spec.error_code_prefix, f"ImageServer request failed url={url}: {exc}"
        )

    ct = (content_type or "").lower()
    if "json" in ct or body[:1] == b"{":
        raise router_upstream_error(
            spec.error_code_prefix,
            f"ImageServer returned a JSON error for {svc_param}={params.get(svc_param)!r} "
            f"bbox={bbox}: {body[:400]!r}",
        )
    if not (body.startswith(b"II*\x00") or body.startswith(b"MM\x00*")):
        raise router_upstream_error(
            spec.error_code_prefix,
            f"ImageServer body is not a TIFF for {svc_param}={params.get(svc_param)!r} "
            f"bbox={bbox}; content-type={ct!r}, body preview: {body[:200]!r}",
        )

    # All-nodata coverage gate: every pixel the nodata sentinel (or the all-zero
    # degenerate over open water) -> the bbox missed coverage -> typed EMPTY.
    sentinel = img.get("nodata_sentinel")
    zero_is_empty = bool(img.get("zero_is_nodata", False))
    if sentinel is not None:
        try:
            with MemoryFile(body) as mem, mem.open() as src:
                arr = src.read(1)
                nod = src.nodata
                nod = int(nod) if nod is not None else int(sentinel)
                empty = bool((arr == nod).all() or (arr == int(sentinel)).all())
                if not empty and zero_is_empty:
                    empty = bool((arr == 0).all())
        except Exception:  # noqa: BLE001 -- unreadable body -> treat as data present
            empty = False
        if empty:
            raise router_empty_error(
                spec.error_code_prefix,
                f"ImageServer returned an all-nodata raster for {svc_param}="
                f"{params.get(svc_param)!r} bbox={bbox}; bbox likely outside coverage.",
                spec.empty_error_suffix,
            )
    return body


def _pc_sign_two_tier(spec: SourceSpec, href: str, collection: str) -> str:
    """Sign a PC asset href: per-href sign endpoint PRIMARY, token path FALLBACK.

    The MODIS/Copernicus blob accounts (``modiseuwest`` / ``elevationeuwest``) are
    NOT authorized by the per-collection token, so the storage-account-aware per-
    href ``/api/sas/v1/sign`` endpoint is primary; on any failure fall back to the
    shared per-collection token path. The transport owns the socket (ADR-0044).
    """
    from ...imagery import _pc_stac
    from ..transport import get_bytes, get_client

    sign_url = "https://planetarycomputer.microsoft.com/api/sas/v1/sign"
    try:
        body, _ct, _u = get_bytes(
            get_client(), sign_url, headers={"User-Agent": _pc_stac.USER_AGENT},
            params={"href": href},
        )
        import json
        signed = json.loads(body).get("href")
        if signed and isinstance(signed, str):
            return signed
    except Exception:  # noqa: BLE001 -- fall back to the per-collection token path
        pass
    return _pc_stac.sas_sign_href(href, collection)


def _normalize_via_aliases(spec: SourceSpec, value: Any, aliases: dict, allowed: list,
                           suffix: str) -> str:
    """Alias-normalize a param (lower->table, else upper) + validate membership.

    Reproduces the modis ``_normalize_product`` / ``_normalize_daynight`` contract:
    a known alias maps to canonical; an unknown value stamps a typed INPUT error.
    """
    key = str(value).strip().lower()
    norm = aliases.get(key, str(value).strip().upper())
    if norm not in allowed:
        raise router_input_error(
            spec.error_code_prefix, f"{value!r} not in {sorted(allowed)}", suffix
        )
    return norm


def _stac_float_to_array(spec: SourceSpec, params: dict[str, Any]) -> tuple[Any, Any, Any]:
    """Continuous-FLOAT STAC read: search -> select -> sign -> windowed reproject
    -> DN scale/offset -> float32 (modis_lst). Two selection modes:
    ``latest`` (most-recent single item) or ``intersect_all`` (first-valid mosaic).

    Output is a physical-scalar float32 array (NaN fill), NOT a categorical uint8
    mosaic -- the raster's ``execute`` default path serializes it with NaN nodata.
    """
    import numpy as np
    import rasterio
    from rasterio.warp import Resampling, reproject

    from ...imagery import _pc_stac
    from ..transport import TransportError, open_windowed_cog

    ingest = spec.ingest or {}
    stac = ingest.get("stac", {})
    tf = ingest.get("transform", {})
    bbox = tuple(params["bbox"])

    # --- collection + asset resolution (static or param-keyed, with aliasing) ---
    prod_norm = None
    cbp = stac.get("collection_by_param")
    if cbp:
        prod_raw = params.get(cbp["param"])
        prod_norm = _normalize_via_aliases(
            spec, prod_raw, stac.get("product_aliases", {}),
            list((cbp.get("map") or {}).keys()), stac.get("param_error_suffix", "PARAM_INVALID"))
        collection = cbp["map"][prod_norm]
    else:
        collection = stac.get("collection")

    abp = stac.get("asset_by_params")
    if abp:
        dn_norm = _normalize_via_aliases(
            spec, params.get(abp["params"][1]), stac.get("daynight_aliases", {}),
            ["day", "night"], stac.get("param_error_suffix", "PARAM_INVALID"))
        asset_key = abp["map"][prod_norm][dn_norm]
    else:
        asset_key = stac.get("data_asset", "data")

    # --- datetime window (both bounds given -> that window, else trailing N days) ---
    dt_range = None
    if stac.get("datetime_window"):
        d0, d1 = params.get("start_date"), params.get("end_date")
        if d0 and d1:
            dt_range = f"{d0}/{d1}"
        else:
            from datetime import datetime, timedelta, timezone
            days = int(stac.get("default_window_days", 120))
            end = datetime.now(timezone.utc).date()
            dt_range = f"{(end - timedelta(days=days)).isoformat()}/{end.isoformat()}"

    # --- search ---
    try:
        from pystac_client import Client
    except ImportError as exc:  # pragma: no cover
        raise router_upstream_error(spec.error_code_prefix, f"pystac-client unavailable: {exc}")
    try:
        client = Client.open(stac.get("root", _pc_stac.PC_STAC_ROOT))
        search = client.search(collections=[collection], bbox=list(bbox),
                               datetime=dt_range, limit=100)
        items = list(search.items())
    except Exception as exc:  # noqa: BLE001
        raise router_upstream_error(
            spec.error_code_prefix,
            f"STAC search failed (collection={collection!r}, bbox={bbox}, window={dt_range}): {exc}")
    if not items:
        raise router_empty_error(
            spec.error_code_prefix, f"no {collection!r} item intersects bbox={bbox} in {dt_range}",
            spec.empty_error_suffix)

    native_cell_m = float(ingest.get("native_cell_m", 1000.0))
    width_px, height_px = _pc_stac.bbox_pixel_dims(bbox, native_cell_m)
    dst_transform = rasterio.transform.from_bounds(bbox[0], bbox[1], bbox[2], bbox[3], width_px, height_px)

    scale = tf.get("scale")
    offset = tf.get("offset", 0.0)
    fill_dn = tf.get("fill_dn")
    src_nodata = tf.get("src_nodata")
    select = stac.get("select", "latest")

    def _read_item(item: Any) -> Any:
        assets = getattr(item, "assets", {}) or {}
        if asset_key not in assets:
            raise router_upstream_error(
                spec.error_code_prefix,
                f"item {getattr(item, 'id', '?')} missing asset {asset_key!r} (have {sorted(assets)[:12]})")
        signed = _pc_sign_two_tier(spec, assets[asset_key].href, collection)
        init = 0.0 if fill_dn is not None else np.nan
        dst = np.full((height_px, width_px), init, dtype="float32")
        try:
            with open_windowed_cog(signed) as src:
                reproject(
                    source=rasterio.band(src, 1), destination=dst,
                    src_transform=src.transform, src_crs=src.crs,
                    dst_transform=dst_transform, dst_crs="EPSG:4326",
                    resampling=Resampling.bilinear,
                    src_nodata=(src_nodata if src_nodata is not None else src.nodata),
                    dst_nodata=init,
                )
        except TransportError as exc:
            raise router_upstream_error(spec.error_code_prefix, f"asset read failed: {exc}")
        except Exception as exc:  # noqa: BLE001
            raise router_upstream_error(spec.error_code_prefix, f"asset read failed: {exc}")
        return dst

    if select == "latest":
        def _dt_key(it: Any) -> str:
            p = getattr(it, "properties", {}) or {}
            return p.get("datetime") or p.get("end_datetime") or p.get("start_datetime") or ""
        items.sort(key=_dt_key, reverse=True)
        raw = _read_item(items[0])
    else:  # intersect_all: first-valid mosaic
        raw = np.full((height_px, width_px), np.nan, dtype="float32")
        for item in items:
            tile = _read_item(item)
            fill = np.isnan(raw) & np.isfinite(tile)
            raw[fill] = tile[fill]

    # --- DN scale/offset -> physical; fill -> NaN ---
    if scale is not None:
        out = raw * float(scale) + float(offset)
        if fill_dn is not None:
            out = np.where(raw == fill_dn, np.nan, out)
        out = out.astype("float32")
    else:
        out = raw.astype("float32")

    if not bool(np.isfinite(out).any()):
        raise router_empty_error(
            spec.error_code_prefix, f"all-fill (no valid pixel) window over bbox={bbox}",
            spec.empty_error_suffix)
    return out, dst_transform, "EPSG:4326"


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
    nodata read-back). Local paths (cached/test COGs) open directly; remote https
    hrefs read through the httpx transport (ADR-0044: the transport owns the
    socket, GDAL parses only -- the /vsicurl/ residual the ingest-transport
    decision named). Returns ``(uint8 (H,W), colormap|None)``.
    """
    import numpy as np
    import rasterio
    from rasterio.warp import reproject, Resampling

    from ..transport import TransportError, open_windowed_cog

    def _warp_from(src: Any) -> tuple[Any, dict | None]:
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

    remote = signed_href.startswith(("http://", "https://")) and not os.path.exists(signed_href)
    try:
        if remote:
            # httpx transport owns the socket; the coalescing range opener feeds
            # GDAL, which never networks. A missing object / 403 / 5xx surfaces as
            # a typed TransportError rather than an opaque RasterioIOError.
            with open_windowed_cog(signed_href) as src:
                return _warp_from(src)
        with rasterio.open(signed_href) as src:
            return _warp_from(src)
    except TransportError as exc:
        raise router_upstream_error(
            spec.error_code_prefix,
            f"STAC tile read failed (href={signed_href[:120]!r}): {exc}",
        )
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
    if access == "imageserver_export":
        # The ImageServer exportImage response IS the artifact (no reserialize) --
        # value-identical to the twin's raw GeoTIFF body.
        return _imageserver_export_bytes(spec, params)
    if access == "stac_search":
        ingest = spec.ingest or {}
        arr, transform, crs, colormap = stac_to_mosaic(spec, params)
        nodata = int((ingest.get("mosaic") or {}).get("nodata", 0))
        dtype = str(ingest.get("dtype", "uint8"))
        return array_to_cog_bytes(
            arr, transform, crs, nodata=nodata, dtype=dtype, colormap=colormap
        )
    arr, transform, crs = fetch_source_array(spec, params)
    # serialize directive (wave-8): a float source that writes a NON-NaN nodata
    # sentinel (copernicus_dem: fill NaN -> -9999, nodata=-9999) declares it here.
    # Absent (every prior float spec) -> NaN-nodata passthrough (modis parity).
    ser = (spec.ingest or {}).get("serialize") or {}
    out_nodata = ser.get("nodata")
    if out_nodata is not None:
        import numpy as np

        out_dtype = str(ser.get("dtype", "float32"))
        filled = np.where(np.isfinite(arr), arr, out_nodata).astype(out_dtype)
        return array_to_cog_bytes(
            filled, transform, crs, nodata=float(out_nodata), dtype=out_dtype
        )
    return array_to_cog_bytes(arr, transform, crs)
