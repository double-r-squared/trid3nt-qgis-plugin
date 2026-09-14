"""The WAQTEL wrapper: its dictionary, and the two coupled bodies a carrier names.

WAQTEL runs UNDER a hydrodynamic module and states only what its caller handed
it. It writes no result file of its own and rows no output: what its processes
produce are TRACERS APPENDED to the carrier's own, which it states by process."""

from __future__ import annotations

import math
from typing import Any, Mapping

from .module import Module, Output

__all__ = ["WAQTEL", "STEERING_FILENAME"]

#: What each process puts on the carrier's result, in the order the engine
#: appends them behind the tracers the carrier declares. Degradation (17) acts
#: on a tracer the carrier already has, so it appends none.
_APPENDED: Mapping[int, tuple[Output, ...]] = {
    2: (Output("DISSOLVED O2", "mgO2/L",
               style={"kind": "mesh", "ramp": "rdylbu", "units": "mg/L",
                      "floor": 0}),
        Output("ORGANIC LOAD", "mgO2/L",
               style={"kind": "mesh", "ramp": "oranges", "units": "mg/L",
                      "floor": 0}),
        Output("NH4 LOAD", "mg/L",
               style={"kind": "mesh", "ramp": "magma", "units": "mg/L",
                      "floor": 0})),
}

#: Hours to the per-hour law and days to the per-day law, as the dictionary's
#: LAW OF TRACERS DEGRADATION numbers them; a named substance reads its preset,
#: which carries a law and a coefficient of its own.
_LAW_PER_HOUR = 2
_LAW_PER_DAY = 3

#: The WAQTEL steering file a carrier names. DAMOCLES parses it against WAQTEL's
#: own dictionary, so it is a sheet of its own rather than a block in the
#: carrier's deck.
STEERING_FILENAME = "t2d_river.waqtel"


class _Waqtel(Module("waqtel")):  # type: ignore[misc]
    """The dictionary, plus the coupled bodies the carrier's ``coupling`` expands."""

    @classmethod
    def degradation(cls, *, substance: Any, half_life_hours: Any,
                    rate_per_day: Any, presets: Any) -> Mapping[str, Any]:
        """First-order tracer DEGRADATION (process 17) over the carrier's tracers,
        from a half-life, a per-day rate, or a named substance's preset row.

        Given nothing, the body states nothing and the carrier couples nothing."""
        return {**_body(17, degradation={"substance": substance,
                                         "half_life_hours": half_life_hours,
                                         "rate_per_day": rate_per_day,
                                         "presets": presets}),
                "given": [substance, half_life_hours, rate_per_day]}

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


def _degradation(value: Mapping[str, Any]) -> tuple[Mapping[str, Any],
                                                    Mapping[str, Any]]:
    """The degradation value -> the law and its coefficient, sized to one tracer.

    A stated half-life or rate beats the preset a substance word names."""
    half_life, rate = value.get("half_life_hours"), value.get("rate_per_day")
    if half_life is not None and float(half_life) > 0.0:
        law, coefficient = _LAW_PER_HOUR, round(math.log(2.0) / float(half_life), 6)
    elif rate is not None and float(rate) > 0.0:
        law, coefficient = _LAW_PER_DAY, round(float(rate), 6)
    else:
        word = str(value.get("substance") or "").strip().lower()
        preset = next((row for key, row in dict(value["presets"]).items()
                       if key in word), None)
        if preset is None:
            raise ValueError(
                f"{value.get('substance')!r} names no degradation preset this deck "
                f"carries ({sorted(value['presets'])}); state decay_half_life_hours "
                "or decay_rate_per_day for it.")
        law, coefficient = int(preset["law"]), float(preset["coef"])
    return ({"LAW_OF_TRACERS_DEGRADATION": [law],
             "COEFFICIENT_1_FOR_LAW_OF_TRACERS_DEGRADATION": [coefficient]}, {})


def _body(process: int, **slots: Any) -> Mapping[str, Any]:
    """One coupled body, as the carrier's ``coupling`` composite reads it.

    A MAPPING, not an object: the sheet's one ref walk descends mappings."""
    return {"module": "waqtel", "steering": STEERING_FILENAME,
            "process": process, "slots": dict(slots)}


def _appended(body: Mapping[str, Any]) -> tuple[Output, ...]:
    """The tracers this coupled body puts on its carrier's result, by process."""
    return _APPENDED.get(int(body.get("process") or 0), ())


WAQTEL = _Waqtel
WAQTEL.composites(degradation=_degradation)
WAQTEL.appends(_appended)
