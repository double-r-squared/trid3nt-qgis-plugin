"""Unit tests for ``trid3nt_server.mode2_classifier`` (job-0101, sprint-12-mega Wave 1).

Coverage:
    - ``.com`` (non-Mode 2 TLD) → ``None``.
    - ``.gov`` with no structural patterns → ``None``.
    - ``.gov`` + JSON-LD → ``Mode2Candidate``, confidence ≥ 0.6.
    - ``.edu`` + data-download link → ``Mode2Candidate``, ``suggested_tool_kind == "fetcher"``.
    - 5 patterns → confidence cap at 0.95.
    - job-0203 (M4): the JSONL audit writer is GONE (remove-don't-shim) —
      Mode-2 audit routes through ``Persistence.append_audit`` at the
      server call site (see ``test_mode2_audit_mcp.py``).
    - ``.mil`` + OpenAPI link → ``suggested_tool_kind == "endpoint"``.
    - Malformed page dict (missing url / non-dict) → ``None``.
    - Snippet truncates to ≤ 280 chars.
"""

from __future__ import annotations

import json

import pytest

from trid3nt_server.mode2_classifier import (
    MODE2_TLDS,
    Mode2Candidate,
    Mode2CandidateEnvelope,
    classify_for_mode2,
)


# ---------------------------------------------------------------------------
# Fixture helpers — build minimal ``web_fetch`` result dicts.
# ---------------------------------------------------------------------------


def _page(
    url: str,
    *,
    content: object = "",
    title: str | None = None,
    extract_mode: str = "main_text",
) -> dict:
    return {
        "url": url,
        "status_code": 200,
        "fetched_at": "2026-06-08T00:00:00+00:00",
        "extract_mode": extract_mode,
        "content": content,
        "title": title,
        "lang": "en",
        "content_length": len(str(content)),
    }


# ---------------------------------------------------------------------------
# Tests.
# ---------------------------------------------------------------------------


def test_non_mode2_tld_returns_none() -> None:
    """A ``.com`` domain — even with structured-data hints — is not a Mode 2 candidate."""
    page = _page(
        "https://example.com/datasets",
        content='<script type="application/ld+json">{"@type":"Dataset"}</script>',
    )
    assert classify_for_mode2(page) is None


def test_gov_with_no_patterns_returns_none() -> None:
    """A ``.gov`` page with only narrative text → ``None`` (rule 2 unmet)."""
    page = _page(
        "https://example.gov/about",
        content="<html><body><p>About our agency. We do important things.</p></body></html>",
    )
    assert classify_for_mode2(page) is None


def test_gov_with_json_ld_yields_candidate_confidence_at_least_0_6() -> None:
    """A ``.gov`` page carrying JSON-LD → candidate with ``confidence >= 0.6``."""
    page = _page(
        "https://data.example.gov/dataset/x",
        content=(
            '<html><head><script type="application/ld+json">'
            '{"@context":"https://schema.org","@type":"Dataset","name":"Rainfall"}'
            "</script></head><body><h1>Dataset</h1></body></html>"
        ),
        title="Rainfall Dataset — Example.gov",
    )
    candidate = classify_for_mode2(page)
    assert candidate is not None
    assert candidate.domain_tld == "gov"
    assert candidate.confidence >= 0.6
    assert "json-ld" in candidate.detected_patterns
    assert candidate.title == "Rainfall Dataset — Example.gov"
    # Default kind is "reference" (no openapi / no download)
    assert candidate.suggested_tool_kind == "reference"


def test_edu_with_data_download_link_suggests_fetcher() -> None:
    """An ``.edu`` page offering a CSV download → ``suggested_tool_kind == "fetcher"``."""
    page = _page(
        "https://research.example.edu/datasets/survey",
        content=(
            "<html><body><h1>Survey data</h1>"
            '<a href="/files/survey.csv">Download CSV</a>'
            "</body></html>"
        ),
    )
    candidate = classify_for_mode2(page)
    assert candidate is not None
    assert candidate.domain_tld == "edu"
    assert candidate.suggested_tool_kind == "fetcher"
    assert "data-download-link" in candidate.detected_patterns


def test_confidence_caps_at_0_95() -> None:
    """5 patterns → confidence saturates at the 0.95 cap (kickoff rule 3)."""
    page = _page(
        "https://data.example.gov/portal",
        content=(
            '<html><head><script type="application/ld+json">{"@type":"Dataset"}</script></head>'
            "<body>"
            '<a href="/openapi.json">API spec</a>'
            '<p>Use the REST API at /api/v1/records.</p>'
            '<a href="/exports/all.geojson">Download GeoJSON</a>'
            '<table><tr><td>x</td></tr><tr><td>y</td></tr><tr><td>z</td></tr>'
            '<tr><td>a</td></tr><tr><td>b</td></tr></table>'
            "data catalog"
            "</body></html>"
        ),
    )
    candidate = classify_for_mode2(page)
    assert candidate is not None
    # All 5 patterns landed.
    assert len(candidate.detected_patterns) == 5
    assert candidate.confidence == pytest.approx(0.95)


def test_jsonl_audit_writer_removed() -> None:
    """job-0203 (M4) remove-don't-shim: the bespoke JSONL writer is gone.

    Mode-2 audit events route through ``Persistence.append_audit`` (the
    MongoDB MCP ``audit_log`` collection) at the server.py call site —
    covered by ``test_mode2_audit_mcp.py``. A reappearing file writer
    here means the migration regressed.
    """
    import trid3nt_server.mode2_classifier as m2

    assert not hasattr(m2, "append_audit_log")
    assert not hasattr(m2, "default_audit_log_path")


def test_mil_with_openapi_link_suggests_endpoint() -> None:
    """A ``.mil`` page exposing an OpenAPI spec → ``suggested_tool_kind == "endpoint"``."""
    page = _page(
        "https://api.example.mil/services",
        content=(
            "<html><body><h1>Public Services API</h1>"
            "<p>See <a href='/swagger.json'>API spec</a> for details.</p>"
            "</body></html>"
        ),
    )
    candidate = classify_for_mode2(page)
    assert candidate is not None
    assert candidate.domain_tld == "mil"
    assert candidate.suggested_tool_kind == "endpoint"
    assert "openapi-spec-link" in candidate.detected_patterns


def test_malformed_page_dict_returns_none() -> None:
    """Defensive: missing ``url`` / non-dict input → ``None`` (never raises)."""
    assert classify_for_mode2({}) is None
    assert classify_for_mode2({"url": ""}) is None
    assert classify_for_mode2({"url": 42}) is None  # type: ignore[arg-type]
    assert classify_for_mode2("not a dict") is None  # type: ignore[arg-type]
    assert classify_for_mode2(None) is None  # type: ignore[arg-type]
    # URL with no host is also rejected.
    assert classify_for_mode2({"url": "https://"}) is None


def test_envelope_to_wire_dict_round_trip() -> None:
    """``Mode2CandidateEnvelope.to_wire_dict`` produces a JSON-serializable dict."""
    page = _page(
        "https://data.example.gov/x",
        content='<script type="application/ld+json">{"@type":"Dataset"}</script>',
    )
    candidate = classify_for_mode2(page)
    assert candidate is not None
    env = Mode2CandidateEnvelope(candidate=candidate)
    wire = env.to_wire_dict()
    assert wire["envelope_type"] == "mode2-candidate"
    assert wire["candidate"]["domain_tld"] == "gov"
    # JSON-serializable round-trip.
    serialized = json.dumps(wire)
    re_loaded = json.loads(serialized)
    assert re_loaded["candidate"]["url"] == "https://data.example.gov/x"


def test_mode2_tlds_constant_matches_kickoff() -> None:
    """Guard against accidental TLD list edits — kickoff names exactly four."""
    assert set(MODE2_TLDS) == {"gov", "edu", "mil", "int"}


def test_snippet_truncates_at_280_chars() -> None:
    """The snippet field never exceeds 280 chars even when the body region is huge."""
    long_body = (
        "<html><body>"
        + ("filler text " * 50)
        + '<script type="application/ld+json">{"@type":"Dataset"}</script>'
        + ("more filler " * 50)
        + "</body></html>"
    )
    page = _page("https://data.example.gov/big", content=long_body)
    candidate = classify_for_mode2(page)
    assert candidate is not None
    assert candidate.snippet is not None
    assert len(candidate.snippet) <= 280
