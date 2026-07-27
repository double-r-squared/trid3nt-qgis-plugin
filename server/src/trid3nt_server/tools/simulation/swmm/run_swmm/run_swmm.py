"""Engine door: ``run_swmm`` - the read-only SWMM urban-drainage concierge.

ROUTING - call this FIRST for ANY urban / street-level / storm-sewer flood
question, then SELECT-THEN-CALL the template it names:
  urban / pluvial street flooding, stormwater / drainage ponding around
  buildings, storm-sewer / pipe-network flooding, a SOUND BARRIER wall or FLAP
  gate flood control, street pollutant washoff to the outfall (buildup +
  washoff) -> call ``run_swmm`` to LIST the available SWMM templates, then call
  the chosen ``swmm_*`` template directly.

This door EXECUTES NOTHING (no solve, no layer). It (1) lists its engine's
registered templates (name, one-line question, required inputs, knobs) straight
from the live registry, (2) makes those templates callable for the rest of the
turn (gate expansion), and (3) briefs the model on SWMM fidelity and redirects
off-engine asks to the right door.

Do NOT use for:
  - coastal / riverine / large-watershed inundation flooding -> run_sfincs
  - groundwater / aquifer contaminant plume -> run_modflow
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
_ENGINE = "swmm"

#: Signature params that are plumbing, not a real user input, and MUST NOT be
#: surfaced as a template's ``required_inputs`` / ``knobs``. ``compute_class`` is
#: the workflow-dispatch compute-tier selector; ``enable_autoscale`` is set by
#: the server-side #154 granularity gate (LLMs leave it unset); underscore-
#: prefixed params (``_extra_ignored`` etc.) are absorbed kwargs.
_IGNORED_PARAMS = frozenset(
    {"compute_class", "enable_autoscale", "project_id", "session_id"}
)

#: Static fidelity brief (narrated by the LLM). SWMM 1D / quasi-2D urban drainage
#: network engine - node-link storm sewer + pluvial overland; planning-grade.
_FIDELITY_BRIEF = (
    "SWMM 1D / quasi-2D urban drainage-network engine (PySWMM; node-link storm "
    "sewer + pluvial overland flow; buildings as obstruction, flood walls as "
    "blocked links, flap gates native). Produces peak overland-depth + a "
    "temporal depth animation for a design storm, plus optional street "
    "pollutant buildup/washoff to the outfall. Planning-grade urban screening "
    "result, NOT a calibrated regulatory drainage model."
)

#: Off-engine redirection map (prose the LLM narrates; it names DOORS, not
#: templates). Urban storm-sewer / pluvial flooding belongs here; everything
#: else points away.
_MISMATCH_REDIRECT = {
    "coastal / riverine / large-watershed inundation flooding": "run_sfincs (flood door)",
    "groundwater / aquifer contaminant plume": "run_modflow",
}

_NEXT_ACTION = (
    "SELECT-THEN-CALL: call the chosen swmm_* template directly with its "
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
    """Registry-driven SWMM template listing (deterministic, sorted).

    Enumerates ``TOOL_REGISTRY`` for entries tagged ``engine == "swmm"`` and
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
            logger.warning("run_swmm door: card derivation failed for %s", name,
                           exc_info=True)
    return cards


@register_tool(
    AtomicToolMetadata(
        name="run_swmm",
        ttl_class="live-no-cache",
        source_class="door",
        cacheable=False,
        engine="swmm",
        tier="door",
        read_only_hint=True,
        open_world_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
    ),
)
def run_swmm() -> dict[str, Any]:
    """List the SWMM urban-drainage templates, then SELECT-THEN-CALL one.

    Read-only concierge for the SWMM urban-drainage engine - call this FIRST for
    any urban / street-level / storm-sewer flood question (pluvial street
    flooding, stormwater ponding around buildings, storm-sewer / pipe-network
    flooding, a flood WALL or FLAP gate control, street pollutant washoff to the
    outfall). It EXECUTES NOTHING: it returns the available ``swmm_*`` templates
    (each with its one-line question, required inputs, and knobs) and makes them
    callable for the rest of the turn. Then call the chosen template directly.

    NOT for coastal / riverine / watershed inundation (run_sfincs) or groundwater
    plumes (run_modflow) - see ``mismatch_redirect``.

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
