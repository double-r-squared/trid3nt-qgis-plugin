"""``search_living_atlas``: ranked retrieval over the harvested ESRI Living Atlas.

BM25 + dense over the two harvested strata (``living_atlas_common`` /
``living_atlas_index``). Enforces NATE's two-pool rule STRUCTURALLY: the
authoritative stratum is the default surface; the community stratum has ZERO
default quota and appears ONLY when ``include_community=True`` (a small labelled
quota) or as a labelled LAST RESORT when the authoritative stratum returns nothing.

Designed to become a stratum when the flag-gated pools arm (TRID3NT_CATALOG_ARM,
NO_ADVANCE) lights: the per-stratum indexes + quota composition here ARE the pool
mechanics, so the same ranking drops behind the harness trigger unchanged.
"""

from __future__ import annotations

import logging
from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.agent.tools import register_tool
from trid3nt_server.agent.tools.search.living_atlas_common import LivingAtlasEntry
from trid3nt_server.agent.tools.search.living_atlas_index import rank_stratum

__all__ = ["search_living_atlas"]

logger = logging.getLogger("trid3nt_server.agent.tools.search.search_living_atlas.search_living_atlas")

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

    **What it does:** Ranks the harvested ESRI Living Atlas catalog (thousands of
    ArcGIS Image / Feature / Map Services) by BM25 + dense relevance to a free-text
    query and returns the top matches, each with its ArcGIS ``service_url``,
    ``service_type``, geographic extent, and a curation label. The returned ``id``
    is passed to ``fetch_living_atlas_layer`` to pull the actual bytes.

    **When to use:**
    - The user wants an ESRI/ArcGIS Living Atlas layer ("find a Living Atlas
      wetlands layer", "ESRI land cover", "authoritative population imagery").
    - A dedicated fetcher does not exist for the needed data and you want ESRI's
      curated, authoritative catalog before falling back to raw sources.

    **When NOT to use:**
    - For the internal curated public-source catalog -> ``search_data_catalog``.
    - For a named US dataset that already has its own fetcher (DEM, NLCD land
      cover, FEMA flood zones) -> call that fetcher directly.
    - To pull bytes -> that is ``fetch_living_atlas_layer`` (this only ranks).

    **Two-pool curation (NATE's rule):** by default ONLY authoritative entries
    (ESRI ``contentStatus`` authoritative badge) are returned. Community entries
    NEVER get priority in an authoritative ask -- they appear only when you pass
    ``include_community=True`` (a small labelled quota, always ranked below the
    authoritative results) or as a labelled last resort when the authoritative
    stratum has nothing.

    **Parameters:**
        query: free-text topic ("wetlands", "land cover", "sea surface
            temperature", "wildfire perimeters"). Required, non-empty.
        include_community: opt in to community-curated entries (default False).
        top_k: max authoritative results to return (default 8).

    **Returns:** a list of dicts ranked by ``relevance_score`` (desc). Each carries
    ``curation`` ("authoritative"|"community"), ``last_resort`` (bool), ``id``,
    ``title``, ``snippet``, ``service_type``, ``service_url``, ``extent``,
    ``authoritative``, ``premium``, ``tags``, and ``fetch_with`` (the exact
    ``fetch_living_atlas_layer`` call). Empty list when nothing matches either
    stratum.
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
