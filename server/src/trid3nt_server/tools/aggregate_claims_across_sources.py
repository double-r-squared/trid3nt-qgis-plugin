"""``aggregate_claims_across_sources`` atomic tool — cross-source claim aggregation (job-0093).

This tool sits at the centre of the FR-HEP news/event-ingest pipeline (FR-HEP-6):
the agent fetches multiple news articles, agency pages, or similar texts about the
same event via ``web_fetch``/``search_news``/``fetch_news_article``, then hands the
``{url, text, fetched_at}`` triples to this aggregator together with the list of
``claim_targets`` to extract. The tool runs deterministic regex-based extraction
per target, groups identical values, and scores each candidate by the number of
sources backing it. The best-supported value per target is returned as the
``value`` + ``confidence`` + ``supporting_sources``; competing values land in
``alternatives``.

v0.1 deterministic-only strategy (no LLM call):

This tool is REGISTERED ``cacheable=False`` / ``ttl_class="live-no-cache"``: each
call's output is a function of the (possibly fresh) ``sources`` list passed in
and cannot be reused across different source lists. The tool body itself does
NOT touch GCS or the cache shim.

Per the audit (job-0093 audit.md), v0.1 uses deterministic regex + keyword
extraction for "date" / "scale" / "casualties" and naive title-case sweeps for
"location" / "contaminant". The OQ-93-NEEDS-LLM-EXTRACTION proposes upgrading
"location" + "contaminant" to LLM-routed extraction in sprint-13 so the agent
can resolve ambiguous mentions (e.g. "the spill near Longview", town vs county,
chemical-family names) the way a human would.

Source-agreement scoring rule (audit-specified):

    confidence = 0.5                            if 1 source supports the value
    confidence = min(0.99, 0.8 + 0.05*(N-2))   if N >= 2 sources support it

So 1 source -> 0.5; 2 sources -> 0.8; 3 sources -> 0.85; 4 sources -> 0.9; ...;
capped at 0.99 regardless of how many sources agree (we never claim certainty
from agreement alone, since systematic source-bias / shared upstream wire
services can drive false agreement).

Typed errors (FR-AS-11):
    - ``ClaimAggError(retryable=False)`` — bad input shape (non-list sources,
      missing required keys, unknown claim target).

Geographic-correctness check (job-0086 codified lesson):

This tool DOES NOT emit geometry; it returns a structured claims dict. The
"location" target's ``value`` is a place-name STRING (reverse-geocoding to a
bbox is deferred to a downstream ``geocode_event_location`` call, per engine.md
scope). A round-trip-only acceptance check is therefore sufficient for v0.1;
when LLM extraction lands in sprint-13 and the tool optionally produces a bbox,
the acceptance test must add a "is the bbox actually around the named place"
geographic check.

FR-TA-3 docstring discipline applies to the public ``aggregate_claims_across_sources``.
"""

from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from . import register_tool

__all__ = [
    "aggregate_claims_across_sources",
    "ClaimAggError",
    "ClaimAggInputError",
    "SUPPORTED_TARGETS",
]

logger = logging.getLogger("trid3nt_server.tools.aggregate_claims_across_sources")


# ---------------------------------------------------------------------------
# Typed errors (FR-AS-11).
# ---------------------------------------------------------------------------


class ClaimAggError(RuntimeError):
    """Base class for aggregate_claims_across_sources failures."""

    error_code: str = "CLAIM_AGG_ERROR"
    retryable: bool = False


class ClaimAggInputError(ClaimAggError):
    """Caller passed a malformed ``sources`` list or unknown claim target."""

    error_code = "CLAIM_AGG_INPUT_INVALID"
    retryable = False


# ---------------------------------------------------------------------------
# Supported claim targets (v0.1 deterministic).
# ---------------------------------------------------------------------------

#: Claim targets the v0.1 deterministic extractor supports. Targets outside
#: this set surface as ``ClaimAggInputError`` so callers fail fast rather than
#: silently get an empty result.
SUPPORTED_TARGETS: tuple[str, ...] = (
    "location",
    "scale",
    "contaminant",
    "date",
    "casualties",
)


# ---------------------------------------------------------------------------
# Per-target extractors. Each returns a list of (raw_value, normalized_value)
# tuples or an empty list if no mention is found. The ``raw_value`` is the
# substring as it appeared in the text (useful for debugging / provenance);
# the ``normalized_value`` is what gets grouped for agreement scoring.
# ---------------------------------------------------------------------------


# Common chemical / contaminant tokens. Not exhaustive — designed to cover the
# vinyl-chloride / benzene / ammonia / chlorine class of incidents the v0.1
# FR-HEP demo cases target. The OQ-93-NEEDS-LLM-EXTRACTION upgrade path swaps
# this regex bag for an LLM-routed entity-extraction call.
_CONTAMINANT_KEYWORDS = (
    "vinyl chloride",
    "benzene",
    "ammonia",
    "chlorine",
    "hydrochloric acid",
    "sulfuric acid",
    "ethylene glycol",
    "phosgene",
    "methanol",
    "ethanol",
    "petroleum",
    "crude oil",
    "diesel",
    "gasoline",
    "natural gas",
    "propane",
    "butadiene",
    "styrene",
    "formaldehyde",
    "polychlorinated biphenyl",
    "pcb",
    "asbestos",
    "lead",
    "mercury",
    "arsenic",
    "cyanide",
    "pesticide",
    "herbicide",
    "fertilizer",
    "anhydrous ammonia",
)


_SCALE_PATTERNS = (
    # "5,000 gallons", "5000 gallons", "5.5 million gallons"
    re.compile(
        r"(\d{1,3}(?:[,\d]{3,})*(?:\.\d+)?|\d+(?:\.\d+)?)\s*"
        r"(?:million|thousand|billion)?\s*"
        r"(gallons?|liters?|tons?|tonnes?|barrels?|cubic\s+(?:meters?|feet)|acres?|hectares?|square\s+(?:miles?|kilometers?))",
        re.IGNORECASE,
    ),
)

_CASUALTIES_PATTERNS = (
    re.compile(
        r"(\d{1,5})\s+(?:people\s+)?(?:were\s+)?(injured|hurt|wounded|killed|dead|deaths?|fatalities|casualties)",
        re.IGNORECASE,
    ),
)

# ISO-8601 dates: 2026-02-15, 2026-02-15T10:30:00Z
_ISO_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})(?:T\d{2}:\d{2}:\d{2}Z?)?\b")

# Long-form dates: "February 15, 2026", "Feb 15, 2026"
_MONTHS = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)
_MONTH_ABBREVS = {m[:3]: m for m in _MONTHS}
_LONG_DATE_RE = re.compile(
    r"\b("
    + "|".join(_MONTHS + tuple(_MONTH_ABBREVS))
    + r")\.?\s+(\d{1,2}),?\s+(\d{4})\b",
    re.IGNORECASE,
)

# US-style: "2/15/2026", "02/15/2026"
_US_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")


# "Longview, Texas", "Palestine, OH" — Title-case word(s) followed by a comma
# and a state name or 2-letter abbreviation.
_US_STATES = {
    "AL": "Alabama",
    "AK": "Alaska",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "FL": "Florida",
    "GA": "Georgia",
    "HI": "Hawaii",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "IA": "Iowa",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "ME": "Maine",
    "MD": "Maryland",
    "MA": "Massachusetts",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MS": "Mississippi",
    "MO": "Missouri",
    "MT": "Montana",
    "NE": "Nebraska",
    "NV": "Nevada",
    "NH": "New Hampshire",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NY": "New York",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "UT": "Utah",
    "VT": "Vermont",
    "VA": "Virginia",
    "WA": "Washington",
    "WV": "West Virginia",
    "WI": "Wisconsin",
    "WY": "Wyoming",
}
_STATE_NAMES_LOWER = {v.lower(): v for v in _US_STATES.values()}
_LOCATION_RE = re.compile(
    r"\b([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+){0,3}),\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?|[A-Z]{2})\b"
)


# ---------------------------------------------------------------------------
# Extraction helpers.
# ---------------------------------------------------------------------------


def _extract_dates(text: str) -> list[tuple[str, str]]:
    """Return [(raw_substring, ISO-8601 yyyy-mm-dd), ...] from text.

    Tries ISO-8601 first (highest confidence), then long-form, then US-style.
    Falls back silently on parse failure (we never raise for un-extractable
    mentions; we just return what we can).
    """
    results: list[tuple[str, str]] = []
    for match in _ISO_DATE_RE.finditer(text):
        results.append((match.group(0), match.group(1)))
    for match in _LONG_DATE_RE.finditer(text):
        month_token = match.group(1).lower().rstrip(".")
        if month_token in _MONTH_ABBREVS:
            month_name = _MONTH_ABBREVS[month_token]
        elif month_token in _MONTHS:
            month_name = month_token
        else:
            continue
        try:
            month_num = _MONTHS.index(month_name) + 1
            day = int(match.group(2))
            year = int(match.group(3))
            iso = datetime(year, month_num, day).strftime("%Y-%m-%d")
            results.append((match.group(0), iso))
        except (ValueError, IndexError):
            continue
    for match in _US_DATE_RE.finditer(text):
        try:
            month_num = int(match.group(1))
            day = int(match.group(2))
            year = int(match.group(3))
            iso = datetime(year, month_num, day).strftime("%Y-%m-%d")
            results.append((match.group(0), iso))
        except ValueError:
            continue
    return results


def _normalize_magnitude(num_str: str, modifier: str | None) -> float:
    """Convert "5,000" / "5.5" + optional "million" to a float."""
    cleaned = num_str.replace(",", "")
    base = float(cleaned)
    if modifier:
        mod = modifier.lower()
        if mod == "thousand":
            base *= 1_000
        elif mod == "million":
            base *= 1_000_000
        elif mod == "billion":
            base *= 1_000_000_000
    return base


def _extract_scale(text: str) -> list[tuple[str, dict[str, Any]]]:
    """Return [(raw, {value: float, unit: str}), ...] from text."""
    results: list[tuple[str, dict[str, Any]]] = []
    for pattern in _SCALE_PATTERNS:
        for match in pattern.finditer(text):
            raw = match.group(0)
            num_str = match.group(1)
            unit = match.group(2)
            # Look for "thousand/million/billion" between num and unit.
            between = text[match.start() : match.end()].lower()
            modifier = None
            for mod in ("thousand", "million", "billion"):
                if mod in between:
                    modifier = mod
                    break
            try:
                magnitude = _normalize_magnitude(num_str, modifier)
            except ValueError:
                continue
            # Normalize unit to singular lowercase, collapse whitespace.
            unit_norm = re.sub(r"\s+", " ", unit.lower()).rstrip("s")
            results.append((raw, {"value": magnitude, "unit": unit_norm}))
    return results


def _extract_casualties(text: str) -> list[tuple[str, int]]:
    """Return [(raw, count_int), ...] from text."""
    results: list[tuple[str, int]] = []
    for pattern in _CASUALTIES_PATTERNS:
        for match in pattern.finditer(text):
            try:
                count = int(match.group(1))
                results.append((match.group(0), count))
            except ValueError:
                continue
    return results


def _extract_contaminants(text: str) -> list[tuple[str, str]]:
    """Return [(raw_substring, normalized_lowercase_name), ...] from text.

    Uses the curated _CONTAMINANT_KEYWORDS list — TENTATIVE deterministic
    approach, OQ-93-NEEDS-LLM-EXTRACTION proposes LLM upgrade for sprint-13.
    """
    results: list[tuple[str, str]] = []
    text_lower = text.lower()
    seen_spans: set[tuple[int, int]] = set()
    # Sort by length descending so longer matches (e.g. "anhydrous ammonia")
    # take precedence over substring matches ("ammonia").
    for keyword in sorted(_CONTAMINANT_KEYWORDS, key=len, reverse=True):
        idx = 0
        while True:
            pos = text_lower.find(keyword, idx)
            if pos < 0:
                break
            # Skip if this span overlaps a longer-match span already recorded.
            span = (pos, pos + len(keyword))
            overlap = any(
                not (span[1] <= s[0] or span[0] >= s[1]) for s in seen_spans
            )
            if not overlap:
                seen_spans.add(span)
                raw = text[pos : pos + len(keyword)]
                results.append((raw, keyword))
            idx = pos + len(keyword)
    return results


def _extract_locations(text: str) -> list[tuple[str, str]]:
    """Return [(raw_substring, normalized_city_state), ...] from text."""
    results: list[tuple[str, str]] = []
    for match in _LOCATION_RE.finditer(text):
        city = match.group(1).strip()
        state_token = match.group(2).strip()
        # Normalize state to full name. Accept 2-letter abbrev or full name.
        if len(state_token) == 2 and state_token.upper() in _US_STATES:
            state_full = _US_STATES[state_token.upper()]
        elif state_token.lower() in _STATE_NAMES_LOWER:
            state_full = _STATE_NAMES_LOWER[state_token.lower()]
        else:
            # Not a recognized US state — skip rather than emit a noisy match.
            continue
        normalized = f"{city}, {state_full}"
        results.append((match.group(0), normalized))
    return results


# ---------------------------------------------------------------------------
# Aggregation core.
# ---------------------------------------------------------------------------


def _confidence_for_n_sources(n: int) -> float:
    """Source-agreement confidence per the audit-specified rule.

    1 source                 -> 0.5
    >= 2 sources             -> min(0.99, 0.8 + 0.05 * (n - 2))
    """
    if n <= 0:
        return 0.0
    if n == 1:
        return 0.5
    return min(0.99, 0.8 + 0.05 * (n - 2))


def _value_to_grouping_key(value: Any) -> str:
    """Stable string key for grouping equivalent values across sources.

    For dicts (scale: ``{value, unit}``), serialize keys in sorted order so two
    sources reporting the same magnitude+unit group together.
    """
    if isinstance(value, dict):
        return "|".join(f"{k}={value[k]}" for k in sorted(value.keys()))
    return str(value)


def _aggregate_target(
    target: str,
    source_extractions: list[tuple[str, list[tuple[str, Any]]]],
    confidence_threshold: float,
) -> dict[str, Any]:
    """Group extractions for one target across sources, score, return claim dict.

    ``source_extractions`` is a list of ``(source_url, [(raw, normalized), ...])``
    tuples — one entry per input source, each carrying every mention found in
    that source's text.

    Strategy:
      1. Per source, collapse to the SET of distinct normalized values found
         (a single article mentioning "Longview, Texas" 5 times is still one
         vote, not five — avoids over-weighting verbose sources).
      2. Across sources, count how many sources support each value.
      3. Best value = the one with the highest source count (ties broken by
         insertion order — first-seen-first wins, stable for tests).
      4. Confidence per the audit-specified rule. If below ``confidence_threshold``,
         the value is still returned (caller may still want to inspect it) but
         the threshold is recorded in the result for downstream filtering.
    """
    # Step 1: per-source distinct values, preserving first-seen order so the
    # cross-source aggregation step has a deterministic tie-breaker.
    per_source: list[tuple[str, list[str], dict[str, Any]]] = []
    for url, mentions in source_extractions:
        distinct_keys_in_order: list[str] = []
        seen_keys: set[str] = set()
        key_to_value: dict[str, Any] = {}
        for _raw, normalized in mentions:
            k = _value_to_grouping_key(normalized)
            if k not in seen_keys:
                seen_keys.add(k)
                distinct_keys_in_order.append(k)
                key_to_value[k] = normalized
        per_source.append((url, distinct_keys_in_order, key_to_value))

    # Step 2: aggregate counts.
    counts: Counter[str] = Counter()
    supporting: dict[str, list[str]] = defaultdict(list)
    values_by_key: dict[str, Any] = {}
    insertion_order: list[str] = []
    for url, keys, key_to_value in per_source:
        for k in keys:
            if k not in values_by_key:
                values_by_key[k] = key_to_value[k]
                insertion_order.append(k)
            counts[k] += 1
            supporting[k].append(url)

    if not counts:
        return {
            "value": None,
            "confidence": 0.0,
            "supporting_sources": [],
            "alternatives": [],
        }

    # Step 3: pick the best value. Sort by (-count, insertion_index) for stable
    # tie-breaking.
    insertion_index = {k: i for i, k in enumerate(insertion_order)}
    sorted_keys = sorted(
        counts.keys(), key=lambda k: (-counts[k], insertion_index[k])
    )
    best_key = sorted_keys[0]
    best_count = counts[best_key]
    best_value = values_by_key[best_key]
    confidence = _confidence_for_n_sources(best_count)

    # Step 4: alternatives = all other distinct values, in count-descending order.
    alternatives: list[dict[str, Any]] = []
    for k in sorted_keys[1:]:
        alternatives.append(
            {
                "value": values_by_key[k],
                "supporting_sources": list(supporting[k]),
            }
        )

    result = {
        "value": best_value,
        "confidence": confidence,
        "supporting_sources": list(supporting[best_key]),
        "alternatives": alternatives,
    }
    if confidence < confidence_threshold:
        result["below_threshold"] = True
    return result


# ---------------------------------------------------------------------------
# Input validation.
# ---------------------------------------------------------------------------


def _validate_sources(sources: Any) -> list[dict[str, Any]]:
    """Validate ``sources`` is list[{url, text, fetched_at}]. Raise on bad shape.

    Returns the validated list (unchanged on success) so callers can chain.
    """
    if not isinstance(sources, list):
        raise ClaimAggInputError(
            f"sources must be a list of dicts; got {type(sources).__name__}"
        )
    for i, item in enumerate(sources):
        if not isinstance(item, dict):
            raise ClaimAggInputError(
                f"sources[{i}] must be a dict; got {type(item).__name__}"
            )
        # job-0295: ``url`` and ``fetched_at`` are provenance metadata the LLM
        # cannot always supply (it doesn't know the fetch timestamp, and may
        # only have the source text). Default them so a direct agent call with
        # ``{text, ...}`` succeeds — only ``text`` (the substance claims are
        # extracted from) is genuinely required. The composer already stamps
        # its own ``fetched_at`` sentinel, so this only affects direct callers.
        item.setdefault("url", item.get("source_id") or "")
        item.setdefault("fetched_at", "1970-01-01T00:00:00Z")
        if "text" not in item:
            raise ClaimAggInputError(
                f"sources[{i}] missing required key 'text'; got keys {list(item.keys())}"
            )
        for key in ("url", "text", "fetched_at"):
            if not isinstance(item[key], str):
                raise ClaimAggInputError(
                    f"sources[{i}][{key!r}] must be a string; got {type(item[key]).__name__}"
                )
    return sources


def _validate_targets(claim_targets: Any) -> list[str]:
    """Validate ``claim_targets`` is a list of known target names."""
    if not isinstance(claim_targets, list):
        raise ClaimAggInputError(
            f"claim_targets must be a list of strings; got {type(claim_targets).__name__}"
        )
    for i, target in enumerate(claim_targets):
        if not isinstance(target, str):
            raise ClaimAggInputError(
                f"claim_targets[{i}] must be a string; got {type(target).__name__}"
            )
        if target not in SUPPORTED_TARGETS:
            raise ClaimAggInputError(
                f"claim_targets[{i}]={target!r} is not supported; "
                f"v0.1 supports {SUPPORTED_TARGETS} (see OQ-93-NEEDS-LLM-EXTRACTION)"
            )
    return claim_targets


# ---------------------------------------------------------------------------
# Registration + public entry point.
# ---------------------------------------------------------------------------


_AGG_METADATA = AtomicToolMetadata(
    name="aggregate_claims_across_sources",
    ttl_class="live-no-cache",
    source_class="claim_aggregator",
    cacheable=False,
)


_EXTRACTORS = {
    "date": _extract_dates,
    "scale": _extract_scale,
    "casualties": _extract_casualties,
    "contaminant": _extract_contaminants,
    "location": _extract_locations,
}


@register_tool(
    _AGG_METADATA,
    # Annotations: readOnlyHint=True (pure in-memory computation; no GCS or DB
    # writes), openWorldHint=False (processes caller-supplied source texts;
    # no external API calls in this tool body), destructiveHint=False,
    # idempotentHint=True (deterministic regex extraction; same inputs → same output).
)
def aggregate_claims_across_sources(
    sources: list[dict[str, Any]],
    claim_targets: list[str],
    confidence_threshold: float = 0.6,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Cross-source claim aggregation for FR-HEP news/event ingest.

    Runs deterministic regex extractors over a list of pre-fetched text sources
    and returns a per-claim best-supported value with confidence score and
    provenance URLs. Confidence is source-agreement-based: 1 source → 0.5;
    N >= 2 sources → min(0.99, 0.80 + 0.05*(N-2)). Not cached (computed fresh
    from the caller-supplied sources list on every invocation).

    When to use:
        - After fetching multiple texts about the same real-world event (news
          articles via ``web_fetch``, NWS alerts, NOAA Storm Events DB) and
          needing a single best-supported value per claim target.
        - Building the ``derived_params`` dict in the Case 2 event-ingest
          workflow (``run_model_news_event_ingest`` calls this after the
          per-source fetch chain).
        - Triggering the FR-HEP-7 "ask the user" gate when no claim target
          reaches the ``confidence_threshold``.

    When NOT to use:
        - Numeric model outputs (those carry their own typed fields from the
          engine, never routed through this surface).
        - Single-source extraction (the multi-source agreement scoring adds no
          value; call the per-target regex helpers directly).
        - Reverse-geocoding a place name to a bbox (use ``geocode_location``).

    Params:
        sources: list of dicts, each ``{"url": str, "text": str, "fetched_at": str}``.
            ``fetched_at`` is ISO-8601 UTC. Empty list returns an empty claims
            dict — not an error.
        claim_targets: subset of ``("location", "scale", "contaminant", "date",
            "casualties")``. Any other target raises ``ClaimAggInputError``.
        confidence_threshold: claims falling below this confidence retain
            their value but are flagged with ``below_threshold=True`` for
            downstream filtering. Default 0.6.

    Returns:
        A dict::

            {
              "claims": {
                "<target>": {
                  "value": str | float | dict | int | None,
                  "confidence": float,   # 0-1, source-agreement-scored
                  "supporting_sources": [url, ...],
                  "alternatives": [{"value": ..., "supporting_sources": [...]}, ...],
                  "below_threshold": bool,  # present only if True
                },
                ...
              },
              "stats": {
                "sources_consulted": int,
                "claims_resolved": int,  # # of targets where value != None
                "confidence_threshold": float,
              },
            }

    Caching: ``cacheable=False``; the output is a function of the supplied
    ``sources`` list (which the caller usually just fetched and may differ
    per call). The shim short-circuits and the dict is computed fresh on
    every invocation.

    Typed errors (FR-AS-11):
        - ``ClaimAggInputError`` (not retryable) — non-list sources, missing
          ``url``/``text``/``fetched_at`` keys, unknown claim target.

    Strategy notes (v0.1):
        - "date" — regex over ISO-8601 / long-form ("February 15, 2026") /
          US-style ("2/15/2026"). Normalized to ISO ``yyyy-mm-dd``.
        - "scale" — magnitude+unit regex (gallons, liters, tons, barrels,
          acres, hectares, cubic meters/feet, square miles/kilometers) with
          optional "thousand/million/billion" modifier. Normalized to
          ``{"value": float, "unit": str}``.
        - "casualties" — "N (people) injured/hurt/wounded/killed/dead" regex.
          Normalized to int.
        - "contaminant" — curated keyword bag (vinyl chloride, benzene,
          ammonia, ...). TENTATIVE per OQ-93-NEEDS-LLM-EXTRACTION; sprint-13
          upgrades to LLM-routed extraction so the long tail of chemical
          names is covered.
        - "location" — "Title-case Name, State" regex against the 50 US
          states. TENTATIVE per OQ-93-NEEDS-LLM-EXTRACTION; sprint-13
          upgrades to LLM-routed extraction for international + ambiguous
          place names.

    Source-agreement scoring (audit-specified):
        - 1 source -> confidence 0.5
        - 2 sources -> 0.80
        - 3 sources -> 0.85
        - N sources (N >= 2) -> min(0.99, 0.80 + 0.05*(N-2))

    Open Questions:
        - OQ-93-NEEDS-LLM-EXTRACTION — "location" + "contaminant" deterministic
          extraction misses the long tail; sprint-13 upgrades to LLM-routed
          via the agent's Gemini access.

    Cross-tool dependencies:
        Upstream (consumes):
        - ``web_fetch`` — each element of ``sources`` is typically a dict from
          ``web_fetch`` output (``url``, ``content`` → renamed ``text``,
          ``fetched_at``).
        - ``fetch_nws_event`` / ``fetch_storm_events_db`` — other upstream
          source types normalized to the ``{url, text, fetched_at}`` shape.
        Downstream (feeds):
        - ``run_model_news_event_ingest`` — calls this after the per-source
          fetch chain; the returned ``claims`` dict populates ``derived_params``
          in the ``EventIngestResult``.
        - ``geocode_location`` — the ``claims["location"]["value"]`` string is
          passed to ``geocode_location`` to derive the event bbox.
    """
    _validate_sources(sources)
    _validate_targets(claim_targets)
    if not isinstance(confidence_threshold, (int, float)) or not (
        0.0 <= confidence_threshold <= 1.0
    ):
        raise ClaimAggInputError(
            f"confidence_threshold must be a float in [0,1]; got {confidence_threshold!r}"
        )

    claims: dict[str, Any] = {}
    for target in claim_targets:
        extractor = _EXTRACTORS[target]
        # Per-source extractions for this target.
        per_source: list[tuple[str, list[tuple[str, Any]]]] = []
        for source in sources:
            mentions = extractor(source["text"])
            per_source.append((source["url"], mentions))
        claims[target] = _aggregate_target(target, per_source, confidence_threshold)

    claims_resolved = sum(1 for c in claims.values() if c["value"] is not None)
    logger.info(
        "aggregate_claims_across_sources sources=%d targets=%s resolved=%d",
        len(sources),
        claim_targets,
        claims_resolved,
    )
    return {
        "claims": claims,
        "stats": {
            "sources_consulted": len(sources),
            "claims_resolved": claims_resolved,
            "confidence_threshold": float(confidence_threshold),
        },
    }
