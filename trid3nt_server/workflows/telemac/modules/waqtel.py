"""The WAQTEL wrapper: its catalog, and the two coupled bodies a carrier names.

WAQTEL runs UNDER a hydrodynamic module rather than on its own. A carrier's
template names one of the bodies here; the body's slots serialize into WAQTEL's
own steering file, and the carrier's COUPLING WITH, WAQTEL STEERING FILE and
WATER QUALITY PROCESS land on the carrier's sheet where the engine reads them.

The bodies assert what the PROCESS needs stated and nothing else. Every keyword
they leave unsaid is the dictionary's default, which is the wrapper's whole
position - a body that restated a default would be an opinion wearing a
requirement's clothes.

WAQTEL binds no OUTPUTS. It writes no result file of its own: the oxygen and the
organic load are tracers on the carrier's result, read by the carrier's reader,
so a binding here would claim a file that does not exist.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Mapping

from .module import Module

__all__ = ["WAQTEL", "STEERING_FILENAME"]

#: The WAQTEL steering file a carrier names. DAMOCLES parses it against WAQTEL's
#: own dictionary, so it is a sheet of its own rather than a block in the
#: carrier's deck.
STEERING_FILENAME = "t2d_river.waqtel"


class _Waqtel(Module("waqtel")):  # type: ignore[misc]
    """The catalog, plus the coupled bodies the carrier's ``coupling`` expands."""

    @classmethod
    def decay(cls, *, law: Any, coefficient: Any) -> Mapping[str, Any]:
        """First-order tracer DEGRADATION (process 17) over the carrier's tracers.

        The nametrac branch applies a decay SINK to every user tracer the carrier
        already carries, so the coupling adds no tracer of its own: the law and
        its coefficient are the whole statement. Both keywords are sized to the
        carrier's tracer count, which is one.
        """
        return _body(17, LAW_OF_TRACERS_DEGRADATION=[law],
                     COEFFICIENT_1_FOR_LAW_OF_TRACERS_DEGRADATION=[coefficient])

    @classmethod
    def o2(cls, *, water_temp_c: Any, k1_per_day: Any, k2_per_day: Any,
           k2_formula: Any, saturation_mgl: Any) -> Mapping[str, Any]:
        """The dissolved-oxygen SAG (process 2) - the Streeter-Phelps pair.

        Deoxygenation balanced by surface reaeration, and nothing else: the
        eutrophication and benthic sources are zeroed and nitrification is off,
        so the modelled curve is the closed form the question is asked against.
        A zero formula for K2 reads the constant reaeration coefficient instead
        of computing one from the modelled velocity and depth, and a zero formula
        for CS reads the constant saturation stated here.
        """
        return _body(
            2, WATER_TEMPERATURE=water_temp_c, WATER_SALINITY=0.0,
            CONSTANT_OF_DEGRADATION_OF_ORGANIC_LOAD_K1=k1_per_day,
            CONSTANT_OF_NITRIFICATION_KINETIC_K4=0.0,
            FORMULA_FOR_COMPUTING_K2=k2_formula,
            K2_REAERATION_COEFFICIENT=k2_per_day,
            FORMULA_FOR_COMPUTING_CS=0,
            O2_SATURATION_DENSITY_OF_WATER__CS_=saturation_mgl,
            BENTHIC_DEMAND=0.0, PHOTOSYNTHESIS_P=0.0, VEGETAL_RESPIRATION_R=0.0)


def _body(process: int, **slots: Any) -> Mapping[str, Any]:
    """One coupled body, as the carrier's ``coupling`` composite reads it.

    A MAPPING and not an object, because the sheet's one ref walk descends
    mappings: a late-bound read inside a coupled body is bound by the carrier's
    fill before the composite ever expands it.
    """
    return MappingProxyType({"module": "waqtel", "steering": STEERING_FILENAME,
                             "process": process,
                             "slots": MappingProxyType(dict(slots))})


WAQTEL = _Waqtel
