"""The GAIA wrapper: its module input and the two sediment composites.

GAIA runs UNDER a hydrodynamic module and states no value of its own: the bed
and the suspension expand what a template handed them, and the density the
dictionary defaults to (quartz) is unwritten. Cohesive sediment is approximated
as very fine non-cohesive; the Krone/Partheniades path is not exposed."""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Mapping

from .module import Module
from .outputs import PRIMITIVES

__all__ = ["GAIA", "GRAIN_UM_MAX", "GRAIN_UM_MIN", "STEERING_FILENAME",
           "RESULT_FILENAME", "VARIABLES", "Bed", "Suspension"]

#: The module's variable vocabulary, by the mnemonic VARIABLES FOR GRAPHIC
#: PRINTOUTS spells: the result-file name and the unit. The evolution is
#: cumulative, so its last frame is the whole event's bed change.
VARIABLES: Mapping[str, tuple[str, str]] = MappingProxyType({
    "B": ("BOTTOM", "M"), "E": ("CUMUL BED EVOL", "M"),
    "QS": ("SOLID DISCH", "M2/S"), "D50": ("MEAN DIAMETER", "M"),
    "TOB": ("BED SHEAR STRESS", "N/M2"),
})

STEERING_FILENAME = "gaia_river.cas"
#: GAIA's own result SELAFIN, carrying CUMUL BED EVOL.
RESULT_FILENAME = "gaia_river.slf"


#: The grain-size window the transport formulae are authored for, in microns.
GRAIN_UM_MIN, GRAIN_UM_MAX = 5.0, 2000.0
#: A mixture sorts; fewer classes than this cannot, and the bed is one class.
_MIXTURE_MIN_CLASSES = 2
_MIXTURE_MAX_CLASSES = 6
#: mg/L -> kg/m3, the unit the source concentration keyword reads.
_MGL_TO_KGM3 = 1.0e-3


class _Gaia(Module("gaia")):  # type: ignore[misc]
    """The module input and the two sediment bodies."""

    @classmethod
    def bed(cls, *, geometry: Any, boundary: Any, mass_balance: Any,
            **bed: Any) -> Mapping[str, Any]:
        """A non-cohesive bed with a real stock, one class or a mixture that SORTS.

        ``bed`` is the ``Bed`` value; the shape follows the gradation it resolves."""
        return _body({"GEOMETRY_FILE": geometry, "BOUNDARY_CONDITIONS_FILE": boundary,
                      "RESULTS_FILE": RESULT_FILENAME, "MASS_BALANCE": mass_balance,
                      "bed": Bed(**bed)})

    @classmethod
    def suspended(cls, *, geometry: Any, boundary: Any, mass_balance: Any,
                  **suspension: Any) -> Mapping[str, Any]:
        """ONE settling class over a bed with NO stock: supply-limited.

        Zero thickness, so only the pulse deposits; a SECOND carrier tracer."""
        return _body({"GEOMETRY_FILE": geometry, "BOUNDARY_CONDITIONS_FILE": boundary,
                      "RESULTS_FILE": RESULT_FILENAME, "MASS_BALANCE": mass_balance,
                      "suspension": Suspension(**suspension)})


def Bed(*, gradation: Any, presets: Any, d50_um: Any,  # noqa: N802
        thickness_m: Any, formula: Any, hiding_factor_formula: Any,
        morphological_factor: Any, printouts: Any,
        mixture_printouts: Any) -> Mapping[str, Any]:
    """The erodible bed as one value: a gradation (a preset name in ``presets``
    or fine-to-coarse ``[d50_um, fraction]`` pairs) or the single ``d50_um``."""
    return {"gradation": gradation, "presets": presets, "d50_um": d50_um,
            "thickness_m": thickness_m, "formula": formula,
            "hiding_factor_formula": hiding_factor_formula,
            "morphological_factor": morphological_factor,
            "printouts": printouts, "mixture_printouts": mixture_printouts}


def Suspension(*, d50_um: Any, concentration_mgl: Any,  # noqa: N802
               transport_formula: Any, advection_scheme: Any,
               printouts: Any) -> Mapping[str, Any]:
    """The one settling class as one value, its source concentration in mg/L."""
    return {"d50_um": d50_um, "concentration_mgl": concentration_mgl,
            "transport_formula": transport_formula,
            "advection_scheme": advection_scheme, "printouts": printouts}


def _classes(gradation: Any, presets: Mapping[str, Any]
             ) -> list[tuple[float, float]] | None:
    """A gradation -> clean fine-to-coarse ``(d50_um, fraction)`` pairs, the
    fractions renormalized; ``None`` where fewer than two classes came."""
    if gradation is None:
        return None
    if isinstance(gradation, str):
        gradation = dict(presets).get(gradation.strip().lower().replace(" ", "_"))
        if gradation is None:
            return None
    pairs = []
    for item in list(gradation):
        um, fraction = ((float(item["d50_um"]), float(item.get("fraction", 0.0)))
                        if isinstance(item, Mapping)
                        else (float(item[0]), float(item[1])))
        if um > 0.0 and fraction >= 0.0:
            pairs.append((_windowed(um), fraction))
    if len(pairs) < _MIXTURE_MIN_CLASSES:
        return None
    pairs = sorted(pairs)[:_MIXTURE_MAX_CLASSES]
    total = sum(fraction for _, fraction in pairs)
    return [(um, fraction / total if total > 0.0 else 1.0 / len(pairs))
            for um, fraction in pairs]


def _windowed(micron: float) -> float:
    """A diameter inside the window the formulae are authored for, or a refusal."""
    if not (GRAIN_UM_MIN <= micron <= GRAIN_UM_MAX):
        raise ValueError(
            f"a {micron:g} um class is outside the {GRAIN_UM_MIN:g}-{GRAIN_UM_MAX:g} "
            "um window GAIA's transport formulae are authored for.")
    return micron


def _bed(value: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """The bed value -> the CLASSES lists, the stock, bedload, and what it prints."""
    classes = _classes(value["gradation"], value["presets"])
    if classes:
        shape = {"CLASSES_TYPE_OF_SEDIMENT": ["NCO" for _ in classes],
                 "CLASSES_SEDIMENT_DIAMETERS": [_metres(um) for um, _ in classes],
                 "CLASSES_INITIAL_FRACTION": [fraction for _, fraction in classes],
                 "HIDING_FACTOR_FORMULA": int(value["hiding_factor_formula"]),
                 "VARIABLES_FOR_GRAPHIC_PRINTOUTS": str(value["mixture_printouts"])}
    else:
        shape = {"CLASSES_TYPE_OF_SEDIMENT": ["NCO"],
                 "CLASSES_SEDIMENT_DIAMETERS": [_metres(_windowed(float(value["d50_um"])))],
                 "VARIABLES_FOR_GRAPHIC_PRINTOUTS": str(value["printouts"])}
    return ({**shape, "BED_LOAD_FOR_ALL_SANDS": True,
             "BED_LOAD_TRANSPORT_FORMULA_FOR_ALL_SANDS": int(value["formula"]),
             "LAYERS_INITIAL_THICKNESS": [max(float(value["thickness_m"]), 0.01)],
             "MORPHOLOGICAL_FACTOR": max(float(value["morphological_factor"]), 1.0)},
            {})


def _suspension(value: Mapping[str, Any]) -> tuple[Mapping[str, Any],
                                                   Mapping[str, Any]]:
    """The suspension value -> one class, suspension armed, no stock to erode."""
    return ({"CLASSES_TYPE_OF_SEDIMENT": ["NCO"],
             "CLASSES_SEDIMENT_DIAMETERS": [_metres(_windowed(float(value["d50_um"])))],
             "SUSPENSION_FOR_ALL_SANDS": True,
             "SUSPENSION_TRANSPORT_FORMULA_FOR_ALL_SANDS": int(value["transport_formula"]),
             "LAYERS_INITIAL_THICKNESS": [0.0],
             "SCHEME_FOR_ADVECTION_OF_SUSPENDED_SEDIMENTS": [
                 int(v) for v in value["advection_scheme"]],
             "SUSPENDED_SEDIMENTS_CONCENTRATION_VALUES_AT_THE_SOURCES": [
                 max(float(value["concentration_mgl"]) * _MGL_TO_KGM3, 0.0)],
             "VARIABLES_FOR_GRAPHIC_PRINTOUTS": str(value["printouts"])}, {})


def _metres(micron: Any) -> float:
    """A diameter stated in the micron the question is asked in, as GAIA's metres."""
    return float(micron) * 1.0e-6


def _body(slots: Mapping[str, Any]) -> Mapping[str, Any]:
    """One coupled body, as the carrier's ``coupling`` composite reads it."""
    return {"module": "gaia", "steering": STEERING_FILENAME,
            "slots": dict(slots)}


GAIA = _Gaia
GAIA.VARIABLES = VARIABLES
#: The result the primitives read: GAIA writes its own file beside the carrier's.
GAIA.RESULT_FILE = RESULT_FILENAME
GAIA.composites(bed=_bed, suspension=_suspension)
GAIA.outputs(**PRIMITIVES)
