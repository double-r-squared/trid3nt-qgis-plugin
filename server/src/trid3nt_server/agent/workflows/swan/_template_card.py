"""Shared ``TemplateCard`` for engine-door template listings (engine-door refactor - SWAN slice).

A template module MAY export a module-level ``TEMPLATE_CARD`` so its engine door
lists a curated one-line question + required inputs + knobs instead of deriving
them from the docstring/signature. The door duck-types the override (it only
reads ``.question`` / ``.required_inputs`` / ``.knobs``), so this class needs no
dependency on the door module - kept zero-import to avoid any load-order cycle
between the workflow template modules and the door under ``tools/``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TemplateCard:
    """Curated door-listing card for one engine template.

    Fields:
        question: the one-line question this template answers (what the door
            surfaces so the LLM can select-then-call).
        required_inputs: the real required user inputs (the door lists these; a
            signature with all-defaulted params derives an empty list otherwise).
        knobs: a short one-line summary of the optional overrides.
    """

    question: str
    required_inputs: list[str]
    knobs: str
