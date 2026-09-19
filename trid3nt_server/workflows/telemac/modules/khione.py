"""The KHIONE wrapper: its module input, the coupled body a host names, and the
twenty-four variables its own result file carries.

KHIONE runs UNDER a hydrodynamic module and writes its OWN result beside the
host's, so nothing it rows lands on the host's table. It has no forcing of its
own either: the air temperature, the dew point, the cloud, the wind, the rain
and the snow are read out of the atmospheric data file the host names. What the
dictionary lacks is the three files a coupled deck carries, which is the whole
of what the body states past the keywords a deck states by name."""

from __future__ import annotations

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
#: The switch the last four rows exist under: the engine allocates those arrays
#: only inside the thermal budget, and asking for one it did not allocate stops
#: the solve at iteration 0 rather than dropping the row.
_HEAT_BUDGET = "HEAT_BUDGET"

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


class _Khione(Module("khione")):  # type: ignore[misc]
    """The module input, and the coupled body a host's ``coupling`` expands."""

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
#: so nothing it rows is counted on the host's table.
KHIONE.RESULT_FILE = RESULT_FILENAME
KHIONE.ONLY_3D = _ONLY_3D
KHIONE.reads(**PRIMITIVES)
