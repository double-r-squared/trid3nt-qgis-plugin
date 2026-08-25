"""Slot payloads: what a template hands the engine facade, as VALUE OBJECTS.

A slot is a NAMED bundle of declared values - the mesh ask, the physics, the
forcing. It exists so a template states each group once and the facade explodes
it into the runner's real signature, instead of a plan step funnelling dozens of
explicitly-named kwargs through three files (the composite disease).

A slot is a plan-CONSTRUCTION value: its members are ``ParamRef``/``Ref``
descriptions, and the facade unpacks it while the plan is being built, so what
reaches ``Step.kwargs`` is a plain mapping the interpreter already knows how to
bind. Nothing here reaches run time.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from .errors import PlanValidationError

__all__ = ["Forcing", "MeshPolicy", "Physics", "Slot"]


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
        object.__setattr__(self, "_values", MappingProxyType(dict(values)))

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


@dataclass(frozen=True, slots=True)
class MeshPolicy:
    """The engine-NEUTRAL mesh ask: HOW FINELY to resolve, and nothing else.

    Sizing is the only part of the ask that is genuinely universal - every
    unstructured solver, structured grid and node-link network answers "how fine".
    SHAPE is not: an extent, a cross-stream width and a bank source describe a
    CORRIDOR, and a corridor is one domain among many. By the placement rule they
    belong to the facade that meshes corridors (``telemac.workflow.CorridorPolicy``),
    not to the universal policy, and they reach ``build_mesh`` as an engine slot.

    Fixed-field because the sizing ask is the same question everywhere, and
    ``EngineOps.build_mesh(domain, policy, **slots)`` is the frozen interface the
    generation strategies evolve behind.
    """

    #: Sizing MODE: auto | fine | coarse. A word, because the user asks in words.
    resolution: Any = "auto"
    #: An explicit target element edge length that overrides the mode.
    target_edge_m: Any = None
