"""Tests for ``model_news_event_ingest`` workflow (job-0119, Case 2 composer).

Coverage (audit-mandated ≥6 unit + 1 live):

1. ``test_workflow_aggregates_mixed_source_types`` — 3 sources of mixed
   types (url + nws_alert + storm_event) all dispatch + aggregator sees
   merged text → derived params.
2. ``test_single_url_source_workflow`` — single URL source handles cleanly
   and produces a typed envelope.
3. ``test_target_event_type_routes_claim_targets`` —
   ``target_event_type="hurricane"`` does NOT include ``contaminant`` in
   claim_targets (spill-only); ``"spill"`` does.
4. ``test_provenance_includes_all_source_urls`` — every source contributes
   one ``EventIngestProvenance`` entry with identifier + final_url +
   citation snippet.
5. ``test_empty_sources_raises_typed_input_error`` — ``sources=[]`` raises
   ``EventIngestInputError``.
6. ``test_invalid_event_type_raises`` — ``target_event_type="lightning"``
   raises ``EventIngestInputError``.
7. ``test_workflow_registers_run_model_news_event_ingest_wrapper`` — the
   LLM-facing wrapper is in ``TOOL_REGISTRY`` with workflow_dispatch
   metadata.
8. ``test_geocode_is_fed_exact_derived_location`` — geographic-correctness
   gate (job-0086): the geocode call receives the EXACT location string
   the aggregator surfaced, not a re-derived approximation.
9. ``test_workflow_returns_event_ingest_result_serializable`` —
   ``EventIngestResult.model_dump(mode="json")`` round-trips cleanly.
10. ``test_workflow_stops_before_solver_dispatch`` — no solver / MODFLOW /
    run_solver / wait_for_completion call is dispatched anywhere in the
    workflow body (Invariant 9).
11. ``test_live_workflow_against_real_news_url`` — env-gated live test.
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.workflows.model_news_event_ingest import (
    EventIngestInputError,
    SUPPORTED_EVENT_TYPES,
    _claim_targets_for_event_type,
    _compose_presentation_text,
    _source_authority_tier,
    _validate_sources,
    model_news_event_ingest,
    run_model_news_event_ingest,
)
from trid3nt_contracts.case_results import (
    DerivedEventParam,
    EventIngestResult,
)


# --------------------------------------------------------------------------- #
# Fixtures + helpers
# --------------------------------------------------------------------------- #


def _make_fake_layer_uri(name: str) -> Any:
    """Build an object with the LayerURI attributes the workflow reads."""
    obj = MagicMock()
    obj.name = name
    obj.uri = f"s3://trid3nt-cache/cache/dynamic-1h/fake/{name}.fgb"
    return obj


def _fake_web_fetch_result(
    url: str = "https://news.example.com/longview-spill-2024",
    title: str = "Train derailment near Longview, Texas spills vinyl chloride",
    content: str = (
        "On February 15, 2026 a freight train derailed near Longview, Texas, "
        "spilling approximately 15,000 gallons of vinyl chloride. Three people "
        "were injured. The incident took place on 2026-02-15."
    ),
) -> dict[str, Any]:
    return {
        "url": url,
        "status_code": 200,
        "fetched_at": "2026-06-08T00:00:00Z",
        "extract_mode": "main_text",
        "content": content,
        "title": title,
        "lang": "en",
        "content_length": len(content),
    }


def _fake_geocode_result(
    name: str = "Longview, Texas",
    bbox: tuple[float, float, float, float] = (-94.85, 32.40, -94.60, 32.60),
) -> dict[str, Any]:
    return {
        "name": name,
        "bbox": list(bbox),
        "latitude": (bbox[1] + bbox[3]) / 2,
        "longitude": (bbox[0] + bbox[2]) / 2,
        "source": "nominatim",
        "osm_type": "city",
        "osm_id": "12345",
        "place_id": "67890",
    }


# --------------------------------------------------------------------------- #
# Test 1 — mixed source-type ingest
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_workflow_aggregates_mixed_source_types() -> None:
    """3 sources (url + nws_alert + storm_event) all dispatch + aggregate."""
    sources = [
        {"type": "url", "identifier": "https://news.example.com/longview"},
        {"type": "nws_alert", "identifier": "TX"},
        {"type": "storm_event", "identifier": "2024:TX"},
    ]

    with (
        patch.dict(
            TOOL_REGISTRY,
            {},
            clear=False,
        ),
        patch(
            "trid3nt_server.workflows.model_news_event_ingest._registry_fn"
        ) as mock_registry_fn,
    ):
        # Build a dispatcher that returns the right fake per tool name.
        def _registry_dispatch(name: str):
            if name == "web_fetch":
                return MagicMock(return_value=_fake_web_fetch_result())
            if name == "fetch_nws_event":
                return MagicMock(
                    return_value=_make_fake_layer_uri(
                        "NWS Active Alerts — State TX, Longview"
                    )
                )
            if name == "fetch_storm_events_db":
                return MagicMock(
                    return_value=_make_fake_layer_uri(
                        "NOAA Storm Events 2024 — TX, Longview, Texas events"
                    )
                )
            if name == "aggregate_claims_across_sources":
                return MagicMock(
                    return_value={
                        "claims": {
                            "location": {
                                "value": "Longview, Texas",
                                "confidence": 0.85,
                                "supporting_sources": [
                                    "https://news.example.com/longview",
                                    "TX",
                                    "2024:TX",
                                ],
                                "alternatives": [],
                            },
                            "date": {
                                "value": "2026-02-15",
                                "confidence": 0.5,
                                "supporting_sources": [
                                    "https://news.example.com/longview"
                                ],
                                "alternatives": [],
                            },
                            "scale": {
                                "value": {"value": 15000.0, "unit": "gallon"},
                                "confidence": 0.5,
                                "supporting_sources": [
                                    "https://news.example.com/longview"
                                ],
                                "alternatives": [],
                            },
                            "contaminant": {
                                "value": "vinyl chloride",
                                "confidence": 0.5,
                                "supporting_sources": [
                                    "https://news.example.com/longview"
                                ],
                                "alternatives": [],
                            },
                            "casualties": {
                                "value": 3,
                                "confidence": 0.5,
                                "supporting_sources": [
                                    "https://news.example.com/longview"
                                ],
                                "alternatives": [],
                            },
                        },
                        "stats": {
                            "sources_consulted": 3,
                            "claims_resolved": 5,
                            "confidence_threshold": 0.6,
                        },
                    }
                )
            if name == "geocode_location":
                return MagicMock(return_value=_fake_geocode_result())
            raise AssertionError(f"unexpected registry tool {name!r}")

        mock_registry_fn.side_effect = _registry_dispatch

        result = await model_news_event_ingest(
            sources=sources, target_event_type="spill"
        )

    assert isinstance(result, EventIngestResult)
    assert result.event_type == "spill"
    # All 5 spill targets resolved.
    assert set(result.derived_params.keys()) == {
        "location", "date", "scale", "contaminant", "casualties"
    }
    # Location was extracted + carries confidence.
    location = result.derived_params["location"]
    assert location.value == "Longview, Texas"
    assert location.confidence == pytest.approx(0.85)
    # bbox resolved from geocode call.
    assert result.bbox is not None
    assert result.bbox[0] == pytest.approx(-94.85)
    # 3 provenance entries (one per source).
    assert len(result.provenance) == 3
    types_seen = {p.source_type for p in result.provenance}
    assert types_seen == {"url", "nws_alert", "storm_event"}


# --------------------------------------------------------------------------- #
# Test 2 — single URL source
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_single_url_source_workflow() -> None:
    """Single URL → claims aggregated; presentation_text built."""
    sources = [{"type": "url", "identifier": "https://news.example.com/spill"}]

    with patch(
        "trid3nt_server.workflows.model_news_event_ingest._registry_fn"
    ) as mock_registry_fn:

        def _dispatch(name: str):
            if name == "web_fetch":
                return MagicMock(return_value=_fake_web_fetch_result())
            if name == "aggregate_claims_across_sources":
                return MagicMock(
                    return_value={
                        "claims": {
                            "location": {
                                "value": "Longview, Texas",
                                "confidence": 0.5,
                                "supporting_sources": [
                                    "https://news.example.com/longview-spill-2024"
                                ],
                                "alternatives": [],
                            },
                        },
                        "stats": {
                            "sources_consulted": 1,
                            "claims_resolved": 1,
                            "confidence_threshold": 0.6,
                        },
                    }
                )
            if name == "geocode_location":
                return MagicMock(return_value=_fake_geocode_result())
            raise AssertionError(f"unexpected tool {name!r}")

        mock_registry_fn.side_effect = _dispatch
        result = await model_news_event_ingest(
            sources=sources, target_event_type="spill"
        )

    assert isinstance(result, EventIngestResult)
    assert len(result.provenance) == 1
    assert result.provenance[0].source_type == "url"
    assert "Event ingest summary — spill" in result.presentation_text


# --------------------------------------------------------------------------- #
# Test 3 — target_event_type routes claim_targets
# --------------------------------------------------------------------------- #


def test_target_event_type_routes_claim_targets() -> None:
    """hurricane / flood / wildfire skip ``contaminant``; spill includes it."""
    assert "contaminant" in _claim_targets_for_event_type("spill")
    for et in ("flood", "wildfire", "hurricane"):
        assert "contaminant" not in _claim_targets_for_event_type(et), et
        # Core targets always present.
        for target in ("location", "date", "scale", "casualties"):
            assert target in _claim_targets_for_event_type(et), (et, target)


# --------------------------------------------------------------------------- #
# Test 4 — provenance includes all source URLs
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_provenance_includes_all_source_urls() -> None:
    """Per-source provenance carries identifier + (when url) final URL."""
    sources = [
        {"type": "url", "identifier": "https://news.example.com/a"},
        {"type": "url", "identifier": "https://news.example.com/b"},
    ]
    with patch(
        "trid3nt_server.workflows.model_news_event_ingest._registry_fn"
    ) as mock_registry_fn:

        def _dispatch(name: str):
            if name == "web_fetch":
                return MagicMock(
                    side_effect=lambda url, extract: _fake_web_fetch_result(
                        url=url
                    )
                )
            if name == "aggregate_claims_across_sources":
                return MagicMock(
                    return_value={
                        "claims": {
                            "location": {
                                "value": "Longview, Texas",
                                "confidence": 0.8,
                                "supporting_sources": [
                                    "https://news.example.com/longview-spill-2024",
                                ],
                                "alternatives": [],
                            }
                        },
                        "stats": {
                            "sources_consulted": 2,
                            "claims_resolved": 1,
                            "confidence_threshold": 0.6,
                        },
                    }
                )
            if name == "geocode_location":
                return MagicMock(return_value=_fake_geocode_result())
            raise AssertionError(f"unexpected tool {name!r}")

        mock_registry_fn.side_effect = _dispatch
        result = await model_news_event_ingest(
            sources=sources, target_event_type="spill"
        )

    assert len(result.provenance) == 2
    identifiers = {p.identifier for p in result.provenance}
    assert identifiers == {
        "https://news.example.com/a",
        "https://news.example.com/b",
    }
    # Every entry has a citation snippet (>= title characters).
    for entry in result.provenance:
        assert entry.citation_snippet is not None
        assert len(entry.citation_snippet) > 0
        # web_fetch sources get a tier-2 classification (non-.gov).
        assert entry.source_authority_tier == 2


# --------------------------------------------------------------------------- #
# Test 5 — empty sources raises typed input error
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_empty_sources_raises_typed_input_error() -> None:
    """Empty sources list → ``EventIngestInputError`` with retryable=False."""
    with pytest.raises(EventIngestInputError) as excinfo:
        await model_news_event_ingest(sources=[], target_event_type="spill")
    assert excinfo.value.error_code == "EVENT_INGEST_INPUT_INVALID"
    assert excinfo.value.retryable is False


# --------------------------------------------------------------------------- #
# Test 6 — invalid event type raises
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_invalid_event_type_raises() -> None:
    """target_event_type outside SUPPORTED_EVENT_TYPES raises."""
    sources = [{"type": "url", "identifier": "https://example.com/a"}]
    with pytest.raises(EventIngestInputError) as excinfo:
        await model_news_event_ingest(
            sources=sources, target_event_type="lightning"
        )
    assert "lightning" in str(excinfo.value)
    assert all(et in str(excinfo.value) for et in SUPPORTED_EVENT_TYPES)


# --------------------------------------------------------------------------- #
# job-0295 — identifier synthesis from natural LLM source-dict shapes
# --------------------------------------------------------------------------- #


def test_identifier_synthesized_for_storm_event_year_state() -> None:
    """LLM emits ``{type, year, state, ...}`` (no identifier) → synthesize
    ``"YYYY:STATE"`` so the natural shape validates without a re-prompt."""
    out = _validate_sources(
        [{"type": "storm_event", "year": 2025, "state": "TX",
          "event_types": ["Flash Flood"], "description": "TX 2025 floods"}]
    )
    assert out[0]["identifier"] == "2025:TX"


def test_identifier_synthesized_for_storm_event_year_only() -> None:
    out = _validate_sources([{"type": "storm_event", "year": 2025}])
    assert out[0]["identifier"] == "2025"


def test_identifier_synthesized_for_url_source() -> None:
    out = _validate_sources(
        [{"type": "url", "url": "https://example.com/a", "extract": "main_text"}]
    )
    assert out[0]["identifier"] == "https://example.com/a"


def test_identifier_synthesized_for_nws_alert_state() -> None:
    out = _validate_sources([{"type": "nws_alert", "state": "FL"}])
    assert out[0]["identifier"] == "FL"


def test_url_source_prefers_real_url_over_label_identifier() -> None:
    # job-0295: the url fetch path uses ``identifier`` AS the URL. When the LLM
    # supplies a real ``url`` but a non-URL ``identifier`` label, prefer the
    # real URL so the fetch has a valid scheme.
    out = _validate_sources(
        [{"type": "url", "url": "https://api.weather.gov/alerts/active?area=TX",
          "identifier": "nws-api-tx-flood-alerts"}]
    )
    assert out[0]["identifier"] == "https://api.weather.gov/alerts/active?area=TX"


def test_url_source_keeps_url_identifier_when_already_a_url() -> None:
    out = _validate_sources(
        [{"type": "url", "identifier": "https://example.com/article"}]
    )
    assert out[0]["identifier"] == "https://example.com/article"


def test_explicit_identifier_is_preserved() -> None:
    out = _validate_sources(
        [{"type": "storm_event", "identifier": "2022:CA", "year": 9999}]
    )
    assert out[0]["identifier"] == "2022:CA"


def test_unsynthesizable_source_still_raises() -> None:
    # storm_event with neither identifier nor year/state can't be synthesized.
    with pytest.raises(EventIngestInputError, match="missing required key 'identifier'"):
        _validate_sources([{"type": "storm_event", "description": "vague"}])


# --------------------------------------------------------------------------- #
# Test 7 — registry registration of wrapper
# --------------------------------------------------------------------------- #


def test_workflow_registers_run_model_news_event_ingest_wrapper() -> None:
    """The LLM-facing wrapper is in TOOL_REGISTRY with workflow_dispatch metadata."""
    assert "run_model_news_event_ingest" in TOOL_REGISTRY, (
        f"workflow wrapper not in TOOL_REGISTRY; keys include "
        f"{sorted(TOOL_REGISTRY)[:20]}"
    )
    entry = TOOL_REGISTRY["run_model_news_event_ingest"]
    assert entry.metadata.cacheable is False
    assert entry.metadata.ttl_class == "live-no-cache"
    assert entry.metadata.source_class == "workflow_dispatch"
    assert entry.fn is run_model_news_event_ingest


# --------------------------------------------------------------------------- #
# Test 8 — geographic-correctness gate
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_geocode_is_fed_exact_derived_location() -> None:
    """Geographic-correctness gate: geocode receives the EXACT location string.

    Per the codified job-0086 lesson, when a workflow emits geometry it must
    verify against the actual geography. Here the geometry seam is the
    geocode call — assert it receives the exact string the aggregator
    surfaced (algebraic identity, not just round-trip).
    """
    sources = [{"type": "url", "identifier": "https://example.com/a"}]
    expected_location = "Palestine, Ohio"
    geocode_call_args: list[Any] = []

    with patch(
        "trid3nt_server.workflows.model_news_event_ingest._registry_fn"
    ) as mock_registry_fn:

        def _capture_geocode(*args, **kwargs):
            geocode_call_args.append((args, kwargs))
            return _fake_geocode_result(
                name=expected_location,
                bbox=(-84.80, 40.85, -84.65, 40.95),
            )

        def _dispatch(name: str):
            if name == "web_fetch":
                return MagicMock(return_value=_fake_web_fetch_result())
            if name == "aggregate_claims_across_sources":
                return MagicMock(
                    return_value={
                        "claims": {
                            "location": {
                                "value": expected_location,
                                "confidence": 0.5,
                                "supporting_sources": ["https://example.com/a"],
                                "alternatives": [],
                            }
                        },
                        "stats": {
                            "sources_consulted": 1,
                            "claims_resolved": 1,
                            "confidence_threshold": 0.6,
                        },
                    }
                )
            if name == "geocode_location":
                return _capture_geocode
            raise AssertionError(f"unexpected tool {name!r}")

        mock_registry_fn.side_effect = _dispatch
        result = await model_news_event_ingest(
            sources=sources, target_event_type="spill"
        )

    assert len(geocode_call_args) == 1
    args, kwargs = geocode_call_args[0]
    # First positional arg is the location string — must match EXACTLY.
    assert args[0] == expected_location, (
        f"geocode call received {args[0]!r}; "
        f"derived location was {expected_location!r}"
    )
    # bbox plumbed through verbatim.
    assert result.bbox == (-84.80, 40.85, -84.65, 40.95)


# --------------------------------------------------------------------------- #
# Test 9 — EventIngestResult round-trips through pydantic JSON dump
# --------------------------------------------------------------------------- #


def test_event_ingest_result_round_trips_via_model_dump() -> None:
    """The result envelope serializes + reconstructs cleanly."""
    presentation = _compose_presentation_text(
        event_type="spill",
        derived_params={
            "location": DerivedEventParam(
                value="Longview, Texas",
                confidence=0.85,
                supporting_sources=["https://a", "https://b"],
            ),
            "scale": DerivedEventParam(
                value={"value": 15000.0, "unit": "gallon"},
                confidence=0.5,
                supporting_sources=["https://a"],
            ),
        },
        bbox=(-94.85, 32.40, -94.60, 32.60),
        n_sources=2,
    )
    envelope = EventIngestResult(
        event_type="spill",
        derived_params={
            "location": DerivedEventParam(
                value="Longview, Texas",
                confidence=0.85,
                supporting_sources=["https://a", "https://b"],
            ),
        },
        provenance=[],
        bbox=(-94.85, 32.40, -94.60, 32.60),
        presentation_text=presentation,
    )
    dumped = envelope.model_dump(mode="json")
    assert dumped["event_type"] == "spill"
    assert "Longview, Texas" in dumped["presentation_text"]
    # Round trip.
    rebuilt = EventIngestResult.model_validate(dumped)
    assert rebuilt.event_type == envelope.event_type
    assert rebuilt.derived_params["location"].value == "Longview, Texas"
    assert rebuilt.bbox == envelope.bbox


# --------------------------------------------------------------------------- #
# Test 10 — workflow STOPS before solver dispatch
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_workflow_stops_before_solver_dispatch() -> None:
    """The workflow never dispatches run_solver / wait_for_completion /
    a downstream MODFLOW or SFINCS tool. Invariant 9: confirmation before
    consequence — the result envelope IS the confirmation substrate."""
    sources = [{"type": "url", "identifier": "https://example.com/a"}]
    dispatched_tools: list[str] = []

    with patch(
        "trid3nt_server.workflows.model_news_event_ingest._registry_fn"
    ) as mock_registry_fn:

        def _dispatch(name: str):
            dispatched_tools.append(name)
            if name == "web_fetch":
                return MagicMock(return_value=_fake_web_fetch_result())
            if name == "aggregate_claims_across_sources":
                return MagicMock(
                    return_value={
                        "claims": {
                            "location": {
                                "value": "Longview, Texas",
                                "confidence": 0.5,
                                "supporting_sources": ["https://example.com/a"],
                                "alternatives": [],
                            }
                        },
                        "stats": {
                            "sources_consulted": 1,
                            "claims_resolved": 1,
                            "confidence_threshold": 0.6,
                        },
                    }
                )
            if name == "geocode_location":
                return MagicMock(return_value=_fake_geocode_result())
            raise AssertionError(f"unexpected tool {name!r}")

        mock_registry_fn.side_effect = _dispatch
        await model_news_event_ingest(
            sources=sources, target_event_type="spill"
        )

    forbidden = {
        "run_solver",
        "wait_for_completion",
        "run_model_flood_scenario",
        "run_model_flood_habitat_scenario",
        "run_modflow_scenario",
    }
    assert not (set(dispatched_tools) & forbidden), (
        f"workflow dispatched forbidden downstream solver tools: "
        f"{set(dispatched_tools) & forbidden}"
    )


# --------------------------------------------------------------------------- #
# Test 11 — source authority tier classification
# --------------------------------------------------------------------------- #


def test_source_authority_tier_classifies_gov_vs_news_vs_agency() -> None:
    """NWS/storm-event agency sources -> tier 1; .gov urls -> tier 1;
    .com news -> tier 2."""
    assert _source_authority_tier("nws_alert", None) == 1
    assert _source_authority_tier("storm_event", None) == 1
    assert _source_authority_tier("url", "https://www.weather.gov/spill") == 1
    assert _source_authority_tier("url", "https://news.example.com/spill") == 2
    assert _source_authority_tier("url", None) is None


# --------------------------------------------------------------------------- #
# Test 12 — presentation_text is deterministic + cites confidences
# --------------------------------------------------------------------------- #


def test_presentation_text_is_deterministic() -> None:
    """Two identical compositions produce identical text (no LLM, no randomness)."""
    params = {
        "location": DerivedEventParam(
            value="Longview, Texas",
            confidence=0.85,
            supporting_sources=["a", "b"],
        ),
        "scale": DerivedEventParam(
            value={"value": 15000.0, "unit": "gallon"},
            confidence=0.5,
            supporting_sources=["a"],
        ),
    }
    bbox = (-94.85, 32.40, -94.60, 32.60)
    a = _compose_presentation_text("spill", params, bbox, 2)
    b = _compose_presentation_text("spill", params, bbox, 2)
    assert a == b
    # Confidence + value cited.
    assert "Longview, Texas" in a
    assert "0.85" in a
    assert "15000" in a
    assert "gallon" in a
    assert "STOP" in a


# --------------------------------------------------------------------------- #
# Test 13 (LIVE) — env-gated: real news URL fixture
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(
    os.environ.get("TRID3NT_TEST_LIVE_CASE2") != "1",
    reason=(
        "Live Case 2 test gated by TRID3NT_TEST_LIVE_CASE2=1 (requires GCS "
        "cache substrate + outbound network)."
    ),
)
@pytest.mark.asyncio
async def test_live_workflow_against_real_news_url() -> None:
    """Live: fetch a real public news URL + produce derived params.

    Uses the BBC public-news front page as a guaranteed-reachable HTTPS
    article surface (won't always parse named-location claims, but the
    workflow MUST return a typed envelope without crashing).
    """
    sources = [
        {"type": "url", "identifier": "https://www.bbc.com/news"},
    ]
    result = await model_news_event_ingest(
        sources=sources, target_event_type="flood"
    )
    assert isinstance(result, EventIngestResult)
    assert result.event_type == "flood"
    assert len(result.provenance) == 1
    # presentation_text always produced.
    assert "Event ingest summary — flood" in result.presentation_text
    # The workflow STOPS — no downstream solver tools dispatched (verified
    # by Test 10's structural check; here we just verify the envelope is
    # the terminal output and doesn't contain solver_run_ids or similar
    # fields that would imply an aborted solver call).
    dumped = result.model_dump(mode="json")
    assert "solver_run_ids" not in dumped
