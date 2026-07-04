"""Catalog atomic tools — `catalog_search` + `catalog_fetch` (job-0047, sprint-08 Stage B).

§F.1.2 Mode 1 (catalog-mediated) substrate. Two atomic tools the LLM calls
to discover and retrieve any vetted public data source from the curated
``public_data_source_catalog.yaml``:

- `catalog_search(topic, location?, source_filter?) → list[CatalogEntry]`
  ranks the seed catalog by topic-match against `description` + `how_to_use`
  + `name` + `source_class`, optionally filters by bbox-overlap (via Tier-2
  envelope heuristics; full bbox-overlap is OQ-47-CATALOG-COVERAGE-INDEX) and
  by `source_class` filter. Returns the matching catalog entries — the
  "labeling" §F.1.2 calls out (the entries carry `how_to_use` strings that
  drive the LLM's next call). FR-DC-2 ``ttl_class="semi-static-7d"`` (the
  catalog file changes weekly when curators update it).

- `catalog_fetch(entry_id, params) → LayerURI | dict` is the generic
  dispatcher. It reads `entry.access_tier` from the catalog and routes to
  the appropriate fetch path:
  - **Tier 1** (STAC + COG): STAC item query → byte-window read → cache write.
  - **Tier 2** (OGC service): dispatch through `ogc_adapter.fetch_ogc_layer`
    — single source of truth for Tier 2 across the engine.
  - **Tier 3** (HTTPS + Range): direct HTTPS GET (or `/vsicurl/` windowed
    read for COG-shaped responses).
  - **Tier 4** (region download + local clip): two-stage cache placeholder —
    full implementation deferred per OQ-37-COUNTRY-FILE-CACHING-STRATEGY
    (the existing fetchers like NHDPlus HR already do the per-source
    region-download pattern; the catalog dispatch is a thin wrapper here).

Backing store for v0.1: the YAML file loaded once at first call and cached
in-memory. Migration path to MongoDB ``catalog_entries`` collection (D.11)
documented in the kickoff — when curator-managed in Mongo, the in-memory
cache becomes a thin TTL-bound MongoDB read with YAML as static fallback.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import yaml

from grace2_contracts.catalog import CatalogEntry
from grace2_contracts.execution import LayerURI
from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from .cache import read_through
from .ogc_adapter import OGCAdapterError, fetch_ogc_layer

__all__ = [
    "catalog_search",
    "catalog_fetch",
    "load_catalog",
    "CatalogNotFoundError",
    "CATALOG_YAML_PATH",
]

logger = logging.getLogger("grace2_agent.tools.catalog")


class CatalogNotFoundError(RuntimeError):
    """The requested catalog entry id was not found in the v0.1 YAML catalog.

    Carries an ``error_code="CATALOG_ENTRY_NOT_FOUND"`` for the FR-AS-11 typed-
    error surface. Not retryable — a missing entry id is a configuration error
    rather than a transient failure.
    """

    error_code: str = "CATALOG_ENTRY_NOT_FOUND"
    retryable: bool = False


# Repo-root location of the catalog YAML. Override via env for tests / non-prod.
def _default_catalog_yaml_path() -> Path:
    """Resolve the default ``public_data_source_catalog.yaml`` path.

    The file lives at the repo root for v0.1 (curator-edited under git).
    Walk up from this module's directory to find the repo root; fall back to
    an explicit env override.
    """
    env_path = os.environ.get("GRACE2_CATALOG_YAML")
    if env_path:
        return Path(env_path).expanduser().resolve()
    # services/agent/src/grace2_agent/tools/catalog.py → repo root is 5 levels up.
    here = Path(__file__).resolve()
    for parent in [here, *here.parents]:
        candidate = parent / "public_data_source_catalog.yaml"
        if candidate.exists():
            return candidate
    return here.parents[5] / "public_data_source_catalog.yaml"


CATALOG_YAML_PATH = _default_catalog_yaml_path()


# In-memory catalog cache (lazy-loaded, refreshed at process restart). v0.1
# only — when D.11 ``catalog_entries`` is populated, this becomes a Mongo
# read at the FR-DC-2 ``semi-static-7d`` cadence.
_CATALOG_CACHE: list[CatalogEntry] | None = None


def _parse_last_verified(raw: Any) -> str:
    """Coerce a YAML ``last_verified`` field into a UTC datetime ISO-Z string.

    The seed catalog stores ``last_verified`` as a YAML date (parsed as
    ``datetime.date``). The CatalogEntry pydantic shape demands a
    ``UTCDatetime`` — we widen the date to midnight UTC.
    """
    from datetime import datetime, time, timezone

    if hasattr(raw, "isoformat"):
        # date or datetime — coerce to UTC midnight if just a date.
        if hasattr(raw, "hour"):
            dt = raw
        else:
            dt = datetime.combine(raw, time.min)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    if isinstance(raw, str):
        # Tolerate a bare date string like "2026-06-07".
        if "T" not in raw:
            return f"{raw}T00:00:00+00:00"
        return raw
    raise ValueError(f"unsupported last_verified shape: {type(raw).__name__}")


def load_catalog(yaml_path: Path | str | None = None) -> list[CatalogEntry]:
    """Load + parse + validate the YAML catalog into a list of CatalogEntry.

    Cached in-memory after the first call. Pass ``yaml_path=...`` to force a
    reload from a different file (test scaffolding).
    """
    global _CATALOG_CACHE
    if yaml_path is None and _CATALOG_CACHE is not None:
        return _CATALOG_CACHE

    path = Path(yaml_path) if yaml_path is not None else CATALOG_YAML_PATH
    if not path.exists():
        raise CatalogNotFoundError(
            f"catalog YAML not found at {path}; set GRACE2_CATALOG_YAML env var "
            "or place the file at the repo root."
        )

    with path.open() as fh:
        raw = yaml.safe_load(fh)

    entries: list[CatalogEntry] = []
    for row in raw.get("entries", []) or []:
        row = dict(row)  # don't mutate the loaded YAML
        row["last_verified"] = _parse_last_verified(row.get("last_verified"))
        try:
            entries.append(CatalogEntry.model_validate(row))
        except Exception as exc:  # noqa: BLE001 — surface the bad row
            logger.warning(
                "skipping catalog row id=%r — validation failed: %s",
                row.get("id"),
                exc,
            )
            continue

    if yaml_path is None:
        _CATALOG_CACHE = entries
    logger.info("loaded %d catalog entries from %s", len(entries), path)
    return entries


def _reset_catalog_cache_for_tests() -> None:
    """Tests force-reload the YAML by clearing the in-memory cache."""
    global _CATALOG_CACHE
    _CATALOG_CACHE = None


# ---------------------------------------------------------------------------
# catalog_search — topic-ranked retrieval over the YAML catalog.
# ---------------------------------------------------------------------------


_CATALOG_SEARCH_METADATA = AtomicToolMetadata(
    name="catalog_search",
    ttl_class="semi-static-7d",
    source_class="catalog_search",
    cacheable=True,
)


def _score_entry(entry: CatalogEntry, topic: str) -> float:
    """Compute a topic-relevance score for a catalog entry.

    Simple lowercase substring + token-overlap heuristic. Surfaced as
    OQ-47-CATALOG-SEARCH-RANKER for a follow-up that lands BM25 or an
    embedding-based search.
    """
    if not topic:
        return 1.0
    haystack = " ".join(
        [
            entry.id,
            entry.name,
            entry.description,
            entry.how_to_use,
            entry.source_class,
        ]
    ).lower()
    needle = topic.lower().strip()
    score = 0.0
    if needle in haystack:
        score += 5.0
    # Token-overlap bonus: every CONTENT-WORD token in topic also in haystack
    # adds 1. Skip generic filler ("data", "source", "name", "the", "for", "of"
    # …) so a bogus phrase like "fake data source name" doesn't rack up a
    # score from filler-only overlap with every catalog entry.
    stopwords = {
        "data",
        "source",
        "sources",
        "name",
        "names",
        "the",
        "of",
        "for",
        "and",
        "a",
        "an",
        "in",
        "to",
        "by",
        "with",
        "on",
        "or",
        "from",
        "any",
        "all",
    }
    tokens = [
        t
        for t in needle.replace("/", " ").replace("-", " ").split()
        if t and t not in stopwords
    ]
    if not tokens:
        return score  # all-filler topic produces zero — escalate to Mode 2.
    matched_tokens = sum(1 for tok in tokens if tok in haystack)
    if matched_tokens == 0:
        return score  # no content-word hit at all.
    score += float(matched_tokens)
    # Require at least 1/3 of the content tokens to hit before the entry
    # qualifies as a real match — guards against single-token false positives.
    if matched_tokens < max(1, len(tokens) // 3):
        score = max(0.0, score - 1.0)
    # Bias matches in the name (most authoritative) over description.
    name_low = entry.name.lower()
    if needle in name_low:
        score += 2.0
    return score


def _bbox_overlaps_world(
    bbox: tuple[float, float, float, float] | None,
    entry: CatalogEntry,
) -> bool:
    """Does the catalog entry plausibly cover ``bbox``?

    v0.1 heuristic: the YAML doesn't carry per-entry spatial extents (the
    Mode 2 enrichment job lands ``coverage_envelope`` per F.1.2). For now,
    apply a coarse rule: entries naming "global" or "world" or matching
    international ISO terms always include international bboxes; entries
    naming "US" / "CONUS" / "L48" / "national" cover the CONUS envelope; the
    rest are treated as plausibly relevant (recall over precision). Captured
    as OQ-47-CATALOG-COVERAGE-INDEX for the Mode 2 schema follow-up.
    """
    if bbox is None:
        return True
    text = (entry.description + " " + entry.name + " " + entry.how_to_use).lower()
    # CONUS / US-only entries — exclude any clearly non-US bbox center. We
    # treat both "CONUS" / "L48" tokens and the broader "us federal data" /
    # "(usgs)" curator language as US-only signals for the v0.1 heuristic.
    # "conterminous us" mentions usually accompany Hawaii/Alaska coverage but
    # still don't extend to international bboxes; treated as US-only here.
    conus_words = {
        "conus",
        "l48",
        "conterminous us",
        "contiguous us",
        "contiguous united states",
        "us federal",
        "usgs federal",
    }
    if any(w in text for w in conus_words):
        mn_lon, mn_lat, mx_lon, mx_lat = bbox
        # Broad US envelope (CONUS + Alaska + Hawaii + PR/USVI) approx
        # (-180 to -60) lon × (15 to 75) lat. Any bbox center in this band
        # qualifies.
        cx, cy = 0.5 * (mn_lon + mx_lon), 0.5 * (mn_lat + mx_lat)
        return (-180.0 <= cx <= -60.0) and (15.0 <= cy <= 75.0)
    return True


@register_tool(
    _CATALOG_SEARCH_METADATA,
    # Annotations: readOnlyHint=True (read-only; no state mutation),
    # openWorldHint=True (in-memory catalog lookup, but catalog_fetch ultimately
    # dispatches to Tier-2/3 external APIs; search step itself is intra-process),
    # destructiveHint=False, idempotentHint=True (cache shim deduplicates).
    open_world_hint=True,
)
def catalog_search(
    topic: str,
    location: tuple[float, float, float, float] | None = None,
    source_filter: str | None = None,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> list[dict[str, Any]]:
    """Search the curated public data-source catalog for vetted entries on a topic.

    Use this when: the agent has a free-text need ("flood zones", "DEM",
    "river flow data", "building footprints") and wants the catalog's
    curator-vetted endpoints + invocation hints (``how_to_use``) — the §F.1.2
    Mode 1 substrate. The returned entries carry stable IDs the LLM passes
    to ``catalog_fetch``.

    Do NOT use this for: live geocoding (use ``geocode_location``); pulling
    actual bytes (use ``catalog_fetch`` or one of the dedicated fetchers);
    enumerating GCS-cached layers (those are not catalog entries — the
    catalog describes external sources).

    Params:
        topic: free-text topic ("flood zones", "DEM", "land cover", etc.).
            Required, non-empty.
        location: optional ``(min_lon, min_lat, max_lon, max_lat)`` bbox in
            EPSG:4326. When provided, the ranker uses a coverage heuristic to
            drop entries that the bbox cannot plausibly hit (CONUS-only
            entries vs an international bbox). See OQ-47-CATALOG-COVERAGE-INDEX.
        source_filter: optional ``source_class`` filter ("dem", "landcover",
            "flood_zone", …). When set, only entries matching this
            source_class are returned.

    Returns:
        A list of dicts (one per matching CatalogEntry), each carrying the
        catalog entry as a JSON-serializable dict + a ``relevance_score``
        float for the ranking. The dict shape matches the §F.1.2 Mode 1
        binding contract (id, name, description, urls, access_tier,
        credential_tier, ttl_class, source_class, license, citation,
        vintage, last_verified, status, how_to_use, api_key_secret_ref).

        Empty list when no entries match — the LLM should escalate to Mode 2
        (offer-catalog-addition) per §F.1.2 prose.

    FR-DC-2 / FR-CE-8: registered with ``ttl_class="semi-static-7d"``,
    ``source_class="catalog_search"``, ``cacheable=True``. The cache key
    incorporates topic + bbox + filter so repeat searches dedup.
    """
    if not isinstance(topic, str) or not topic.strip():
        raise CatalogNotFoundError("catalog_search requires a non-empty topic string")

    # Normalize the bbox to a list for cache-key canonicalization.
    bbox_param: list[float] | None = list(location) if location is not None else None
    params = {
        "topic": topic.strip().lower(),
        "bbox": bbox_param,
        "source_filter": source_filter,
    }

    def _do_search() -> bytes:
        catalog = load_catalog()
        active = [e for e in catalog if e.status == "active"]
        if source_filter:
            active = [e for e in active if e.source_class == source_filter]
        if location is not None:
            active = [e for e in active if _bbox_overlaps_world(location, e)]
        scored = [(_score_entry(e, topic), e) for e in active]
        scored = [(s, e) for s, e in scored if s > 0.0]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        out = [
            {
                "relevance_score": s,
                **json.loads(e.model_dump_json()),
            }
            for s, e in scored
        ]
        return json.dumps(out).encode("utf-8")

    result = read_through(
        metadata=_CATALOG_SEARCH_METADATA,
        params=params,
        ext="json",
        fetch_fn=_do_search,
    )
    payload = json.loads(result.data.decode("utf-8"))
    logger.info(
        "catalog_search topic=%r n_matches=%d cache_hit=%s",
        topic,
        len(payload),
        result.hit,
    )
    return payload


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
            headers={"User-Agent": "grace-2/0.1 catalog_fetch Tier-3"},
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
    # mutate GRACE-2 state; writes to read-through cache only),
    # openWorldHint=True (Tier-2 OGC services, Tier-3 HTTPS external endpoints),
    # destructiveHint=False, idempotentHint=True (cache shim deduplicates).
    open_world_hint=True,
)
def catalog_fetch(entry_id: str, params: dict[str, Any] | None = None, **_extra_ignored: Any) -> dict[str, Any]:
    """Fetch bytes for a vetted catalog entry by its stable id (§F.1.2 Mode 1).

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
          (``gs://grace-2-hazard-prod-cache/cache/static-30d/catalog_fetch/<key>.<ext>``).
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
