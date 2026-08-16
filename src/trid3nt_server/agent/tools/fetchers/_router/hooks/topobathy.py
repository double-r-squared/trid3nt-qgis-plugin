"""Coastal topo-bathymetry delegate hooks: the ``fetch_topobathy`` fold.

``fetch_topobathy`` folds onto the router as a ``library_delegate`` raster source.
It is NOT a single-source raster read but a 4-leg UTM-precedence COMPOSITE (NOAA
NCEI CUDEM 1/9" tiles -> NCEI regional 1 m tiles -> ETOPO 2022 global fallback
bathy -> USGS 3DEP land via the sibling ``fetch_dem``), each reprojected onto one
shared EPSG:32616 grid + a per-tile NAVD88 datum gate. The heterogeneous discovery
+ warp-merge + datum gate IS our bespoke code, so it lives here as delegate hooks:

  * ``topobathy.validate`` (delegate_validate) -- the US-coastal-envelope + degenerate
    bbox + resolution / offset / timeout / min_pixel finiteness gate, raised
    pre-cache / pre-network as a ``TopobathyInputError``.
  * ``topobathy.read`` (delegate) -- the 4-leg select + datum gate + merge -> the
    composite ``(array, transform, crs)`` for the shared COG writer. It ALSO records
    the FETCH-TIME provenance (which legs painted the merge) via the provenance
    channel, so the four ``TopobathyResult`` fields survive a cache hit.
  * ``topobathy.envelope`` -- the twin's exact ``topobathy-...`` layer_id / name and
    the four provenance fields read back from the channel (declared defaults when a
    pre-channel cache object has no sidecar -- byte-identical to the twin's own
    cache-hit behaviour, which reverted to defaults).

The four fetch-time provenance fields are FETCH-TIME provenance -- which of the four
heterogeneous sources painted the merge -- and are NOT recoverable from the final
single-band float32 COG; the provenance channel (bytes + a cache-replayable sidecar)
is exactly the general capability that unblocks this fold.

LOUD-FALLBACK NORM (follow-up row, applied in the fold rather than
preserved): the 3DEP land leg's SILENT swallow becomes a LABELED ``land_absent``
degrade (a provenance entry + ``fallback_warning``), and the CUDEM -> ETOPO
proceed-and-warn is verified to reach the envelope on every path. Topobathy is NOT
hard-gated this wave -- coastal flood scenarios depend on best-effort terrain, so
labeling (not a pause-and-ask gate) is the agreed treatment for this consumer.

The ``TopobathyError`` classes live HERE (their stable importable home now that the
coded twin is deleted). Their base is ``FetchError`` so ``library_delegate.invoke``
passes them through unchanged (its ``except FetchError: raise`` passthrough),
preserving the pinned ``error_code`` through the delegate wrapper.
"""

from __future__ import annotations

import logging
import math
import os
import re
import tempfile
from typing import Any

from trid3nt_server.agent.tools.cache import record_provenance

from ..._fetch_common import FetchError
from . import register_hook

logger = logging.getLogger(
    "trid3nt_server.agent.tools.fetchers._router.hooks.topobathy"
)

__all__ = [
    "TopobathyError",
    "TopobathyInputError",
    "TopobathyUpstreamError",
    "TopobathyEmptyError",
    "TopobathyDatumError",
    "estimate_payload_mb",
    "estimate_payload_mb_detail",
    "validate_topobathy",
    "read_topobathy",
    "envelope_topobathy",
    "CUDEM_COLLECTION_ROOT",
    "CUDEM_URLLIST_URL",
    "ETOPO_GLOBAL_ROOT",
    "NCEI_REGIONAL_COASTAL_DEMS",
    "TARGET_CRS",
]


# ---------------------------------------------------------------------------
# Error types (typed-error surface). Base = FetchError so the pinned
# error_code survives library_delegate.invoke's passthrough.
# ---------------------------------------------------------------------------


class TopobathyError(FetchError):
    """Base class for fetch_topobathy failures.

    ``error_code`` maps to the WebSocket A.6 error frame emitted by the agent
    surface. ``retryable`` guides retry/clarify/fallback logic.
    """

    error_code: str = "TOPOBATHY_ERROR"
    retryable: bool = True


class TopobathyInputError(TopobathyError):
    """Bad inputs (bbox shape, out-of-range coordinates, bad datum offset)."""

    error_code = "TOPOBATHY_INPUT_INVALID"
    retryable = False


class TopobathyUpstreamError(TopobathyError):
    """CUDEM tile-index download / tile read / merge / COG materialization
    failure that is NOT a "no coverage" condition (network 5xx, GDAL read
    error, gdalwarp non-zero, etc.)."""

    error_code = "TOPOBATHY_UPSTREAM_ERROR"
    retryable = True


class TopobathyEmptyError(TopobathyError):
    """Neither CUDEM nor 3DEP produced any usable elevation for the AOI.

    This is the hard dead-end: no land DEM AND no bathy. The softer case --
    CUDEM missing but 3DEP land present -- does NOT raise; it degrades to a
    land-only DEM and returns a ``TopobathyResult`` carrying an honest
    ``bathymetry_present=False`` warning (data-source fallback norm)."""

    error_code = "TOPOBATHY_EMPTY"
    retryable = False


class TopobathyDatumError(TopobathyError):
    """A CUDEM tile's vertical datum is NOT NAVD88 and no documented NAVD88
    offset was supplied (Invariant 7 -- never silently merge mismatched
    datums)."""

    error_code = "TOPOBATHY_DATUM_MISMATCH"
    retryable = False


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

#: NOAA NCEI CUDEM 1/9 arc-second "Topobathy 2014" collection root (public
#: S3, anonymous read).
CUDEM_COLLECTION_ROOT = (
    "https://noaa-nos-coastal-lidar-pds.s3.amazonaws.com/"
    "dem/NCEI_ninth_Topobathy_2014_8483/"
)

#: Authoritative per-tile URL manifest (one https://...tif per line) -- the
#: tile-index footprint intersected with the AOI (filenames encode NW corners).
CUDEM_URLLIST_URL = CUDEM_COLLECTION_ROOT + "urllist8483.txt"

#: Each CUDEM tile is a 0.25-degree square; the filename encodes its NW corner.
_CUDEM_TILE_DEG = 0.25

#: GLOBAL topo-bathy FALLBACK -- NOAA NCEI ETOPO 2022 15 arc-second (~450 m)
#: "surface" relief COGs (land positive, sea floor NEGATIVE, positive-up). Used
#: when CUDEM has no coverage for the AOI so a coastal/tsunami run still gets a
#: real nearshore bed. EGM2008/MSL-referenced (NOT NAVD88), flagged honestly.
ETOPO_GLOBAL_ROOT = (
    "https://www.ngdc.noaa.gov/mgg/global/relief/ETOPO2022/data/15s/"
    "15s_surface_elev_gtif/"
)

#: ETOPO 2022 COG tiles are 15-degree squares on a complete global grid.
_ETOPO_TILE_DEG = 15.0

#: NCEI REGIONAL high-resolution coastal-DEM collections (the FINE nested layer)
#: for coasts CUDEM's hosted 1/9" collection omits (e.g. the US Pacific coast).
NCEI_REGIONAL_COASTAL_DEMS: tuple[dict[str, Any], ...] = (
    {
        "name": "CA_north_coned_DEM_2020_9181",
        "label": "USGS CoNED Northern California 1 m topo-bathy DEM (2020, NAVD88)",
        "bbox": (-124.5719, 37.7702, -122.4453, 42.0126),
        "stac_items_url": (
            "https://noaa-nos-coastal-lidar-pds.s3.amazonaws.com/"
            "dem/CA_north_coned_DEM_2020_9181/stac/noaa_item_collection_m9181.json"
        ),
    },
)

#: Target output CRS -- UTM 16N (the coastal SFINCS reference AOI). NAVD88
#: vertical is preserved (the merge + reproject only touches the horizontal grid).
TARGET_CRS = "EPSG:32616"

#: US coastal envelope (incl. AK + HI + territories) -- a coarse pre-screen so a
#: clearly-inland or foreign bbox fails fast before we download the manifest.
_US_COASTAL_BBOX: tuple[float, float, float, float] = (-180.0, 13.0, -64.0, 72.0)

#: Filename -> (NW-lat, NW-lon) parser. Example: ``ncei19_n30X00_w085X25_2019v1``.
_TILE_NAME_RE = re.compile(
    r"ncei19_n(?P<lat_i>\d{2})X(?P<lat_f>\d{2})_w(?P<lon_i>\d{2,3})X(?P<lon_f>\d{2})",
    re.IGNORECASE,
)

#: 6-dp bbox quantization (~0.1 m) for cache-key stability.
_BBOX_DECIMALS = 6

#: Single shared style preset (same continuous-DEM ramp as fetch_dem).
_STYLE_PRESET = "continuous_dem"

#: Absolute physical cap (metres) on a coastal topo-bathymetry elevation; any
#: |z| at or above this is an UNFLAGGED fill/nodata sentinel leak, masked to NaN.
_TOPOBATHY_SENTINEL_ABS = 9000.0

#: GDAL no-sign-request env for anonymous public-S3 /vsicurl/ reads. The MULTIRANGE +
#: MERGE + HTTP/2 options batch the many small per-block (256x256) range requests a
#: bbox-windowed CUDEM read issues into few multiplexed transfers -- without them each
#: tile block is a separate latency-bound round trip and a native-resolution AOI read
#: crawls (the dominant surge-bathymetry fetch cost).
_VSICURL_ENV_KW = dict(
    AWS_NO_SIGN_REQUEST="YES",
    GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
    CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif,.tiff,.vrt",
    VSI_CACHE=True,
    VSI_CACHE_SIZE="104857600",  # 100 MB per-file block cache
    GDAL_HTTP_MULTIRANGE="YES",
    GDAL_HTTP_MERGE_CONSECUTIVE_RANGES="YES",
    GDAL_HTTP_VERSION="2",
    GDAL_BAND_BLOCK_CACHE="HASHSET",
)


# ---------------------------------------------------------------------------
# Payload estimator (kept importable for tests; the router synthesizes its own
# from source.yaml's payload_estimate block for the promoted tool).
# ---------------------------------------------------------------------------


def _analytic_payload_mb(bbox: tuple[float, float, float, float] | None) -> float:
    """The coarse bytes-per-square-degree fallback (used when sampling is unavailable)."""
    if bbox is None:
        return 50.0
    try:
        west, south, east, north = bbox
        sq_deg = max(0.0, (east - west)) * max(0.0, (north - south))
    except (TypeError, ValueError):
        return 50.0
    return max(0.5, sq_deg * 400.0)


#: Window (pixels/side) read from the finest source tile to MEASURE emit density --
#: big enough to capture real compression, small enough to keep the gate sample fast.
_SAMPLE_WIN_PX = 512


def _sample_topobathy_density(
    aoi_bbox: tuple[float, float, float, float],
) -> Any:
    """Measure the emit density from a SMALL native window in the AOI (R-B sampling).

    Reads a ``_SAMPLE_WIN_PX`` window from the FINEST real source tile covering the AOI
    centre (CUDEM 1/9" where present, else the ETOPO global base), re-encodes it as the
    SAME float32 LZW COG the fetch emits to measure real bytes-per-pixel, and derives
    the native output pixel density from the tile's native cell projected to metres.
    Returns ``None`` (analytic fallback) when no real tile covers the AOI (offline / no
    coverage). One header-range open + one window read -- no 4-leg merge, no urllist
    beyond the centre intersect -- so it is gate-fast and cached per region."""
    import numpy as np
    import rasterio
    from rasterio.windows import Window

    from trid3nt_server.agent.tools.payload_sampling import SampledDensity

    w, s, e, n = (float(v) for v in aoi_bbox)
    cx, cy = 0.5 * (w + e), 0.5 * (s + n)
    half = 0.01
    win_bbox = (cx - half, cy - half, cx + half, cy + half)

    # Finest source tile over the AOI centre: CUDEM, else ETOPO global base.
    tile_url: str | None = None
    try:
        cudem = _select_cudem_tiles(win_bbox, 30.0)
        if cudem:
            tile_url = "/vsicurl/" + cudem[0]
    except Exception:  # noqa: BLE001 -- best-effort; fall through to ETOPO
        tile_url = None
    if tile_url is None:
        etopo = _select_etopo_tiles(win_bbox)
        if etopo:
            tile_url = "/vsicurl/" + etopo[0]
    if tile_url is None:
        return None

    with rasterio.Env(**_VSICURL_ENV_KW):
        with rasterio.open(tile_url) as ds:
            wpx = min(_SAMPLE_WIN_PX, ds.width)
            hpx = min(_SAMPLE_WIN_PX, ds.height)
            if wpx <= 0 or hpx <= 0:
                return None
            col0 = max(0, (ds.width - wpx) // 2)
            row0 = max(0, (ds.height - hpx) // 2)
            arr = ds.read(1, window=Window(col0, row0, wpx, hpx)).astype("float32")
            res_deg_x = abs(ds.transform.a)
            res_deg_y = abs(ds.transform.e)
            src_is_geographic = bool(getattr(ds.crs, "is_geographic", True))
            win_transform = ds.window_transform(Window(col0, row0, wpx, hpx))
            src_crs = ds.crs

    npx = int(arr.shape[0]) * int(arr.shape[1])
    if npx <= 0:
        return None
    cog_bytes = _array_to_topobathy_cog_bytes(arr, win_transform, src_crs)
    bytes_per_px = len(cog_bytes) / npx

    # Native OUTPUT pixel density: convert the source cell to metres (the output grid
    # is metric EPSG:32616 at the finest source cell), then px per square degree.
    if src_is_geographic:
        mid = math.radians(cy)
        res_m = min(res_deg_x * 111320.0 * math.cos(mid), res_deg_y * 110540.0)
    else:
        res_m = min(res_deg_x, res_deg_y)  # already metric
    res_m = max(res_m, 0.5)
    px_x_per_deg = 111320.0 * math.cos(math.radians(cy)) / res_m
    px_y_per_deg = 110540.0 / res_m
    px_per_sq_deg = px_x_per_deg * px_y_per_deg
    return SampledDensity(bytes_per_px=bytes_per_px, px_per_sq_deg=px_per_sq_deg)


def estimate_payload_mb(
    bbox: tuple[float, float, float, float] | None = None,
    resolution_m: float | int | None = None,
    **_kw: Any,
) -> float:
    """Estimate the emitted COG size in MB (R-B: measured sample, analytic fallback).

    Samples a small native window's real emit density (bytes/px + native pixel density)
    and scales it by the AOI area, bounded by the 12000 px-per-side guard; falls back
    to the bytes-per-square-degree analytic model when sampling is unavailable.
    ``resolution_m=None`` estimates the NATIVE composite; an explicit value estimates
    that coarsened grid."""
    if bbox is None:
        return _analytic_payload_mb(bbox)
    from trid3nt_server.agent.tools.payload_sampling import estimate_mb

    est = estimate_mb(
        "topobathy", tuple(float(v) for v in bbox),
        analytic_mb=_analytic_payload_mb(bbox),
        sample_fn=_sample_topobathy_density,
        resolution_m=float(resolution_m) if resolution_m else None,
    )
    return est.mb


def estimate_payload_mb_detail(
    bbox: tuple[float, float, float, float] | None = None,
    resolution_m: float | int | None = None,
    **_kw: Any,
) -> str | None:
    """Gate-text detail: the estimate + estimator KIND (measured vs analytic).

    Companion to ``estimate_payload_mb`` (the gate resolves ``<estimator>_detail``);
    returns a one-line human string naming whether the quoted number was measured from
    a sampled window or the analytic fallback, so the payload card is honest about its
    provenance. Returns ``None`` when there is no bbox to reason about."""
    if bbox is None:
        return None
    from trid3nt_server.agent.tools.payload_sampling import estimate_mb

    est = estimate_mb(
        "topobathy", tuple(float(v) for v in bbox),
        analytic_mb=_analytic_payload_mb(bbox),
        sample_fn=_sample_topobathy_density,
        resolution_m=float(resolution_m) if resolution_m else None,
    )
    grid = "native" if not resolution_m else f"{float(resolution_m):.0f} m"
    return f"topo-bathy {grid} grid ~{est.mb:.1f} MB ({est.kind})"


# ---------------------------------------------------------------------------
# bbox helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    if len(bbox) != 4:
        raise TopobathyInputError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    min_lon, min_lat, max_lon, max_lat = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise TopobathyInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise TopobathyInputError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise TopobathyInputError(f"bbox lat out of [-90,90]: {bbox!r}")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise TopobathyInputError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )
    west, south, east, north = _US_COASTAL_BBOX
    if max_lon < west or min_lon > east or max_lat < south or min_lat > north:
        raise TopobathyInputError(
            f"bbox {bbox} does not intersect the US coastal envelope "
            f"{_US_COASTAL_BBOX}; NOAA NCEI CUDEM is US-coast-only"
        )


def _round_bbox_to_6dp(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    return tuple(round(v, _BBOX_DECIMALS) for v in bbox)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# CUDEM tile-index intersect.
# ---------------------------------------------------------------------------


def _parse_tile_nw_corner(url_or_name: str) -> tuple[float, float] | None:
    """Parse the NW (upper-left) corner (lat, lon) of a CUDEM tile from its name."""
    m = _TILE_NAME_RE.search(url_or_name)
    if m is None:
        return None
    lat = float(m.group("lat_i")) + float(m.group("lat_f")) / 100.0
    lon = float(m.group("lon_i")) + float(m.group("lon_f")) / 100.0
    return (lat, -lon)


def _tile_intersects_bbox(
    nw_lat: float,
    nw_lon: float,
    bbox: tuple[float, float, float, float],
) -> bool:
    """A 0.25-deg CUDEM tile (NW corner at nw_lat/nw_lon) intersects the AOI?"""
    min_lon, min_lat, max_lon, max_lat = bbox
    tile_south = nw_lat - _CUDEM_TILE_DEG
    tile_north = nw_lat
    tile_west = nw_lon
    tile_east = nw_lon + _CUDEM_TILE_DEG
    return not (
        tile_east < min_lon
        or tile_west > max_lon
        or tile_north < min_lat
        or tile_south > max_lat
    )


def _fetch_cudem_urllist(timeout_s: float) -> list[str]:
    """Download the CUDEM per-tile URL manifest (urllist8483.txt)."""
    import requests  # lazy -- keep module import cheap

    try:
        resp = requests.get(CUDEM_URLLIST_URL, timeout=timeout_s)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        raise TopobathyUpstreamError(
            f"could not download CUDEM tile manifest {CUDEM_URLLIST_URL}: {exc}"
        ) from exc
    lines = [
        ln.strip()
        for ln in resp.text.splitlines()
        if ln.strip().lower().endswith(".tif")
    ]
    if not lines:
        raise TopobathyUpstreamError(
            f"CUDEM tile manifest {CUDEM_URLLIST_URL} parsed to zero .tif URLs "
            "(manifest format may have changed)"
        )
    return lines


def _select_cudem_tiles(
    bbox: tuple[float, float, float, float],
    timeout_s: float,
) -> list[str]:
    """Return the CUDEM tile URLs whose 0.25-deg footprint intersects the AOI."""
    urls = _fetch_cudem_urllist(timeout_s)
    selected: list[str] = []
    for url in urls:
        corner = _parse_tile_nw_corner(url)
        if corner is None:
            continue
        nw_lat, nw_lon = corner
        if _tile_intersects_bbox(nw_lat, nw_lon, bbox):
            selected.append(url)
    logger.info(
        "fetch_topobathy: %d/%d CUDEM tiles intersect bbox=%s",
        len(selected), len(urls), bbox,
    )
    return selected


# ---------------------------------------------------------------------------
# GLOBAL topo-bathy fallback tile-index intersect (NOAA ETOPO 2022 15").
# ---------------------------------------------------------------------------


def _etopo_url_for_corner(nw_lat: float, nw_lon: float) -> str:
    """Build the ETOPO 2022 15" COG URL for the 15-degree tile at NW ``(nw_lat, nw_lon)``."""
    ns = "N" if nw_lat >= 0 else "S"
    ew = "W" if nw_lon < 0 else "E"
    return (
        f"{ETOPO_GLOBAL_ROOT}ETOPO_2022_v1_15s_"
        f"{ns}{abs(int(round(nw_lat))):02d}{ew}{abs(int(round(nw_lon))):03d}"
        "_surface.tif"
    )


def _select_etopo_tiles(bbox: tuple[float, float, float, float]) -> list[str]:
    """Return the ETOPO 2022 15" COG URLs whose 15-degree footprint intersects the AOI."""
    min_lon, min_lat, max_lon, max_lat = bbox
    lat_south = math.floor(min_lat / _ETOPO_TILE_DEG) * _ETOPO_TILE_DEG
    lon_west = math.floor(min_lon / _ETOPO_TILE_DEG) * _ETOPO_TILE_DEG
    urls: list[str] = []
    s = lat_south
    while s < max_lat:
        nw_lat = s + _ETOPO_TILE_DEG
        w = lon_west
        while w < max_lon:
            tile_west, tile_east = w, w + _ETOPO_TILE_DEG
            tile_south, tile_north = s, nw_lat
            if not (
                tile_east < min_lon
                or tile_west > max_lon
                or tile_north < min_lat
                or tile_south > max_lat
            ):
                urls.append(_etopo_url_for_corner(nw_lat, w))
            w += _ETOPO_TILE_DEG
        s += _ETOPO_TILE_DEG
    logger.info(
        "fetch_topobathy: selected %d global ETOPO 2022 15\" fallback tile(s) "
        "for bbox=%s",
        len(urls), bbox,
    )
    return urls


# ---------------------------------------------------------------------------
# NCEI REGIONAL high-resolution coastal-DEM tile selection (the FINE shore layer).
# ---------------------------------------------------------------------------


def _select_regional_coastal_dem_tiles(
    bbox: tuple[float, float, float, float],
    timeout_s: float,
) -> tuple[list[str], list[str]]:
    """Return the FINE regional NCEI coastal-DEM tile URLs intersecting the AOI."""
    import requests  # lazy -- keep module import cheap

    min_lon, min_lat, max_lon, max_lat = bbox
    tiles: list[str] = []
    collections_hit: list[str] = []
    for coll in NCEI_REGIONAL_COASTAL_DEMS:
        cw, cs, ce, cn = coll["bbox"]
        if max_lon < cw or min_lon > ce or max_lat < cs or min_lat > cn:
            continue
        try:
            resp = requests.get(coll["stac_items_url"], timeout=timeout_s)
            resp.raise_for_status()
            fc = resp.json()
        except Exception as exc:  # noqa: BLE001 -- fine layer is best-effort
            logger.warning(
                "fetch_topobathy: could not load NCEI regional collection %s "
                "STAC items (%s); skipping this fine source",
                coll["name"], exc,
            )
            continue
        n_before = len(tiles)
        for feat in (fc.get("features") or []):
            b = feat.get("bbox")
            if not b or len(b) < 4:
                continue
            if b[2] < min_lon or b[0] > max_lon or b[3] < min_lat or b[1] > max_lat:
                continue
            for asset in (feat.get("assets") or {}).values():
                href = asset.get("href") if isinstance(asset, dict) else None
                if href and href.lower().endswith((".tif", ".tiff")):
                    tiles.append(str(href))
                    break
        if len(tiles) > n_before:
            collections_hit.append(coll["name"])
    logger.info(
        "fetch_topobathy: %d NCEI regional fine-DEM tile(s) from %s intersect "
        "bbox=%s", len(tiles), collections_hit or "[]", bbox,
    )
    return tiles, collections_hit


# ---------------------------------------------------------------------------
# Vertical-datum gate (Invariant 7).
# ---------------------------------------------------------------------------


def _assert_navd88(
    vsicurl_path: str,
    navd88_offset_m: float | None,
) -> float:
    """Datum gate for one CUDEM tile (reads the tile CRS WKT + tags, classifies)."""
    datum_text = ""
    try:
        import rasterio

        with rasterio.Env(**_VSICURL_ENV_KW):
            with rasterio.open(vsicurl_path) as ds:
                try:
                    crs = ds.crs
                    if crs is not None:
                        datum_text += " " + (crs.to_wkt() or "")
                except Exception:  # noqa: BLE001
                    pass
                try:
                    for k, v in (ds.tags() or {}).items():
                        datum_text += f" {k}={v}"
                    for k, v in (ds.tags(1) or {}).items():
                        datum_text += f" {k}={v}"
                except Exception:  # noqa: BLE001
                    pass
    except Exception as exc:  # noqa: BLE001
        raise TopobathyUpstreamError(
            f"could not read CUDEM tile header for datum check ({vsicurl_path}): {exc}"
        ) from exc

    return _classify_vertical_datum(datum_text, navd88_offset_m, vsicurl_path)


def _classify_vertical_datum(
    datum_text: str,
    navd88_offset_m: float | None,
    tile_id: str,
) -> float:
    """Pure decision over a vertical-datum string (metres to add to reach NAVD88)."""
    text = (datum_text or "").lower()
    if "navd88" in text or "navd 88" in text or "navd_88" in text:
        return 0.0
    tidal_markers = ("mhw", "mhhw", "mllw", "mlw", "lmsl", "msl", "mean sea level",
                     "mean high water", "mean low water", "tidal")
    if any(mk in text for mk in tidal_markers):
        if navd88_offset_m is not None:
            logger.warning(
                "fetch_topobathy: tile %s reports a non-NAVD88 tidal datum; "
                "applying supplied NAVD88 offset %.4f m (documented)",
                tile_id, navd88_offset_m,
            )
            return float(navd88_offset_m)
        raise TopobathyDatumError(
            f"CUDEM tile {tile_id} reports a non-NAVD88 vertical datum "
            f"(detected tidal datum in: {datum_text.strip()[:200]!r}); refusing "
            "to merge mismatched datums. Supply a documented navd88_offset_m for "
            "this AOI to convert, or use a NAVD88 tile."
        )
    if navd88_offset_m is not None:
        return float(navd88_offset_m)
    logger.info(
        "fetch_topobathy: tile %s carries no per-file vertical-CS tag; "
        "accepting CUDEM collection default (NAVD88, positive-up)",
        tile_id,
    )
    return 0.0


# ---------------------------------------------------------------------------
# 3DEP land DEM (REUSE fetch_dem via the registry closure -- seam-1).
# ---------------------------------------------------------------------------


def _fetch_3dep_land_to_file(
    bbox: tuple[float, float, float, float],
    resolution_m: int,
) -> str | None:
    """Fetch the 3DEP land DEM by reusing ``fetch_dem`` and stage it to a temp .tif.

    Returns the temp path, or ``None`` on failure (the caller degrades)."""
    try:
        from trid3nt_server.agent.tools import TOOL_REGISTRY

        fetch_dem = TOOL_REGISTRY["fetch_dem"].fn
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_topobathy: could not resolve fetch_dem: %s", exc)
        return None
    try:
        land_layer = fetch_dem(bbox=bbox, resolution_m=resolution_m)
    except Exception as exc:  # noqa: BLE001 -- land DEM is best-effort here
        logger.warning(
            "fetch_topobathy: 3DEP land fetch_dem failed for bbox=%s: %s",
            bbox, exc,
        )
        return None
    uri = land_layer.uri
    if not uri:
        return None
    return _stage_uri_to_local(uri)


def _stage_uri_to_local(uri: str) -> str | None:
    """Stage an ``s3://`` / local DEM URI to a local temp .tif for the merge."""
    if uri.startswith("/") or uri.startswith("file://"):
        return uri[len("file://"):] if uri.startswith("file://") else uri
    try:
        if uri.startswith("s3://"):
            from trid3nt_server.agent.tools.cache import read_object_bytes_s3

            data = read_object_bytes_s3(uri)
        else:
            logger.warning("fetch_topobathy: unknown DEM URI scheme: %s", uri)
            return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_topobathy: could not stage DEM %s locally: %s", uri, exc)
        return None
    with tempfile.NamedTemporaryFile(
        suffix=".tif", delete=False, prefix="trid3nt_topobathy_3dep_"
    ) as f:
        f.write(data)
        return f.name


# ---------------------------------------------------------------------------
# Merge + reproject + COG.
# ---------------------------------------------------------------------------


def _build_merged_topobathy(
    cudem_vsicurl_paths: list[str],
    land_local_path: str | None,
    datum_offsets: list[float],
    bbox: tuple[float, float, float, float],
    target_crs: str,
    etopo_paths: list[str] | None = None,
    regional_paths: list[str] | None = None,
    min_pixel_m: float | None = None,
) -> tuple[bytes, bool, int, int]:
    """Merge bathymetry + 3DEP land into one EPSG:32616 float32 COG.

    Precedence (LOW -> HIGH, last source WINS where it has valid data): ETOPO
    global base -> 3DEP land -> CUDEM 1/9" -> NCEI regional 1 m. Returns
    ``(cog_bytes, bathymetry_present, cudem_tile_count, regional_tile_count)``.
    """
    array, transform, crs, bathy_present, cudem_count, regional_count = (
        _merge_topobathy_to_array(
            cudem_vsicurl_paths,
            land_local_path,
            datum_offsets,
            bbox,
            target_crs,
            etopo_paths=etopo_paths,
            regional_paths=regional_paths,
            min_pixel_m=min_pixel_m,
        )
    )
    cog_bytes = _array_to_topobathy_cog_bytes(array, transform, crs)
    return cog_bytes, bathy_present, cudem_count, regional_count


def _merge_topobathy_to_array(
    cudem_vsicurl_paths: list[str],
    land_local_path: str | None,
    datum_offsets: list[float],
    bbox: tuple[float, float, float, float],
    target_crs: str,
    etopo_paths: list[str] | None = None,
    regional_paths: list[str] | None = None,
    min_pixel_m: float | None = None,
) -> tuple[Any, Any, Any, bool, int, int]:
    """Merge the sources onto one EPSG:32616 float32 grid; return the COMPOSITE
    ``(array, transform, crs, bathymetry_present, cudem_count, regional_count)``.

    The delegate returns this array directly for the shared COG writer; the
    ``_build_merged_topobathy`` byte helper wraps it into a COG (for the merge
    tests + any live byte-shape caller)."""
    etopo_paths = list(etopo_paths or [])
    regional_paths = list(regional_paths or [])
    have_cudem = len(cudem_vsicurl_paths) > 0
    have_etopo = len(etopo_paths) > 0
    have_regional = len(regional_paths) > 0
    have_land = land_local_path is not None
    if not have_cudem and not have_etopo and not have_regional and not have_land:
        raise TopobathyEmptyError(
            f"no CUDEM tiles, no NCEI regional fine DEM, no ETOPO global fallback "
            f"AND no 3DEP land DEM for bbox={bbox} -- no elevation data available "
            "for this AOI"
        )

    tmp_paths: list[str] = []
    try:
        adjusted_cudem: list[str] = []
        for path, offset in zip(cudem_vsicurl_paths, datum_offsets):
            if offset and abs(offset) > 1e-9:
                shifted = _apply_vertical_offset(path, offset)
                tmp_paths.append(shifted)
                adjusted_cudem.append(shifted)
            else:
                adjusted_cudem.append(path)
        sources_in_precedence = (
            etopo_paths
            + ([land_local_path] if have_land else [])  # type: ignore[list-item]
            + adjusted_cudem
            + regional_paths
        )
        array, transform, crs = _composite_sources_to_array(
            sources_in_precedence, target_crs, bbox, min_pixel_m=min_pixel_m
        )
        logger.info(
            "fetch_topobathy: merged %d CUDEM + %d regional-fine + %d ETOPO-global "
            "+ %s land -> composite array (%s)",
            len(cudem_vsicurl_paths), len(regional_paths), len(etopo_paths),
            "1" if have_land else "0", target_crs,
        )
        return (
            array, transform, crs,
            (have_cudem or have_regional or have_etopo),
            len(cudem_vsicurl_paths),
            len(regional_paths),
        )
    finally:
        for p in tmp_paths:
            try:
                os.unlink(p)
            except OSError:
                pass


def _array_to_topobathy_cog_bytes(array: Any, transform: Any, crs: Any) -> bytes:
    """Serialize the composite array to a single-band float32 NaN-nodata COG (LZW)."""
    import numpy as np
    import rasterio

    with tempfile.NamedTemporaryFile(
        suffix=".tif", delete=False, prefix="trid3nt_topobathy_cog_"
    ) as f:
        cog_path = f.name
    try:
        profile = {
            "driver": "COG", "dtype": "float32", "count": 1,
            "height": array.shape[0], "width": array.shape[1],
            "crs": crs, "transform": transform,
            "nodata": float("nan"), "compress": "LZW", "BIGTIFF": "IF_SAFER",
        }
        with rasterio.open(cog_path, "w", **profile) as dst:
            dst.write(np.asarray(array, dtype="float32"), 1)
        with rasterio.open(cog_path) as ds:
            assert ds.count == 1, f"expected single-band COG, got {ds.count}"
            assert str(ds.dtypes[0]) == "float32", f"expected float32, got {ds.dtypes[0]}"
        with open(cog_path, "rb") as fh:
            return fh.read()
    finally:
        try:
            os.unlink(cog_path)
        except OSError:
            pass


def _apply_vertical_offset(vsicurl_path: str, offset_m: float) -> str:
    """Add a constant vertical offset (metres) to a tile's elevations (temp .tif)."""
    import numpy as np
    import rasterio

    with rasterio.Env(**_VSICURL_ENV_KW):
        with rasterio.open(vsicurl_path) as ds:
            profile = ds.profile.copy()
            arr = ds.read(1, masked=True).astype("float32")
    arr = arr + np.float32(offset_m)
    profile.update(dtype="float32", driver="GTiff")
    with tempfile.NamedTemporaryFile(
        suffix=".tif", delete=False, prefix="trid3nt_topobathy_voffset_"
    ) as f:
        out = f.name
    with rasterio.open(out, "w", **profile) as dst:
        dst.write(arr.filled(profile.get("nodata", np.nan)).astype("float32"), 1)
    return out


#: Deep-water rung: the 3DEP land DEM returns a FLAT ~0 m fill over open
#: water (no bathymetry -- 3DEP is a bare-earth LAND product), and it sits at HIGHER
#: composite precedence than the ETOPO global topo-bathy base. Over a rupture/basin-
#: scale offshore domain (a finite-fault tsunami) that 0 m ocean fill CLOBBERS the
#: ETOPO deep column, flattening the sea to land-only (the Chignik land-only
#: refusal: raw min -68 m, 0 % below -5 m, from a genuine ETOPO base of min -6438 m,
#: 87 % below -5 m). When a caller forces the ETOPO bathy base ON (``force_bathy_base``
#: -- the offshore/tsunami intent), the 3DEP land leg must contribute ONLY genuine
#: emergent terrain (elevation ABOVE this waterline); its at/below-waterline cells are
#: masked to NaN so the ETOPO full-column shows through offshore. ETOPO 2022 is itself
#: a COMPLETE topo-bathy (land positive, sea negative), so the seam is: ETOPO base
#: (deep + shelf + coarse onshore) <- 3DEP fine onshore (positive only) <- CUDEM 1/9"
#: nearshore <- NCEI regional. Genuine below-datum US land is inland (not in an
#: offshore tsunami domain) and ETOPO already carries it, so the mask never drops
#: run-up-relevant terrain.
_LAND_LEG_WATERLINE_M = 0.0


def _mask_land_leg_ocean_fill(land_local_path: str) -> str:
    """Drop the 3DEP land DEM's at/below-waterline ocean-fill cells (deep-water rung).

    Reads the staged 3DEP land tif, masks every cell at or below
    ``_LAND_LEG_WATERLINE_M`` to NaN (the flat 0 m ocean fill + any negative fringe),
    and writes a temp GTiff carrying ONLY the genuine emergent (positive) terrain. The
    generic LAST-wins composite then lets the ETOPO full-column bathy base show through
    the masked cells offshore while the finer 3DEP land still paints onshore. Returns
    the temp path (registered by the caller for cleanup)."""
    import numpy as np
    import rasterio

    with rasterio.open(land_local_path) as ds:
        profile = ds.profile.copy()
        arr = ds.read(1).astype("float32")
    arr = np.where(arr <= np.float32(_LAND_LEG_WATERLINE_M), np.float32("nan"), arr)
    profile.update(dtype="float32", driver="GTiff", nodata=float("nan"))
    with tempfile.NamedTemporaryFile(
        suffix=".tif", delete=False, prefix="trid3nt_topobathy_landmask_"
    ) as f:
        out = f.name
    with rasterio.open(out, "w", **profile) as dst:
        dst.write(arr, 1)
    return out


def _merge_sources(
    sources_in_precedence: list[str],
    target_crs: str,
    bbox: tuple[float, float, float, float],
    min_pixel_m: float | None = None,
) -> str:
    """Mosaic ``sources_in_precedence`` (LAST wins) onto ``target_crs``, clipped to
    ``bbox``; return a path to the merged float32 GTiff (per-source warp, no CLI)."""
    if not sources_in_precedence:
        raise TopobathyEmptyError("no sources to merge")
    array, transform, crs = _composite_sources_to_array(
        sources_in_precedence, target_crs, bbox, min_pixel_m=min_pixel_m
    )
    import rasterio

    with tempfile.NamedTemporaryFile(
        suffix=".tif", delete=False, prefix="trid3nt_topobathy_merged_"
    ) as f:
        merged_path = f.name
    profile = {
        "driver": "GTiff", "dtype": "float32", "count": 1,
        "height": array.shape[0], "width": array.shape[1],
        "crs": crs, "transform": transform, "nodata": float("nan"),
    }
    with rasterio.open(merged_path, "w", **profile) as out:
        out.write(array, 1)
    return merged_path


def _compute_target_grid(
    sources_in_precedence: list[str],
    target_crs: str,
    bbox: tuple[float, float, float, float],
    min_pixel_m: float | None = None,
):
    """Build the common bbox-aligned target grid (transform, width, height)."""
    import math as _math

    import rasterio
    from rasterio.warp import transform_bounds

    west, south, east, north = bbox
    t_west, t_south, t_east, t_north = transform_bounds(
        "EPSG:4326", target_crs, west, south, east, north, densify_pts=21
    )
    if not (t_east > t_west and t_north > t_south):
        raise TopobathyUpstreamError(
            f"degenerate AOI bounds in {target_crs}: "
            f"({t_west}, {t_south}, {t_east}, {t_north})"
        )

    finest_res: float | None = None
    with rasterio.Env(**_VSICURL_ENV_KW):
        for src in sources_in_precedence:
            try:
                with rasterio.open(src) as ds:
                    px_w = abs(ds.transform.a)
                    px_h = abs(ds.transform.e)
                    s_crs = ds.crs
                    cx, cy = (ds.bounds.left + ds.bounds.right) / 2.0, (
                        ds.bounds.bottom + ds.bounds.top
                    ) / 2.0
                    rt_w, rt_s, rt_e, rt_n = transform_bounds(
                        s_crs, target_crs, cx, cy, cx + px_w, cy + px_h, densify_pts=2,
                    )
                    res_m = min(abs(rt_e - rt_w), abs(rt_n - rt_s))
            except Exception as exc:  # noqa: BLE001 -- skip an unreadable source
                logger.warning(
                    "fetch_topobathy: could not probe resolution of %s: %s", src, exc,
                )
                continue
            if res_m and res_m > 0 and (finest_res is None or res_m < finest_res):
                finest_res = res_m

    if finest_res is None or finest_res <= 0:
        finest_res = 3.0
    if min_pixel_m is not None and finest_res < float(min_pixel_m):
        finest_res = float(min_pixel_m)

    width = max(1, int(_math.ceil((t_east - t_west) / finest_res)))
    height = max(1, int(_math.ceil((t_north - t_south) / finest_res)))
    _MAX_DIM = 12000
    if width > _MAX_DIM or height > _MAX_DIM:
        scale = max(width / _MAX_DIM, height / _MAX_DIM)
        finest_res *= scale
        width = max(1, int(_math.ceil((t_east - t_west) / finest_res)))
        height = max(1, int(_math.ceil((t_north - t_south) / finest_res)))
        logger.info(
            "fetch_topobathy: AOI grid exceeded %d px at native res; coarsened "
            "to %.2f m (%d x %d)", _MAX_DIM, finest_res, width, height,
        )

    from rasterio.transform import from_origin

    dst_transform = from_origin(t_west, t_north, finest_res, finest_res)
    return dst_transform, width, height


def _source_res_m(ds: Any) -> float:
    """Approximate a source's native cell size in METRES (geographic -> mid-lat scaled)."""
    res = abs(ds.transform.a)
    try:
        if ds.crs is not None and ds.crs.is_geographic:
            lat = (ds.bounds.bottom + ds.bounds.top) / 2.0
            return res * 111320.0 * max(math.cos(math.radians(lat)), 0.1)
    except Exception:  # noqa: BLE001
        pass
    return res


def _decimated_source_read(
    ds: Any, target_res_m: float, aoi_bbox_4326: tuple[float, float, float, float],
) -> tuple[Any, Any]:
    """Read a source band CLIPPED to the AOI and decimated to ~the target resolution,
    returning ``(array_float32, transform)`` (or ``(None, None)`` when the AOI does not
    intersect this source).

    Reading a full-native CUDEM 1/9" tile (8112x8112) only to resample it onto a coarse
    bbox-clipped output grid is the dominant fetch cost (many tiles over /vsicurl).
    CUDEM COGs carry no overviews but ARE internally tiled, so a bbox WINDOW read pulls
    only the AOI-overlapping blocks (skipping the tile regions outside the AOI), and the
    ``out_shape`` decimation then shrinks the decoded array + the downstream reproject
    cost. The read is oversampled ~2x relative to the target cell so the bilinear
    reprojection stays clean; a source already at/coarser than the target is read at
    the window's native size."""
    import numpy as np
    import rasterio
    from rasterio.warp import transform_bounds
    from rasterio.windows import Window, from_bounds

    w, s, e, n = aoi_bbox_4326
    full = Window(0, 0, ds.width, ds.height)
    try:
        sw, ss, se, sn = transform_bounds("EPSG:4326", ds.crs, w, s, e, n, densify_pts=21)
        win = from_bounds(sw, ss, se, sn, ds.transform).round_offsets().round_lengths()
        win = win.intersection(full)
    except Exception:  # noqa: BLE001 -- fall back to the whole tile
        win = full
    if win.width < 1 or win.height < 1:
        return None, None

    src_res_m = _source_res_m(ds)
    factor = 1
    if target_res_m and src_res_m and src_res_m > 0:
        factor = max(1, int(target_res_m / src_res_m / 2.0))
    oh = max(1, int(win.height) // factor)
    ow = max(1, int(win.width) // factor)
    arr = ds.read(1, window=win, out_shape=(oh, ow), masked=True).astype("float32")
    transform = ds.window_transform(win) * rasterio.Affine.scale(
        int(win.width) / ow, int(win.height) / oh
    )
    return arr.filled(np.nan).astype("float32"), transform


def _composite_sources_to_array(
    sources_in_precedence: list[str],
    target_crs: str,
    bbox: tuple[float, float, float, float],
    min_pixel_m: float | None = None,
) -> tuple[Any, Any, str]:
    """Per-source warp + precedence composite -> ``(array, transform, target_crs)``.

    NEVER ``rasterio.merge``s raw heterogeneous sources (the upside-down MergeError
    for the CUDEM-EPSG:4269 + 3DEP-EPSG:5070 mix): each source is reprojected from
    its OWN CRS onto the shared bbox-clipped grid (normalising CRS + orientation),
    an unflagged |z|>=cap sentinel is masked to NaN, then composited LAST-wins."""
    import numpy as np
    import rasterio
    from rasterio.warp import Resampling, reproject

    if not sources_in_precedence:
        raise TopobathyEmptyError("no sources to merge")

    dst_transform, width, height = _compute_target_grid(
        sources_in_precedence, target_crs, bbox, min_pixel_m=min_pixel_m
    )

    composite = np.full((height, width), np.nan, dtype="float32")
    any_painted = False

    target_res_m = abs(dst_transform.a)
    with rasterio.Env(**_VSICURL_ENV_KW):
        for src in sources_in_precedence:
            try:
                with rasterio.open(src) as ds:
                    src_arr, src_transform = _decimated_source_read(
                        ds, target_res_m, bbox
                    )
                    if src_arr is None:
                        continue  # AOI does not intersect this source
                    src_arr = np.where(
                        np.abs(src_arr) >= np.float32(_TOPOBATHY_SENTINEL_ABS),
                        np.float32("nan"),
                        src_arr,
                    )
                    src_crs = ds.crs
            except Exception as exc:  # noqa: BLE001 -- skip an unreadable source
                logger.warning(
                    "fetch_topobathy: skipping unreadable merge source %s: %s", src, exc,
                )
                continue

            warped = np.full((height, width), np.nan, dtype="float32")
            reproject(
                source=src_arr,
                destination=warped,
                src_transform=src_transform,
                src_crs=src_crs,
                dst_transform=dst_transform,
                dst_crs=target_crs,
                src_nodata=np.nan,
                dst_nodata=np.nan,
                resampling=Resampling.bilinear,
            )
            valid = ~np.isnan(warped)
            if valid.any():
                composite[valid] = warped[valid]
                any_painted = True

    if not any_painted:
        raise TopobathyUpstreamError(
            "merge produced no valid cells -- all sources were empty / "
            "unreadable / outside the AOI"
        )
    return composite, dst_transform, target_crs


# ---------------------------------------------------------------------------
# Orchestration -- the 4-leg select + merge, returning the array + provenance.
# ---------------------------------------------------------------------------


def _compose_fallback_warnings(
    *,
    bbox: tuple[float, float, float, float],
    cudem_status: str,
    cudem_count: int,
    regional_count: int,
    has_etopo: bool,
    bathy_present: bool,
    land_absent: bool,
) -> str | None:
    """Build the LABELED fallback-warning string (data-source + loud-fallback norms).

    Pure over the merge outcome so both R-C branches are testable without a fetch. The
    GLOBAL-FALLBACK cause is HONEST: only ``cudem_status == "no_intersect"`` (a real
    tile-index intersect that returned zero) may claim the collection omits this coast;
    a caller ``skipped`` / an ``index_unreachable`` / a datum-gated ``present`` each
    name their own true cause (the 0221 fix -- the old text lied when CUDEM was
    skipped)."""
    warnings: list[str] = []
    if not bathy_present:
        warnings.append(
            "BATHYMETRY ABSENT: no NOAA NCEI CUDEM / regional topo-bathy tiles AND "
            f"no global ETOPO 2022 fallback were available for this AOI {bbox}; the "
            "elevation surface is 3DEP LAND-ONLY (below-waterline cells are nodata). "
            "A coastal flood / surge / tsunami run on this DEM has NO nearshore bed "
            "and will under-represent inundation. Treat results as land-pluvial only "
            "until bathymetry is available."
        )
    elif cudem_count == 0 and regional_count == 0 and has_etopo:
        if cudem_status == "skipped":
            cause = ("the fine NOAA NCEI CUDEM 1/9\" nearshore composite was SKIPPED "
                     "for this run (a coarse screening acquisition where CUDEM's fine "
                     "detail could not survive the requested grid)")
        elif cudem_status == "index_unreachable":
            cause = ("the NOAA NCEI CUDEM tile index could not be reached, so CUDEM "
                     "coverage for this AOI could not be determined")
        elif cudem_status == "present":
            cause = ("NOAA NCEI CUDEM 1/9\" tiles intersect this AOI but every one "
                     "failed the vertical-datum / header gate")
        else:  # no_intersect -- a real intersect returned zero
            cause = ("no NOAA NCEI CUDEM 1/9\" topo-bathy tiles cover this AOI "
                     "(CUDEM's hosted collection omits this coast)")
        warnings.append(
            f"GLOBAL-FALLBACK BATHYMETRY: {cause} for AOI {bbox}; nearshore "
            "bathymetry was sourced from the GLOBAL NOAA ETOPO 2022 15 arc-second "
            "relief model (~450 m, EGM2008/MSL-referenced rather than NAVD88, a "
            "sub-metre vertical offset). This provides a REAL below-waterline bed "
            "(so a tsunami/surge run produces actual inundation) but is COARSER than "
            "CUDEM; treat nearshore detail as approximate."
        )
    # LOUD-FALLBACK NORM (the 0091 follow-up): the 3DEP land leg's SILENT swallow is a
    # LABELED degrade -- when land failed but a bathy source is present the surface
    # proceeds bathy-only, named honestly (never a silent land drop). (Land absent AND
    # bathy absent already raised EmptyError upstream.)
    if land_absent and bathy_present:
        warnings.append(
            "LABELED DEGRADE (land_absent): the USGS 3DEP land DEM leg failed for "
            f"this AOI {bbox}; the surface is BATHYMETRY-ONLY (onshore / above-"
            "waterline cells are nodata). Onshore inundation extent is not "
            "represented -- treat above-waterline results as best-effort until the "
            "land DEM is available."
        )
    return " ".join(warnings) or None


def _select_and_merge(
    bbox: tuple[float, float, float, float],
    resolution_m: int,
    target_crs: str,
    navd88_offset_m: float | None,
    timeout_s: float,
    force_bathy_base: bool,
    include_regional_fine: bool,
    min_pixel_m: float | None,
    skip_cudem: bool = False,
    skip_land: bool = False,
) -> tuple[Any, Any, str, dict[str, Any]]:
    """Run the 4-leg discovery + datum gate + merge; return ``(array, transform,
    crs, provenance)``. ``provenance`` carries the four TopobathyResult fields plus
    the LABELED loud-degrade warnings.

    ``skip_cudem`` drops the fine NOAA CUDEM 1/9" nearshore composite (and its
    per-tile network reads) -- a SCREENING caller (e.g. a coarse surge TIN) that
    only needs the GLOBAL ETOPO shelf base + 3DEP land, where reading dozens of
    CUDEM tiles over a large domain is both wasted (at coarse node density) and the
    dominant time/failure cost. It forces the ETOPO bathy base on so a real
    below-waterline bed is still present.

    ``skip_land`` drops the 3DEP land leg. The 3DEP land DEM fills the nearshore
    ocean with a 0 m sea-level value that (as the higher-precedence source) CLOBBERS
    the ETOPO negative bathy over water -- flattening a surge domain to ~0 m depth.
    ETOPO 2022 is already a COMPLETE topo-bathy (land positive, sea negative), so a
    screening surge mesh (whose land nodes are clamped to min-wet anyway) wants
    ETOPO-only: real negative bathy offshore, no 0 m ocean clobber."""
    # 1) CUDEM tiles (best-effort -- empty == no coverage). ``cudem_status`` records
    # WHY the fine composite is absent so the fallback warning is HONEST: only a real
    # tile-index intersect that returns zero may claim the collection omits this coast
    # (NATE resolution doctrine, 2026-08-11 -- the 0221 blockiness was CUDEM SKIPPED by
    # the caller, not absent, so the old "collection omits this coast" text lied).
    cudem_urls: list[str] = []
    cudem_status: str  # skipped | index_unreachable | no_intersect | present
    if skip_cudem:
        force_bathy_base = True  # ETOPO shelf base replaces the skipped CUDEM bathy
        cudem_status = "skipped"
        logger.info("fetch_topobathy: skip_cudem -- caller acquisition on the "
                    "ETOPO global shelf base (no CUDEM tile reads)")
    else:
        try:
            cudem_urls = _select_cudem_tiles(bbox, timeout_s)
            cudem_status = "present" if cudem_urls else "no_intersect"
        except TopobathyUpstreamError as exc:
            logger.warning(
                "fetch_topobathy: CUDEM tile-index unreachable (%s); degrading to "
                "3DEP-land-only", exc,
            )
            cudem_urls = []
            cudem_status = "index_unreachable"
    cudem_vsicurl: list[str] = [f"/vsicurl/{u}" for u in cudem_urls]

    # 2) Datum gate per selected CUDEM tile (Invariant 7).
    datum_offsets: list[float] = []
    gated_paths: list[str] = []
    for vp in cudem_vsicurl:
        try:
            offset = _assert_navd88(vp, navd88_offset_m)
        except TopobathyUpstreamError as exc:
            logger.warning(
                "fetch_topobathy: skipping CUDEM tile (header unreadable): %s", exc
            )
            continue
        gated_paths.append(vp)
        datum_offsets.append(offset)
    cudem_vsicurl = gated_paths

    # 2b) GLOBAL ETOPO 2022 base / fallback.
    etopo_vsicurl: list[str] = []
    if force_bathy_base or not cudem_vsicurl:
        try:
            etopo_urls = _select_etopo_tiles(bbox)
            etopo_vsicurl = [f"/vsicurl/{u}" for u in etopo_urls]
        except Exception as exc:  # noqa: BLE001 -- fallback is best-effort
            logger.warning(
                "fetch_topobathy: ETOPO global-fallback tile selection failed "
                "(%s); will degrade to 3DEP-land-only", exc,
            )
            etopo_vsicurl = []

    # 2c) NCEI REGIONAL fine SHORE DEM (~1 m).
    regional_vsicurl: list[str] = []
    regional_collections: list[str] = []
    if include_regional_fine:
        try:
            regional_urls, regional_collections = _select_regional_coastal_dem_tiles(
                bbox, timeout_s
            )
            regional_vsicurl = [f"/vsicurl/{u}" for u in regional_urls]
        except Exception as exc:  # noqa: BLE001 -- fine layer is best-effort
            logger.warning(
                "fetch_topobathy: NCEI regional fine-DEM selection failed (%s); "
                "degrading to CUDEM / ETOPO", exc,
            )
            regional_vsicurl = []

    # 3) 3DEP land DEM (REUSE fetch_dem) -- best-effort. Skipped for a screening
    # surge (its 0 m ocean fill would clobber the ETOPO negative bathy over water).
    land_local = None if skip_land else _fetch_3dep_land_to_file(bbox, resolution_m)
    land_absent = land_local is None
    # Deep-water rung: when the ETOPO bathy base is forced ON (the
    # offshore/tsunami intent), the 3DEP land leg contributes ONLY genuine emergent
    # terrain -- its flat ~0 m ocean fill is masked so the ETOPO full-column deep
    # bathy shows through offshore instead of being clobbered to land-only. Onshore
    # (positive) 3DEP detail is preserved; ETOPO/CUDEM paint the wet nearshore.
    land_raw_tmp: str | None = land_local  # the un-masked 3DEP staged file (cleanup)
    if force_bathy_base and land_local is not None:
        try:
            land_local = _mask_land_leg_ocean_fill(land_local)
        except Exception as exc:  # noqa: BLE001 -- best-effort; fall back to raw land
            logger.warning(
                "fetch_topobathy: deep-water land-leg ocean-fill mask failed (%s); "
                "using the raw 3DEP land leg", exc,
            )

    # 4) Merge / reproject -> composite array.
    have_land = land_local is not None
    adjusted_cudem: list[str] = []
    tmp_paths: list[str] = []
    array = transform = crs = None
    try:
        for path, offset in zip(cudem_vsicurl, datum_offsets):
            if offset and abs(offset) > 1e-9:
                shifted = _apply_vertical_offset(path, offset)
                tmp_paths.append(shifted)
                adjusted_cudem.append(shifted)
            else:
                adjusted_cudem.append(path)
        if not (cudem_vsicurl or etopo_vsicurl or regional_vsicurl or have_land):
            raise TopobathyEmptyError(
                f"no CUDEM tiles, no NCEI regional fine DEM, no ETOPO global fallback "
                f"AND no 3DEP land DEM for bbox={bbox} -- no elevation data available "
                "for this AOI"
            )
        sources_in_precedence = (
            etopo_vsicurl
            + ([land_local] if have_land else [])  # type: ignore[list-item]
            + adjusted_cudem
            + regional_vsicurl
        )
        array, transform, crs = _composite_sources_to_array(
            sources_in_precedence, target_crs, bbox, min_pixel_m=min_pixel_m
        )
    finally:
        for p in tmp_paths:
            try:
                os.unlink(p)
            except OSError:
                pass
        # Clean up both the masked land tif (now land_local) AND, when the deep-water
        # rung masked it, the raw 3DEP staged predecessor it was derived from.
        for p in (land_local, land_raw_tmp):
            if p and p.startswith(tempfile.gettempdir()):
                try:
                    os.unlink(p)
                except OSError:
                    pass

    cudem_count = len(cudem_vsicurl)
    regional_count = len(regional_vsicurl)
    bathy_present = bool(cudem_vsicurl or regional_vsicurl or etopo_vsicurl)

    # 5) Honest, LABELED fallback warnings (data-source + loud-fallback norms).
    fallback_warning = _compose_fallback_warnings(
        bbox=bbox, cudem_status=cudem_status, cudem_count=cudem_count,
        regional_count=regional_count, has_etopo=bool(etopo_vsicurl),
        bathy_present=bathy_present, land_absent=land_absent,
    )
    if fallback_warning:
        logger.warning("fetch_topobathy: %s", fallback_warning)

    provenance = {
        "bathymetry_present": bathy_present,
        "fallback_warning": fallback_warning,
        "cudem_tile_count": cudem_count,
        "regional_tile_count": regional_count,
        "land_absent": land_absent,
    }
    return array, transform, crs, provenance


# ---------------------------------------------------------------------------
# HOOK: delegate_validate -- US-coastal + finiteness gate (pre-cache/pre-network).
# ---------------------------------------------------------------------------


@register_hook("topobathy.validate")
def validate_topobathy(spec: Any, params: dict[str, Any]) -> None:
    """Pre-cache input gate: US coastal envelope + finiteness (twin-identical).

    The router's generic bbox validation already stamps TOPOBATHY_INPUT_INVALID for
    shape / range / degenerate bboxes; this adds the topobathy-specific checks the
    declarative surface cannot express (the US-coastal envelope, the offset / timeout
    / min_pixel finiteness), raising ``TopobathyInputError`` pre-network."""
    bbox = tuple(float(v) for v in params["bbox"])
    _validate_bbox(bbox)  # US coastal envelope + degenerate (raises TopobathyInputError)

    offset = params.get("navd88_offset_m")
    if offset is not None and not math.isfinite(float(offset)):
        raise TopobathyInputError(f"navd88_offset_m must be finite; got {offset!r}")
    t_s = params.get("timeout_s")
    if t_s is not None and (not math.isfinite(float(t_s)) or float(t_s) <= 0):
        raise TopobathyInputError(f"timeout_s must be > 0 and finite; got {t_s!r}")
    mpx = params.get("min_pixel_m")
    if mpx is not None and (not math.isfinite(float(mpx)) or float(mpx) <= 0):
        raise TopobathyInputError(f"min_pixel_m must be > 0 and finite; got {mpx!r}")


# ---------------------------------------------------------------------------
# HOOK: delegate -- the 4-leg merge + provenance record; returns (array, tf, crs).
# ---------------------------------------------------------------------------


@register_hook("topobathy.read")
def read_topobathy(
    spec: Any, params: dict[str, Any], *, timeout_s: float
) -> tuple[Any, Any, Any]:
    """Fetch + merge the coastal topo-bathymetry composite; RECORD the fetch-time
    provenance and return ``(array, transform, crs)`` for the shared COG
    writer. The provenance dict (which legs painted the merge + the labeled
    degrades) reaches ``topobathy.envelope`` via the channel."""
    bbox = tuple(float(v) for v in params["bbox"])
    resolution_m = int(params.get("resolution_m", 10))
    target_crs = (str(params.get("target_crs") or TARGET_CRS)).strip()
    navd88_offset_m = params.get("navd88_offset_m")
    if navd88_offset_m is not None:
        navd88_offset_m = float(navd88_offset_m)
    # The tile-index download budget is the source param (default 120), NOT the
    # delegate wrapper's nominal outer timeout.
    fetch_timeout = float(params.get("timeout_s") or 120.0)
    force_bathy_base = bool(params.get("force_bathy_base", False))
    include_regional_fine = bool(params.get("include_regional_fine", False))
    skip_cudem = bool(params.get("skip_cudem", False))
    skip_land = bool(params.get("skip_land", False))
    mpx = params.get("min_pixel_m")
    min_pixel_m = float(mpx) if mpx is not None else None

    array, transform, crs, provenance = _select_and_merge(
        bbox, resolution_m, target_crs, navd88_offset_m, fetch_timeout,
        force_bathy_base, include_regional_fine, min_pixel_m, skip_cudem, skip_land,
    )
    record_provenance(provenance)
    return array, transform, crs


# ---------------------------------------------------------------------------
# HOOK: envelope -- twin layer_id/name + the 4 provenance fields (channel replay).
# ---------------------------------------------------------------------------


@register_hook("topobathy.envelope")
def envelope_topobathy(
    spec: Any,
    params: dict[str, Any],
    layer: Any,
    data: bytes | None,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the TopobathyResult fields: the twin's exact layer_id / name plus the
    four FETCH-TIME provenance fields read back from the channel.

    ``provenance`` is None for a cache object written before the channel existed (no
    sidecar); the declared defaults (bathymetry_present=True, no warning, counts 0)
    then hold -- byte-identical to the twin's own cache-hit behaviour, which reverted
    to those defaults because ``fetch_fn`` did not run."""
    b = tuple(float(v) for v in params["bbox"])
    layer_id = f"topobathy-{b[0]:.4f}-{b[1]:.4f}-{b[2]:.4f}-{b[3]:.4f}"
    name = (
        "Coastal topo-bathymetry DEM (NOAA CUDEM 1/9\" + USGS 3DEP, NAVD88 m) -- bbox "
        f"({b[0]:.2f},{b[1]:.2f},{b[2]:.2f},{b[3]:.2f})"
    )
    prov = provenance or {}
    return {
        "layer_id": layer_id,
        "name": name,
        "bathymetry_present": bool(prov.get("bathymetry_present", True)),
        "fallback_warning": prov.get("fallback_warning"),
        "cudem_tile_count": int(prov.get("cudem_tile_count", 0)),
        "regional_tile_count": int(prov.get("regional_tile_count", 0)),
    }
