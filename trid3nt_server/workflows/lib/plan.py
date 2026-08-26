"""The plan VALUE: steps, gates, refs, modifiers, charts, the stage sequence.

Nothing here executes. A template's ``plan(ops)`` returns the step sequence and
the SKELETON names and engines the :class:`Plan`. The plan is STATIC: it reads no
concrete value, so it is built ONCE - at registration - and the interpreter walks
the same value on every run. Every read is a late-bound ``P.<param>`` /
``D.<data>`` / ``Ref("step.field")`` description, and every conditional is a
:class:`When` the interpreter decides AFTER the gates have run.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Callable, Literal, Mapping

from .errors import ModifierIllegalError, PlanValidationError

__all__ = [
    "ChartSpec",
    "D",
    "DataRef",
    "DrawGate",
    "FormGate",
    "Gate",
    "Node",
    "P",
    "ParamRef",
    "Plan",
    "Ref",
    "RenderSpec",
    "RunMode",
    "STAGES",
    "Step",
    "When",
]


def declaration_site(depth: int = 2) -> str:
    """Where a declaration-time ref was BUILT, as ``file.py:line``.

    A ``P.<name>`` typo is caught at registration, far from the module line that
    wrote it, and the sheet it is checked against lists forty names - so the
    refusal has to be able to point back at the line. ``sys._getframe`` is the
    only thing that knows, and it costs one frame walk per declared ref at import.
    """
    try:
        frame = sys._getframe(depth)
    except ValueError:      # shallower stack than the caller assumed
        return ""
    return f"{frame.f_code.co_filename.rsplit('/', 1)[-1]}:{frame.f_lineno}"

#: The universal stage sequence the skeleton walks. A step names the stage it
#: belongs to so the plan reads as the sequence rather than as a list of runners;
#: the facade's five operations are what stamp it.
STAGES: tuple[str, ...] = ("acquire", "prep", "mesh", "gates", "author", "solve",
                           "post", "publish")


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


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class ParamRef:
    """A LATE-BOUND read of a declared param: what ``p.<name>`` yields in ``plan()``.

    A plan DESCRIBES; the interpreter SUBSTITUTES. Baking the concrete value into
    ``Step.kwargs`` at construction time would freeze the sheet before the form
    gate the plan itself declares, so an approved revision could never reach the
    run. The interpreter resolves these against the CURRENT param state instead.

    Every operation that would silently turn the description INTO data refuses:
    truthiness, ``str``/``format``, equality and hashing. Each one is a real leak
    path - an f-string bakes ``ParamRef(...)`` into a layer title, ``==`` answers
    ``False`` against the value the author meant, and hashing lets a ref sit in a
    set the binder used not to walk. ``repr`` stays live: naming the ref is what a
    diagnostic is for.
    """

    name: str
    #: Where ``P.<name>`` was written, as ``file.py:line``. Carried so a name the
    #: template does not declare can be refused AT its construction site.
    origin: str = ""

    def __post_init__(self) -> None:
        if not self.name or not self.name.isidentifier():
            raise PlanValidationError(f"ParamRef({self.name!r}) has no identifier name.")

    def _refuse(self, operation: str) -> "PlanValidationError":
        return PlanValidationError(
            f"ParamRef({self.name!r}) does not support {operation} at "
            "plan-construction time - it is a description of a late-bound read, not "
            "the value. Leave the ref in the plan and let the interpreter "
            "substitute it; a conditional is a When the interpreter decides."
        )

    def __bool__(self) -> bool:
        raise PlanValidationError(
            f"ParamRef({self.name!r}) has no truth value at plan-construction time - "
            "it is a description, not the value. A branch on it is "
            f"When(P.{self.name}, ...), which the interpreter decides after the gates."
        )

    def __str__(self) -> str:
        raise self._refuse("str()")

    def __format__(self, _spec: str) -> str:
        raise self._refuse("f-string / format() interpolation")

    def __eq__(self, _other: Any) -> bool:
        raise self._refuse("==/!= comparison")

    def __hash__(self) -> int:
        raise self._refuse("hashing (set/dict membership)")

    def __repr__(self) -> str:
        return f"ParamRef({self.name!r})"


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class DataRef(Ref):
    """A late-bound read of a declared ``Data``: what ``D.<name>`` yields.

    A :class:`Ref` so the interpreter dereferences it with everything else, and its
    own type so the registration check can say WHICH namespace a bad name came
    from - ``D.terain`` is a Data typo, not a step nobody named.
    """

    origin: str = ""

    def __repr__(self) -> str:
        return f"DataRef({self.path!r})"


class _Namespace:
    """A declaration-time ref namespace. Attribute access BUILDS a ref."""

    __slots__ = ("_kind",)

    def __init__(self, kind: str) -> None:
        object.__setattr__(self, "_kind", kind)

    def __setattr__(self, name: str, value: Any) -> None:
        raise PlanValidationError(
            f"{self._kind} is a declaration namespace, not a place to store values."
        )

    def __repr__(self) -> str:
        return self._kind


class _ParamNamespace(_Namespace):
    """``P.spill_fraction`` IS ``ParamRef('spill_fraction')``, wherever it is written.

    Module-level: a binding block above ``plan()`` (PHYSICS, FORCING, MESH,
    CORRIDOR) is a plain frozen value built out of these, so the recipe reads as
    declarations rather than as a function that has to be called with a sheet to
    mean anything.
    """

    def __getattr__(self, name: str) -> ParamRef:
        if name.startswith("__"):           # let the object protocol probe
            raise AttributeError(name)
        return ParamRef(name, origin=declaration_site())


class _DataNamespace(_Namespace):
    """``D.rivers`` IS ``DataRef('rivers')`` - the declared artifact, by name."""

    def __getattr__(self, name: str) -> DataRef:
        if name.startswith("__"):
            raise AttributeError(name)
        return DataRef(name, origin=declaration_site())


#: The two declaration-time namespaces. They carry NO sheet and NO workflow: a
#: name is checked against the template's own PARAMS/DATA at registration, which
#: is what lets a binding block sit at module level above the plan it feeds.
P = _ParamNamespace("P")
D = _DataNamespace("D")


class _RunMode:
    def __repr__(self) -> str:
        return "RunMode"


#: Declared read of the run's input-gate mode. A composite step that runs its OWN
#: input-review gate takes ``input_mode=RunMode`` so the lever reaches it without
#: becoming a Param - it governs whether the run pauses, it is not a physical value.
RunMode = _RunMode()


@dataclass(frozen=True, slots=True)
class RenderSpec:
    """A declared styling of a step's raster result through the one publish seam."""

    preset: str


@dataclass(frozen=True, slots=True)
class ChartSpec:
    """A declared chart: the SPEC is the product; the plugin dock is the renderer.

    ``builder`` is the FUNCTION ITSELF - a plain, standalone-runnable
    ``(result, params) -> payload dict`` colocated in the template file beside
    the plan it charts. The builder owns the axes: it writes the vega-lite
    encodings, so the spec declares no x/y of its own. A dotted string is
    refused: an import path defers the "does this exist" question to run time,
    after the solve it was supposed to describe.
    """

    name: str
    builder: Callable[..., Any]

    def __post_init__(self) -> None:
        if isinstance(self.builder, str):
            raise PlanValidationError(
                f"chart {self.name!r}: builder is the function object, not the "
                f"dotted path {self.builder!r}. Import the builder and pass it."
            )
        if not callable(self.builder):
            raise PlanValidationError(
                f"chart {self.name!r}: builder {self.builder!r} is not callable."
            )

    @property
    def builder_path(self) -> str:
        """Where the builder lives - the ledger's record of which code ran."""
        return (f"{getattr(self.builder, '__module__', '?')}."
                f"{getattr(self.builder, '__qualname__', repr(self.builder))}")


@dataclass(frozen=True, slots=True)
class Step:
    """One declared unit of work: a dotted runner path plus its declared arguments."""

    runner: str
    kwargs: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    name: str | None = None
    consequential: bool = False
    rebinds_domain: bool = False
    #: This step runs its OWN input-review gate (a migrated composite does), so the
    #: plan must not declare a second one in front of it - the validator refuses it.
    self_gating: bool = False
    renders: tuple[RenderSpec, ...] = ()
    charts: tuple[ChartSpec, ...] = ()
    #: Which universal stage this step belongs to (see ``STAGES``). Stamped by the
    #: engine facade's five operations; a step a template declares directly leaves
    #: it empty and simply reads as unstaged.
    stage: str = ""

    def __post_init__(self) -> None:
        if not self.runner:
            raise PlanValidationError("Step declares no runner path.")
        if self.stage and self.stage not in STAGES:
            raise PlanValidationError(
                f"step {self.runner!r} names stage {self.stage!r}, which is not one "
                f"of the universal stages {STAGES}."
            )
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

    def render(self, *, preset: str) -> "Step":
        """Declare how this step's raster result is styled when published."""
        return replace(self, renders=self.renders + (RenderSpec(preset=preset),))

    def chart(self, name: str, *, builder: Callable[..., Any]) -> "Step":
        """Declare a chart SPEC built from this step's result by ``builder`` itself."""
        return replace(self, charts=self.charts + (ChartSpec(name=name, builder=builder),))


@dataclass(frozen=True, slots=True)
class Gate(Step):
    """A declared pause point on the existing pending-confirmation spine."""

    kind: Literal["form", "draw"] = "form"
    param: str | None = None
    geometry: str | None = None
    prompt: str = ""

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


def DrawGate(*, param: str, geometry: str = "point", prompt: str = "") -> Gate:  # noqa: N802
    """Ask the user to draw the value of one USER-door param on the canvas."""
    if geometry not in ("point", "polyline", "polygon", "rectangle"):
        raise PlanValidationError(f"DrawGate geometry {geometry!r} is not a draw kind.")
    return Gate(runner="declarative.gate.draw", kind="draw", param=param,
                geometry=geometry, prompt=prompt)


@dataclass(frozen=True, slots=True, init=False)
class When:
    """A branch the INTERPRETER decides, after the gates have run.

    The ONE conditional in the language. Its condition is a late-bound read -
    ``P.<param>``, ``D.<data>`` or ``Ref("step.field")`` - and the interpreter
    binds it against the CURRENT sheet at the moment the branch is reached, so an
    approved form-gate revision decides which body runs. A construction-time
    ``if`` could not: the plan value is built once, before any gate, and would
    freeze the branch against the pre-review sheet while the provenance claimed
    the approved one.

    A guarded body is also a SCOPE: a step named inside it is Ref-able only from
    inside it, because the branch may not fire.
    """

    condition: Any
    body: tuple[Any, ...]

    def __init__(self, condition: Any, *body: Any) -> None:
        if not isinstance(condition, (ParamRef, Ref)):
            raise PlanValidationError(
                f"When({condition!r}) is not a late-bound condition. A branch takes "
                "P.<param>, D.<data> or Ref('step.field') and the interpreter "
                "decides it after the gates; a concrete "
                f"{type(condition).__name__} would decide it while the plan value "
                "is being built, which is before anything the user could approve."
            )
        object.__setattr__(self, "condition", condition)
        object.__setattr__(self, "body", tuple(body))
        if not self.body:
            raise PlanValidationError("When(...) guards no steps.")

    @property
    def label(self) -> str:
        name = getattr(self.condition, "name", None) or getattr(self.condition, "path", "?")
        return f"when:{name}"


Node = Step | Gate | When


@dataclass(frozen=True, slots=True)
class Plan:
    """A workflow's step tree - a pure value the interpreter walks.

    Built ONCE, by the skeleton, from the template's ``plan(ops)`` declaration:
    the name and the engine are the workflow's, not something a template restates.
    Which steps RUN is the interpreter's answer, not the plan's - a ``When`` body
    is declared here and decided there.
    """

    name: str
    engine: str | None
    steps: tuple[Node, ...]

    def __post_init__(self) -> None:
        if not self.name:
            raise PlanValidationError("Plan declares no name.")
        object.__setattr__(self, "steps", tuple(self.steps))
        _flatten(self.steps)  # shape check at construction

    def declared(self) -> tuple[Step, ...]:
        """Every step, guarded ones included - what the validator and printer read."""
        return tuple(_flatten(self.steps))

    def describe(self) -> list[str]:
        lines = [f"{self.name} (engine={self.engine or '-'})"]
        for i, (step, guards) in enumerate(_flatten_guarded(self.steps, ()), 1):
            bits = [step.label]
            if step.stage:
                bits.append(f"[{step.stage}]")
            for guard in guards:
                bits.append(f"[{guard.label}]")
            if step.rebinds_domain:
                bits.append("[overrides domain]")
            for r in step.renders:
                bits.append(f"[render {r.preset}]")
            for c in step.charts:
                bits.append(f"[chart {c.name}]")
            lines.append(f"  {i}. {' '.join(bits)}")
        return lines


def _flatten(nodes: tuple[Any, ...]) -> list[Step]:
    return [step for step, _ in _flatten_guarded(nodes, ())]


def _flatten_guarded(nodes: tuple[Any, ...],
                     guards: tuple[When, ...]) -> list[tuple[Step, tuple[When, ...]]]:
    """Every step in declaration order, each with the ``When`` chain that guards it."""
    out: list[tuple[Step, tuple[When, ...]]] = []
    for node in nodes:
        if isinstance(node, When):
            out.extend(_flatten_guarded(node.body, guards + (node,)))
        elif isinstance(node, Step):
            out.append((node, guards))
        else:
            raise PlanValidationError(
                f"plan node {node!r} is not a Step, Gate or When."
            )
    return out
