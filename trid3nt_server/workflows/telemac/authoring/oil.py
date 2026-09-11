"""The oil module's two input files, as content: the preset the module reads,
and the release routine this run's source is compiled into.

The presence of the steering file activates the module on top of the tracer
solve. The point compiled in has to be the settled one: a source the flow never
reaches solves clean and slicks nothing."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

__all__ = ["release_routine", "steering_text"]

#: The engine's own release routine, shipped beside this module because the
#: release coordinates are compiled INTO it.
_TEMPLATE = Path(__file__).resolve().parent / "oil_templates" / "oil_flot_template.f"


def steering_text(name: str, preset: Mapping[str, Any]) -> str:
    """The oil steering file in the module reader's own format.

    ``compo`` rows are (mass fraction, boiling point); ``hap`` rows add the
    solubility and the dissolution and volatilisation rates."""
    lines = [f"{name.upper()} - trid3nt oil preset",
             str(len(preset["compo"])), "FM_COMPO TB_COMPO"]
    lines += [f"{fm} {tb}" for fm, tb in preset["compo"]]
    lines += ["NB_HAP", str(len(preset["hap"])), "FM_HAP TB_HAP SOLU KDISS KVOL"]
    lines += [" ".join(str(v) for v in row) for row in preset["hap"]]
    lines += ["RHO_OIL", str(preset["rho"]), "ETA_OIL", str(preset["eta"]),
              "VOLDEV", str(preset["voldev"]), "TAMB", str(preset["tamb"]),
              "ETAL", str(preset["etal"])]
    return "\n".join(lines) + "\n"


def release_routine(release_step: int, x: float, y: float) -> str:
    """This run's ``oil_flot.f``: the routine with its step and point compiled in."""
    fortran = _TEMPLATE.read_text(encoding="utf-8")
    fortran = fortran.replace("IF(LT.EQ.60)", f"IF(LT.EQ.{int(release_step)})")
    fortran = re.sub(r"COORD_X=\d+\.D0", f"COORD_X={x:.0f}.D0", fortran)
    fortran = re.sub(r"COORD_Y=\d+\.D0", f"COORD_Y={y:.0f}.D0", fortran)
    return fortran
