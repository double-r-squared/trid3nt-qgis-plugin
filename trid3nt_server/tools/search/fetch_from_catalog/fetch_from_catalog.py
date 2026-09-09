"""``fetch_from_catalog`` - one catalog entry, by its stable id, through the
tiered STAC -> OGC -> HTTPS -> region ladder into a cached LayerURI. Dispatch is
by the entry's declared ``access_tier``, never by guessing."""

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
from trid3nt_server.tools.search.ogc_adapter import OGCAdapterError, fetch_ogc_layer
from trid3nt_server.tools.search.catalog_common import (
    CATALOG_YAML_PATH,
    CatalogNotFoundError,
    load_catalog,
)

__all__ = ["fetch_from_catalog"]

logger = logging.getLogger("trid3nt_server.tools.search.fetch_from_catalog.fetch_from_catalog")

#: Catalog-surfacing arms that route via a spec-served ``source`` name instead of an
#: entry id. Read at IMPORT so the registered tool exposes a ``source`` param only
#: under those arms; the default config keeps the entry_id-only signature exactly.
_SOURCE_PARAM_ARM = os.environ.get("TRID3NT_CATALOG_ARM", "").strip() in ("1", "3")


# ---------------------------------------------------------------------------
# fetch_from_catalog -- generic Tier-aware dispatcher.
# ---------------------------------------------------------------------------


_FETCH_FROM_CATALOG_METADATA = AtomicToolMetadata(
    name="fetch_from_catalog",
    ttl_class="static-30d",
    source_class="fetch_from_catalog",
    cacheable=True,
)

def _get_catalog_entry(entry_id: str) -> CatalogEntry:
    """One CatalogEntry by id, from the loaded YAML catalog. Raises
    ``CatalogNotFoundError`` on a miss, enumerating the known ids."""
    catalog = load_catalog()
    for entry in catalog:
        if entry.id == entry_id:
            return entry
    # Enumerate the known ids so the caller can correct itself on the next call.
    ids = sorted(e.id for e in catalog)
    raise CatalogNotFoundError(
        f"catalog entry id={entry_id!r} not found in v0.1 catalog "
        f"({len(catalog)} entries; first 5: {ids[:5]})"
    )

def _layer_uri_from_entry(
    entry: CatalogEntry, uri: str, ext: str
) -> LayerURI:
    """A LayerURI for a fetched cached artifact. A catalog entry declares no style,
    so the layer takes its kind's BARE default - guessing a physical band from a
    source-class substring would paint an unknown quantity as an assumed one."""
    layer_type = "vector" if ext in ("fgb", "geojson", "json") else "raster"
    return LayerURI(
        layer_id=f"catalog-{entry.id}",
        name=entry.name,
        layer_type=layer_type,
        uri=uri,
        style={"kind": "reference" if layer_type == "vector" else "continuous"},
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
    """Tier-1 (STAC + COG) dispatch. NOT implemented: a STAC search plus a COG
    windowed read is what the dedicated raster fetchers already do, so this refuses
    rather than duplicating them."""
    raise NotImplementedError(
        f"Tier-1 STAC dispatch via fetch_from_catalog is reserved for a follow-up "
        f"(entry_id={entry.id!r}); use the dedicated `fetch_dem` / "
        f"`fetch_landcover` tools for STAC-backed sources in v0.1."
    )

def _tier2_ogc_fetch(entry: CatalogEntry, params: dict[str, Any]) -> tuple[bytes, str]:
    """Tier-2 (OGC service) dispatch through the shared adapter. The flavor is
    inferred from URL fragments and defaults to WMS when ambiguous; an explicit
    ``params["service_type"]`` always wins over the sniff."""
    bbox_in = params.get("bbox") or params.get("location")
    if bbox_in is not None and not isinstance(bbox_in, (list, tuple)):
        raise OGCAdapterError(
            f"fetch_from_catalog params.bbox must be a list/tuple; got {type(bbox_in).__name__}"
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
    # width/height default to None so the adapter computes an extent-aware grid
    # from the bbox. With no target_resolution_m pinned, the entry's curated
    # native_resolution_m is the fallback, so the auto-grid targets the SOURCE's
    # native ground sampling rather than a fixed pixel count that would coarsen a
    # large AOI.
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
        # ArcGIS REST endpoints often have no /wms in the path -- use REST.
        service_type = "ARCGIS_REST" if "arcgis" in sniff else "WMS"

    # An ArcGIS REST entry URL points at /MapServer, but the /query path has to be
    # on a SPECIFIC layer, so layer 0 is the default unless the caller names one -
    # a flood-hazard layer, say, is rarely layer 0.
    #
    # An ImageServer endpoint does NOT support /<layer>/query at all; it exposes
    # /exportImage, whose params are named differently (bbox + size). The URL
    # substring is what tells the two apart.
    fetch_url = url
    layer_name = layer_name_param or ""
    if service_type == "ARCGIS_REST":
        if "/imageserver" in sniff:
            # ImageServer exportImage -- produces a PNG / TIFF clip of the
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
    """Tier-3 dispatch: ONE HTTPS GET for the entry's primary URL, body returned
    whole. No range-aware windowed read here - a COG-shaped response is the
    dedicated fetchers' business."""
    import requests as _rq

    extra_qs = params.get("query") or {}
    try:
        resp = _rq.get(
            entry.urls[0],
            params={str(k): str(v) for k, v in extra_qs.items()},
            headers={"User-Agent": "trid3nt/0.1 fetch_from_catalog Tier-3"},
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
    """Tier-4 (region download + local clip) dispatch. NOT implemented: the region
    routing is per-source - HUC4, ISO3 country files, continental tiles - so it
    refuses rather than guessing which one an entry means."""
    raise NotImplementedError(
        f"Tier-4 region-download dispatch via fetch_from_catalog is reserved for a "
        f"follow-up (entry_id={entry.id!r}); use the dedicated `fetch_river_geometry` / "
        f"`fetch_population` tools for Tier-4 sources in v0.1."
    )

def _fetch_from_catalog_entry(entry_id: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Fetch bytes for a vetted catalog entry by its stable id - the actual layer.

    ROUTING: an entry id is already in hand - typically chosen from
    `search_data_catalog`, which only LISTS candidates - and the actual bytes are
    wanted. NOT for discovering sources, NOT where a dedicated fetcher already
    covers the dataset, NOT for a URL that is not in the catalog (`web_fetch`).

    `params` carries the dispatch shape: `bbox` (EPSG:4326), `layer_name` when the
    entry URL does not name the layer, `layer_id` for a MapServer's integer layer
    index, `service_type` to override URL sniffing, `crs`, `where` for an ESRI WHERE
    clause, `query` for extra HTTPS params. For a raster, `width_px`/`height_px` set
    the grid; omit both and `target_resolution_m` picks a ground cell size instead,
    defaulting to the entry's native resolution, clamped to 4096 px per axis.

    Returns {layer (a LayerURI on the cached artifact), entry_id, access_tier,
    source_class, citation, last_verified}. Identical fetches dedup at the cache.
    """
    if not isinstance(entry_id, str) or not entry_id.strip():
        raise CatalogNotFoundError("fetch_from_catalog requires a non-empty entry_id")
    params = params or {}

    entry = _get_catalog_entry(entry_id)
    tier = entry.access_tier

    # Cache key params -- entry_id + (normalized) params dict.
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
        else:  # pragma: no cover -- Literal exhaustive
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
        metadata=_FETCH_FROM_CATALOG_METADATA,
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
        "fetch_from_catalog entry_id=%r tier=%d cache_hit=%s bytes=%d",
        entry_id,
        tier,
        result.hit,
        len(result.data),
    )
    return payload


def _fetch_from_catalog_via_spec(
    source: str, params: dict[str, Any] | None
) -> dict[str, Any]:
    """Resolve a spec-served source name to its ``SourceSpec`` and route it. There
    is no provider inputSchema on this path, so the router's own ``validate_params``
    is the SOLE gate on the arguments."""
    from trid3nt_server.tools.fetchers._router import registration as _reg
    from trid3nt_server.tools.fetchers._router import router as _router

    spec = _reg._SPEC_REGISTRY.get(source)
    if spec is None:
        known = sorted(_reg._SPEC_REGISTRY)
        raise CatalogNotFoundError(
            f"catalog source {source!r} is not a spec-served source "
            f"({len(known)} known; first 5: {known[:5]})"
        )
    layer = _router.route(spec, params or {})
    return {
        "layer": layer,
        "source": source,
        "source_class": spec.source_class,
    }


if _SOURCE_PARAM_ARM:

    def fetch_from_catalog(
        entry_id: str | None = None,
        params: dict[str, Any] | None = None,
        source: str | None = None,
        **_extra_ignored: Any,
    ) -> dict[str, Any]:
        if isinstance(source, str) and source.strip():
            return _fetch_from_catalog_via_spec(source.strip(), params)
        return _fetch_from_catalog_entry(entry_id, params)  # type: ignore[arg-type]

    fetch_from_catalog.__doc__ = (
        (_fetch_from_catalog_entry.__doc__ or "")
        + "\n\nCATALOG-SURFACING (Design 1/3): for a spec-served data source, pass "
        "source=<source name> (e.g. 'fetch_gridmet') plus params={...} (the card's "
        "typed param schema) instead of entry_id. The router validates params and "
        "dispatches; a typed error feeds the retry loop."
    )
else:

    def fetch_from_catalog(
        entry_id: str, params: dict[str, Any] | None = None, **_extra_ignored: Any
    ) -> dict[str, Any]:
        return _fetch_from_catalog_entry(entry_id, params)

    # Reuse the SAME docstring object so the DEFAULT provider FunctionDeclaration +
    # the retrieval-index document text are byte-identical to before the refactor.
    fetch_from_catalog.__doc__ = _fetch_from_catalog_entry.__doc__

# Annotations: readOnlyHint=True (dispatches to external API but does not mutate
# server state; writes to read-through cache only), openWorldHint=True (Tier-2 OGC
# services, Tier-3 HTTPS external endpoints), destructiveHint=False,
# idempotentHint=True (cache shim deduplicates).
fetch_from_catalog = register_tool(
    _FETCH_FROM_CATALOG_METADATA, open_world_hint=True
)(fetch_from_catalog)


def _ext_hint_for(entry: CatalogEntry, params: dict[str, Any]) -> str:
    """Predict the cache file extension for an entry+params dispatch. A wrong guess
    is purely cosmetic: the cache key is content-addressed, and the extension is
    only there for a human reading the bucket."""
    if entry.access_tier == 2:
        # Sniff: explicit service_type wins.
        st = (params.get("service_type") or "").upper()
        sniff = entry.urls[0].lower()
        if st == "WCS" or "/wcs" in sniff:
            return "tif"
        if st == "WFS" or "/wfs" in sniff:
            return "json"
        if st == "ARCGIS_REST" or "arcgis" in sniff or "/mapserver" in sniff or "/imageserver" in sniff:
            # A query endpoint answers JSON; an ImageServer exportImage answers
            # a tif. The dispatch defaults an ImageServer to exportImage, because a
            # raster surface has no MapServer-style /query.
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
