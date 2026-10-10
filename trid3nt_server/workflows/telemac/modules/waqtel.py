"""The WAQTEL wrapper: its dictionary, and the coupled bodies a carrier names.

WAQTEL runs under a hydrodynamic module, writes no result file and rows no output: its
processes produce tracers appended to the carrier's own.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Mapping

from .module import Module, Output, SlotRefused

__all__ = ["WAQTEL", "STEERING_FILENAME"]

# The engine dispatches with ``IF( p*INT(WAQPROCESS/p).EQ.WAQPROCESS )``, so every process whose prime divides the keyword runs; 1 is the default and no process.
_O2 = 2
_BIOMASS = 3
_EUTRO = 5
_MICROPOL = 7
_THERMAL = 11
_DEGRADATION = 17

# One style per appended variable. A background variable (oxygen at 8.0-8.7 mg/L on a ramp pinned
# to zero would spend half a percent of its colours) is ranged over what was measured on wet nodes.
_TEMPERATURE = {"kind": "mesh", "ramp": "rdylbu_r", "units": "C"}
_O2_STYLE = {"kind": "mesh", "ramp": "rdylbu", "units": "mg/L"}
_ORGANIC = {"kind": "mesh", "ramp": "oranges", "units": "mg/L"}
_NH4 = {"kind": "mesh", "ramp": "magma", "units": "mg/L"}
_ALGAE = {"kind": "mesh", "ramp": "greens", "units": "ug/L"}
_PO4 = {"kind": "mesh", "ramp": "ylorrd", "units": "mg/L"}
_POR = {"kind": "mesh", "ramp": "plasma", "units": "mg/L"}
_NO3 = {"kind": "mesh", "ramp": "gnbu", "units": "mg/L"}
_NOR = {"kind": "mesh", "ramp": "cividis", "units": "mg/L"}
# MICROPOL's five, in its source terms' units: suspended sediment is kg/m3 (the distribution coefficient is m3/kg); bed sediment and pollutant are per square metre settled.
_SUSPENDED = {"kind": "mesh", "ramp": "oranges", "units": "kg/m3", "floor": 0}
_DEPOSITED = {"kind": "mesh", "ramp": "ylorrd", "units": "kg/m2", "floor": 0}
_DISSOLVED = {"kind": "mesh", "ramp": "reds", "units": "mg/L", "floor": 0}
_ON_SUSPENDED = {"kind": "mesh", "ramp": "magma", "units": "mg/L", "floor": 0}
_ON_DEPOSITED = {"kind": "mesh", "ramp": "plasma", "units": "g/m2", "floor": 0}

# What each process appends to the carrier's result, in engine order behind the carrier's tracers,
# under 16-character names. A carrier declaring one of these names keeps its row. Degradation (17)
# acts on an existing tracer and appends none. The third algal tracer has two spellings (EUTRO
# ``POR NON ASSIMIL``, BIOMASS ``POR NON ASSIM``), so the row sets cannot share it. Water-column
# variables are everywhere, so no peak-fraction mask; a micropollutant is put into the water and has an edge.
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

# The eleven keywords EUTRO reads and BIOMASS does not, all terms in the oxygen, organic-load and ammonium balance; stating one under BIOMASS is a number nobody reads.
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

# The second sorption site's model number appends two tracers, shifting the MICROPOL rows and every tracer-count array, so the one-site model is exposed.
_TWO_SITE = frozenset(("KINETIC_EXCHANGE_MODEL", "COEFFICIENT_OF_DISTRIBUTION_2",
                       "CONSTANT_OF_DESORPTION_KINETIC_2"))

# The WAQTEL steering file a carrier names; DAMOCLES parses it against WAQTEL's dictionary, so it is a sheet of its own.
STEERING_FILENAME = "t2d_river.waqtel"


class _Waqtel(Module("waqtel")):  # type: ignore[misc]
    """The dictionary, plus the coupled bodies the carrier's ``coupling`` expands."""

    @classmethod
    def degradation(cls, *, substance: Any, presets: Any) -> Mapping[str, Any]:
        """First-order tracer degradation (process 17) over the carrier's tracers, from a named substance's preset row.

        A caller with its own rate states LAW OF TRACERS DEGRADATION and COEFFICIENT 1 by name. Given nothing, it states nothing.
        """
        return {**_body(_DEGRADATION,
                        degradation={"substance": substance,
                                     "presets": presets}),
                "given": [substance]}

    @classmethod
    def thermal(cls, **keywords: Any) -> Mapping[str, Any]:
        """The heat budget between water and air (process 11), appending a TEMPERATURE tracer.

        Forcing is the host's atmospheric data file. ATMOSPHERE-WATER EXCHANGE MODEL is the 3D
        surface-exchange switch; its engine default is the only value a 2D run takes.
        """
        return cls._process(_THERMAL, keywords)

    @classmethod
    def o2(cls, **keywords: Any) -> Mapping[str, Any]:
        """The dissolved-oxygen balance (process 2), appending oxygen, organic load and ammonium."""
        return cls._process(_O2, keywords)

    @classmethod
    def micropollutant(cls, **keywords: Any) -> Mapping[str, Any]:
        """A sorbing substance and the sediment it rides (process 7), appending suspended and bed sediment and the dissolved, suspended-sorbed and bed-sorbed substance.

        The sorption sink is the desorption kinetic times the distribution coefficient (m3/kg) times the
        suspended load, so sediment is kg/m3; a deck stating mg/L sorbs a thousandfold too much.
        """
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
        """Nutrient-limited algal growth, and the oxygen balance it drives where asked.

        ``oxygen`` chooses the engine's algal source term: EUTRO (five tracers plus the three oxygen ones) or BIOMASS (five).
        """
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
        """One coupled body: the process, and keywords stated by name; an unknown keyword refuses at import."""
        for name in keywords:
            cls.slot(name)
        return _body(number, **keywords)


def _degradation(value: Mapping[str, Any]) -> tuple[Mapping[str, Any],
                                                    Mapping[str, Any]]:
    """The named substance -> the law and its coefficient, sized to one tracer; a word absent from the preset table refuses (no invented die-off)."""
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
    return {"module": "waqtel", "steering": STEERING_FILENAME,
            "process": process, "slots": dict(slots)}


def _appended(body: Mapping[str, Any]) -> tuple[Output, ...]:
    return _APPENDED.get(int(body.get("process") or 0), ())


WAQTEL = _Waqtel
WAQTEL.APPENDABLE = tuple((f"process {process}", rows)
                          for process, rows in sorted(_APPENDED.items()))
WAQTEL.composites(reads={"degradation": ("substance",)},
                  degradation=_degradation)
WAQTEL.appends(_appended)
