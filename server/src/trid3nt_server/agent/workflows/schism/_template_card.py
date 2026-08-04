"""Shared ``TemplateCard`` for the SCHISM engine-template listing.

A template module MAY export a module-level ``TEMPLATE_CARD`` so its engine door
lists a curated one-line question + required inputs + knobs instead of deriving
them from the docstring/signature. The door duck-types the override (it only reads
``.question`` / ``.required_inputs`` / ``.knobs``), so this class needs no
dependency on the door module -- kept zero-import to avoid any load-order cycle.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TemplateCard:
    """Curated door-listing card for one engine template.

    Fields:
        question: the one-line question this template answers.
        required_inputs: the real required user inputs.
        knobs: a short one-line summary of the optional overrides.
    """

    question: str
    required_inputs: list[str]
    knobs: str
