"""River/waterway geometry fetcher (``fetch_river_geometry``): OSM Overpass waterways primary, NHDPlus HR fallback -> clipped FlatGeobuf.
"""

from __future__ import annotations

import io
import json
import logging
import math
import os
import re
import tempfile
import time
from collections.abc import Callable
from typing import Any

import requests

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.cache import read_through
from trid3nt_server.tools.fetchers._fetch_common import (
    FetchError,
    UpstreamAPIError,
    BboxInvalidError,
    _DEFAULT_USER_AGENT,
    _validate_bbox,
    round_bbox_to_resolution,
    _bbox_area_km2,
)

__all__ = [
    "fetch_river_geometry",
]

logger = logging.getLogger("trid3nt_server.tools.fetchers.hydrology.fetch_river_geometry")


# ---------------------------------------------------------------------------
# fetch_river_geometry — NHDPlus HR (USGS) (sprint-07 Stage B, job-0039).
# ---------------------------------------------------------------------------
#
# Access pattern tier — LIVE-VERIFIED matches kickoff inference (2026-06-07):
#
#   * USGS publishes NHDPlus HR as **HUC4-scoped FileGDB zip files** under
#     ``prd-tnm.s3.amazonaws.com/StagedProducts/Hydrography/NHDPlusHR/Beta/
#     GDB/NHDPLUS_H_<HUC4>_HU4_GDB.zip``. Live probe (HUC4 ``0309`` for the
#     Fort Myers / Caloosahatchee region): HTTP 200, accept-ranges=bytes,
#     content-length=151,111,923 (~144 MB).
#   * No per-bbox query API exists for NHDPlus HR raw geometry — the only
#     bbox-aware path is to download the HUC4 GDB and clip locally. The
#     USGS National Map TNM Access REST API (`tnmaccess.nationalmap.gov`)
#     returns the same download URL with file-size metadata.
#   * The ``.zip`` URLs return HTTP 403, so we route through ``.GDB.zip``
#     (the actual product file, not the wrapper zip).
#
# This is the **Tier 4 (region download + local clip)** pattern in §F.1.1.
# Two-stage cache:
#   - Stage 1: the HUC4 region GDB lives at
#     ``cache/static-30d/river_geometry/_regions/NHDPLUS_H_<HUC4>_HU4_GDB.zip``
#     (downloaded once per HUC4, shared across all clips inside that region).
#   - Stage 2: the per-call clip at
#     ``cache/static-30d/river_geometry/<hash>.fgb`` (the clipped FlatGeobuf
#     under the bbox-quantized key).
#
# v0.1 substrate scope: the per-call clip extracts the NHDFlowline feature
# class from the HUC4 GDB, clips by bbox, and writes a FlatGeobuf. The
# implementation does NOT use the two-stage cache in v0.1 — the kickoff calls
# for a single ``read_through`` write per call, and the GDB download is
# inside the fetcher (so the HUC4 region is fetched fresh on every cache
# miss). The two-stage optimization is captured as
# OQ-39-NHDPLUSHR-TWO-STAGE-CACHE for a follow-up job.
#
# HUC4 routing: a bbox in EPSG:4326 must be mapped to a HUC4 region code.
# Per the kickoff's per-source bbox quantization rule: "NHDPlus HR: HUC4-
# scoped (region-download Tier 4); cache key includes HUC4 region per §F.1.1
# Tier-4 discipline." The v0.1 substrate uses a small **bbox → HUC4
# heuristic envelope table** (mirrors the ``_state_fips_for_lonlat``
# heuristic from job-0033 — Fort Myers / Caloosahatchee = HUC4 ``0309``);
# replacement with a real point-in-polygon over the WBD HUC4 dataset is a
# tracked follow-up. Surface as OQ-39-NHDPLUSHR-HUC4-ROUTING-HEURISTIC.


_FETCH_RIVER_GEOMETRY_METADATA = AtomicToolMetadata(
    name="fetch_river_geometry",
    ttl_class="static-30d",
    source_class="river_geometry",
    cacheable=True,
)

# NHDPlus HR staged-products S3 base. HUC4 GDB at
# ``StagedProducts/Hydrography/NHDPlusHR/Beta/GDB/NHDPLUS_H_<HUC4>_HU4_GDB.zip``.
_NHDPLUSHR_BASE = (
    "https://prd-tnm.s3.amazonaws.com/StagedProducts/Hydrography/NHDPlusHR/Beta/GDB"
)

# Heuristic bbox → HUC4 region code. Each entry is (HUC4 code, envelope bbox).
# CONUS-centric for v0.1; HUC4 0309 covers the Fort Myers / Caloosahatchee
# region (the M5 demo target). Replacement with a real point-in-polygon over
# the WBD HUC4 dataset is a tracked follow-up — see
# OQ-39-NHDPLUSHR-HUC4-ROUTING-HEURISTIC.
_HUC4_BBOX_ENVELOPES: list[tuple[str, tuple[float, float, float, float]]] = [
    # Florida — South Florida (Caloosahatchee, Big Cypress, Everglades)
    ("0309", (-82.0, 25.0, -80.0, 27.5)),
    # Florida — Peninsular (Tampa Bay south to about Lake Okeechobee)
    ("0310", (-82.9, 26.7, -80.5, 28.7)),
    # Florida — Suwannee / North Florida
    ("0311", (-83.7, 28.5, -82.0, 31.0)),
    # Texas — Lower Colorado (Houston / Galveston Bay)
    ("1209", (-96.0, 28.0, -93.5, 31.5)),
    # Louisiana — Lower Mississippi
    ("0807", (-91.5, 28.5, -89.0, 31.0)),
    # New York — Hudson (Hurricane Sandy reference region)
    ("0203", (-75.0, 40.5, -73.0, 43.0)),
    # North Carolina — Cape Fear (Hurricane Florence reference region)
    ("0303", (-79.5, 33.0, -77.0, 35.8)),
    # California — South Coast (Los Angeles basin)
    ("1807", (-119.0, 33.0, -117.0, 35.0)),
]

def _huc4_for_bbox(bbox: tuple[float, float, float, float]) -> str | None:
    """Best-effort HUC4 lookup from a bbox center — heuristic only.

    Returns ``None`` if no envelope matches. A future enrichment job replaces
    this with a real point-in-polygon over the WBD HUC4 dataset cached in the
    cache bucket. Same shape/role as the job-0033 ``_state_fips_for_lonlat``
    heuristic and the job-0037 ``_iso3_for_lonlat`` heuristic.
    """
    mid_lon = 0.5 * (bbox[0] + bbox[2])
    mid_lat = 0.5 * (bbox[1] + bbox[3])
    for huc4, (mn_lon, mn_lat, mx_lon, mx_lat) in _HUC4_BBOX_ENVELOPES:
        if mn_lon <= mid_lon <= mx_lon and mn_lat <= mid_lat <= mx_lat:
            return huc4
    return None

# ---------------------------------------------------------------------------
# OSM Overpass waterway path — PRIMARY source for fetch_river_geometry.
# ---------------------------------------------------------------------------
#
# Root-cause fix: the NHDPlus HR HUC4 routing heuristic only covers a handful
# of CONUS demo envelopes, so most bboxes hit "could not route bbox to a HUC4
# region" and the tool dead-ends (data-source-fallback norm violation). OSM
# Overpass exposes a true per-bbox waterway query that fills the WHOLE bbox
# (not just a seed-connected sub-network), is global, and serializes to the
# same FlatGeobuf -> inline-GeoJSON render path the Wave 4.9 vector pipeline
# already drives (``add_loaded_layer`` reads the .fgb, converts to GeoJSON).
#
# Overpass QL shape (mirrors fetch_roads_osm, but for waterways):
#
#     [out:json][timeout:60];
#     (way["waterway"~"^(river|stream|canal)$"](s,w,n,e););
#     out geom;
#
# Overpass returns the bbox corners as (south, west, north, east) — the
# OPPOSITE corner-pair ordering from the caller's (min_lon, min_lat, max_lon,
# max_lat). Same convention as the roads tool.

#: Overpass interpreter endpoint (same public mirror fetch_roads_osm uses).
_OVERPASS_URL = "https://overpass-api.de/api/interpreter"

#: HTTP timeout for the Overpass POST (Overpass is slow under load).
_OVERPASS_HTTP_TIMEOUT = 120.0

#: Overpass-side internal-query timeout (the ``[timeout:N]`` directive).
_OVERPASS_QL_TIMEOUT = 60

#: OSM ``waterway`` tag values treated as "rivers and streams" for this tool.
#: ``river`` + ``stream`` + ``canal`` is the channel-carrying network most
#: comparable to NHDFlowline; ``ditch``/``drain`` are excluded by default
#: (they explode feature counts in agricultural/urban areas with little
#: hydrologic-modeling value).
_WATERWAY_CLASSES: tuple[str, ...] = ("river", "stream", "canal")

#: The full set of OSM ``waterway`` tag values this tool will let a caller
#: request via ``waterway_type``. ``river``/``stream``/``canal`` are the
#: default channel network; ``ditch``/``drain`` are the small artificial
#: drainage channels that dominate drained-agriculture and tiled-field
#: landscapes (Imperial Valley, the Fens) — opt-in because they explode
#: feature counts elsewhere. Anything outside this set is rejected so an
#: LLM-invented value cannot inject arbitrary text into the Overpass regex.
_WATERWAY_TYPE_ALIASES: dict[str, tuple[str, ...]] = {
    # Convenience labels that map to a class set.
    "default": ("river", "stream", "canal"),
    "rivers": ("river", "stream", "canal"),
    "channels": ("river", "stream", "canal"),
    "drainage": ("ditch", "drain"),
    "ditches": ("ditch", "drain"),
    "all": ("river", "stream", "canal", "ditch", "drain"),
}

#: Individual OSM ``waterway`` values a caller may name directly (singular or
#: comma/plus-joined). Kept separate from the aliases so both forms validate
#: against the same closed vocabulary.
_WATERWAY_ALLOWED_VALUES: tuple[str, ...] = (
    "river",
    "stream",
    "canal",
    "ditch",
    "drain",
)

def _resolve_waterway_classes(
    waterway_type: str | tuple[str, ...] | list[str] | None,
) -> tuple[str, ...]:
    """Resolve a caller ``waterway_type`` to a validated tuple of OSM classes.

    Accepts:
      * ``None`` -> the default ``_WATERWAY_CLASSES`` (backward compatible).
      * A convenience alias string in ``_WATERWAY_TYPE_ALIASES``
        (e.g. ``"all"``, ``"drainage"``).
      * A single OSM value (e.g. ``"ditch"``).
      * A comma- or plus-separated string of OSM values
        (e.g. ``"ditch,drain"`` or ``"river+ditch"``).
      * A list/tuple of OSM values (e.g. ``["ditch", "drain"]``).

    De-duplicates while preserving order and validates every resolved token
    against ``_WATERWAY_ALLOWED_VALUES`` so an LLM-invented value cannot inject
    arbitrary text into the Overpass ``~"^(...)$"`` regex. Raises
    ``BboxInvalidError`` (the tool's input-validation error type) on any
    unknown token. Returns the default tuple when the input resolves to empty.
    """
    if waterway_type is None:
        return _WATERWAY_CLASSES

    # Normalize the input into a flat list of lowercase tokens.
    raw_tokens: list[str] = []
    if isinstance(waterway_type, str):
        text = waterway_type.strip().lower()
        if not text:
            return _WATERWAY_CLASSES
        if text in _WATERWAY_TYPE_ALIASES:
            return _WATERWAY_TYPE_ALIASES[text]
        # Split on commas / plus / whitespace so "ditch,drain" and "ditch drain"
        # both work.
        for chunk in re.split(r"[,+\s]+", text):
            if chunk:
                raw_tokens.append(chunk)
    elif isinstance(waterway_type, (list, tuple)):
        for item in waterway_type:
            if not isinstance(item, str):
                raise BboxInvalidError(
                    f"waterway_type list entries must be strings; got "
                    f"{type(item).__name__}"
                )
            tok = item.strip().lower()
            if tok:
                raw_tokens.append(tok)
    else:
        raise BboxInvalidError(
            f"waterway_type must be a str or list of str; got "
            f"{type(waterway_type).__name__}"
        )

    resolved: list[str] = []
    for tok in raw_tokens:
        if tok not in _WATERWAY_ALLOWED_VALUES:
            raise BboxInvalidError(
                f"unsupported waterway_type token {tok!r}; allowed OSM waterway "
                f"values: {', '.join(_WATERWAY_ALLOWED_VALUES)} (or an alias: "
                f"{', '.join(sorted(_WATERWAY_TYPE_ALIASES))})."
            )
        if tok not in resolved:
            resolved.append(tok)

    if not resolved:
        return _WATERWAY_CLASSES
    return tuple(resolved)

def _build_overpass_waterway_ql(
    bbox: tuple[float, float, float, float],
    waterway_classes: tuple[str, ...],
) -> str:
    """Construct the Overpass QL payload for waterway ways inside ``bbox``.

    Overpass expects the bbox corners as ``(south, west, north, east)``
    (lat first) — the OPPOSITE ordering from the caller's
    ``(min_lon, min_lat, max_lon, max_lat)``.
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    s, w, n, e = min_lat, min_lon, max_lat, max_lon
    classes_pipe = "|".join(waterway_classes)
    return (
        f"[out:json][timeout:{_OVERPASS_QL_TIMEOUT}];"
        f"(way[\"waterway\"~\"^({classes_pipe})$\"]({s},{w},{n},{e}););"
        f"out geom;"
    )

def _post_overpass_waterways(ql: str) -> dict[str, Any]:
    """POST ``ql`` to the Overpass interpreter; return the parsed JSON dict.

    Raises ``UpstreamAPIError`` on network / HTTP / parse failure so the
    caller can fall through to the NHDPlus HR fallback (data-source-fallback
    norm) rather than dead-ending.
    """
    try:
        resp = requests.post(
            _OVERPASS_URL,
            data={"data": ql},
            headers={"User-Agent": _DEFAULT_USER_AGENT},
            timeout=_OVERPASS_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise UpstreamAPIError(
            f"Overpass waterway query failed (transport/HTTP): {exc}"
        ) from exc
    try:
        return resp.json()
    except ValueError as exc:
        raise UpstreamAPIError(
            f"Overpass returned non-JSON response for waterway query: {exc}"
        ) from exc

def _extract_overpass_waterway_records(
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    """Project Overpass ``way`` elements to LineString records.

    Each record carries ``coords`` (list of ``(lon, lat)`` tuples) plus the
    ``osm_id``, ``name``, and ``waterway`` attributes. Ways with fewer than
    two valid coordinates are dropped (a LineString needs >= 2 points).
    """
    elements = payload.get("elements") or []
    if not isinstance(elements, list):
        raise UpstreamAPIError(
            f"Overpass 'elements' is not a list: {type(elements).__name__}"
        )
    records: list[dict[str, Any]] = []
    for el in elements:
        if not isinstance(el, dict) or el.get("type") != "way":
            continue
        geom = el.get("geometry") or []
        if not isinstance(geom, list) or len(geom) < 2:
            continue
        coords: list[tuple[float, float]] = []
        for pt in geom:
            if not isinstance(pt, dict):
                continue
            lat_v = pt.get("lat")
            lon_v = pt.get("lon")
            if lat_v is None or lon_v is None:
                continue
            try:
                lat = float(lat_v)
                lon = float(lon_v)
            except (TypeError, ValueError):
                continue
            if not (math.isfinite(lat) and math.isfinite(lon)):
                continue
            coords.append((lon, lat))
        if len(coords) < 2:
            continue
        tags = el.get("tags") or {}
        if not isinstance(tags, dict):
            tags = {}
        records.append(
            {
                "osm_id": el.get("id"),
                "name": tags.get("name"),
                "waterway": tags.get("waterway"),
                "coords": coords,
            }
        )
    return records

def _waterway_records_to_clipped_fgb_bytes(
    records: list[dict[str, Any]],
    bbox: tuple[float, float, float, float],
) -> bytes:
    """Serialize waterway LineString records to bbox-clipped FlatGeobuf bytes.

    Builds a GeoDataFrame of LineStrings (EPSG:4326), clips it to the exact
    requested bbox so the layer fills the whole bbox without spilling outside
    it, and writes FlatGeobuf bytes (the same `.fgb` -> inline-GeoJSON render
    path Wave 4.9 drives via ``add_loaded_layer``). An empty record list still
    produces a valid (empty) FlatGeobuf — never a sentinel (cache.py poison
    contract).
    """
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
        from shapely.geometry import LineString, box as shapely_box  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise UpstreamAPIError(
            f"geopandas / shapely unavailable for OSM waterway serialization: {exc}"
        ) from exc

    if records:
        geometries = [LineString(r["coords"]) for r in records]
        attrs = [
            {
                "osm_id": r.get("osm_id"),
                "name": r.get("name"),
                "waterway": r.get("waterway"),
            }
            for r in records
        ]
        gdf = gpd.GeoDataFrame(attrs, geometry=geometries, crs="EPSG:4326")
        # Clip to the exact bbox so geometry doesn't spill outside the AOI.
        try:
            gdf = gdf.clip(shapely_box(*bbox))
        except Exception as exc:  # noqa: BLE001 — clip is best-effort precision
            logger.warning(
                "OSM waterway clip failed; returning unclipped features: %s", exc
            )
    else:
        import pandas as pd  # type: ignore[import-not-found]

        empty_df = pd.DataFrame(
            {
                "osm_id": pd.Series(dtype="Int64"),
                "name": pd.Series(dtype="object"),
                "waterway": pd.Series(dtype="object"),
            }
        )
        gdf = gpd.GeoDataFrame(
            empty_df,
            geometry=gpd.GeoSeries([], crs="EPSG:4326"),
            crs="EPSG:4326",
        )

    out_tmp: str | None = None
    try:
        import tempfile

        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="trid3nt_osm_rivers_"
        ) as f:
            out_tmp = f.name
        try:
            gdf.to_file(out_tmp, driver="FlatGeobuf")
        except Exception as exc:  # noqa: BLE001
            raise UpstreamAPIError(
                f"FlatGeobuf write failed for OSM waterways (bbox={bbox}): {exc}"
            ) from exc
        with open(out_tmp, "rb") as f:
            return f.read()
    finally:
        if out_tmp is not None:
            try:
                os.unlink(out_tmp)
            except OSError:
                pass

def _fetch_osm_waterway_geometry_bytes(
    bbox: tuple[float, float, float, float],
    waterway_classes: tuple[str, ...] = _WATERWAY_CLASSES,
) -> bytes:
    """PRIMARY river-geometry fetcher — OSM Overpass waterway query over the bbox.

    Queries Overpass for ``waterway`` ways (``waterway_classes``, default
    river/stream/canal) inside the bbox, projects each to a LineString, clips
    to the bbox, and returns FlatGeobuf bytes. Fills the WHOLE bbox (true
    per-bbox query — not a seed-connected sub-network like NLDI). Raises
    ``UpstreamAPIError`` on any failure so ``fetch_river_geometry`` can fall
    through to NHDPlus HR.

    ``waterway_classes`` lets the caller widen/narrow the OSM ``waterway`` tag
    set (e.g. add ``ditch``/``drain`` over drained agriculture). The default
    preserves the original river/stream/canal behavior exactly.
    """
    _validate_bbox(bbox)
    classes = tuple(waterway_classes) if waterway_classes else _WATERWAY_CLASSES
    ql = _build_overpass_waterway_ql(bbox, classes)
    payload = _post_overpass_waterways(ql)
    records = _extract_overpass_waterway_records(payload)
    logger.info(
        "fetch_river_geometry[osm]: extracted %d waterway(s) for bbox=%s classes=%s",
        len(records),
        bbox,
        classes,
    )
    return _waterway_records_to_clipped_fgb_bytes(records, bbox)

def _fetch_river_geometry_bytes(
    bbox: tuple[float, float, float, float],
    huc4: str | None,
    waterway_classes: tuple[str, ...] = _WATERWAY_CLASSES,
) -> bytes:
    """Internal fallback chain for river geometry (data-source-fallback norm).

    Order:
      1. PRIMARY — OSM Overpass waterway query over the bbox (global, true
         per-bbox, fills the whole AOI). Empty-but-valid results are accepted
         (no rivers in the bbox is a legitimate answer, not a failure).
      2. FALLBACK — NHDPlus HR HUC4 region download + local clip, but only
         when the bbox routed to a HUC4 region (``huc4`` is not None).
      3. Typed honest error (``UpstreamAPIError``) if every path fails — never
         a silent dead-end or a hallucinated success.

    ``waterway_classes`` controls the OSM ``waterway`` tag set on the PRIMARY
    path (default river/stream/canal). The NHDPlus HR FALLBACK is the NHDPlus
    NHDFlowline channel network and is unaffected by ``waterway_classes``
    (NHDPlus does not carry an OSM ``waterway`` tag), so a non-default
    ``waterway_classes`` only changes the OSM result.

    Returns FlatGeobuf bytes. The caller (``fetch_river_geometry``) routes
    these through ``read_through`` so the 30-day cache absorbs repeat calls.
    """
    primary_exc: Exception | None = None
    try:
        return _fetch_osm_waterway_geometry_bytes(bbox, waterway_classes)
    except Exception as exc:  # noqa: BLE001 — fall through to NHDPlus HR
        primary_exc = exc
        logger.warning(
            "fetch_river_geometry: OSM Overpass primary failed (%s: %s); "
            "falling back to NHDPlus HR (huc4=%s)",
            type(exc).__name__,
            exc,
            huc4,
        )

    if huc4 is not None:
        try:
            return _fetch_nhdplushr_geometry_bytes(bbox, huc4)
        except Exception as exc:  # noqa: BLE001 — both paths failed
            logger.warning(
                "fetch_river_geometry: NHDPlus HR fallback also failed "
                "(huc4=%s): %s: %s",
                huc4,
                type(exc).__name__,
                exc,
            )
            raise UpstreamAPIError(
                "fetch_river_geometry: both OSM Overpass (primary) and NHDPlus HR "
                f"(fallback, huc4={huc4}) failed. OSM error: {primary_exc}. "
                f"NHDPlus HR error: {exc}."
            ) from exc

    # OSM failed and there is no HUC4 fallback available.
    raise UpstreamAPIError(
        "fetch_river_geometry: OSM Overpass (primary) failed and no NHDPlus HR "
        f"HUC4 fallback is available for this bbox. OSM error: {primary_exc}."
    )

def _fetch_nhdplushr_geometry_bytes(
    bbox: tuple[float, float, float, float], huc4: str
) -> bytes:
    """Download the NHDPlus HR HUC4 GDB, extract NHDFlowline, clip by bbox, return FlatGeobuf.

    Tier 4 access pattern: download the HUC4 region GDB (~144 MB for HUC4
    0309 South Florida), extract the ``NHDFlowline`` feature class from the
    GeoDatabase via OpenFileGDB driver (GDAL native), clip features whose
    geometry intersects the bbox, and rewrite as FlatGeobuf. Raises
    ``UpstreamAPIError`` on any download / extraction failure.

    Implementation note: the substrate downloads the full HUC4 GDB on every
    cache miss; the two-stage region-cache optimization is OQ-39-NHDPLUSHR-
    TWO-STAGE-CACHE. For the Fort Myers demo path the per-bbox cache miss is
    a one-time ~144 MB transfer, cached for 30 days.
    """
    _validate_bbox(bbox)
    url = f"{_NHDPLUSHR_BASE}/NHDPLUS_H_{huc4}_HU4_GDB.zip"

    # rasterio + geopandas/pyogrio import lazily.
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
        from shapely.geometry import box as shapely_box  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise UpstreamAPIError(
            f"geopandas / shapely unavailable for NHDPlus HR clip: {exc}"
        ) from exc

    import tempfile
    import zipfile

    zip_tmp: str | None = None
    gdb_dir: str | None = None
    out_tmp: str | None = None
    try:
        # Download the HUC4 GDB zip.
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": _DEFAULT_USER_AGENT},
                timeout=300.0,
                stream=True,
                allow_redirects=True,
            )
            if resp.status_code == 404:
                raise UpstreamAPIError(
                    f"NHDPlus HR HUC4 GDB not found at {url} (huc4={huc4}); "
                    "the staged-products tree may have moved — verify the base path."
                )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise UpstreamAPIError(
                f"NHDPlus HR GDB download failed url={url}: {exc}"
            ) from exc

        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as zf:
            zip_tmp = zf.name
            for chunk in resp.iter_content(chunk_size=1 << 20):
                if chunk:
                    zf.write(chunk)

        # Extract the GDB directory.
        gdb_dir = tempfile.mkdtemp(prefix="nhdplushr-")
        try:
            with zipfile.ZipFile(zip_tmp) as zfh:
                zfh.extractall(gdb_dir)
        except zipfile.BadZipFile as exc:
            raise UpstreamAPIError(
                f"NHDPlus HR HUC4 GDB zip is corrupt or empty for huc4={huc4}: {exc}"
            ) from exc

        # Find the .gdb directory inside the extracted tree.
        import os as _os

        gdb_path: str | None = None
        for root, dirs, _files in _os.walk(gdb_dir):
            for d in dirs:
                if d.endswith(".gdb"):
                    gdb_path = _os.path.join(root, d)
                    break
            if gdb_path:
                break
        if gdb_path is None:
            raise UpstreamAPIError(
                f"could not find .gdb directory in extracted NHDPlus HR archive "
                f"for huc4={huc4} (extracted under {gdb_dir})"
            )

        # Read NHDFlowline, clip by bbox, write FlatGeobuf.
        try:
            gdf = gpd.read_file(gdb_path, layer="NHDFlowline", bbox=bbox)
        except Exception as exc:  # noqa: BLE001
            raise UpstreamAPIError(
                f"geopandas could not read NHDFlowline from {gdb_path}: {exc}"
            ) from exc

        # Clip by bbox polygon for tight precision (geopandas bbox read is
        # a spatial filter, not a clip — features extending outside the bbox
        # are returned whole; clip trims them).
        try:
            bbox_geom = shapely_box(*bbox)
            gdf_clipped = gdf.clip(bbox_geom)
        except Exception as exc:  # noqa: BLE001
            # Fall back to the unclipped result if clip fails (some geometry
            # types don't clip cleanly); surface a warning in the log.
            logger.warning("NHDPlus HR clip failed; returning bbox-filtered features: %s", exc)
            gdf_clipped = gdf

        with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as ot:
            out_tmp = ot.name
        try:
            gdf_clipped.to_file(out_tmp, driver="FlatGeobuf")
        except Exception as exc:  # noqa: BLE001
            raise UpstreamAPIError(
                f"FlatGeobuf write failed for NHDPlus HR clip (huc4={huc4}, bbox={bbox}): {exc}"
            ) from exc

        with open(out_tmp, "rb") as f:
            return f.read()
    finally:
        # Best-effort cleanup of all tmp paths.
        for path in (zip_tmp, out_tmp):
            if path is None:
                continue
            try:
                os.unlink(path)
            except OSError:
                pass
        if gdb_dir is not None:
            try:
                import shutil

                shutil.rmtree(gdb_dir, ignore_errors=True)
            except Exception:  # noqa: BLE001
                pass

@register_tool(
    _FETCH_RIVER_GEOMETRY_METADATA,
    # Annotations: readOnlyHint=True, openWorldHint=True (USGS NHDPlus HR),
    # destructiveHint=False, idempotentHint=True (cache shim deduplicates).
    open_world_hint=True,
)
def fetch_river_geometry(
    bbox: tuple[float, float, float, float],
    source: str = "nhdplus_hr",
    waterway_type: str | list[str] | None = None,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Fetch river and stream flowline geometry for a bbox (OSM + NHDPlus HR).

    **What it does:** Returns river/stream/canal LineStrings that fill the
    requested bbox, as a FlatGeobuf that renders inline on the map (Wave 4.9
    vector path). Access pattern: Tier 2/Tier 4 with an internal fallback
    chain (data-source-fallback norm):

    1. PRIMARY — OSM Overpass ``waterway`` query over the bbox
       (river/stream/canal). Global, true per-bbox: fills the WHOLE bbox, not
       just a seed-connected sub-network. Clipped to the bbox.
    2. FALLBACK — USGS NHDPlus High Resolution NHDFlowline (Tier 4 region
       download + local clip), used when the bbox routes to one of the v0.1
       HUC4 envelopes and OSM is unavailable.
    3. Typed honest error if both fail — never a silent dead-end.

    Both paths serialize to FlatGeobuf and clip to the requested bbox. The
    30-day cache absorbs repeat calls.

    **When to use:**
    - ``build_sfincs_model`` needs river flowlines for DEM hydro-conditioning
      (HydroMT's ``setup_rivers_from_dem`` step burns channel geometry).
    - Fluvial flood workflow requires channel network for boundary-condition
      placement (upstream inflow nodes, downstream outlets).
    - User asks to visualize stream networks or watershed drainage patterns.
    - Watershed delineation: ``delineate_watershed`` tool consumes the
      flowline outlet point to route upstream.

    **When NOT to use:**
    - Real-time streamflow measurements — use ``fetch_streamflow`` (NWIS
      USGS gauges) for discharge time series.
    - Flow-direction / accumulation grids — derive from the DEM inside
      HydroMT; NHDPlus HR publishes those separately.
    - Areas larger than 5,000 km² — the tool enforces a guardrail to keep a
      single fetch tractable (use a smaller bbox or a future tiled workflow).

    **Parameters:**
    - ``bbox`` (tuple[float,float,float,float]): ``(min_lon, min_lat, max_lon,
      max_lat)`` in EPSG:4326. Max area 5,000 km².
    - ``source`` (str, default ``"nhdplus_hr"``): preferred hydrography
      source label. ``"nhdplus_hr"`` and ``"osm"`` are accepted; the internal
      fallback chain (OSM primary, NHDPlus HR fallback) runs regardless so the
      tool stays reliable across all bboxes. Unsupported labels (e.g.
      ``"merit_hydro"``) raise ``BboxInvalidError``.
    - ``waterway_type`` (str | list[str] | None, default ``None``): widens or
      narrows the OSM ``waterway`` tag set on the PRIMARY (OSM Overpass) path.
      ``None`` keeps the default channel network (``river``/``stream``/
      ``canal``). Pass individual OSM values (``"river"``, ``"stream"``,
      ``"canal"``, ``"ditch"``, ``"drain"``) singly, comma/plus-joined
      (``"ditch,drain"``), or as a list (``["ditch", "drain"]``); or a
      convenience alias: ``"all"`` (every class incl. ditch+drain),
      ``"drainage"`` / ``"ditches"`` (ditch+drain only — the artificial
      drainage channels that dominate drained-agriculture / tiled-field
      landscapes), or ``"default"`` / ``"rivers"`` / ``"channels"``
      (river+stream+canal). ``ditch``/``drain`` are opt-in because they
      explode feature counts in agricultural/urban areas. Unknown tokens raise
      ``BboxInvalidError``. Distinct ``waterway_type`` values get distinct
      cache keys. The NHDPlus HR fallback is unaffected (no OSM waterway tag).

    **Returns:**
    A ``LayerURI`` pointing at a FlatGeobuf of river/stream LineStrings in the
    cache bucket (``s3://trid3nt-cache/cache/static-30d/river_geometry/<key>.fgb``).
    ``layer_type="vector"``, ``role="input"``. The FlatGeobuf renders inline
    on the map via the Wave 4.9 GeoJSON path (``add_loaded_layer``) — it is
    NOT published through ``publish_layer`` (that path is raster-only).

    **Cross-tool dependencies:**
    - Upstream: ``geocode_location`` for bbox derivation.
    - Downstream: ``build_sfincs_model`` (river-burning DEM step),
      ``delineate_watershed``, stream-network display in map panel.
    """
    if isinstance(source, str) and source.strip().lower() in ("nhdplus", "nhd"):
        # F25-class alias: the model's natural label for the NHDPlus family.
        # The fallback chain is OSM-primary regardless, so aliasing is safe.
        source = "nhdplus_hr"
    if source not in ("nhdplus_hr", "osm"):
        # Reserved future sources (NHDPlus V2, MERIT-Hydro) — not in v0.1.
        raise BboxInvalidError(
            f"unsupported source={source!r}; allowed: 'nhdplus_hr' (Tier-4 HUC4 GDB) "
            "or 'osm' (Overpass waterway). The internal fallback chain runs "
            "OSM-primary regardless of which label you pass."
        )

    # Resolve + validate the OSM waterway class set BEFORE any bbox work so an
    # unknown waterway_type token fails fast with a typed error. None -> the
    # default river/stream/canal tuple (fully backward compatible).
    waterway_classes = _resolve_waterway_classes(waterway_type)

    _validate_bbox(bbox)
    quantized = round_bbox_to_resolution(bbox, 10)

    # Guardrail: keep a single fetch tractable (OSM Overpass + NHDPlus HR HUC4
    # GDBs are both heavy for huge bboxes). 5,000 km^2 explicit bound — matches
    # the previous NHDPlus-only behavior.
    if _bbox_area_km2(quantized) > 5_000.0:
        raise BboxInvalidError(
            f"bbox area {_bbox_area_km2(quantized):.1f} km^2 exceeds 5000 km^2 "
            "guardrail for fetch_river_geometry (use a smaller bbox or a future "
            "tiled workflow)."
        )

    # HUC4 routing is now BEST-EFFORT (fallback only) — a missing HUC4 no
    # longer dead-ends the tool, because OSM Overpass is the primary path
    # (root-cause fix for "could not route bbox to a HUC4 region").
    huc4 = _huc4_for_bbox(quantized)

    # Cache key is keyed on the quantized bbox (+ HUC4 when available, for
    # backward-compatible dedup discipline). The fallback chain decides the
    # actual provider; identical bboxes dedup to the same artifact.
    params = {
        "bbox": list(quantized),
        "source": "river_geometry",  # provider-agnostic; chain decides at fetch time
        "huc4": huc4,
    }
    # Only fold waterway_type into the cache key when it deviates from the
    # default so existing default-source artifacts keep their current keys
    # (backward-compatible dedup). A non-default class set is a DISTINCT query
    # (different OSM features) and must NOT alias the default artifact.
    if waterway_classes != _WATERWAY_CLASSES:
        params["waterway_classes"] = list(waterway_classes)
    result = read_through(
        metadata=_FETCH_RIVER_GEOMETRY_METADATA,
        params=params,
        ext="fgb",
        fetch_fn=lambda: _fetch_river_geometry_bytes(
            quantized, huc4, waterway_classes
        ),
    )
    assert result.uri is not None
    return LayerURI(
        layer_id=f"rivers-{quantized[0]:.4f}-{quantized[1]:.4f}",
        name="Rivers & Streams",
        layer_type="vector",
        uri=result.uri,
        style_preset="osm_waterways",  # water-vector preset (mirrors osm_roads for fetch_roads_osm)
        role="input",
    )
