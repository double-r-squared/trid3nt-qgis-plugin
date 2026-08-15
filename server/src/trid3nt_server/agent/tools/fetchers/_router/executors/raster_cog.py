"""raster-cog executor (contract sec 2.1).

Reads a gridded source to a CRS-tagged single-band COG. Three sub-modes keyed by
``ingest.access``:
  - ``opendap``       xarray subset + time_reduce collapse  (gridmet)
  - ``direct_window`` httpx-transport windowed read of a known COG/VRT
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
    router_not_available_error,
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
    nodata: float | None = float("nan"),
    dtype: str = "float32",
    colormap: dict | None = None,
    colorinterp: str | None = None,
) -> bytes:
    """Serialize a 2D (or multi-band 3D) array to COG bytes (pure, offline).

    North-up is the caller's responsibility (the transform already carries a
    negative y-step). CRS is re-asserted on the profile after the astype so the
    geographic-correctness gate never sees a dropped CRS (codified lesson).

    ``colormap`` (a GDAL ``{class_code: (r,g,b,a)}`` table) bakes a categorical
    palette into band 1 with ``ColorInterp.palette`` -- byte-identical to the
    esri_landcover twin's ``_write_palette_cog`` so ``publish_layer`` colorizes
    from the embedded table (categorical passthrough, no rescale).

    ``colorinterp="rgba"`` tags a 4-band uint8 array as red/green/blue/alpha so
    ``publish_layer`` renders a server-symbolized overlay's baked palette directly
    (the mapserver_export siblings' transparent RGBA raster). ``nodata=None`` omits
    the nodata tag from the profile (an RGBA overlay carries transparency in the
    alpha band, not a nodata sentinel). Both default to the single-band float32
    behaviour -- strictly no-op for every prior caller.
    """
    import numpy as np
    import rasterio

    arr = np.asarray(array, dtype=dtype)
    if arr.ndim == 2:
        arr = arr[np.newaxis, :, :]
    if arr.ndim != 3:
        raise ValueError(f"array must be 2D or multi-band 3D; got shape {arr.shape}")
    count, height, width = arr.shape

    def _write_palette(dst: Any) -> None:
        if colormap is not None:
            dst.write_colormap(1, colormap)
        try:
            from rasterio.enums import ColorInterp

            interp = list(dst.colorinterp)
            if colormap is not None:
                interp[0] = ColorInterp.palette
            if colorinterp == "rgba" and count == 4:
                interp[:4] = [ColorInterp.red, ColorInterp.green,
                              ColorInterp.blue, ColorInterp.alpha]
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
            "compress": "DEFLATE",
        }
        if nodata is not None:
            base_profile["nodata"] = nodata
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
    if access == "multi_url":
        return _multi_url_to_array(spec, params)
    if access == "projected_vrt_window":
        return _projected_vrt_window_to_array(spec, params)
    if access == "gzip_object":
        return _gzip_object_to_array(spec, params)
    if access == "grib_object":
        return _grib_object_to_array(spec, params)
    if access == "griddap":
        return _griddap_to_array(spec, params)
    if access == "fixed_tile_grid":
        return _fixed_tile_grid_to_array(spec, params)
    if access == "categorical_tile_grid":
        return _categorical_tile_grid_to_array(spec, params)
    if access == "stac_search":
        return _stac_to_array(spec, params)
    if access == "stac_float":
        return _stac_float_to_array(spec, params)
    if access == "library_delegate":
        # the delegate hook owns the library socket and returns
        # (array, transform, crs); the constrained invoke wrapper (declared
        # timeout + telemetry + upstream-error backstop) is the impurity boundary.
        from . import library_delegate

        return library_delegate.invoke(spec, params)
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

    Reads via the coalescing/parallel range opener -- GDAL never
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


# --------------------------------------------------------------------------- #
# multi_url (VRT fan-out): a mosaic source declared over MANY member URLs. The
# single-URL opener serves ONE object, so a multi-tile .vrt read returns all-NaN
# (it re-serves the VRT bytes for every sub-tile open); this mode resolves the
# member tiles, windows the intersecting ones through the SAME transport opener,
# and mosaics them into the requested window (the highest-leverage enabler).
# Member discovery is pluggable (``mode: vrt`` today) so a future
# declared-tile-grid source reuses the identical windowed-mosaic read path.
# --------------------------------------------------------------------------- #


class _VrtSource:
    """One VRT member: its object URL + src/dst pixel rects (contract sec 2.1)."""

    __slots__ = ("url", "sx", "sy", "sw", "sh", "dx", "dy", "dw", "dh")

    def __init__(self, url: str, src: tuple[int, int, int, int],
                 dst: tuple[int, int, int, int]) -> None:
        self.url = url
        self.sx, self.sy, self.sw, self.sh = src
        self.dx, self.dy, self.dw, self.dh = dst


def _parse_vrt(vrt_xml: bytes, base_url: str) -> tuple[Any, int, int, Any, float, list["_VrtSource"]]:
    """Parse a GDAL ``.vrt`` mosaic into ``(transform, xsize, ysize, crs, nodata, sources)``.

    Reads the mosaic geotransform / raster size / SRS / band NoDataValue and each
    ``(Simple|Complex)Source``'s ``SourceFilename`` + ``SrcRect`` + ``DstRect`` -- the
    exact fields GDAL uses to fan a windowed read out to the member tiles, so an
    explicit member-by-member read reproduces ``/vsicurl/`` value-for-value.
    """
    import xml.etree.ElementTree as ET

    import rasterio
    from rasterio.transform import Affine

    root = ET.fromstring(vrt_xml)
    xsize = int(root.attrib["rasterXSize"])
    ysize = int(root.attrib["rasterYSize"])
    gt_el = root.find("GeoTransform")
    if gt_el is None or not gt_el.text:
        raise ValueError("VRT carries no GeoTransform")
    gt = [float(v) for v in gt_el.text.replace(",", " ").split()]
    transform = Affine.from_gdal(*gt)
    srs_el = root.find("SRS")
    crs = rasterio.crs.CRS.from_wkt(srs_el.text) if (srs_el is not None and srs_el.text) else rasterio.crs.CRS.from_epsg(4326)
    band = root.find("VRTRasterBand")
    if band is None:
        raise ValueError("VRT carries no VRTRasterBand")
    nod_el = band.find("NoDataValue")
    nodata = float(nod_el.text) if (nod_el is not None and nod_el.text) else float("nan")

    base_dir = base_url.rsplit("/", 1)[0]
    sources: list[_VrtSource] = []
    for src_el in list(band.findall("ComplexSource")) + list(band.findall("SimpleSource")):
        fn_el = src_el.find("SourceFilename")
        if fn_el is None or not fn_el.text:
            continue
        rel = fn_el.attrib.get("relativeToVRT", "0") == "1"
        member = f"{base_dir}/{fn_el.text}" if rel else fn_el.text
        sr = src_el.find("SrcRect")
        dr = src_el.find("DstRect")
        if sr is None or dr is None:
            continue
        src = tuple(int(round(float(sr.attrib[k]))) for k in ("xOff", "yOff", "xSize", "ySize"))
        dst = tuple(int(round(float(dr.attrib[k]))) for k in ("xOff", "yOff", "xSize", "ySize"))
        sources.append(_VrtSource(member, src, dst))  # type: ignore[arg-type]
    return transform, xsize, ysize, crs, nodata, sources


def _resolve_multi_url_members(spec: SourceSpec, params: dict[str, Any]) -> tuple[Any, int, int, Any, float, list["_VrtSource"]]:
    """Resolve the mosaic grid + member tiles for a ``multi_url`` source.

    ``mode: vrt`` fetches the declared ``.vrt`` whole-object through the transport
    and parses it. The dispatch is isolated so a future ``mode: tile_grid`` can
    synthesize members from a declarative grid and reuse the identical read path.
    """
    from ..transport import TransportError, get_bytes, get_client

    ingest = spec.ingest or {}
    mu = ingest.get("multi_url", {})
    mode = mu.get("mode", "vrt")
    endpoint = spec.endpoints.get("data") or next(iter(spec.endpoints.values()))
    # A param-templated VRT URL (soilgrids: {property}/{property}_{depth}_mean.vrt)
    # is filled from params; a static url passes through unchanged (no-op for hrsl /
    # every prior multi_url spec, which declare a placeholder-free ``url``).
    url = endpoint.url or (endpoint.url_template.format(**params) if endpoint.url_template else "")
    if url.startswith("/vsicurl/"):
        url = url[len("/vsicurl/"):]
    if mode != "vrt":
        raise router_upstream_error(spec.error_code_prefix, f"unknown multi_url mode {mode!r}")
    ua = spec.auth.user_agent if spec.auth else "trid3nt_default"
    try:
        body, _ct, final_url = get_bytes(get_client(), url, headers={"User-Agent": ua})
    except TransportError as exc:
        raise router_upstream_error(spec.error_code_prefix, f"VRT fetch failed url={url}: {exc}")
    try:
        return _parse_vrt(body, final_url or url)
    except Exception as exc:  # noqa: BLE001 -- malformed VRT is an upstream defect
        raise router_upstream_error(spec.error_code_prefix, f"VRT parse failed url={url}: {exc}")


def _multi_url_to_array(spec: SourceSpec, params: dict[str, Any]) -> tuple[Any, Any, Any]:
    """VRT fan-out windowed mosaic read (; hrsl_population).

    Windows the mosaic to ``bbox`` (outward integer-pixel rounding, the twin's
    window math), reads each INTERSECTING member's sub-window through the coalescing
    transport opener (bounded parallel), and pastes the non-nodata pixels into the
    output window. An all-nodata window (over open water / off coverage) -> typed
    EMPTY; ANY intersecting-member read failure -> typed UPSTREAM (never a silent
    partial), matching the twin whose GDAL read fails the whole window.
    """
    from concurrent.futures import ThreadPoolExecutor

    import numpy as np
    import rasterio
    from rasterio.windows import Window
    from rasterio.windows import from_bounds as window_from_bounds

    from ..transport import (
        MAX_PARALLEL,
        TransportError,
        open_windowed_cog,
    )

    bbox = params["bbox"]
    transform, xsize, ysize, crs, nodata, sources = _resolve_multi_url_members(spec, params)

    # Window math reproduces the twin: from_bounds -> floor offsets, ceil lengths,
    # clip to the mosaic extent. A window with no mosaic overlap -> typed EMPTY.
    win = window_from_bounds(*bbox, transform=transform)
    win = win.round_offsets(op="floor").round_lengths(op="ceil")
    c0 = max(0, int(win.col_off))
    r0 = max(0, int(win.row_off))
    c1 = min(xsize, int(win.col_off) + int(win.width))
    r1 = min(ysize, int(win.row_off) + int(win.height))
    if c1 <= c0 or r1 <= r0:
        raise router_empty_error(
            spec.error_code_prefix,
            f"bbox={bbox} produces a zero-size mosaic window (outside coverage)",
            spec.empty_error_suffix)
    out_w, out_h = c1 - c0, r1 - r0
    out = np.full((out_h, out_w), nodata, dtype="float64")

    def _member_window(s: "_VrtSource") -> tuple["_VrtSource", int, int, int, int] | None:
        ox0, ox1 = max(c0, s.dx), min(c1, s.dx + s.dw)
        oy0, oy1 = max(r0, s.dy), min(r1, s.dy + s.dh)
        if ox1 <= ox0 or oy1 <= oy0:
            return None
        return s, ox0, oy0, ox1, oy1

    hits = [w for w in (_member_window(s) for s in sources) if w is not None]

    def _read_hit(hit: tuple["_VrtSource", int, int, int, int]) -> tuple[int, int, int, int, Any]:
        s, ox0, oy0, ox1, oy1 = hit
        # 1:1 src/dst mapping is the tiled-mosaic norm; scale by the rect ratio
        # otherwise so a non-1:1 VRT source still reads the correct sub-window.
        rx = s.sw / s.dw if s.dw else 1.0
        ry = s.sh / s.dh if s.dh else 1.0
        sx0 = s.sx + int(round((ox0 - s.dx) * rx))
        sy0 = s.sy + int(round((oy0 - s.dy) * ry))
        sw = max(1, int(round((ox1 - ox0) * rx)))
        sh = max(1, int(round((oy1 - oy0) * ry)))
        with open_windowed_cog(s.url) as src:
            tile = src.read(1, window=Window(sx0, sy0, sw, sh),
                            out_shape=(oy1 - oy0, ox1 - ox0)).astype("float64")
        return ox0, oy0, ox1, oy1, tile

    if hits:
        workers = min(MAX_PARALLEL, len(hits))
        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                reads = list(pool.map(_read_hit, hits))
        except TransportError as exc:
            raise router_upstream_error(spec.error_code_prefix, f"VRT member read failed: {exc}")
        except Exception as exc:  # noqa: BLE001 -- any member read failure -> upstream
            raise router_upstream_error(spec.error_code_prefix, f"VRT member read failed: {exc}")
        for ox0, oy0, ox1, oy1, tile in reads:
            valid = (tile == tile) if nodata != nodata else (tile != nodata)
            dst_slice = out[oy0 - r0:oy1 - r0, ox0 - c0:ox1 - c0]
            dst_slice[valid] = tile[valid]

    # all-nodata gate (honesty floor): a window with no valid pixel is honest
    # no-coverage (over open water / off the mosaic), never a fabricated layer.
    valid_any = bool(np.isfinite(out).any()) if nodata != nodata else bool((out != nodata).any())
    if not valid_any:
        raise router_empty_error(
            spec.error_code_prefix,
            f"bbox={bbox} produced no valid pixels (all-nodata window -- over open "
            f"water or outside coverage)", spec.empty_error_suffix)

    out_transform = rasterio.windows.transform(Window(c0, r0, out_w, out_h), transform)
    return np.asarray(out, dtype="float32"), out_transform, crs


# --------------------------------------------------------------------------- #
# projected_vrt_window: a VRT mosaic in a NON-4326 projected CRS (SoilGrids'
# Interrupted Goode Homolosine 250 m grid). Unlike multi_url (which
# windows a 4326 VRT directly and returns the native array), this transform_bounds
# the 4326 bbox INTO the source CRS (densified), windows the native grid (the twin's
# floor/ceil + pad), reads the intersecting members through the SAME coalescing
# transport opener, reprojects the native window -> EPSG:4326 (bilinear, the twin's
# target-res grid), and applies a per-property Int16->physical scale divisor. NaN
# fill; the serialize directive (nodata=-9999) writes the float32 COG. STRICT no-op
# for every prior raster spec (a distinct access mode).
# --------------------------------------------------------------------------- #


def _projected_vrt_window_to_array(spec: SourceSpec, params: dict[str, Any]) -> tuple[Any, Any, Any]:
    """Projected VRT window read + native->4326 reproject + per-property scale.

    Reproduces the fetch_soilgrids twin ``_fetch_soilgrids_bytes`` value-for-value:
    the 4326 bbox -> Homolosine bounds (densified), the floor/ceil + 2 px pad native
    window, the intersecting-member mosaic, the bilinear reproject to the ~250 m
    EPSG:4326 target grid, and the fixed-point Int16 -> physical scale (NaN fill).
    An all-nodata window (ocean / off the soil land surface) -> typed EMPTY.
    """
    from concurrent.futures import ThreadPoolExecutor

    import numpy as np
    import rasterio
    from rasterio.warp import Resampling, reproject, transform_bounds
    from rasterio.windows import Window
    from rasterio.windows import from_bounds as window_from_bounds

    from ..transport import MAX_PARALLEL, TransportError, open_windowed_cog

    ingest = spec.ingest or {}
    pw = ingest.get("projected_window", {})
    bbox = tuple(params["bbox"])

    # fast-reject outside the source's global land coverage envelope (honest empty).
    cov = pw.get("coverage_bbox")
    if cov and not (bbox[0] <= cov[2] and bbox[2] >= cov[0]
                    and bbox[1] <= cov[3] and bbox[3] >= cov[1]):
        raise router_empty_error(
            spec.error_code_prefix,
            f"bbox={bbox} falls outside coverage {tuple(cov)}", spec.empty_error_suffix)

    transform, xsize, ysize, crs, nodata, sources = _resolve_multi_url_members(spec, params)
    src_nodata = int(nodata) if nodata == nodata else int(pw.get("src_nodata", -32768))

    # 4326 bbox -> source (projected) bounds; densify so the curved edges are captured.
    densify = int(pw.get("densify_pts", 21))
    l, b, r, t = transform_bounds("EPSG:4326", crs, *bbox, densify_pts=densify)
    win = window_from_bounds(l, b, r, t, transform=transform)
    win = win.round_offsets(op="floor").round_lengths(op="ceil")
    pad = int(pw.get("pad_px", 2))
    win = Window(win.col_off - pad, win.row_off - pad,
                 win.width + 2 * pad, win.height + 2 * pad)
    c0 = max(0, int(win.col_off))
    r0 = max(0, int(win.row_off))
    c1 = min(xsize, int(win.col_off) + int(win.width))
    r1 = min(ysize, int(win.row_off) + int(win.height))
    if c1 <= c0 or r1 <= r0:
        raise router_empty_error(
            spec.error_code_prefix,
            f"bbox={bbox} produces a zero-size window (outside coverage)",
            spec.empty_error_suffix)
    max_px = int(pw.get("max_window_pixels", 20_000 * 20_000))
    if (c1 - c0) * (r1 - r0) > max_px:
        err = router_input_error(
            spec.error_code_prefix,
            f"bbox={bbox} would request {(c1 - c0) * (r1 - r0):,} native pixels "
            f"(refuse > {max_px:,}; narrow the bbox)", spec.input_error_suffix)
        raise err
    out_w, out_h = c1 - c0, r1 - r0
    native = np.full((out_h, out_w), src_nodata, dtype="int16")

    def _member_window(s: "_VrtSource") -> tuple["_VrtSource", int, int, int, int] | None:
        ox0, ox1 = max(c0, s.dx), min(c1, s.dx + s.dw)
        oy0, oy1 = max(r0, s.dy), min(r1, s.dy + s.dh)
        if ox1 <= ox0 or oy1 <= oy0:
            return None
        return s, ox0, oy0, ox1, oy1

    hits = [w for w in (_member_window(s) for s in sources) if w is not None]

    def _read_hit(hit: tuple["_VrtSource", int, int, int, int]) -> tuple[int, int, int, int, Any]:
        s, ox0, oy0, ox1, oy1 = hit
        rx = s.sw / s.dw if s.dw else 1.0
        ry = s.sh / s.dh if s.dh else 1.0
        sx0 = s.sx + int(round((ox0 - s.dx) * rx))
        sy0 = s.sy + int(round((oy0 - s.dy) * ry))
        sw = max(1, int(round((ox1 - ox0) * rx)))
        sh = max(1, int(round((oy1 - oy0) * ry)))
        with open_windowed_cog(s.url) as src:
            tile = src.read(1, window=Window(sx0, sy0, sw, sh),
                            out_shape=(oy1 - oy0, ox1 - ox0))
        return ox0, oy0, ox1, oy1, tile

    if hits:
        workers = min(MAX_PARALLEL, len(hits))
        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                reads = list(pool.map(_read_hit, hits))
        except TransportError as exc:
            raise router_upstream_error(spec.error_code_prefix, f"VRT member read failed: {exc}")
        except Exception as exc:  # noqa: BLE001 -- any member read failure -> upstream
            raise router_upstream_error(spec.error_code_prefix, f"VRT member read failed: {exc}")
        for ox0, oy0, ox1, oy1, tile in reads:
            native[oy0 - r0:oy1 - r0, ox0 - c0:ox1 - c0] = tile

    native_transform = rasterio.windows.transform(Window(c0, r0, out_w, out_h), transform)

    # Reproject the native window -> EPSG:4326 target grid (the twin's res).
    target_res = float(pw.get("target_res_deg", 0.0025))
    dst_w = max(1, int(round((bbox[2] - bbox[0]) / target_res)))
    dst_h = max(1, int(round((bbox[3] - bbox[1]) / target_res)))
    dst_transform = rasterio.transform.from_bounds(bbox[0], bbox[1], bbox[2], bbox[3], dst_w, dst_h)
    reproj = np.full((dst_h, dst_w), src_nodata, dtype="int16")
    reproject(
        native, reproj,
        src_transform=native_transform, src_crs=crs,
        dst_transform=dst_transform, dst_crs="EPSG:4326",
        src_nodata=src_nodata, dst_nodata=src_nodata,
        resampling=Resampling.bilinear,
    )
    valid = reproj != src_nodata
    if not bool(valid.any()):
        raise router_empty_error(
            spec.error_code_prefix,
            f"bbox={bbox} produced no valid pixels (all-nodata window -- over open "
            f"water or off the soil land surface)", spec.empty_error_suffix)

    # Per-property fixed-point Int16 -> physical units; nodata -> NaN (the serialize
    # directive fills NaN -> the -9999 float sentinel).
    scale_div = 1.0
    sbp = pw.get("scale_by_param")
    if sbp:
        scale_div = float((sbp.get("map") or {})[params.get(sbp.get("param"))])
    out = np.full((dst_h, dst_w), np.nan, dtype="float32")
    out[valid] = reproj[valid].astype("float32") / scale_div
    return out, dst_transform, "EPSG:4326"


# --------------------------------------------------------------------------- #
# gzip_object: a whole-object GET of a date-templated ``.tif.gz``, gunzip, in-
# memory open + window. A gzip stream is NOT a byte-servable COG (no windowable
# layout), so the whole-object cost is accepted and gated honestly by the payload
# estimator; ``bbox=None`` reads the full grid (supports_global_query).,
# chirps_precipitation.
# --------------------------------------------------------------------------- #


def _resolve_gzip_url(spec: SourceSpec, params: dict[str, Any], go: dict[str, Any]) -> str:
    """Build the date-templated object URL for a ``gzip_object`` source.

    Period-selected template (chirps monthly vs daily path patterns) filled from a
    parsed ``date`` param. A template that references ``{day}`` requires a full
    ``YYYY-MM-DD``; a monthly template accepts ``YYYY-MM`` (or ``YYYY-MM-DD``, day
    ignored). Coverage bounds (``min_year`` floor, no-future) raise a typed INPUT
    error -- the twin's pre-network date validation, reproduced.
    """
    import re
    from datetime import date as _date
    from datetime import datetime, timezone

    endpoint = spec.endpoints.get("data") or next(iter(spec.endpoints.values()))
    base = (endpoint.url or endpoint.url_template or "").rstrip("/")
    templates = go.get("url_templates", {})
    period = params.get(go.get("period_param", "period"))
    tmpl = templates.get(period)
    if tmpl is None:
        raise router_input_error(
            spec.error_code_prefix, f"no URL template for period={period!r}", spec.input_error_suffix)
    date_str = params.get(go.get("date_param", "date"))
    if not isinstance(date_str, str) or not date_str.strip():
        raise router_input_error(
            spec.error_code_prefix, f"date must be a non-empty string; got {date_str!r}", spec.input_error_suffix)
    needs_day = "{day" in tmpl
    s = date_str.strip()
    if needs_day:
        m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s)
        if not m:
            raise router_input_error(spec.error_code_prefix, f"date={date_str!r} is not a valid {period} date: expected YYYY-MM-DD", spec.input_error_suffix)
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = re.fullmatch(r"(\d{4})-(\d{2})(?:-\d{2})?", s)
        if not m:
            raise router_input_error(spec.error_code_prefix, f"date={date_str!r} is not a valid {period} date: expected YYYY-MM or YYYY-MM-DD", spec.input_error_suffix)
        y, mo, d = int(m.group(1)), int(m.group(2)), 1
    try:
        parsed = _date(y, mo, d)
    except ValueError as exc:
        raise router_input_error(spec.error_code_prefix, f"date={date_str!r} is not a valid {period} date: {exc}", spec.input_error_suffix)
    min_year = int(go.get("min_year", 0))
    if parsed.year < min_year:
        raise router_input_error(spec.error_code_prefix, f"source record starts in {min_year}; date={date_str!r} predates it", spec.input_error_suffix)
    if parsed > datetime.now(timezone.utc).date():
        raise router_input_error(spec.error_code_prefix, f"date={date_str!r} is in the future; only past data is published", spec.input_error_suffix)
    return tmpl.format(base=base, year=y, month=mo, day=d)


def _gzip_object_to_array(spec: SourceSpec, params: dict[str, Any]) -> tuple[Any, Any, Any]:
    """Whole-object GET + gunzip + in-memory window (; chirps_precipitation).

    Reads the whole ``.tif.gz`` through the transport (accepting the whole-object
    cost -- a gzip stream is not windowed-servable), gunzips, opens in memory, and
    windows to ``bbox`` (``None`` -> the full grid). A source-embedded nodata
    sentinel (``arr <= threshold``) collapses to NaN; an all-nodata window ->
    typed EMPTY. A 404 -> typed NOT_AVAILABLE (the date is unpublished); any other
    fetch/gunzip failure -> typed UPSTREAM.
    """
    import gzip
    import math

    import numpy as np
    import rasterio
    from rasterio.io import MemoryFile
    from rasterio.windows import Window
    from rasterio.windows import from_bounds as window_from_bounds

    from ..transport import (
        TransportError,
        TransportNotFound,
        get_bytes,
        get_client,
    )

    ingest = spec.ingest or {}
    go = ingest.get("gzip_object", {})
    bbox = params.get("bbox")
    url = _resolve_gzip_url(spec, params, go)
    ua = spec.auth.user_agent if spec.auth else "trid3nt_default"
    try:
        gz_bytes, _ct, _u = get_bytes(get_client(), url, headers={"User-Agent": ua})
    except TransportNotFound as exc:
        raise router_not_available_error(
            spec.error_code_prefix,
            f"no raster published at {url} (HTTP 404) -- the date may be too recent or outside the record: {exc}")
    except TransportError as exc:
        raise router_upstream_error(spec.error_code_prefix, f"object fetch failed url={url}: {exc}")
    if not gz_bytes:
        raise router_upstream_error(spec.error_code_prefix, f"empty response from {url}")
    try:
        tif_bytes = gzip.decompress(gz_bytes)
    except (OSError, gzip.BadGzipFile) as exc:
        raise router_upstream_error(spec.error_code_prefix, f"gzip decompression failed for {url}: {exc}")

    with MemoryFile(tif_bytes) as mf, mf.open() as src:
        src_crs = src.crs or rasterio.crs.CRS.from_epsg(4326)
        if bbox is not None:
            window = window_from_bounds(bbox[0], bbox[1], bbox[2], bbox[3], transform=src.transform)
            row_off = max(0, int(math.floor(window.row_off)))
            col_off = max(0, int(math.floor(window.col_off)))
            row_end = min(src.height, int(math.ceil(window.row_off + window.height)))
            col_end = min(src.width, int(math.ceil(window.col_off + window.width)))
            if row_end <= row_off or col_end <= col_off:
                raise router_empty_error(
                    spec.error_code_prefix, f"bbox={bbox} does not intersect the source extent",
                    spec.empty_error_suffix)
            rw = Window(col_off, row_off, col_end - col_off, row_end - row_off)
            arr = src.read(1, window=rw).astype("float32")
            out_transform = src.window_transform(rw)
        else:
            arr = src.read(1).astype("float32")
            out_transform = src.transform

    sentinel = go.get("nodata_sentinel")
    if sentinel is not None:
        arr = np.where(arr <= float(sentinel), np.nan, arr).astype("float32")
    if not bool(np.isfinite(arr).any()):
        raise router_empty_error(
            spec.error_code_prefix,
            f"bbox={bbox} clipped to all-nodata (ocean / outside land coverage); no valid pixels",
            spec.empty_error_suffix)
    return arr, out_transform, src_crs


# --------------------------------------------------------------------------- #
# grib_object: a whole-object GET of a resolved ``.grib2(.gz)`` key, gunzip, GRIB
# decode (the GRIB driver needs a real path -- a MemoryFile cannot host its
# tabular index -- so the bytes land in a tempfile), a source-grid bbox window,
# a sentinel->nodata collapse, and a conditional reproject to EPSG:4326. The
# gzip_object precedent at GRIB scale: GRIB is whole-object by nature
# (no byte-range windowing), so the whole-object cost is accepted + payload-gated,
# and the decode receiving whole bytes is pure. The S3-listed key is resolved
# pre-cache-key by the resolve phase (mrms_qpe hooks) and merged into
# params, so this mode only reads params[key_param] and never lists. NOAA MRMS QPE.
# --------------------------------------------------------------------------- #


def _grib_object_to_array(spec: SourceSpec, params: dict[str, Any]) -> tuple[Any, Any, Any]:
    """Whole-object GRIB GET + gunzip + windowed decode + sentinel-nodata.

    Reproduces the MRMS twin ``_grib2_to_geotiff`` value-for-value: read band 1 as
    float32, collapse the source sentinels (``sentinel_equals`` list + ``sentinel_below``
    floor) to the ``nodata`` value, clip to ``bbox`` on the SOURCE grid (floor-offset
    / ceil-length / clip-to-extent -- cheaper + integrity-safe since the source CRS
    is also geographic), then reproject to EPSG:4326 ONLY when the decoded CRS is not
    already 4326 (calculate_default_transform + nearest, nodata-preserving). ``bbox=None``
    reads the full grid (supports_global_query). A window off the source extent -> typed
    EMPTY; a 404 (the key vanished between resolve + fetch) -> typed NOT_AVAILABLE; any
    other fetch / gunzip / decode failure -> typed UPSTREAM. The returned array carries
    the ``nodata`` sentinel in-band; ``execute``'s ``serialize`` block (nodata=<same>)
    writes it through unchanged (every pixel is finite, so the fill is a no-op).
    """
    import gzip
    import math

    import numpy as np
    import rasterio
    from rasterio.crs import CRS
    from rasterio.transform import Affine
    from rasterio.warp import Resampling, calculate_default_transform, reproject
    from rasterio.windows import from_bounds as window_from_bounds

    from ..transport import (
        TransportError,
        TransportNotFound,
        get_bytes,
        get_client,
    )

    ingest = spec.ingest or {}
    go = ingest.get("grib_object", {})
    bbox = params.get("bbox")
    nodata = float(go.get("nodata", -9999.0))
    sentinel_equals = [float(v) for v in go.get("sentinel_equals", [])]
    sentinel_below = go.get("sentinel_below")

    key = params.get(go.get("key_param", "_grib_key"))
    if not isinstance(key, str) or not key:
        raise router_upstream_error(
            spec.error_code_prefix, "grib_object: no resolved object key (resolve phase produced none)")
    endpoint = spec.endpoints.get("data") or next(iter(spec.endpoints.values()))
    base = (endpoint.url or endpoint.url_template or "").rstrip("/")
    url = f"{base}/{key.lstrip('/')}"
    ua = spec.auth.user_agent if spec.auth else "trid3nt_default"

    try:
        blob, _ct, _u = get_bytes(get_client(), url, headers={"User-Agent": ua})
    except TransportNotFound as exc:
        raise router_not_available_error(
            spec.error_code_prefix,
            f"no GRIB object at {url} (HTTP 404) -- the resolved key may have rolled out of the bucket: {exc}")
    except TransportError as exc:
        raise router_upstream_error(spec.error_code_prefix, f"GRIB object fetch failed url={url}: {exc}")
    if not blob:
        raise router_upstream_error(spec.error_code_prefix, f"empty response from {url}")
    if bool(go.get("gzip", True)):
        try:
            grib_bytes = gzip.decompress(blob)
        except (OSError, gzip.BadGzipFile) as exc:
            raise router_upstream_error(spec.error_code_prefix, f"gzip decompression failed for {url}: {exc}")
    else:
        grib_bytes = blob

    tmp_grib = None
    try:
        fd, tmp_grib = tempfile.mkstemp(suffix=".grib2", prefix="trid3nt_router_grib_")
        os.close(fd)
        with open(tmp_grib, "wb") as gf:
            gf.write(grib_bytes)
        with rasterio.open(tmp_grib) as src:
            src_crs = src.crs
            src_transform = src.transform
            src_height, src_width = src.shape
            arr = src.read(1).astype("float32")
    except RouterError:
        raise
    except Exception as exc:  # noqa: BLE001 -- GRIB decode failure is an upstream defect
        raise router_upstream_error(spec.error_code_prefix, f"GRIB decode failed url={url}: {exc}")
    finally:
        if tmp_grib is not None:
            try:
                os.unlink(tmp_grib)
            except OSError:
                pass

    # Sentinel collapse -> nodata (MRMS: -3 no-precip, -1 missing, plus a floor).
    mask = np.zeros(arr.shape, dtype=bool)
    for sv in sentinel_equals:
        mask |= (arr == sv)
    if sentinel_below is not None:
        mask |= (arr < float(sentinel_below))
    arr = np.where(mask, nodata, arr).astype("float32")

    # Clip on the source grid BEFORE reproject (twin: cheaper + integrity-safe).
    if bbox is not None:
        window = window_from_bounds(bbox[0], bbox[1], bbox[2], bbox[3], transform=src_transform)
        row_off = max(0, int(math.floor(window.row_off)))
        col_off = max(0, int(math.floor(window.col_off)))
        row_end = min(src_height, int(math.ceil(window.row_off + window.height)))
        col_end = min(src_width, int(math.ceil(window.col_off + window.width)))
        if row_end <= row_off or col_end <= col_off:
            raise router_empty_error(
                spec.error_code_prefix, f"bbox={tuple(bbox)} does not intersect the source grid",
                spec.empty_error_suffix)
        arr = arr[row_off:row_end, col_off:col_end]
        src_transform = Affine(
            src_transform.a, src_transform.b, src_transform.c + col_off * src_transform.a,
            src_transform.d, src_transform.e, src_transform.f + row_off * src_transform.e)
        src_height, src_width = arr.shape

    dst_crs = CRS.from_epsg(4326)
    if src_crs is not None and src_crs != dst_crs:
        dst_transform, dst_width, dst_height = calculate_default_transform(
            src_crs, dst_crs, src_width, src_height,
            left=src_transform.c, bottom=src_transform.f + src_height * src_transform.e,
            right=src_transform.c + src_width * src_transform.a, top=src_transform.f)
        dst_arr = np.full((dst_height, dst_width), nodata, dtype="float32")
        reproject(
            source=arr, destination=dst_arr,
            src_transform=src_transform, src_crs=src_crs,
            dst_transform=dst_transform, dst_crs=dst_crs,
            resampling=Resampling.nearest, src_nodata=nodata, dst_nodata=nodata)
        arr = dst_arr
        out_transform = dst_transform
    else:
        out_transform = src_transform
    return np.asarray(arr, dtype="float32"), out_transform, dst_crs


# --------------------------------------------------------------------------- #
# griddap: an ERDDAP griddap bracket-selector REST endpoint that returns a
# PRE-SUBSET NetCDF (``.nc?<var>[(<time>)][(<lat_hi>):(<lat_lo>)][(<lon_lo>):
# (<lon_hi>)]``) -- the server does the bbox+day subset, so the whole (small)
# object is a windowed read by construction. A single GET through the shared
# transport, an in-memory xarray open + squeeze, and a north-up (array, transform,
# crs). A 404 whose body carries the ERDDAP no-matching / axis-range markers is
# honest no-data (typed EMPTY, the twin's SSTNoDataError); an all-NaN window
# (fully-land AOI, masked ocean product) is also EMPTY. NOAA CoastWatch CRW SST.
# --------------------------------------------------------------------------- #


def _griddap_to_array(spec: SourceSpec, params: dict[str, Any]) -> tuple[Any, Any, Any]:
    """ERDDAP griddap bracket-selector ``.nc`` GET + xarray subset -> float32 array.

    Builds the griddap bracket-selector URL (lat written high:low when the grid
    descends), GETs the pre-subset NetCDF, opens it in-memory, squeezes the
    singleton time, and returns the north-up ``(array, transform, "EPSG:4326")``.
    ``date`` defaults to the most-recent likely-published day (today-1 UTC) when
    absent -- consistent with the stac_float ``latest`` default (that default day
    does NOT enter the cache key, an explicit date does). A 404 with the no-data
    body markers -> typed EMPTY; any other non-2xx / parse failure -> typed UPSTREAM.
    """
    import datetime as _dt

    import numpy as np
    import rasterio.transform as rtransform

    from ..transport import TransportError, TransportNotFound, get_bytes, get_client

    ingest = spec.ingest or {}
    gd = ingest.get("griddap", {})
    bbox = params["bbox"]
    west, south, east, north = (float(v) for v in bbox)

    # variable -> ERDDAP grid variable (already a validated enum).
    vbp = gd.get("var_by_param", {})
    var = (vbp.get("map") or {}).get(params.get(vbp.get("param")))
    if var is None:
        raise router_upstream_error(
            spec.error_code_prefix,
            f"no griddap variable for {vbp.get('param')}={params.get(vbp.get('param'))!r}",
        )

    # date: explicit request param else the default (today-1 UTC).
    date = params.get("date")
    if not date:
        date = (_dt.datetime.now(_dt.timezone.utc).date() - _dt.timedelta(days=1)).isoformat()
    ts = f"{date}T{gd.get('time_of_day', '12:00:00Z')}"

    if gd.get("lat_descending", True):
        sel = f"{var}[({ts})][({north}):({south})][({west}):({east})]"
    else:
        sel = f"{var}[({ts})][({south}):({north})][({west}):({east})]"
    endpoint = spec.endpoints.get("data") or next(iter(spec.endpoints.values()))
    base = (endpoint.url or endpoint.url_template or "").rstrip("/")
    dataset = gd.get("dataset", "")
    url = f"{base}/griddap/{dataset}.nc?{sel}"

    ua = spec.auth.user_agent if spec.auth else "trid3nt_default"
    markers = [str(m).lower() for m in gd.get("nodata_body_markers", [])]

    def _is_nodata_body(body: str | None) -> bool:
        low = (body or "").lower()
        return any(m in low for m in markers)

    try:
        nc_bytes, _ct, _u = get_bytes(get_client(), url, headers={"User-Agent": ua})
    except TransportNotFound as exc:
        if _is_nodata_body(exc.body):
            raise router_empty_error(
                spec.error_code_prefix,
                f"no {dataset} data for date={date} (ERDDAP: {(exc.body or '')[:200]})",
                spec.empty_error_suffix,
            )
        raise router_upstream_error(spec.error_code_prefix, f"griddap 404 url={url}: {exc}")
    except TransportError as exc:
        if _is_nodata_body(getattr(exc, "body", None)):
            raise router_empty_error(
                spec.error_code_prefix,
                f"no {dataset} data for date={date} (ERDDAP: {(exc.body or '')[:200]})",
                spec.empty_error_suffix,
            )
        raise router_upstream_error(spec.error_code_prefix, f"griddap request failed url={url}: {exc}")
    if not nc_bytes:
        raise router_upstream_error(spec.error_code_prefix, f"empty response from {url}")

    try:
        import xarray as xr  # noqa: F401
    except ImportError as exc:  # pragma: no cover
        raise router_upstream_error(spec.error_code_prefix, f"xarray unavailable: {exc}")

    tmp_nc: str | None = None
    ds = None
    try:
        fd, tmp_nc = tempfile.mkstemp(suffix=".nc", prefix="trid3nt_router_griddap_")
        with os.fdopen(fd, "wb") as f:
            f.write(nc_bytes)
        try:
            ds = xr.open_dataset(tmp_nc, engine="netcdf4")
        except Exception as exc:  # noqa: BLE001
            raise router_upstream_error(spec.error_code_prefix, f"could not parse griddap NetCDF: {exc}")
        if var not in ds.variables:
            raise router_upstream_error(
                spec.error_code_prefix,
                f"griddap subset missing variable {var!r} (have {list(ds.data_vars)})",
            )
        da = ds[var]
        for tdim in ("time",):
            if tdim in da.dims:
                da = da.squeeze(tdim, drop=True)
        lat_dim = next((d for d in da.dims if d in ("latitude", "lat", "y")), None)
        lon_dim = next((d for d in da.dims if d in ("longitude", "lon", "x")), None)
        if lat_dim is None or lon_dim is None:
            raise router_upstream_error(
                spec.error_code_prefix, f"griddap DataArray missing lat/lon dims; dims={da.dims}")
        if da.size == 0 or any(s == 0 for s in da.shape):
            raise router_empty_error(
                spec.error_code_prefix,
                f"griddap returned an empty window for bbox={tuple(bbox)} on {date} "
                "(no grid cells intersect the AOI)",
                spec.empty_error_suffix,
            )
        arr = np.asarray(da.values, dtype="float32")
        lat_vals = np.asarray(da[lat_dim].values, dtype="float64")
        lon_vals = np.asarray(da[lon_dim].values, dtype="float64")
        # North-up: row 0 must be the northernmost lat. Flip if the coord ascends
        # (NOAA_DHW descends, so this is a no-op there; defensive for other grids).
        if lat_vals.size >= 2 and lat_vals[0] < lat_vals[-1]:
            arr = arr[::-1, :]
        if not np.isfinite(arr).any():
            raise router_empty_error(
                spec.error_code_prefix,
                f"griddap window is all-NaN over bbox={tuple(bbox)} on {date} "
                "(the AOI is land / outside the ocean mask)",
                spec.empty_error_suffix,
            )
        transform = rtransform.from_bounds(
            float(lon_vals.min()), float(lat_vals.min()),
            float(lon_vals.max()), float(lat_vals.max()),
            arr.shape[1], arr.shape[0],
        )
        return arr, transform, spec.normalize.crs
    except RouterError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise router_upstream_error(
            spec.error_code_prefix, f"griddap NetCDF -> array failed for bbox={tuple(bbox)}: {exc}")
    finally:
        if ds is not None:
            try:
                ds.close()
            except Exception:  # noqa: BLE001
                pass
        if tmp_nc is not None:
            try:
                os.unlink(tmp_nc)
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# fixed_tile_grid: a global raster cut into a REGULAR degree grid of per-tile
# ZIP objects, each wrapping ONE DEFLATE-compressed .tif member (GHS-POP tiles).
# A DEFLATE member is not windowable by a byte range (decoding forces a near-whole
# member transfer), so the honest shape is a WHOLE-OBJECT GET of each intersecting
# tile's ZIP (the shared ``get_zip`` step), an in-memory member read, a
# per-tile window, and a NaN-nodata merge -- value-identical to the twin's
# ``/vsizip//vsicurl/`` windowed read (same member bytes, same window math)..
# --------------------------------------------------------------------------- #


def _tile_grid_tiles(
    bbox: tuple[float, float, float, float], g: dict[str, Any]
) -> list[tuple[int, int]]:
    """Map a bbox to the (row, col) tiles of a regular degree grid (GHSL parity).

    Grid origin is offset from the integer-degree lattice by ``lon_offset`` /
    ``top_offset`` (the global raster does not start exactly at -180/+90). The
    row/col math reproduces the twin's ``_tiles_for_bbox`` exactly.
    """
    import math

    tile_deg = float(g.get("tile_deg", 10.0))
    lon_off = float(g.get("lon_offset", 0.0))
    top_off = float(g.get("top_offset", 0.0))
    min_lon, min_lat, max_lon, max_lat = bbox
    c0 = math.floor((min_lon - lon_off + 180.0) / tile_deg) + 1
    c1 = math.floor((max_lon - lon_off + 180.0) / tile_deg) + 1
    r0 = math.floor((90.0 + top_off - max_lat) / tile_deg) + 1
    r1 = math.floor((90.0 + top_off - min_lat) / tile_deg) + 1
    tiles: list[tuple[int, int]] = []
    for r in range(min(r0, r1), max(r0, r1) + 1):
        for c in range(min(c0, c1), max(c0, c1) + 1):
            if r >= 1 and c >= 1:
                tiles.append((r, c))
    return tiles


def _fixed_tile_grid_to_array(spec: SourceSpec, params: dict[str, Any]) -> tuple[Any, Any, Any]:
    """Whole-object per-tile ZIP GET + in-memory member window + NaN merge.

    For each intersecting grid tile: ``get_zip`` the tile's ZIP object through the
    shared transport, read the named DEFLATE ``.tif`` member into a MemoryFile, and
    window it to ``bbox`` (the twin's floor-offset / ceil-length / clip window math).
    A missing tile (the archive omits ocean-only R/C -> 404) is a coverage gap, not
    a failure (skip). Negative source fill -> NaN; the tiles are NaN-merged; an
    all-NaN / no-tile window -> typed EMPTY; a per-tile read failure -> typed UPSTREAM.
    A window exceeding ``max_pixels`` -> typed INPUT error (the twin's refusal).
    """
    import numpy as np
    import rasterio
    import rasterio.io
    from rasterio.merge import merge
    from rasterio.windows import Window, from_bounds

    from ..transport import (
        TransportError,
        TransportNotFound,
        get_client,
        get_zip,
    )

    ingest = spec.ingest or {}
    g = ingest.get("fixed_tile_grid", {})
    bbox = tuple(params["bbox"])

    cov = g.get("coverage_bbox")
    if cov and not (
        bbox[0] <= cov[2] and bbox[2] >= cov[0] and bbox[1] <= cov[3] and bbox[3] >= cov[1]
    ):
        raise router_empty_error(
            spec.error_code_prefix, f"bbox={bbox} falls outside coverage {tuple(cov)}",
            spec.empty_error_suffix)

    tiles = _tile_grid_tiles(bbox, g)
    if not tiles:
        raise router_empty_error(
            spec.error_code_prefix, f"bbox={bbox} maps to no tiles (outside coverage)",
            spec.empty_error_suffix)

    url_tmpl = g.get("url_template", "")
    member_tmpl = g.get("member_template", "")
    max_pixels = int(g.get("max_pixels", 60_000_000))
    negative_nodata = bool(g.get("negative_is_nodata", True))
    ua = spec.auth.user_agent if spec.auth else "trid3nt_default"

    datasets: list[Any] = []
    try:
        for (r, c) in tiles:
            url = url_tmpl.format(r=r, c=c)
            member = member_tmpl.format(r=r, c=c)
            try:
                zf = get_zip(get_client(), url, headers={"User-Agent": ua})
                tif_bytes = zf.read(member)
            except TransportNotFound:
                # A missing tile (ocean-only R/C the archive omits) is a coverage
                # gap, not a hard failure when other tiles exist (twin: continue).
                logger.info("router.fixed_tile_grid: tile R%d_C%d absent (404); skipping", r, c)
                continue
            except Exception as exc:  # noqa: BLE001 -- any fetch/extract failure: no-coverage
                logger.info(
                    "router.fixed_tile_grid: tile R%d_C%d open failed (%s); treating as "
                    "no-coverage for this tile", r, c, exc)
                continue
            try:
                with rasterio.io.MemoryFile(tif_bytes) as mf, mf.open() as src:
                    win = from_bounds(*bbox, transform=src.transform)
                    win = win.round_offsets(op="floor").round_lengths(op="ceil")
                    win = win.intersection(Window(0, 0, src.width, src.height))
                    if win.width <= 0 or win.height <= 0:
                        continue
                    if int(win.width) * int(win.height) > max_pixels:
                        raise router_input_error(
                            spec.error_code_prefix,
                            f"bbox={bbox} would request {int(win.width) * int(win.height):,} "
                            f"pixels in tile R{r}_C{c} -- refuse to materialize > "
                            f"{max_pixels:,}; narrow the bbox.", spec.input_error_suffix)
                    arr = src.read(1, window=win).astype(np.float32)
                    if negative_nodata:
                        arr[arr < 0] = np.nan
                    out_transform = src.window_transform(win)
                    dst_mem = rasterio.io.MemoryFile()
                    dst = dst_mem.open(
                        driver="GTiff", height=int(win.height), width=int(win.width),
                        count=1, dtype="float32", crs=spec.normalize.crs,
                        transform=out_transform, nodata=float("nan"))
                    dst.write(arr, 1)
                    datasets.append(dst)
            except RouterError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise router_upstream_error(
                    spec.error_code_prefix, f"tile R{r}_C{c} window read failed: {exc}")

        if not datasets:
            raise router_empty_error(
                spec.error_code_prefix,
                f"bbox={bbox} produced no pixels (over open water or outside coverage)",
                spec.empty_error_suffix)
        if len(datasets) == 1:
            mosaic = datasets[0].read(1)
            mtransform = datasets[0].transform
        else:
            merged, mtransform = merge(datasets, nodata=float("nan"))
            mosaic = merged[0]
        if not np.isfinite(mosaic).any():
            raise router_empty_error(
                spec.error_code_prefix,
                f"bbox={bbox} produced no valid pixels (all-NaN window -- likely over water)",
                spec.empty_error_suffix)
        return np.asarray(mosaic, dtype="float32"), mtransform, spec.normalize.crs
    finally:
        for d in datasets:
            try:
                d.close()
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------------- #
# wcs_getcoverage: a WCS 1.0.0 GetCoverage templated GET of a CATEGORICAL coverage
# (NLCD via the MRLC GeoServer) returning the canonical class integers in the band
# (NOT palette indices) -> a NLCD background(0)->nodata pixel remap -> a palette COG
# (embedded band-1 color table preserved). The coverage id resolves from the vintage
# year (declarative map); the effective resolution + quantized bbox are the pre_resolve
# auto-coarsen (merged into params before the cache key). The GET runs through the
# shared ogc adapter (the twin's Tier-2 WCS seam), the ONE sanctioned socket for this
# mode. ``execute`` bakes the source's embedded palette into the serialized COG. MRLC
# NLCD.
# --------------------------------------------------------------------------- #


def _wcs_getcoverage_to_array(spec: SourceSpec, params: dict[str, Any]) -> tuple[Any, Any, Any, dict | None, float | None]:
    """WCS 1.0.0 GetCoverage GET + NLCD background(0)->nodata remap.

    Returns ``(array uint8, transform, crs, colormap|None, nodata)``. The MRLC WCS
    embedded color table maps class 0 (Background / no-coverage: open ocean,
    international waters) to opaque black rather than transparent, and 0 is NEVER a
    legitimate NLCD code (real codes are 11-95), so every 0-valued pixel is folded
    into the raster's declared nodata sentinel (already-transparent) -- the twin's
    ``_fix_nlcd_background_transparency`` at the pixel level. A missing coverage /
    non-TIFF body -> typed UPSTREAM.
    """
    import math

    import numpy as np
    import rasterio
    from rasterio.io import MemoryFile

    from trid3nt_server.agent.tools.search.ogc_adapter import OGCAdapterError, fetch_ogc_layer

    ingest = spec.ingest or {}
    w = ingest.get("wcs", {})
    bbox = tuple(float(v) for v in params["bbox"])
    vintage_year = int(params["vintage_year"])
    res_m = max(1, int(params["resolution_m"]))
    background_class = int(w.get("background_class", 0))

    coverage_by_year = {int(k): v for k, v in (w.get("coverage_by_year") or {}).items()}
    coverage = coverage_by_year.get(vintage_year)
    if coverage is None:
        raise router_upstream_error(
            spec.error_code_prefix,
            f"NLCD vintage year {vintage_year} not in the WCS catalog "
            f"(available: {sorted(coverage_by_year)}).",
        )

    # WCS 1.0.0 GetCoverage requires explicit WIDTH/HEIGHT; size the pixel grid to
    # the bbox at the effective resolution, clamped to the MRLC ~4000 px/axis cap.
    min_lon, min_lat, max_lon, max_lat = bbox
    mid_lat = 0.5 * (min_lat + max_lat)
    m_per_deg_lon = 111_320.0 * math.cos(math.radians(mid_lat))
    max_px = int(w.get("max_px", 4000))
    width_px = max(16, min(max_px, int(round((max_lon - min_lon) * m_per_deg_lon / res_m))))
    height_px = max(16, min(max_px, int(round((max_lat - min_lat) * 111_320.0 / res_m))))

    endpoint = spec.endpoints.get("data") or next(iter(spec.endpoints.values()))
    wcs_url = endpoint.url or endpoint.url_template or ""
    ua = spec.auth.user_agent if spec.auth else "trid3nt_default"
    try:
        resp = fetch_ogc_layer(
            url=wcs_url, layer_name=coverage, bbox=bbox, crs="EPSG:4326",
            service_type="WCS", image_format=str(w.get("image_format", "GeoTIFF")),
            version="1.0.0", width_px=width_px, height_px=height_px,
            timeout_s=float(w.get("timeout_s", 120.0)), user_agent=ua,
        )
    except OGCAdapterError as exc:
        raise router_upstream_error(
            spec.error_code_prefix, f"MRLC WCS GetCoverage failed for coverage={coverage} bbox={bbox}: {exc}"
        )
    ct = (resp.content_type or "").lower()
    if "tiff" not in ct and "geotiff" not in ct:
        raise router_upstream_error(
            spec.error_code_prefix,
            f"MRLC WCS returned unexpected content-type={resp.content_type!r} for coverage={coverage} "
            f"bbox={bbox}; body preview: {resp.content[:200]!r}",
        )

    with MemoryFile(resp.content) as mem, mem.open() as src:
        arr = src.read(1)
        transform = src.transform
        crs = src.crs
        try:
            colormap = src.colormap(1)
        except (ValueError, KeyError):
            colormap = None
        src_nodata = src.nodata

    target_nodata = float(background_class) if src_nodata is None else float(src_nodata)
    if int(target_nodata) != background_class:
        arr = arr.copy()
        arr[arr == background_class] = int(target_nodata)
    return np.asarray(arr, dtype="uint8"), transform, crs, colormap, target_nodata


# --------------------------------------------------------------------------- #
# categorical_tile_grid: a global CATEGORICAL raster cut into a fixed h/v degree
# grid of per-tile direct-GET GeoTIFFs (NASA LANCE MCDWD flood tiles), each a
# uint8 class raster (NOT zip-wrapped, NOT continuous). The first-valid-wins uint8
# mosaic + embedded palette variant of ``fixed_tile_grid`` (continuous NaN-merge)
# named: per-10-deg-tile GeoTIFF -> nearest-window -> FIRST-VALID uint8
# mosaic. The (year, doy) drive the per-tile URL and are resolved pre-cache-key by
# the ``pre_resolve`` dir-walk hook (merged into params). ``execute`` serializes
# the returned uint8 array with the declarative palette (nodata transparent). A
# missing tile (404) is a coverage gap (skip); an all-nodata mosaic -> typed EMPTY.
# NASA LANCE MCDWD_L3_F3_NRT.
# --------------------------------------------------------------------------- #


def _ctg_tile_bounds(h: int, v: int, tile_deg: float) -> tuple[float, float, float, float]:
    """(west, south, east, north) of an h{hh}v{vv} tile (twin ``_tile_bounds``)."""
    west = -180.0 + tile_deg * h
    north = 90.0 - tile_deg * v
    return (west, north - tile_deg, west + tile_deg, north)


def _ctg_tiles_for_bbox(
    bbox: tuple[float, float, float, float], g: dict[str, Any]
) -> list[tuple[int, int]]:
    """The (h, v) tiles overlapping ``bbox`` clamped to the grid (twin ``_tiles_for_bbox``)."""
    import math

    tile_deg = float(g.get("tile_deg", 10.0))
    h_max = int(g.get("h_max", 35))
    v_max = int(g.get("v_max", 17))
    west, south, east, north = bbox
    h0 = max(0, int(math.floor((west + 180.0) / tile_deg)))
    h1 = min(h_max, int(math.floor((east + 180.0) / tile_deg)))
    v0 = max(0, int(math.floor((90.0 - north) / tile_deg)))
    v1 = min(v_max, int(math.floor((90.0 - south) / tile_deg)))
    return [(h, v) for v in range(v0, v1 + 1) for h in range(h0, h1 + 1)]


def _ctg_read_tile_window(
    tile_bytes: bytes, bbox: tuple[float, float, float, float],
    width_px: int, height_px: int, nodata: int,
) -> Any:
    """Reproject+window a tile's band-1 to EPSG:4326 at ``bbox`` nearest -> uint8
    (H, W), ``nodata`` filling no-data pixels (twin ``_read_tile_window``)."""
    import numpy as np
    import rasterio
    from rasterio.io import MemoryFile
    from rasterio.warp import Resampling, reproject

    with MemoryFile(tile_bytes) as mem, mem.open() as src:
        dst_transform = rasterio.transform.from_bounds(
            bbox[0], bbox[1], bbox[2], bbox[3], width_px, height_px
        )
        dst = np.full((height_px, width_px), nodata, dtype="uint8")
        src_nodata = src.nodata if src.nodata is not None else nodata
        reproject(
            source=rasterio.band(src, 1), destination=dst,
            src_transform=src.transform, src_crs=src.crs,
            dst_transform=dst_transform, dst_crs="EPSG:4326",
            resampling=Resampling.nearest, src_nodata=src_nodata, dst_nodata=nodata,
        )
    return dst


def _categorical_tile_grid_to_array(spec: SourceSpec, params: dict[str, Any]) -> tuple[Any, Any, Any]:
    """Direct-GET per-tile categorical GeoTIFF + first-valid uint8 mosaic.

    For each covering h/v tile: GET the ``{archive}/{year}/{doy}/{fname}`` object
    through the shared transport (404 -> skip, a coverage gap), reproject-window its
    band-1 to ``bbox`` nearest (uint8), and paste its non-nodata pixels where the
    mosaic is still nodata (FIRST-VALID wins -- the twin's tile order). An all-nodata
    / no-tile mosaic -> typed EMPTY (honest no-coverage). Any non-404 tile fetch or
    read failure -> typed UPSTREAM. Returns ``(mosaic uint8, transform, "EPSG:4326")``;
    ``execute`` bakes the declarative palette into the serialized COG.
    """
    import numpy as np
    import rasterio

    from ..transport import (
        TransportError,
        TransportNotFound,
        get_bytes,
        get_client,
    )

    ingest = spec.ingest or {}
    g = ingest.get("categorical_tile_grid", {})
    bbox = tuple(float(v) for v in params["bbox"])
    nodata = int(g.get("nodata", 255))
    cell_deg = float(g.get("cell_deg", 10.0 / 4800.0))
    year = int(params["year"])
    doy = int(params["doy"])

    archive = str(g.get("archive_url", "")).rstrip("/")
    product = str(g.get("product", ""))
    fname_tmpl = str(g.get("fname_template", "{product}.A{year}{doy:03d}.h{h:02d}v{v:02d}.061.tif"))
    ua = spec.auth.user_agent if spec.auth else "trid3nt_default"

    width_px = max(1, round((bbox[2] - bbox[0]) / cell_deg))
    height_px = max(1, round((bbox[3] - bbox[1]) / cell_deg))
    dst_transform = rasterio.transform.from_bounds(
        bbox[0], bbox[1], bbox[2], bbox[3], width_px, height_px
    )

    mosaic = np.full((height_px, width_px), nodata, dtype="uint8")
    filled = np.zeros((height_px, width_px), dtype=bool)
    for h, v in _ctg_tiles_for_bbox(bbox, g):
        fname = fname_tmpl.format(product=product, year=year, doy=doy, h=h, v=v)
        url = f"{archive}/{year}/{doy:03d}/{fname}"
        try:
            tile_bytes, _ct, _u = get_bytes(get_client(), url, headers={"User-Agent": ua})
        except TransportNotFound:
            continue  # a missing tile is a coverage gap (twin: b"" -> skip)
        except TransportError as exc:
            raise router_upstream_error(spec.error_code_prefix, f"MCDWD tile fetch failed url={url}: {exc}")
        if not tile_bytes:
            continue
        try:
            arr = _ctg_read_tile_window(tile_bytes, bbox, width_px, height_px, nodata)
        except Exception as exc:  # noqa: BLE001
            raise router_upstream_error(spec.error_code_prefix, f"MCDWD tile read failed: {exc}")
        valid = (arr != nodata) & (~filled)
        if bool(valid.any()):
            mosaic[valid] = arr[valid]
            filled |= valid

    if not bool(filled.any()):
        raise router_empty_error(
            spec.error_code_prefix,
            f"no coverage over bbox={tuple(round(x, 3) for x in bbox)} for {year}-{doy:03d} "
            "(no tile downloaded, or every pixel is insufficient-data/cloud). Try a nearby "
            "date or a different AOI.",
            spec.empty_error_suffix,
        )
    return mosaic, dst_transform, "EPSG:4326"


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
    owns the socket; GDAL only parses the returned bytes for the
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


# --------------------------------------------------------------------------- #
# mapserver_export: an ArcGIS MapServer ``/export`` returning a SERVER-SYMBOLIZED
# PNG32 (a baked color scheme, not raw values), georeferenced client-side into a
# 4-band RGBA COG so publish_layer renders the baked symbology directly (no
# colormap, no style-registry row). The transport owns the socket;
# PIL/GDAL only decode the returned image. A fully-transparent export (a bbox with
# no coverage at that level) is a VALID transparent overlay, never a fabricated
# layer AND never a typed EMPTY (the twin's honesty floor: the layer appears and
# renders nothing). NOAA OCM SLR Viewer conf_* / marsh_* siblings.
# --------------------------------------------------------------------------- #


def _mapserver_export_grid(
    bbox: tuple[float, float, float, float], res_deg: float, img: dict[str, Any]
) -> tuple[int, int]:
    """MapServer/export ``size`` (width_px, height_px) from a res_deg cell size.

    Reproduces the twin's ``grid_size``: ceil the bbox span over ``res_deg``, clamp
    per axis to ``[px_min, px_max]`` (NOAA rejects very large export requests).
    """
    import math

    px_min = int(img.get("px_min", 16))
    px_max = int(img.get("px_max", 2048))
    min_lon, min_lat, max_lon, max_lat = bbox
    w = max(px_min, min(px_max, int(math.ceil((max_lon - min_lon) / res_deg))))
    h = max(px_min, min(px_max, int(math.ceil((max_lat - min_lat) / res_deg))))
    return w, h


def _mapserver_export_rgba_bytes(spec: SourceSpec, params: dict[str, Any]) -> bytes:
    """MapServer ``/export`` PNG32 -> georeferenced 4-band RGBA COG bytes.

    Resolves the service name from a request param (the SLR level -> conf_*/marsh_*
    service), fetches the server-rendered PNG over the bbox through the shared
    transport, decodes it to RGBA, georeferences it with the request-bbox transform,
    and serializes a 4-band RGBA COG. A missing service (an out-of-set level) is a
    typed INPUT error (the twin's ``NOAA_SLR_RASTER_INPUT_INVALID``); an undecodable
    body / HTTP failure is a typed UPSTREAM error. No nodata coverage gate -- a
    transparent export is a valid empty overlay.
    """
    import io

    import numpy as np
    from PIL import Image
    from rasterio.transform import from_bounds

    from ..transport import TransportError, get_bytes, get_client

    ingest = spec.ingest or {}
    img = ingest.get("mapserver", {})
    bbox = tuple(params["bbox"])

    # service name resolved from a request param (the level -> service map).
    svc_cfg = img.get("service_by_param", {})
    svc_param = svc_cfg.get("param")
    svc_map = svc_cfg.get("map", {})
    service = svc_map.get(params.get(svc_param))
    if service is None:
        # An out-of-set level is an input defect (the twin's per-level validation
        # raised NOAA_SLR_RASTER_INPUT_INVALID before any network call).
        raise router_input_error(
            spec.error_code_prefix,
            f"{svc_param}={params.get(svc_param)!r} is not a valid level (no service in the map)",
            spec.input_error_suffix)

    endpoint = spec.endpoints.get("data") or next(iter(spec.endpoints.values()))
    base = (endpoint.url or endpoint.url_template or "").rstrip("/")
    url = f"{base}/{service}/MapServer/export"

    # res_deg is a request param (model-overridable); fall back to the static
    # default. A non-positive / non-finite value is a typed INPUT error (the twin's
    # resolve_res_deg guard), reproduced before any network call.
    import math as _math

    res_deg = params.get("res_deg")
    res_deg = float(res_deg) if res_deg is not None else float(img.get("res_deg", 0.0005))
    if not (_math.isfinite(res_deg) and res_deg > 0):
        raise router_input_error(
            spec.error_code_prefix, f"res_deg must be a positive number; got {res_deg!r}",
            spec.input_error_suffix)
    width_px, height_px = _mapserver_export_grid(bbox, res_deg, img)
    query = dict(img.get("export_query", {}))
    query["bbox"] = f"{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}"
    query["size"] = f"{width_px},{height_px}"

    ua = spec.auth.user_agent if spec.auth else "trid3nt_default"
    try:
        body, _ct, _u = get_bytes(get_client(), url, headers={"User-Agent": ua}, params=query)
    except TransportError as exc:
        raise router_upstream_error(
            spec.error_code_prefix, f"MapServer export failed url={url}: {exc}")
    try:
        im = Image.open(io.BytesIO(body)).convert("RGBA")
    except Exception as exc:  # noqa: BLE001 -- undecodable upstream payload (JSON error / HTML)
        raise router_upstream_error(
            spec.error_code_prefix, f"MapServer export returned an undecodable image url={url}: {exc}")

    arr = np.asarray(im, dtype=np.uint8)  # (H, W, 4)
    out_h, out_w = arr.shape[0], arr.shape[1]
    chw = np.transpose(arr, (2, 0, 1))  # (4, H, W)
    transform = from_bounds(bbox[0], bbox[1], bbox[2], bbox[3], out_w, out_h)
    return array_to_cog_bytes(
        chw, transform, spec.normalize.crs, nodata=None, dtype="uint8", colorinterp="rgba")


def _pc_sign_two_tier(spec: SourceSpec, href: str, collection: str) -> str:
    """Sign a PC asset href: per-href sign endpoint PRIMARY, token path FALLBACK.

    The MODIS/Copernicus blob accounts (``modiseuwest`` / ``elevationeuwest``) are
    NOT authorized by the per-collection token, so the storage-account-aware per-
    href ``/api/sas/v1/sign`` endpoint is primary; on any failure fall back to the
    shared per-collection token path. The transport owns the socket.
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


def _aoi_coverage(item: Any, aoi: Any) -> float:
    """Fraction of ``aoi`` covered by ``item.geometry`` (0.0 on bad geometry).

    The coverage-fraction leg shared by the ``coverage`` scene-select (sentinel1_sar)
    and the multi-asset RGB composite rank.
    """
    from shapely.geometry import shape

    try:
        inter = shape(item.geometry).intersection(aoi).area
        return inter / aoi.area if aoi.area > 0 else 0.0
    except Exception:  # noqa: BLE001 -- bad geometry: treat as no coverage
        return 0.0


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

    abp1 = stac.get("asset_by_param")
    abp = stac.get("asset_by_params")
    if abp1:
        # Single-param asset map (mobi ``layer`` -> asset key): the param is an
        # already-validated enum (router rejects an unknown value pre-network with
        # the param's LAYER_INVALID suffix), so a direct map lookup -- no alias
        # normalization. No-op for every prior spec (none set asset_by_param).
        asset_key = abp1["map"][params.get(abp1["param"])]
    elif abp:
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

    def _dt_key(it: Any) -> str:
        p = getattr(it, "properties", {}) or {}
        return p.get("datetime") or p.get("end_datetime") or p.get("start_datetime") or ""

    if select == "latest":
        items.sort(key=_dt_key, reverse=True)
        raw = _read_item(items[0])
    elif select == "coverage":
        # coverage-fraction-then-recency select with an asset-presence pre-filter
        # (sentinel1_sar): only scenes carrying the requested asset are ranked; a
        # swath edge can clip a corner, so AOI coverage is the primary key and
        # recency breaks ties. No candidate carrying the asset -> typed EMPTY.
        from shapely.geometry import box

        aoi = box(*bbox)
        cand = [it for it in items if asset_key in (getattr(it, "assets", {}) or {})]
        if not cand:
            raise router_empty_error(
                spec.error_code_prefix,
                f"no {collection!r} scene carrying asset {asset_key!r} intersects bbox={bbox}",
                spec.empty_error_suffix,
            )

        cand.sort(key=lambda it: (_aoi_coverage(it, aoi), _dt_key(it)), reverse=True)
        raw = _read_item(cand[0])
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

    # positive_only (mobi): the importance products (species richness / RSR) are
    # strictly positive where mapped; <=0 (and non-finite) is nodata -- the twin's
    # ``valid = isfinite(dst) & (dst > 0)`` gate. No-op for every prior spec.
    if tf.get("positive_only"):
        out = np.where(np.isfinite(out) & (out > 0.0), out, np.nan).astype("float32")

    # log10_db (sentinel1_sar): linear gamma0 power -> decibels (10*log10(power)).
    # Non-positive / non-finite power is not renderable backscatter -> NaN (the
    # serialize directive fills it with the source's dB nodata sentinel); an
    # all-invalid window -> the typed EMPTY below (the twin's NO_IMAGERY). No-op for
    # every prior spec (none set log10_db).
    if tf.get("log10_db"):
        valid = np.isfinite(out) & (out > 0.0)
        db = np.full(out.shape, np.nan, dtype="float32")
        db[valid] = (10.0 * np.log10(out[valid])).astype("float32")
        out = db

    if not bool(np.isfinite(out).any()):
        raise router_empty_error(
            spec.error_code_prefix, f"all-fill (no valid pixel) window over bbox={bbox}",
            spec.empty_error_suffix)
    return out, dst_transform, "EPSG:4326"


# --------------------------------------------------------------------------- #
# stac_multi_asset_rgb: a baked RGB composite from PC STAC. ONE mode
# serving fetch_landsat_imagery (true/false-color + thermal LST), fetch_sentinel2_
# truecolor and fetch_naip. Per item it reads N single-band RGB assets (or N bands
# of ONE multi-band asset) + an optional QA/SCL mask asset, scales to physical
# reflectance / land-surface-temperature (or reads raw), masks cloud/shadow/fill,
# and bakes either a joint-2/98-percentile RGB, an inferno-ramped single-band LST,
# or a raw-uint8 passthrough into a 3-band photometric-RGB uint8 COG (publish_layer
# multiband passthrough). Scene selection is a declarative cloud-cover query +
# platform filter + a coverage/cloud rank. STRICT no-op for the stac_float specs (a
# distinct access mode)..
# --------------------------------------------------------------------------- #


def _rgb_resolve_window(stac: dict, params: dict[str, Any]) -> str | None:
    """The STAC datetime window: both bounds -> that window; else a trailing N-day
    window (``default_window_days``). None when the source is time-static (naip)."""
    if not stac.get("datetime_window"):
        return None
    d0, d1 = params.get("start_date"), params.get("end_date")
    if d0 and d1:
        return f"{d0}/{d1}"
    from datetime import datetime, timedelta, timezone

    days = int(stac.get("default_window_days", 120))
    end = datetime.now(timezone.utc).date()
    return f"{(end - timedelta(days=days)).isoformat()}/{end.isoformat()}"


def _rgb_search_items(spec: SourceSpec, stac: dict, sel: dict, collection: str,
                      bbox: tuple, dt_range: str | None, params: dict[str, Any]) -> list[Any]:
    """PC STAC search with an optional ``eo:cloud_cover`` query + platform filter."""
    from ...imagery import _pc_stac

    try:
        from pystac_client import Client
    except ImportError as exc:  # pragma: no cover
        raise router_upstream_error(spec.error_code_prefix, f"pystac-client unavailable: {exc}")
    query: dict[str, Any] = {}
    if sel.get("cloud_query"):
        mc = params.get("max_cloud_cover")
        if mc is not None:
            query["eo:cloud_cover"] = {"lt": float(mc)}
    pq = sel.get("platform_query")
    if pq:
        plats = list(pq.get("base", []))
        if params.get(pq.get("param")):
            plats = plats + list(pq.get("extra", []))
        query["platform"] = {"in": plats}
    try:
        client = Client.open(stac.get("root", _pc_stac.PC_STAC_ROOT))
        search = client.search(collections=[collection], bbox=list(bbox),
                               datetime=dt_range, query=query or None, limit=100)
        items = list(search.items())
    except Exception as exc:  # noqa: BLE001
        raise router_upstream_error(
            spec.error_code_prefix,
            f"STAC search failed (collection={collection!r}, bbox={bbox}, window={dt_range}): {exc}")
    if not items:
        raise router_empty_error(
            spec.error_code_prefix,
            f"no {collection!r} imagery intersects bbox={bbox}"
            + (f" in {dt_range}" if dt_range else "")
            + (f" under {params.get('max_cloud_cover')}% cloud cover" if sel.get("cloud_query") else ""),
            spec.empty_error_suffix)
    return items


def _rgb_rank_item(sel: dict, items: list[Any], bbox: tuple) -> Any:
    """Return the best item per the declarative ``select.rank`` keys.

    Each directive: ``{by: coverage_bucket, min_frac}`` (full-coverage scenes
    first), ``{by: cloud_cover}`` (least-cloudy), or ``{by: coverage}`` (most AOI
    overlap). An empty rank returns ``items[0]`` (naip: most-recent intersecting).
    Landsat ranks coverage_bucket -> cloud_cover -> coverage; sentinel2 cloud_cover.
    """
    rank = sel.get("rank") or []
    if not rank:
        return items[0]
    from shapely.geometry import box

    aoi = box(*bbox)

    def _key(it: Any) -> tuple:
        parts: list[float] = []
        for d in rank:
            by = d.get("by")
            if by == "coverage_bucket":
                cov = _aoi_coverage(it, aoi)
                parts.append(-(1 if cov >= float(d.get("min_frac", 0.99)) else 0))
            elif by == "cloud_cover":
                parts.append((getattr(it, "properties", {}) or {}).get("eo:cloud_cover", 100.0))
            elif by == "coverage":
                parts.append(-_aoi_coverage(it, aoi))
        return tuple(parts)

    return sorted(items, key=_key)[0]


def _rgb_read_float(spec: SourceSpec, signed: str, band: int, bbox: tuple,
                    w: int, h: int, dst_transform: Any, *, nearest: bool) -> Any:
    """Warp+window-read one band to EPSG:4326 as float32 (nodata reads back 0.0)."""
    import numpy as np
    import rasterio
    from rasterio.warp import Resampling, reproject

    from ..transport import TransportError, open_windowed_cog

    dst = np.zeros((h, w), dtype="float32")
    try:
        with open_windowed_cog(signed) as src:
            reproject(
                source=rasterio.band(src, band), destination=dst,
                src_transform=src.transform, src_crs=src.crs,
                dst_transform=dst_transform, dst_crs="EPSG:4326",
                resampling=Resampling.nearest if nearest else Resampling.bilinear,
                src_nodata=(src.nodata if src.nodata is not None else 0), dst_nodata=0)
    except TransportError as exc:
        raise router_upstream_error(spec.error_code_prefix, f"asset read failed: {exc}")
    except Exception as exc:  # noqa: BLE001
        raise router_upstream_error(spec.error_code_prefix, f"asset read failed: {exc}")
    return dst


def _rgb_read_passthrough(spec: SourceSpec, signed: str, bands: list[int], bbox: tuple,
                          w: int, h: int, dst_transform: Any) -> Any:
    """Warp+window-read up to 3 bands of ONE asset directly into a uint8 RGB array
    (naip: no scale/mask; a missing high band stays 0 -- the twin's min(3,count))."""
    import numpy as np
    import rasterio
    from rasterio.warp import Resampling, reproject

    from ..transport import TransportError, open_windowed_cog

    rgb = np.zeros((3, h, w), dtype="uint8")
    try:
        with open_windowed_cog(signed) as src:
            for i in range(min(len(bands), int(src.count))):
                reproject(
                    source=rasterio.band(src, bands[i]), destination=rgb[i],
                    src_transform=src.transform, src_crs=src.crs,
                    dst_transform=dst_transform, dst_crs="EPSG:4326",
                    resampling=Resampling.bilinear)
    except TransportError as exc:
        raise router_upstream_error(spec.error_code_prefix, f"asset read failed: {exc}")
    except Exception as exc:  # noqa: BLE001
        raise router_upstream_error(spec.error_code_prefix, f"asset read failed: {exc}")
    return rgb


def _rgb_apply_transform(dn: Any, tf: dict) -> Any:
    """DN -> physical scalar per ``transform.kind`` (reflectance / lst_celsius /
    none); the fill DN reads back as NaN so the render's nodata rule masks it."""
    import numpy as np

    kind = tf.get("kind", "none")
    if kind == "reflectance":
        ref = dn * float(tf["scale"]) + float(tf.get("offset", 0.0))
        return np.where(dn == float(tf.get("fill_dn", 0)), np.nan, ref).astype("float32")
    if kind == "lst_celsius":
        c = dn * float(tf["scale"]) + float(tf.get("offset", 0.0)) - float(tf.get("kelvin_offset", 273.15))
        return np.where(dn == float(tf.get("fill_dn", 0)), np.nan, c).astype("float32")
    return dn.astype("float32")


def _rgb_bad_mask(mask_arr: Any, mask: dict) -> Any:
    """Boolean bad (cloud/shadow/fill) mask from a QA bitmask or an SCL class set."""
    import numpy as np

    kind = mask.get("kind")
    if kind == "bitmask":
        qa = mask_arr.astype("uint16")
        bits = 0
        for b in mask.get("bad_bits", []):
            bits |= (1 << int(b))
        return np.asarray((qa & bits) != 0)
    if kind == "classes":
        return np.isin(mask_arr.astype("int16"), np.asarray(mask.get("bad_classes", [])))
    return np.zeros(mask_arr.shape, dtype=bool)


def _rgb_render_joint_stretch(spec: SourceSpec, bands: list[Any], bad: Any, render: dict) -> Any:
    """Joint 2..98 percentile-stretch N float bands to a uint8 RGB (nodata+bad -> 0).

    ``nodata_rule=all_bands_zero`` (sentinel2 raw uint16) else the non-finite fill
    from the reflectance transform (landsat). Raises a typed EMPTY when no clear
    pixel remains. Returns a ``(N, H, W)`` uint8 array."""
    import numpy as np

    stack = np.stack(bands)  # (N, H, W) float32
    finite = np.all(np.isfinite(stack), axis=0)
    if render.get("nodata_rule") == "all_bands_zero":
        nodata = np.all(stack == 0, axis=0)
    else:
        nodata = ~finite
    if bad is None:
        bad = np.zeros(finite.shape, dtype=bool)
    clear = finite & (~bad) & (~nodata)
    clear_vals = stack[:, clear]
    if clear_vals.size == 0:
        raise router_empty_error(
            spec.error_code_prefix,
            "scene produced an all-cloud / all-nodata window over the AOI (no clear pixels to render)",
            spec.empty_error_suffix)
    lo = float(np.nanpercentile(clear_vals, float(render.get("lo_pct", 2.0))))
    hi = float(np.nanpercentile(clear_vals, float(render.get("hi_pct", 98.0))))
    span = max(float(render.get("span_floor", 1e-6)), hi - lo)
    scaled = np.clip((stack - lo) / span * 255.0, 0.0, 255.0)
    rgb = np.nan_to_num(scaled, nan=0.0).astype("uint8")
    zero = nodata | bad
    for bi in range(rgb.shape[0]):
        rgb[bi][zero] = 0
    return rgb


def _rgb_render_colormap(spec: SourceSpec, band: Any, bad: Any, render: dict) -> Any:
    """Percentile-stretch one float band through a baked matplotlib ramp (landsat
    thermal LST -> inferno), zeroing nodata/clouded pixels. ``(3, H, W)`` uint8."""
    import numpy as np

    if bad is None:
        bad = np.zeros(band.shape, dtype=bool)
    valid = np.isfinite(band) & (~bad)
    vals = band[valid]
    if vals.size == 0:
        raise router_empty_error(
            spec.error_code_prefix,
            "thermal band produced an all-cloud / all-nodata window over the AOI "
            "(no valid surface-temperature pixels to render)",
            spec.empty_error_suffix)
    lo = float(np.nanpercentile(vals, float(render.get("lo_pct", 2.0))))
    hi = float(np.nanpercentile(vals, float(render.get("hi_pct", 98.0))))
    span = max(float(render.get("span_floor", 1e-6)), hi - lo)
    norm = np.nan_to_num(np.clip((band - lo) / span, 0.0, 1.0), nan=0.0)
    try:
        import matplotlib

        cmap = matplotlib.colormaps[render.get("cmap", "inferno")]
        rgba = (cmap(norm) * 255.0).astype("uint8")  # (H, W, 4)
        rgb = np.transpose(rgba[..., :3], (2, 0, 1))  # (3, H, W)
    except Exception:  # noqa: BLE001 -- no matplotlib: manual black->red->yellow ramp
        r = np.clip(norm * 2.0, 0.0, 1.0)
        g = np.clip(norm * 2.0 - 1.0, 0.0, 1.0)
        b = np.zeros_like(norm)
        rgb = (np.stack([r, g, b]) * 255.0).astype("uint8")
    nod = ~valid
    for bi in range(3):
        rgb[bi][nod] = 0
    return rgb


def _rgb_cog_bytes(rgb: Any, transform: Any, crs: Any) -> bytes:
    """Serialize a 3-band uint8 array to a photometric-RGB DEFLATE COG (multiband
    passthrough; byte-identical to the twins' ``_write_rgb_cog``)."""
    import rasterio

    count, height, width = rgb.shape
    out_fd, out_path = tempfile.mkstemp(suffix=".tif", prefix="trid3nt_router_rgb_")
    os.close(out_fd)
    try:
        profile = dict(
            driver="COG", dtype="uint8", count=count, height=height, width=width,
            crs=crs, transform=transform, compress="DEFLATE", photometric="RGB")
        with rasterio.open(out_path, "w", **profile) as dst:
            dst.write(rgb)
            dst.colorinterp = [
                rasterio.enums.ColorInterp.red,
                rasterio.enums.ColorInterp.green,
                rasterio.enums.ColorInterp.blue,
            ]
        with open(out_path, "rb") as f:
            return f.read()
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass


def _stac_multi_asset_rgb_to_array(spec: SourceSpec, params: dict[str, Any]) -> tuple[Any, Any, Any]:
    """Search -> rank -> read RGB (+mask) assets -> scale/mask/render -> 3-band uint8
    RGB ``(array, transform, "EPSG:4326")``. ``execute`` serializes it via
    :func:`_rgb_cog_bytes` (photometric-RGB passthrough), NOT the float COG path."""
    import rasterio

    from ...imagery import _pc_stac

    ingest = spec.ingest or {}
    stac = ingest.get("stac", {})
    comp = ingest.get("composite", {})
    sel = stac.get("select", {})
    bbox = tuple(params["bbox"])
    collection = stac.get("collection")

    combo_param = comp.get("combo_param")
    combo = (params.get(combo_param) if combo_param else None) or comp.get("default_combo")
    recipe = (comp.get("combos") or {}).get(combo)
    if recipe is None:
        raise router_upstream_error(spec.error_code_prefix, f"no composite recipe for combo {combo!r}")

    dt_range = _rgb_resolve_window(stac, params)
    items = _rgb_search_items(spec, stac, sel, collection, bbox, dt_range, params)
    item = _rgb_rank_item(sel, items, bbox)
    assets = getattr(item, "assets", {}) or {}

    native_cell_m = float(ingest.get("native_cell_m", 10.0))
    px_max = int(comp.get("px_max", 4096))
    w, h = _pc_stac.bbox_pixel_dims(bbox, native_cell_m, px_max=px_max)
    dst_transform = rasterio.transform.from_bounds(bbox[0], bbox[1], bbox[2], bbox[3], w, h)

    def _sign(asset_key: str) -> str:
        return _pc_sign_two_tier(spec, assets[asset_key].href, collection)

    channels = recipe.get("assets", [])
    render = recipe.get("render", {})
    render_kind = render.get("kind", "joint_stretch")

    if render_kind == "passthrough":
        # naip: N bands of ONE asset, read straight to uint8 (no scale/mask/stretch).
        image_asset = channels[0]
        if image_asset not in assets:
            raise router_upstream_error(
                spec.error_code_prefix,
                f"item {getattr(item, 'id', '?')} missing asset {image_asset!r} "
                f"(have {sorted(assets)})")
        rgb = _rgb_read_passthrough(
            spec, _sign(image_asset), recipe.get("bands", [1, 2, 3]), bbox, w, h, dst_transform)
        if not rgb.any():
            raise router_empty_error(
                spec.error_code_prefix,
                f"item {getattr(item, 'id', '?')} produced an all-black window over "
                f"bbox={bbox} (item does not actually cover the AOI)",
                spec.empty_error_suffix)
        return rgb, dst_transform, "EPSG:4326"

    # float path (joint_stretch / colormap): separate single-band assets + a mask.
    mask_asset = recipe.get("mask_asset")
    needed = list(dict.fromkeys([*channels, *([mask_asset] if mask_asset else [])]))
    missing = [a for a in needed if a not in assets]
    if missing:
        raise router_upstream_error(
            spec.error_code_prefix,
            f"item {getattr(item, 'id', '?')} missing assets {missing} for combo={combo!r} "
            f"(have {sorted(assets)[:12]})")

    tf = recipe.get("transform", {})
    bands = [
        _rgb_apply_transform(
            _rgb_read_float(spec, _sign(a), 1, bbox, w, h, dst_transform, nearest=False), tf)
        for a in channels
    ]
    bad = None
    if mask_asset:
        mask_arr = _rgb_read_float(spec, _sign(mask_asset), 1, bbox, w, h, dst_transform, nearest=True)
        bad = _rgb_bad_mask(mask_arr, recipe.get("mask", {}))

    if render_kind == "colormap":
        rgb = _rgb_render_colormap(spec, bands[0], bad, render)
    else:
        rgb = _rgb_render_joint_stretch(spec, bands, bad, render)
    return rgb, dst_transform, "EPSG:4326"


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
    hrefs read through the httpx transport (the transport owns the
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


# --------------------------------------------------------------------------- #
# stac_continuous_mosaic: a CONTINUOUS-value uint8 STAC mosaic. Unlike
# stac_search (categorical, Resampling.nearest, a single static mosaic.nodata, the
# token-based sas sign) this reads each intersecting item's band-1 through the
# two-tier PC REST /sign endpoint with BILINEAR resampling into a uint8 window with
# a PER-BAND nodata sentinel (mosaic.nodata_by_param -- 0 for occurrence/recurrence/
# seasonality, 253 for change), first-valid mosaic. execute() then bakes the pure
# per-band colormap (hooks.colormap) into the band-1 palette via array_to_cog_bytes.
# The jrc-gsw fold: a continuous 30 m water-statistics mosaic the categorical
# stac_search mode cannot express without regressing the esri_landcover prior. STRICT
# no-op for every prior raster spec (a distinct access mode).
# --------------------------------------------------------------------------- #


def _stac_continuous_nodata(spec: SourceSpec, params: dict[str, Any]) -> int:
    """The per-band nodata sentinel (``mosaic.nodata_by_param`` keyed on a param)."""
    mosaic_cfg = (spec.ingest or {}).get("mosaic", {})
    nbp = mosaic_cfg.get("nodata_by_param")
    if nbp:
        return int((nbp.get("map") or {})[params.get(nbp.get("param"))])
    return int(mosaic_cfg.get("nodata", 0))


def _stac_continuous_mosaic(
    spec: SourceSpec, params: dict[str, Any]
) -> tuple[Any, Any, str, int]:
    """Continuous-value uint8 STAC mosaic: search -> two-tier sign -> bilinear
    windowed reproject -> first-valid mosaic (``(mosaic_uint8, transform, crs,
    nodata)``). Reproduces the jrc-gsw twin ``_fetch_surface_water_cog_bytes``
    fetch leg (the colormap bake is in :func:`execute`).
    """
    import numpy as np
    import rasterio
    from rasterio.warp import Resampling, reproject

    from ...imagery import _pc_stac
    from ..transport import TransportError, open_windowed_cog

    ingest = spec.ingest or {}
    stac = ingest.get("stac", {})
    bbox = tuple(params["bbox"])
    collection = stac.get("collection")
    native_cell_m = float(ingest.get("native_cell_m", 30.0))
    nodata = _stac_continuous_nodata(spec, params)

    # asset key: a param-keyed map (jrc band -> asset name) or a static data_asset.
    abp = stac.get("asset_by_param")
    if abp:
        asset_key = (abp.get("map") or {})[params.get(abp.get("param"))]
    else:
        asset_key = stac.get("data_asset", "data")

    try:
        from pystac_client import Client
    except ImportError as exc:  # pragma: no cover -- pystac_client is a hard dep
        raise router_upstream_error(spec.error_code_prefix, f"pystac-client unavailable: {exc}")
    try:
        client = Client.open(stac.get("root", _pc_stac.PC_STAC_ROOT))
        search = client.search(collections=[collection], bbox=list(bbox), limit=100)
        all_items = list(search.items())
    except Exception as exc:  # noqa: BLE001 -- translate any pystac/http error
        raise router_upstream_error(
            spec.error_code_prefix,
            f"STAC search failed (collection={collection!r}, bbox={bbox}): {exc}")
    items = [it for it in all_items if _bbox_intersects(getattr(it, "bbox", None), bbox)]
    if not items:
        raise router_empty_error(
            spec.error_code_prefix,
            f"no {collection!r} item covers bbox={bbox}", spec.empty_error_suffix)

    width_px, height_px = _pc_stac.bbox_pixel_dims(bbox, native_cell_m)
    dst_transform = rasterio.transform.from_bounds(
        bbox[0], bbox[1], bbox[2], bbox[3], width_px, height_px
    )
    mosaic = np.full((height_px, width_px), nodata, dtype="uint8")
    filled = np.zeros((height_px, width_px), dtype=bool)
    for item in items:
        assets = getattr(item, "assets", {}) or {}
        if asset_key not in assets:
            continue
        signed = _pc_sign_two_tier(spec, assets[asset_key].href, str(collection))
        dst = np.full((height_px, width_px), nodata, dtype="uint8")
        try:
            with open_windowed_cog(signed) as src:
                reproject(
                    source=rasterio.band(src, 1), destination=dst,
                    src_transform=src.transform, src_crs=src.crs,
                    dst_transform=dst_transform, dst_crs="EPSG:4326",
                    resampling=Resampling.bilinear,
                    src_nodata=(src.nodata if src.nodata is not None else nodata),
                    dst_nodata=nodata,
                )
        except TransportError as exc:
            raise router_upstream_error(spec.error_code_prefix, f"STAC tile read failed: {exc}")
        except Exception as exc:  # noqa: BLE001 -- translate any rasterio/GDAL error
            raise router_upstream_error(spec.error_code_prefix, f"STAC tile read failed: {exc}")
        valid = (dst != nodata) & (~filled)
        if bool(valid.any()):
            mosaic[valid] = dst[valid]
            filled |= valid

    if not bool(filled.any()):
        # Items intersected but no real coverage over the AOI (ocean / fully dry).
        raise router_empty_error(
            spec.error_code_prefix,
            f"{collection!r} items intersected bbox={bbox} but the mosaic is "
            "entirely no-data over the AOI", spec.empty_error_suffix)
    return mosaic, dst_transform, "EPSG:4326", nodata


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
    if access == "mapserver_export":
        # A MapServer/export server-symbolized PNG32 georeferenced client-side into
        # a 4-band RGBA COG (noaa_slr conf_*/marsh_* overlays).
        return _mapserver_export_rgba_bytes(spec, params)
    if access == "stac_multi_asset_rgb":
        # a baked 3-band uint8 photometric-RGB COG (the imagery trio); the
        # RGB serializer is distinct from the float NaN-nodata path below.
        rgb, transform, crs = _stac_multi_asset_rgb_to_array(spec, params)
        try:
            return _rgb_cog_bytes(rgb, transform, crs)
        except RouterError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise router_upstream_error(spec.error_code_prefix, f"RGB COG write failed: {exc}")
    if access == "stac_search":
        ingest = spec.ingest or {}
        arr, transform, crs, colormap = stac_to_mosaic(spec, params)
        nodata = int((ingest.get("mosaic") or {}).get("nodata", 0))
        dtype = str(ingest.get("dtype", "uint8"))
        return array_to_cog_bytes(
            arr, transform, crs, nodata=nodata, dtype=dtype, colormap=colormap
        )
    if access == "stac_continuous_mosaic":
        # a continuous-value uint8 STAC mosaic (bilinear + per-band nodata
        # + two-tier PC REST /sign) with the PURE per-band colormap (hooks.colormap)
        # baked into the band-1 palette -- value-identical to the jrc-gsw twin's COG.
        arr, transform, crs, nodata = _stac_continuous_mosaic(spec, params)
        colormap: dict | None = None
        if spec.hooks is not None and spec.hooks.colormap:
            from ..hooks import resolve_hook

            colormap = resolve_hook(spec.hooks.colormap)(spec, params)
        return array_to_cog_bytes(
            arr, transform, crs, nodata=float(nodata), dtype="uint8", colormap=colormap
        )
    if access == "categorical_tile_grid":
        # a uint8 categorical first-valid mosaic + the declarative palette
        # baked into a 256-entry band-1 color table (nodata index transparent) --
        # value-identical to the twin's ``_fetch_flood_extent_cog_bytes`` COG.
        g = (spec.ingest or {}).get("categorical_tile_grid", {})
        nodata = int(g.get("nodata", 255))
        arr, transform, crs = _categorical_tile_grid_to_array(spec, params)
        colors = {int(k): tuple(int(c) for c in v) for k, v in (g.get("colors") or {}).items()}
        colormap = {i: colors.get(i, (0, 0, 0, 0)) for i in range(256)}
        return array_to_cog_bytes(
            arr, transform, crs, nodata=nodata, dtype="uint8", colormap=colormap
        )
    if access == "wcs_getcoverage":
        # WCS 1.0.0 GetCoverage categorical NLCD -> background(0)->nodata
        # remap -> palette COG (the source's embedded band-1 color table preserved,
        # nodata transparent) -- the twin's paletted, overview-carrying landcover COG.
        arr, transform, crs, colormap, nodata = _wcs_getcoverage_to_array(spec, params)
        return array_to_cog_bytes(
            arr, transform, crs, nodata=nodata, dtype="uint8", colormap=colormap
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
