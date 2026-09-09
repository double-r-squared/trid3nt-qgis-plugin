"""The WAQTEL wrapper: its catalog, and the two coupled bodies a carrier names.

WAQTEL runs UNDER a hydrodynamic module and states only what its caller handed
it. It binds no OUTPUTS: it writes no result file of its own, and the oxygen and
the organic load are tracers on the carrier's result."""

from __future__ import annotations

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

        The coupling adds no tracer; both keywords size to the carrier's count."""
        return _body(17, LAW_OF_TRACERS_DEGRADATION=[law],
                     COEFFICIENT_1_FOR_LAW_OF_TRACERS_DEGRADATION=[coefficient])

    @classmethod
    def o2(cls, *, water_temp_c: Any, salinity_ppt: Any, k1_per_day: Any,
           k4_per_day: Any, k2_per_day: Any, k2_formula: Any,
           saturation_mgl: Any, benthic_demand: Any, photosynthesis_p: Any,
           respiration_r: Any) -> Mapping[str, Any]:
        """The dissolved-oxygen balance (process 2), as the caller states it.

        FORMULA FOR COMPUTING CS is unwritten, so the stated constant is read."""
        return _body(
            2, WATER_TEMPERATURE=water_temp_c, WATER_SALINITY=salinity_ppt,
            CONSTANT_OF_DEGRADATION_OF_ORGANIC_LOAD_K1=k1_per_day,
            CONSTANT_OF_NITRIFICATION_KINETIC_K4=k4_per_day,
            FORMULA_FOR_COMPUTING_K2=k2_formula,
            K2_REAERATION_COEFFICIENT=k2_per_day,
            O2_SATURATION_DENSITY_OF_WATER__CS_=saturation_mgl,
            BENTHIC_DEMAND=benthic_demand, PHOTOSYNTHESIS_P=photosynthesis_p,
            VEGETAL_RESPIRATION_R=respiration_r)


def _body(process: int, **slots: Any) -> Mapping[str, Any]:
    """One coupled body, as the carrier's ``coupling`` composite reads it.

    A MAPPING, not an object: the sheet's one ref walk descends mappings."""
    return {"module": "waqtel", "steering": STEERING_FILENAME,
            "process": process, "slots": dict(slots)}


WAQTEL = _Waqtel
