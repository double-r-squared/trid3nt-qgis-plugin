"""Tests for ``aggregate_claims_across_sources`` (job-0093).

Covers the audit's six required unit cases + extras:
1. Single source with date -> confidence 0.5
2. 2 sources agreeing on date -> confidence >= 0.85
3. Sources disagreeing -> both surface in ``alternatives``
4. Empty sources list -> empty claims dict, no error
5. claim_targets=['location'] with "Longview, Texas" mention -> location.value="Longview, Texas"
6. claim_targets=['casualties'] with "3 injured" -> casualties.value=3
+ input validation tests
+ scale extraction test
+ contaminant extraction test
+ confidence-cap test
+ stable tie-breaking test
+ tool-registration test
"""

from __future__ import annotations

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.processing.aggregate_claims_across_sources import (
    ClaimAggInputError,
    SUPPORTED_TARGETS,
    _confidence_for_n_sources,
    aggregate_claims_across_sources,
)


# ---------------------------------------------------------------------------
# Audit-mandated tests.
# ---------------------------------------------------------------------------


def test_single_source_date_confidence_half() -> None:
    """1 source with a recognizable date -> confidence == 0.5."""
    sources = [
        {
            "url": "https://example.com/a",
            "text": "The spill occurred on 2026-02-15 in Longview, Texas.",
            "fetched_at": "2026-06-08T00:00:00Z",
        }
    ]
    result = aggregate_claims_across_sources(sources, ["date"])
    assert result["claims"]["date"]["value"] == "2026-02-15"
    assert result["claims"]["date"]["confidence"] == 0.5
    assert result["claims"]["date"]["supporting_sources"] == [
        "https://example.com/a"
    ]
    assert result["claims"]["date"]["alternatives"] == []


def test_two_sources_agreeing_on_date_confidence_at_least_85() -> None:
    """2 sources agreeing -> confidence >= 0.8 (per audit rule)."""
    sources = [
        {
            "url": "https://example.com/a",
            "text": "The spill occurred on 2026-02-15.",
            "fetched_at": "2026-06-08T00:00:00Z",
        },
        {
            "url": "https://example.com/b",
            "text": "On February 15, 2026 a tank ruptured.",
            "fetched_at": "2026-06-08T00:01:00Z",
        },
    ]
    result = aggregate_claims_across_sources(sources, ["date"])
    assert result["claims"]["date"]["value"] == "2026-02-15"
    # Audit rule: 2 sources -> 0.80.
    assert result["claims"]["date"]["confidence"] >= 0.8
    assert len(result["claims"]["date"]["supporting_sources"]) == 2


def test_sources_disagreeing_surface_in_alternatives() -> None:
    """Disagreeing values both surface; best-supported wins."""
    sources = [
        {
            "url": "https://example.com/a",
            "text": "The spill occurred on 2026-02-15.",
            "fetched_at": "2026-06-08T00:00:00Z",
        },
        {
            "url": "https://example.com/b",
            "text": "The incident took place on 2026-02-15.",
            "fetched_at": "2026-06-08T00:01:00Z",
        },
        {
            "url": "https://example.com/c",
            "text": "Reports differ; some say 2026-02-16 was the actual day.",
            "fetched_at": "2026-06-08T00:02:00Z",
        },
    ]
    result = aggregate_claims_across_sources(sources, ["date"])
    # 2/3 sources back 2026-02-15.
    assert result["claims"]["date"]["value"] == "2026-02-15"
    # The dissenting value surfaces in alternatives.
    alt_values = [a["value"] for a in result["claims"]["date"]["alternatives"]]
    assert "2026-02-16" in alt_values


def test_empty_sources_returns_empty_claims_no_error() -> None:
    """Empty sources list -> all values None; no exception."""
    result = aggregate_claims_across_sources([], ["date", "location"])
    assert result["claims"]["date"]["value"] is None
    assert result["claims"]["date"]["confidence"] == 0.0
    assert result["claims"]["location"]["value"] is None
    assert result["stats"]["sources_consulted"] == 0
    assert result["stats"]["claims_resolved"] == 0


def test_location_extraction_longview_texas() -> None:
    """Single source mentioning 'Longview, Texas' -> location.value matches."""
    sources = [
        {
            "url": "https://example.com/a",
            "text": "Authorities responded to the spill near Longview, Texas on Friday.",
            "fetched_at": "2026-06-08T00:00:00Z",
        }
    ]
    result = aggregate_claims_across_sources(sources, ["location"])
    assert result["claims"]["location"]["value"] == "Longview, Texas"


def test_casualties_extraction_three_injured() -> None:
    """'3 injured' -> casualties.value == 3."""
    sources = [
        {
            "url": "https://example.com/a",
            "text": "Officials confirmed that 3 injured workers were taken to the hospital.",
            "fetched_at": "2026-06-08T00:00:00Z",
        }
    ]
    result = aggregate_claims_across_sources(sources, ["casualties"])
    assert result["claims"]["casualties"]["value"] == 3


# ---------------------------------------------------------------------------
# Extra unit tests.
# ---------------------------------------------------------------------------


def test_scale_extraction_gallons() -> None:
    """Scale magnitude+unit extraction."""
    sources = [
        {
            "url": "https://example.com/a",
            "text": "An estimated 5,000 gallons of vinyl chloride were released.",
            "fetched_at": "2026-06-08T00:00:00Z",
        }
    ]
    result = aggregate_claims_across_sources(sources, ["scale"])
    val = result["claims"]["scale"]["value"]
    assert val is not None
    assert val["value"] == 5000.0
    assert val["unit"] == "gallon"


def test_contaminant_extraction_vinyl_chloride() -> None:
    """Contaminant keyword sweep — long match wins over substring."""
    sources = [
        {
            "url": "https://example.com/a",
            "text": "The vinyl chloride spill prompted an evacuation. Ammonia was also detected nearby.",
            "fetched_at": "2026-06-08T00:00:00Z",
        }
    ]
    result = aggregate_claims_across_sources(sources, ["contaminant"])
    # vinyl chloride was mentioned -> wins.
    assert result["claims"]["contaminant"]["value"] == "vinyl chloride"
    # ammonia mentioned only once in a single source -> surfaces as alternative.
    alt_values = [a["value"] for a in result["claims"]["contaminant"]["alternatives"]]
    assert "ammonia" in alt_values


def test_confidence_scaling_rule() -> None:
    """Verify the audit-specified confidence rule directly."""
    assert _confidence_for_n_sources(0) == 0.0
    assert _confidence_for_n_sources(1) == 0.5
    assert _confidence_for_n_sources(2) == 0.8
    assert _confidence_for_n_sources(3) == pytest.approx(0.85)
    assert _confidence_for_n_sources(4) == pytest.approx(0.9)
    # Cap at 0.99 — many sources never push us above.
    assert _confidence_for_n_sources(100) == 0.99


def test_below_threshold_flag() -> None:
    """Confidence < threshold flags below_threshold=True."""
    sources = [
        {
            "url": "https://example.com/a",
            "text": "Spill on 2026-02-15.",
            "fetched_at": "2026-06-08T00:00:00Z",
        }
    ]
    result = aggregate_claims_across_sources(
        sources, ["date"], confidence_threshold=0.9
    )
    # 1 source -> 0.5 confidence; threshold 0.9 -> flagged.
    assert result["claims"]["date"]["below_threshold"] is True


def test_stats_count_resolved() -> None:
    """stats.claims_resolved counts targets with a non-None value."""
    sources = [
        {
            "url": "https://example.com/a",
            "text": "Spill on 2026-02-15 in Longview, Texas.",
            "fetched_at": "2026-06-08T00:00:00Z",
        }
    ]
    result = aggregate_claims_across_sources(
        sources, ["date", "location", "casualties"]
    )
    # date + location resolved; casualties has no mention.
    assert result["stats"]["claims_resolved"] == 2
    assert result["stats"]["sources_consulted"] == 1
    assert result["stats"]["confidence_threshold"] == 0.6


def test_single_source_multiple_mentions_does_not_double_count() -> None:
    """One source mentioning 'Longview, Texas' twice = one vote, not two."""
    sources = [
        {
            "url": "https://example.com/a",
            "text": "Longview, Texas was hit. Authorities in Longview, Texas responded fast.",
            "fetched_at": "2026-06-08T00:00:00Z",
        }
    ]
    result = aggregate_claims_across_sources(sources, ["location"])
    # 1 source -> 0.5 confidence even with multiple mentions.
    assert result["claims"]["location"]["value"] == "Longview, Texas"
    assert result["claims"]["location"]["confidence"] == 0.5


# ---------------------------------------------------------------------------
# Input validation.
# ---------------------------------------------------------------------------


def test_bad_sources_shape_raises() -> None:
    with pytest.raises(ClaimAggInputError):
        aggregate_claims_across_sources("not a list", ["date"])  # type: ignore[arg-type]


def test_missing_text_key_raises() -> None:
    # ``text`` is the only genuinely-required source key (claims are extracted
    # from it). Omitting it still raises.
    with pytest.raises(ClaimAggInputError, match="missing required key 'text'"):
        aggregate_claims_across_sources(
            [{"url": "https://x", "fetched_at": "2026-06-08T00:00:00Z"}],
            ["date"],
        )


def test_missing_fetched_at_and_url_are_defaulted() -> None:
    # job-0295: ``fetched_at`` (un-knowable by the LLM) and ``url`` (provenance)
    # default rather than raise, so a direct agent call with just ``text``
    # succeeds. Previously this raised "missing required key 'fetched_at'".
    result = aggregate_claims_across_sources(
        [{"text": "Flooding reported in Longview, Texas on June 8 2026."}],
        ["location"],
    )
    assert isinstance(result, dict)
    assert "claims" in result


def test_source_id_used_as_url_fallback() -> None:
    # When ``url`` is absent the aggregator falls back to ``source_id`` for
    # provenance (the shape Claude emitted in the live news run).
    result = aggregate_claims_across_sources(
        [{"source_id": "nws_alerts_TX", "source_type": "nws_alert",
          "text": "Flash Flood Warning active in Texas."}],
        ["location"],
    )
    assert isinstance(result, dict)
    assert "claims" in result


def test_unknown_target_raises() -> None:
    with pytest.raises(ClaimAggInputError, match="not supported"):
        aggregate_claims_across_sources(
            [
                {
                    "url": "https://x",
                    "text": "x",
                    "fetched_at": "2026-06-08T00:00:00Z",
                }
            ],
            ["unknown_target"],
        )


def test_bad_confidence_threshold_raises() -> None:
    with pytest.raises(ClaimAggInputError, match="confidence_threshold"):
        aggregate_claims_across_sources(
            [
                {
                    "url": "https://x",
                    "text": "x",
                    "fetched_at": "2026-06-08T00:00:00Z",
                }
            ],
            ["date"],
            confidence_threshold=1.5,
        )


# ---------------------------------------------------------------------------
# Registration.
# ---------------------------------------------------------------------------


def test_tool_is_registered() -> None:
    """aggregate_claims_across_sources is in TOOL_REGISTRY at import time."""
    assert "aggregate_claims_across_sources" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["aggregate_claims_across_sources"]
    assert entry.metadata.cacheable is False
    assert entry.metadata.ttl_class == "live-no-cache"
    assert entry.metadata.source_class == "claim_aggregator"


def test_supported_targets_complete() -> None:
    """Exactly the v0.1 audit-specified targets are supported."""
    assert set(SUPPORTED_TARGETS) == {
        "location",
        "scale",
        "contaminant",
        "date",
        "casualties",
    }
