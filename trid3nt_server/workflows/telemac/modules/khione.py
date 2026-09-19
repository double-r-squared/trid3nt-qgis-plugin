"""The KHIONE wrapper: its module input, the coupled body a host names, the
variables its own result file carries, and the tracers it puts on its host's.

KHIONE runs UNDER a hydrodynamic module. It writes its OWN result beside the
host's AND appends tracers to the host's - the water temperature in every
coupled run, and the salinity, the frazil and the dynamic cover under the
switches the engine adds each under. It has no forcing of its own: the air
temperature, the dew point, the cloud, the wind, the rain and the snow are read
out of the atmospheric data file the host names. What the dictionary lacks is
the three files a coupled deck carries, which is the whole of what the body
states past the keywords a deck states by name."""

from __future__ import annotations

from dataclasses import replace
from types import MappingProxyType
from typing import Any, Mapping

from .module import Module, Output
from .outputs import PRIMITIVES

__all__ = ["KHIONE", "MODULE_OUTPUT", "RESULT_FILENAME", "STEERING_FILENAME"]

#: The KHIONE steering file a host names. DAMOCLES parses it against KHIONE's
#: own dictionary, so it is a sheet of its own rather than a block in the host's.
STEERING_FILENAME = "khione_domain.cas"
#: KHIONE's own result SELAFIN, written beside the host's.
RESULT_FILENAME = "khione_domain.slf"

#: The keyword whose value this table is written into.
PRINTOUTS = "VARIABLES_FOR_GRAPHIC_PRINTOUTS"
#: The switches the engine reads its own branches off. The thermal budget
#: allocates the frazil, temperature and cover arrays, and asking for one it did
#: not allocate stops the solve at iteration 0 rather than dropping the row;
#: clogging runs the same source terms, so the tracer branch takes either.
_HEAT_BUDGET = "HEAT_BUDGET"
_CLOGGING = "CLOGGING_ON_BARS"
_SALINITY = "SALINITY"
_DYNAMIC_COVER = "DYNAMIC_ICE_COVER"
_FRAZIL_CLASSES = "NUMBER_OF_CLASSES_FOR_SUSPENDED_FRAZIL_ICE"

#: The INCOMING shortwave, which is a quantity with a floor at darkness.
_SOLAR = {"kind": "mesh", "ramp": "inferno", "units": "W/m2", "floor": 0}
#: A net or turbulent flux, which the water GAINS or LOSES, so it diverges about
#: the zero that is the answer's own hinge - a reach that is neither warming nor
#: cooling is the line ice forms on the far side of.
_FLUX = {"kind": "mesh", "ramp": "rdbu_r", "units": "W/m2", "center": 0.0}
#: WHAT HAS AN EDGE. Ice is PUT on the water where the water made it, so a cover
#: and its thickness are masked below a fraction of their own peak and read as
#: the shape they have; a heat flux is over the whole domain and is drawn whole.
_THICKNESS = {"kind": "mesh", "ramp": "blues", "units": "m", "floor": 0}
_COVER = {"kind": "mesh", "ramp": "blues", "units": "", "floor": 0}
#: An elevation the module computes over the cover, which is a surface rather
#: than a quantity injected into the domain.
_ELEVATION = {"kind": "mesh", "ramp": "viridis", "units": "m"}
_PROBABILITY = {"kind": "mesh", "ramp": "purples", "units": ""}
_VELOCITY = {"kind": "mesh", "ramp": "plasma", "units": "m/s", "floor": 0}
#: A CLASS rather than a magnitude: open water, static border ice, thickened
#: border ice, and the products of those primes for a node that is several.
_CLASS = {"kind": "mesh", "ramp": "set1", "units": ""}
_FRAZIL = {"kind": "mesh", "ramp": "magma", "units": "", "floor": 0}
_PARTICLES = {"kind": "mesh", "ramp": "magma", "units": "1/m3", "floor": 0}
#: A BACKGROUND quantity - one the water already carries everywhere - is ranged
#: over what was measured rather than pinned to a floor it never reaches.
_TEMPERATURE = {"kind": "mesh", "ramp": "rdylbu_r", "units": "C"}
_SALT = {"kind": "mesh", "ramp": "ylgnbu", "units": "ppt"}

#: What the module WRITES, by the mnemonic VARIABLES FOR GRAPHIC PRINTOUTS
#: spells: the SIXTEEN characters the result record names the row in, the unit
#: it is read in, and how it draws. The engine indexes exactly twenty-four
#: variables and packs each name and unit into one thirty-two character record,
#: so a name longer than sixteen characters spills its remaining words into the
#: field the unit would have been in - which is what the record then carries.
#: The first fifteen rows are written in SI and the record says so literally.
#: The last four are the thermal budget's; under a two-dimensional host the
#: engine ADDs its NTOT and CTOT arrays a second time to reach its own fixed
#: count and assigns those two positions no name of their own, so what a result
#: calls them is the name of the two rows above.
MODULE_OUTPUT: Mapping[str, Output] = MappingProxyType({
    "PHCL": Output("SOLRAD CLEAR SKY", "W/m2", style=_SOLAR, has_edge=False),
    "PHRI": Output("SOLRAD CLOUDY", "W/m2", style=_SOLAR, has_edge=False),
    "PHPS": Output("NET SOLRAD", "W/m2", style=_FLUX, has_edge=False),
    "PHIB": Output("EFFECTIVE SOLRAD", "W/m2", style=_FLUX, has_edge=False),
    "PHIE": Output("EVAPO HEAT FLUX", "W/m2", style=_FLUX, has_edge=False),
    "PHIH": Output("CONDUC HEAT FLUX", "W/m2", style=_FLUX, has_edge=False),
    "PHIP": Output("PRECIP HEAT FLUX", "W/m2", style=_FLUX, has_edge=False),
    "COV_TH0": Output("FRAZIL THETA0", "", style=_PROBABILITY, has_edge=False),
    "COV_TH1": Output("FRAZIL THETA1", "", style=_PROBABILITY, has_edge=False),
    "COV_BT1": Output("REENTRAINMENT", "", style=_PROBABILITY, has_edge=False),
    "COV_VBB": Output("SETTLING VEL.", "m/s", style=_VELOCITY, has_edge=False),
    "COV_FC": Output("SOLID ICE CONC.", "", style=_COVER, has_edge=True),
    "COV_THS": Output("SOLID ICE THICK.", "m", style=_THICKNESS, has_edge=True),
    "COV_THF": Output("FRAZIL THICKNESS", "m", style=_THICKNESS, has_edge=True),
    "COV_THUN": Output("UNDER ICE THICK.", "m", style=_THICKNESS, has_edge=True),
    "COV_EQ": Output("EQUIV. SURFACE", "m", style=_ELEVATION, has_edge=False),
    "COV_ET": Output("TOP ICE COVER", "m", style=_ELEVATION, has_edge=False),
    "COV_EB": Output("BOTTOM ICE COVER", "m", style=_ELEVATION, has_edge=False),
    "COV_THT": Output("TOTAL ICE THICK.", "m", style=_THICKNESS, has_edge=True),
    "ICETYPE": Output("CHARACTERISTICS", "", style=_CLASS, has_edge=True),
    "NTOT": Output("PARTICLES NUMBER", "1/m3", style=_PARTICLES, has_edge=True,
                   under=_HEAT_BUDGET),
    "CTOT": Output("TOTAL CONCENTRAT", "", style=_FRAZIL, has_edge=True,
                   under=_HEAT_BUDGET),
    "NTOTS": Output("PARTICLES NUMBER", "1/m3", style=_PARTICLES, has_edge=True,
                    under=_HEAT_BUDGET),
    "CTOTS": Output("TOTAL CONCENTRAT", "", style=_FRAZIL, has_edge=True,
                    under=_HEAT_BUDGET),
})

#: What the module ADDTRACERs to its HOST's tracers, in the order it adds them,
#: under the engine's own 16-character names and units. The temperature is added
#: in every coupled run; the rest are added under the branch each is written in.
#: A frazil class is PUT into the water and an ice cover is PUT on it, so each
#: has a visible edge; a temperature and a salinity are everywhere the water is.
_TEMPERATURE_ROW = Output("TEMPERATURE", "oC", style=_TEMPERATURE, has_edge=False)
_SALINITY_ROW = Output("SALINITY", "ppt", style=_SALT, has_edge=False)
#: The unit field the engine writes beside a frazil tracer, which is what the
#: result record carries rather than a dimension.
_VOLUME_FRACTION = "VOLUME FRACTION"
_FRAZIL_ROW = Output("FRAZIL", _VOLUME_FRACTION, style=_FRAZIL, has_edge=True)
_COVER_FRACTION_ROW = Output("ICE COVER FRAC.", "SURFAC FRACTION", style=_COVER,
                             has_edge=True)
_COVER_THICKNESS_ROW = Output("ICE COVER THICK.", "M", style=_THICKNESS,
                              has_edge=True)


def _appended(body: Mapping[str, Any]) -> tuple[Output, ...]:
    """The tracers this coupled body puts on its host's own.

    The engine refuses to run a coupled KHIONE with no active process at all, so
    the temperature is on every host. The frazil and the cover ride the branch
    the thermal budget or the clogging opens; a cover-impact or border-ice run
    takes the other branch, which zeroes the class count and adds neither."""
    stated = dict(body.get("slots") or {})
    rows = [_TEMPERATURE_ROW]
    if _Khione.switched(_SALINITY, stated):
        rows.append(_SALINITY_ROW)
    if _suspends_frazil(stated):
        rows += _frazil_rows(_classes(stated))
        if _Khione.switched(_DYNAMIC_COVER, stated):
            rows += [_COVER_FRACTION_ROW, _COVER_THICKNESS_ROW]
    return tuple(rows)


def _suspends_frazil(stated: Mapping[str, Any]) -> bool:
    """Does this deck open the branch that suspends frazil in the water column?"""
    return (_Khione.switched(_HEAT_BUDGET, stated)
            or _Khione.switched(_CLOGGING, stated))


def _classes(stated: Mapping[str, Any]) -> int:
    """How many frazil classes this deck counts, its own way or the engine's."""
    counted = stated.get(_FRAZIL_CLASSES,
                         _Khione.slot(_FRAZIL_CLASSES).engine_default)
    return max(int(counted), 1)


def _frazil_rows(classes: int) -> list[Output]:
    """One host tracer per frazil class, numbered from one.

    A single class is added under the bare name; several are numbered, which is
    the engine's own spelling and not a suffix this wrapper invents."""
    if classes == 1:
        return [_FRAZIL_ROW]
    return [replace(_FRAZIL_ROW, name=f"FRAZIL {n}") for n in range(1, classes + 1)]


#: The keywords a coupled KHIONE deck has only under a THREE-dimensional host:
#: the second result file and its format, the table written into it, and the
#: three-dimensional frazil diffusion the water column is the whole reason for.
#: A two-dimensional host builds none of those fields.
_ONLY_3D = frozenset((
    "RD_RESULTS_FILE",
    "RD_RESULTS_FILE_FORMAT",
    "VARIABLES_FOR_3D_GRAPHIC_PRINTOUTS",
    "SCHEME_FOR_DIFFUSION_OF_FRAZIL_IN_3D",
    "COEFFICIENT_FOR_HORIZONTAL_DIFFUSION_OF_FRAZIL",
    "COEFFICIENT_FOR_VERTICAL_DIFFUSION_OF_FRAZIL",
))


#: What the engine writes into its OWN result past the twenty-four it indexes,
#: all of it allocated inside the thermal budget. The frazil concentration and
#: the particle number are written once PER CLASS, at the surface as well as in
#: the column, and the engine numbers those mnemonics from one - which is what
#: the dictionary spells with an ``i``. A single class is named without a number
#: and several are named by it; the surface particle number is given the same
#: result-file name as the column one, so a multi-class result carries that name
#: twice.
_PER_CLASS: Mapping[str, tuple[str, str, Mapping[str, Any], bool]] = MappingProxyType({
    "F": ("FRAZIL", "FRAZIL CLASS {n}", _FRAZIL, True),
    "N": ("NB PARTICLE", "NB PARTICLE {n}", _PARTICLES, True),
    "SF": ("FRAZIL S", "FRAZIL CLASS {n}S", _FRAZIL, True),
    "SN": ("NB PARTICLE S", "NB PARTICLE {n}", _PARTICLES, True),
})

#: The rows the thermal budget adds beside those, each under the switch that
#: allocates it. The engine writes no unit field for any of them.
_BUDGET_ROWS: Mapping[str, Output] = MappingProxyType({
    "TEMP": Output("TEMPERATURE", "", style=_TEMPERATURE, has_edge=False,
                   under=_HEAT_BUDGET),
    "TEMPS": Output("TEMPERATURE S", "", style=_TEMPERATURE, has_edge=False,
                    under=_HEAT_BUDGET),
})
_SALINITY_ROWS: Mapping[str, Output] = MappingProxyType({
    "SAL": Output("SALINITY", "", style=_SALT, has_edge=False, under=_SALINITY),
    "SALS": Output("SALINITY S", "", style=_SALT, has_edge=False, under=_SALINITY),
})
_DYNAMIC_COVER_ROWS: Mapping[str, Output] = MappingProxyType({
    "DYNCOVC": Output("ICE COVER FRAC.", "", style=_COVER, has_edge=True,
                      under=_DYNAMIC_COVER),
    "DYNCOVT": Output("ICE COVER THICK.", "", style=_THICKNESS, has_edge=True,
                      under=_DYNAMIC_COVER),
})


class _Khione(Module("khione")):  # type: ignore[misc]
    """The module input, and the coupled body a host's ``coupling`` expands."""

    @classmethod
    def table(cls, stated: Mapping[str, Any] = MappingProxyType({}),
              ) -> Mapping[str, Output]:
        """The twenty-four indexed rows, then everything the thermal budget adds.

        The class count is the deck's own, so a deck that suspends frazil in
        several classes rows every one of them and leaves no written variable
        unnamed."""
        rows = dict(super().table(stated))
        if not cls.switched(_HEAT_BUDGET, stated):
            return MappingProxyType(rows)
        classes = _classes(stated)
        for mnemonic, (mono, numbered, style, edge) in _PER_CLASS.items():
            for n in range(1, classes + 1):
                rows[f"{mnemonic}{n}"] = Output(
                    mono if classes == 1 else numbered.format(n=n), "",
                    style=style, has_edge=edge, under=_HEAT_BUDGET)
        rows.update(_BUDGET_ROWS)
        if cls.switched(_SALINITY, stated):
            rows.update(_SALINITY_ROWS)
        if cls.switched(_DYNAMIC_COVER, stated):
            rows.update(_DYNAMIC_COVER_ROWS)
        return MappingProxyType(rows)

    @classmethod
    def ice(cls, *, geometry: Any, boundary: Any,
            **keywords: Any) -> Mapping[str, Any]:
        """The ice a reach makes under the weather the host is already reading.

        The mesh and the boundary file are the HOST's own - KHIONE solves on the
        same nodes - and the result is the module's own file beside the host's.
        Everything else is a keyword the dictionary carries, stated by its own
        name on the body: the thermal budget and its constants, the ice cover's
        friction and its impact on the hydrodynamics, the frazil classes, the
        restart the initial cover is read from."""
        for name in keywords:
            cls.slot(name)
        return _body({"GEOMETRY_FILE": geometry,
                      "BOUNDARY_CONDITIONS_FILE": boundary,
                      "RESULTS_FILE": RESULT_FILENAME, **keywords})


def _body(slots: Mapping[str, Any]) -> Mapping[str, Any]:
    """One coupled body, as the host's ``coupling`` composite reads it."""
    return {"module": "khione", "steering": STEERING_FILENAME,
            "slots": dict(slots)}


KHIONE = _Khione
KHIONE.MODULE_OUTPUT = MODULE_OUTPUT
KHIONE.PRINTOUTS = PRINTOUTS
#: The result the primitives read: KHIONE writes its own file beside the host's,
#: which is where everything its own table rows lands. What reaches the HOST's
#: file is the tracer set above, counted among the host's own tracers.
KHIONE.RESULT_FILE = RESULT_FILENAME
KHIONE.ONLY_3D = _ONLY_3D
KHIONE.APPENDABLE = (
    ("every coupled run", (_TEMPERATURE_ROW,)),
    (_SALINITY, (_SALINITY_ROW,)),
    (f"{_HEAT_BUDGET} or {_CLOGGING}, one per frazil class", (_FRAZIL_ROW,)),
    (_DYNAMIC_COVER, (_COVER_FRACTION_ROW, _COVER_THICKNESS_ROW)),
)
KHIONE.appends(_appended)
KHIONE.reads(**PRIMITIVES)
