"""RESOLUTION SENSITIVITY: which of a run's published reads a coarse mesh gets
wrong, and which way.

A label is conditioned on the run's own resolution lever, never a spacing
threshold: only a lever row with BOTH a user basis and a seated value counts as
refined. The note is FEEDBACK about the run's fidelity, so it names what the run
PUBLISHED and says nothing about what the numbers mean.
"""

from __future__ import annotations

from typing import Any, Collection, Mapping, Sequence

__all__ = ["CLASSES", "SensitivityDecl", "sensitivity_notes"]


#: The four sensitive classes: what the class IS, and which way a coarse mesh
#: reads it. Every direction is MEASURED on run pairs at two spacings - the peak
#: 6x low, the flooded extent 4x low, a crest location 2x high, the gradient reads
#: low (upwind Hs -62%, agitation Kd -30 to -50%, stratification dT -25%) - and
#: every one of them in the unsafe direction. The CONVERGED classes (integrals,
#: saturated maxima, ratios) carry no label, because labeling everything is the
#: same as labeling nothing.
CLASSES: Mapping[str, tuple[str, str]] = {
    "peak": ("a concentration/magnitude PEAK",
             "a coarse element averages a peak away, so this reads LOW"),
    "extent": ("an area bounded by a WET/DRY front",
               "the front lands between nodes, so this reads LOW"),
    "location": ("WHERE a local feature sits",
                 "the feature moves with the element that resolves it"),
    "gradient": ("a value read inside a steep GRADIENT zone",
                 "the gradient is flattened across the element, so this reads LOW"),
}


class SensitivityDecl:
    """One template's declaration: which PUBLISHED quantities are in which class.
    Rows are ``(quantity, class)`` - the name the run's own layer or chart carries,
    so a declaration stands on something a reader can open - and an unknown class
    name is refused where it is declared, because a label nobody can read is worse
    than no label."""

    __slots__ = ("rows",)

    def __init__(self, rows: Sequence[tuple[str, str]] = ()) -> None:
        out: list[tuple[str, str]] = []
        for row in rows:
            pair = tuple(row)
            if len(pair) != 2 or pair[1] not in CLASSES:
                raise ValueError(
                    f"sensitivity row {row!r} is not (quantity, class) with "
                    f"class in {sorted(CLASSES)}.")
            out.append((str(pair[0]), str(pair[1])))
        self.rows = tuple(out)

    def __bool__(self) -> bool:
        return bool(self.rows)


def _lever(metadata: Any) -> str | None:
    """The resolution PARAM this engine's published reads depend on, off the metadata.
    Read off the declared ``ResolutionSpec``, never restated on the sensitivity
    declaration: two names for one lever is a mirror waiting to disagree."""
    for spec in getattr(metadata, "resolution_specs", ()) or ():
        param = getattr(spec, "param", None)
        if param:
            return str(param)
    return None


def sensitivity_notes(decl: SensitivityDecl, metadata: Any,
                      published: Collection[str],
                      sheet: Sequence[Any],
                      fill: Mapping[str, Mapping[str, Any]] | None = None,
                      mesh_size_m: Any = None) -> tuple[str, ...]:
    """The honesty note(s) this run carries about its own fidelity, or ``()``.
    ONE note per run, not one per quantity, and a declared quantity this run did
    not publish is dropped - a note about a product that is not there points at
    nothing. ``published`` is what the run put on the map and on its charts.
    ``fill`` is the solved deck's slots with their origins, so a lever that is a KEYWORD
    rather than a param is read where the run actually states it, and
    ``mesh_size_m`` is the edge off the mesh the run published."""
    if not decl:
        return ()
    present = [(quantity, cls) for quantity, cls in decl.rows
               if quantity in published]
    if not present:
        return ()

    lever = _lever(metadata)
    row = next((r for r in sheet if getattr(r, "name", None) == lever), None)
    # BOTH halves are load-bearing: a lever declared optional on the USER door
    # carries a user basis on the row nobody supplied, so the SEATED VALUE is
    # what says the user actually put a spacing through the door. Testing the
    # basis alone labels a default-spacing run as refined and swallows the bound.
    refined = (getattr(row, "basis", None) == "user"
               and getattr(row, "value", None) is not None)
    # A LEVER THAT IS A KEYWORD has no param row at all: the run states it on
    # the deck, and the fill records who put it there. A user or a model set it
    # for the same reason - somebody chose this granularity.
    stated = str(((fill or {}).get(lever) or {}).get("from") or "")
    refined = refined or stated.startswith(("user", "model"))
    at = f" at {float(mesh_size_m):g} m" if mesh_size_m is not None else ""

    classes = sorted({cls for _, cls in present})
    what = "; ".join(f"{CLASSES[c][0]} - {CLASSES[c][1]}" for c in classes)
    fields = ", ".join(quantity for quantity, _ in present)
    if refined:
        return (
            f"RESOLUTION-SENSITIVE: {fields} sit in a class the mesh decides "
            f"({what}). This run was solved{at} on the spacing you asked for, "
            "which is a refinement, not a demonstrated convergence - the class "
            "stays sensitive.",
        )
    return (
        f"RESOLUTION-LIMITED, TREAT AS A BOUND: {fields} sit in a class the mesh "
        f"decides ({what}), and this run was solved{at} at the template's labeled "
        f"default spacing rather than a resolution you chose. Refine with "
        f"{lever or 'the resolution lever'} to test how far the answer moves; the "
        "measured moves are all in the unsafe direction.",
    )
