"""The WAQTEL wrapper: its dictionary, and the coupled bodies a carrier names.

WAQTEL runs UNDER a hydrodynamic module and states only what its caller handed
it. It writes no result file of its own and rows no output: what its processes
produce are TRACERS APPENDED to the carrier's own, which it states by process."""

from __future__ import annotations

import math
from types import MappingProxyType
from typing import Any, Mapping

from .module import Module, Output, SlotRefused

__all__ = ["WAQTEL", "STEERING_FILENAME"]

#: The processes a carrier's WATER QUALITY PROCESS carries. The engine dispatches
#: with ``IF( p*INT(WAQPROCESS/p).EQ.WAQPROCESS )`` and so reaches every process
#: whose PRIME divides the keyword; 1 is the dictionary's default and is no
#: process at all.
_O2 = 2
_BIOMASS = 3
_EUTRO = 5
_MICROPOL = 7
_THERMAL = 11
_DEGRADATION = 17

#: One style per appended variable, stated where the rows that carry it are. A
#: variable two processes both write draws the same way under either.
_TEMPERATURE = {"kind": "mesh", "ramp": "rdylbu_r", "units": "C"}
_O2_STYLE = {"kind": "mesh", "ramp": "rdylbu", "units": "mg/L", "floor": 0}
_ORGANIC = {"kind": "mesh", "ramp": "oranges", "units": "mg/L", "floor": 0}
_NH4 = {"kind": "mesh", "ramp": "magma", "units": "mg/L", "floor": 0}
_ALGAE = {"kind": "mesh", "ramp": "greens", "units": "ug/L", "floor": 0}
_PO4 = {"kind": "mesh", "ramp": "ylorrd", "units": "mg/L", "floor": 0}
_POR = {"kind": "mesh", "ramp": "plasma", "units": "mg/L", "floor": 0}
_NO3 = {"kind": "mesh", "ramp": "gnbu", "units": "mg/L", "floor": 0}
_NOR = {"kind": "mesh", "ramp": "cividis", "units": "mg/L", "floor": 0}
_SUSPENDED = {"kind": "mesh", "ramp": "oranges", "units": "mg/L", "floor": 0}
_DEPOSITED = {"kind": "mesh", "ramp": "ylorrd", "units": "mg/L", "floor": 0}
_DISSOLVED = {"kind": "mesh", "ramp": "reds", "units": "mg/L", "floor": 0}
_ON_SUSPENDED = {"kind": "mesh", "ramp": "magma", "units": "mg/L", "floor": 0}
_ON_DEPOSITED = {"kind": "mesh", "ramp": "plasma", "units": "mg/L", "floor": 0}

#: What each process puts on the carrier's result, in the order the engine
#: appends them behind the tracers the carrier declares, under the engine's own
#: 16-character names. A carrier that declares one of these names keeps its own
#: row and the process attaches to it. Degradation (17) acts on a tracer the
#: carrier already has, so it appends none. The third algal tracer is one
#: quantity under two spellings - EUTRO writes ``POR NON ASSIMIL``, BIOMASS the
#: shorter ``POR NON ASSIM`` - so the two row sets cannot share it.
_APPENDED: Mapping[int, tuple[Output, ...]] = MappingProxyType({
    _O2: (Output("DISSOLVED O2", "mgO2/L", style=_O2_STYLE),
          Output("ORGANIC LOAD", "mgO2/L", style=_ORGANIC),
          Output("NH4 LOAD", "mg/L", style=_NH4)),
    _BIOMASS: (Output("PHYTO BIOMASS", "ug/L", style=_ALGAE),
               Output("DISSOLVED PO4", "mg/L", style=_PO4),
               Output("POR NON ASSIM", "mg/L", style=_POR),
               Output("DISSOLVED NO3", "mg/L", style=_NO3),
               Output("NOR NON ASSIM", "mg/L", style=_NOR)),
    _EUTRO: (Output("PHYTO BIOMASS", "ug/L", style=_ALGAE),
             Output("DISSOLVED PO4", "mg/L", style=_PO4),
             Output("POR NON ASSIMIL", "mg/L", style=_POR),
             Output("DISSOLVED NO3", "mg/L", style=_NO3),
             Output("NOR NON ASSIM", "mg/L", style=_NOR),
             Output("NH4 LOAD", "mg/L", style=_NH4),
             Output("ORGANIC LOAD", "mgO2/L", style=_ORGANIC),
             Output("DISSOLVED O2", "mgO2/L", style=_O2_STYLE)),
    _MICROPOL: (Output("SUSPENDED LOAD", "mg/L", style=_SUSPENDED),
                Output("BED SEDIMENTS", "mg/L", style=_DEPOSITED),
                Output("MICRO POLLUTANT", "mg/L", style=_DISSOLVED),
                Output("ABS. SUSP. LOAD.", "mg/L", style=_ON_SUSPENDED),
                Output("ABSORB. BED SED.", "mg/L", style=_ON_DEPOSITED)),
    _THERMAL: (Output("TEMPERATURE", "oC", style=_TEMPERATURE),),
})

#: The ELEVEN keywords EUTRO reads and BIOMASS does not - every one a term in the
#: oxygen, organic-load and ammonium balance the three extra tracers carry. A
#: body that states one of them under BIOMASS is stating a number nobody reads.
_OXYGEN_ONLY = frozenset((
    "CONSTANT_OF_DEGRADATION_OF_ORGANIC_LOAD_K120",
    "CONSTANT_FOR_THE_NITRIFICATION_KINETIC_K520",
    "OXYGEN_PRODUCED_BY_PHOTOSYNTHESIS",
    "CONSUMED_OXYGEN_BY_NITRIFICATION",
    "BENTHIC_DEMAND",
    "K2_REAERATION_COEFFICIENT",
    "FORMULA_FOR_COMPUTING_K2",
    "O2_SATURATION_DENSITY_OF_WATER__CS_",
    "FORMULA_FOR_COMPUTING_CS",
    "SEDIMENTATION_VELOCITY_OF_ORGANIC_LOAD",
    "WATER_SALINITY",
))

#: The second sorption site. Its model number appends two more tracers, which
#: moves the MICROPOL rows above and every array a carrier sizes to its tracer
#: count, so the one-site model is what is exposed.
_TWO_SITE = frozenset(("KINETIC_EXCHANGE_MODEL", "COEFFICIENT_OF_DISTRIBUTION_2",
                       "CONSTANT_OF_DESORPTION_KINETIC_2"))

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
        return {**_body(_DEGRADATION,
                        degradation={"substance": substance,
                                     "half_life_hours": half_life_hours,
                                     "rate_per_day": rate_per_day,
                                     "presets": presets}),
                "given": [substance, half_life_hours, rate_per_day]}

    @classmethod
    def thermal(cls, **keywords: Any) -> Mapping[str, Any]:
        """The heat budget between the water and the air (process 11), which
        appends a TEMPERATURE tracer to the carrier's own.

        The forcing is the HOST's: the exchange reads the air temperature, the
        humidity, the wind, the cloud and the solar radiation out of the
        atmospheric data file the carrier names. ATMOSPHERE-WATER EXCHANGE MODEL
        is the 3D surface-exchange switch, and its engine default - no model - is
        the only value a 2D run takes."""
        return cls._process(_THERMAL, keywords)

    @classmethod
    def o2(cls, **keywords: Any) -> Mapping[str, Any]:
        """The dissolved-oxygen balance (process 2), which appends the oxygen,
        the organic load and the ammonium to the carrier's own tracers."""
        return cls._process(_O2, keywords)

    @classmethod
    def micropollutant(cls, **keywords: Any) -> Mapping[str, Any]:
        """A sorbing substance and the sediment it rides on (process 7), which
        appends the sediment in suspension and on the bed and the substance
        dissolved, on the suspended sediment and on the bed sediment.

        The settling and the bed exchange, the sorption equilibrium and its
        kinetics and the substance's own decay are WAQTEL's own keywords."""
        named = sorted(set(keywords) & _TWO_SITE)
        if named:
            raise SlotRefused(
                f"{', '.join(named)} puts MICROPOL on two sorption sites, which "
                "writes seven tracers rather than the five this process rows; the "
                "one-site model is what is exposed.")
        return cls._process(_MICROPOL, keywords)

    @classmethod
    def eutrophication(cls, *, oxygen: bool = False,
                       **keywords: Any) -> Mapping[str, Any]:
        """Nutrient-limited algal growth over the carrier's water, and the oxygen
        balance it drives where the caller asks for the oxygen half.

        ``oxygen`` is the whole of the choice between the engine's two algal
        source terms - EUTRO's five tracers plus the three the oxygen balance is
        carried on, or BIOMASS's five alone."""
        if not oxygen:
            named = sorted(set(keywords) & _OXYGEN_ONLY)
            if named:
                raise SlotRefused(
                    f"{', '.join(named)} is read by the eutrophication source "
                    "term and not by the biomass one, so process 3 would drop it "
                    "silently; ask for oxygen=True or drop the keyword.")
        return cls._process(_EUTRO if oxygen else _BIOMASS, keywords)

    @classmethod
    def _process(cls, number: int, keywords: Mapping[str, Any]) -> Mapping[str, Any]:
        """One coupled body: the process, and the keywords stated by their own
        name. An unknown keyword refuses at IMPORT, naming the nearest."""
        for name in keywords:
            cls.slot(name)
        return _body(number, **keywords)


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
WAQTEL.APPENDABLE = tuple((f"process {process}", rows)
                          for process, rows in sorted(_APPENDED.items()))
WAQTEL.composites(degradation=_degradation)
WAQTEL.appends(_appended)
