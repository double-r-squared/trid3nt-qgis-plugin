"""What was spilled: the substance CLASS and the modules its class arms.

Four classes ride one solve: a conservative dye tracer (the default), an
oil-family slick, a first-order WAQTEL decay, and GAIA sediment (with the
erodible-bed, graded-mixture and NESTOR-dredging variants layered on it).

The keyword vocabularies are shared between classification and the auto-arm so
the two gates route off the SAME words and cannot disagree - an erodible bed
that classified as a tracer would couple nothing and only LOOK morphodynamic.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("trid3nt_server.workflows.telemac.helpers.substance")

__all__ = [
    "DECAY_SUBSTANCE_PRESETS",
    "DREDGE_KEYWORDS",
    "GRADATION_KEYWORDS",
    "GRADATION_PRESETS",
    "OIL_SUBSTANCE_PRESETS",
    "SCOUR_KEYWORDS",
    "SEDIMENT_SUBSTANCE_PRESETS",
    "arm_sediment_modules",
    "classify_substance",
    "resolve_decay_law",
    "resolve_gradation",
    "resolve_grain",
    "sanitize_substance",
    "substance_class",
]


#: Oil-family substances ALSO run the TELEMAC oil-spill module (floating particle
#: slick + the dissolved fraction in the tracer). Matched as substrings; generic
#: ``oil`` is LAST so the specific fuels win.
OIL_SUBSTANCE_PRESETS: dict[str, str] = {
    "diesel": "diesel",
    "gasoline": "diesel",
    "petrol": "diesel",
    "heavy fuel": "heavy_fuel",
    "heavy_fuel": "heavy_fuel",
    "bunker": "heavy_fuel",
    "crude": "light_crude",
    "oil": "light_crude",
}

#: First-order DECAY class: the run couples WAQTEL with WATER QUALITY PROCESS =
#: 17, whose nametrac branch applies a decay SINK to the existing dye tracer - no
#: new tracer, no postprocess or contract change. ``law``: 1 = T90 bacterial
#: die-off (coef = T90 hours), 2 = first-order (k in h^-1), 3 = first-order (k in
#: d^-1), per telemac2d.dico LAW OF TRACERS DEGRADATION. Bacterial keywords carry
#: T90 ~ 2 h (daylight-freshwater fecal-coliform die-off, a narrated literature
#: default). Period-stripped variants are listed because the substance sanitize
#: drops the periods.
DECAY_SUBSTANCE_PRESETS: dict[str, dict[str, float]] = {
    "sewage": {"law": 1, "coef": 2.0},
    "e. coli": {"law": 1, "coef": 2.0},
    "e.coli": {"law": 1, "coef": 2.0},
    "e coli": {"law": 1, "coef": 2.0},
    "ecoli": {"law": 1, "coef": 2.0},
    "coliform": {"law": 1, "coef": 2.0},
    "coli": {"law": 1, "coef": 2.0},
    "bacteria": {"law": 1, "coef": 2.0},
    "bacterial": {"law": 1, "coef": 2.0},
    "effluent": {"law": 1, "coef": 2.0},
    "wastewater": {"law": 1, "coef": 2.0},
    "die-off": {"law": 1, "coef": 2.0},
    "decaying": {"law": 2, "coef": 0.35},
    "half-life": {"law": 2, "coef": 0.35},
}

#: GAIA SEDIMENT class: the run couples GAIA, which appends one suspended class
#: as a second tracer and writes gaia_river.slf CUMUL BED EVOL. ``grain_size`` is
#: the default d50 in microns for the type - a demo default the ``grain_size_um``
#: param overrides, never a measured site value (no bed-composition fetcher
#: exists). All types are modeled NON-cohesive, so ``mud`` is a very-fine NCO
#: approximation and is narrated as one.
SEDIMENT_SUBSTANCE_PRESETS: dict[str, dict[str, float | str]] = {
    "sediment-laden runoff": {"type": "silt", "grain_size": 20.0},
    "sediment": {"type": "sand", "grain_size": 200.0},
    "sand": {"type": "sand", "grain_size": 200.0},
    "silt": {"type": "silt", "grain_size": 20.0},
    "mud": {"type": "mud", "grain_size": 8.0},
    "slurry": {"type": "sand", "grain_size": 200.0},
    "tailings": {"type": "silt", "grain_size": 30.0},
}

#: SCOUR / EROSION / mobile-bed vocabulary: a real erodible bed with active
#: bedload, so the bed scours and re-deposits (GAIA erodible-bed path).
SCOUR_KEYWORDS: tuple[str, ...] = (
    "scour", "erosion", "erod", "bedload", "bed load", "degradation",
    "bed lowering", "mobile bed", "morpholog", "aggrad", "degrade",
)

#: GRADED / MIXED-GRAIN vocabulary: a mixture of grain sizes that SORTS.
#: Distinct from scour - scour is single-grain bed lowering, grading is
#: multi-class differential mobility (armoring / downstream fining).
GRADATION_KEYWORDS: tuple[str, ...] = (
    "graded", "gradation", "mixed grain", "mixed-grain", "multi-grain",
    "multigrain", "multi-class", "multiclass", "grain size distribution",
    "grain-size distribution", "sorting", "segregat", "armor", "armour",
    "poorly sorted", "well sorted", "well graded", "well-graded", "bimodal",
    "fining", "sediment mixture", "grain mixture",
)

#: DREDGING vocabulary (NESTOR): an ENGINEERED dig/dump intervention against
#: siltation, not a natural transport process.
DREDGE_KEYWORDS: tuple[str, ...] = (
    "dredg", "maintenance dredging", "channel maintenance", "spoil",
    "disposal placement", "shoaling", "navigation channel depth",
    "keep the channel", "maintain the channel", "silt up", "silting",
    "infill the channel", "dig and dump", "dig-and-dump",
)

#: Named demo gradations (d50 in microns, initial fraction) - honest demo mixes,
#: never a measured site sieve curve. The worker renormalizes the fractions.
GRADATION_PRESETS: dict[str, list[list[float]]] = {
    "graded_sand": [[100.0, 0.34], [400.0, 0.33], [1000.0, 0.33]],
    "poorly_sorted": [[80.0, 0.4], [300.0, 0.3], [1200.0, 0.3]],
    "sand_gravel_bimodal": [[200.0, 0.5], [1800.0, 0.5]],
    "fine_coarse_sand": [[120.0, 0.5], [800.0, 0.5]],
}

#: The label the author reads. Sanitized to alnum + separators because it
#: travels onto the manifest and into layer narration.
_SUBSTANCE_MAX_CHARS = 24


#: The grain-size window GAIA's transport formulae are authored for, in microns.
#: It MIRRORS river_dye's declared ``grain_size_um`` bounds and exists because a
#: gradation curve arrives as a list, which no per-value door can police. ONE
#: constant, two clamp sites: the declaration and both clamps must read the same
#: two numbers from here, never restate them.
GRAIN_UM_MIN, GRAIN_UM_MAX = 5.0, 2000.0


def sanitize_substance(value: Any, *, limit: int = _SUBSTANCE_MAX_CHARS,
                       default: str = "dye") -> str:
    """A safe substance label. Label only - never solver-affecting on its own."""
    kept = "".join(c for c in str(value if value is not None else "").strip().lower()
                   if c.isalnum() or c in " -_")
    return kept[:limit] or default


def resolve_gradation(spec: list | str | None) -> list[list[float]] | None:
    """Coerce a gradation arg to a clean ``[[d50_um, fraction], ...]`` list.

    Accepts a :data:`GRADATION_PRESETS` key, an explicit list of pairs (or
    ``{'d50_um','fraction'}`` dicts), or None. A surviving list of >= 2 classes is
    what arms the multi-class run; a 1-class list collapses to None (nothing to
    sort). Mirrors the worker's own normalization so the tool and the author
    agree on what counts as a usable gradation.
    """
    if spec is None:
        return None
    if isinstance(spec, str):
        pairs = GRADATION_PRESETS.get(spec.strip().lower().replace(" ", "_"))
        if pairs is None:
            return None
        spec = pairs
    out: list[list[float]] = []
    try:
        items = list(spec)
    except TypeError:
        return None
    for item in items:
        try:
            if isinstance(item, dict):
                um = float(item.get("d50_um"))
                fr = float(item.get("fraction", 0.0))
            else:
                um = float(item[0])
                fr = float(item[1])
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        if not (um > 0.0) or fr < 0.0:
            continue
        out.append([min(max(um, GRAIN_UM_MIN), GRAIN_UM_MAX), fr])
    if len(out) < 2:
        return None
    out.sort(key=lambda p: p[0])
    return out[:6]


def classify_substance(substance: str) -> tuple[str, str | dict[str, float] | None]:
    """Route a substance string to its TELEMAC class + the class payload.

    Order matters: oil first, then decay, then sediment (grain names OR the
    scour/grading vocabularies), else the conservative tracer - so ``oil`` stays
    oil, ``sewage`` stays decay, ``sand`` and ``scour`` are sediment, and a bare
    ``dye`` stays a tracer.
    """
    s = str(substance or "dye").strip().lower()
    for key, preset in OIL_SUBSTANCE_PRESETS.items():
        if key in s:
            return "oil", preset
    for key, params in DECAY_SUBSTANCE_PRESETS.items():
        if key in s:
            return "decay", dict(params)
    for key, params in SEDIMENT_SUBSTANCE_PRESETS.items():
        if key in s:
            return "sediment", dict(params)
    if any(w in s for w in SCOUR_KEYWORDS) or any(w in s for w in GRADATION_KEYWORDS):
        return "sediment", {"type": "sand", "grain_size": 200.0}
    return "tracer", None


def arm_sediment_modules(
    substance: str,
    *,
    erodible_bed: bool | None,
    sediment_gradation: list | str | None,
    dredging: bool | None,
) -> tuple[bool, list[list[float]] | None, bool]:
    """Decide which GAIA modules the ask arms. Returns ``(erodible, gradation, dredging)``.

    Unset flags auto-arm from the substance vocabulary; an explicit True/False
    always wins. A graded mixture and a NESTOR dig rule both need a MOBILE bed to
    act on, so either one forces the erodible bed.
    """
    s = str(substance or "").lower()
    erodible = (bool(erodible_bed) if erodible_bed is not None
                else any(w in s for w in SCOUR_KEYWORDS))

    gradation = resolve_gradation(sediment_gradation)
    if gradation is None and any(w in s for w in GRADATION_KEYWORDS):
        gradation = list(GRADATION_PRESETS["graded_sand"])
    if gradation:
        erodible = True

    dredge = (bool(dredging) if dredging is not None
              else any(w in s for w in DREDGE_KEYWORDS))
    if dredge:
        erodible = True
    return erodible, gradation, dredge


def resolve_decay_law(payload: Any, half_life_hours: float | None,
                      rate_per_day: float | None) -> tuple[int, float]:
    """The WAQTEL degradation law + coefficient for a decaying substance.

    The class preset supplies a narrated literature default; an explicit
    half-life switches to first-order law 2 (k = ln2/hl, h^-1) and an explicit
    per-day rate to law 3. These are USER parameters with narrated defaults -
    never fabricated observations.
    """
    import math

    law, coef = 1, 2.0
    if isinstance(payload, dict):
        law = int(payload.get("law", 1))
        coef = float(payload.get("coef", 2.0))
    if half_life_hours is not None and float(half_life_hours) > 0.0:
        return 2, round(math.log(2.0) / float(half_life_hours), 6)
    if rate_per_day is not None and float(rate_per_day) > 0.0:
        return 3, round(float(rate_per_day), 6)
    return law, coef


def resolve_grain(payload: Any, sediment_type: str | None,
                  grain_size_um: float | None) -> tuple[str, float]:
    """The sediment type + d50 (microns) the sheet carries, params overriding presets."""
    sed_type, sed_grain_um = "sand", 200.0
    if isinstance(payload, dict):
        sed_grain_um = float(payload.get("grain_size", 200.0))
        sed_type = str(payload.get("type", "sand"))
    if sediment_type is not None and str(sediment_type).strip():
        sed_type = sanitize_substance(sediment_type, limit=8, default=sed_type)
    if grain_size_um is not None:
        sed_grain_um = float(grain_size_um)
    return sed_type, float(min(max(sed_grain_um, GRAIN_UM_MIN), GRAIN_UM_MAX))


def substance_class() -> Any:
    """A coercion reconciling ``substance`` with a separately-named ``contaminant``.

    Models split intent across the two fields - ``substance="dye"`` AND
    ``contaminant="crude oil"`` - so an oil spill silently ran the tracer class.
    Any NON-tracer contaminant class wins over a tracer-class substance.

    NEITHER field supplied leaves NO row. A coercion's output merges into the
    door-1 supplied sheet, so emitting the tracer default here would resolve it
    through the USER door and report the template's own default as "supplied on
    this invocation". Abstaining lets the declared default seat through its own
    door with its own basis.
    """

    def _coerce(args: Any) -> dict[str, Any]:
        supplied = sanitize_substance(args.get("substance"), default="")
        substance = supplied or sanitize_substance(None)
        contaminant = args.get("contaminant")
        if contaminant:
            cont = sanitize_substance(contaminant, default="")
            if cont and classify_substance(substance)[0] == "tracer" \
                    and classify_substance(cont)[0] != "tracer":
                logger.info("substance %r is tracer-class but contaminant %r is "
                            "%s-family - classifying by contaminant", substance, cont,
                            classify_substance(cont)[0])
                return {"substance": cont}
        return {"substance": supplied} if supplied else {}

    _coerce.__name__ = "substance_class"
    return _coerce
