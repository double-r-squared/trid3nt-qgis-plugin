"""What PRODUCES a row: the producer a declaration names, and the one word it is written with.

``tool(...)`` is the one producer word; what a supplied artifact must be shaped
like is read off the sources that measure its class.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from trid3nt_server.inputs.geometry import artifact_class
from trid3nt_server.inputs.input import GEOMETRY_CLASS, Input, take_name

from .errors import DeclarationError, SuppliedGeometryError

__all__ = [
    "Producer",
    "ToolWord",
    "refuse_wrong_shape",
    "shapes_of",
    "tool",
]

def shapes_of(data_class: str) -> frozenset[str]:
    """The artifact CLASSES one data class arrives in, off the sources that measure it.
    Read from the rows, so a source published in a new shape widens what a slot accepts."""
    from trid3nt_server.tools.fetchers._router.registration import _SPEC_REGISTRY
    from trid3nt_server.tools.search.match import sources_with_coverage

    return frozenset(
        str(_SPEC_REGISTRY[name].output.layer_type)
        for name, row in sources_with_coverage()
        if row.data_class == str(data_class) and name in _SPEC_REGISTRY)


@dataclass(frozen=True, slots=True)
class Producer:
    """How an artifact comes into being: a runner name plus its declared args."""

    runner: str
    kwargs: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    row: str = ""

    def __set_name__(self, owner: type, name: str) -> None:
        take_name(self, "row", name)

    def __post_init__(self) -> None:
        if not self.runner:
            raise DeclarationError("Producer declares no runner path.")
        object.__setattr__(self, "kwargs", MappingProxyType(dict(self.kwargs)))


class ToolWord:
    """The ONE author word a template declares with: ``tool(name, **kwargs)`` for a
    row's producer, ``tool.build_mesh(...)`` for the mesh ask. No role prefix:
    what a runner does to the world is the tool REGISTRY's knowledge."""

    def __call__(self, name: str, **kwargs: Any) -> Producer:
        return Producer(runner=name, kwargs=kwargs)

    @staticmethod
    def build_mesh(**ask: Any) -> Any:
        """Declare a mesh ask -> a frozen declaration, checked at the mesh router."""
        from trid3nt_server.tools.mesh.tool import MeshTool

        return MeshTool.build_mesh(**ask)


#: One object, so every import path of ``tool`` is the same name.
tool = ToolWord()


def refuse_wrong_shape(row: Input, value: Any) -> None:
    """Refuse a supplied artifact whose CLASS is not the shape ``row`` declared.

    Suffix-deep and no deeper; an unclassifiable artifact passes."""
    if row.data_class:
        # The supplied twin takes every shape the class's sources publish; a value no shape reads out of is the number itself.
        found = artifact_class(value)
        shapes = shapes_of(row.data_class)
        if found is None or not shapes or found in shapes:
            return
        raise SuppliedGeometryError(
            f"Input {row.name!r} needs {row.data_class}, which is measured "
            f"as {' or '.join(sorted(shapes))}; what was supplied reads as "
            f"{found} ({value!r}).")
    if row.geometry is None:
        return
    found = artifact_class(value)
    wanted = GEOMETRY_CLASS[row.geometry]
    if found is None or found == wanted:
        return
    raise SuppliedGeometryError(
        f"Input {row.name!r} declares geometry={row.geometry!r}, so it takes a "
        f"{wanted} artifact; what was supplied reads as {found} ({value!r}). "
        "Supply the shape the slot declares, or leave it unfilled."
    )
