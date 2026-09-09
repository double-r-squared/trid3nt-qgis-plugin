"""``search_living_atlas`` - BM25 + dense retrieval over the two harvested ESRI
Living Atlas strata. The two-pool rule is enforced STRUCTURALLY: authoritative is
the default surface, while community has ZERO default quota and appears only under
``include_community=True`` or as a labelled LAST RESORT when the authoritative
stratum returns nothing at all."""

from __future__ import annotations

import logging
from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.search.living_atlas_common import LivingAtlasEntry
from trid3nt_server.tools.search.living_atlas_index import rank_stratum

__all__ = ["search_living_atlas"]

logger = logging.getLogger("trid3nt_server.tools.search.search_living_atlas.search_living_atlas")

#: Small community quota so an opt-in never lets the community pool crowd the
#: authoritative results (they are appended AFTER, never interleaved above).
_COMMUNITY_QUOTA = 4

_SEARCH_LIVING_ATLAS_METADATA = AtomicToolMetadata(
    name="search_living_atlas",
    ttl_class="live-no-cache",
    source_class="living_atlas_search",
    cacheable=False,
)


def _entry_result(entry: LivingAtlasEntry, curation: str, score: float, last_resort: bool) -> dict[str, Any]:
    """One ranked entry + its curation label + the fetch instruction."""
    return {
        "relevance_score": round(float(score), 6),
        "curation": curation,
        "last_resort": last_resort,
        "id": entry.id,
        "title": entry.title,
        "snippet": entry.snippet,
        "service_type": entry.service_type,
        "service_url": entry.service_url,
        "extent": list(entry.extent) if entry.extent else None,
        "authoritative": entry.authoritative,
        "premium": entry.premium,
        "tags": entry.tags,
        "fetch_with": (
            "fetch_living_atlas_layer(item_id=%r, bbox=(min_lon,min_lat,max_lon,max_lat))"
            % entry.id
        ),
    }


@register_tool(_SEARCH_LIVING_ATLAS_METADATA, open_world_hint=True)
def search_living_atlas(
    query: str,
    include_community: bool = False,
    top_k: int = 8,
    **_extra_ignored: Any,
) -> list[dict[str, Any]]:
    """Search the ESRI Living Atlas of the World for fetchable map/data layers.

    ROUTING: the user wants an ESRI/ArcGIS Living Atlas layer, or no dedicated
    fetcher exists and ESRI's curated catalog is worth trying first. NOT for the
    internal public-source catalog (`search_data_catalog`), NOT for a dataset that
    already has its own fetcher, NOT to pull bytes - this only RANKS; the returned
    `id` goes to `fetch_living_atlas_layer`.

    Two-pool curation: by default ONLY authoritative entries come back. Community
    entries never take priority in an authoritative ask - they appear on
    `include_community=True` as a small labelled quota ranked BELOW authoritative,
    or as a labelled last resort when authoritative has nothing.

    `query` is a free-text topic, non-empty; `top_k` caps the authoritative results.
    Returns dicts ranked by `relevance_score`, each carrying `curation`,
    `last_resort`, `id`, `title`, `snippet`, `service_type`, `service_url`,
    `extent`, `authoritative`, `premium`, `tags` and `fetch_with`.
    """
    if not isinstance(query, str) or not query.strip():
        return []
    k = max(1, int(top_k)) if isinstance(top_k, (int, float)) else 8

    auth_ranked = rank_stratum(query, "authoritative", k)
    results = [_entry_result(e, "authoritative", s, False) for e, s in auth_ranked]

    if include_community:
        comm_ranked = rank_stratum(query, "community", _COMMUNITY_QUOTA)
        results += [_entry_result(e, "community", s, False) for e, s in comm_ranked]
    elif not results:
        # Authoritative returned nothing -> community as a LABELLED last resort.
        comm_ranked = rank_stratum(query, "community", k)
        results += [_entry_result(e, "community", s, True) for e, s in comm_ranked]

    logger.info(
        "search_living_atlas query=%r include_community=%s n_auth=%d n_total=%d",
        query, include_community, len(auth_ranked), len(results),
    )
    return results
