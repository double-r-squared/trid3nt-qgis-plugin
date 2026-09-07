"""The WAQTEL wrapper: its catalog, and the two coupled bodies a carrier names.

WAQTEL runs UNDER a hydrodynamic module rather than on its own. A carrier's
template names one of the bodies here; the body's slots serialize into WAQTEL's
own steering file, and the carrier's COUPLING WITH, WAQTEL STEERING FILE and
WATER QUALITY PROCESS land on the carrier's sheet where the engine reads them.

A body here states only what its CALLER handed it. Every keyword it leaves
unsaid is the dictionary's default, which is the wrapper's whole position - a
constant written in here would be an opinion wearing a requirement's clothes,
and it would reach every template that ever names the body.

WAQTEL binds no OUTPUTS. It writes no result file of its own: the oxygen and the
organic load are tracers on the carrier's result, read by the carrier's reader,
so a binding here would claim a file that does not exist.
"""

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

        The nametrac branch applies a decay SINK to every user tracer the carrier
        already carries, so the coupling adds no tracer of its own: the law and
        its coefficient are the whole statement. Both keywords are sized to the
        carrier's tracer count, which is one.
        """
        return _body(17, LAW_OF_TRACERS_DEGRADATION=[law],
                     COEFFICIENT_1_FOR_LAW_OF_TRACERS_DEGRADATION=[coefficient])

    @classmethod
    def o2(cls, *, water_temp_c: Any, salinity_ppt: Any, k1_per_day: Any,
           k4_per_day: Any, k2_per_day: Any, k2_formula: Any,
           saturation_mgl: Any, benthic_demand: Any, photosynthesis_p: Any,
           respiration_r: Any) -> Mapping[str, Any]:
        """The dissolved-oxygen balance (process 2), as the caller states it.

        Every term the process reads is an argument: the water it runs in, the
        two Streeter-Phelps rates and the formula K2 is read under, the
        saturation the deficit is measured against, and the three sources -
        nitrification, benthic demand, photosynthesis less respiration - a
        template either asks for or zeroes. Which of them this run is about is
        the template's statement, not this wrapper's.

        FORMULA FOR COMPUTING CS is left unwritten, so the dictionary's own
        reads the constant saturation the caller states rather than computing
        one.
        """
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

    A MAPPING and not an object, because the sheet's one ref walk descends
    mappings: a late-bound read inside a coupled body is bound by the carrier's
    fill before the composite ever expands it.
    """
    return {"module": "waqtel", "steering": STEERING_FILENAME,
            "process": process, "slots": dict(slots)}


WAQTEL = _Waqtel
