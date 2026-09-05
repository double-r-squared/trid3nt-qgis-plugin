"""The GAIA wrapper: its catalog, the sediment bodies, and the NESTOR composite.

GAIA runs UNDER a hydrodynamic module. A carrier's template names one of the
bodies here; the body's slots serialize into GAIA's own steering file, and the
carrier's COUPLING WITH and GAIA STEERING FILE land on the carrier's sheet.

The three bodies are three different questions, not three settings of one:
GRADED sorts a mixture over an erodible bed, ERODIBLE scours and re-deposits one
class, and SUSPENDED carries one settling class over a bed with no stock at all,
so only what was injected can deposit. Cohesive sediment is approximated as very
fine non-cohesive; the Krone/Partheniades path is not exposed.

GAIA writes its own result SELAFIN and its own share of the listing balance, so
it carries OUTPUTS of its own: the bed evolution the deposition COG is built
from, the surface grading a graded bed sorts into, and the sediment balance.
"""

from __future__ import annotations

from types import MappingProxyType
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
    """The catalog, the three sediment bodies, and NESTOR as one value."""

    @classmethod
    def graded(cls, *, geometry: Any, boundary: Any, classes: Any,
               density: Any, thickness_m: Any, formula: Any,
               morphological_factor: Any,
               dredging: Any = None) -> Mapping[str, Any]:
        """A MIXTURE of non-cohesive classes over one erodible bed.

        The classes are coupled by a hiding factor, so the bed SORTS under a
        flood: fines winnow out of the high-shear thalweg and settle in slack
        water. ``classes`` is the fine-to-coarse ``[(d50_um, fraction), ...]``
        the four parallel CLASSES lists are written from, so the four cannot
        disagree about how many classes there are.
        """
        return _body(cls._sediment(
            geometry, boundary, dredging,
            VARIABLES_FOR_GRAPHIC_PRINTOUTS="B,E,D50",
            CLASSES_TYPE_OF_SEDIMENT=["NCO" for _ in classes],
            CLASSES_SEDIMENT_DIAMETERS=[_metres(um) for um, _ in classes],
            CLASSES_SEDIMENT_DENSITY=[density for _ in classes],
            CLASSES_INITIAL_FRACTION=[fraction for _, fraction in classes],
            BED_LOAD_FOR_ALL_SANDS=True,
            BED_LOAD_TRANSPORT_FORMULA_FOR_ALL_SANDS=formula,
            HIDING_FACTOR_FORMULA=1,
            LAYERS_INITIAL_THICKNESS=[thickness_m],
            MORPHOLOGICAL_FACTOR=morphological_factor))

    @classmethod
    def erodible(cls, *, geometry: Any, boundary: Any, d50_um: Any, density: Any,
                 thickness_m: Any, formula: Any, morphological_factor: Any,
                 dredging: Any = None) -> Mapping[str, Any]:
        """ONE non-cohesive class over a real sediment stock: bedload scour.

        The bed erodes where the flow steepens and re-deposits where it slackens.
        Suspension stays off, which is also what keeps the carrier's dye the sole
        hydrodynamic tracer.
        """
        return _body(cls._sediment(
            geometry, boundary, dredging,
            VARIABLES_FOR_GRAPHIC_PRINTOUTS="B,E",
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
                  concentration_kgm3: Any) -> Mapping[str, Any]:
        """ONE settling class over a bed with NO stock: supply-limited.

        Zero initial thickness, so nothing erodes and only the injected pulse
        deposits. The class arrives at the carrier as a SECOND tracer, which is
        why the carrier's tracer count and its boundary values move with it.
        """
        return _body(cls._sediment(
            geometry, boundary, None,
            VARIABLES_FOR_GRAPHIC_PRINTOUTS="B,E",
            CLASSES_TYPE_OF_SEDIMENT=["NCO"],
            CLASSES_SEDIMENT_DIAMETERS=[_metres(d50_um)],
            CLASSES_SEDIMENT_DENSITY=[density],
            CLASSES_INITIAL_FRACTION=[1.0],
            CLASSES_SETTLING_VELOCITIES=[-9.0],
            SUSPENSION_FOR_ALL_SANDS=True,
            SUSPENSION_TRANSPORT_FORMULA_FOR_ALL_SANDS=3,
            LAYERS_INITIAL_THICKNESS=[0.0],
            SCHEME_FOR_ADVECTION_OF_SUSPENDED_SEDIMENTS=[1],
            SUSPENDED_SEDIMENTS_CONCENTRATION_VALUES_AT_THE_SOURCES=[
                concentration_kgm3]))

    @staticmethod
    def _sediment(geometry: Any, boundary: Any, dredging: Any,
                  **slots: Any) -> dict[str, Any]:
        """What every shape states: which files it reads, and the balance it prints."""
        return {"GEOMETRY_FILE": geometry, "BOUNDARY_CONDITIONS_FILE": boundary,
                "RESULTS_FILE": RESULT_FILENAME, **slots, "MASS_BALANCE": True,
                **({"dredging": dredging} if dredging is not None else {})}


def Dredging(*, action: Any, polygon: Any, surface_ref: Any  # noqa: N802
             ) -> Mapping[str, Any]:
    """NESTOR dig and dump on the erodible bed, as one value.

    The three files ride together because the module reads all three on every
    action: the polygons name the fields, the actions say when and how deep, and
    the surface reference is what each node's chainage and design grade are
    interpolated from - so a run naming two of them is a run NESTOR cannot read.
    """
    return MappingProxyType({"action": action, "polygon": polygon,
                             "surface_ref": surface_ref})


def _dredging(value: Mapping[str, Any]) -> tuple[Mapping[str, Any],
                                                 Mapping[str, Any]]:
    """The NESTOR value -> the keywords it means and the files they name."""
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
    return MappingProxyType({"module": "gaia", "steering": STEERING_FILENAME,
                             "slots": MappingProxyType(dict(slots))})


GAIA = _Gaia
GAIA.composites(dredging=_dredging)
GAIA.outputs(deposition=postprocess_telemac_deposition,
             surface_d50=surface_d50_spread, mass_balance=gaia_mass_balance)
