"""The accepted grid recipe as GeoClaw keywords, and the topofile coverage check.

An artifact built by the ``claw_grid`` mesher carries its grid facts in its
probes; ``grid_keywords`` lands them on the dictionary's grid keywords and
refuses a template that states one itself. ``check_topofile`` applies the
engine's own topo-covers-domain stop to a topofile header before the run.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from ..errors import GeoclawError

__all__ = ["GRID_KEYWORDS", "check_topofile", "grid_keywords"]

#: The keywords a grid recipe states; a template stating one beside it refuses.
GRID_KEYWORDS = (
    "CLAW_LOWER", "CLAW_UPPER", "CLAW_NUM_CELLS", "AMR_AMR_LEVELS_MAX",
    "AMR_REFINEMENT_RATIOS_X", "AMR_REFINEMENT_RATIOS_Y",
    "AMR_REFINEMENT_RATIOS_T", "FLAGREGION_FLAGREGIONS")

#: The engine stops when a topo file set's overlap with the domain differs from
#: the domain's area by more than this fraction (topo_module.f90, "topo arrays do
#: not cover domain").
_COVER_TOLERANCE = 1e-2

#: A topofile header's registration word -> the offset of its first node from
#: the stated lower-left, in cells (the reader shifts a corner by half a cell).
_REGISTRATION = {"corner": 0.5, "centre": 0.0}


def _refuse(code: str, message: str) -> None:
    raise GeoclawError(message, error_code=f"GEOCLAW_{code}")


def _facts(artifact: Any) -> dict[str, Any]:
    facts = (getattr(artifact, "probes", None) or {}).get("claw_grid")
    if getattr(artifact, "mode", None) != "claw_grid" or not facts:
        _refuse("GRID_NOT_A_RECIPE",
                f"the mesh {getattr(artifact, 'name', artifact)!r} was not built "
                "by the claw_grid mesher, so it states no GeoClaw grid.")
    return facts


def grid_keywords(artifact: Any, stated: Iterable[str] = ()) -> dict[str, Any]:
    """The grid keywords an accepted grid recipe states -> name to value.

    ``stated`` is every keyword the template already states; one that is a grid
    keyword refuses naming it, since the recipe is the one place the grid is
    stated. No flagregion keyword is returned when the recipe holds none."""
    facts = _facts(artifact)
    clash = sorted(set(stated) & set(GRID_KEYWORDS))
    if clash:
        _refuse("GRID_KEYWORD_STATED",
                f"{', '.join(clash)} {'is' if len(clash) == 1 else 'are'} "
                "stated beside a grid recipe: the recipe states the grid, so "
                "remove the keyword or change the recipe.")
    keywords = {
        "CLAW_LOWER": list(facts["lower"]),
        "CLAW_UPPER": list(facts["upper"]),
        "CLAW_NUM_CELLS": [int(n) for n in facts["num_cells"]],
        "AMR_AMR_LEVELS_MAX": int(facts["levels"]),
        "AMR_REFINEMENT_RATIOS_X": list(facts["ratios"]),
        "AMR_REFINEMENT_RATIOS_Y": list(facts["ratios"]),
        "AMR_REFINEMENT_RATIOS_T": list(facts["ratios"]),
    }
    if facts["flagregions"]:
        keywords["FLAGREGION_FLAGREGIONS"] = [
            [r["name"], r["minlevel"], r["maxlevel"], r["t1"], r["t2"], 1,
             [r["extent"][0], r["extent"][2], r["extent"][1], r["extent"][3]]]
            for r in facts["flagregions"]]
    return keywords


def check_topofile(artifact: Any, header: Mapping[str, Any]) -> None:
    """Refuse a topofile the engine would stop on for not covering the level-1 box.

    ``header`` carries ncols, nrows, cellsize, xll, yll and registration
    ('corner' or 'centre'). The topo's reach is its NODE extent: a corner
    header's first node is half a cell in from the stated lower-left."""
    facts = _facts(artifact)
    try:
        offset = _REGISTRATION[str(header["registration"])]
        dx = float(header["cellsize"])
        x0 = float(header["xll"]) + offset * dx
        y0 = float(header["yll"]) + offset * dx
        x1 = x0 + (int(header["ncols"]) - 1) * dx
        y1 = y0 + (int(header["nrows"]) - 1) * dx
    except (KeyError, TypeError, ValueError):
        _refuse("TOPO_HEADER",
                f"a topofile header states ncols, nrows, cellsize, xll, yll and "
                f"a registration in {sorted(_REGISTRATION)}; got {dict(header)!r}.")
    (lx, ly), (ux, uy) = facts["lower"], facts["upper"]
    reach = max(0.0, min(ux, x1) - max(lx, x0)) * max(0.0, min(uy, y1) - max(ly, y0))
    domain = (ux - lx) * (uy - ly)
    if abs(reach - domain) > _COVER_TOLERANCE * domain:
        nearest = (max(lx, x0), max(ly, y0), min(ux, x1), min(uy, y1))
        _refuse("TOPO_COVERAGE",
                f"the topofile's nodes span x {x0:g}..{x1:g}, y {y0:g}..{y1:g} "
                f"({header['registration']} registration, cell {dx:g}), which "
                f"covers {reach:g} of the level-1 grid's {domain:g} square "
                f"degrees (x {lx:g}..{ux:g}, y {ly:g}..{uy:g}); the engine "
                f"stops beyond {_COVER_TOLERANCE:g} of the area. The nearest "
                f"covered extent is {nearest}.")
