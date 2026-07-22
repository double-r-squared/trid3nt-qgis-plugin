"""``catalog_fetch``: fetch a catalog entry by id through the tiered
STAC -> OGC -> HTTPS -> region ladder into a cached LayerURI.

Carved out of the original two-tool ``catalog`` module in the tools/ reorg;
behavior and the registered tool surface are unchanged. The YAML loader +
catalog cache live in ``trid3nt_server.tools.discovery.catalog_common``.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import yaml

from trid3nt_contracts.catalog import CatalogEntry
from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.cache import read_through
from trid3nt_server.tools.discovery.ogc_adapter import OGCAdapterError, fetch_ogc_layer
from trid3nt_server.tools.discovery.catalog_common import (
    CATALOG_YAML_PATH,
    CatalogNotFoundError,
    load_catalog,
)

__all__ = ["catalog_fetch"]

logger = logging.getLogger("trid3nt_server.tools.discovery.catalog_fetch")


# ---------------------------------------------------------------------------
# catalog_fetch — generic Tier-aware dispatcher.
# ---------------------------------------------------------------------------


_CATALOG_FETCH_METADATA = AtomicToolMetadata(
    name="catalog_fetch",
    ttl_class="static-30d",
    source_class="catalog_fetch",
    cacheable=True,
)

def _get_catalog_entry(entry_id: str) -> CatalogEntry:
    """Fetch a single CatalogEntry by id. Raises CatalogNotFoundError on miss.

    v0.1: looks up the YAML cache. Forward path (D.11 catalog_entries
    collection): becomes a MongoDB read; YAML stays as fallback when the
    Mongo cluster is unreachable.
    """
    catalog = load_catalog()
    for entry in catalog:
        if entry.id == entry_id:
            return entry
    # Surfacable hint for the LLM/agent: enumerate the v0.1 entry IDs so it
    # can adjust on the next call.
    ids = sorted(e.id for e in catalog)
    raise CatalogNotFoundError(
        f"catalog entry id={entry_id!r} not found in v0.1 catalog "
        f"({len(catalog)} entries; first 5: {ids[:5]})"
    )

def _layer_uri_from_entry(
    entry: CatalogEntry, uri: str, ext: str
) -> LayerURI:
    """Build a LayerURI for a fetched cached artifact.

    Style preset routing per the v0.1 seven engine-owned presets:
    ``categorical_landcover`` for landcover; ``continuous_dem`` for DEM /
    elevation; ``affected_buildings`` for buildings; everything else falls
    back to ``continuous_dem`` as a placeholder (the catalog-driven preset
    routing is OQ-47-CATALOG-STYLE-PRESET-ROUTING).
    """
    preset = "continuous_dem"
    sc = entry.source_class.lower()
    if "landcover" in sc:
        preset = "categorical_landcover"
    elif "building" in sc:
        preset = "affected_buildings"
    elif "flood" in sc:
        preset = "flood_depth"
    elif "track" in sc:
        preset = "hurricane_track"
    layer_type = "vector" if ext in ("fgb", "geojson", "json") else "raster"
    return LayerURI(
        layer_id=f"catalog-{entry.id}",
        name=entry.name,
        layer_type=layer_type,
        uri=uri,
        style_preset=preset,
        role="input",
    )

def _ext_for_content_type(content_type: str, service_type: str) -> str:
    """Pick a cache-write extension for an OGC adapter response."""
    ct = content_type.lower()
    if "tiff" in ct or "geotiff" in ct:
        return "tif"
    if "geojson" in ct or "json" in ct:
        return "json"
    if "png" in ct:
        return "png"
    if "jpeg" in ct or "jpg" in ct:
        return "jpg"
    if service_type == "ARCGIS_REST":
        return "json"
    if service_type == "WFS":
        return "json"
    return "bin"

def _tier1_stac_fetch(entry: CatalogEntry, params: dict[str, Any]) -> tuple[bytes, str]:
    """Tier-1 (STAC + COG) dispatch: thin substrate.

    The seed catalog's Tier-1 entries (Copernicus DEM, ESA WorldCover, MODIS
    LC, etc.) all expose Microsoft Planetary Computer STAC collections.
    Implementing a full STAC search + COG windowed read here would duplicate
    `fetch_dem` / `fetch_landcover`'s logic; v0.1 surfaces the access pattern
    inferred from the entry's URLs and routes through the OGC adapter as a
    raw HTTPS GET. Captured as OQ-47-CATALOG-TIER1-STAC for a follow-up.
    """
    raise NotImplementedError(
        f"Tier-1 STAC dispatch via catalog_fetch is reserved for a follow-up "
        f"(entry_id={entry.id!r}); use the dedicated `fetch_dem` / "
        f"`fetch_landcover` tools for STAC-backed sources in v0.1."
    )

def _tier2_ogc_fetch(entry: CatalogEntry, params: dict[str, Any]) -> tuple[bytes, str]:
    """Tier-2 (OGC service) dispatch: route through the generic OGC adapter.

    Inspects the entry's URLs + how_to_use to infer the service flavor
    (WCS / WMS / WFS / ArcGIS REST). v0.1 heuristic: URL fragments name the
    flavor (``/wms``, ``/wcs``, ``/wfs``, ``/MapServer/<n>/query``,
    ``/FeatureServer/<n>``, etc.). When ambiguous, default to WMS (the most
    common visualization surface).
    """
    bbox_in = params.get("bbox") or params.get("location")
    if bbox_in is not None and not isinstance(bbox_in, (list, tuple)):
        raise OGCAdapterError(
            f"catalog_fetch params.bbox must be a list/tuple; got {type(bbox_in).__name__}"
        )
    bbox: tuple[float, float, float, float] | None = (
        tuple(bbox_in) if bbox_in is not None else None  # type: ignore[assignment]
    )

    # Caller can override the service type explicitly; otherwise sniff URL.
    service_type_param = params.get("service_type")
    layer_name_param = params.get("layer_name")
    crs_param = params.get("crs", "EPSG:4326")
    version_param = params.get("version")
    image_format_param = params.get("format")
    # Phase-2 resolution lever: width/height default to None so fetch_ogc_layer
    # computes an extent-aware raster grid from the bbox. When the caller does
    # not pin a target_resolution_m, fall back to the entry's curated
    # native_resolution_m (e.g. 10 m for 3DEP, 30 m for NLCD/LANDFIRE) so the
    # auto-grid targets the source's native ground sampling instead of a fixed
    # 1024 px that coarsened large AOIs.
    _wp = params.get("width_px")
    _hp = params.get("height_px")
    width_px = int(_wp) if _wp is not None else None
    height_px = int(_hp) if _hp is not None else None
    target_resolution_m = params.get("target_resolution_m")
    if target_resolution_m is not None:
        target_resolution_m = float(target_resolution_m)
    elif entry.native_resolution_m is not None:
        target_resolution_m = float(entry.native_resolution_m)
    where_clause = params.get("where", "1=1")

    url = entry.urls[0]
    sniff = url.lower()

    if service_type_param:
        service_type = service_type_param.upper()
    elif "/wcs" in sniff:
        service_type = "WCS"
    elif "/wfs" in sniff:
        service_type = "WFS"
    elif "/mapserver" in sniff or "/featureserver" in sniff or "/imageserver" in sniff:
        service_type = "ARCGIS_REST"
    elif "/wms" in sniff or "wmsserver" in sniff:
        service_type = "WMS"
    else:
        # ArcGIS REST endpoints often have no /wms in the path — use REST.
        service_type = "ARCGIS_REST" if "arcgis" in sniff else "WMS"

    # ArcGIS REST entry URLs typically point to /MapServer; the /query path
    # has to be on a specific layer. v0.1 default: layer 0 unless params.layer
    # overrides. The kickoff's FEMA NFHL flood zones live on layer 28 — the
    # caller passes layer_name="28" (or an integer-coercible string).
    #
    # ArcGIS ImageServer endpoints (raster) do NOT support /<layer>/query;
    # they expose ``/exportImage`` instead. Detect ImageServer endpoints by
    # URL substring and route through the ImageServer path with a thin
    # extra_params override that maps bbox + size to ImageServer's parameter
    # names (``bbox`` + ``size``).
    fetch_url = url
    layer_name = layer_name_param or ""
    if service_type == "ARCGIS_REST":
        if "/imageserver" in sniff:
            # ImageServer exportImage — produces a PNG / TIFF clip of the
            # raster mosaic. Different param shape than MapServer/query.
            base = url.rstrip("/")
            fetch_url = f"{base}/exportImage"
            layer_name = "exportImage"
        else:
            layer_id = params.get("layer_id", layer_name_param or "0")
            base = url.rstrip("/")
            fetch_url = f"{base}/{layer_id}/query"
            layer_name = str(layer_id)

    # Defaults per service flavor.
    if service_type == "WMS":
        image_format = image_format_param or "image/png"
        version = version_param or "1.1.1"
    elif service_type == "WCS":
        image_format = image_format_param or "GeoTIFF"
        version = version_param or "1.0.0"
    elif service_type == "WFS":
        image_format = image_format_param or "application/json"
        version = version_param or "2.0.0"
    else:  # ARCGIS_REST
        # ImageServer exportImage needs an explicit raster format
        # (default JPEG isn't useful for DEM bytes); ImageServer routing
        # is set above. For MapServer/FeatureServer /query default to
        # GeoJSON output.
        if "/imageserver" in sniff:
            image_format = image_format_param or "tiff"
        else:
            image_format = "geojson"
        version = "rest"

    resp = fetch_ogc_layer(
        url=fetch_url,
        layer_name=layer_name,
        bbox=bbox,
        crs=crs_param,
        service_type=service_type,  # type: ignore[arg-type]
        image_format=image_format,
        version=version,
        width_px=width_px,
        height_px=height_px,
        target_resolution_m=target_resolution_m,
        where_clause=where_clause,
    )
    ext = _ext_for_content_type(resp.content_type, service_type)
    return resp.content, ext

def _tier3_https_fetch(entry: CatalogEntry, params: dict[str, Any]) -> tuple[bytes, str]:
    """Tier-3 (direct HTTPS + Range / point-query) dispatch.

    v0.1 substrate: issues a single HTTPS GET for the entry's primary URL and
    returns the body. Range-aware windowed reads for COG-shaped responses
    live in the dedicated fetchers (`fetch_dem` / `fetch_landcover`) for v0.1;
    captured as OQ-47-CATALOG-TIER3-RANGE for the follow-up.
    """
    import requests as _rq

    extra_qs = params.get("query") or {}
    try:
        resp = _rq.get(
            entry.urls[0],
            params={str(k): str(v) for k, v in extra_qs.items()},
            headers={"User-Agent": "trid3nt/0.1 catalog_fetch Tier-3"},
            timeout=120.0,
        )
        resp.raise_for_status()
    except _rq.RequestException as exc:
        raise OGCAdapterError(
            f"Tier-3 HTTPS GET failed for entry={entry.id!r} url={entry.urls[0]}: {exc}"
        ) from exc
    content = resp.content
    ct = resp.headers.get("content-type", "").lower()
    if "tiff" in ct:
        ext = "tif"
    elif "json" in ct:
        ext = "json"
    elif "csv" in ct or "text" in ct:
        ext = "csv"
    else:
        ext = "bin"
    return content, ext

def _tier4_region_fetch(
    entry: CatalogEntry, params: dict[str, Any]
) -> tuple[bytes, str]:
    """Tier-4 (region download + local clip) dispatch.

    v0.1 substrate raises NotImplementedError — the per-source region
    download + clip path is intricate (NHDPlus HR uses HUC4 routing; WorldPop
    uses ISO3 country files; HydroSHEDS uses continental files). The existing
    fetchers (``fetch_river_geometry`` for NHDPlus HR; ``fetch_population``
    for WorldPop) already implement Tier-4 per source. Catalog-driven Tier-4
    dispatch is OQ-47-CATALOG-TIER4-REGION for the follow-up.
    """
    raise NotImplementedError(
        f"Tier-4 region-download dispatch via catalog_fetch is reserved for a "
        f"follow-up (entry_id={entry.id!r}); use the dedicated `fetch_river_geometry` / "
        f"`fetch_population` tools for Tier-4 sources in v0.1."
    )

@register_tool(
    _CATALOG_FETCH_METADATA,
    # Annotations: readOnlyHint=True (dispatches to external API but does not
    # mutate server state; writes to read-through cache only),
    # openWorldHint=True (Tier-2 OGC services, Tier-3 HTTPS external endpoints),
    # destructiveHint=False, idempotentHint=True (cache shim deduplicates).
    open_world_hint=True,
)
def catalog_fetch(entry_id: str, params: dict[str, Any] | None = None, **_extra_ignored: Any) -> dict[str, Any]:
    """Fetch bytes for a vetted catalog entry by its stable id (§F.1.2 Mode 1).

    Use this (not catalog_search, which only LISTS candidates) when you already have a stable catalog entry id and want its actual layer BYTES.

    Use this when: the LLM has chosen a `CatalogEntry` from `catalog_search`
    and needs the actual layer bytes — generic dispatcher routes by the
    entry's ``access_tier``: Tier 1 (STAC+COG), Tier 2 (OGC service), Tier 3
    (HTTPS+Range), Tier 4 (region+clip). The dispatched bytes are written
    through the FR-DC-3 cache and surfaced as a LayerURI.

    Do NOT use this for: discovering candidate sources (use ``catalog_search``);
    direct-bbox raster retrieval where a dedicated fetcher already exists
    (use ``fetch_dem`` / ``fetch_landcover`` etc.); user-supplied URLs not in
    the catalog (use ``web_fetch`` once it lands).

    Params:
        entry_id: stable catalog id (e.g. ``"fema-nfhl-flood-zones"``,
            ``"usgs-3dep-elevation-image-service"``).
        params: dispatch-specific request shape. Common keys:
            - ``bbox`` (Tier 2/3/4 raster + ArcGIS REST): EPSG:4326 bbox.
            - ``layer_name`` (Tier 2 WMS/WCS/WFS): override the layer/coverage
              name when the entry URL doesn't name it directly.
            - ``layer_id`` (Tier 2 ArcGIS REST): the integer layer index on
              a MapServer (e.g. ``28`` for FEMA NFHL flood hazard zones).
            - ``service_type`` (Tier 2): override URL sniffing
              (``"WCS"`` / ``"WMS"`` / ``"WFS"`` / ``"ARCGIS_REST"``).
            - ``width_px`` / ``height_px`` (Tier 2 raster): explicit pixel
              dimensions. Optional — when omitted, the dispatch derives an
              extent-aware raster grid from ``bbox`` (resolution lever below).
            - ``target_resolution_m`` (Tier 2 raster): ground cell size in
              metres for the auto-computed grid (the fetch-side resolution
              lever). Omit to target the entry's curated
              ``native_resolution_m`` (e.g. 10 m for 3DEP, 30 m for
              NLCD/LANDFIRE), falling back to a bounded 30 m default; pass a
              finer value (e.g. ``10``) on a large AOI to opt into more pixels.
              Each axis is clamped to 4096 px so payloads stay bounded.
            - ``crs`` (Tier 2): default ``"EPSG:4326"``.
            - ``where`` (Tier 2 ArcGIS REST): ESRI WHERE clause.
            - ``query`` (Tier 3): extra HTTPS query params.

    Returns:
        A dict with:
        - ``layer``: a ``LayerURI`` pointing at the cached artifact
          (``s3://trid3nt-cache/cache/static-30d/catalog_fetch/<key>.<ext>``).
        - ``entry_id``: the catalog id (echo).
        - ``access_tier``: the dispatched tier (1/2/3/4).
        - ``source_class``: the entry's source_class (for downstream routing).
        - ``citation``: the entry's citation string (NFR-L-3 provenance).
        - ``last_verified``: the entry's curator-vetted UTC timestamp.

    FR-DC-3 / FR-CE-8: registered with ``ttl_class="static-30d"``,
    ``source_class="catalog_fetch"``, ``cacheable=True``. The cache key is the
    entry id + params; identical fetches dedup.
    """
    if not isinstance(entry_id, str) or not entry_id.strip():
        raise CatalogNotFoundError("catalog_fetch requires a non-empty entry_id")
    params = params or {}

    entry = _get_catalog_entry(entry_id)
    tier = entry.access_tier

    # Cache key params — entry_id + (normalized) params dict.
    cache_params = {"entry_id": entry.id, "tier": tier, "request": params}

    def _do_fetch() -> bytes:
        nonlocal_ext_holder: list[str] = []
        if tier == 1:
            data, ext = _tier1_stac_fetch(entry, params)
        elif tier == 2:
            data, ext = _tier2_ogc_fetch(entry, params)
        elif tier == 3:
            data, ext = _tier3_https_fetch(entry, params)
        elif tier == 4:
            data, ext = _tier4_region_fetch(entry, params)
        else:  # pragma: no cover — Literal exhaustive
            raise CatalogNotFoundError(
                f"unknown access_tier={tier!r} for entry={entry.id!r}"
            )
        nonlocal_ext_holder.append(ext)
        # We can't easily return both data + ext from read_through (signature
        # demands bytes), so we tag the bytes header with a small JSON
        # metadata prefix. Cleaner approach: write the ext into the cache
        # path itself, but read_through expects ext at call time. We
        # therefore choose the ext via a side-channel attribute on _do_fetch.
        _do_fetch._ext = ext  # type: ignore[attr-defined]
        return data

    # Pre-flight: dispatch once OUTSIDE read_through purely to derive the ext
    # (so the cache path's extension matches what we fetched). To keep that
    # cheap and avoid double-fetching on a cache-miss, we use a lightweight
    # mapping by tier + entry knowledge.
    ext_hint = _ext_hint_for(entry, params)

    result = read_through(
        metadata=_CATALOG_FETCH_METADATA,
        params=cache_params,
        ext=ext_hint,
        fetch_fn=_do_fetch,
    )
    assert result.uri is not None

    layer = _layer_uri_from_entry(entry, result.uri, ext_hint)
    payload: dict[str, Any] = {
        "layer": layer,
        "entry_id": entry.id,
        "access_tier": tier,
        "source_class": entry.source_class,
        "citation": entry.citation,
        "last_verified": entry.last_verified.isoformat(),
        "cache_hit": result.hit,
        "bytes": len(result.data),
    }
    logger.info(
        "catalog_fetch entry_id=%r tier=%d cache_hit=%s bytes=%d",
        entry_id,
        tier,
        result.hit,
        len(result.data),
    )
    return payload

def _ext_hint_for(entry: CatalogEntry, params: dict[str, Any]) -> str:
    """Predict the cache file extension for an entry+params dispatch.

    Reads the entry's URL / how_to_use + the caller's params (e.g. WCS
    GetCoverage → ``tif``; ArcGIS REST query → ``json``; WMS GetMap →
    ``png``; HydroMT-conditioned DEM ZIP → ``zip``). Wrong guesses are
    purely cosmetic (the cache key is content-addressed; the extension is
    metadata for human inspection of the bucket).
    """
    if entry.access_tier == 2:
        # Sniff: explicit service_type wins.
        st = (params.get("service_type") or "").upper()
        sniff = entry.urls[0].lower()
        if st == "WCS" or "/wcs" in sniff:
            return "tif"
        if st == "WFS" or "/wfs" in sniff:
            return "json"
        if st == "ARCGIS_REST" or "arcgis" in sniff or "/mapserver" in sniff or "/imageserver" in sniff:
            # ArcGIS REST → JSON (geojson) responses for query endpoints; tif
            # for ImageServer exportImage. ImageServer endpoints default to
            # exportImage in the catalog Tier-2 dispatch (raster surfaces
            # don't have MapServer-style /query).
            if "imageserver" in sniff:
                return "tif"
            return "json"
        if st == "WMS" or "/wms" in sniff:
            fmt = (params.get("format") or "image/png").lower()
            if "tiff" in fmt or "geotiff" in fmt:
                return "tif"
            if "jpeg" in fmt or "jpg" in fmt:
                return "jpg"
            return "png"
    if entry.access_tier == 3:
        sc = entry.source_class.lower()
        if "track" in sc or "csv" in entry.how_to_use.lower():
            return "csv"
        return "bin"
    if entry.access_tier == 4:
        return "zip"
    if entry.access_tier == 1:
        return "tif"
    return "bin"
