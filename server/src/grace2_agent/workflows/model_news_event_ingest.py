"""``model_news_event_ingest`` workflow — Case 2 partial composer (job-0119).

This module implements the **Case 2 partial composer**:

    [for each source: dispatch web_fetch | fetch_nws_event | fetch_storm_events_db]
      → aggregate_claims_across_sources(claim_targets=[...])
      → geocode_location("<derived location>") → bbox
      → compose presentation envelope (derived params + provenance + confidence)
      → EventIngestResult
    STOP — sprint-13 picks up with MODFLOW

Per the kickoff, this is a REVIEW-GATED workflow: it produces the typed
result envelope (``EventIngestResult``) BUT does not dispatch any downstream
solver. The web UI renders the result as a ``case2-event-ingest-result``
review modal; the user approves the derived params; sprint-13's MODFLOW
consumes the approved envelope to start its solve.

Implementation discipline:

- Each atomic tool call goes through the registry (``TOOL_REGISTRY[name].fn``)
  rather than directly importing the function, so the workflow honors the
  registry-as-source-of-truth invariant the kickoff calls out explicitly
  ("do NOT bypass the registry").
- ``pipeline_emitter`` is awaited at each major stage so the web UI gets
  live pipeline-state envelopes per FR-WC-12 and Invariant 8 (cancel-
  through-emitter at any await point).
- The LLM is NOT in the chain inside this workflow body — composition is
  deterministic Python. The kickoff's "LLM guidance" line refers to the
  agent's external routing layer choosing this workflow + its
  ``target_event_type`` argument from natural language; once the workflow
  runs, no model call happens here.

Cross-cutting principles in force:

- **Invariant 1 (Determinism boundary): preserves.** No LLM in the chain.
  ``presentation_text`` is built from format strings keyed on the
  ``derived_params`` dict.
- **Invariant 2 (Deterministic workflows): preserves.** Straight-line
  composition with typed-exception handling at the boundary.
- **Invariant 8 (Cancellation is first-class): preserves.** Every await
  point (pipeline emitter calls, geocode call) gives the cancel chain
  a propagation site. Asyncio.CancelledError bubbles untouched.
- **Invariant 9 (Confirmation before consequence): preserves.** The
  workflow STOPS BEFORE solver dispatch; the returned envelope is what
  the agent surface shows to the user for review/approval.
- **Geographic-correctness gate (job-0086): preserves.** The geocoded
  bbox is anchored to the actual claim-aggregator's location value —
  not URL/render consistency. A unit test asserts the geocode is fed
  the exact derived ``location`` string (algebraic identity, not just
  round-trip).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from grace2_contracts.case_results import (
    DerivedEventParam,
    EventIngestProvenance,
    EventIngestResult,
)
from grace2_contracts.tool_registry import AtomicToolMetadata

from ..tools import TOOL_REGISTRY, register_tool

if TYPE_CHECKING:
    from ..pipeline_emitter import PipelineEmitter

__all__ = [
    "model_news_event_ingest",
    "run_model_news_event_ingest",
    "EventIngestError",
    "EventIngestInputError",
    "SUPPORTED_EVENT_TYPES",
    "SUPPORTED_SOURCE_TYPES",
]

logger = logging.getLogger("grace2_agent.workflows.model_news_event_ingest")


# --------------------------------------------------------------------------- #
# Typed errors (FR-AS-11)
# --------------------------------------------------------------------------- #


class EventIngestError(RuntimeError):
    """Base class for ``model_news_event_ingest`` failures."""

    error_code: str = "EVENT_INGEST_ERROR"
    retryable: bool = False


class EventIngestInputError(EventIngestError):
    """Caller passed bad ``sources`` / ``target_event_type`` / ... ."""

    error_code = "EVENT_INGEST_INPUT_INVALID"
    retryable = False


# --------------------------------------------------------------------------- #
# Closed-enough enums (caller still receives an error if outside)
# --------------------------------------------------------------------------- #

#: Event types the v0.1 workflow routes its claim_targets list against.
#: ``spill`` / ``flood`` / ``wildfire`` / ``hurricane`` cover the FR-HEP demo
#: case spectrum (the Longview spill, Hurricane Idalia, etc.). New event
#: types are an additive expansion; unknown types raise ``EventIngestInputError``.
SUPPORTED_EVENT_TYPES: tuple[str, ...] = (
    "spill",
    "flood",
    "wildfire",
    "hurricane",
)

#: Source types this workflow dispatches. ``url`` → ``web_fetch``;
#: ``nws_alert`` → ``fetch_nws_event``; ``storm_event`` → ``fetch_storm_events_db``.
SUPPORTED_SOURCE_TYPES: tuple[str, ...] = (
    "url",
    "nws_alert",
    "storm_event",
)


# --------------------------------------------------------------------------- #
# Claim-target routing per event type
# --------------------------------------------------------------------------- #


def _claim_targets_for_event_type(event_type: str) -> list[str]:
    """Per-event-type claim_targets list passed to the aggregator.

    ``spill`` events care about contaminant + scale + location + date +
    casualties. ``flood``/``wildfire``/``hurricane`` skip ``contaminant``
    (those event types don't carry a contaminant claim) and substitute
    different focus targets where it makes sense. The aggregator silently
    drops unknown targets, but its registered surface raises
    ``ClaimAggInputError`` on bad targets — so we never pass anything
    outside the aggregator's ``SUPPORTED_TARGETS``.
    """
    base = ["location", "date", "scale", "casualties"]
    if event_type == "spill":
        return ["location", "scale", "contaminant", "date", "casualties"]
    if event_type in ("flood", "wildfire", "hurricane"):
        return base
    # Unsupported event type is rejected upstream; defensive fallback here.
    return base


# --------------------------------------------------------------------------- #
# Source-tier classification (FR-HEP-2)
# --------------------------------------------------------------------------- #


def _source_authority_tier(source_type: str, final_url: str | None) -> int | None:
    """Return the FR-HEP-2 source-authority tier for a source.

    Tier 1 — federal agency (NWS, NOAA Storm Events DB, .gov news pages
        with agency authoritative provenance).
    Tier 2 — major news outlet (.com news domains; v0.1 doesn't yet
        distinguish further — surfaced as OQ-0119-NEWS-SOURCE-TIERING).
    """
    if source_type in ("nws_alert", "storm_event"):
        return 1
    if source_type == "url" and final_url:
        lowered = final_url.lower()
        if ".gov/" in lowered or lowered.endswith(".gov"):
            return 1
        return 2
    return None


# --------------------------------------------------------------------------- #
# Per-source dispatchers (via the registry — kickoff hard rule)
# --------------------------------------------------------------------------- #


def _registry_fn(name: str) -> Any:
    """Resolve ``name`` to the registered tool callable.

    Goes through ``TOOL_REGISTRY`` so the workflow honors the registry seam.
    Raises ``EventIngestError`` with a clear message if the tool is missing —
    a configuration-time failure surfaces here loudly rather than during a
    demo run.
    """
    entry = TOOL_REGISTRY.get(name)
    if entry is None:
        raise EventIngestError(
            f"required atomic tool {name!r} is not registered "
            f"(known tools: {sorted(TOOL_REGISTRY)[:10]}...)"
        )
    return entry.fn


async def _fetch_url_source(
    identifier: str, pipeline_emitter: "PipelineEmitter | None"
) -> dict[str, Any]:
    """Dispatch ``web_fetch`` against a URL source. Returns the raw result."""
    if pipeline_emitter is not None:
        step_id = await pipeline_emitter.add_step(
            name=f"Fetch URL: {identifier[:60]}", tool_name="web_fetch"
        )
        await pipeline_emitter.mark_running(step_id)
    else:
        step_id = None
    try:
        fn = _registry_fn("web_fetch")
        # main_text mode gives narrative-ready text for claim aggregation.
        result = fn(url=identifier, extract="main_text")
    except Exception:
        if pipeline_emitter is not None and step_id is not None:
            await pipeline_emitter.mark_failed(
                step_id, "WEB_FETCH_FAILED", f"web_fetch failed for {identifier!r}"
            )
        raise
    if pipeline_emitter is not None and step_id is not None:
        await pipeline_emitter.mark_complete(step_id)
    return result if isinstance(result, dict) else {"content": result}


async def _fetch_nws_alert_source(
    identifier: str, pipeline_emitter: "PipelineEmitter | None"
) -> dict[str, Any]:
    """Dispatch ``fetch_nws_event`` against an NWS source identifier.

    The identifier is interpreted as the ``area`` argument: a 2-letter state
    code or a 5-digit FIPS county (the only string forms ``fetch_nws_event``
    accepts; a bbox would need a tuple, which the source dict shape doesn't
    carry — surfaced as OQ-0119-NWS-IDENTIFIER-SHAPE).
    """
    if pipeline_emitter is not None:
        step_id = await pipeline_emitter.add_step(
            name=f"Fetch NWS alerts: {identifier}",
            tool_name="fetch_nws_event",
        )
        await pipeline_emitter.mark_running(step_id)
    else:
        step_id = None
    try:
        fn = _registry_fn("fetch_nws_event")
        result = fn(area=identifier)
    except Exception:
        if pipeline_emitter is not None and step_id is not None:
            await pipeline_emitter.mark_failed(
                step_id,
                "NWS_FETCH_FAILED",
                f"fetch_nws_event failed for {identifier!r}",
            )
        raise
    if pipeline_emitter is not None and step_id is not None:
        await pipeline_emitter.mark_complete(step_id)
    # fetch_nws_event returns a LayerURI; for claim aggregation we just need
    # enough text to feed the aggregator. The alert's headlines+descriptions
    # live INSIDE the FlatGeobuf the LayerURI references — we surface the
    # layer name + a structured marker the aggregator can extract from.
    return {
        "layer_uri": result,
        "layer_name": getattr(result, "name", str(identifier)),
        "description": getattr(result, "name", str(identifier)),
    }


async def _fetch_storm_event_source(
    identifier: str, pipeline_emitter: "PipelineEmitter | None"
) -> dict[str, Any]:
    """Dispatch ``fetch_storm_events_db`` against a storm-event identifier.

    The identifier is interpreted as ``"<year>"`` or ``"<year>:<state>"``
    (e.g. ``"2022"`` or ``"2022:FL"``) so the dict-keyed source shape can
    address the storm-events year+state surface. Year-only fetches return
    all states.
    """
    if pipeline_emitter is not None:
        step_id = await pipeline_emitter.add_step(
            name=f"Fetch Storm Events: {identifier}",
            tool_name="fetch_storm_events_db",
        )
        await pipeline_emitter.mark_running(step_id)
    else:
        step_id = None
    try:
        if ":" in identifier:
            year_str, state = identifier.split(":", 1)
        else:
            year_str, state = identifier, None
        try:
            year = int(year_str)
        except ValueError as exc:
            raise EventIngestInputError(
                f"storm_event identifier {identifier!r} must be 'YYYY' or 'YYYY:STATE'; "
                f"got non-integer year token"
            ) from exc
        fn = _registry_fn("fetch_storm_events_db")
        result = fn(year=year, state=state)
    except Exception:
        if pipeline_emitter is not None and step_id is not None:
            await pipeline_emitter.mark_failed(
                step_id,
                "STORM_EVENTS_FETCH_FAILED",
                f"fetch_storm_events_db failed for {identifier!r}",
            )
        raise
    if pipeline_emitter is not None and step_id is not None:
        await pipeline_emitter.mark_complete(step_id)
    return {
        "layer_uri": result,
        "layer_name": getattr(result, "name", str(identifier)),
        "description": getattr(result, "name", str(identifier)),
    }


# --------------------------------------------------------------------------- #
# Text extraction per source-type (feeds the claim aggregator)
# --------------------------------------------------------------------------- #


def _extract_text_for_aggregator(
    source_type: str, fetched: dict[str, Any]
) -> tuple[str, str | None, str | None]:
    """Return ``(text, title, final_url)`` for the claim aggregator.

    The aggregator expects ``[{"url", "text", "fetched_at"}, ...]`` triples.
    Per-source-type extraction:

    - ``url`` — ``web_fetch`` returns ``content`` (main_text) + ``title``
      + ``url`` (final after redirects). Concatenate title + content so the
      aggregator's location/date regex sees the headline (often where the
      strongest location mention lives).
    - ``nws_alert`` — the LayerURI's ``name`` field carries the area label
      (e.g. ``"NWS Active Alerts — State FL"``); a richer extraction would
      open the FlatGeobuf and read each feature's ``description``/``headline``
      properties, but per the kickoff scope we keep this MVP and surface the
      layer-name as the text body (OQ-0119-NWS-DESCRIPTION-EXTRACTION).
    - ``storm_event`` — same shape as ``nws_alert``; the FGB carries
      ``EPISODE_NARRATIVE`` per feature, surfaced as a layer-name fallback
      for v0.1 (OQ-0119-STORM-NARRATIVE-EXTRACTION).
    """
    if source_type == "url":
        title = fetched.get("title")
        content = fetched.get("content")
        if content is None:
            text = title or ""
        elif isinstance(content, str):
            text = f"{title}\n{content}" if title else content
        else:
            text = str(content)
        final_url = fetched.get("url")
        return text, title, final_url
    # nws_alert / storm_event — layer-name fallback. The LayerURI's ``name``
    # is typically descriptive enough to surface a location mention to the
    # aggregator's regex.
    description = fetched.get("description") or fetched.get("layer_name") or ""
    return description, fetched.get("layer_name"), None


# --------------------------------------------------------------------------- #
# Presentation-text composition (deterministic format strings)
# --------------------------------------------------------------------------- #


def _format_param_value(target: str, value: Any) -> str:
    """Format one derived-param value for the presentation_text."""
    if value is None:
        return "unknown"
    if target == "scale" and isinstance(value, dict):
        v = value.get("value")
        unit = value.get("unit")
        if v is not None and unit:
            return f"{v:g} {unit}"
        return str(value)
    return str(value)


def _compose_presentation_text(
    event_type: str,
    derived_params: dict[str, DerivedEventParam],
    bbox: tuple[float, float, float, float] | None,
    n_sources: int,
) -> str:
    """Build the human-readable summary the web UI renders.

    Deterministic format string keyed off the derived-params + bbox — never
    invokes an LLM. The shape is fixed so tests can assert exact substrings.
    """
    lines: list[str] = []
    lines.append(f"Event ingest summary — {event_type}")
    for target in ("location", "date", "scale", "contaminant", "casualties"):
        param = derived_params.get(target)
        if param is None:
            continue
        value_str = _format_param_value(target, param.value)
        conf = f"{param.confidence:.2f}"
        n_supp = len(param.supporting_sources)
        lines.append(
            f"  - {target}: {value_str} "
            f"(confidence {conf}; {n_supp} source{'s' if n_supp != 1 else ''})"
        )
    if bbox is not None:
        lines.append(
            f"Resolved bbox: ({bbox[0]:.4f}, {bbox[1]:.4f}, "
            f"{bbox[2]:.4f}, {bbox[3]:.4f}) EPSG:4326"
        )
    else:
        lines.append("Resolved bbox: (unresolved)")
    lines.append(f"Sources consulted: {n_sources}")
    lines.append("STOP — review derived parameters before downstream modeling.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Input validation
# --------------------------------------------------------------------------- #


def _validate_sources(sources: Any) -> list[dict[str, Any]]:
    """Validate ``sources`` is ``[{"type": ..., "identifier": ...}, ...]``."""
    if not isinstance(sources, list):
        raise EventIngestInputError(
            f"sources must be a list of dicts; got {type(sources).__name__}"
        )
    if not sources:
        raise EventIngestInputError(
            "sources list must contain at least one source"
        )
    for i, item in enumerate(sources):
        if not isinstance(item, dict):
            raise EventIngestInputError(
                f"sources[{i}] must be a dict; got {type(item).__name__}"
            )
        # job-0295: the LLM naturally emits richer, type-specific source dicts
        # (e.g. storm_event as ``{type, year, state, event_types}``, url as
        # ``{type, url}``) rather than the canonical ``{type, identifier}``.
        # Synthesize ``identifier`` from those keys so the natural shape works
        # without a re-prompt. Identifier semantics (see _fetch_*_source):
        #   url         → the URL string
        #   nws_alert   → 2-letter state code or 5-digit county FIPS
        #   storm_event → "YYYY" or "YYYY:STATE"
        s_type = item.get("type")
        if s_type == "url":
            # _fetch_url_source uses ``identifier`` AS the URL to fetch. The LLM
            # sometimes supplies a real URL under ``url`` but a non-URL label
            # under ``identifier`` (e.g. "nws-api-tx-alerts"). Prefer the real
            # URL so the fetch targets a valid scheme.
            url_val = item.get("url") or item.get("link") or item.get("href")
            ident = item.get("identifier")
            ident_is_url = isinstance(ident, str) and ident.lower().startswith(
                ("http://", "https://")
            )
            if url_val and not ident_is_url:
                item["identifier"] = str(url_val)
        elif "identifier" not in item and isinstance(s_type, str):
            if s_type == "nws_alert":
                synth = item.get("area") or item.get("state") or item.get("fips")
            elif s_type == "storm_event":
                year = item.get("year")
                state = item.get("state")
                if year is not None:
                    synth = f"{year}:{state}" if state else f"{year}"
                else:
                    synth = state
            else:
                synth = None
            if synth is not None:
                item["identifier"] = str(synth)
        for key in ("type", "identifier"):
            if key not in item:
                raise EventIngestInputError(
                    f"sources[{i}] missing required key {key!r}; "
                    f"got keys {list(item.keys())}"
                )
        if item["type"] not in SUPPORTED_SOURCE_TYPES:
            raise EventIngestInputError(
                f"sources[{i}].type={item['type']!r} not in {SUPPORTED_SOURCE_TYPES}"
            )
        if not isinstance(item["identifier"], str) or not item["identifier"].strip():
            raise EventIngestInputError(
                f"sources[{i}].identifier must be a non-empty string; "
                f"got {item['identifier']!r}"
            )
    return sources


def _validate_event_type(event_type: Any) -> str:
    if not isinstance(event_type, str):
        raise EventIngestInputError(
            f"target_event_type must be a string; got {type(event_type).__name__}"
        )
    if event_type not in SUPPORTED_EVENT_TYPES:
        raise EventIngestInputError(
            f"target_event_type={event_type!r} not in {SUPPORTED_EVENT_TYPES}"
        )
    return event_type


# --------------------------------------------------------------------------- #
# The workflow itself
# --------------------------------------------------------------------------- #


async def model_news_event_ingest(
    sources: list[dict[str, Any]],
    target_event_type: str = "spill",
    *,
    pipeline_emitter: "PipelineEmitter | None" = None,
) -> EventIngestResult:
    """Compose news/alert ingest → claim aggregation → derived event params.

    This is the Case 2 partial composer. It STOPS BEFORE any solver dispatch
    (sprint-13 picks up with MODFLOW). The returned envelope is the review
    substrate the user approves before any downstream modeling.

    Args:
        sources: list of dicts each ``{"type": "url"|"nws_alert"|"storm_event",
            "identifier": str}``. At least one source is required.
        target_event_type: hazard label routing the claim_targets list.
            One of ``"spill"``, ``"flood"``, ``"wildfire"``, ``"hurricane"``.
        pipeline_emitter: optional PipelineEmitter for live progress.
            When ``None`` (smoke / direct-call), the workflow runs silently.

    Returns:
        ``EventIngestResult`` carrying derived params (each with confidence),
        per-source provenance, resolved bbox (if any), and a deterministic
        presentation_text the web UI displays for user review.

    Raises:
        ``EventIngestInputError`` — bad sources or unsupported event type.
        ``EventIngestError`` — required atomic tool missing from registry.
        Propagates ``asyncio.CancelledError`` from any await point (Invariant 8).
    """
    sources = _validate_sources(sources)
    event_type = _validate_event_type(target_event_type)

    logger.info(
        "model_news_event_ingest start sources=%d target_event_type=%s",
        len(sources),
        event_type,
    )

    # --- Stage 1: dispatch per-source fetches ---
    fetched_sources: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for src in sources:
        s_type = src["type"]
        identifier = src["identifier"]
        if s_type == "url":
            fetched = await _fetch_url_source(identifier, pipeline_emitter)
        elif s_type == "nws_alert":
            fetched = await _fetch_nws_alert_source(identifier, pipeline_emitter)
        elif s_type == "storm_event":
            fetched = await _fetch_storm_event_source(identifier, pipeline_emitter)
        else:  # pragma: no cover — _validate_sources filters this
            raise EventIngestInputError(f"unknown source type {s_type!r}")
        fetched_sources.append((src, fetched))

    # --- Stage 2: aggregate claims across sources ---
    aggregator_fn = _registry_fn("aggregate_claims_across_sources")
    claim_targets = _claim_targets_for_event_type(event_type)

    if pipeline_emitter is not None:
        agg_step_id = await pipeline_emitter.add_step(
            name="Aggregate claims across sources",
            tool_name="aggregate_claims_across_sources",
        )
        await pipeline_emitter.mark_running(agg_step_id)
    else:
        agg_step_id = None

    aggregator_input: list[dict[str, Any]] = []
    extracted_per_source: list[tuple[str, str | None, str | None]] = []
    fetched_at_default = "1970-01-01T00:00:00Z"
    for src, fetched in fetched_sources:
        text, title, final_url = _extract_text_for_aggregator(src["type"], fetched)
        fetched_at = fetched.get("fetched_at") if isinstance(fetched, dict) else None
        url_for_agg = final_url or src["identifier"]
        aggregator_input.append(
            {
                "url": url_for_agg,
                "text": text or "",
                "fetched_at": fetched_at or fetched_at_default,
            }
        )
        extracted_per_source.append((text or "", title, final_url))

    try:
        agg_result = aggregator_fn(
            sources=aggregator_input, claim_targets=claim_targets
        )
    except Exception:
        if pipeline_emitter is not None and agg_step_id is not None:
            await pipeline_emitter.mark_failed(
                agg_step_id,
                "CLAIM_AGG_FAILED",
                "aggregate_claims_across_sources raised",
            )
        raise

    if pipeline_emitter is not None and agg_step_id is not None:
        await pipeline_emitter.mark_complete(agg_step_id)

    # Promote each per-target claim dict into a typed DerivedEventParam.
    raw_claims = agg_result.get("claims", {}) if isinstance(agg_result, dict) else {}
    derived_params: dict[str, DerivedEventParam] = {}
    for target, claim in raw_claims.items():
        if not isinstance(claim, dict):
            continue
        derived_params[target] = DerivedEventParam(
            value=claim.get("value"),
            confidence=float(claim.get("confidence", 0.0)),
            supporting_sources=list(claim.get("supporting_sources", []) or []),
            alternatives=list(claim.get("alternatives", []) or []),
            below_threshold=bool(claim.get("below_threshold", False)),
        )

    # --- Stage 3: geocode the derived location → bbox ---
    bbox: tuple[float, float, float, float] | None = None
    location_param = derived_params.get("location")
    if location_param is not None and location_param.value:
        location_query = str(location_param.value)
        if pipeline_emitter is not None:
            geo_step_id = await pipeline_emitter.add_step(
                name=f"Geocode: {location_query}", tool_name="geocode_location"
            )
            await pipeline_emitter.mark_running(geo_step_id)
        else:
            geo_step_id = None
        try:
            geocode_fn = _registry_fn("geocode_location")
            geo_result = geocode_fn(location_query)
        except Exception as exc:  # noqa: BLE001 — geocode is best-effort
            logger.warning(
                "geocode_location(%r) failed (%s); bbox remains None",
                location_query,
                exc,
            )
            if pipeline_emitter is not None and geo_step_id is not None:
                await pipeline_emitter.mark_failed(
                    geo_step_id,
                    "GEOCODE_FAILED",
                    f"geocode_location failed for {location_query!r}",
                )
        else:
            bb = geo_result.get("bbox") if isinstance(geo_result, dict) else None
            if bb and len(bb) == 4:
                bbox = (
                    float(bb[0]),
                    float(bb[1]),
                    float(bb[2]),
                    float(bb[3]),
                )
            if pipeline_emitter is not None and geo_step_id is not None:
                await pipeline_emitter.mark_complete(geo_step_id)

    # --- Stage 4: build per-source provenance ---
    provenance: list[EventIngestProvenance] = []
    for (src, fetched), (text, title, final_url) in zip(
        fetched_sources, extracted_per_source, strict=True
    ):
        snippet: str | None = None
        if text:
            snippet = text[:280] + ("..." if len(text) > 280 else "")
        provenance.append(
            EventIngestProvenance(
                source_type=src["type"],
                identifier=src["identifier"],
                final_url=final_url,
                title=title,
                citation_snippet=snippet,
                source_authority_tier=_source_authority_tier(
                    src["type"], final_url
                ),
                fetched_at=fetched.get("fetched_at") if isinstance(fetched, dict) else None,
            )
        )

    # --- Stage 5: compose presentation_text + assemble result ---
    presentation_text = _compose_presentation_text(
        event_type=event_type,
        derived_params=derived_params,
        bbox=bbox,
        n_sources=len(sources),
    )

    result = EventIngestResult(
        event_type=event_type,
        derived_params=derived_params,
        provenance=provenance,
        bbox=bbox,
        presentation_text=presentation_text,
    )
    logger.info(
        "model_news_event_ingest done event_type=%s targets_resolved=%d bbox=%s",
        event_type,
        sum(1 for p in derived_params.values() if p.value is not None),
        bbox,
    )
    return result


# --------------------------------------------------------------------------- #
# LLM-exposed thin atomic-tool wrapper (workflow_dispatch source class)
# --------------------------------------------------------------------------- #


_RUN_MODEL_NEWS_EVENT_INGEST_METADATA = AtomicToolMetadata(
    name="run_model_news_event_ingest",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
)


@register_tool(_RUN_MODEL_NEWS_EVENT_INGEST_METADATA)
async def run_model_news_event_ingest(
    sources: list[dict[str, Any]],
    target_event_type: str = "spill",
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Ingest news / alert sources and derive event parameters for user review.

    Three-step composition chain (all deterministic Python, zero LLM calls):
    1. Per-source fetch dispatch:
       - "url" sources → ``web_fetch(url, extract="main_text")``
       - "nws_alert" sources → ``fetch_nws_event(area=identifier)``
       - "storm_event" sources → ``fetch_storm_events_db(year, state)``
    2. ``aggregate_claims_across_sources(sources, claim_targets)`` — derives
       best-supported values for location, scale, contaminant, date,
       casualties with source-agreement confidence scoring.
    3. ``geocode_location(derived_location_value)`` — converts the aggregated
       location claim to a bbox for the review modal.
    Returns an ``EventIngestResult`` with typed ``derived_params`` +
    ``provenance`` + deterministic ``presentation_text``. STOPS here —
    review-gated by design (Invariant 9); downstream solvers run only after
    user approval.

    When to use:
        - Case 2 intent: agent has one or more sources about a real-world
          event (news article URLs, NWS alert area codes, Storm Events DB
          identifiers) and needs derived event parameters for user review
          before any solver dispatches.
        - Any prompt mentioning "incident report", "news article about", "NWS
          alert", "storm event database" followed by a decision to model it.

    When NOT to use:
        - Dispatching a downstream solver (this workflow stops before any
          solver — review-gated by Invariant 9 design).
        - Flood modeling without news/alert sources (use ``run_model_flood_scenario``
          directly with bbox / location_query).
        - Summarizing a single page (use ``web_fetch`` directly).

    Params:
        sources: list of dicts each ``{"type": "url" | "nws_alert" |
            "storm_event", "identifier": "<URL or area code or year[:state]>"}``.
            At least one source is required.
        target_event_type: one of ``"spill"`` / ``"flood"`` / ``"wildfire"`` /
            ``"hurricane"`` — routes the claim-target list used by the
            aggregator. Default ``"spill"``.

    Returns:
        The ``EventIngestResult`` serialized as a JSON-compatible dict
        (``model_dump(mode="json")``) so the LLM tool surface can narrate it
        directly. Carries ``event_type``, ``derived_params`` (each with
        ``value`` / ``confidence`` / ``supporting_sources`` / ``alternatives``),
        ``provenance`` (per-source citation entries), ``bbox`` (or ``None``),
        and ``presentation_text`` (the deterministic summary the web UI
        renders for review).

    Side effects:
        - Per-source atomic tools (``web_fetch``, ``fetch_nws_event``,
          ``fetch_storm_events_db``) cache their results in GCS per their
          own TTL classes (``dynamic-1h`` for the first; ``dynamic-1h`` /
          ``static-30d`` for the latter two). This wrapper itself is
          ``cacheable=False`` — the workflow's value is the dispatch +
          aggregation, never the cached return.

    FR-DC-6: This wrapper declares ``cacheable=False`` +
    ``ttl_class="live-no-cache"`` + ``source_class="workflow_dispatch"`` —
    the same shape as job-0042's ``run_model_flood_scenario``.

    Cross-tool dependencies:
        Upstream (step chain):
        - ``web_fetch(url)`` — called for each "url" source in ``sources``.
        - ``fetch_nws_event(area)`` — called for each "nws_alert" source.
        - ``fetch_storm_events_db(year, state)`` — called for each
          "storm_event" source.
        - ``aggregate_claims_across_sources(sources, claim_targets)`` — step 2;
          derives typed event parameter values with confidence scores.
        - ``geocode_location(location_value)`` — step 3; converts location
          claim to a bbox.
        Downstream (feeds):
        - Sprint-13 MODFLOW / spill-plume solver — the returned
          ``EventIngestResult`` is the review envelope the user approves;
          the approved derived params (location bbox, scale, contaminant)
          become inputs to the next solver dispatch.
    """
    result = await model_news_event_ingest(
        sources=sources,
        target_event_type=target_event_type,
        pipeline_emitter=None,
    )
    return result.model_dump(mode="json")
