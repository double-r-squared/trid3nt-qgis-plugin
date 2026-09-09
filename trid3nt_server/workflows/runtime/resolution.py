"""RESOLUTION SENSITIVITY: which answers a coarse mesh reads wrong, and which way.

A label is conditioned on the run's own SHEET, never a spacing threshold: only a
lever row with BOTH a user basis and a seated value counts as refined.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

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
    """One template's declaration: which ANSWER fields are in which class.
    Rows are ``(answer_field, class)``; an unknown class name is refused where it is
    declared, because a label nobody can read is worse than no label."""

    __slots__ = ("rows",)

    def __init__(self, rows: Sequence[tuple[str, str]] = ()) -> None:
        out: list[tuple[str, str]] = []
        for row in rows:
            pair = tuple(row)
            if len(pair) != 2 or pair[1] not in CLASSES:
                raise ValueError(
                    f"sensitivity row {row!r} is not (answer_field, class) with "
                    f"class in {sorted(CLASSES)}.")
            out.append((str(pair[0]), str(pair[1])))
        self.rows = tuple(out)

    def __bool__(self) -> bool:
        return bool(self.rows)


def _lever(metadata: Any) -> str | None:
    """The resolution PARAM this engine's answers depend on, from the tool metadata.
    Read off the declared ``ResolutionSpec``, never restated on the sensitivity
    declaration: two names for one lever is a mirror waiting to disagree."""
    for spec in getattr(metadata, "resolution_specs", ()) or ():
        param = getattr(spec, "param", None)
        if param:
            return str(param)
    return None


def sensitivity_notes(decl: SensitivityDecl, metadata: Any, result: Any,
                      sheet: Sequence[Any]) -> tuple[str, ...]:
    """The honesty note(s) this run's answer carries, or ``()``.
    ONE note per run, not one per field, and fields the run did not produce are
    dropped - a note about a number that is not there points at nothing."""
    if not decl:
        return ()
    present = [(field, cls) for field, cls in decl.rows
               if getattr(result, field, None) is not None]
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
    mesh_m = getattr(result, "mesh_size_m", None)
    at = f" at {float(mesh_m):g} m" if mesh_m is not None else ""

    classes = sorted({cls for _, cls in present})
    what = "; ".join(f"{CLASSES[c][0]} - {CLASSES[c][1]}" for c in classes)
    fields = ", ".join(field for field, _ in present)
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
