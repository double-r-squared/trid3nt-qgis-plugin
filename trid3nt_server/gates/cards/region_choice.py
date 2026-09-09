"""Region-disambiguation picker card builders (pure payload construction)."""
from __future__ import annotations

import logging

from pydantic import ValidationError

from trid3nt_contracts.region_choice import (
    RegionCandidate,
    RegionChoiceRequestEnvelopePayload,
)

logger = logging.getLogger("trid3nt_server.gates.cards.region_choice")


# --------------------------------------------------------------------------- #
# Region-disambiguation picker (state-bbox-fallback narrowing).
# --------------------------------------------------------------------------- #
#
# A vague or regional query snaps to the WHOLE state bbox, stamped
# ``source="state-bbox-fallback"`` with an honest ``fallback_reason``, and that
# state bbox stays the DEFAULT automated answer. ON TOP of it, when an
# interactive client is connected, the user is offered a NARROWER sub-region.
# Fail-open: a headless client or a timeout keeps the state bbox unchanged, so
# the automated path never blocks.

# Default candidate granularity, a module constant so a light state-size or
# goal heuristic can override it per request. The ``RegionAdminLevel`` Literal
# is CLOSED to ``"county"``, so any other value fails envelope validation until
# the finer-level TIGER fetch plumbing exists.
_DEFAULT_REGION_ADMIN_LEVEL = "county"

# How many candidate regions to surface at most. A large state (e.g. Texas =
# 254 counties) would otherwise flood the in-chat card list + the map
# choropleth; the cap keeps the picker legible. The whole-state default is
# always available regardless, so a capped list never hides the honest answer.
_MAX_REGION_CANDIDATES = 254


def _region_admin_level_for(state_code: str, query: str) -> str:
    """Choose the candidate admin granularity for ``state_code`` and ``query``.

    The ONE seam where that policy lives; ``"county"`` for every state today."""
    return _DEFAULT_REGION_ADMIN_LEVEL


def _admin_boundaries_fgb_bytes(
    level: str, bbox: tuple[float, float, float, float]
) -> bytes:
    """Fetch TIGER admin-boundary FGB bytes in-process: no cache, no publish.

    Params are validated and quantized exactly as the published tool does."""
    from trid3nt_server.tools.fetchers._router import registration, router

    spec = registration.get_spec("fetch_administrative_boundaries")
    if spec is None:
        raise RuntimeError("fetch_administrative_boundaries spec not registered")
    params = router.validate_params(spec, {"level": level, "bbox": list(bbox)})
    return router.select_executor(spec)(spec, params)


def _build_region_candidates(
    state_bbox: tuple[float, float, float, float],
    admin_level: str,
) -> list[RegionCandidate]:
    """Build the candidate sub-regions for a snapped state, one per feature.

    Never raises: any failure returns an EMPTY list and the caller then offers
    only the whole-state default."""
    # Read through the in-process executor rather than the cache-wrapped tool,
    # so the candidate build is decoupled from the layer-publish path: only the
    # geometry and attributes are needed here, never a published LayerURI.
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
        from io import BytesIO
    except ImportError:
        logger.debug("region-choice: geopandas unavailable", exc_info=True)
        return []

    try:
        fgb_bytes = _admin_boundaries_fgb_bytes(admin_level, tuple(state_bbox))
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

    ``None`` when the state cannot be resolved or the shape is not a state-snap,
    and the caller then leaves the state bbox unchanged."""
    from trid3nt_server.tools.fetchers.us_states import resolve_state_code, state_display_name

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

    # The prompt must NAME the snap as well as the offer, and it leads with the
    # geocode's own fallback_reason so the narration stays consistent with it.
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
