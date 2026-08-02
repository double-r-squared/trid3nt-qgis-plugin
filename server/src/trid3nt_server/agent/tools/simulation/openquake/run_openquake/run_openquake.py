"""Engine door: ``run_openquake`` - the read-only OpenQuake seismic-hazard concierge.

ROUTING - call this FIRST for ANY probabilistic seismic-hazard / earthquake
ground-motion question, then SELECT-THEN-CALL the template it names:
  probabilistic seismic hazard (PSHA), PGA / spectral-acceleration ground-motion
  map, "10% in 50 years" (475-year) return-period shaking, the ground-motion
  INPUT to a Pelicun earthquake damage assessment -> call ``run_openquake`` to
  LIST the available OpenQuake templates, then call the chosen ``openquake_*``
  template directly.

This door EXECUTES NOTHING (no solve, no layer). It (1) lists its engine's
registered templates (name, one-line question, required inputs, knobs) straight
from the live registry, (2) makes those templates callable for the rest of the
turn (gate expansion), and (3) briefs the model on OpenQuake fidelity and
redirects off-engine asks to the right door.

Do NOT use for:
  - landslide / ground-failure susceptibility -> run_landlab
  - structural damage / loss from a hazard -> run_pelicun
See ``mismatch_redirect`` in the return.

Determinism (Invariant 1): every field is derived from the live registry /
callable signatures / a static fidelity brief - no free generation, no
fabricated template. The door lists ONLY registered templates.

FR-DC-6: ``cacheable=False`` + ``ttl_class="live-no-cache"`` - the door must
never be served from cache because each call also drives the per-turn gate
expansion (a side effect on the turn's visible tool set).
"""

from __future__ import annotations

import inspect
import logging
import sys
from dataclasses import dataclass
from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.agent.tools import TOOL_REGISTRY, register_tool

logger = logging.getLogger(__name__)

#: The engine slug this door concierges. The door lists / gate-expands over the
#: registry's ``tier="template"`` entries whose ``engine`` matches this slug.
_ENGINE = "openquake"

#: Signature params that are plumbing, not a real user input, and MUST NOT be
#: surfaced as a template's ``required_inputs`` / ``knobs``. ``compute_class`` is
#: the workflow-dispatch compute-tier selector; underscore-prefixed params
#: (``_extra_ignored`` etc.) are absorbed kwargs.
_IGNORED_PARAMS = frozenset({"compute_class"})

#: Static fidelity brief (narrated by the LLM). OpenQuake classical PSHA -
#: real-fault (GEM Global Active Faults) source when a fault intersects the AOI,
#: else a synthetic Gutenberg-Richter area source (the source_model_kind must be
#: narrated honestly); demo-default G-R recurrence + GMPE, planning-grade hazard
#: envelopes, cloud-only (RAM-hungry containerized Batch run).
_FIDELITY_BRIEF = (
    "OpenQuake classical probabilistic seismic-hazard (PSHA) engine. Builds a "
    "site-grid hazard map: a REAL active-fault source when GEM Global Active "
    "Faults intersect the AOI (hazard peaks on the trace), else a SYNTHETIC "
    "Gutenberg-Richter area source - the returned source_model_kind "
    "(real-fault / synthetic-area) must be narrated HONESTLY. G-R recurrence + "
    "GMPE default to narrated demo values unless supplied; planning-grade "
    "envelopes, not a site-specific hazard study. Cloud-only Batch run."
)

#: Off-engine redirection map (prose the LLM narrates; it names DOORS, not
#: templates). Seismic hazard belongs here; everything else points away.
_MISMATCH_REDIRECT = {
    "landslide / ground-failure susceptibility": "run_landlab",
    "structural damage / loss from a hazard": "run_pelicun",
}

_NEXT_ACTION = (
    "SELECT-THEN-CALL: call the chosen openquake_* template directly with its "
    "required inputs."
)


@dataclass(frozen=True)
class TemplateCard:
    """Optional curated one-liner a template module MAY export as ``TEMPLATE_CARD``.

    When present on a template's module, the door prefers it over the
    signature/docstring derivation - a zero-maintenance escape hatch for a
    hand-written card. All three fields are required on the override.
    """

    question: str
    required_inputs: list[str]
    knobs: str


def _first_sentence(doc: str | None, *, max_chars: int = 200) -> str:
    """First non-empty docstring line, truncated - the template's one-line question.

    Mirrors ``categories._first_sentence`` (replicated locally to avoid a
    module-load-time import cycle: the door is imported while ``tools`` is still
    populating the registry).
    """
    if not doc:
        return ""
    for line in doc.splitlines():
        line = line.strip()
        if line:
            if len(line) > max_chars:
                return line[: max_chars - 1].rstrip() + "…"
            return line
    return ""


def _split_signature_params(fn: Any) -> tuple[list[str], list[str]]:
    """Return ``(required_inputs, knobs)`` from ``fn``'s signature.

    - ``required_inputs`` = params WITHOUT a default (the real required args),
      minus ``_IGNORED_PARAMS`` and underscore-absorbed / var-args params.
    - ``knobs`` = params WITH a default (the optional overrides), same filter.
    Never fabricates a param; on any inspection fault returns ``([], [])``.
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return [], []
    required: list[str] = []
    knobs: list[str] = []
    for name, param in sig.parameters.items():
        if param.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue
        if name.startswith("_") or name in _IGNORED_PARAMS:
            continue
        if param.default is inspect.Parameter.empty:
            required.append(name)
        else:
            knobs.append(name)
    return required, knobs


def _derive_card(entry: Any) -> dict[str, Any]:
    """Build one template card dict for a registered template ``entry``.

    Prefers a module-level ``TEMPLATE_CARD`` override (duck-typed on
    ``question`` / ``required_inputs`` / ``knobs``); otherwise derives the card
    from the callable's docstring + signature. Honest-by-construction: every
    field traces to the live registration.
    """
    fn = entry.fn
    tool_name = entry.metadata.name
    override = None
    try:
        module = sys.modules.get(getattr(fn, "__module__", "") or "")
        override = getattr(module, "TEMPLATE_CARD", None) if module else None
    except Exception:  # noqa: BLE001 -- an override lookup fault must not break listing
        override = None

    if override is not None and all(
        hasattr(override, attr) for attr in ("question", "required_inputs", "knobs")
    ):
        return {
            "tool_name": tool_name,
            "question": str(override.question),
            "required_inputs": list(override.required_inputs),
            "knobs": str(override.knobs),
        }

    required, knobs = _split_signature_params(fn)
    return {
        "tool_name": tool_name,
        "question": _first_sentence(getattr(fn, "__doc__", "") or ""),
        "required_inputs": required,
        "knobs": ", ".join(knobs),
    }


def _list_templates() -> list[dict[str, Any]]:
    """Registry-driven OpenQuake template listing (deterministic, sorted).

    Enumerates ``TOOL_REGISTRY`` for entries tagged ``engine == "openquake"`` and
    ``tier == "template"``. NO hardcoded template list: adding a template folder
    with the tags makes it appear here with zero door changes. A single template
    that fails card derivation is skipped (logged), never crashes the door.
    """
    cards: list[dict[str, Any]] = []
    for name in sorted(TOOL_REGISTRY.keys()):
        entry = TOOL_REGISTRY[name]
        meta = entry.metadata
        if getattr(meta, "engine", None) != _ENGINE:
            continue
        if getattr(meta, "tier", "general") != "template":
            continue
        try:
            cards.append(_derive_card(entry))
        except Exception:  # noqa: BLE001 -- one bad template never breaks the door
            logger.warning("run_openquake door: card derivation failed for %s", name,
                           exc_info=True)
    return cards


@register_tool(
    AtomicToolMetadata(
        name="run_openquake",
        ttl_class="live-no-cache",
        source_class="door",
        cacheable=False,
        engine="openquake",
        tier="door",
        read_only_hint=True,
        open_world_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
    ),
)
def run_openquake() -> dict[str, Any]:
    """List the OpenQuake seismic-hazard templates, then SELECT-THEN-CALL one.

    Read-only concierge for the OpenQuake engine - call this FIRST for any
    probabilistic seismic-hazard / earthquake ground-motion question (PSHA,
    PGA / spectral-acceleration map, "10% in 50 years" return-period shaking, or
    the ground-motion INPUT to a Pelicun earthquake damage assessment). It
    EXECUTES NOTHING: it returns the available ``openquake_*`` templates (each
    with its one-line question, required inputs, and knobs) and makes them
    callable for the rest of the turn. Then call the chosen template directly.

    NOT for landslide / ground-failure susceptibility (run_landlab) or
    structural damage / loss from a hazard (run_pelicun) - see
    ``mismatch_redirect``.

    Returns a read-only concierge envelope: ``engine``, ``kind``, ``templates``
    (each ``tool_name`` / ``question`` / ``required_inputs`` / ``knobs``),
    ``fidelity_brief``, ``mismatch_redirect``, ``next_action``.
    """
    return {
        "engine": _ENGINE,
        "kind": "engine_door",
        "templates": _list_templates(),
        "fidelity_brief": _FIDELITY_BRIEF,
        "mismatch_redirect": dict(_MISMATCH_REDIRECT),
        "next_action": _NEXT_ACTION,
    }
