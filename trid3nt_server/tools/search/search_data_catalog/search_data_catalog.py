"""``search_data_catalog`` - keyword and bbox relevance ranking over the audited
public data-source YAML catalog. It only LISTS: the ids it returns are what
``fetch_from_catalog`` takes to pull bytes."""

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

__all__ = ["search_data_catalog"]

logger = logging.getLogger("trid3nt_server.tools.search.search_data_catalog.search_data_catalog")


# ---------------------------------------------------------------------------
# search_data_catalog -- topic-ranked retrieval over the YAML catalog.
# ---------------------------------------------------------------------------


_SEARCH_DATA_CATALOG_METADATA = AtomicToolMetadata(
    name="search_data_catalog",
    ttl_class="semi-static-7d",
    source_class="search_data_catalog",
    cacheable=True,
)

def _score_entry(entry: CatalogEntry, topic: str) -> float:
    """A topic-relevance score for one catalog entry: a lowercase substring and
    token-overlap heuristic, never a semantic match."""
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
    # Every CONTENT-WORD token of the topic found in the haystack adds 1. The
    # filler words are skipped, so a phrase made only of them cannot rack up a score
    # by overlapping with every entry in the catalog.
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
        return score  # an all-filler topic scores nothing here.
    matched_tokens = sum(1 for tok in tokens if tok in haystack)
    if matched_tokens == 0:
        return score  # no content-word hit at all.
    score += float(matched_tokens)
    # At least a third of the content tokens must hit before the entry counts as a
    # real match; otherwise one shared token carries an unrelated entry.
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
    """Does the entry plausibly cover ``bbox``? A COARSE heuristic - the catalog
    carries no per-entry spatial extent - so anything not clearly US-scoped is kept:
    recall over precision, since a dropped entry is invisible to the caller."""
    if bbox is None:
        return True
    text = (entry.description + " " + entry.name + " " + entry.how_to_use).lower()
    # A US-scoped entry drops out for a clearly non-US bbox centre. Both the
    # explicit tokens and the broader "us federal" curator language count as
    # US-only signals; "conterminous us" usually accompanies Hawaii and Alaska
    # coverage but still never extends internationally, so it counts too.
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
        # A broad US envelope covering the lower 48, Alaska, Hawaii and the
        # Caribbean territories. Any bbox centre inside the band qualifies.
        cx, cy = 0.5 * (mn_lon + mx_lon), 0.5 * (mn_lat + mx_lat)
        return (-180.0 <= cx <= -60.0) and (15.0 <= cy <= 75.0)
    return True

@register_tool(
    _SEARCH_DATA_CATALOG_METADATA,
    # Open-world: the lookup itself is in-process, but what it surfaces are
    # external endpoints a later fetch will actually hit.
    open_world_hint=True,
)
def search_data_catalog(
    topic: str,
    location: tuple[float, float, float, float] | None = None,
    source_filter: str | None = None,
    # Absorb model-invented kwargs; the normalizer already strips most of them.
    **_extra_ignored: Any,
) -> list[dict[str, Any]]:
    """Search the curated public data-source catalog for vetted entries on a topic.

    ROUTING: a free-text data need where the catalog's curator-vetted endpoints and
    their `how_to_use` hints are wanted. The entries carry stable ids that
    `fetch_from_catalog` takes. NOT for geocoding, NOT for pulling bytes, and NOT
    for enumerating already-published layers - the catalog describes EXTERNAL
    sources, not what is on the map.

    `topic` is required and non-empty. `location` is an optional EPSG:4326 bbox; it
    drops entries a coverage heuristic says the bbox cannot plausibly hit.
    `source_filter` narrows to one `source_class`.

    Returns one dict per matching entry - the catalog row plus a `relevance_score` -
    ranked, with the row carrying id, name, description, urls, access_tier,
    credential_tier, ttl_class, source_class, license, citation, vintage,
    last_verified, status, how_to_use and api_key_secret_ref. An EMPTY list when
    nothing matches; fall back to a generic fetcher or research, do not invent an id.
    """
    if not isinstance(topic, str) or not topic.strip():
        raise CatalogNotFoundError("search_data_catalog requires a non-empty topic string")

    # Catalog-surfacing Design 1 (arm-flagged): return spec-served source CARDS
    # (full docstring + typed param schema + gates/caveats/endpoint mirrors) instead of the
    # YAML catalog entries. The model then calls fetch_from_catalog(source=..., params=...).
    # DEFAULT config (no arm flag) is unaffected -- the YAML path below runs.
    from trid3nt_server.tools.fetchers._router import registration as _reg

    if _reg.catalog_arm() == "1":
        cards = _reg.search_spec_cards(topic.strip(), k=10)
        logger.info(
            "search_data_catalog[arm1] topic=%r n_cards=%d", topic, len(cards)
        )
        return cards

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
