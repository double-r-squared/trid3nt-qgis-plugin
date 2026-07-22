"""``model_groundwater_contamination_scenario`` — Case 2 composer (job-0228).

The Case 2 end-to-end higher-order workflow: it turns a news article about a
chemical / solvent spill into a rendered groundwater-contaminant plume layer.
It is the MODFLOW analogue of ``model_flood_habitat_scenario`` (Case 1) and the
downstream half of ``model_news_event_ingest`` (the sprint-12 review-gated
front half stopped before any solver; this composer picks up and runs the
solver after a confirmation gate).

Chain:

    1. INGEST + EXTRACT
       - Source is either pasted ``article_text`` or a ``source_url`` (fetched
         via the registry's ``web_fetch``).
       - Claim extraction runs the existing
         ``aggregate_claims_across_sources`` machinery for location + scale +
         date, AND two composer-level extractors the deterministic aggregator
         does not cover: the SOLVENT/contaminant detector (the aggregator's
         keyword bag is curated for the vinyl-chloride / ammonia class and does
         NOT know TCE / PCE / solvents) and the RELEASE-DURATION detector
         ("over roughly six hours").
       - Derive the four ``MODFLOWRunArgs`` forcing fields with EXPLICIT unit
         conversions:
           * scale (gallons / liters / barrels / tons) + contaminant density
             -> total released mass in kg.
           * duration (hours / days) -> duration_days.
           * release_rate_kg_s = total_mass_kg / duration_seconds.
         Plausibility CLAMPS bound the derived forcing into the physically
         meaningful range the demo aquifer can solve:
           * release_rate_kg_s in [1e-6, 100].
           * duration_days     in [0.1, 3650].
       - Geocode the derived location string -> spill point (lat, lon).

    2. CONFIRMATION BEFORE CONSEQUENCE (Invariant 9)
       - The composer emits a ``tool-payload-warning`` envelope (reusing the
         existing user-pause pattern — the only confirmation-gate envelope the
         client already renders inline in chat) carrying the derived params
         + the demo-aquifer caveat, and BLOCKS until the user confirms.
       - A MODFLOW run is a "consequence" (a solver execution, FR-AS-8 /
         Invariant 9). The submission happens ONLY after the user confirms.
       - ``confirmed=True`` is a documented bypass for programmatic / test use
         (the live-evidence harness + pytest pass it). It does NOT exist to skip
         the gate in production — the server-side confirmation hook around
         ``run_modflow_job`` is the independent fail-closed backstop.

    3. RUN + PUBLISH
       - ``run_modflow_job`` (job-0227) builds the GWF+GWT deck, runs mf6
         (Cloud Workflows, or local ``mf6`` when ``TRID3NT_MODFLOW_LOCAL=1``),
         postprocesses the UCN concentration output into an EPSG:4326 plume COG,
         publishes it, and returns a ``PlumeLayerURI`` carrying the two
         narration scalars (``max_concentration_mgl`` + ``plume_area_km2``).
       - The composer returns a ``Case2Result`` with the plume layer + a
         narrative summary dict ``{plume_area_km2, max_concentration_mgl,
         location_name}`` the agent narrates from typed fields (Invariant 1).

Invariants:
- **1. Determinism boundary: preserves.** Every narrated number comes from a
  typed field — the derived forcing dict (computed by plain arithmetic) and the
  ``PlumeLayerURI`` scalars. No LLM call anywhere in this module; the
  ``summary`` dict is built from those typed fields, not free text.
- **2. Deterministic workflows: preserves.** Straight-line Python composition
  over registered atomic tools + ``run_modflow_job``; typed-exception handling
  at the boundary.
- **8. Cancellation is first-class: preserves.** Every ``await`` (emitter
  calls, the confirmation wait, ``run_modflow_job``) is a cancel-propagation
  site; ``asyncio.CancelledError`` bubbles untouched.
- **9. Confirmation before consequence: preserves.** The MODFLOW run is gated
  behind an explicit user-confirm; the gate fails closed (cancel / timeout /
  disconnect -> no run). ``confirmed=True`` is the documented programmatic
  bypass for the test + live-evidence harnesses, not a production skip.
- **10. Minimal parameter surface: preserves.** The signature exposes intent
  (the article text / URL); the spill location, contaminant, release rate, and
  duration are EXTRACTED, and the aquifer K / porosity are demo defaults from
  the contract — the user supplies none of them.

Confirmation seam (TENTATIVE per kickoff — surfaced as
OQ-0228-CONFIRM-ENVELOPE-CHOICE): the composer reuses the
``tool-payload-warning`` / ``tool-payload-confirmation`` pair rather than the
A.4 ``confirmation-request`` / ``confirm-response`` pair, because the kickoff
says "same pattern as the payload-warning user-pause" AND the payload-warning
gate is the one the composer can drive end-to-end through an injected
confirmation hook without reaching into the server's per-solver hook. The
``confirmation-request`` pair stays the server-side fail-closed backstop around
``run_modflow_job`` itself.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Awaitable, Callable

from pydantic import Field

from trid3nt_contracts import new_ulid
from trid3nt_contracts.common import GraceModel
from trid3nt_contracts.modflow_contracts import (
    DEFAULT_AQUIFER_K_MS,
    DEFAULT_POROSITY,
    MODFLOWRunArgs,
    PlumeLayerURI,
)
from trid3nt_contracts.payload_warning import PayloadWarningEnvelopePayload
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from ..tools import TOOL_REGISTRY, register_tool

logger = logging.getLogger(
    "trid3nt_server.workflows.model_groundwater_contamination_scenario"
)

__all__ = [
    "Case2Result",
    "model_groundwater_contamination_scenario",
    "run_model_groundwater_contamination_scenario",
    "GroundwaterContaminationError",
    "GroundwaterContaminationInputError",
    "ParameterExtractionError",
    "ConfirmationDeniedError",
    "extract_spill_parameters",
    "RELEASE_RATE_MIN_KG_S",
    "RELEASE_RATE_MAX_KG_S",
    "DURATION_MIN_DAYS",
    "DURATION_MAX_DAYS",
    "CONTAMINANT_DENSITY_KG_L",
    "DEFAULT_CONTAMINANT_DENSITY_KG_L",
    "ConfirmationHook",
]


# --------------------------------------------------------------------------- #
# Case 2 result envelope
# --------------------------------------------------------------------------- #
#
# Kept LOCAL to the agent (not in ``contracts``) because the Case 2
# composer result is an agent-side composition headline, the schema package is
# concurrently edited by other Stage 2 jobs (shared-file warning), and the
# contract scope for this job is agent-only. If a future job needs this shape on
# the wire it can be promoted to ``case_results.py`` by ``schema`` then — for
# now it carries only typed fields the LLM-facing wrapper dumps with
# ``model_dump(mode="json")``. Surfaced as OQ-0228-CASE2RESULT-PROMOTION.


class Case2Result(GraceModel):
    """Return type for ``model_groundwater_contamination_scenario`` (job-0228).

    Bundles the Case 2 composer output: the published plume layer + the derived
    forcing params (with explicit unit conversions + any clamps) + a narration
    summary dict + the confirmation envelope that gated the run.

    Invariant 1 (Determinism boundary): every narrated number is a typed field.
    ``plume_layer`` carries ``max_concentration_mgl`` + ``plume_area_km2``; the
    ``summary`` dict mirrors those plus the derived release rate / duration —
    all computed, none free-generated.

    Fields:
        plume_layer: the ``PlumeLayerURI`` the MODFLOW postprocess produced.
        derived_params: the JSON-able derived-params dict (contaminant, location,
            spill point, scale, total mass, duration, release rate, clamps,
            extraction notes).
        summary: narration dict ``{location_name, contaminant, plume_area_km2,
            max_concentration_mgl, release_rate_kg_s, duration_days,
            demo_aquifer_caveat}``.
        confirmation_envelope: the parameter-confirmation envelope (payload-
            warning shape) that gated the run, serialized for the surface.
    """

    schema_version: str = "v1"

    plume_layer: PlumeLayerURI
    derived_params: dict[str, Any] = Field(default_factory=dict)
    summary: dict[str, Any] = Field(default_factory=dict)
    confirmation_envelope: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Plausibility clamps (kickoff-specified)
# --------------------------------------------------------------------------- #

#: Release-rate clamp (kg/s). The demo aquifer + GWT transport solve is stable
#: across this band; outside it the derived forcing is almost certainly an
#: extraction artifact (a mis-parsed magnitude or a zero-duration division).
RELEASE_RATE_MIN_KG_S: float = 1e-6
RELEASE_RATE_MAX_KG_S: float = 100.0

#: Duration clamp (days). 0.1 d ~= 2.4 h lower bound; 3650 d = 10 y upper bound.
DURATION_MIN_DAYS: float = 0.1
DURATION_MAX_DAYS: float = 3650.0


# --------------------------------------------------------------------------- #
# Unit-conversion constants
# --------------------------------------------------------------------------- #

#: US liquid gallon -> liter.
LITERS_PER_GALLON: float = 3.785411784
#: US (short) barrel of liquid petroleum -> liter (42 US gallons).
LITERS_PER_BARREL: float = 42.0 * LITERS_PER_GALLON
#: Seconds per day.
SECONDS_PER_DAY: float = 86_400.0
#: Hours per day.
HOURS_PER_DAY: float = 24.0

#: Contaminant liquid densities (kg/L) for the volume->mass conversion. Curated
#: for the solvent / hydrocarbon class the Case 2 demo targets. Keyed by the
#: lowercase contaminant name the extractor normalizes to. A contaminant not in
#: this table falls back to ``DEFAULT_CONTAMINANT_DENSITY_KG_L`` (water-like).
CONTAMINANT_DENSITY_KG_L: dict[str, float] = {
    # Chlorinated solvents (the canonical groundwater-plume contaminants).
    "trichloroethylene": 1.46,
    "tce": 1.46,
    "tetrachloroethylene": 1.62,
    "perchloroethylene": 1.62,
    "pce": 1.62,
    "carbon tetrachloride": 1.59,
    "1,1,1-trichloroethane": 1.34,
    "dichloromethane": 1.33,
    "methylene chloride": 1.33,
    "chloroform": 1.49,
    "vinyl chloride": 0.91,
    # Aromatics / fuels.
    "benzene": 0.876,
    "toluene": 0.867,
    "xylene": 0.864,
    "styrene": 0.906,
    "methanol": 0.792,
    "ethanol": 0.789,
    "ethylene glycol": 1.113,
    "gasoline": 0.745,
    "diesel": 0.85,
    "petroleum": 0.85,
    "crude oil": 0.87,
    "sulfuric acid": 1.83,
    "hydrochloric acid": 1.18,
}

#: Default density (kg/L) when the contaminant is unknown — water-like. Keeps
#: the mass derivation defined for any extracted contaminant string.
DEFAULT_CONTAMINANT_DENSITY_KG_L: float = 1.0


# --------------------------------------------------------------------------- #
# Composer-level extractors (cover the gaps in the deterministic aggregator)
# --------------------------------------------------------------------------- #

#: Solvent / contaminant detector. The aggregator's keyword bag is curated for
#: the vinyl-chloride / ammonia / fuels class and does NOT include the
#: chlorinated solvents (TCE / PCE / DCM) that are the textbook groundwater-
#: plume contaminants. We add a composer-level bag covering those, ordered so
#: longer (more specific) names win over substrings. Each entry maps a regex to
#: the normalized contaminant name (the density-table key).
_SOLVENT_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("trichloroethylene", "trichloroethylene"),
    ("trichloroethene", "trichloroethylene"),
    ("tetrachloroethylene", "tetrachloroethylene"),
    ("tetrachloroethene", "tetrachloroethylene"),
    ("perchloroethylene", "tetrachloroethylene"),
    ("carbon tetrachloride", "carbon tetrachloride"),
    ("1,1,1-trichloroethane", "1,1,1-trichloroethane"),
    ("methylene chloride", "dichloromethane"),
    ("dichloromethane", "dichloromethane"),
    ("chloroform", "chloroform"),
    # Aromatic solvents the aggregator's bag misses but we have densities for.
    ("toluene", "toluene"),
    ("xylene", "xylene"),
    # Acronyms — word-boundary matched so "PCE" inside another token is ignored.
    (r"\btce\b", "trichloroethylene"),
    (r"\bpce\b", "tetrachloroethylene"),
    (r"\bdcm\b", "dichloromethane"),
)

#: Reuse the aggregator's curated bag for the contaminants it DOES know (so a
#: "benzene"/"ammonia" article still resolves a contaminant through this
#: composer). Imported lazily inside the extractor to avoid an import cycle.

#: Release-duration detector. Matches "<n> hours/hrs/h" or "<n> days" optionally
#: preceded by an "over/within/across/during/for/in" lead-in. Captures the
#: number + the unit so the composer converts to days.
_DURATION_RE = re.compile(
    r"(?:over|within|across|during|for|in|about|roughly|approximately|some)?\s*"
    r"(?:a\s+(?:period|span|stretch)\s+of\s+)?"  # "over a period of 2 days"
    r"(?:about|roughly|approximately|some|nearly|almost)?\s*"
    r"(\d+(?:\.\d+)?)\s*"
    r"(hours?|hrs?|h|days?|d)\b",
    re.IGNORECASE,
)

#: Spelled-out small numbers the duration detector also accepts ("six hours").
_NUMBER_WORDS: dict[str, float] = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "twenty-four": 24,
    "forty-eight": 48,
    "half": 0.5,
}
_DURATION_WORD_RE = re.compile(
    r"(?:over|within|across|during|for|in|about|roughly|approximately|some)\s+"
    r"(?:a\s+(?:period|span|stretch)\s+of\s+)?"  # "over a period of two days"
    r"(?:about|roughly|approximately|some|nearly|almost)?\s*"
    r"(" + "|".join(re.escape(w) for w in _NUMBER_WORDS) + r")\s+"
    r"(hours?|hrs?|days?)\b",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- #
# Typed errors (FR-AS-11)
# --------------------------------------------------------------------------- #


class GroundwaterContaminationError(RuntimeError):
    """Base class for ``model_groundwater_contamination_scenario`` failures."""

    error_code: str = "GROUNDWATER_CONTAMINATION_ERROR"
    retryable: bool = False


class GroundwaterContaminationInputError(GroundwaterContaminationError):
    """Caller passed neither ``article_text`` nor ``source_url`` (or both empty)."""

    error_code = "GROUNDWATER_CONTAMINATION_INPUT_INVALID"


class ParameterExtractionError(GroundwaterContaminationError):
    """The article text did not yield the spill parameters MODFLOW requires.

    Raised when a REQUIRED forcing field cannot be derived: no contaminant, no
    release scale, no release duration, or no geocodable location. The agent
    narrates this honestly (it cannot model a spill it cannot parameterize) and
    may ``request_clarification`` for the missing field.
    """

    error_code = "GROUNDWATER_PARAM_EXTRACTION_FAILED"


class ConfirmationDeniedError(GroundwaterContaminationError):
    """The user declined / timed out at the parameter-confirmation gate.

    Confirmation fails closed (Invariant 9): no MODFLOW run proceeds. The agent
    narrates that the run was not started and the derived params are available
    for the user to revise.
    """

    error_code = "GROUNDWATER_CONFIRMATION_DENIED"


# --------------------------------------------------------------------------- #
# Confirmation-hook seam
# --------------------------------------------------------------------------- #

#: A confirmation hook is an awaitable the server injects so the composer can
#: pause for the user without reaching into the WebSocket directly. It receives
#: the ``PayloadWarningEnvelopePayload`` describing the derived params + caveat
#: and returns ``True`` (proceed) / ``False`` (deny). When no hook is injected
#: AND ``confirmed`` is not True, the gate fails closed (denies) — a missing
#: hook must never silently authorize a solver run.
ConfirmationHook = Callable[[PayloadWarningEnvelopePayload], Awaitable[bool]]


# --------------------------------------------------------------------------- #
# Registry / fetch helpers
# --------------------------------------------------------------------------- #


def _registry_fn(name: str) -> Any:
    """Resolve ``name`` to the registered tool callable (registry seam)."""
    entry = TOOL_REGISTRY.get(name)
    if entry is None:
        raise GroundwaterContaminationError(
            f"required atomic tool {name!r} is not registered "
            f"(known tools: {sorted(TOOL_REGISTRY)[:8]}...)"
        )
    return entry.fn


def _fetch_article_text(source_url: str) -> str:
    """Fetch an article URL via ``web_fetch`` and return its main text."""
    fn = _registry_fn("web_fetch")
    result = fn(url=source_url, extract="main_text")
    if isinstance(result, dict):
        title = result.get("title")
        content = result.get("content")
        if isinstance(content, str):
            return f"{title}\n{content}" if title else content
        return str(content or title or "")
    return str(result)


# --------------------------------------------------------------------------- #
# Parameter extraction (pure; unit-testable without an emitter or solver)
# --------------------------------------------------------------------------- #


def _extract_contaminant(text: str) -> str | None:
    """Return the normalized contaminant name, or None if none is found.

    First runs the composer's solvent bag (chlorinated solvents the aggregator
    misses), then falls back to the aggregator's curated keyword bag (benzene /
    ammonia / fuels). Longer / more-specific names win.
    """
    lowered = text.lower()
    for pattern, normalized in _SOLVENT_KEYWORDS:
        if pattern.startswith(r"\b"):
            if re.search(pattern, lowered):
                return normalized
        elif pattern in lowered:
            return normalized
    # Fall back to the aggregator's bag (covers the non-solvent contaminants).
    from ..tools.processing.aggregate_claims_across_sources import _extract_contaminants

    hits = _extract_contaminants(text)
    if hits:
        # _extract_contaminants returns [(raw, normalized_keyword), ...].
        return hits[0][1]
    return None


def _extract_duration_days(text: str) -> float | None:
    """Return the release duration in DAYS, or None if no duration is found.

    Recognizes numeric ("six hours" -> spelled, "6 hours", "3 days") forms and
    converts hours -> days. The first plausible match wins.
    """
    # Spelled-out numbers first ("over roughly six hours").
    m = _DURATION_WORD_RE.search(text)
    if m:
        num = _NUMBER_WORDS[m.group(1).lower()]
        unit = m.group(2).lower()
        return _duration_to_days(num, unit)
    # Numeric forms.
    for match in _DURATION_RE.finditer(text):
        num = float(match.group(1))
        unit = match.group(2).lower()
        # Guard the bare "in 6 h" false-positives on tiny values being noise:
        # any positive number with an explicit time unit is accepted.
        if num > 0:
            return _duration_to_days(num, unit)
    return None


def _best_location(loc_hits: list[tuple[str, str]]) -> str:
    """Pick the cleanest 'City, State' candidate from the aggregator's hits.

    The aggregator's location regex can over-match across a newline (joining a
    headline phrase to an all-caps dateline, e.g. ``"Toward Aquifer\\n\\nTWIN
    FALLS, Idaho"``). Geocoding such a string fails or resolves wrong. We score
    each candidate and prefer ones that look like a real place name: no embedded
    newline, a city portion of at most three words, and (as a final tiebreak)
    the shortest candidate. The candidate's normalized value (``hit[1]``) is
    what we score + return.
    """

    def _score(norm: str) -> tuple[int, int, int]:
        has_newline = 1 if ("\n" in norm or "\r" in norm) else 0
        city = norm.split(",", 1)[0].strip()
        city_words = len(city.split())
        too_many_words = 1 if city_words > 3 else 0
        return (has_newline, too_many_words, len(norm))

    best = min((h[1] for h in loc_hits), key=_score)
    # Defensive: if the chosen value still carries a newline, keep only the tail
    # "City, State" segment after the last newline.
    if "\n" in best or "\r" in best:
        tail = re.split(r"[\r\n]+", best)[-1].strip()
        if "," in tail:
            best = tail
    return best


def _duration_to_days(num: float, unit: str) -> float:
    """Convert (num, unit) where unit is an hour/day token into days."""
    u = unit.lower()
    if u.startswith("d"):
        return num
    # hours / hrs / hr / h
    return num / HOURS_PER_DAY


def _scale_to_mass_kg(
    scale_value: float, scale_unit: str, density_kg_l: float
) -> float | None:
    """Convert an extracted (value, unit) release scale into a mass in kg.

    Handles volume units (gallons / liters / barrels) via density and direct
    mass units (tons / tonnes). Returns None for an unconvertible unit (e.g.
    "acres" — an area, not a release amount).
    """
    u = scale_unit.lower().rstrip("s")  # singularize: "gallons" -> "gallon"
    if u in ("gallon",):
        liters = scale_value * LITERS_PER_GALLON
        return liters * density_kg_l
    if u in ("liter", "litre"):
        return scale_value * density_kg_l
    if u in ("barrel",):
        liters = scale_value * LITERS_PER_BARREL
        return liters * density_kg_l
    if u in ("tonne",):  # metric ton
        return scale_value * 1000.0
    if u in ("ton",):  # US short ton -> kg
        return scale_value * 907.18474
    # cubic meter / cubic feet are possible but rarely a spill scale; treat a
    # cubic meter as 1000 L for completeness.
    if "cubic meter" in scale_unit.lower():
        return scale_value * 1000.0 * density_kg_l
    return None


def _clamp(value: float, lo: float, hi: float) -> tuple[float, bool]:
    """Clamp ``value`` into [lo, hi]; return (clamped, was_clamped)."""
    if value < lo:
        return lo, True
    if value > hi:
        return hi, True
    return value, False


def extract_spill_parameters(
    text: str,
    *,
    geocode: bool = True,
) -> dict[str, Any]:
    """Derive the four MODFLOW forcing fields from a spill article's text.

    Pure function (no emitter, no solver) so the unit-conversion + clamp logic
    is independently testable. Runs the aggregator for location + scale, the
    composer extractors for contaminant + duration, applies the explicit unit
    conversions, clamps the forcing into the plausibility band, and (optionally)
    geocodes the location to a spill point.

    Args:
        text: the article body.
        geocode: when True, call ``geocode_location`` to resolve the location
            string to a (lat, lon) point. When False (unit tests), the point is
            left None and the location string is returned for assertion.

    Returns a dict::

        {
          "contaminant": str,
          "contaminant_density_kg_l": float,
          "location_name": str,
          "spill_location_latlon": (lat, lon) | None,
          "scale_value": float, "scale_unit": str,
          "total_mass_kg": float,
          "duration_days": float,
          "release_rate_kg_s": float,
          "clamps_applied": [ "release_rate" | "duration", ... ],
          "extraction_notes": [ str, ... ],
        }

    Raises:
        ParameterExtractionError: a REQUIRED field (contaminant / scale /
            duration / geocodable location) could not be derived.
    """
    from ..tools.processing.aggregate_claims_across_sources import (
        _extract_locations,
        _extract_scale,
    )

    notes: list[str] = []

    # --- contaminant ---
    contaminant = _extract_contaminant(text)
    if not contaminant:
        raise ParameterExtractionError(
            "could not identify a contaminant in the article text; "
            "MODFLOW needs a named contaminant to parameterize transport."
        )
    density = CONTAMINANT_DENSITY_KG_L.get(
        contaminant, DEFAULT_CONTAMINANT_DENSITY_KG_L
    )
    if contaminant not in CONTAMINANT_DENSITY_KG_L:
        notes.append(
            f"contaminant {contaminant!r} density unknown; assumed water-like "
            f"({DEFAULT_CONTAMINANT_DENSITY_KG_L} kg/L)"
        )

    # --- scale -> mass ---
    scale_hits = _extract_scale(text)
    mass_kg: float | None = None
    scale_value = scale_unit = None
    for _raw, scale in scale_hits:
        m = _scale_to_mass_kg(scale["value"], scale["unit"], density)
        if m is not None and m > 0:
            mass_kg = m
            scale_value = float(scale["value"])
            scale_unit = str(scale["unit"])
            break
    if mass_kg is None:
        raise ParameterExtractionError(
            "could not extract a convertible release amount (gallons / liters / "
            "barrels / tons) from the article text."
        )

    # --- duration ---
    duration_days_raw = _extract_duration_days(text)
    if duration_days_raw is None:
        raise ParameterExtractionError(
            "could not extract a release duration (hours / days) from the "
            "article text; the release rate is mass / duration and needs both."
        )

    # --- location ---
    loc_hits = _extract_locations(text)
    if not loc_hits:
        raise ParameterExtractionError(
            "could not extract a 'City, State' location from the article text."
        )
    location_name = _best_location(loc_hits)

    # --- unit conversions + clamps ---
    duration_days, dur_clamped = _clamp(
        duration_days_raw, DURATION_MIN_DAYS, DURATION_MAX_DAYS
    )
    duration_seconds = duration_days * SECONDS_PER_DAY
    release_rate_raw = mass_kg / duration_seconds
    release_rate_kg_s, rate_clamped = _clamp(
        release_rate_raw, RELEASE_RATE_MIN_KG_S, RELEASE_RATE_MAX_KG_S
    )

    clamps: list[str] = []
    if dur_clamped:
        clamps.append("duration")
        notes.append(
            f"duration {duration_days_raw:.4g} d clamped to "
            f"[{DURATION_MIN_DAYS}, {DURATION_MAX_DAYS}] d"
        )
    if rate_clamped:
        clamps.append("release_rate")
        notes.append(
            f"release rate {release_rate_raw:.4g} kg/s clamped to "
            f"[{RELEASE_RATE_MIN_KG_S:g}, {RELEASE_RATE_MAX_KG_S:g}] kg/s"
        )

    # --- geocode (optional) ---
    spill_latlon: tuple[float, float] | None = None
    if geocode:
        try:
            geocode_fn = _registry_fn("geocode_location")
            geo = geocode_fn(location_name)
        except Exception as exc:  # noqa: BLE001 — geocode failure is fatal here
            raise ParameterExtractionError(
                f"geocode_location({location_name!r}) failed: {exc}; "
                "MODFLOW needs a spill point."
            ) from exc
        lat = geo.get("latitude") if isinstance(geo, dict) else None
        lon = geo.get("longitude") if isinstance(geo, dict) else None
        if lat is None or lon is None:
            raise ParameterExtractionError(
                f"geocode_location({location_name!r}) returned no centroid "
                f"lat/lon; cannot place the spill."
            )
        spill_latlon = (float(lat), float(lon))

    return {
        "contaminant": contaminant,
        "contaminant_density_kg_l": density,
        "location_name": location_name,
        "spill_location_latlon": spill_latlon,
        "scale_value": scale_value,
        "scale_unit": scale_unit,
        "total_mass_kg": mass_kg,
        "duration_days": duration_days,
        "duration_days_raw": duration_days_raw,
        "release_rate_kg_s": release_rate_kg_s,
        "release_rate_kg_s_raw": release_rate_raw,
        "clamps_applied": clamps,
        "extraction_notes": notes,
    }


# --------------------------------------------------------------------------- #
# Confirmation envelope composition
# --------------------------------------------------------------------------- #


def _build_confirmation_envelope(
    derived: dict[str, Any], run_args: MODFLOWRunArgs
) -> PayloadWarningEnvelopePayload:
    """Compose the parameter-confirmation envelope (payload-warning pattern).

    Carries the derived forcing params + the demo-aquifer caveat in
    ``recommendation`` and the structured forcing in ``tool_args`` so the client
    can render them. ``estimated_mb`` / ``threshold_mb`` are 0 — this is a
    parameter gate, not a payload-size gate (no cost theater, Invariant 9; the
    only numbers are the structured forcing fields).
    """
    lat, lon = run_args.spill_location_latlon
    caveat = (
        f"Demo aquifer parameterization (K={DEFAULT_AQUIFER_K_MS:g} m/s, "
        f"porosity={DEFAULT_POROSITY:g}) — NOT site-specific hydrogeology. "
        f"Confirm to run the MODFLOW groundwater-plume simulation for "
        f"{run_args.contaminant} near {derived['location_name']}."
    )
    notes = derived.get("extraction_notes") or []
    if notes:
        caveat = caveat + " Extraction notes: " + "; ".join(notes)
    return PayloadWarningEnvelopePayload(
        warning_id=new_ulid(),
        tool_name="run_modflow_job",
        tool_args={
            "contaminant": run_args.contaminant,
            "location_name": derived["location_name"],
            "spill_location_latlon": [lat, lon],
            "release_rate_kg_s": run_args.release_rate_kg_s,
            "duration_days": run_args.duration_days,
            "aquifer_k_ms": run_args.aquifer_k_ms,
            "porosity": run_args.porosity,
            "total_mass_kg": derived.get("total_mass_kg"),
            "scale": f"{derived.get('scale_value')} {derived.get('scale_unit')}",
            "clamps_applied": derived.get("clamps_applied", []),
        },
        estimated_mb=0.0,
        threshold_mb=0.0,
        recommendation=caveat[:512],
        options=["proceed", "cancel"],
    )


# --------------------------------------------------------------------------- #
# Summary text (deterministic; never LLM-generated)
# --------------------------------------------------------------------------- #


def _build_summary(
    derived: dict[str, Any], plume: PlumeLayerURI
) -> dict[str, Any]:
    """Build the narrative summary dict the agent narrates from typed fields."""
    return {
        "location_name": derived["location_name"],
        "contaminant": derived["contaminant"],
        "plume_area_km2": plume.plume_area_km2,
        "max_concentration_mgl": plume.max_concentration_mgl,
        "release_rate_kg_s": derived["release_rate_kg_s"],
        "duration_days": derived["duration_days"],
        "demo_aquifer_caveat": (
            f"Aquifer K={DEFAULT_AQUIFER_K_MS:g} m/s, porosity={DEFAULT_POROSITY:g} "
            "are demo defaults, not site-specific hydrogeology."
        ),
    }


# --------------------------------------------------------------------------- #
# The composer
# --------------------------------------------------------------------------- #


async def model_groundwater_contamination_scenario(
    article_text: str | None = None,
    source_url: str | None = None,
    *,
    confirmed: bool = False,
    confirmation_hook: ConfirmationHook | None = None,
    aquifer_k_ms: float | None = None,
    porosity: float | None = None,
    compute_class: str = "standard",
    pipeline_emitter: Any | None = None,
) -> Case2Result:
    """Compose news -> claim extraction -> MODFLOW -> plume (Case 2).

    Args:
        article_text: pasted article body (preferred for the demo / tests).
        source_url: a news URL fetched via ``web_fetch`` when ``article_text``
            is not supplied. Exactly one of the two must be provided.
        confirmed: documented programmatic / test bypass for the confirmation
            gate. When True the MODFLOW run proceeds without emitting a
            confirmation envelope. Production callers leave this False and supply
            a ``confirmation_hook`` instead.
        confirmation_hook: an awaitable the server injects to pause for the user
            (it renders the confirmation envelope and returns the user's
            decision). When None AND ``confirmed`` is False, the gate FAILS
            CLOSED — no run proceeds (Invariant 9).
        aquifer_k_ms / porosity: optional overrides for the demo-aquifer
            defaults (narrated as demo values by the agent).
        compute_class: FR-CE-3 compute class for the MODFLOW run.
        pipeline_emitter: optional PipelineEmitter for live progress cards.

    Returns:
        ``Case2Result`` carrying the ``PlumeLayerURI`` + the derived-params dict
        + the narrative summary dict ``{plume_area_km2, max_concentration_mgl,
        location_name, ...}``.

    Raises:
        GroundwaterContaminationInputError: neither / both of article_text /
            source_url supplied.
        ParameterExtractionError: a required forcing field could not be derived.
        ConfirmationDeniedError: the user declined / timed out at the gate.
        Propagates ``asyncio.CancelledError`` from any await (Invariant 8).
    """
    # --- input validation: exactly one source form ---
    has_text = bool(article_text and article_text.strip())
    has_url = bool(source_url and source_url.strip())
    if has_text == has_url:  # both or neither
        raise GroundwaterContaminationInputError(
            "supply exactly one of article_text or source_url "
            f"(got article_text={has_text}, source_url={has_url})."
        )

    # --- Stage 1: ingest ---
    if has_url:
        text = await _maybe_emit(
            pipeline_emitter,
            name=f"Fetch article: {source_url[:60]}",  # type: ignore[index]
            tool_name="web_fetch",
            invoke=lambda: _fetch_article_text(source_url),  # type: ignore[arg-type]
        )
    else:
        text = article_text  # type: ignore[assignment]
    if not text or not str(text).strip():
        raise ParameterExtractionError("article text is empty after ingest.")

    # --- Stage 2: extract + convert + clamp + geocode ---
    derived = await _maybe_emit(
        pipeline_emitter,
        name="Extract spill parameters",
        tool_name="aggregate_claims_across_sources",
        invoke=lambda: extract_spill_parameters(str(text), geocode=True),
    )
    logger.info(
        "case2 extracted contaminant=%r location=%r rate=%.6g kg/s duration=%.4g d "
        "clamps=%s",
        derived["contaminant"],
        derived["location_name"],
        derived["release_rate_kg_s"],
        derived["duration_days"],
        derived["clamps_applied"],
    )

    # --- assemble + validate the forcing contract ---
    kwargs: dict[str, Any] = dict(
        spill_location_latlon=derived["spill_location_latlon"],
        contaminant=derived["contaminant"],
        release_rate_kg_s=derived["release_rate_kg_s"],
        duration_days=derived["duration_days"],
    )
    if aquifer_k_ms is not None:
        kwargs["aquifer_k_ms"] = float(aquifer_k_ms)
    if porosity is not None:
        kwargs["porosity"] = float(porosity)
    try:
        run_args = MODFLOWRunArgs(**kwargs)
    except Exception as exc:  # noqa: BLE001 — pydantic ValidationError
        raise ParameterExtractionError(
            f"derived parameters failed MODFLOWRunArgs validation: {exc}"
        ) from exc

    # --- Stage 3: CONFIRMATION BEFORE CONSEQUENCE (Invariant 9) ---
    envelope = _build_confirmation_envelope(derived, run_args)
    if not confirmed:
        proceed = False
        if confirmation_hook is not None:
            proceed = bool(await confirmation_hook(envelope))
        if not proceed:
            logger.info(
                "case2 confirmation denied / no hook; MODFLOW run NOT started "
                "(fail-closed) location=%r",
                derived["location_name"],
            )
            raise ConfirmationDeniedError(
                "MODFLOW run not started: the parameter-confirmation gate was "
                "not approved (declined, timed out, or no confirmation channel "
                "was available)."
            )

    # --- Stage 4: run MODFLOW -> publish -> PlumeLayerURI ---
    run_modflow_fn = _registry_fn("run_modflow_job")
    result = await _maybe_emit(
        pipeline_emitter,
        name=f"Model groundwater plume [{derived['contaminant']}]",
        tool_name="run_modflow_job",
        invoke=lambda: run_modflow_fn(
            spill_location_latlon=run_args.spill_location_latlon,
            contaminant=run_args.contaminant,
            release_rate_kg_s=run_args.release_rate_kg_s,
            duration_days=run_args.duration_days,
            aquifer_k_ms=run_args.aquifer_k_ms,
            porosity=run_args.porosity,
            compute_class=compute_class,
        ),
    )
    if not isinstance(result, PlumeLayerURI):
        # run_modflow_job returns an error dict on failure (honest narration).
        error_code = "MODFLOW_RUN_FAILED"
        error_message = "MODFLOW run did not produce a plume layer"
        if isinstance(result, dict):
            error_code = result.get("error_code", error_code)
            error_message = result.get("error_message", error_message)
        raise GroundwaterContaminationError(
            f"{error_code}: {error_message}"
        )

    plume = result
    summary = _build_summary(derived, plume)
    logger.info(
        "case2 complete location=%r plume_area_km2=%.6g max_concentration_mgl=%.6g",
        derived["location_name"],
        plume.plume_area_km2,
        plume.max_concentration_mgl,
    )

    return Case2Result(
        plume_layer=plume,
        derived_params=_jsonable_derived(derived),
        summary=summary,
        confirmation_envelope=envelope.model_dump(mode="json"),
    )


def _jsonable_derived(derived: dict[str, Any]) -> dict[str, Any]:
    """Return a JSON-serializable copy of the derived-params dict.

    Tuples (``spill_location_latlon``) become lists so the dict survives
    ``model_dump(mode="json")`` on the wrapper surface.
    """
    out = dict(derived)
    loc = out.get("spill_location_latlon")
    if isinstance(loc, tuple):
        out["spill_location_latlon"] = list(loc)
    return out


# --------------------------------------------------------------------------- #
# Pipeline-emitter helper (mirror model_flood_habitat_scenario)
# --------------------------------------------------------------------------- #


async def _maybe_emit(
    emitter: Any | None,
    *,
    name: str,
    tool_name: str,
    invoke: Any,
) -> Any:
    """Run ``invoke()`` through ``emitter.emit_tool_call`` if given, else direct.

    Synchronous callables are awaited transparently by ``emit_tool_call``; we
    mirror that for the no-emitter path so the composer body can stay agnostic.
    """
    if emitter is not None:
        return await emitter.emit_tool_call(
            name=name,
            tool_name=tool_name,
            invoke=invoke,
        )
    result = invoke()
    if asyncio.iscoroutine(result):
        result = await result
    return result


# --------------------------------------------------------------------------- #
# LLM-exposed thin atomic-tool wrapper (workflow_dispatch source class)
# --------------------------------------------------------------------------- #


_RUN_GROUNDWATER_METADATA = AtomicToolMetadata(
    name="run_model_groundwater_contamination_scenario",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
)


@register_tool(
    _RUN_GROUNDWATER_METADATA,
    # readOnlyHint=False (submits a solver run), openWorldHint=False (intra-GCP
    # / local mf6), destructiveHint=False (writes a new runs/ prefix),
    # idempotentHint=False (each call mints a new run + Workflow execution).
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def run_model_groundwater_contamination_scenario(
    article_text: str | None = None,
    source_url: str | None = None,
    aquifer_k_ms: float | None = None,
    porosity: float | None = None,
    compute_class: str = "standard",
    # job-0241: server-managed confirmation flag. The solver-confirm gate in
    # server.py strips any LLM-supplied value and injects True only after the
    # user approves the derived parameters. Default False = fail-closed.
    confirmed: bool = False,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Model a groundwater contamination plume from a spill news article (Case 2).

    Turns a news article describing a chemical / solvent spill into a rendered
    groundwater-contaminant plume layer: it extracts the spill location,
    contaminant, release amount, and release duration from the article text;
    derives the MODFLOW forcing (mass via density, release rate = mass /
    duration) with plausibility clamps; CONFIRMS the derived parameters with the
    user (confirmation-before-consequence — a solver run is a consequence);
    then runs the MODFLOW 6 + MF6-GWT groundwater-transport model and publishes
    the plume.

    Use this when:
        - The user pastes (or links) a news article about a chemical spill,
          tanker leak, solvent release, or groundwater-contamination incident
          and asks to model the resulting plume.
        - A spill story names a place + a contaminant + an amount + a duration
          and the user wants "how far does it spread / how concentrated."

    Do NOT use this for:
        - Surface-water / inundation flooding (use ``run_model_flood_scenario``
          — that is SFINCS).
        - A spill with explicit numeric parameters already in hand (call
          ``run_modflow_job`` directly with the forcing fields).
        - Ingesting a news event WITHOUT modeling it (use
          ``run_model_news_event_ingest`` — it stops before any solver).

    Params:
        article_text: the pasted article body. Supply this OR ``source_url``.
        source_url: a news article URL (fetched via ``web_fetch``). Supply this
            OR ``article_text`` — exactly one.
        aquifer_k_ms: optional hydraulic-conductivity override (m/s). Defaults
            to the demo value (narrate as a demo default).
        porosity: optional effective-porosity override. Defaults to the demo
            value (narrate as a demo default).
        compute_class: FR-CE-3 compute class. Default ``"standard"``.

    Returns:
        A JSON dict (``Case2Result.model_dump(mode="json")``) with the
        ``plume_layer`` (a ``PlumeLayerURI`` carrying ``max_concentration_mgl``
        + ``plume_area_km2`` — the agent narrates these typed numbers, never
        invents them), the ``derived_params`` (with explicit unit conversions +
        any clamps applied), the ``summary`` narration dict, and the
        ``confirmation_envelope`` that gated the run. On a recoverable failure
        (extraction / confirmation / solver) the tool raises a typed error the
        agent narrates honestly.

    Confirmation-before-consequence (Invariant 9): the MODFLOW run is gated
    behind a user-confirm. The server's solver-confirm gate
    (``server.SOLVER_CONFIRM_TOOLS`` → ``_gate_on_solver_confirm``, job-0241)
    runs the pure extraction, shows the user the derived forcing on a
    ``tool-payload-warning`` card, and injects ``confirmed=True`` only on an
    explicit proceed. Without that injection this wrapper fails closed.

    FR-DC-6: ``cacheable=False`` + ``ttl_class="live-no-cache"`` +
    ``source_class="workflow_dispatch"`` — the cache shim is NOT invoked.

    Cross-tool dependencies:
        Upstream (step chain): ``web_fetch`` (when ``source_url`` is given),
        ``aggregate_claims_across_sources`` (location + scale extraction),
        ``geocode_location`` (location -> spill point), ``run_modflow_job``
        (deck build -> mf6 -> postprocess -> publish -> ``PlumeLayerURI``).
    """
    # job-0241 (Stage 3 live-gate fix): ``confirmed`` is injected as True by the
    # server-side solver-confirm gate (server.SOLVER_CONFIRM_TOOLS) ONLY after
    # the user approves the derived parameters on the tool-payload-warning
    # card. Default False = fail-closed — the previous hardcoded
    # ``confirmed=True`` relied on a "server hook around run_modflow_job" that
    # never existed, and the live Case 2 acceptance (job-0235) proved the
    # solver ran with zero user confirmation. The server also STRIPS any
    # LLM-supplied ``confirmed`` before gating, so Gemini cannot self-approve.
    result = await model_groundwater_contamination_scenario(
        article_text=article_text,
        source_url=source_url,
        confirmed=confirmed,
        aquifer_k_ms=aquifer_k_ms,
        porosity=porosity,
        compute_class=compute_class,
        pipeline_emitter=None,
    )
    return result.model_dump(mode="json")
