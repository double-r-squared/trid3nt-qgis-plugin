"""The accepted grid recipe as GeoClaw keywords, and the topofile coverage check.

An artifact built by the ``claw_grid`` mesher carries its grid facts in its
probes; ``grid_keywords`` lands them on the dictionary's grid keywords and
refuses a template that states one itself. ``check_topofile`` applies the
engine's own topo-covers-domain stop to a topofile header before the run.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from ..errors import GeoclawError

__all__ = ["GRID_KEYWORDS", "REGIONS_REFUSAL", "check_topofile", "check_topofiles", "covered_by",
           "grid_keywords"]

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


REGIONS_REFUSAL = ("REGION_REGIONS (Clawpack's old-style regions) is not supported: state the refinement as "
                   "FLAGREGION_FLAGREGIONS, or as flagregion ops on the grid recipe.")


def _refuse(code: str, message: str) -> None:
    raise GeoclawError(message, error_code=f"GEOCLAW_{code}")


def _facts(artifact: Any) -> dict[str, Any]:
    facts = (getattr(artifact, "probes", None) or {}).get("claw_grid")
    if getattr(artifact, "mode", None) != "claw_grid" or not facts:
        _refuse("GRID_NOT_A_RECIPE",
                f"the mesh {getattr(artifact, 'name', artifact)!r} was not built "
                "by the claw_grid mesher, so it states no GeoClaw grid.")
    return facts


def grid_keywords(artifact: Any, stated: Iterable[str] = (), rupture_end: float | None = None) -> dict[str, Any]:
    """The grid keywords an accepted grid recipe states -> name to value.

    ``stated`` is every keyword the template already states; one that is a grid
    keyword refuses naming it, since the recipe is the one place the grid is
    stated. No flagregion keyword is returned when the recipe holds none. A region that follows the
    rupture ends at ``rupture_end`` when that is later than its own t2; with no rupture stated its t2 stands."""
    facts = _facts(artifact)
    if "REGION_REGIONS" in stated:
        _refuse("REGION_REGIONS_UNSUPPORTED", REGIONS_REFUSAL)
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
            [r["name"], r["minlevel"], r["maxlevel"], r["t1"],
             max(r["t2"], rupture_end) if r.get("with_rupture") and rupture_end is not None else r["t2"],
             *((2, dict(r["ruled"])) if "ruled" in r else (1, [r["extent"][0], r["extent"][2], r["extent"][1], r["extent"][3]]))]
            for r in facts["flagregions"]]
    return keywords


def check_topofile(artifact: Any, header: Mapping[str, Any]) -> None:
    """Refuse a topofile the engine would stop on for not covering the level-1 box.

    ``header`` carries ncols, nrows, cellsize, xll, yll and registration
    ('corner' or 'centre'). The topo's reach is its NODE extent: a corner
    header's first node is half a cell in from the stated lower-left."""
    check_topofiles(artifact, [header])


def check_topofiles(artifact: Any, headers: Iterable[Mapping[str, Any]]) -> None:
    """Refuse a topofile set the engine would stop on for not covering the level-1 box.

    The engine measures the area of the domain the UNION of the files' node extents
    covers, so a fine file inside a coarse one adds nothing and leaves nothing out."""
    facts = _facts(artifact)
    covered_by(facts["lower"], facts["upper"], headers)


def covered_by(lower: Iterable[float], upper: Iterable[float],
               headers: Iterable[Mapping[str, Any]]) -> None:
    """Refuse the topofile headers whose union leaves more than the engine's tolerance of the box [lower, upper] uncovered."""
    from shapely.geometry import box
    from shapely.ops import unary_union

    (lx, ly), (ux, uy) = lower, upper
    spans = [_span(header) for header in headers]
    if not spans:
        _refuse("TOPO_HEADER", "no topofile header was given, so nothing covers the level-1 grid.")
    domain = box(lx, ly, ux, uy)
    union = unary_union([box(x0, y0, x1, y1) for x0, y0, x1, y1, _ in spans])
    reach = union.intersection(domain).area
    if abs(reach - domain.area) <= _COVER_TOLERANCE * domain.area:
        return
    listed = "; ".join(
        f"topofile {n} spans x {x0:g}..{x1:g}, y {y0:g}..{y1:g} ({how})"
        for n, (x0, y0, x1, y1, how) in enumerate(spans, 1))
    nearest = tuple(union.intersection(domain).bounds)
    missing = tuple(domain.difference(union).bounds)
    _refuse("TOPO_COVERAGE",
            f"the topofiles' nodes cover {reach:g} of the level-1 grid's "
            f"{domain.area:g} square degrees (x {lx:g}..{ux:g}, y {ly:g}..{uy:g}); "
            f"the engine stops beyond {_COVER_TOLERANCE:g} of the area. {listed}. "
            f"The nearest covered extent is {nearest}; the uncovered part lies "
            f"within {missing}.")


def _span(header: Mapping[str, Any]) -> tuple[float, float, float, float, str]:
    """A topofile header's node extent (x0, y0, x1, y1) and how it was stated."""
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
    return x0, y0, x1, y1, f"{header['registration']} registration, cell {dx:g}"
