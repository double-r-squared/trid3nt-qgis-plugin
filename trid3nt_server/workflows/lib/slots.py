"""Slot payloads: what a template hands the engine facade, as VALUE OBJECTS.

A slot is a NAMED bundle of declared values - the mesh ask, the physics, the
forcing. It exists so a template states each group once and the facade explodes
it into the runner's real signature, instead of a plan step funnelling dozens of
explicitly-named kwargs through three files (the composite disease).

A slot is a plan-CONSTRUCTION value: its members are ``ParamRef``/``Ref``
descriptions, and the facade unpacks it while the plan is being built, so what
reaches ``Step.kwargs`` is a plain mapping the interpreter already knows how to
bind. Nothing here reaches run time.

A slot is also a BINDING BLOCK: a template writes PHYSICS / FORCING at module
level, above ``plan(ops)``, beside the MESH declaration the mesh tool owns, and
the plan reads them. That makes them process-lifetime values shared by every run,
so they are DEEP-frozen - a nested mapping or list inside one would otherwise be a
mutable global that one run could edit for the next.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Mapping

from .errors import PlanValidationError

__all__ = ["Forcing", "Physics", "Slot", "deep_freeze"]


def deep_freeze(value: Any) -> Any:
    """Freeze a declared value ALL THE WAY DOWN - mappings included.

    A binding block lives at module scope for the life of the process and every
    run of the template reads the same object, so a mutable container inside one
    is a cross-run channel: a step that pops a key out of a declared dict changes
    what the NEXT run declares. Mappings become read-only views, sequences become
    tuples, sets become frozensets. Anything else is left alone - a ``ParamRef``,
    a ``Ref`` and a number are already values.
    """
    if isinstance(value, (str, bytes, bytearray)):
        return bytes(value) if isinstance(value, bytearray) else value
    if isinstance(value, Mapping):
        return MappingProxyType({k: deep_freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        frozen = [deep_freeze(v) for v in value]
        if isinstance(value, tuple) and hasattr(value, "_make"):
            return value._make(frozen)      # a namedtuple keeps its field names
        return tuple(frozen)
    if isinstance(value, (set, frozenset)):
        return frozenset(deep_freeze(v) for v in value)
    return value


class Slot:
    """An open bundle of declared values, keyed by name.

    Open rather than fixed-field because what shapes a run varies by engine and
    by question; the facade that unpacks it is where the names are CHECKED
    against a real signature, so a typo fails at plan construction rather than
    silently vanishing.
    """

    __slots__ = ("_values",)
    kind = "slot"

    def __init__(self, **values: Any) -> None:
        object.__setattr__(
            self, "_values",
            MappingProxyType({k: deep_freeze(v) for k, v in values.items()}))

    @property
    def values(self) -> Mapping[str, Any]:
        return self._values

    def __setattr__(self, name: str, value: Any) -> None:
        raise PlanValidationError(f"{type(self).__name__} is a frozen declaration.")

    def __getattr__(self, name: str) -> Any:
        values = object.__getattribute__(self, "_values")
        if name in values:
            return values[name]
        raise AttributeError(
            f"{type(self).__name__} declares no {name!r} (declared: {sorted(values)})"
        )

    def __repr__(self) -> str:
        inner = ", ".join(f"{k}={v!r}" for k, v in self._values.items())
        return f"{type(self).__name__}({inner})"


class Physics(Slot):
    """WHAT is being modeled: the named process plus the values that shape it.

    The process name is the question's physics in one word - ``"tracer"``,
    ``"waqtel_o2"``, ``"morphodynamics"`` - and the facade routes on it when an
    engine serializes different processes differently.
    """

    kind = "physics"
    __slots__ = ("_process",)

    def __init__(self, process: str, **values: Any) -> None:
        if not process:
            raise PlanValidationError("Physics declares no process.")
        object.__setattr__(self, "_process", str(process))
        super().__init__(**values)

    @property
    def process(self) -> str:
        return self._process

    def __repr__(self) -> str:
        inner = ", ".join(f"{k}={v!r}" for k, v in self.values.items())
        return f"Physics({self._process!r}, {inner})"


class Forcing(Slot):
    """WHAT DRIVES the run: inflow, rain, wind, tide - the world pushing on it."""

    kind = "forcing"
