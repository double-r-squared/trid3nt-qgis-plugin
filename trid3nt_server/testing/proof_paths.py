"""Where a run's proof packet lands: ``run/proof/<template>/<run-id>/``.

PROOF IS TRANSIENT: the tree is gitignored and the renderer sweeps it to a TTL,
so a packet exists to be DELIVERED and the delivery is the record. Every render
script, canary and evidence writer asks HERE instead of joining its own path,
and the filenames inside a packet are cited by name in that delivery.
"""

from __future__ import annotations

import os

__all__ = ["PACKET_ROOT", "VARIANTS", "evidence_path", "packet_dir",
           "split_variant"]

#: ``run/proof`` - the TRANSIENT packet tree. Ignored by git under ``run/`` and
#: pruned by the packet renderer, so nothing here is evidence a later reader is
#: expected to find. Keyed by RUN rather than by variant: one run, one folder, so
#: a re-render replaces its own packet and can never overwrite another run's.
PACKET_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "run", "proof")

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
#: The variant names the DECLARATION and the filename stem its deliverables
#: carry; it does not name a folder, because the folder is the run's.
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


def packet_dir(template: str, run_id: str | None = None, *,
               create: bool = True) -> str:
    """Where this RUN's packet lands: ``run/proof/<template>/<run-id>/``.

    Without a run id, the template's own transient folder - where a drive-lane
    evidence file that is not one run's packet lands."""
    # An empty string is a run id the caller failed to read, not a request for
    # the template folder: keying the packet on "" would file every run of the
    # template on top of the last one, which is the collision the run id exists
    # to prevent.
    if run_id is not None and not str(run_id).strip():
        raise ValueError("a packet folder is named for the run it proves; "
                         f"{run_id!r} is not a run id")
    parts = [PACKET_ROOT, template] + ([str(run_id)] if run_id is not None else [])
    path = os.path.join(*parts)
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def evidence_path(name: str, run_id: str | None = None) -> str:
    """Where the run named ``name`` writes its canary evidence JSON.

    ``<name>_canary_evidence.json`` - the filename the packet assembler looks for
    when it is pointed at a folder rather than handed a path."""
    # No run id means the canary never dispatched, so there is no run to name a
    # packet after and the file lands in the template's own folder. A failure is
    # still evidence, and refusing to write it would lose exactly the case a
    # reader most needs.
    template, _ = split_variant(name)
    return os.path.join(packet_dir(template, run_id or None),
                        f"{name}_canary_evidence.json")
