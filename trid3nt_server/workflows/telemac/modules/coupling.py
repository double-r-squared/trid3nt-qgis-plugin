"""The ``coupling`` composite, which both hydrodynamic carriers register.

A coupled body names its module, its steering file and the slots that go into
that file; what the CARRIER states about it is three keywords and nothing more.
The one thing the two carriers do not share is the water column: a module has
keywords that describe one, and a carrier that solves none would parse them and
then report a field nobody solved, so it refuses them by name."""

from __future__ import annotations

from typing import Any, Callable, Mapping

from .module import SlotRefused

__all__ = ["couples"]

Expander = Callable[[Any], tuple[Mapping[str, Any], Mapping[str, Any]]]


def couples(*, water_column: bool) -> Expander:
    """The composite a carrier that does or does not solve a column registers."""

    def _coupling(value: Any) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        """The coupled bodies a template named -> what the CARRIER states about them.

        A coupled body's own slots go to that module's steering file, never here.
        A body whose ``given`` values all resolved to nothing was asked for
        nothing, and states nothing."""
        from . import wrapper_for

        bodies = [body for body in value
                  if any(v is not None for v in body.get("given", (True,)))]
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
    """A coupled body states nothing this carrier cannot build.

    The keywords a module has only under a three-dimensional host describe a
    water column; read off a deck a two-dimensional carrier couples, they would
    be parsed and then ignored, and the run would report a field nobody solved."""
    named = sorted(set(dict(body.get("slots") or {})) & set(wrapper.ONLY_3D))
    if named:
        raise SlotRefused(
            f"{', '.join(wrapper.slot(name).keyword for name in named)} is a "
            f"{wrapper.MODULE} keyword a THREE-dimensional carrier states; this "
            "run solves no water column for it to describe.")
