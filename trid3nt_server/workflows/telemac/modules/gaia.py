"""The GAIA wrapper: its dictionary, the sediment bodies, and the NESTOR composite.

GAIA runs UNDER a hydrodynamic module and states no value of its own. Cohesive
sediment is approximated as very fine non-cohesive; the Krone/Partheniades path
is not exposed."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from ..products.postprocess_telemac import postprocess_telemac_deposition
from ..products.run_reads import gaia_mass_balance, surface_d50_spread
from .module import Module

__all__ = ["GAIA", "STEERING_FILENAME", "RESULT_FILENAME",
           "ACTION_FILENAME", "POLYGON_FILENAME", "SURFACE_REF_FILENAME"]

STEERING_FILENAME = "gaia_river.cas"
#: GAIA's own result SELAFIN, carrying CUMUL BED EVOL.
RESULT_FILENAME = "gaia_river.slf"
ACTION_FILENAME = "nestor.act"
POLYGON_FILENAME = "nestor.pol"
SURFACE_REF_FILENAME = "nestor.ref"


class _Gaia(Module("gaia")):  # type: ignore[misc]
    """The dictionary, the three sediment bodies, and NESTOR as one value."""

    @classmethod
    def graded(cls, *, geometry: Any, boundary: Any, classes: Any,
               density: Any, thickness_m: Any, formula: Any,
               hiding_factor_formula: Any, morphological_factor: Any,
               printouts: Any, mass_balance: Any,
               dredging: Any = None) -> Mapping[str, Any]:
        """A MIXTURE of non-cohesive classes over one erodible bed, which SORTS.

        ``classes`` is the fine-to-coarse pairs all four CLASSES lists come from."""
        return _body(cls._sediment(
            geometry, boundary, dredging, printouts, mass_balance,
            CLASSES_TYPE_OF_SEDIMENT=["NCO" for _ in classes],
            CLASSES_SEDIMENT_DIAMETERS=[_metres(um) for um, _ in classes],
            CLASSES_SEDIMENT_DENSITY=[density for _ in classes],
            CLASSES_INITIAL_FRACTION=[fraction for _, fraction in classes],
            BED_LOAD_FOR_ALL_SANDS=True,
            BED_LOAD_TRANSPORT_FORMULA_FOR_ALL_SANDS=formula,
            HIDING_FACTOR_FORMULA=hiding_factor_formula,
            LAYERS_INITIAL_THICKNESS=[thickness_m],
            MORPHOLOGICAL_FACTOR=morphological_factor))

    @classmethod
    def erodible(cls, *, geometry: Any, boundary: Any, d50_um: Any, density: Any,
                 thickness_m: Any, formula: Any, morphological_factor: Any,
                 printouts: Any, mass_balance: Any,
                 dredging: Any = None) -> Mapping[str, Any]:
        """ONE non-cohesive class over a real sediment stock: bedload scour.

        Suspension stays off, so the carrier's dye is the sole tracer."""
        return _body(cls._sediment(
            geometry, boundary, dredging, printouts, mass_balance,
            CLASSES_TYPE_OF_SEDIMENT=["NCO"],
            CLASSES_SEDIMENT_DIAMETERS=[_metres(d50_um)],
            CLASSES_SEDIMENT_DENSITY=[density],
            CLASSES_INITIAL_FRACTION=[1.0],
            BED_LOAD_FOR_ALL_SANDS=True,
            BED_LOAD_TRANSPORT_FORMULA_FOR_ALL_SANDS=formula,
            LAYERS_INITIAL_THICKNESS=[thickness_m],
            MORPHOLOGICAL_FACTOR=morphological_factor))

    @classmethod
    def suspended(cls, *, geometry: Any, boundary: Any, d50_um: Any, density: Any,
                  concentration_kgm3: Any, transport_formula: Any,
                  advection_scheme: Any, printouts: Any,
                  mass_balance: Any) -> Mapping[str, Any]:
        """ONE settling class over a bed with NO stock: supply-limited.

        Zero thickness, so only the pulse deposits; a SECOND carrier tracer."""
        return _body(cls._sediment(
            geometry, boundary, None, printouts, mass_balance,
            CLASSES_TYPE_OF_SEDIMENT=["NCO"],
            CLASSES_SEDIMENT_DIAMETERS=[_metres(d50_um)],
            CLASSES_SEDIMENT_DENSITY=[density],
            CLASSES_INITIAL_FRACTION=[1.0],
            CLASSES_SETTLING_VELOCITIES=[-9.0],
            SUSPENSION_FOR_ALL_SANDS=True,
            SUSPENSION_TRANSPORT_FORMULA_FOR_ALL_SANDS=transport_formula,
            LAYERS_INITIAL_THICKNESS=[0.0],
            SCHEME_FOR_ADVECTION_OF_SUSPENDED_SEDIMENTS=advection_scheme,
            SUSPENDED_SEDIMENTS_CONCENTRATION_VALUES_AT_THE_SOURCES=[
                concentration_kgm3]))

    @staticmethod
    def _sediment(geometry: Any, boundary: Any, dredging: Any, printouts: Any,
                  mass_balance: Any, **slots: Any) -> dict[str, Any]:
        """What every shape states: the files it reads, and what its caller reads back."""
        return {"GEOMETRY_FILE": geometry, "BOUNDARY_CONDITIONS_FILE": boundary,
                "RESULTS_FILE": RESULT_FILENAME,
                "VARIABLES_FOR_GRAPHIC_PRINTOUTS": printouts, **slots,
                "MASS_BALANCE": mass_balance,
                **({"dredging": dredging} if dredging is not None else {})}


def Dredging(*, action: Any, polygon: Any, surface_ref: Any  # noqa: N802
             ) -> Mapping[str, Any]:
    """NESTOR dig and dump on the erodible bed, as one value.

    The three files ride together: a run naming two is one NESTOR cannot read."""
    return {"action": action, "polygon": polygon,
            "surface_ref": surface_ref}


def _dredging(value: Mapping[str, Any]) -> tuple[Mapping[str, Any],
                                                 Mapping[str, Any]]:
    """The NESTOR value -> the keywords it means and the files they name."""
    if not value["action"]:
        # No dig was cut, so this run states no dredging at all.
        return ({}, {})
    return ({"NESTOR": True, "NESTOR_ACTION_FILE": ACTION_FILENAME,
             "NESTOR_POLYGON_FILE": POLYGON_FILENAME,
             "NESTOR_SURFACE_REFERENCE_FILE": SURFACE_REF_FILENAME},
            {ACTION_FILENAME: value["action"],
             POLYGON_FILENAME: value["polygon"],
             SURFACE_REF_FILENAME: value["surface_ref"]})


def _metres(micron: Any) -> float:
    """A diameter stated in the micron the question is asked in, as GAIA's metres."""
    return float(micron) * 1.0e-6


def _body(slots: Mapping[str, Any]) -> Mapping[str, Any]:
    """One coupled body, as the carrier's ``coupling`` composite reads it."""
    return {"module": "gaia", "steering": STEERING_FILENAME,
            "slots": dict(slots)}


GAIA = _Gaia
GAIA.composites(dredging=_dredging)
GAIA.outputs(deposition=postprocess_telemac_deposition,
             surface_d50=surface_d50_spread, mass_balance=gaia_mass_balance)
