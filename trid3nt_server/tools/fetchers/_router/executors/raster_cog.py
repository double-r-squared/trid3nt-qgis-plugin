"""raster-cog executor (contract sec 2.1).

Reads a gridded source to a CRS-tagged single-band COG over the router's own
transport: every sub-mode keyed by ``ingest.access`` reads through
``transport/range_file.py``, so GDAL parses and never networks. A STAC catalog
source reads through ``stac_raster.py`` instead.

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
    "trid3nt_server.tools.fetchers._router.executors.raster_cog"
)

__all__ = ["array_to_cog_bytes", "fetch_source_array", "execute"]


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
        StagedEndpointNotConfigured,
        TransportAuthError,
        TransportNotFound,
        is_staged_uri,
        open_windowed_cog,
        staged_object_url,
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
    # A staged dataset names its object by bucket/key; the host is deployment
    # state, resolved here against the active endpoint. A staged object is known
    # to exist (this repo uploaded it for full declared coverage), so any failure
    # resolving or fetching it is a config/upstream defect, never a coverage
    # answer -- distinguished from the ordinary 404->EMPTY path below.
    was_staged = is_staged_uri(url)
    if was_staged:
        try:
            url = staged_object_url(url)
        except StagedEndpointNotConfigured as exc:
            raise router_upstream_error(
                spec.error_code_prefix,
                f"STAGED_OBJECT_UNAVAILABLE: {exc} (bbox={bbox})",
            )

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
        if was_staged:
            # A staged object is uploaded for its declared coverage; a 404 here
            # means the resolved endpoint points at the wrong deployment (or the
            # upload is missing), NOT that the AOI lacks data.
            raise router_upstream_error(
                spec.error_code_prefix,
                f"STAGED_OBJECT_UNAVAILABLE: staged object not found at resolved "
                f"url={url} (checked AWS_ENDPOINT_URL_S3/AWS_ENDPOINT_URL) -- this "
                f"object is staged for full declared coverage, so a 404 is a "
                f"deployment/config defect, not an absence of data (bbox={bbox}): {exc}",
            )
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
        # A NaN sentinel needs the finiteness test: ``arr != nan`` is True for
        # every pixel INCLUDING the nodata ones, so the equality form would let an
        # entirely-empty window through as a valid layer.
        if sentinel != sentinel:
            has_valid = bool(np.isfinite(arr).any())
        else:
            has_valid = bool((arr != sentinel).any())
        if not has_valid:
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
    absent (that default day does NOT enter the cache key, an explicit date does). A 404 with the no-data
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
    import numpy as np
    import rasterio
    from rasterio.io import MemoryFile

    from trid3nt_server.tools.search.ogc_adapter import OGCAdapterError, fetch_ogc_layer

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
    from pyproj import Geod

    geod = Geod(ellps="WGS84")
    max_px = int(w.get("max_px", 4000))
    width_m = geod.inv(min_lon, mid_lat, max_lon, mid_lat)[2]
    height_m = geod.inv(min_lon, min_lat, min_lon, max_lat)[2]
    width_px = max(16, min(max_px, int(round(width_m / res_m))))
    height_px = max(16, min(max_px, int(round(height_m / res_m))))

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
    (H, W), ``nodata`` filling no-data pixels."""
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

    Two declared sizings. ``px_per_deg`` asks for a fixed pixel density per
    DEGREE on both axes -- an angular grid, so the cell is not square away from
    the equator, and a caller that wants the sample lattice reproduced exactly
    (rather than a metric cell) declares this one; the density itself may be a
    request param, so a param of the same name overrides the spec default.
    Otherwise the metric sizing applies: m/degree at the bbox midpoint latitude
    rounded to ``native_cell_m``. Both clamp per axis to ``px_min`` / ``px_max``.
    """
    px_min = int(ingest.get("px_min", 16))
    px_max = int(ingest.get("px_max", 4096))
    min_lon, min_lat, max_lon, max_lat = bbox
    px_per_deg = ingest.get("px_per_deg")
    if px_per_deg is not None:
        density = float(px_per_deg)
        width_px = max(px_min, min(px_max, int(round((max_lon - min_lon) * density))))
        height_px = max(px_min, min(px_max, int(round((max_lat - min_lat) * density))))
        return width_px, height_px
    cell_m = float(ingest.get("native_cell_m", 30.0))
    mid_lat = 0.5 * (min_lat + max_lat)
    from pyproj import Geod

    geod = Geod(ellps="WGS84")
    width_m = geod.inv(min_lon, mid_lat, max_lon, mid_lat)[2]
    height_m = geod.inv(min_lon, min_lat, min_lon, max_lat)[2]
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

    # The service is either FIXED on the spec (one mosaic, no choice to offer) or
    # resolved from a request param (the layer -> ImageServer map).
    service = img.get("service")
    svc_param = None
    if service is None:
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

    # A caller may declare the sample lattice itself (px_per_deg / max_px_per_side);
    # a request value overrides the spec default so one spec serves callers whose
    # grids differ, without either of them re-implementing the request.
    sizing = dict(img)
    for knob, key in (("px_per_deg", "px_per_deg"), ("max_px_per_side", "px_max")):
        if params.get(knob) is not None:
            sizing[key] = params[knob]
    width_px, height_px = _imageserver_size(bbox, sizing)
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


def execute(spec: SourceSpec, params: dict[str, Any]) -> bytes:
    """Fetch the source array and serialize to COG bytes (the ``fetch_fn`` body)."""
    access = (spec.ingest or {}).get("access", "opendap")
    if access == "imageserver_export":
        # The ImageServer exportImage response IS the artifact (no reserialize) --
        # value-identical to the twin's raw GeoTIFF body.
        return _imageserver_export_bytes(spec, params)
    if access == "mapserver_export":
        # A MapServer/export server-symbolized PNG32 georeferenced client-side into
        # a 4-band RGBA COG (noaa_slr conf_*/marsh_* overlays).
        return _mapserver_export_rgba_bytes(spec, params)
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
