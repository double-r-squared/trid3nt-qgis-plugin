"""The TOMAWAC wrapper: its module input, the forty variables its own result
carries, and the coupled body a hydrodynamic host names.

TOMAWAC serves BOTH roles off one dictionary. STANDALONE it is a template's own
steering base, solving the wave field on its own mesh and boundary file.
COUPLED it runs under a host through that host's own keywords and hands the
wave forces back in memory - it appends NO tracer to the host's result, so the
host feels the waves only with its own WAVE DRIVEN CURRENTS true, which is why
a coupled body ARMS that switch. What the dictionary already spells - the wind,
the current, the imposed and the boundary spectra - is stated by keyword name,
so the wrapper carries no composite at all."""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Mapping

from .module import Module, Output, Unwritten
from .outputs import PRIMITIVES, read_spectrum

__all__ = ["MODULE_OUTPUT", "RESULT_FILENAME", "STEERING_FILENAME", "WAC"]

#: The TOMAWAC steering file a host names, and the module's own 2D result. A
#: standalone deck names the same result file, so the wave field is read off one
#: name in both roles.
STEERING_FILENAME = "tomawac_waves.cas"
RESULT_FILENAME = "tomawac_waves.slf"

#: The keyword this table is written into, and the one saying how often.
PRINTOUTS = "VARIABLES_FOR_2D_GRAPHIC_PRINTOUTS"
CADENCE = "PERIOD_FOR_GRAPHIC_PRINTOUTS"

#: Deep water: the engine computes no bottom quantity at all under it.
_INFINITE_DEPTH = "INFINITE_DEPTH"

#: A wave height and the variance under it are quantities the sea carries
#: everywhere the water is, floored where there is no wave.
_HEIGHT = {"kind": "mesh", "ramp": "ylgnbu", "units": "m", "floor": 0}
#: A DIRECTION is a compass bearing, and a bearing wraps: its ramp closes on
#: itself so 0 and 360 degrees are one colour.
_DIRECTION = {"kind": "mesh", "ramp": "hsv", "units": "deg"}
#: A signed component reads about zero, so its ramp diverges there and the
#: legend is ranged symmetrically. The current and the wind are both read as
#: metres per second along the mesh's own axes.
_COMPONENT = {"kind": "mesh", "ramp": "rdbu", "units": "m/s", "center": 0.0}
_FORCE = {"kind": "mesh", "ramp": "rdbu", "units": "m/s2", "center": 0.0}
_STRESS = {"kind": "mesh", "ramp": "rdbu", "units": "m3/s2", "center": 0.0}
#: A SPEED rather than a component: it has no sign to diverge about.
_SPEED = {"kind": "mesh", "ramp": "plasma", "units": "m/s", "floor": 0}
_FREQUENCY = {"kind": "mesh", "ramp": "cividis", "units": "Hz", "floor": 0}
_PERIOD = {"kind": "mesh", "ramp": "viridis", "units": "s", "floor": 0}
#: WHAT HAS AN EDGE. The roller the breaking leaves happens in a band the sea
#: makes where the bar or the shore is, so it is read as the shape it has; a
#: height, a period and a direction are everywhere the water is and are drawn
#: whole.
#: WHAT DISSIPATION IS PUBLISHED AS. The engine writes every dissipation row as
#: a NEGATIVE quantity - energy leaving the spectrum - so a surf band runs from
#: the field's minimum up to zero. The ramp is reversed for that, the strongest
#: dissipation taking the deepest colour, and no floor is declared: a bottom
#: pinned at zero would collapse the whole band onto one colour and an edge
#: taken as a fraction of the peak would clip every node of it away.
_BREAKING = {"kind": "mesh", "ramp": "reds_r", "units": "1/s"}

#: What the module WRITES, by the mnemonic VARIABLES FOR 2D GRAPHIC PRINTOUTS
#: spells: the SIXTEEN characters the result record names the row in, the unit
#: it is read in, and how it draws. The engine indexes exactly forty variables
#: and packs each name and unit into one thirty-two character record, so a name
#: longer than sixteen characters spills its remaining letter into the field the
#: unit would have been in - which is what the record then carries, and which is
#: why six period rows are named without their last letter.
#:
#: TOMAWAC never dies on a row it cannot compute: LECDON clears the output flag
#: for every variable this deck's physics does not produce - the wind rows with
#: no wind, the current rows with neither current nor coupling, the bottom and
#: radiation-stress rows over infinite depth, the roller rows without surface
#: rollers, the white-capping rate with that dissipation off - and the result
#: simply does not carry the row. So every row is declared and the ones a deck
#: does not reach are skipped.
MODULE_OUTPUT: Mapping[str, Output] = MappingProxyType({
    "M0": Output("VARIANCE M0", "m2",
                 style={"kind": "mesh", "ramp": "ylgnbu", "units": "m2",
                        "floor": 0}),
    "HM0": Output("WAVE HEIGHT HM0", "m", style=_HEIGHT),
    "DMOY": Output("MEAN DIRECTION", "deg", style=_DIRECTION),
    "SPD": Output("WAVE SPREAD", "deg",
                  style={"kind": "mesh", "ramp": "viridis", "units": "deg",
                         "floor": 0}),
    "ZF": Output("BOTTOM", "m", varies=False,
                 style={"kind": "mesh", "ramp": "terrain", "units": "m"}),
    "WD": Output("WATER DEPTH", "m",
                 style={"kind": "mesh", "ramp": "ylgnbu", "units": "m",
                        "floor": 0}),
    "UX": Output("VELOCITY U", "m/s", style=_COMPONENT),
    "UY": Output("VELOCITY V", "m/s", style=_COMPONENT),
    "VX": Output("WIND ALONG X", "m/s", style=_COMPONENT),
    "VY": Output("WIND ALONG Y", "m/s", style=_COMPONENT),
    "FX": Output("FORCE FX", "m/s2", style=_FORCE),
    "FY": Output("FORCE FY", "m/s2", style=_FORCE),
    "SXX": Output("STRESS SXX", "m3/s2", style=_STRESS),
    "SXY": Output("STRESS SXY", "m3/s2", style=_STRESS),
    "SYY": Output("STRESS SYY", "m3/s2", style=_STRESS),
    "UWB": Output("BOTTOM VELOCITY", "m/s", style=_SPEED),
    # PRIVATE 1 is the user-Fortran table. Nothing in the distributed code
    # writes it, so it is a slot the wildcard may reach and never a row.
    "FMOY": Output("MEAN FREQ FMOY", "Hz", style=_FREQUENCY),
    "FM01": Output("MEAN FREQ FM01", "Hz", style=_FREQUENCY),
    "FM02": Output("MEAN FREQ FM02", "Hz", style=_FREQUENCY),
    "FPD": Output("PEAK FREQ FPD", "Hz", style=_FREQUENCY),
    "FPR5": Output("PEAK FREQ FPR5", "Hz", style=_FREQUENCY),
    "FPR8": Output("PEAK FREQ FPR8", "Hz", style=_FREQUENCY),
    "US": Output("USTAR", "m/s", style=_SPEED),
    "CD": Output("CD", "",
                 style={"kind": "mesh", "ramp": "viridis", "floor": 0}),
    "Z0": Output("Z0", "m",
                 style={"kind": "mesh", "ramp": "viridis", "units": "m",
                        "floor": 0}),
    "WS": Output("WAVE STRESS", "kg/(m.s2)",
                 style={"kind": "mesh", "ramp": "plasma",
                        "units": "kg/(m.s2)", "floor": 0}),
    "TMOY": Output("MEAN PERIOD TMOY", "s", style=_PERIOD),
    "TM01": Output("MEAN PERIOD TM01", "s", style=_PERIOD),
    "TM02": Output("MEAN PERIOD TM02", "s", style=_PERIOD),
    "TPD": Output("PEAK PERIOD TPD", "s", style=_PERIOD),
    "TPR5": Output("PEAK PERIOD TPR5", "s", style=_PERIOD),
    "TPR8": Output("PEAK PERIOD TPR8", "s", style=_PERIOD),
    "POW": Output("WAVE POWER", "kW/m",
                  style={"kind": "mesh", "ramp": "inferno", "units": "kW/m",
                         "floor": 0}),
    "BETA": Output("BREAKING RAT", "1/s", style=_BREAKING),
    "BETAWC": Output("WHITE CAPING", "1/s", style=_BREAKING),
    "SRE": Output("SURFACE ROLLER E", "m3/s2", has_edge=True,
                  style={"kind": "mesh", "ramp": "oranges", "units": "m3/s2",
                         "floor": 0}),
    "DBR": Output("BREAKER DISSIP", "m2/s",
                  style={"kind": "mesh", "ramp": "reds_r", "units": "m2/s"}),
    "DSR": Output("ROLLER DISSIP", "m3/s3",
                  style={"kind": "mesh", "ramp": "oranges_r",
                         "units": "m3/s3"}),
    "DPIC": Output("PEAK DIRECTION", "deg", style=_DIRECTION),
})


class _Tomawac(Module("tomawac")):  # type: ignore[misc]
    """The module input, and the coupled body a host's ``coupling`` expands."""

    @classmethod
    def table(cls, stated: Mapping[str, Any] = MappingProxyType({}),
              ) -> Mapping[str, Output]:
        """The rows above, less the one the engine leaves to whatever was there.

        Over infinite depth DUMP2D computes no bottom velocity, and LECDON does
        NOT clear that row the way it clears the other bottom rows, so a deep
        deck asking for it writes an untouched work array. It is the one row a
        deck can take off this table."""
        rows = dict(super().table(stated))
        if cls.switched(_INFINITE_DEPTH, stated):
            rows.pop("UWB", None)
        return MappingProxyType(rows)

    @classmethod
    def wave(cls, *, geometry: Any, boundary: Any,
             **keywords: Any) -> Mapping[str, Any]:
        """The wave field a host solves its currents under.

        The mesh is the host's own where the coupling is same-mesh, the boundary
        file is TOMAWAC's own walk of it, and the result is the module's file
        beside the host's. Everything else is a keyword the dictionary carries,
        stated by its own name on the body: the frequency and direction
        discretisation, the boundary spectrum, the wind, the source terms."""
        for name in keywords:
            cls.slot(name)
        return {"module": "tomawac", "steering": STEERING_FILENAME,
                "slots": {"GEOMETRY_FILE": geometry,
                          "BOUNDARY_CONDITIONS_FILE": boundary,
                          "ED_RESULTS_FILE": RESULT_FILENAME, **keywords}}


WAC = _Tomawac
WAC.MODULE_OUTPUT = MODULE_OUTPUT
WAC.PRINTOUTS = PRINTOUTS
WAC.CADENCE = CADENCE
#: The result the primitives read: TOMAWAC writes its own 2D field, standalone
#: and beside a host's alike.
WAC.RESULT_FILE = RESULT_FILENAME
#: TOMAWAC spells that file 2D RESULTS FILE, not RESULTS FILE.
WAC.RESULT_KEYWORD = "ED_RESULTS_FILE"
#: THE CLOCK: TOMAWAC has no DURATION keyword at all - it names the step and how
#: many of them, and their product is the seconds the wave field is marched over.
WAC.CLOCK = ("TIME_STEP", "NUMBER_OF_TIME_STEP")
#: The SPECTRA, which are results over the polar frequency-direction grid rather
#: than over the domain: the run keeps and publishes each file a deck names, and
#: no primitive of the geographic mesh reads either.
WAC.RESULT_FILES = ("PUNCTUAL_RESULTS_FILE", "ZD_SPECTRA_RESULTS_FILE")
#: The source carries no DEPRECATED mark for PRI; its mark is that the array is
#: the user's own, which the engine computes nothing into.
WAC.UNWRITTEN = MappingProxyType({
    "PRI": Unwritten("PRIVATE 1", (
        "point_tomawac.f: 'USER DEDICATED ARRAY (2-DIMENSIONAL * NPRIV)', the "
        "SPRIVE block output 17 is bound to"), french="PRIVE 1"),
})
#: The wave forces reach a host as a MOMENTUM SOURCE in memory - PROSOU adds
#: FXWAVE and FYWAVE into FU and FV - and the host records no wave row, so this
#: module appends nothing to its host's tracers and arms the switch that
#: addition happens under instead.
WAC.ARMS_ON_HOST = ("WAVE_DRIVEN_CURRENTS",)
#: The spectra are read by the one primitive that reads a polar grid, and by no
#: module whose results are written over a geographic mesh.
WAC.reads(**PRIMITIVES, spectrum=read_spectrum)
