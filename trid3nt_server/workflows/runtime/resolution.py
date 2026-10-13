"""RESOLUTION SENSITIVITY: which of a run's published reads a coarse mesh gets wrong, and which way.

A label is conditioned on the run's own resolution lever: only a lever the user stated counts as refined.
The note names what the run PUBLISHED and says nothing about what the numbers mean.
"""

from __future__ import annotations

from typing import Any, Collection, Mapping, Sequence

__all__ = ["CLASSES", "SensitivityDecl", "sensitivity_notes"]


#: The four sensitive classes and which way a coarse mesh reads each, measured on run pairs at two spacings:
#: peak 6x low, flooded extent 4x low, crest location 2x high, gradient low (upwind Hs -62%, agitation Kd -30 to -50%,
#: stratification dT -25%), all unsafe. Converged classes (integrals, saturated maxima, ratios) carry no label.
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
    """One template's declaration: ``(quantity, class)`` rows by the name the run's layer or chart carries.
    An unknown class is refused where declared; ``lever`` is the granularity the reads depend on
    (the mesh edge, or a module keyword such as a 3D deck's plane count)."""

    __slots__ = ("rows", "lever")

    def __init__(self, rows: Sequence[tuple[str, str]] = (),
                 lever: str = "mesh_resolution_m") -> None:
        out: list[tuple[str, str]] = []
        for row in rows:
            pair = tuple(row)
            if len(pair) != 2 or pair[1] not in CLASSES:
                raise ValueError(
                    f"sensitivity row {row!r} is not (quantity, class) with "
                    f"class in {sorted(CLASSES)}.")
            out.append((str(pair[0]), str(pair[1])))
        self.rows = tuple(out)
        self.lever = str(lever)

    def __bool__(self) -> bool:
        return bool(self.rows)


def sensitivity_notes(decl: SensitivityDecl,
                      published: Collection[str],
                      inputs: Mapping[str, Mapping[str, Any]],
                      mesh_size_m: Any = None) -> tuple[str, ...]:
    """The honesty note(s) this run carries about its own fidelity, or ``()``.
    One note per run; a declared quantity the run did not publish is dropped. ``inputs`` is the run's inputs
    by name with their origins - the lever is read there, a value or a keyword alike; ``mesh_size_m`` is the published mesh edge."""
    if not decl:
        return ()
    present = [(quantity, cls) for quantity, cls in decl.rows
               if quantity in published]
    if not present:
        return ()

    lever = decl.lever
    # A lever a user stated was set deliberately.
    refined = (inputs.get(lever) or {}).get("origin") == "user"
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
        f"decides ({what}), and this run was solved{at} at the spacing its own "
        f"input data and deck set rather than a resolution you chose. Refine with "
        f"{lever} to test how far the answer moves; the "
        "measured moves are all in the unsafe direction.",
    )
