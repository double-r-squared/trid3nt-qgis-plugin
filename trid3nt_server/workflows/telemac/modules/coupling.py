"""The ``coupling`` composite, which both hydrodynamic carriers register.

A coupled body names its module, steering file and slots; the carrier states three keywords.
A carrier that solves no water column refuses a module's column keywords by name.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from .module import SlotRefused

__all__ = ["couples"]

Expander = Callable[..., tuple[Mapping[str, Any], Mapping[str, Any]]]


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
