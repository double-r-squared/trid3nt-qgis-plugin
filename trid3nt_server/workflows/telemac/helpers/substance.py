"""What was released, and what its class arms under the hydrodynamic solve.

A question is a TEMPLATE now, so nothing here classifies a word into a family.
What survives is the two things a template still asks: the literature die-off a
named decaying substance carries, and the GAIA sediment body a bed question
couples the solve with. Both are values the composites on the wrappers expand;
what decides which SHAPE a sediment body takes is the shape of the value the ask
carried, which is why it is resolved in a producer rather than asserted in a
body.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from trid3nt_server.workflows.runtime import Step

logger = logging.getLogger("trid3nt_server.workflows.telemac.helpers.substance")

__all__ = [
    "DECAY_SUBSTANCE_PRESETS",
    "GRADATION_PRESETS",
    "GRAIN_UM_MAX",
    "GRAIN_UM_MIN",
    "OIL_SUBSTANCE_PRESETS",
    "SEDIMENT_DENSITY_KGM3",
    "Decay",
    "SedimentBed",
    "SuspendedClass",
    "oil_preset",
    "resolve_decay",
    "resolve_gradation",
    "resolve_sediment_bed",
    "resolve_suspended_class",
    "sanitize_substance",
]

_HELPERS = "trid3nt_server.workflows.telemac.helpers"

#: Oil-family substances, as the module preset each names. Matched as
#: substrings; generic ``oil`` is LAST so the specific fuels win.
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

#: First-order DECAY presets. The coupling applies a decay SINK to the tracer the
#: carrier already runs - no new tracer - so the law and its coefficient are the
#: whole statement. ``law``: 1 = T90 bacterial die-off (coef = T90 hours), 2 =
#: first-order k in h^-1, 3 = first-order k in d^-1, per the dictionary's LAW OF
#: TRACERS DEGRADATION. Bacterial keywords carry T90 ~ 2 h, the daylight
#: freshwater fecal-coliform die-off, which is a narrated literature default and
#: never a measured observation.
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

#: Named demo gradations (d50 in microns, initial fraction) - honest demo mixes,
#: never a measured site sieve curve. The fractions are renormalized on use.
GRADATION_PRESETS: dict[str, list[list[float]]] = {
    "graded_sand": [[100.0, 0.34], [400.0, 0.33], [1000.0, 0.33]],
    "poorly_sorted": [[80.0, 0.4], [300.0, 0.3], [1200.0, 0.3]],
    "sand_gravel_bimodal": [[200.0, 0.5], [1800.0, 0.5]],
    "fine_coarse_sand": [[120.0, 0.5], [800.0, 0.5]],
}

#: The grain-size window GAIA's transport formulae are authored for, in microns.
#: ONE constant, and the declared ``grain_size_um`` bounds read the same two
#: numbers from here rather than restating them: a gradation arrives as a LIST,
#: which no per-value door can police.
GRAIN_UM_MIN, GRAIN_UM_MAX = 5.0, 2000.0

#: Quartz density, which is what every non-cohesive class here is made of.
SEDIMENT_DENSITY_KGM3 = 2650.0

#: mg/L -> kg/m3, the unit GAIA's own source keyword reads.
_MGL_TO_KGM3 = 1.0e-3

#: The label a run narrates itself by. Sanitized to alnum + separators because it
#: travels onto the manifest and into layer narration.
_SUBSTANCE_MAX_CHARS = 24


def sanitize_substance(value: Any, *, limit: int = _SUBSTANCE_MAX_CHARS,
                       default: str = "dye") -> str:
    """A safe substance label. Label only - never solver-affecting on its own."""
    kept = "".join(c for c in str(value if value is not None else "").strip().lower()
                   if c.isalnum() or c in " -_")
    return kept[:limit] or default


def oil_preset(named: Any) -> str:
    """The oil preset a named fuel runs under; light crude when unnamed."""
    word = sanitize_substance(named, default="")
    for key, preset in OIL_SUBSTANCE_PRESETS.items():
        if key in word:
            return preset
    return "light_crude"


def resolve_gradation(spec: list | str | None) -> list[list[float]] | None:
    """A gradation ask -> a clean fine-to-coarse ``[[d50_um, fraction], ...]``.

    Accepts a :data:`GRADATION_PRESETS` key, an explicit list of pairs (or
    ``{'d50_um','fraction'}`` dicts), or None. A surviving list of >= 2 classes is
    a MIXTURE; a shorter one is not something that can sort, so it collapses to
    None and the single-class bed stands.
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
                um, fr = float(item[0]), float(item[1])
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        if not (um > 0.0) or fr < 0.0:
            continue
        out.append([min(max(um, GRAIN_UM_MIN), GRAIN_UM_MAX), fr])
    if len(out) < 2:
        return None
    out.sort(key=lambda pair: pair[0])
    out = out[:6]
    total = sum(fr for _um, fr in out)
    equal = 1.0 / len(out)
    return [[um, (fr / total if total > 0.0 else equal)] for um, fr in out]


def Decay(*, substance: Any, half_life_hours: Any,  # noqa: N802
          rate_per_day: Any) -> Step:
    """The tracer degradation this run couples, or nothing at all."""
    return Step(runner=f"{_HELPERS}.substance.resolve_decay", stage="prep",
                kwargs={"substance": substance, "half_life_hours": half_life_hours,
                        "rate_per_day": rate_per_day})


async def resolve_decay(*, substance: Any, half_life_hours: float | None,
                        rate_per_day: float | None) -> dict[str, Any]:
    """The WAQTEL degradation coupling -> ``{"coupling": [...] }`` or nothing.

    An explicit half-life switches to first-order law 2 (k = ln2/hl, in h^-1) and
    an explicit per-day rate to law 3; a named decaying substance carries its own
    narrated literature default. Nothing named and nothing stated is NO decay,
    and the composite reading this expands to no keyword at all.
    """
    from trid3nt_server.workflows.telemac.modules import WAQTEL

    if half_life_hours is not None and float(half_life_hours) > 0.0:
        law, coef = 2, round(math.log(2.0) / float(half_life_hours), 6)
    elif rate_per_day is not None and float(rate_per_day) > 0.0:
        law, coef = 3, round(float(rate_per_day), 6)
    else:
        word = sanitize_substance(substance, default="")
        preset = next((dict(v) for k, v in DECAY_SUBSTANCE_PRESETS.items()
                       if k in word), None)
        if preset is None:
            return {"coupling": None, "note": "no decay was named or stated"}
        law, coef = int(preset["law"]), float(preset["coef"])
    logger.info("telemac decay coupling: law=%d coef=%.4g", law, coef)
    return {"coupling": [WAQTEL.decay(law=law, coefficient=coef)],
            "law": law, "coefficient": coef,
            "note": f"first-order tracer degradation, law {law} coefficient {coef:g}"}


def SedimentBed(*, gradation: Any, grain_size_um: Any,  # noqa: N802
                bed_thickness_m: Any, bedload_formula: Any,
                morphological_factor: Any, settled: Any,
                injected: Any) -> Step:
    """The erodible bed this run scours, in the shape the ask carried.

    It reads the SETTLED run rather than the dredge fields inside it: whether a
    dredge rule was cut at all is this producer's question, and a ref to a field
    the run holds as nothing is refused at binding.
    """
    return Step(runner=f"{_HELPERS}.substance.resolve_sediment_bed", stage="prep",
                kwargs={"gradation": gradation, "grain_size_um": grain_size_um,
                        "bed_thickness_m": bed_thickness_m,
                        "bedload_formula": bedload_formula,
                        "morphological_factor": morphological_factor,
                        "settled": settled, "injected": injected})


async def resolve_sediment_bed(*, gradation: Any, grain_size_um: float,
                               bed_thickness_m: float, bedload_formula: int,
                               morphological_factor: float,
                               settled: dict[str, Any], injected: dict[str, Any]
                               ) -> dict[str, Any]:
    """The GAIA body the bed is solved as -> the coupling and what it injected.

    A MIXTURE and a single class are two shapes of one value, not two questions:
    both scour the same bed with the same formula, and the mixture additionally
    SORTS under a hiding factor. Which one this run is comes from the shape of
    the gradation the ask carried.
    """
    from trid3nt_server.workflows.telemac.modules import GAIA
    from trid3nt_server.workflows.telemac.modules.gaia import Dredging

    classes = resolve_gradation(gradation)
    dredging = settled.get("dredging")
    dig = (None if not dredging else
           Dredging(action=dredging["action"], polygon=dredging["polygon"],
                    surface_ref=dredging["surface_ref"]))
    common = dict(geometry="river.slf", boundary="river.cli",
                  density=SEDIMENT_DENSITY_KGM3,
                  thickness_m=max(float(bed_thickness_m), 0.01),
                  formula=int(bedload_formula),
                  morphological_factor=max(float(morphological_factor), 1.0),
                  dredging=dig)
    body = (GAIA.graded(classes=[(um, fr) for um, fr in classes], **common)
            if classes else GAIA.erodible(d50_um=float(grain_size_um), **common))
    logger.info("telemac sediment bed: %s classes, dredging=%s",
                len(classes) if classes else 1, dig is not None)
    return {"coupling": [body], "n_classes": len(classes) if classes else 1,
            "injected_kg": _injected_kg(injected)}


def SuspendedClass(*, grain_size_um: Any, concentration_mgl: Any,  # noqa: N802
                   injected: Any) -> Step:
    """The one settling class this run carries over a bed with no stock."""
    return Step(runner=f"{_HELPERS}.substance.resolve_suspended_class", stage="prep",
                kwargs={"grain_size_um": grain_size_um,
                        "concentration_mgl": concentration_mgl,
                        "injected": injected})


async def resolve_suspended_class(*, grain_size_um: float,
                                  concentration_mgl: float,
                                  injected: dict[str, Any]) -> dict[str, Any]:
    """The GAIA suspension body -> the coupling and what it injected."""
    from trid3nt_server.workflows.telemac.modules import GAIA

    body = GAIA.suspended(
        geometry="river.slf", boundary="river.cli",
        d50_um=float(grain_size_um), density=SEDIMENT_DENSITY_KGM3,
        concentration_kgm3=max(float(concentration_mgl) * _MGL_TO_KGM3, 0.0))
    return {"coupling": [body], "n_classes": 1,
            "injected_kg": _injected_kg(injected)}


def _injected_kg(injected: dict[str, Any]) -> float:
    """What the pulse PUT IN: discharge x concentration x window, in kilograms.

    The deposit fraction is measured against this rather than against an assumed
    load, so it is the run's own statement of what it released.
    """
    return round(float(injected["q_m3s"])
                 * max(float(injected["concentration_mgl"]) * _MGL_TO_KGM3, 0.0)
                 * float(injected["window_s"]), 3)
