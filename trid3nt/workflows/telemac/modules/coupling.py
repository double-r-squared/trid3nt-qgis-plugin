"""The ``coupling`` composite, which both hydrodynamic carriers register.

A coupled body names its module, steering file and slots; the carrier states three keywords.
A carrier that solves no water column refuses a module's column keywords by name.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Mapping

from ...runtime.module import SlotRefused
from .module import USER_FORTRAN_DIR

__all__ = ["bare_coupling", "couples"]

Expander = Callable[..., tuple[Mapping[str, Any], Mapping[str, Any]]]


def bare_coupling(carrier: type, stated: Any) -> list[dict[str, Any]]:
    """Convert explicit body choices to the carrier's coupling composite declaration."""
    from . import WRAPPERS

    if not isinstance(stated, (list, tuple)):
        raise SlotRefused("coupling takes a list of body declarations.")
    bodies = []
    modules: set[str] = set()
    filenames: set[str] = set()
    for row in stated:
        if not isinstance(row, Mapping) or set(row) - {"module", "steering", "process"}:
            raise SlotRefused("Each coupling declaration takes module, steering and process.")
        module = str(row.get("module", "")).lower()
        if ("coupling" not in carrier.COMPOSITES or module not in WRAPPERS
                or module == carrier.MODULE or f"{module.upper()}_STEERING_FILE" not in carrier.MODULE_INPUT):
            raise SlotRefused(f"{carrier.MODULE} cannot attach the {module or 'unnamed'} body.")
        steering = row.get("steering") or f"{module}.cas"
        if not isinstance(steering, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", steering) is None:
            raise SlotRefused("Coupled steering must be a single filename.")
        if module in modules or steering.casefold() in filenames:
            raise SlotRefused("Coupling modules and steering filenames must be unique.")
        modules.add(module)
        filenames.add(steering.casefold())
        declaration = {"module": module, "steering": steering, "slots": {}}
        if module == "waqtel":
            slot = carrier.slot("WATER_QUALITY_PROCESS")
            if row.get("process") is None:
                raise SlotRefused(f"{slot.keyword} is required when attaching {module}.")
            declaration["process"] = slot.check(row["process"])
        elif row.get("process") is not None:
            raise SlotRefused(f"{module} does not take a coupling process.")
        bodies.append(declaration)
    return bodies


def couples(*, water_column: bool) -> Expander:
    """The composite a carrier registers, by whether it solves a column."""

    def _coupling(value: Any, *, run: Mapping[str, Any]
                  ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        """The coupled bodies a template named -> what the carrier states about them.

        A body's own slots go to its module's steering file; a body whose ``given`` inputs all hold nothing states nothing.
        """
        from . import wrapper_for

        bodies = [body for body in value
                  if any((run.get(v) if isinstance(v, str) else v) is not None
                         for v in body.get("given", (True,)))]
        if not bodies:
            return ({}, {})
        slots: dict[str, Any] = {
            "COUPLING_WITH": ";".join(body["module"].upper() for body in bodies)}
        files: dict[str, Any] = {}
        for body in bodies:
            wrapper = wrapper_for(body["module"])
            if not water_column:
                _refuse_3d_only(body, wrapper)
            slots[f"{body['module'].upper()}_STEERING_FILE"] = body["steering"]
            if "process" in body:
                slots["WATER_QUALITY_PROCESS"] = body["process"]
            files[body["steering"]] = body
            for held, code in wrapper.CODE_ON_HOST.items():
                if run.get(held) is not None:
                    slots["FORTRAN_FILE"] = USER_FORTRAN_DIR
                    files.update({f"{USER_FORTRAN_DIR}/{name}": text()
                                  for name, text in code.items()})
        return slots, files

    return _coupling


def _refuse_3d_only(body: Mapping[str, Any], wrapper: Any) -> None:
    """Keywords a module has only under a 3D host describe a water column; a 2D carrier would parse and ignore them, reporting a field nobody solved."""
    named = sorted(set(dict(body.get("slots") or {})) & set(wrapper.ONLY_3D))
    if named:
        raise SlotRefused(
            f"{', '.join(wrapper.slot(name).keyword for name in named)} is a "
            f"{wrapper.MODULE} keyword a THREE-dimensional carrier states; this "
            "run solves no water column for it to describe.")
