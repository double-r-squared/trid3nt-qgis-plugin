"""Engine door: ``run_modflow`` - the read-only MODFLOW groundwater concierge.

ROUTING - call this FIRST for ANY groundwater / aquifer question, then
SELECT-THEN-CALL the template it names:
  contaminant plume / spill spread + peak concentration, capture zone,
  wellhead protection (WHPA), mine dewatering, saltwater intrusion, managed
  aquifer recharge (MAR), aquifer storage & recovery (ASR), sustainable yield /
  pumping drawdown, wetland hydroperiod, regional water budget, river seepage /
  stream depletion -> call ``run_modflow`` to LIST the available MODFLOW
  templates, then call the chosen ``modflow_*`` template directly.

This door EXECUTES NOTHING (no solve, no layer). It (1) lists its engine's
registered templates (name, one-line question, required inputs, knobs) straight
from the live registry, (2) makes those templates callable for the rest of the
turn (gate expansion), and (3) briefs the model on MODFLOW fidelity and
redirects off-engine asks to the right door.

Do NOT use for:
  - surface-water / inundation flooding -> the SFINCS flood door
  - urban storm-sewer / pipe-network flooding -> run_swmm
  - coastal spectral wave fields -> run_swan
See ``mismatch_redirect`` in the return.

Determinism (Invariant 1): every field is derived from the live registry /
callable signatures / a static fidelity brief - no free generation, no
fabricated template. The door lists ONLY registered templates, so a
not-yet-built archetype never appears.

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
_ENGINE = "modflow"

#: Signature params that are plumbing, not a real user input, and MUST NOT be
#: surfaced as a template's ``required_inputs`` / ``knobs``. ``compute_class`` is
#: the workflow-dispatch compute-tier selector; underscore-prefixed params
#: (``_extra_ignored`` etc.) are absorbed kwargs.
_IGNORED_PARAMS = frozenset({"compute_class"})

#: Static fidelity brief (narrated by the LLM). MODFLOW 6 / MF6-GWT groundwater
#: engine, demo-default aquifer properties, conservative tracer unless advanced,
#: local planning-grade PRT / saltwater runs.
_FIDELITY_BRIEF = (
    "MODFLOW 6 / MF6-GWT groundwater engine. Aquifer K/porosity default to "
    "narrated demo values unless supplied. Conservative-tracer transport unless "
    "advanced_physics is set. Capture-zone / wellhead / saltwater runs are "
    "local, planning-grade envelopes, not calibrated regulatory delineations."
)

#: Off-engine redirection map (prose the LLM narrates; it names DOORS, not
#: templates). Groundwater questions belong here; everything else points away.
_MISMATCH_REDIRECT = {
    "surface-water / inundation flooding": "run_sfincs (flood door)",
    "urban storm-sewer / pipe-network flooding": "run_swmm",
    "coastal spectral wave field": "run_swan",
}

_NEXT_ACTION = (
    "SELECT-THEN-CALL: call the chosen modflow_* template directly with its "
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
    """Registry-driven MODFLOW template listing (deterministic, sorted).

    Enumerates ``TOOL_REGISTRY`` for entries tagged ``engine == "modflow"`` and
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
            logger.warning("run_modflow door: card derivation failed for %s", name,
                           exc_info=True)
    return cards


@register_tool(
    AtomicToolMetadata(
        name="run_modflow",
        ttl_class="live-no-cache",
        source_class="door",
        cacheable=False,
        engine="modflow",
        tier="door",
        read_only_hint=True,
        open_world_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
    ),
)
def run_modflow() -> dict[str, Any]:
    """List the MODFLOW groundwater templates, then SELECT-THEN-CALL one.

    Read-only concierge for the MODFLOW engine - call this FIRST for any
    groundwater / aquifer question (contaminant plume, capture zone, wellhead
    protection, mine dewatering, saltwater intrusion, MAR, ASR, sustainable
    yield, wetland hydroperiod, regional water budget, river seepage). It
    EXECUTES NOTHING: it returns the available ``modflow_*`` templates (each with
    its one-line question, required inputs, and knobs) and makes them callable
    for the rest of the turn. Then call the chosen template directly.

    NOT for surface-water flooding (SFINCS), urban storm sewers (run_swmm), or
    spectral waves (run_swan) - see ``mismatch_redirect``.

    Returns a read-only concierge envelope::

        {"engine": "modflow", "kind": "engine_door",
         "templates": [{"tool_name", "question", "required_inputs", "knobs"}, ...],
         "fidelity_brief": <str>, "mismatch_redirect": {...},
         "next_action": <str>}
    """
    return {
        "engine": _ENGINE,
        "kind": "engine_door",
        "templates": _list_templates(),
        "fidelity_brief": _FIDELITY_BRIEF,
        "mismatch_redirect": dict(_MISMATCH_REDIRECT),
        "next_action": _NEXT_ACTION,
    }
