"""``search_data_catalog``: keyword/bbox relevance search over the audited public
data-source YAML catalog.

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

__all__ = ["search_data_catalog"]

logger = logging.getLogger("trid3nt_server.tools.discovery.search_data_catalog")


# ---------------------------------------------------------------------------
# search_data_catalog — topic-ranked retrieval over the YAML catalog.
# ---------------------------------------------------------------------------


_SEARCH_DATA_CATALOG_METADATA = AtomicToolMetadata(
    name="search_data_catalog",
    ttl_class="semi-static-7d",
    source_class="search_data_catalog",
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
    _SEARCH_DATA_CATALOG_METADATA,
    # Annotations: readOnlyHint=True (read-only; no state mutation),
    # openWorldHint=True (in-memory catalog lookup, but fetch_from_catalog ultimately
    # dispatches to Tier-2/3 external APIs; search step itself is intra-process),
    # destructiveHint=False, idempotentHint=True (cache shim deduplicates).
    open_world_hint=True,
)
def search_data_catalog(
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
    to ``fetch_from_catalog``.

    Do NOT use this for: live geocoding (use ``geocode_location``); pulling
    actual bytes (use ``fetch_from_catalog`` or one of the dedicated fetchers);
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
    ``source_class="search_data_catalog"``, ``cacheable=True``. The cache key
    incorporates topic + bbox + filter so repeat searches dedup.
    """
    if not isinstance(topic, str) or not topic.strip():
        raise CatalogNotFoundError("search_data_catalog requires a non-empty topic string")

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
        metadata=_SEARCH_DATA_CATALOG_METADATA,
        params=params,
        ext="json",
        fetch_fn=_do_search,
    )
    payload = json.loads(result.data.decode("utf-8"))
    logger.info(
        "search_data_catalog topic=%r n_matches=%d cache_hit=%s",
        topic,
        len(payload),
        result.hit,
    )
    return payload
