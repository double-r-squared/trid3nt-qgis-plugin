"""Engine door: ``run_swan`` - the read-only SWAN nearshore-wave concierge.

ROUTING - call this FIRST for ANY nearshore spectral wave-field question, then
SELECT-THEN-CALL the template it names:
  significant wave height (Hs) / peak period (Tp) / mean direction (Dir) over a
  coastal AOI, a defensible engineering-grade wave climate, buoy-validation wave
  field, overtopping-input wave field, "show me the incoming waves", or a
  standalone SWAN run to COMPARE against the SFINCS+SnapWave wave field -> call
  ``run_swan`` to LIST the available SWAN templates, then call the chosen
  ``swan_*`` template directly.

This door EXECUTES NOTHING (no solve, no layer). It (1) lists its engine's
registered templates (name, one-line question, required inputs, knobs) straight
from the live registry, (2) makes those templates callable for the rest of the
turn (gate expansion), and (3) briefs the model on SWAN fidelity and redirects
off-engine asks to the right door.

Do NOT use for:
  - compound-flood / surge / pluvial / riverine INUNDATION depth -> run_sfincs
  - tsunami / dam-break / surge RUN-UP inundation -> run_geoclaw
  - urban storm-sewer / pipe-network flooding -> run_swmm
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

from trid3nt_server.tools import TOOL_REGISTRY, register_tool

logger = logging.getLogger(__name__)

#: The engine slug this door concierges. The door lists / gate-expands over the
#: registry's ``tier="template"`` entries whose ``engine`` matches this slug.
_ENGINE = "swan"

#: Signature params that are plumbing, not a real user input, and MUST NOT be
#: surfaced as a template's ``required_inputs`` / ``knobs``. ``compute_class`` is
#: the workflow-dispatch compute-tier selector; underscore-prefixed params
#: (``_extra_ignored`` etc.) are absorbed kwargs.
_IGNORED_PARAMS = frozenset({"compute_class", "project_id", "session_id"})

#: Static fidelity brief (narrated by the LLM). SWAN 3rd-gen phase-averaged
#: spectral nearshore wave engine - Hs / Tp / Dir over real bathymetry; the
#: ADDITIVE defensible wave field (standalone or to compare against
#: SFINCS+SnapWave). Batch-only GPL Fortran solver, planning/engineering-grade.
_FIDELITY_BRIEF = (
    "SWAN (Simulating WAves Nearshore) 3rd-generation PHASE-AVERAGED spectral "
    "wave engine (significant wave height Hs / peak period Tp / mean direction "
    "Dir over real nearshore bathymetry; wind-sea growth, swell, depth-induced "
    "shoaling + breaking). The DEFENSIBLE standalone wave field - run it on its "
    "own or to COMPARE against the fast SFINCS+SnapWave in-model wave setup on "
    "the SAME case. Requires real below-datum bathymetry (an all-land DEM "
    "no-ops). Batch-only Fortran solver; planning / engineering-grade wave "
    "field, NOT a compound-flood inundation solver."
)

#: Off-engine redirection map (prose the LLM narrates; it names DOORS, not
#: templates). A nearshore spectral wave field belongs here; inundation depth /
#: run-up point away.
_MISMATCH_REDIRECT = {
    "compound-flood / surge / pluvial / riverine inundation depth": "run_sfincs",
    "tsunami / dam-break / surge run-up inundation": "run_geoclaw",
    "urban storm-sewer / pipe-network flooding": "run_swmm",
}

_NEXT_ACTION = (
    "SELECT-THEN-CALL: call the chosen swan_* template directly with its "
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
    """Registry-driven SWAN template listing (deterministic, sorted).

    Enumerates ``TOOL_REGISTRY`` for entries tagged ``engine == "swan"`` and
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
            logger.warning("run_swan door: card derivation failed for %s", name,
                           exc_info=True)
    return cards


@register_tool(
    AtomicToolMetadata(
        name="run_swan",
        ttl_class="live-no-cache",
        source_class="door",
        cacheable=False,
        engine="swan",
        tier="door",
        read_only_hint=True,
        open_world_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
    ),
)
def run_swan() -> dict[str, Any]:
    """List the SWAN nearshore-wave templates, then SELECT-THEN-CALL one.

    Read-only concierge for the SWAN spectral wave engine - call this FIRST for
    any nearshore wave-field question (significant wave height Hs / peak period
    Tp / mean direction Dir over a coastal AOI, a defensible engineering-grade
    wave climate, buoy-validation or overtopping-input wave field, "show me the
    incoming waves", or a standalone SWAN run to COMPARE against SFINCS+SnapWave).
    It EXECUTES NOTHING: it returns the available ``swan_*`` templates (each with
    its one-line question, required inputs, and knobs) and makes them callable for
    the rest of the turn. Then call the chosen template directly.

    NOT for compound-flood / surge / pluvial inundation depth (run_sfincs),
    tsunami / dam-break run-up (run_geoclaw), or urban storm sewers (run_swmm) -
    see ``mismatch_redirect``.

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
