"""Where a template's proofs live: ``docs/proof/templates/<template>/<variant>/``.

The proof pile used to be flat and name-prefixed, so finding "the refined coastal
run's animation" meant reading four hundred filenames. Folders inherit the two
facts that were already encoded in every name - WHICH template and WHICH variant
of it - and the filenames stay exactly as they were, because the renders are
cited by name in ADRs and evidence JSONs.

FOUR variants, and no more, because a fifth would be a category nobody agreed on:

  * ``coarse``        - the default-resolution canary run. The baseline.
  * ``refined``       - the same question on a finer mesh. The pair that makes a
                        resolution-sensitivity claim measurable.
  * ``postmigration`` - the same question re-run after a refactor, to show the
                        representation changed and the numbers did not.
  * ``addendum``      - a proof that is not one of those three: a gate-card
                        walkthrough, a release-point acceptance case, a
                        one-off diagnostic kept because it settled something.

One function so the writers cannot drift: every render script, canary and
evidence writer asks HERE for its directory rather than joining its own path.
"""

from __future__ import annotations

import os

__all__ = ["PROOF_ROOT", "VARIANTS", "evidence_path", "proof_dir"]

#: ``docs/proof/templates`` - the audit folder. Never cleaned or pruned without
#: NATE's explicit say-so.
PROOF_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "docs", "proof", "templates")

VARIANTS: tuple[str, ...] = ("coarse", "refined", "postmigration", "addendum")


def split_variant(name: str) -> tuple[str, str]:
    """A run NAME split into ``(template, variant)``.

    The canary registry names a refined run ``<template>_refined``, which is the
    only encoding that exists, so it is the only one read. Anything else is the
    template's coarse baseline.
    """
    for variant in ("refined", "postmigration", "addendum"):
        suffix = f"_{variant}"
        if name.endswith(suffix):
            return name[: -len(suffix)], variant
    return name, "coarse"


def proof_dir(template: str, variant: str = "coarse", *, create: bool = True) -> str:
    """The directory this template's ``variant`` proofs live in.

    An unknown variant REFUSES rather than quietly creating a fifth folder: the
    scheme's value is that a reader knows the four names, and a typo that made
    ``refned/`` would hide a render rather than misfile it visibly.
    """
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

    The FILENAME is unchanged - ``<name>_canary_evidence.json`` - because ADRs,
    render scripts and the module-coverage board cite it. Only the folder moved.
    """
    template, variant = split_variant(name)
    return os.path.join(proof_dir(template, variant), f"{name}_canary_evidence.json")
