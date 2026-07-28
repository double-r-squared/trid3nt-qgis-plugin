"""Region-disambiguation picker card builders (pure payload construction)."""
from __future__ import annotations

import logging

from pydantic import ValidationError

from trid3nt_contracts.region_choice import (
    RegionCandidate,
    RegionChoiceRequestEnvelopePayload,
)

logger = logging.getLogger("trid3nt_server.agent.gates.cards.region_choice")


# --------------------------------------------------------------------------- #
# Region-disambiguation picker (state-bbox-fallback narrowing).
# --------------------------------------------------------------------------- #
#
# made ``geocode_location`` snap a vague/regional query ("south
# Florida") to the WHOLE state bbox and stamp ``source="state-bbox-fallback"``
# + an honest ``fallback_reason``. That state bbox stays the DEFAULT/automated
# answer. ON TOP of it, when an interactive client is connected, surface a user
# choice to NARROW to a sub-region (default: counties). This MIRRORS the
# credential-request pause/resume seam above: emit a ``region-choice-request``,
# pause the turn on a future keyed by the choice request_id, and on
# ``region-choice-provided`` either narrow the geocode bbox (choice="region")
# or keep the state bbox (choice="whole_state"). Fail-open: a headless client /
# timeout keeps the state bbox unchanged, so the automated path never blocks.

# Default candidate granularity. Counties ship at v0.1; structured as a module
# constant so a light state-size/goal heuristic can override it per request.
# TODO(region-choice): coarser ("state_region" groupings) / finer ("place" /
# "zcta") levels are a follow-up — the RegionAdminLevel Literal + the TIGER
# fetch plumbing in fetch_administrative_boundaries gate that expansion.
_DEFAULT_REGION_ADMIN_LEVEL = "county"

# How many candidate regions to surface at most. A large state (e.g. Texas =
# 254 counties) would otherwise flood the in-chat card list + the map
# choropleth; the cap keeps the picker legible. The whole-state default is
# always available regardless, so a capped list never hides the honest answer.
_MAX_REGION_CANDIDATES = 254


def _region_admin_level_for(state_code: str, query: str) -> str:
    """Choose the candidate admin granularity for ``state_code`` + ``query``.

    DEFAULT is ``"county"`` for every state (the v0.1 shipping behaviour). This
    is the single seam a future heuristic (or the agent) hooks to pick a
    coarser/finer level by state size + query goal — kept as a function so the
    policy lives in one place. Today it returns the county default unchanged;
    the ``RegionAdminLevel`` Literal is closed to ``"county"`` so any other
    return value would fail envelope validation (a deliberate guard until the
    finer-level fetch plumbing lands).
    """
    return _DEFAULT_REGION_ADMIN_LEVEL


def _build_region_candidates(
    state_bbox: tuple[float, float, float, float],
    admin_level: str,
) -> list[RegionCandidate]:
    """Build the candidate sub-regions for a snapped state via TIGER boundaries.

    Fetches the administrative boundaries for ``admin_level`` (default
    ``"county"``) clipped to the whole-state ``state_bbox`` through the EXISTING
    ``fetch_administrative_boundaries`` fetch path, reads the resulting
    FlatGeobuf back with geopandas, and emits one ``RegionCandidate`` per
    feature: ``region_id`` from the TIGER GEOID, ``name`` from the feature
    NAME(LSAD), ``bbox`` from the feature polygon's ``total_bounds``.

    Best-effort: any failure (geopandas missing, TIGER download hiccup, empty
    clip) returns an EMPTY list — the caller then offers only the whole-state
    default (honest degrade, fallback norm). Never raises.

    Calls ``_fetch_admin_boundaries_bytes`` directly (rather than the
    cache-wrapped ``fetch_administrative_boundaries``) so the candidate build
    is decoupled from the layer-publish path: we only need the geometry +
    attributes in-process, not a published LayerURI. The TIGER download is
    itself cached for the published-boundary path, so this does not add a new
    uncached fetch in practice.
    """
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
        from io import BytesIO

        from ...tools.fetchers.socioeconomic.fetch_administrative_boundaries.fetch_administrative_boundaries import (
            _fetch_admin_boundaries_bytes,
        )
    except ImportError:
        logger.debug("region-choice: geopandas unavailable", exc_info=True)
        return []

    try:
        fgb_bytes = _fetch_admin_boundaries_bytes(admin_level, tuple(state_bbox))
    except Exception:  # noqa: BLE001 — boundary fetch is best-effort
        logger.warning(
            "region-choice: fetch_admin_boundaries failed level=%s bbox=%s; "
            "offering whole-state default only",
            admin_level,
            state_bbox,
            exc_info=True,
        )
        return []

    try:
        gdf = gpd.read_file(BytesIO(fgb_bytes), engine="pyogrio")
    except Exception:  # noqa: BLE001 — parse is best-effort
        logger.warning("region-choice: FlatGeobuf read failed", exc_info=True)
        return []

    candidates: list[RegionCandidate] = []
    seen_ids: set[str] = set()
    for _, row in gdf.iterrows():
        geom = row.get("geometry")
        if geom is None or geom.is_empty:
            continue
        geoid = (
            row.get("GEOID")
            or row.get("GEOIDFQ")
            or row.get("COUNTYFP")
            or ""
        )
        region_id = f"{admin_level}-{geoid}" if geoid else f"{admin_level}-{len(candidates)}"
        if region_id in seen_ids:
            continue
        seen_ids.add(region_id)
        name = (
            row.get("NAMELSAD")
            or row.get("NAME")
            or region_id
        )
        minx, miny, maxx, maxy = (float(v) for v in geom.bounds)
        try:
            candidate = RegionCandidate(
                region_id=str(region_id)[:120],
                name=str(name)[:200],
                bbox=(minx, miny, maxx, maxy),
                admin_level=admin_level,  # type: ignore[arg-type]
            )
        except ValidationError:
            # A degenerate / out-of-range polygon bbox — skip it rather than
            # abort the whole set (one bad TIGER feature must not kill the pick).
            continue
        candidates.append(candidate)
        if len(candidates) >= _MAX_REGION_CANDIDATES:
            break

    candidates.sort(key=lambda c: c.name)
    logger.info(
        "region-choice: built %d candidate region(s) level=%s",
        len(candidates),
        admin_level,
    )
    return candidates


def _build_region_choice_request_payload(
    *,
    request_id: str,
    geocode_result: dict,
) -> "RegionChoiceRequestEnvelopePayload | None":
    """Build a validated ``region-choice-request`` from a state-snap geocode dict.

    Derives the state name + 2-letter code from the geocode result's ``name``
    (``"<State>, United States"``), uses its ``bbox`` as the whole-state extent,
    builds the candidate sub-regions (default: counties), and composes an honest
    prompt that says the agent snapped to the whole state and is offering a
    narrower pick (the fallback honesty floor).

    Returns ``None`` when the state cannot be resolved or the result is not a
    valid state-snap shape — the caller then leaves the state bbox unchanged.
    """
    from ...tools.fetchers.us_states import resolve_state_code, state_display_name

    bbox = geocode_result.get("bbox")
    if not (isinstance(bbox, (list, tuple)) and len(bbox) == 4):
        return None
    # The state-snap name is "<State>, United States"; strip the suffix to get
    # the state name, then resolve the 2-letter code.
    raw_name = str(geocode_result.get("name") or "")
    state_name = raw_name.split(",")[0].strip()
    state_code = resolve_state_code(state_name)
    if state_code is None:
        logger.info(
            "region-choice: could not resolve state from name=%r; "
            "keeping whole-state bbox",
            raw_name,
        )
        return None
    # Prefer the canonical display name for the resolved code.
    state_name = state_display_name(state_code)

    admin_level = _region_admin_level_for(
        state_code, str(geocode_result.get("query") or "")
    )
    candidates = _build_region_candidates(tuple(bbox), admin_level)

    # Honest prompt — name the snap + the offer (fallback norm). Prefer the
    # geocode's own fallback_reason as the lead so the narration is consistent.
    reason = str(geocode_result.get("fallback_reason") or "").strip()
    level_word = "county" if admin_level == "county" else admin_level
    if candidates:
        offer = (
            f" Pick a {level_word} below to narrow the area, or keep the whole "
            f"state of {state_name}."
        )
    else:
        offer = (
            f" I could not load {level_word} boundaries right now, so I will "
            f"use the whole state of {state_name} unless you refine the area."
        )
    lead = reason or (
        f"No precise match for that location; I snapped to the whole state of "
        f"{state_name}."
    )
    message = (lead + offer)[:1024]

    try:
        return RegionChoiceRequestEnvelopePayload(
            request_id=request_id,
            state_name=state_name,
            state_code=state_code,
            state_bbox=(float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])),
            candidates=candidates,
            message=message,
        )
    except ValidationError:
        logger.warning(
            "region-choice: request payload validation failed name=%r bbox=%s",
            raw_name,
            bbox,
            exc_info=True,
        )
        return None
