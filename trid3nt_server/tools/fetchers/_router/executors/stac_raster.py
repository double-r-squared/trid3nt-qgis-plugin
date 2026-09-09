"""stac-raster executor: a STAC catalog read through ``odc.stac.load``.

One access mode (``ingest.access: stac``) serving three renders
(``ingest.render``):

- ``float``     a physical-scalar float32 grid (DN scale/offset, dB, masks).
- ``mosaic``    a uint8 first-valid mosaic with a palette baked into band 1.
- ``rgb``       a 3-band photometric-RGB uint8 composite.

The library owns search, signing, the destination grid and the pixel fuse; this
module owns the spec vocabulary (which collection, which asset, which scene) and
the band math no catalog does.

READ PATH. Assets are read by GDAL's own HTTP client, configured once through
``odc.loader.configure_rio`` -- odc opens its own rasterio env per read from a
module-global config, so an enclosing ``rasterio.Env`` does not reach it. The
upstream STATUS is verbatim on the exception (``HTTP response code: 404``) and
retries fire on 429/500/502/503/504. Two measured deviations from the transport's
norm are ledgered for this family: GDAL keeps the status but discards the S3 XML
``<Code>`` body, and its backoff is exponential-with-jitter rather than the
server's ``Retry-After``.

SOURCE NODATA is the source's own where the asset header publishes one. Where it
does not, the spec's declared sentinel stands in, so a fill value never blends
into a resampling kernel; the loader has no channel for that, so it rides in on
a reader (see ``_driver_with_src_nodata``).
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
from .raster_cog import array_to_cog_bytes

logger = logging.getLogger(
    "trid3nt_server.tools.fetchers._router.executors.stac_raster"
)

__all__ = ["execute", "stac_to_mosaic", "fetch_source_array"]

#: The read policy for every asset in this family. The retry half replaces
#: odc-stac's own default (10 tries at 0.5 s on GDAL's hard-coded code set) with
#: the explicit codes the transport retries on. The range half is what the
#: coalescing transport opener did for free: without it a windowed read of a
#: large COG issues one request per block, and a high-resolution window costs
#: thousands of round trips instead of a few merged ones.
_READ_PATH = {
    "GDAL_HTTP_MAX_RETRY": 5,
    "GDAL_HTTP_RETRY_DELAY": 1,
    "GDAL_HTTP_RETRY_CODES": "429,500,502,503,504",
    "GDAL_HTTP_MULTIRANGE": "YES",
    "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
    "VSI_CACHE": "TRUE",
    # A netrc-gated catalog answers the first read with a redirect chain that ends
    # in a per-user signed URL, and the login cookie has to survive it. One
    # writable path for both ends is what carries it; it is inert for a catalog
    # that needs no login.
    "GDAL_HTTP_COOKIEFILE": os.path.join(tempfile.gettempdir(), "trid3nt_gdal_cookies"),
    "GDAL_HTTP_COOKIEJAR": os.path.join(tempfile.gettempdir(), "trid3nt_gdal_cookies"),
}

_read_path_configured = False


def _configure_read_path() -> None:
    """Apply the read policy to odc's reader (idempotent, process-global)."""
    global _read_path_configured
    if _read_path_configured:
        return
    import odc.loader

    odc.loader.configure_rio(cloud_defaults=True, **_READ_PATH)
    _read_path_configured = True


# --------------------------------------------------------------------------- #
# Catalog: signing, collection/asset resolution, search, scene select.
# --------------------------------------------------------------------------- #


def _signing_modifier(spec: SourceSpec) -> Any:
    """The catalog's asset-signing hook, or a typed refusal naming what is missing.

    ``planetary_computer`` signs Azure blob hrefs off the PC SAS endpoint.
    ``netrc:<host>`` names a catalog whose assets are behind an interactive
    login: the refusal names the file and the host rather than failing at the
    first 401.
    """
    sign = str(((spec.ingest or {}).get("stac") or {}).get("sign") or "none")
    if sign == "planetary_computer":
        import planetary_computer

        return planetary_computer.sign_inplace
    if sign.startswith("netrc:"):
        host = sign.split(":", 1)[1]
        _require_netrc(spec, host)
        return None
    return None


def _require_netrc(spec: SourceSpec, host: str) -> None:
    """Refuse by name until ``~/.netrc`` carries credentials for ``host``."""
    import netrc as _netrc

    path = os.path.join(os.path.expanduser("~"), ".netrc")
    try:
        if _netrc.netrc(path).authenticators(host) is not None:
            return
    except (OSError, _netrc.NetrcParseError):
        pass
    raise router_upstream_error(
        spec.error_code_prefix,
        f"this source's assets are behind {host}; add a machine entry for {host} "
        f"to {path} (an Earthdata Login account is an interactive step); the "
        "cookie jar the redirect chain needs is already configured",
    )


def _normalize_via_aliases(spec: SourceSpec, value: Any, aliases: dict, allowed: list,
                           suffix: str) -> str:
    """Alias-normalize a param (lower->table, else upper) + validate membership."""
    key = str(value).strip().lower()
    norm = aliases.get(key, str(value).strip().upper())
    if norm not in allowed:
        raise router_input_error(
            spec.error_code_prefix, f"{value!r} not in {sorted(allowed)}", suffix
        )
    return norm


def _resolve_collection(spec: SourceSpec, params: dict[str, Any]) -> tuple[str, str | None]:
    """``(collection_id, normalized_product)`` -- static or param-keyed with aliasing."""
    stac = (spec.ingest or {}).get("stac", {})
    cbp = stac.get("collection_by_param")
    if not cbp:
        return str(stac.get("collection")), None
    prod = _normalize_via_aliases(
        spec, params.get(cbp["param"]), stac.get("product_aliases", {}),
        list((cbp.get("map") or {}).keys()),
        stac.get("param_error_suffix", "PARAM_INVALID"))
    return str(cbp["map"][prod]), prod


def _resolve_asset(spec: SourceSpec, params: dict[str, Any], product: str | None,
                   items: list[Any] | None = None) -> str:
    """The asset key to read: static, param-keyed, or matched on a declared suffix.

    A product whose asset keys are numbered per collection (OPERA DSWx's
    ``1_B01_WTR`` / ``0_B01_WTR``) declares the suffix its variants share, and
    the key is read off the items rather than hard-coded.
    """
    stac = (spec.ingest or {}).get("stac", {})
    suffix = stac.get("asset_suffix")
    if suffix:
        for item in items or []:
            for key in sorted(getattr(item, "assets", {}) or {}):
                if key.endswith(str(suffix)):
                    return key
        raise router_upstream_error(
            spec.error_code_prefix,
            f"no asset ending {suffix!r} on item "
            f"{getattr((items or [None])[0], 'id', '?')} "
            f"(have {sorted(getattr((items or [None])[0], 'assets', {}) or {})[:12]})")
    abp1 = stac.get("asset_by_param")
    abp = stac.get("asset_by_params")
    if abp1:
        return str(abp1["map"][params.get(abp1["param"])])
    if abp:
        dn = _normalize_via_aliases(
            spec, params.get(abp["params"][1]), stac.get("daynight_aliases", {}),
            ["day", "night"], stac.get("param_error_suffix", "PARAM_INVALID"))
        return str(abp["map"][product][dn])
    return str(stac.get("data_asset", "data"))


def _datetime_window(stac: dict, params: dict[str, Any]) -> str | None:
    """Both bounds -> that window; else a trailing N-day window; None if time-static."""
    year_param = stac.get("year_param")
    if year_param:
        year = params.get(year_param)
        return f"{year}-01-01/{year}-12-31" if year is not None else None
    if not stac.get("datetime_window"):
        return None
    d0, d1 = params.get("start_date"), params.get("end_date")
    if d0 and d1:
        return f"{d0}/{d1}"
    from datetime import datetime, timedelta, timezone

    days = int(stac.get("default_window_days", 120))
    end = datetime.now(timezone.utc).date()
    return f"{(end - timedelta(days=days)).isoformat()}/{end.isoformat()}"


def _search(spec: SourceSpec, params: dict[str, Any], collection: str,
            dt_range: str | None) -> list[Any]:
    """Search the catalog with the spec's declared query; typed empty on no items."""
    stac = (spec.ingest or {}).get("stac", {})
    sel = stac.get("select") or {}
    query: dict[str, Any] = {}
    if sel.get("cloud_query") and params.get("max_cloud_cover") is not None:
        query["eo:cloud_cover"] = {"lt": float(params["max_cloud_cover"])}
    pq = sel.get("platform_query")
    if pq:
        plats = list(pq.get("base", []))
        if params.get(pq.get("param")):
            plats = plats + list(pq.get("extra", []))
        query["platform"] = {"in": plats}

    bbox = tuple(params["bbox"])
    try:
        from pystac_client import Client
    except ImportError as exc:  # pragma: no cover -- pystac-client is a hard dep
        raise router_upstream_error(spec.error_code_prefix, f"pystac-client unavailable: {exc}")
    try:
        client = Client.open(stac.get("root"), modifier=_signing_modifier(spec))
        items = list(client.search(collections=[collection], bbox=list(bbox),
                                   datetime=dt_range, query=query or None,
                                   limit=100).items())
    except RouterError:
        raise
    except Exception as exc:  # noqa: BLE001 -- translate any pystac/http error
        raise router_upstream_error(
            spec.error_code_prefix,
            f"STAC search failed (collection={collection!r}, bbox={bbox}, "
            f"window={dt_range}): {exc}")

    if not items:
        raise router_empty_error(
            spec.error_code_prefix,
            f"no {collection!r} item intersects bbox={bbox}"
            + (f" in {dt_range}" if dt_range else "")
            + (f" under {params.get('max_cloud_cover')}% cloud cover"
               if sel.get("cloud_query") else ""),
            spec.empty_error_suffix)
    return items


def _aoi_coverage(item: Any, aoi: Any) -> float:
    """Fraction of ``aoi`` covered by ``item.geometry`` (0.0 on bad geometry)."""
    from shapely.geometry import shape

    try:
        return shape(item.geometry).intersection(aoi).area / aoi.area if aoi.area > 0 else 0.0
    except Exception:  # noqa: BLE001 -- bad geometry: treat as no coverage
        return 0.0


def _dt_key(item: Any) -> str:
    p = getattr(item, "properties", {}) or {}
    return p.get("datetime") or p.get("end_datetime") or p.get("start_datetime") or ""


def _rank_item(sel: dict, items: list[Any], bbox: tuple) -> Any:
    """The best item per the declarative ``select.rank`` keys.

    ``{by: coverage_bucket, min_frac}`` (full-coverage scenes first),
    ``{by: cloud_cover}`` (least-cloudy) or ``{by: coverage}`` (most AOI overlap).
    An empty rank returns the first item (most-recent intersecting).
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


def _select_items(spec: SourceSpec, params: dict[str, Any], items: list[Any],
                  asset_key: str) -> list[Any]:
    """Narrow the search result to the scenes the spec's select mode asks for.

    ``mosaic`` keeps every item (the load fuses them first-valid, in search
    order); ``latest`` takes the most recent; ``coverage`` ranks AOI coverage
    then recency over the scenes that actually carry the asset; ``best`` applies
    the declared rank ladder.
    """
    stac = (spec.ingest or {}).get("stac", {})
    sel = stac.get("select") or {}
    mode = sel.get("mode", "mosaic")
    bbox = tuple(params["bbox"])

    if mode == "mosaic":
        return items
    if mode == "latest":
        return [sorted(items, key=_dt_key, reverse=True)[0]]
    if mode == "coverage":
        from shapely.geometry import box

        aoi = box(*bbox)
        cand = [it for it in items if asset_key in (getattr(it, "assets", {}) or {})]
        if not cand:
            raise router_empty_error(
                spec.error_code_prefix,
                f"no scene carrying asset {asset_key!r} intersects bbox={bbox}",
                spec.empty_error_suffix)
        cand.sort(key=lambda it: (_aoi_coverage(it, aoi), _dt_key(it)), reverse=True)
        return [cand[0]]
    return [_rank_item(sel, items, bbox)]


# --------------------------------------------------------------------------- #
# The destination grid.
# --------------------------------------------------------------------------- #


def _source_cell(items: list[Any]) -> tuple[float, float, float] | None:
    """``(cell_deg, origin_x, origin_y)`` of the items' own EPSG:4326 lattice.

    Read off the STAC item's projection metadata, so a native-lattice ask costs
    no probe read. ``None`` when the item does not publish a geographic grid.
    """
    import odc.stac

    try:
        for gb in odc.stac.parse_item(items[0]).geoboxes():
            if str(gb.crs) == "EPSG:4326":
                tf = gb.transform
                return abs(float(tf.a)), float(tf.c), float(tf.f)
    except Exception as exc:  # noqa: BLE001 -- unknown grid, not a failed fetch
        logger.warning("router.stac: source-lattice read failed (%s)", exc)
    return None


def _geobox(spec: SourceSpec, params: dict[str, Any], items: list[Any],
            *, px_max: int | None = None) -> tuple[Any, str]:
    """``(GeoBox, resampling)`` -- the source's own lattice when asked for, else metric.

    Without ``px_per_deg`` the metric sizing applies: a grid derived from
    ``native_cell_m`` at the bbox mid-latitude. That is a lattice of the
    router's OWN, so it needs bilinear, and every value it returns is an
    interpolation of the source rather than the source.

    ``px_per_deg`` asks instead for the SOURCE's lattice, and the phase comes
    from the data. A global 1-arcsecond DEM tile is pixel-is-POINT: its pixel
    CENTRES sit on the integer arcsecond, so its edges sit half a pixel off the
    integer degree, and a lattice snapped to the prime meridian would land every
    destination centre exactly BETWEEN two source centres. Snapping to the
    source's own origin instead puts every destination pixel centre on a source
    pixel centre, and NEAREST carries the value across unchanged, so a consumer
    that samples the returned raster at its own nodes reads the number an
    in-container read of the tile would have returned.

    Two things fall back to the resampled grid rather than pretending: a source
    whose cell is not the declared density (the ask does not describe the data),
    and a lattice past the pixel cap (a large AOI).
    """
    import math

    import rasterio
    from odc.geo.geobox import GeoBox

    from ..._fetch_common import bbox_pixel_dims

    ingest = spec.ingest or {}
    bbox = tuple(params["bbox"])
    px_min = int(ingest.get("px_min", 16))
    px_max = int(px_max if px_max is not None else ingest.get("px_max", 4096))
    default_resampling = str(((ingest.get("stac") or {}).get("resampling")) or "bilinear")

    def _resampled() -> tuple[Any, str]:
        w, h = bbox_pixel_dims(bbox, float(ingest.get("native_cell_m", 1000.0)),
                               px_min=px_min, px_max=px_max)
        return GeoBox((h, w), rasterio.transform.from_bounds(*bbox, w, h),
                      "EPSG:4326"), default_resampling

    # The density may be a REQUEST param: the lattice a consumer samples against
    # is the consumer's fact, so it travels from the caller and the spec default
    # is only what applies when nobody said.
    px_per_deg = params.get("px_per_deg")
    if px_per_deg is None:
        px_per_deg = ingest.get("px_per_deg")
    if px_per_deg is None:
        return _resampled()

    density = float(px_per_deg)
    cell = 1.0 / density
    src = _source_cell(items)
    if src is None or abs(src[0] - cell) > cell * 1e-6:
        logger.warning(
            "router.stac: %g px/deg was asked for but the source grid is %s - the "
            "ask does not describe the data, so the metric grid applies",
            density, "unknown" if src is None else f"{src[0]:.10g} deg")
        return _resampled()

    _, ox, oy = src
    west = ox + math.floor((bbox[0] - ox) / cell) * cell
    east = ox + math.ceil((bbox[2] - ox) / cell) * cell
    north = oy - math.floor((oy - bbox[3]) / cell) * cell
    south = oy - math.ceil((oy - bbox[1]) / cell) * cell
    width = max(px_min, int(round((east - west) * density)))
    height = max(px_min, int(round((north - south) * density)))
    if width > px_max or height > px_max:
        logger.warning(
            "router.stac: the %g px/deg native lattice needs %dx%d px over bbox=%s, "
            "past the %d px cap - falling back to the metric grid (the delivered "
            "cell is COARSER than the ask)",
            density, width, height, tuple(round(v, 4) for v in bbox), px_max)
        return _resampled()
    return (GeoBox((height, width),
                   rasterio.transform.from_origin(west, north, cell, cell),
                   "EPSG:4326"), "nearest")


# --------------------------------------------------------------------------- #
# The load.
# --------------------------------------------------------------------------- #


def _one_group(item: Any, parsed: Any, idx: int) -> int:
    """Every item into one pixel plane, so the load returns a fused mosaic."""
    return 0


def _driver_with_src_nodata(nodata: float) -> Any:
    """A reader driver that carries a DECLARED source sentinel into the read.

    The loader resolves a source's nodata from the asset header alone. A source
    that publishes none (JRC's surface-water COGs) still HAS one, declared on the
    spec row, and excluding it from the resampling kernel is the difference
    between a class boundary and a blend of a class code with a fill value. The
    load API has no channel for it, so the value rides in on the reader.
    """
    from dataclasses import replace

    from odc.loader import RioDriver

    class _Reader:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def read(self, cfg: Any, *args: Any, **kwargs: Any) -> Any:
            return self._inner.read(replace(cfg, src_nodata_fallback=nodata),
                                    *args, **kwargs)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

    class _Driver(RioDriver):
        def open(self, src: Any, ctx: Any) -> Any:
            return _Reader(super().open(src, ctx))

    return _Driver()


def _load(spec: SourceSpec, items: list[Any], bands: list[str], geobox: Any,
          resampling: Any, *, dtype: str, nodata: float | None,
          src_nodata: float | None = None) -> dict[str, Any]:
    """``{band: 2D array}`` fused first-valid over ``items`` in the supplied order.

    Every source failure arrives here as the library's own exception carrying the
    verbatim upstream status; it is restamped with the spec's code, never
    swallowed.
    """
    import odc.stac

    _configure_read_path()
    # a band name is an asset key, optionally with a band index ("image.2")
    keys = {b.rsplit(".", 1)[0] if b.rsplit(".", 1)[-1].isdigit() else b for b in bands}
    missing = [k for k in sorted(keys)
               if not any(k in (getattr(it, "assets", {}) or {}) for it in items)]
    if missing:
        raise router_upstream_error(
            spec.error_code_prefix,
            f"item {getattr(items[0], 'id', '?')} missing asset(s) {missing} "
            f"(have {sorted(getattr(items[0], 'assets', {}) or {})[:12]})")
    try:
        ds = odc.stac.load(items, bands=bands, geobox=geobox, resampling=resampling,
                           groupby=_one_group, preserve_original_order=True,
                           dtype=dtype, nodata=nodata, use_overviews=False,
                           driver=(_driver_with_src_nodata(src_nodata)
                                   if src_nodata is not None else None))
    except RouterError:
        raise
    except Exception as exc:  # noqa: BLE001 -- translate any odc/rasterio/GDAL error
        raise router_upstream_error(spec.error_code_prefix, f"asset read failed: {exc}")
    return {b: ds[b].isel(time=0).values for b in bands}


# --------------------------------------------------------------------------- #
# Renders.
# --------------------------------------------------------------------------- #


def _render_float(spec: SourceSpec, params: dict[str, Any]) -> tuple[Any, Any, Any]:
    """Continuous-FLOAT read -> DN scale/offset -> float32 ``(array, transform, crs)``.

    NaN is the fill; the serialize directive decides whether ``execute`` writes it
    as NaN or as the source's own sentinel.
    """
    import numpy as np

    ingest = spec.ingest or {}
    tf = ingest.get("transform", {})
    collection, product = _resolve_collection(spec, params)
    found = _search(spec, params, collection,
                    _datetime_window(ingest.get("stac", {}), params))
    asset = _resolve_asset(spec, params, product, found)
    items = _select_items(spec, params, found, asset)
    geobox, resampling = _geobox(spec, params, items)

    fill_dn = tf.get("fill_dn")
    raw = _load(spec, items, [asset], geobox, resampling,
                dtype="float32", nodata=(float(fill_dn) if fill_dn is not None else None))[asset]

    scale = tf.get("scale")
    if scale is not None:
        out = (raw * float(scale) + float(tf.get("offset", 0.0))).astype("float32")
        if fill_dn is not None:
            out = np.where(raw == float(fill_dn), np.nan, out).astype("float32")
    else:
        out = raw.astype("float32")

    # positive_only: an importance product is strictly positive where mapped, so
    # <=0 (and non-finite) is nodata.
    if tf.get("positive_only"):
        out = np.where(np.isfinite(out) & (out > 0.0), out, np.nan).astype("float32")
    # log10_db: linear gamma0 power -> decibels. Non-positive / non-finite power
    # is not renderable backscatter, so it is nodata.
    if tf.get("log10_db"):
        valid = np.isfinite(out) & (out > 0.0)
        db = np.full(out.shape, np.nan, dtype="float32")
        db[valid] = (10.0 * np.log10(out[valid])).astype("float32")
        out = db

    if not bool(np.isfinite(out).any()):
        raise router_empty_error(
            spec.error_code_prefix,
            f"all-fill (no valid pixel) window over bbox={tuple(params['bbox'])}",
            spec.empty_error_suffix)
    return out, geobox.transform, "EPSG:4326"


def _source_colormap(spec: SourceSpec, items: list[Any], asset: str) -> dict | None:
    """The band-1 colour table the source itself publishes (palette passthrough)."""
    import rasterio

    try:
        with rasterio.open((getattr(items[0], "assets", {}) or {})[asset].href) as src:
            return src.colormap(1)
    except Exception as exc:  # noqa: BLE001 -- no palette is not a failed fetch
        logger.warning("router.stac: source palette read failed (%s)", exc)
        return None


def stac_to_mosaic(spec: SourceSpec, params: dict[str, Any]) -> tuple[Any, Any, str, dict | None]:
    """uint8 first-valid mosaic ``(array, transform, crs, colormap|None)``.

    The palette is the source's own where the spec declares passthrough, else the
    spec's pure colormap hook.
    """
    import numpy as np

    ingest = spec.ingest or {}
    mosaic_cfg = ingest.get("mosaic", {})
    nbp = mosaic_cfg.get("nodata_by_param")
    nodata = int((nbp.get("map") or {})[params.get(nbp.get("param"))]) if nbp \
        else int(mosaic_cfg.get("nodata", 0))

    collection, product = _resolve_collection(spec, params)
    found = _search(spec, params, collection,
                    _datetime_window(ingest.get("stac", {}), params))
    asset = _resolve_asset(spec, params, product, found)
    items = _select_items(spec, params, found, asset)
    geobox, resampling = _geobox(spec, params, items)
    # the sentinel is the destination fill AND the source's own where the asset
    # header publishes none (the header wins wherever it exists)
    arr = _load(spec, items, [asset], geobox, resampling, dtype="uint8",
                nodata=float(nodata), src_nodata=float(nodata))[asset]

    if not bool((arr != nodata).any()):
        raise router_empty_error(
            spec.error_code_prefix,
            f"{collection!r} items intersected bbox={tuple(params['bbox'])} but the "
            "mosaic is entirely no-data over the AOI", spec.empty_error_suffix)

    if ingest.get("palette") == "passthrough":
        colormap = _source_colormap(spec, items, asset)
    elif spec.hooks is not None and spec.hooks.colormap:
        from ..hooks import resolve_hook

        colormap = resolve_hook(spec.hooks.colormap)(spec, params)
    else:
        colormap = None
    return np.asarray(arr), geobox.transform, "EPSG:4326", colormap


def fetch_source_array(spec: SourceSpec, params: dict[str, Any]) -> tuple[Any, Any, Any]:
    """``(array, transform, crs)`` for the spec's render (the tiled-mosaic seam)."""
    render = str((spec.ingest or {}).get("render", "float"))
    if render == "mosaic":
        arr, transform, crs, _cmap = stac_to_mosaic(spec, params)
        return arr, transform, crs
    return _render_float(spec, params)


# --------------------------------------------------------------------------- #
# RGB composites: N single-band assets (or N bands of one asset) scaled, masked
# and rendered into a 3-band photometric-RGB uint8 COG.
# --------------------------------------------------------------------------- #


def _declare_asset_bands(items: list[Any], asset: str) -> int:
    """Restate an item's per-band list in the form the loader enumerates, and
    return how many bands the asset carries.

    A multi-band asset described only by the legacy ``eo:bands`` list resolves to
    a single band, because the loader counts the STAC 1.1 ``bands`` array. The
    count comes from the item's OWN band list either way -- this translates the
    form, it does not supply a number the item did not publish.
    """
    n = 1
    for item in items:
        a = (getattr(item, "assets", {}) or {}).get(asset)
        if a is None:
            continue
        declared = a.extra_fields.get("bands") or a.extra_fields.get("eo:bands") or []
        a.extra_fields.setdefault("bands", [{} for _ in declared])
        n = max(n, len(declared))
    return n


def _rgb_apply_transform(dn: Any, tf: dict) -> Any:
    """DN -> physical scalar per ``transform.kind`` (reflectance / lst_celsius /
    none); the fill DN reads back as NaN so the render's nodata rule masks it."""
    import numpy as np

    kind = tf.get("kind", "none")
    if kind == "reflectance":
        ref = dn * float(tf["scale"]) + float(tf.get("offset", 0.0))
        return np.where(dn == float(tf.get("fill_dn", 0)), np.nan, ref).astype("float32")
    if kind == "lst_celsius":
        c = (dn * float(tf["scale"]) + float(tf.get("offset", 0.0))
             - float(tf.get("kelvin_offset", 273.15)))
        return np.where(dn == float(tf.get("fill_dn", 0)), np.nan, c).astype("float32")
    return dn.astype("float32")


def _rgb_bad_mask(mask_arr: Any, mask: dict) -> Any:
    """Boolean bad (cloud/shadow/fill) mask from a QA bitmask or an SCL class set."""
    import numpy as np

    kind = mask.get("kind")
    if kind == "bitmask":
        bits = 0
        for b in mask.get("bad_bits", []):
            bits |= (1 << int(b))
        return np.asarray((mask_arr.astype("uint16") & bits) != 0)
    if kind == "classes":
        return np.isin(mask_arr.astype("int16"), np.asarray(mask.get("bad_classes", [])))
    return np.zeros(mask_arr.shape, dtype=bool)


def _rgb_render_joint_stretch(spec: SourceSpec, bands: list[Any], bad: Any, render: dict) -> Any:
    """Joint 2..98 percentile-stretch N float bands to a uint8 RGB (nodata+bad -> 0).

    ``nodata_rule=all_bands_zero`` (raw uint16 reflectance) else the non-finite
    fill from the reflectance transform. Raises a typed EMPTY when no clear pixel
    remains. Returns an ``(N, H, W)`` uint8 array.
    """
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
            "scene produced an all-cloud / all-nodata window over the AOI "
            "(no clear pixels to render)",
            spec.empty_error_suffix)
    lo = float(np.nanpercentile(clear_vals, float(render.get("lo_pct", 2.0))))
    hi = float(np.nanpercentile(clear_vals, float(render.get("hi_pct", 98.0))))
    span = max(float(render.get("span_floor", 1e-6)), hi - lo)
    rgb = np.nan_to_num(np.clip((stack - lo) / span * 255.0, 0.0, 255.0),
                        nan=0.0).astype("uint8")
    zero = nodata | bad
    for bi in range(rgb.shape[0]):
        rgb[bi][zero] = 0
    return rgb


def _rgb_render_colormap(spec: SourceSpec, band: Any, bad: Any, render: dict) -> Any:
    """Percentile-stretch one float band through a baked matplotlib ramp, zeroing
    nodata/clouded pixels. ``(3, H, W)`` uint8."""
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

        rgba = (matplotlib.colormaps[render.get("cmap", "inferno")](norm) * 255.0).astype("uint8")
        rgb = np.transpose(rgba[..., :3], (2, 0, 1))
    except Exception:  # noqa: BLE001 -- no matplotlib: manual black->red->yellow ramp
        r = np.clip(norm * 2.0, 0.0, 1.0)
        g = np.clip(norm * 2.0 - 1.0, 0.0, 1.0)
        rgb = (np.stack([r, g, np.zeros_like(norm)]) * 255.0).astype("uint8")
    nod = ~valid
    for bi in range(3):
        rgb[bi][nod] = 0
    return rgb


def _rgb_cog_bytes(rgb: Any, transform: Any, crs: Any) -> bytes:
    """Serialize a 3-band uint8 array to a photometric-RGB DEFLATE COG."""
    import rasterio

    count, height, width = rgb.shape
    out_fd, out_path = tempfile.mkstemp(suffix=".tif", prefix="trid3nt_router_rgb_")
    os.close(out_fd)
    try:
        with rasterio.open(out_path, "w", driver="COG", dtype="uint8", count=count,
                           height=height, width=width, crs=crs, transform=transform,
                           compress="DEFLATE", photometric="RGB") as dst:
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


def _render_rgb(spec: SourceSpec, params: dict[str, Any]) -> tuple[Any, Any, str]:
    """Search -> rank -> read the recipe's assets -> scale/mask/render -> 3-band uint8."""
    import numpy as np

    ingest = spec.ingest or {}
    comp = ingest.get("composite", {})
    bbox = tuple(params["bbox"])

    combo_param = comp.get("combo_param")
    combo = (params.get(combo_param) if combo_param else None) or comp.get("default_combo")
    recipe = (comp.get("combos") or {}).get(combo)
    if recipe is None:
        raise router_upstream_error(
            spec.error_code_prefix, f"no composite recipe for combo {combo!r}")

    collection, _product = _resolve_collection(spec, params)
    channels = list(recipe.get("assets", []))
    items = _select_items(
        spec, params,
        _search(spec, params, collection, _datetime_window(ingest.get("stac", {}), params)),
        channels[0])
    geobox, resampling = _geobox(spec, params, items,
                                 px_max=int(comp.get("px_max", 4096)))

    render = recipe.get("render", {})
    if render.get("kind") == "passthrough":
        # N bands of ONE asset read straight to uint8 (no scale/mask/stretch); a
        # band the asset does not carry stays 0.
        asset = channels[0]
        n = _declare_asset_bands(items, asset)
        idx = [int(b) for b in recipe.get("bands", [1, 2, 3]) if int(b) <= n][:3]
        loaded = _load(spec, items, [f"{asset}.{b}" for b in idx], geobox, resampling,
                       dtype="uint8", nodata=0.0)
        rgb = np.zeros((3, *geobox.shape), dtype="uint8")
        for i, b in enumerate(idx):
            rgb[i] = loaded[f"{asset}.{b}"]
        if not rgb.any():
            raise router_empty_error(
                spec.error_code_prefix,
                f"item {getattr(items[0], 'id', '?')} produced an all-black window "
                f"over bbox={bbox} (item does not actually cover the AOI)",
                spec.empty_error_suffix)
        return rgb, geobox.transform, "EPSG:4326"

    mask_asset = recipe.get("mask_asset")
    wanted = list(dict.fromkeys([*channels, *([mask_asset] if mask_asset else [])]))
    loaded = _load(spec, items, wanted, geobox,
                   {mask_asset: "nearest", "*": resampling} if mask_asset else resampling,
                   dtype="float32", nodata=0.0, src_nodata=0.0)

    tf = recipe.get("transform", {})
    bands = [_rgb_apply_transform(loaded[a], tf) for a in channels]
    bad = _rgb_bad_mask(loaded[mask_asset], recipe.get("mask", {})) if mask_asset else None
    if render.get("kind") == "colormap":
        rgb = _rgb_render_colormap(spec, bands[0], bad, render)
    else:
        rgb = _rgb_render_joint_stretch(spec, bands, bad, render)
    return rgb, geobox.transform, "EPSG:4326"


# --------------------------------------------------------------------------- #
# Entry point.
# --------------------------------------------------------------------------- #


def execute(spec: SourceSpec, params: dict[str, Any]) -> bytes:
    """Load the requested window from the catalog and serialize it to COG bytes."""
    ingest = spec.ingest or {}
    render = str(ingest.get("render", "float"))

    if render == "rgb":
        rgb, transform, crs = _render_rgb(spec, params)
        try:
            return _rgb_cog_bytes(rgb, transform, crs)
        except RouterError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise router_upstream_error(spec.error_code_prefix, f"RGB COG write failed: {exc}")

    if render == "mosaic":
        arr, transform, crs, colormap = stac_to_mosaic(spec, params)
        mosaic_cfg = ingest.get("mosaic", {})
        nbp = mosaic_cfg.get("nodata_by_param")
        nodata = int((nbp.get("map") or {})[params.get(nbp.get("param"))]) if nbp \
            else int(mosaic_cfg.get("nodata", 0))
        return array_to_cog_bytes(arr, transform, crs, nodata=float(nodata),
                                  dtype="uint8", colormap=colormap)

    arr, transform, crs = _render_float(spec, params)
    # A float source that writes a NON-NaN nodata sentinel declares it here;
    # absent, the NaN-nodata passthrough applies.
    ser = ingest.get("serialize") or {}
    out_nodata = ser.get("nodata")
    if out_nodata is None:
        return array_to_cog_bytes(arr, transform, crs)
    import numpy as np

    out_dtype = str(ser.get("dtype", "float32"))
    filled = np.where(np.isfinite(arr), arr, out_nodata).astype(out_dtype)
    return array_to_cog_bytes(filled, transform, crs, nodata=float(out_nodata),
                              dtype=out_dtype)
