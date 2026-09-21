"""Nominatim forward geocoder via geopy: the query as written, the service's own
answer -- nothing between them."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from geopy.exc import GeocoderServiceError
from geopy.geocoders import Nominatim

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.gates.input_review import resolve_input_gate_mode
from trid3nt_server.tools import register_tool
from trid3nt_server.tools.cache import read_through
from trid3nt_server.tools.fetchers._fetch_common import (
    BboxInvalidError,
    UpstreamAPIError,
    _DEFAULT_USER_AGENT,
)

__all__ = [
    "geocode_location",
    "GeocodeNoMatchError",
    "GeocodeUnconfirmableError",
]

logger = logging.getLogger(
    "trid3nt_server.tools.fetchers.socioeconomic.geocode_location.geocode_location"
)


class GeocodeNoMatchError(UpstreamAPIError):
    """Forward-geocoding found no match -- including a bare regional qualifier
    ("south Florida", "greater Boston") that Nominatim itself cannot narrow
    with no state or country steer this tool does not add. An HONEST,
    NOT-retryable failure: the SAME query string will not resolve on a
    retry -- name a specific place, add a state/country, or draw an area."""

    error_code = "GEOCODE_NO_MATCH"
    retryable = False


class GeocodeUnconfirmableError(UpstreamAPIError):
    """No session is bound, so the service's top match has nobody to confirm it
    against the map. An HONEST, NOT-retryable failure: a top match accepted with
    no one looking is a guess wearing the shape of an answer. State the extent on
    the call, or draw the area in a session."""

    error_code = "GEOCODE_UNCONFIRMABLE"
    retryable = False


_GEOCODE_LOCATION_METADATA = AtomicToolMetadata(
    name="geocode_location",
    ttl_class="dynamic-1h",
    source_class="geocode",
)

#: Built once: geopy.geocoders.Nominatim owns the socket, the usage-policy
#: pacing and the response parse; this module states no HTTP of its own.
_geocoder: Nominatim | None = None


def _client() -> Nominatim:
    global _geocoder
    if _geocoder is None:
        user_agent = os.environ.get("TRID3NT_NOMINATIM_USER_AGENT", _DEFAULT_USER_AGENT)
        _geocoder = Nominatim(user_agent=user_agent, timeout=15.0)
    return _geocoder


def _fetch_nominatim_geocode_bytes(query: str) -> bytes:
    """Forward-geocode ``query`` through geopy and return exactly what the
    service published, as JSON bytes -- no reordering, no expansion, no
    fallback. A query the service cannot resolve to one place is a typed
    refusal, never a guess."""
    try:
        location = _client().geocode(query, exactly_one=True, addressdetails=False)
    except GeocoderServiceError as exc:
        raise UpstreamAPIError(
            f"Nominatim search failed for query={query!r}: {exc}"
        ) from exc

    if location is None:
        raise GeocodeNoMatchError(
            f"Nominatim found no match for {query!r}. Name a specific place, add "
            f"a state or country, or draw an area instead."
        )

    raw = location.raw or {}
    bb = raw.get("boundingbox")
    if not bb or len(bb) != 4:
        raise GeocodeNoMatchError(
            f"Nominatim matched {query!r} but published no bounding box for it. "
            f"Name a specific place or draw an area instead."
        )
    # Nominatim publishes boundingbox as [south, north, west, east] strings.
    south, north, west, east = (float(v) for v in bb)

    structured = {
        "name": raw.get("display_name", query),
        "latitude": float(location.latitude),
        "longitude": float(location.longitude),
        # Normalize to (min_lon, min_lat, max_lon, max_lat) -- the project
        # canonical bbox shape (matches LayerURI / Census / py3dep).
        "bbox": [west, south, east, north],
        "source": "nominatim",
        "query": query,
        "osm_type": raw.get("osm_type"),
        "osm_id": raw.get("osm_id"),
        "place_id": raw.get("place_id"),
    }
    return json.dumps(structured).encode("utf-8")


@register_tool(
    _GEOCODE_LOCATION_METADATA,
    # Annotations: readOnlyHint=True, openWorldHint=True (OSM Nominatim API),
    # destructiveHint=False, idempotentHint=True (cache shim deduplicates).
    open_world_hint=True,
)
def geocode_location(
    query: str, input_mode: str | None = None, **_extra_ignored: Any
) -> dict[str, Any]:
    """Forward-geocode a free-text place name through OSM Nominatim (via
    geopy): the query travels AS WRITTEN and the answer is exactly what the
    service gives back -- its own matched name, its own bounding box.

    STRICT by design: no state detection, no qualifier stripping, no bbox
    expansion, no result-class reordering, no private lookup table. A value
    set once by a person is not automated toward correctness, only toward
    strictness. A query the service cannot resolve to one place refuses by
    name; the fix is naming a specific place, adding a state or country, or
    drawing an area, never a retry of the same string.

    Use this when: a request names a place and a downstream tool needs a bbox.

    Do NOT use this for: reverse geocoding, routing or distance, or
    parcel-level address resolution. The bbox is whatever extent Nominatim
    publishes for the matched feature, so a county or a state comes back very
    large -- narrow it before handing it to a heavy download.

    Params: ``query``, a non-empty free-text place name. ``input_mode``
    (``"auto"`` | ``"user_gated"``, default the session's), read the same
    lever every other input gate reads.

    Returns the canonical ``name``, a ``[min_lon, min_lat, max_lon, max_lat]``
    ``bbox``, a centroid, the source, and ``match_mode``: ``"auto-accepted"``
    in auto mode (the top match, labeled as such in the journal) or
    ``"pending-confirm"`` in user-gated mode -- the case-AOI-commit step is
    what shows the confirm gate on that label; this call always returns its
    match at once, never waiting on a turn. With no session bound it refuses
    naming the query: a top match nobody can look at is not an answer.
    """
    if not isinstance(query, str) or not query.strip():
        raise BboxInvalidError("geocode_location requires a non-empty string query")
    text = query.strip()

    from trid3nt_server.render.pipeline_emitter import current_emitter

    if current_emitter() is None:
        raise GeocodeUnconfirmableError(
            f"{text!r} resolves to the service's top match, which a person "
            "confirms on the map; no session is bound to this run, so there is "
            "nobody to confirm it. State the extent on the call, or ask in a "
            "session."
        )

    mode = resolve_input_gate_mode(input_mode)

    result = read_through(
        metadata=_GEOCODE_LOCATION_METADATA,
        params={"query": text},
        ext="json",
        fetch_fn=lambda: _fetch_nominatim_geocode_bytes(text),
    )
    # The cache URI is intentionally NOT returned to the LLM -- Tier
    # separation (invariant 5): no gs:// / s3:// URIs leak into model text.
    payload = json.loads(result.data.decode("utf-8"))
    payload["match_mode"] = "auto-accepted" if mode == "auto" else "pending-confirm"

    logger.info(
        "geocode_location query=%r resolved name=%r mode=%s cache_hit=%s",
        text, payload.get("name"), mode, result.hit,
    )
    return payload
