"""Mode 2 ``.gov``/``.edu`` offer-to-add classifier (FR-DS-* Mode 2; SRS §F.1.2).

job-0101 (sprint-12-mega Wave 1) — When the agent fetches a web page (via the
``web_fetch`` atomic tool, job-0092) AND the page is on a ``.gov`` / ``.edu`` /
``.mil`` / ``.int`` top-level domain AND it carries patterns consistent with
*structured* data (JSON-LD, an OpenAPI / Swagger spec link, a REST endpoint
pattern, a "Download CSV / GeoJSON" link, or a tabular dataset listing), this
classifier flags it as a candidate "Mode 2 source" the user might want to
formally add to the catalog.

Why this is a separate module (not a tool)

- The classifier is **deterministic + cheap** — pure string detection over the
  ``web_fetch`` result dict. It runs on EVERY ``web_fetch`` invocation as a
  side-effect filter, not as an LLM-callable tool. Putting it in the tool
  registry would tempt the LLM into calling it directly; the design point is
  that the agent surfaces candidates *automatically* during research.
- The wire envelope ``Mode2CandidateEnvelope`` it produces is rendered by a
  forthcoming web modal (sprint-12-mega Wave 2/3) — the modal is the place
  where the user accepts / rejects the suggested catalog entry. That modal
  work is a separate job; this module just emits the candidate envelope and
  the audit-log line.

Relationship to the existing ``offer-catalog-addition`` envelope
(``packages/contracts/src/trid3nt_contracts/ws.py``, sprint-08): the heavier
``offer-catalog-addition`` flow expects a full agent-side conformity probe
(``ProbeFindings``) + a drafted ``SuggestedCatalogEntry``. The ``mode2-candidate``
envelope here is intentionally lighter — it's a *low-friction*, fire-and-forget
preview that says "hey, this page looks like it might host structured data, do
you want to enrich it?" without committing the agent to running the full probe.
The forthcoming Wave 2/3 modal renders ``mode2-candidate``; when the user clicks
"yes, enrich this", the agent runs the heavier ``offer-catalog-addition`` flow
on top. The two envelopes coexist; the lighter one feeds the heavier one. This
overlap is surfaced as OQ-0101-MODE2-ENVELOPE-OVERLAP for orchestrator review.

Audit log
~~~~~~~~~

Every emitted candidate is appended to the MongoDB MCP ``audit_log``
collection (D.15) via ``Persistence.append_audit("mode2-candidate", ...)``
at the server.py call site (job-0203 / Wave 4.11 M4) — persistent across
sessions so the user can later review "what did the classifier flag this
week?" without scrolling back through chat. The earlier bespoke JSONL file
writer was removed (remove-don't-shim).

Inputs / outputs
~~~~~~~~~~~~~~~~

``classify_for_mode2(page_dict)`` consumes the dict ``web_fetch`` returns
(documented in ``tools/web_fetch.py:web_fetch.__doc__``):

    {
      "url": str,           # final URL after redirects
      "status_code": int,
      "fetched_at": str,
      "extract_mode": str,
      "content": str | dict | None,
      "title": str | None,
      "lang": str | None,
      "content_length": int,
    }

and returns a ``Mode2Candidate`` if the page qualifies, else ``None``. The
classification is deterministic — the same page always produces the same
candidate envelope (modulo the freshly-generated ``candidate_id``).

FROZEN boundary notes (sprint-12-mega Wave 1)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

This module does NOT modify ``web_fetch.py`` (sibling job-0092, FROZEN).
The integration site is ``server.py``'s ``_invoke_tool_via_emitter`` wrapper
where every tool result passes through anyway — we hook the
``mode2_candidate_check`` there post-result (≤ 20 lines per kickoff).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlparse

from trid3nt_contracts import new_ulid

__all__ = [
    "Mode2Candidate",
    "Mode2CandidateEnvelope",
    "MODE2_TLDS",
    "classify_for_mode2",
]

logger = logging.getLogger("trid3nt_server.mode2_classifier")


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

#: TLDs considered trust-eligible for Mode 2 catalog enrichment per SRS §F.1.2.
#: ``.mil`` / ``.int`` are included alongside ``.gov`` / ``.edu`` because they
#: share the same provenance discipline (institutionally-attested authoritative
#: source); the kickoff spells out all four.
MODE2_TLDS: tuple[str, ...] = ("gov", "edu", "mil", "int")

#: Confidence base + per-pattern bump + cap. The deterministic ladder lets
#: ``classify_for_mode2`` produce a single repeatable score per page.
_CONFIDENCE_BASE: float = 0.5
_CONFIDENCE_PER_PATTERN: float = 0.1
_CONFIDENCE_CAP: float = 0.95

#: Pattern names in deterministic order (drives ``suggested_tool_kind``
#: precedence + makes the ``detected_patterns`` list stable across runs).
_PATTERN_ORDER = (
    "json-ld",
    "openapi-spec-link",
    "rest-endpoint-pattern",
    "data-download-link",
    "tabular-data",
)

#: Heuristic regexes for body-text detection. Kept narrow + permissive — we'd
#: rather under-detect than spam the user with false positives.
_JSON_LD_PATTERN = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\']', re.IGNORECASE
)
_OPENAPI_PATTERN = re.compile(
    r"(openapi\.json|swagger\.json|swagger-ui|/openapi\.yaml|/api-docs)",
    re.IGNORECASE,
)
_REST_ENDPOINT_PATTERN = re.compile(
    r"(/api/v?\d+/|/api/[a-z]|/v\d+/(api|service|data)|rest\s+(api|endpoint))",
    re.IGNORECASE,
)
_DATA_DOWNLOAD_PATTERN = re.compile(
    r"(download\s+(csv|geojson|json|shapefile|netcdf|excel|xls|kml|gpkg)|"
    r'\.(?:csv|geojson|shp|nc|kml|gpkg)["\'\)\s]|'
    r'href=["\'][^"\']*\.(?:csv|geojson|shp|nc|kml|gpkg)["\'])',
    re.IGNORECASE,
)
_TABULAR_PATTERN = re.compile(
    r"(<table[^>]*>[\s\S]{40,}?</table>|"
    r"data\s+catalog|dataset\s+(listing|catalog|index))",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Public dataclasses.
# ---------------------------------------------------------------------------


@dataclass
class Mode2Candidate:
    """A single ``.gov`` / ``.edu`` / ``.mil`` / ``.int`` candidate page.

    Constructed by ``classify_for_mode2``; carried inside
    ``Mode2CandidateEnvelope`` and rendered by the web "offer to add" modal
    (Wave 2/3 work).

    Fields:
        candidate_id: ULID identifying this candidate emission. Unique per
            ``classify_for_mode2`` call so the client UI can correlate user
            response → originating fetch.
        url: the final URL after redirects (the ``web_fetch`` result's ``url``).
        domain: the host of ``url`` (lowercased).
        domain_tld: which Mode 2 TLD the host carries (``gov``/``edu``/``mil``/
            ``int``); ``"other"`` is included for parity with the kickoff but
            never appears in a returned candidate (non-Mode 2 TLDs short-circuit
            to ``None`` before construction).
        confidence: deterministic 0-1 score; ``0.5 + 0.1 * n_patterns`` capped at
            0.95.
        detected_patterns: stable-ordered list of pattern names detected in the
            body / metadata.
        title: the ``<title>`` extracted by ``web_fetch`` (if any).
        suggested_tool_kind: hint for the client UI's pre-filled "tool type"
            radio — ``"endpoint"`` if the page exposes an OpenAPI spec,
            ``"fetcher"`` if it offers downloadable structured data, else
            ``"reference"``.
        snippet: optional ≤ 280-char excerpt of the matched body region; useful
            for the modal to show "here's the thing we matched". TENTATIVE
            default: the first ~280 chars of the body where the first pattern
            hit.
    """

    candidate_id: str
    url: str
    domain: str
    domain_tld: Literal["gov", "edu", "mil", "int", "other"]
    confidence: float
    detected_patterns: list[str]
    title: str | None = None
    suggested_tool_kind: Literal["fetcher", "endpoint", "reference"] = "reference"
    snippet: str | None = None


@dataclass
class Mode2CandidateEnvelope:
    """WebSocket envelope wrapper for a ``Mode2Candidate``.

    Light by design — no ``request_id``/``ttl_seconds``/``probe_findings``
    (the heavier ``offer-catalog-addition`` envelope carries those for the full
    review flow). The client opens a passive "candidate detected" indicator;
    user opt-in to the full review fires the heavier flow.

    Serialized via ``to_wire_dict`` because ``packages/contracts/`` is FROZEN
    for this job — we emit raw JSON rather than introducing a contract model.
    Wave 2/3 (or a follow-up schema job) may promote this to a real pydantic
    envelope under ``trid3nt_contracts.ws``; OQ-0101-MODE2-ENVELOPE-OVERLAP
    surfaces that decision.
    """

    candidate: Mode2Candidate
    envelope_type: Literal["mode2-candidate"] = "mode2-candidate"

    def to_wire_dict(self) -> dict[str, Any]:
        """Return a plain-dict form for JSON serialization on the wire."""
        c = self.candidate
        return {
            "envelope_type": self.envelope_type,
            "candidate": {
                "candidate_id": c.candidate_id,
                "url": c.url,
                "domain": c.domain,
                "domain_tld": c.domain_tld,
                "confidence": c.confidence,
                "detected_patterns": list(c.detected_patterns),
                "title": c.title,
                "suggested_tool_kind": c.suggested_tool_kind,
                "snippet": c.snippet,
            },
        }


# ---------------------------------------------------------------------------
# Classification.
# ---------------------------------------------------------------------------


def _tld_for_host(host: str) -> Literal["gov", "edu", "mil", "int", "other"]:
    """Return the Mode 2 TLD bucket for ``host`` (lowercased), or ``"other"``.

    Match policy: the bare TLD label (``"weather.gov"`` → ``"gov"``;
    ``"sub.example.gov"`` → ``"gov"``). State / second-level Mode 2 TLDs like
    ``.k12.ca.us`` are intentionally NOT matched in v0.1 — we keep the trust
    boundary tight to the four top-level institutional TLDs the SRS calls out.
    """
    host = host.lower().strip()
    # Strip port if present.
    if ":" in host:
        host = host.split(":", 1)[0]
    parts = host.split(".")
    if len(parts) < 2:
        return "other"
    tld = parts[-1]
    if tld in MODE2_TLDS:
        return tld  # type: ignore[return-value]
    return "other"


def _body_text_for_detection(page_dict: dict[str, Any]) -> str:
    """Coalesce the ``web_fetch`` result into a single text blob for detection.

    The ``content`` field varies by ``extract_mode``:
        - ``"full_html"`` / ``"main_text"`` → str
        - ``"metadata"`` → dict (Open Graph + meta tags)
        - ``"json"`` → dict (parsed JSON)

    For dicts we serialize to JSON so the regex patterns can still find
    URL-like strings (``/api/...``, ``.csv``) inside values without us
    enumerating every shape.
    """
    content = page_dict.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, (dict, list)):
        try:
            return json.dumps(content)
        except (TypeError, ValueError):
            return repr(content)
    return ""


def _detect_patterns(body: str) -> list[str]:
    """Return the stable-ordered list of patterns matched in ``body``."""
    matches: list[str] = []
    if _JSON_LD_PATTERN.search(body):
        matches.append("json-ld")
    if _OPENAPI_PATTERN.search(body):
        matches.append("openapi-spec-link")
    if _REST_ENDPOINT_PATTERN.search(body):
        matches.append("rest-endpoint-pattern")
    if _DATA_DOWNLOAD_PATTERN.search(body):
        matches.append("data-download-link")
    if _TABULAR_PATTERN.search(body):
        matches.append("tabular-data")
    # Preserve _PATTERN_ORDER ordering even if regex evaluation order changes.
    return [p for p in _PATTERN_ORDER if p in matches]


def _suggested_kind(
    patterns: list[str],
) -> Literal["fetcher", "endpoint", "reference"]:
    """Map detected patterns to a ``suggested_tool_kind``.

    Precedence (kickoff rule 4):
        1. ``openapi-spec-link`` → ``"endpoint"`` (callable spec wins)
        2. ``data-download-link`` → ``"fetcher"`` (downloadable artifact)
        3. else → ``"reference"`` (informational / index page)
    """
    if "openapi-spec-link" in patterns:
        return "endpoint"
    if "data-download-link" in patterns:
        return "fetcher"
    return "reference"


def _snippet_for(body: str, patterns: list[str]) -> str | None:
    """Return a ≤ 280-char excerpt around the first pattern hit, or None.

    Useful in the client UI so the user sees "here's the thing we matched"
    without re-fetching the page. We deliberately keep this short — the modal
    has a "see source" link for the full content.
    """
    if not body or not patterns:
        return None
    # Find the first pattern's match position using the same regexes.
    first = patterns[0]
    pattern_map = {
        "json-ld": _JSON_LD_PATTERN,
        "openapi-spec-link": _OPENAPI_PATTERN,
        "rest-endpoint-pattern": _REST_ENDPOINT_PATTERN,
        "data-download-link": _DATA_DOWNLOAD_PATTERN,
        "tabular-data": _TABULAR_PATTERN,
    }
    rx = pattern_map.get(first)
    if rx is None:
        return None
    m = rx.search(body)
    if m is None:
        return None
    start = max(0, m.start() - 60)
    end = min(len(body), m.start() + 220)
    excerpt = body[start:end].strip()
    if len(excerpt) > 280:
        excerpt = excerpt[:280]
    return excerpt or None


def classify_for_mode2(page_dict: dict[str, Any]) -> Mode2Candidate | None:
    """Decide whether the ``web_fetch`` result ``page_dict`` is a Mode 2 candidate.

    Returns a ``Mode2Candidate`` if the page qualifies, else ``None``. The
    classification is deterministic — same page in, same candidate out (modulo
    the freshly-minted ``candidate_id``).

    Decision rules (kickoff):
        1. ``domain`` MUST be a Mode 2 TLD (``.gov`` / ``.edu`` / ``.mil`` /
           ``.int``); anything else → ``None``.
        2. detect at least 1 structural pattern from the page content
           (``_detect_patterns``); zero → ``None``.
        3. ``confidence = min(0.95, 0.5 + 0.1 * n_patterns)``.
        4. ``suggested_tool_kind`` follows ``_suggested_kind``.

    Args:
        page_dict: the result dict returned by the ``web_fetch`` atomic tool
            (see ``tools/web_fetch.py``). Must carry at least ``"url"``;
            missing or malformed inputs return ``None`` defensively (we never
            raise inside the side-effect classifier — the tool result is the
            authoritative signal, the classifier is best-effort).

    Returns:
        A ``Mode2Candidate`` or ``None``.
    """
    if not isinstance(page_dict, dict):
        return None
    url = page_dict.get("url")
    if not isinstance(url, str) or not url:
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if not host:
        return None
    tld = _tld_for_host(host)
    if tld == "other":
        return None

    body = _body_text_for_detection(page_dict)
    patterns = _detect_patterns(body)
    if not patterns:
        return None

    confidence = min(
        _CONFIDENCE_CAP, _CONFIDENCE_BASE + _CONFIDENCE_PER_PATTERN * len(patterns)
    )
    kind = _suggested_kind(patterns)
    title = page_dict.get("title") if isinstance(page_dict.get("title"), str) else None
    snippet = _snippet_for(body, patterns)

    return Mode2Candidate(
        candidate_id=new_ulid(),
        url=url,
        domain=host,
        domain_tld=tld,
        confidence=round(confidence, 3),
        detected_patterns=patterns,
        title=title,
        suggested_tool_kind=kind,
        snippet=snippet,
    )


# ---------------------------------------------------------------------------
# Audit log — REMOVED (job-0203 / Wave 4.11 M4, remove-don't-shim).
#
# The bespoke JSONL file writer (``append_audit_log`` +
# ``default_audit_log_path``, ``~/.trid3nt/mode2_audit.log``) was the last
# CRUD path bypassing MongoDB MCP. Mode-2 candidate audit events now route
# through ``Persistence.append_audit("mode2-candidate", ...)`` at the
# server.py call site — the ``audit_log`` collection (D.15) is the single
# audit stream. On a dev box the file-backed substrate lands them in
# ``~/.trid3nt/dev_persistence/<db>/audit_log.json``.
# ---------------------------------------------------------------------------
