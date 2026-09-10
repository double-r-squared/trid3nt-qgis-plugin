"""Where a template's proofs live: ``docs/proof/templates/<template>/<variant>/``.

FILENAMES must not change: renders are cited by name from decision notes and
evidence JSONs, and a rename silently breaks every citation. Every render
script, canary and evidence writer asks HERE rather than joining its own path.
"""

from __future__ import annotations

import os

__all__ = ["PROOF_ROOT", "VARIANTS", "evidence_path", "proof_dir"]

#: ``docs/proof/templates`` - the audit folder. Never cleaned or pruned
#: automatically: a delivered proof is evidence and outlives the run that made it.
PROOF_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "docs", "proof", "templates")

#: FOUR variants, and no more, because a fifth would be a category nobody
#: agreed on:
#:   * ``coarse``        - the default-resolution canary run, the baseline.
#:   * ``refined``       - the same question on a finer mesh; the pair is what
#:                         makes a resolution-sensitivity claim measurable.
#:   * ``postmigration`` - the same question re-run after a refactor, showing
#:                         that the representation changed and the numbers did
#:                         not.
#:   * ``addendum``      - a proof that is none of those three: a gate-card
#:                         walkthrough, a release-point acceptance case, a
#:                         one-off diagnostic that settled something.
VARIANTS: tuple[str, ...] = ("coarse", "refined", "postmigration", "addendum")


def split_variant(name: str) -> tuple[str, str]:
    """A run NAME split into ``(template, variant)``.

    A ``<template>_<variant>`` suffix is the only encoding there is; anything
    else is the template's coarse baseline."""
    for variant in ("refined", "postmigration", "addendum"):
        suffix = f"_{variant}"
        if name.endswith(suffix):
            return name[: -len(suffix)], variant
    return name, "coarse"


def proof_dir(template: str, variant: str = "coarse", *, create: bool = True) -> str:
    """The directory this template's ``variant`` proofs live in.

    An unknown variant REFUSES rather than quietly creating a fifth folder."""
    # The scheme's value is that a reader knows the four names, and a typo that
    # made ``refned/`` would HIDE a render rather than misfile it visibly.
    if variant not in VARIANTS:
        raise ValueError(
            f"{variant!r} is not a proof variant; the four are {list(VARIANTS)}. "
            "A proof that is none of them is an addendum.")
    path = os.path.join(PROOF_ROOT, template, variant)
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def evidence_path(name: str) -> str:
    """Where the run named ``name`` writes its canary evidence JSON.

    ``<name>_canary_evidence.json``, a filename decision notes, render scripts
    and the coverage board all cite."""
    template, variant = split_variant(name)
    return os.path.join(proof_dir(template, variant), f"{name}_canary_evidence.json")
