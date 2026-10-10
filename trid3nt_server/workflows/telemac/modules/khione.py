"""The KHIONE wrapper: its module input, the coupled body a host names, its result variables, and the tracers it puts on its host's.

KHIONE runs under a hydrodynamic module, writes its own result beside the host's and appends
tracers to the host's (temperature always; salinity, frazil, cover under engine switches). Its
forcing is read from the atmospheric data file the host names.
"""

from __future__ import annotations

from dataclasses import replace
from types import MappingProxyType
from typing import Any, Mapping

from .module import Module, Output, Unwritten
from .outputs import PRIMITIVES

__all__ = ["KHIONE", "MODULE_OUTPUT", "RESULT_FILENAME", "STEERING_FILENAME"]

# The KHIONE steering file a host names; DAMOCLES parses it against KHIONE's dictionary, so it is a sheet of its own.
STEERING_FILENAME = "khione_domain.cas"
# KHIONE's own result SELAFIN, beside the host's.
RESULT_FILENAME = "khione_domain.slf"

# The keyword this table is written into.
PRINTOUTS = "VARIABLES_FOR_GRAPHIC_PRINTOUTS"
# The switches the engine reads its branches off. The thermal budget allocates the frazil, temperature and cover arrays and asking for an unallocated one stops the solve at iteration 0; clogging runs the same source terms.
_HEAT_BUDGET = "HEAT_BUDGET"
_CLOGGING = "CLOGGING_ON_BARS"
_SALINITY = "SALINITY"
_DYNAMIC_COVER = "DYNAMIC_ICE_COVER"
_FRAZIL_CLASSES = "NUMBER_OF_CLASSES_FOR_SUSPENDED_FRAZIL_ICE"

# The incoming shortwave, floored at darkness.
_SOLAR = {"kind": "mesh", "ramp": "inferno", "units": "W/m2", "floor": 0}
# A net or turbulent flux gains or loses, so it diverges about zero, the hinge between ice forming or not.
_FLUX = {"kind": "mesh", "ramp": "rdbu_r", "units": "W/m2", "center": 0.0}
# Ice is put on the water where it formed, so cover and thickness are masked below a fraction of their peak; a heat flux is drawn whole.
_THICKNESS = {"kind": "mesh", "ramp": "blues", "units": "m", "floor": 0}
_COVER = {"kind": "mesh", "ramp": "blues", "units": "", "floor": 0}
# An elevation the module computes over the cover, a surface rather than an injected quantity.
_ELEVATION = {"kind": "mesh", "ramp": "viridis", "units": "m"}
_PROBABILITY = {"kind": "mesh", "ramp": "purples", "units": ""}
_VELOCITY = {"kind": "mesh", "ramp": "plasma", "units": "m/s", "floor": 0}
# A class rather than a magnitude: open water, static and thickened border ice, and the products of those primes for a node in several.
_CLASS = {"kind": "mesh", "ramp": "set1", "units": ""}
_FRAZIL = {"kind": "mesh", "ramp": "magma", "units": "", "floor": 0}
_PARTICLES = {"kind": "mesh", "ramp": "magma", "units": "1/m3", "floor": 0}
# A background quantity is ranged over what was measured, not pinned to a floor it never reaches.
_TEMPERATURE = {"kind": "mesh", "ramp": "rdylbu_r", "units": "C"}
_SALT = {"kind": "mesh", "ramp": "ylgnbu", "units": "ppt"}

# What the module writes, by VARIABLES FOR GRAPHIC PRINTOUTS mnemonic: the sixteen characters the
# record names the row in, its unit, how it draws. The engine indexes exactly 24 variables packed into
# 32-character records, so a longer name spills into the unit field. The first fifteen rows are SI;
# the last two are the thermal budget's. Two more (surface particle number, concentration) get no
# result-file name in the English branch and a nameless record is unreadable, so they are not rowed.
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
    # FRAZIL THICKNESS and UNDER ICE THICK. are deprecated and bound to work arrays never written, so the file holds stale memory; neither is a row.
    "COV_EQ": Output("EQUIV. SURFACE", "m", style=_ELEVATION, has_edge=False),
    "COV_ET": Output("TOP ICE COVER", "m", style=_ELEVATION, has_edge=False),
    "COV_EB": Output("BOTTOM ICE COVER", "m", style=_ELEVATION, has_edge=False),
    "COV_THT": Output("TOTAL ICE THICK.", "m", style=_THICKNESS, has_edge=True),
    "ICETYPE": Output("CHARACTERISTICS", "", style=_CLASS, has_edge=True),
    "NTOT": Output("PARTICLES NUMBER", "1/m3", style=_PARTICLES, has_edge=True,
                   under=_HEAT_BUDGET),
    "CTOT": Output("TOTAL CONCENTRAT", "", style=_FRAZIL, has_edge=True,
                   under=_HEAT_BUDGET),
})

# What the module ADDTRACERs to its host's tracers, in order, under the engine's 16-character
# names and units. Temperature is added in every coupled run; the rest under their branch. Frazil
# classes and ice cover have an edge; temperature and salinity are everywhere.
_TEMPERATURE_ROW = Output("TEMPERATURE", "oC", style=_TEMPERATURE, has_edge=False)
_SALINITY_ROW = Output("SALINITY", "ppt", style=_SALT, has_edge=False)
# The unit field the engine writes beside a frazil tracer; the record carries it rather than a dimension.
_VOLUME_FRACTION = "VOLUME FRACTION"
_FRAZIL_ROW = Output("FRAZIL", _VOLUME_FRACTION, style=_FRAZIL, has_edge=True)
_COVER_FRACTION_ROW = Output("ICE COVER FRAC.", "SURFAC FRACTION", style=_COVER,
                             has_edge=True)
_COVER_THICKNESS_ROW = Output("ICE COVER THICK.", "M", style=_THICKNESS,
                              has_edge=True)


def _appended(body: Mapping[str, Any]) -> tuple[Output, ...]:
    """The tracers this coupled body puts on its host's own.

    The engine refuses a coupled KHIONE with no active process, so temperature is on every host.
    Frazil and cover ride the thermal or clogging branch; cover-impact or border-ice runs add neither.
    """
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
    return (_Khione.switched(_HEAT_BUDGET, stated)
            or _Khione.switched(_CLOGGING, stated))


def _classes(stated: Mapping[str, Any]) -> int:
    counted = stated.get(_FRAZIL_CLASSES,
                         _Khione.slot(_FRAZIL_CLASSES).engine_default)
    return max(int(counted), 1)


def _frazil_rows(classes: int) -> list[Output]:
    """One host tracer per frazil class, numbered from one.

    A single class is added under the bare name; several are numbered, the engine's own spelling.
    """
    if classes == 1:
        return [_FRAZIL_ROW]
    return [replace(_FRAZIL_ROW, name=f"FRAZIL {n}") for n in range(1, classes + 1)]


# Keywords a coupled KHIONE deck has only under a 3D host: the second result file and format, its table, and 3D frazil diffusion. A 2D host builds none of those fields.
_ONLY_3D = frozenset((
    "RD_RESULTS_FILE",
    "RD_RESULTS_FILE_FORMAT",
    "VARIABLES_FOR_3D_GRAPHIC_PRINTOUTS",
    "SCHEME_FOR_DIFFUSION_OF_FRAZIL_IN_3D",
    "COEFFICIENT_FOR_HORIZONTAL_DIFFUSION_OF_FRAZIL",
    "COEFFICIENT_FOR_VERTICAL_DIFFUSION_OF_FRAZIL",
))


# Written into its OWN result past the indexed rows, allocated in the thermal budget. Frazil
# concentration and particle number are written per class, at the surface as well, numbered from one
# (the dictionary's ``i``); a single class has no number, and the surface particle number shares the
# column one's result-file name, so a multi-class result carries it twice.
_PER_CLASS: Mapping[str, tuple[str, str, Mapping[str, Any], bool]] = MappingProxyType({
    "F": ("FRAZIL", "FRAZIL CLASS {n}", _FRAZIL, True),
    "N": ("NB PARTICLE", "NB PARTICLE {n}", _PARTICLES, True),
    "SF": ("FRAZIL S", "FRAZIL CLASS {n}S", _FRAZIL, True),
    "SN": ("NB PARTICLE S", "NB PARTICLE {n}", _PARTICLES, True),
})

# The rows the thermal budget adds beside those, each under the switch allocating it; the engine writes no unit field for them.
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
        """The rows above, then everything the thermal budget adds past them.

        The class count is the deck's own, so every frazil class is rowed.
        """
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

        Mesh and boundary file are the host's (KHIONE solves on the same nodes); the result is the module's
        own file. Everything else is a dictionary keyword stated by name on the body.
        """
        for name in keywords:
            cls.slot(name)
        return _body({"GEOMETRY_FILE": geometry,
                      "BOUNDARY_CONDITIONS_FILE": boundary,
                      "RESULTS_FILE": RESULT_FILENAME, **keywords})


def _body(slots: Mapping[str, Any]) -> Mapping[str, Any]:
    return {"module": "khione", "steering": STEERING_FILENAME,
            "slots": dict(slots)}


KHIONE = _Khione
KHIONE.MODULE_OUTPUT = MODULE_OUTPUT
KHIONE.PRINTOUTS = PRINTOUTS
# The result the primitives read: KHIONE's own file beside the host's; the host's file carries only the tracer set above.
KHIONE.RESULT_FILE = RESULT_FILENAME
KHIONE.ONLY_3D = _ONLY_3D
# Slots the engine marks deprecated and binds to never-filled work arrays; the printouts keyword may fold them into a prefix so the file carries them, but nothing is read off them.
KHIONE.UNWRITTEN = MappingProxyType({
    "COV_THF": Unwritten("FRAZIL THICKNESS", (
        "point_khione.f: '14 FRAZIL ICE THICKNESS EX:THIFEMF -> DEPRECATED', "
        "the slot bound to the work array T1")),
    "COV_THUN": Unwritten("UNDER ICE THICK.", (
        "point_khione.f: '15 UNDERCOVER ICE THICKNESS EX:HUN -> DEPRECATED', "
        "the slot bound to the work array T2")),
})
KHIONE.APPENDABLE = (
    ("every coupled run", (_TEMPERATURE_ROW,)),
    (_SALINITY, (_SALINITY_ROW,)),
    (f"{_HEAT_BUDGET} or {_CLOGGING}, one per frazil class", (_FRAZIL_ROW,)),
    (_DYNAMIC_COVER, (_COVER_FRACTION_ROW, _COVER_THICKNESS_ROW)),
)
KHIONE.appends(_appended)
KHIONE.reads(**PRIMITIVES)
