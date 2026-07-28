"""Engine door: ``run_landlab`` - the read-only Landlab surface-process concierge.

ROUTING - call this FIRST for ANY landslide-susceptibility / slope-stability /
overland-flow question, then SELECT-THEN-CALL the template it names:
  landslide susceptibility / factor-of-safety / probability-of-failure over a
  hillslope or catchment, slope-stability hazard mapping, rainfall overland-flow
  / surface-runoff routing over a DEM -> call ``run_landlab`` to LIST the
  available Landlab templates, then call the chosen ``landlab_*`` template
  directly.

This door EXECUTES NOTHING (no solve, no layer). It (1) lists its engine's
registered templates (name, one-line question, required inputs, knobs) straight
from the live registry, (2) makes those templates callable for the rest of the
turn (gate expansion), and (3) briefs the model on Landlab fidelity and
redirects off-engine asks to the right door.

Do NOT use for:
  - channel / riverine / coastal inundation flooding -> run_sfincs
  - post-fire debris-flow hazard over a burn scar -> model_debris_flow
  - probabilistic seismic hazard -> run_openquake
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
_ENGINE = "landlab"

#: Signature params that are plumbing, not a real user input, and MUST NOT be
#: surfaced as a template's ``required_inputs`` / ``knobs``. ``compute_class`` is
#: the workflow-dispatch compute-tier selector; underscore-prefixed params
#: (``_extra_ignored`` etc.) are absorbed kwargs.
_IGNORED_PARAMS = frozenset({"compute_class", "project_id", "session_id"})

#: Static fidelity brief (narrated by the LLM). Landlab CSDMS component grids -
#: infinite-slope Monte-Carlo landslide susceptibility OR rainfall overland-flow
#: routing; demo-default soil / rainfall properties, planning-grade hillslope
#: envelopes, batch-only (scale-to-zero island).
_FIDELITY_BRIEF = (
    "Landlab landscape-process engine (CSDMS component grids). Landslide "
    "susceptibility as an infinite-slope Monte-Carlo factor-of-safety / "
    "probability-of-failure raster, OR rainfall OVERLAND-FLOW surface routing "
    "(de Almeida shallow water). Soil / rainfall properties default to narrated "
    "demo values unless supplied; planning-grade hillslope / small-catchment "
    "envelopes, NOT site-calibrated geotechnical models."
)

#: Off-engine redirection map (prose the LLM narrates; it names DOORS, not
#: templates - except ``model_debris_flow`` which is a general pfdf tool, not an
#: engine door). Landslide / overland-flow belongs here; everything else points
#: away.
_MISMATCH_REDIRECT = {
    "channel / riverine / coastal inundation flooding": "run_sfincs",
    "post-fire debris-flow hazard over a burn scar": "model_debris_flow",
    "probabilistic seismic hazard": "run_openquake",
}

_NEXT_ACTION = (
    "SELECT-THEN-CALL: call the chosen landlab_* template directly with its "
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
    """Registry-driven Landlab template listing (deterministic, sorted).

    Enumerates ``TOOL_REGISTRY`` for entries tagged ``engine == "landlab"`` and
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
            logger.warning("run_landlab door: card derivation failed for %s", name,
                           exc_info=True)
    return cards


@register_tool(
    AtomicToolMetadata(
        name="run_landlab",
        ttl_class="live-no-cache",
        source_class="door",
        cacheable=False,
        engine="landlab",
        tier="door",
        read_only_hint=True,
        open_world_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
    ),
)
def run_landlab() -> dict[str, Any]:
    """List the Landlab surface-process templates, then SELECT-THEN-CALL one.

    Read-only concierge for the Landlab engine - call this FIRST for any
    landslide-susceptibility / slope-stability / factor-of-safety question or a
    rainfall overland-flow / surface-runoff routing ask over a hillslope or
    catchment. It EXECUTES NOTHING: it returns the available ``landlab_*``
    templates (each with its one-line question, required inputs, and knobs) and
    makes them callable for the rest of the turn. Then call the chosen template
    directly.

    NOT for channel / riverine / coastal flooding (run_sfincs), post-fire
    debris-flow hazard (model_debris_flow), or seismic hazard (run_openquake) -
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
