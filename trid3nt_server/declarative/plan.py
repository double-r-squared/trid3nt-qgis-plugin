"""The plan VALUE: steps, gates, refs, modifiers, ``Workflow[...]`` composition.

Nothing here executes; ``plan(p, d)`` returns one of these trees and the
interpreter walks it.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Literal, Mapping

from .errors import ModifierIllegalError, PlanValidationError

__all__ = [
    "ChartSpec",
    "DrawGate",
    "FormGate",
    "Gate",
    "Node",
    "Plan",
    "Ref",
    "RenderSpec",
    "Step",
    "Transparent",
    "When",
    "Within",
    "Workflow",
]


@dataclass(frozen=True, slots=True)
class Ref:
    """A reference to a declared param, a declared Data, or a ``.named()`` step.

    Dotted (``Ref("reach.seed")``) reads a field off the referenced step's result.
    """

    path: str

    def __post_init__(self) -> None:
        if not self.path or not self.path.split(".")[0].isidentifier():
            raise PlanValidationError(f"Ref({self.path!r}) has no identifier root.")

    @property
    def root(self) -> str:
        return self.path.split(".", 1)[0]

    @property
    def tail(self) -> tuple[str, ...]:
        return tuple(self.path.split(".")[1:])


class _Transparent:
    def __repr__(self) -> str:
        return "Transparent"


#: Render-only zero handling. Rasters keep their zeros (law 9 applies to pixels).
Transparent = _Transparent()


@dataclass(frozen=True, slots=True)
class Within:
    """Draw-time constraint: the drawn geometry must fall inside the referenced feature."""

    target: Ref


@dataclass(frozen=True, slots=True)
class RenderSpec:
    """A declared styling of a step's raster result through the one publish seam."""

    preset: str
    zero: Any = None


@dataclass(frozen=True, slots=True)
class ChartSpec:
    """A declared chart: the SPEC is the product; the plugin dock is the renderer.

    ``builder`` is a dotted import path to a pure ``(result, params) -> payload dict``.
    """

    name: str
    builder: str
    x: str | None = None
    y: str | None = None


@dataclass(frozen=True, slots=True)
class Step:
    """One declared unit of work: a dotted runner path plus its declared arguments."""

    runner: str
    kwargs: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    name: str | None = None
    consequential: bool = False
    rebinds_domain: bool = False
    renders: tuple[RenderSpec, ...] = ()
    charts: tuple[ChartSpec, ...] = ()

    def __post_init__(self) -> None:
        if not self.runner:
            raise PlanValidationError("Step declares no runner path.")
        object.__setattr__(self, "kwargs", MappingProxyType(dict(self.kwargs)))

    @property
    def label(self) -> str:
        return self.name or self.runner.rsplit(".", 1)[-1]

    def named(self, name: str) -> "Step":
        """Name this step so later steps can ``Ref`` its result."""
        if self.name is not None:
            raise ModifierIllegalError(
                f"step {self.name!r} is already named; .named() applies once."
            )
        if not name.isidentifier():
            raise PlanValidationError(f".named({name!r}) is not an identifier.")
        return replace(self, name=name)

    def overrides_domain(self) -> "Step":
        """Declare that this step REFINES the current domain for every step after it."""
        return replace(self, rebinds_domain=True)

    def render(self, *, preset: str, zero: Any = None) -> "Step":
        """Declare how this step's raster result is styled when published."""
        return replace(self, renders=self.renders + (RenderSpec(preset=preset, zero=zero),))

    def chart(self, name: str, *, builder: str, x: str | None = None,
              y: str | None = None) -> "Step":
        """Declare a chart SPEC built from this step's result."""
        return replace(self, charts=self.charts + (ChartSpec(name=name, builder=builder,
                                                             x=x, y=y),))


@dataclass(frozen=True, slots=True)
class Gate(Step):
    """A declared pause point on the existing pending-confirmation spine."""

    kind: Literal["form", "draw"] = "form"
    param: str | None = None
    geometry: str | None = None
    prompt: str = ""
    constrain: Any = None

    def named(self, name: str) -> "Gate":
        raise ModifierIllegalError("a gate is not Ref-able; .named() is illegal on a gate.")

    def overrides_domain(self) -> "Gate":
        raise ModifierIllegalError(".overrides_domain() is illegal on a gate.")

    def render(self, **_kw: Any) -> "Gate":
        raise ModifierIllegalError(".render() is illegal on a gate.")

    def chart(self, *_a: Any, **_kw: Any) -> "Gate":
        raise ModifierIllegalError(".chart() is illegal on a gate.")


def FormGate(*, title: str = "") -> Gate:  # noqa: N802 - a value constructor
    """The resolved param sheet as an editable form (auto mode: labeled defaults)."""
    return Gate(runner="declarative.gate.form", kind="form", prompt=title)


def DrawGate(*, param: str, geometry: str = "point", prompt: str = "",  # noqa: N802
             constrain: Any = None) -> Gate:
    """Ask the user to draw the value of one USER-door param on the canvas."""
    if geometry not in ("point", "polyline", "polygon", "rectangle"):
        raise PlanValidationError(f"DrawGate geometry {geometry!r} is not a draw kind.")
    return Gate(runner="declarative.gate.draw", kind="draw", param=param,
                geometry=geometry, prompt=prompt, constrain=constrain)


@dataclass(frozen=True, slots=True, init=False)
class When:
    """A branch kept INSPECTABLE in the plan value: the condition and the body it guards."""

    condition: Any
    body: tuple[Any, ...]

    def __init__(self, condition: Any, *body: Any) -> None:
        object.__setattr__(self, "condition", condition)
        object.__setattr__(self, "body", tuple(body))
        if not self.body:
            raise PlanValidationError("When(...) guards no steps.")

    @property
    def taken(self) -> bool:
        return bool(self.condition)


Node = Step | Gate | When


@dataclass(frozen=True, slots=True)
class Plan:
    """A workflow's step tree - a pure value the interpreter walks."""

    name: str
    engine: str | None
    steps: tuple[Node, ...]

    def flat(self) -> tuple[Step, ...]:
        """Every step in execution order, with untaken ``When`` bodies dropped."""
        out: list[Step] = []
        for node in self.steps:
            if isinstance(node, When):
                if node.taken:
                    out.extend(_flatten(node.body))
            else:
                out.append(node)
        return tuple(out)

    def declared(self) -> tuple[Step, ...]:
        """Every step INCLUDING untaken branches - what the validator and printer read."""
        out: list[Step] = []
        for node in self.steps:
            out.extend(_flatten((node,)))
        return tuple(out)

    def describe(self) -> list[str]:
        lines = [f"{self.name} (engine={self.engine or '-'})"]
        for i, step in enumerate(self.flat(), 1):
            bits = [step.label]
            if step.rebinds_domain:
                bits.append("[overrides domain]")
            for r in step.renders:
                bits.append(f"[render {r.preset}]")
            for c in step.charts:
                bits.append(f"[chart {c.name}]")
            lines.append(f"  {i}. {' '.join(bits)}")
        return lines


def _flatten(nodes: tuple[Any, ...]) -> list[Step]:
    out: list[Step] = []
    for node in nodes:
        if isinstance(node, When):
            out.extend(_flatten(node.body))
        elif isinstance(node, Step):
            out.append(node)
        else:
            raise PlanValidationError(
                f"plan node {node!r} is not a Step, Gate or When."
            )
    return out


class Workflow:
    """``Workflow(name, engine=...)[step, step, ...]`` - the plan constructor."""

    __slots__ = ("_name", "_engine")

    def __init__(self, name: str, *, engine: str | None = None) -> None:
        if not name:
            raise PlanValidationError("Workflow declares no name.")
        self._name = name
        self._engine = engine

    def __getitem__(self, items: Any) -> Plan:
        nodes = items if isinstance(items, tuple) else (items,)
        _flatten(nodes)  # shape check at construction
        return Plan(name=self._name, engine=self._engine, steps=tuple(nodes))
