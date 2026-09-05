"""The oil module's own preset, and the per-run Fortran the release is compiled into.

The module rides ON TOP of the tracer solve and the presence of its steering file
activates it. What is built here is the CONTENT of both: the preset in the
module reader's own format, and the engine's own ``oil_flot.f`` with this run's
release step and coordinates in it - because the release the module gets has to
be the clearance-snapped point the caller settled, and a release the flow never
reaches produces a clean run and an empty slick.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

__all__ = ["OIL_PRESETS", "oil_inputs"]

#: Oil presets in the module reader's own format. Fractions sum to 1 per preset;
#: the aromatic rows carry mass fraction, boiling point, solubility and the
#: dissolution and volatilisation rates.
OIL_PRESETS: dict[str, dict[str, Any]] = {
    "light_crude": dict(
        compo=[(0.5, 645.0), (0.3, 830.0)],
        hap=[(0.2, 673.0, 0.018, 1.0e-5, 5.0e-5)],
        rho=850.0, eta=1.0e-5, voldev=20.0, tamb=288.0, etal=1),
    "diesel": dict(
        compo=[(0.6, 560.0), (0.25, 700.0)],
        hap=[(0.15, 610.0, 0.005, 1.0e-5, 8.0e-5)],
        rho=840.0, eta=4.0e-6, voldev=10.0, tamb=288.0, etal=1),
    "heavy_fuel": dict(
        compo=[(0.75, 900.0), (0.2, 1050.0)],
        hap=[(0.05, 800.0, 0.001, 5.0e-6, 1.0e-5)],
        rho=960.0, eta=5.0e-4, voldev=30.0, tamb=288.0, etal=1),
}

#: The engine's own release routine, shipped beside this module because the
#: release coordinates are compiled INTO it.
_TEMPLATE = Path(__file__).resolve().parent / "oil_templates" / "oil_flot_template.f"


def oil_inputs(*, preset: str, release_step: int,
               x: float, y: float) -> dict[str, str]:
    """The oil steering file and this run's ``oil_flot.f``, as content."""
    name = str(preset)
    spec = OIL_PRESETS.get(name, OIL_PRESETS["light_crude"])
    lines = [f"{name.upper()} - trid3nt oil preset",
             str(len(spec["compo"])), "FM_COMPO TB_COMPO"]
    lines += [f"{fm} {tb}" for fm, tb in spec["compo"]]
    lines += ["NB_HAP", str(len(spec["hap"])), "FM_HAP TB_HAP SOLU KDISS KVOL"]
    lines += [" ".join(str(v) for v in row) for row in spec["hap"]]
    lines += ["RHO_OIL", str(spec["rho"]), "ETA_OIL", str(spec["eta"]),
              "VOLDEV", str(spec["voldev"]), "TAMB", str(spec["tamb"]),
              "ETAL", str(spec["etal"])]
    fortran = _TEMPLATE.read_text(encoding="utf-8")
    fortran = fortran.replace("IF(LT.EQ.60)", f"IF(LT.EQ.{int(release_step)})")
    fortran = re.sub(r"COORD_X=\d+\.D0", f"COORD_X={x:.0f}.D0", fortran)
    fortran = re.sub(r"COORD_Y=\d+\.D0", f"COORD_Y={y:.0f}.D0", fortran)
    return {"steering": "\n".join(lines) + "\n", "fortran": fortran}
