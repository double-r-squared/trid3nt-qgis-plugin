"""The WAQTEL wrapper: its dictionary, and the coupled bodies a carrier names.

WAQTEL runs UNDER a hydrodynamic module and states only what its caller handed
it. It writes no result file of its own and rows no output: what its processes
produce are TRACERS APPENDED to the carrier's own, which it states by process."""

from __future__ import annotations

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
#:
#: A BACKGROUND variable - one the water already carries everywhere - is ranged
#: over what was measured on the wet nodes and pins no bottom: oxygen sitting
#: between 8.0 and 8.7 mg/L on a ramp pinned to zero spends half a percent of
#: its colours on the whole answer.
_TEMPERATURE = {"kind": "mesh", "ramp": "rdylbu_r", "units": "C"}
_O2_STYLE = {"kind": "mesh", "ramp": "rdylbu", "units": "mg/L"}
_ORGANIC = {"kind": "mesh", "ramp": "oranges", "units": "mg/L"}
_NH4 = {"kind": "mesh", "ramp": "magma", "units": "mg/L"}
_ALGAE = {"kind": "mesh", "ramp": "greens", "units": "ug/L"}
_PO4 = {"kind": "mesh", "ramp": "ylorrd", "units": "mg/L"}
_POR = {"kind": "mesh", "ramp": "plasma", "units": "mg/L"}
_NO3 = {"kind": "mesh", "ramp": "gnbu", "units": "mg/L"}
_NOR = {"kind": "mesh", "ramp": "cividis", "units": "mg/L"}
#: MICROPOL's five, in the units its own source terms are written in: the
#: sediment in suspension is the concentration COEFFICIENT OF DISTRIBUTION is
#: read against, which the dictionary states in m3/kg, so it is kg/m3; the bed
#: sediment and the pollutant on it are what SETTLED onto a square metre, which
#: is the same terms without the water column divided out.
_SUSPENDED = {"kind": "mesh", "ramp": "oranges", "units": "kg/m3", "floor": 0}
_DEPOSITED = {"kind": "mesh", "ramp": "ylorrd", "units": "kg/m2", "floor": 0}
_DISSOLVED = {"kind": "mesh", "ramp": "reds", "units": "mg/L", "floor": 0}
_ON_SUSPENDED = {"kind": "mesh", "ramp": "magma", "units": "mg/L", "floor": 0}
_ON_DEPOSITED = {"kind": "mesh", "ramp": "plasma", "units": "g/m2", "floor": 0}

#: What each process puts on the carrier's result, in the order the engine
#: appends them behind the tracers the carrier declares, under the engine's own
#: 16-character names. A carrier that declares one of these names keeps its own
#: row and the process attaches to it. Degradation (17) acts on a tracer the
#: carrier already has, so it appends none. The third algal tracer is one
#: quantity under two spellings - EUTRO writes ``POR NON ASSIMIL``, BIOMASS the
#: shorter ``POR NON ASSIM`` - so the two row sets cannot share it.
#: WHAT HAS AN EDGE. A process variable of the water column - oxygen, a nutrient,
#: the temperature - is everywhere the water is, so masking it below a fraction
#: of its own peak erases the field rather than shaping it. A micropollutant is
#: PUT INTO the water, so the ground it has reached has a boundary to draw.
_APPENDED: Mapping[int, tuple[Output, ...]] = MappingProxyType({
    _O2: (Output("DISSOLVED O2", "mgO2/L", style=_O2_STYLE, has_edge=False),
          Output("ORGANIC LOAD", "mgO2/L", style=_ORGANIC, has_edge=False),
          Output("NH4 LOAD", "mg/L", style=_NH4, has_edge=False)),
    _BIOMASS: (Output("PHYTO BIOMASS", "ug/L", style=_ALGAE, has_edge=False),
               Output("DISSOLVED PO4", "mg/L", style=_PO4, has_edge=False),
               Output("POR NON ASSIM", "mg/L", style=_POR, has_edge=False),
               Output("DISSOLVED NO3", "mg/L", style=_NO3, has_edge=False),
               Output("NOR NON ASSIM", "mg/L", style=_NOR, has_edge=False)),
    _EUTRO: (Output("PHYTO BIOMASS", "ug/L", style=_ALGAE, has_edge=False),
             Output("DISSOLVED PO4", "mg/L", style=_PO4, has_edge=False),
             Output("POR NON ASSIMIL", "mg/L", style=_POR, has_edge=False),
             Output("DISSOLVED NO3", "mg/L", style=_NO3, has_edge=False),
             Output("NOR NON ASSIM", "mg/L", style=_NOR, has_edge=False),
             Output("NH4 LOAD", "mg/L", style=_NH4, has_edge=False),
             Output("ORGANIC LOAD", "mgO2/L", style=_ORGANIC, has_edge=False),
             Output("DISSOLVED O2", "mgO2/L", style=_O2_STYLE, has_edge=False)),
    _MICROPOL: (Output("SUSPENDED LOAD", "kg/m3", style=_SUSPENDED, has_edge=True),
                Output("BED SEDIMENTS", "kg/m2", style=_DEPOSITED, has_edge=True),
                Output("MICRO POLLUTANT", "mg/L", style=_DISSOLVED, has_edge=True),
                Output("ABS. SUSP. LOAD.", "mg/L", style=_ON_SUSPENDED,
                       has_edge=True),
                Output("ABSORB. BED SED.", "g/m2", style=_ON_DEPOSITED,
                       has_edge=True)),
    _THERMAL: (Output("TEMPERATURE", "oC", style=_TEMPERATURE, has_edge=False),),
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

#: The WAQTEL steering file a carrier names. DAMOCLES parses it against WAQTEL's
#: own dictionary, so it is a sheet of its own rather than a block in the
#: carrier's deck.
STEERING_FILENAME = "t2d_river.waqtel"


class _Waqtel(Module("waqtel")):  # type: ignore[misc]
    """The dictionary, plus the coupled bodies the carrier's ``coupling`` expands."""

    @classmethod
    def degradation(cls, *, substance: Any, presets: Any) -> Mapping[str, Any]:
        """First-order tracer DEGRADATION (process 17) over the carrier's tracers,
        from a named substance's preset row.

        A caller with a rate of its own states LAW OF TRACERS DEGRADATION and
        COEFFICIENT 1 FOR LAW OF TRACERS DEGRADATION by name, which is what the
        refusal below names. Given nothing, the body states nothing and the
        carrier couples nothing."""
        return {**_body(_DEGRADATION,
                        degradation={"substance": substance,
                                     "presets": presets}),
                "given": [substance]}

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
        kinetics and the substance's own decay are WAQTEL's own keywords. The
        sorption sink on the dissolved phase is the desorption kinetic times the
        COEFFICIENT OF DISTRIBUTION times the SUSPENDED LOAD, so the sediment a
        deck states is a concentration in kg/m3 - the class that coefficient's
        own m3/kg is defined over - and a deck that states it in mg/L sorbs a
        thousandfold and empties the dissolved phase in minutes."""
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
    """The named substance -> the law and its coefficient, sized to one tracer.

    A word the deck's own preset table does not carry is refused by the keyword
    pair that states a rate directly: this composite invents no die-off."""
    word = str(value.get("substance") or "").strip().lower()
    preset = next((row for key, row in dict(value["presets"]).items()
                   if key in word), None)
    if preset is None:
        raise ValueError(
            f"{value.get('substance')!r} names no degradation preset this deck "
            f"carries ({sorted(value['presets'])}); state the law and its "
            "coefficient by name instead - LAW OF TRACERS DEGRADATION = 2 "
            "(per hour) or 3 (per day) with COEFFICIENT 1 FOR LAW OF TRACERS "
            "DEGRADATION at that law's rate.")
    return ({"LAW_OF_TRACERS_DEGRADATION": [int(preset["law"])],
             "COEFFICIENT_1_FOR_LAW_OF_TRACERS_DEGRADATION":
                 [float(preset["coef"])]}, {})


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
