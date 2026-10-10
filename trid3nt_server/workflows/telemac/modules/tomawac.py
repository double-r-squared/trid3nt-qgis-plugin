"""The TOMAWAC wrapper: its module input, the forty variables its result carries, and the coupled body a host names.

Standalone it is a template's steering base; coupled it hands wave forces back in memory and
appends no tracer, so the host needs its WAVE DRIVEN CURRENTS true, which a coupled body arms.
Dictionary keywords are stated by name, so there is no composite.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Mapping

from .module import Module, Output, Unwritten
from .outputs import PRIMITIVES, read_spectrum

__all__ = ["MODULE_OUTPUT", "RESULT_FILENAME", "STEERING_FILENAME", "WAC"]

# The TOMAWAC steering file a host names and the module's 2D result; a standalone deck names the same result file.
STEERING_FILENAME = "tomawac_waves.cas"
RESULT_FILENAME = "tomawac_waves.slf"

# The keyword this table is written into, and the one saying how often.
PRINTOUTS = "VARIABLES_FOR_2D_GRAPHIC_PRINTOUTS"
CADENCE = "PERIOD_FOR_GRAPHIC_PRINTOUTS"

# Deep water: the engine computes no bottom quantity under it.
_INFINITE_DEPTH = "INFINITE_DEPTH"

# Wave height and its variance exist wherever the water is, floored where there is no wave.
_HEIGHT = {"kind": "mesh", "ramp": "ylgnbu", "units": "m", "floor": 0}
# A direction is a compass bearing: the ramp closes on itself so 0 and 360 are one colour.
_DIRECTION = {"kind": "mesh", "ramp": "hsv", "units": "deg"}
# A signed component reads about zero, so its ramp diverges and the legend is symmetric; current and wind are m/s along the mesh axes.
_COMPONENT = {"kind": "mesh", "ramp": "rdbu", "units": "m/s", "center": 0.0}
_FORCE = {"kind": "mesh", "ramp": "rdbu", "units": "m/s2", "center": 0.0}
_STRESS = {"kind": "mesh", "ramp": "rdbu", "units": "m3/s2", "center": 0.0}
# A speed has no sign to diverge about.
_SPEED = {"kind": "mesh", "ramp": "plasma", "units": "m/s", "floor": 0}
_FREQUENCY = {"kind": "mesh", "ramp": "cividis", "units": "Hz", "floor": 0}
_PERIOD = {"kind": "mesh", "ramp": "viridis", "units": "s", "floor": 0}
# The roller lies in a band, so it is read as the shape it has; height, period and direction are drawn whole.
# The engine writes dissipation as a negative quantity (energy leaving), so its ramp is reversed with no
# floor: a bottom pinned at zero would collapse the band to one colour and a peak-fraction edge would clip it.
_BREAKING = {"kind": "mesh", "ramp": "reds_r", "units": "1/s"}

# What the module writes, by VARIABLES FOR 2D GRAPHIC PRINTOUTS mnemonic: the sixteen characters the
# result names the row in, its unit, how it draws. The engine indexes exactly forty variables and packs
# name and unit into one 32-character record, so a name past sixteen spills into the unit field (why
# six period rows lack their last letter). LECDON clears the output flag for any variable this deck's
# physics does not produce (wind, current, bottom and radiation stress over infinite depth, rollers,
# white-capping), so every row is declared and absent ones are skipped.
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
    # PRIVATE 1 is the user-Fortran table; nothing writes it, so it is a slot the wildcard may reach, never a row.
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

        Over infinite depth DUMP2D computes no bottom velocity and LECDON does not clear that row, so
        a deep deck asking for it writes an untouched work array.
        """
        rows = dict(super().table(stated))
        if cls.switched(_INFINITE_DEPTH, stated):
            rows.pop("UWB", None)
        return MappingProxyType(rows)

    @classmethod
    def wave(cls, *, geometry: Any, boundary: Any,
             **keywords: Any) -> Mapping[str, Any]:
        """The wave field a host solves its currents under.

        The mesh is the host's where coupling is same-mesh, the boundary file is TOMAWAC's walk of it;
        everything else is a dictionary keyword stated by name on the body.
        """
        for name in keywords:
            cls.slot(name)
        return {"module": "tomawac", "steering": STEERING_FILENAME,
                "filled_by": ("wave",),
                "slots": {"GEOMETRY_FILE": geometry,
                          "BOUNDARY_CONDITIONS_FILE": boundary,
                          "ED_RESULTS_FILE": RESULT_FILENAME, **keywords}}


WAC = _Tomawac
WAC.MODULE_OUTPUT = MODULE_OUTPUT
WAC.PRINTOUTS = PRINTOUTS
WAC.CADENCE = CADENCE
# The result the primitives read: TOMAWAC writes its own 2D field, standalone and beside a host.
WAC.RESULT_FILE = RESULT_FILENAME
# TOMAWAC spells that file 2D RESULTS FILE, not RESULTS FILE.
WAC.RESULT_KEYWORD = "ED_RESULTS_FILE"
# TOMAWAC has no DURATION keyword: step times count is the seconds marched over.
WAC.CLOCK = ("TIME_STEP", "NUMBER_OF_TIME_STEP")
# The spectra are results over the polar frequency-direction grid, not the domain: published per file a deck names; no geographic-mesh primitive reads them.
WAC.RESULT_FILES = ("PUNCTUAL_RESULTS_FILE", "ZD_SPECTRA_RESULTS_FILE")
# PRI carries no DEPRECATED mark; its mark is that the array is the user's own and the engine computes nothing into it.
WAC.UNWRITTEN = MappingProxyType({
    "PRI": Unwritten("PRIVATE 1", (
        "point_tomawac.f: 'USER DEDICATED ARRAY (2-DIMENSIONAL * NPRIV)', the "
        "SPRIVE block output 17 is bound to"), french="PRIVE 1"),
})
# Wave forces reach a host as a momentum source in memory (PROSOU adds FXWAVE, FYWAVE into FU, FV) and the host records no wave row, so nothing is appended to its tracers and the switch is armed instead.
WAC.ARMS_ON_HOST = ("WAVE_DRIVEN_CURRENTS",)
# The spectra are read by the one primitive that reads a polar grid.
WAC.reads(**PRIMITIVES, spectrum=read_spectrum)
# The sea state at the open edge fills the JONSWAP numbers; the gauged level is the still water depths are read under; a settled station is where the spectrum is printed. A coupled body takes only the sea state.
WAC.FILLED_BY = MappingProxyType({
    "wave": {"BOUNDARY_SIGNIFICANT_WAVE_HEIGHT": lambda wave: wave.height_m,
             "BOUNDARY_PEAK_FREQUENCY": lambda wave: wave.peak_frequency_hz,
             "BOUNDARY_MAIN_DIRECTION_1": lambda wave: wave.direction_deg},
    "level": {"INITIAL_STILL_WATER_LEVEL": lambda level: level.value},
    "station": {
        "ABSCISSAE_OF_SPECTRUM_PRINTOUT_POINTS": lambda placed: [placed["at"][0]],
        "ORDINATES_OF_SPECTRUM_PRINTOUT_POINTS": lambda placed: [placed["at"][1]]}})
