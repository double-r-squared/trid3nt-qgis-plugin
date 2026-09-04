"""The plan VALUE: steps, gates, refs, modifiers, charts, the stage sequence.

Nothing here executes. A template's ``plan(ops)`` returns the step sequence and
the SKELETON names and engines the :class:`Plan`. The plan is STATIC: it reads no
concrete value, so it is built ONCE - at registration - and the interpreter walks
the same value on every run. Every read is a late-bound ``PARAMS.<param>`` /
``DATA.<data>`` / ``Ref("step.field")`` description, and every conditional is a
:class:`When` the interpreter decides AFTER the gates have run.

A read is attribute access on the template's OWN declaration body (:class:`Row`),
so a misspelled name is an ``AttributeError`` at the import line that wrote it and
a name from another template's sheet is unwritable.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Callable, Iterable, Literal, Mapping

from .errors import ModifierIllegalError, PlanValidationError

__all__ = [
    "ChartSpec",
    "DataRef",
    "DrawGate",
    "FormGate",
    "Gate",
    "Node",
    "ParamRef",
    "Plan",
    "Ref",
    "Row",
    "RunMode",
    "STAGES",
    "Step",
    "When",
    "body_rows",
    "declared_reads",
]


def declared_reads(value: Any, kind: type) -> Iterable[Any]:
    """Every declared read of ``kind`` sitting inside a declared container.

    The ONE walk. The validator checks these reads resolve, the interpreter binds
    them and evicts on them, and a derivation decides from them which steps its
    overrides reach - three readers that must agree about what a plan value reads.

    ``Mapping`` rather than ``dict``: a binding block is deep-frozen into
    ``MappingProxyType``, which is a Mapping and not a dict, so a walk that
    descended dicts alone would call a ref hidden in a declared block invisible.
    Sets and frozensets are walked for the same reason.
    """
    if isinstance(value, kind):
        yield value
    elif isinstance(value, Mapping):
        for v in value.values():
            yield from declared_reads(v, kind)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for v in value:
            yield from declared_reads(v, kind)


#: The universal stage sequence the skeleton walks. A step names the stage it
#: belongs to so the plan reads as the sequence rather than as a list of runners;
#: the facade's four operations and the mesh declaration are what stamp it.
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


class _Placeholder:
    """A plan-time DESCRIPTION of a read, and the operations that must not read it.

    Truthiness, ``str()`` and f-string interpolation are the three ways a
    placeholder silently becomes data: a construction-time ``if`` decides a branch
    against a description, and an f-string bakes ``ParamRef('x')`` into a layer
    title a user reads. Every placeholder refuses all three the same way, because
    the leak is the same leak whichever namespace the ref came from. ``repr`` stays
    live: naming the ref is what a diagnostic is for.
    """

    __slots__ = ()

    def _refuse(self, operation: str) -> "PlanValidationError":
        return PlanValidationError(
            f"{self!r} does not support {operation} at plan-construction time - it "
            "is a description of a late-bound read, not the value. Leave the ref in "
            "the plan and let the interpreter substitute it; a conditional is a When "
            "the interpreter decides."
        )

    def __bool__(self) -> bool:
        raise self._refuse("truth-value testing")

    def __str__(self) -> str:
        raise self._refuse("str()")

    def __format__(self, _spec: str) -> str:
        raise self._refuse("f-string / format() interpolation")


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class ParamRef(_Placeholder):
    """A LATE-BOUND read of a declared param: what ``PARAMS.<name>`` yields.

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

    def __post_init__(self) -> None:
        if not self.name or not self.name.isidentifier():
            raise PlanValidationError(f"ParamRef({self.name!r}) has no identifier name.")

    def __bool__(self) -> bool:
        raise PlanValidationError(
            f"ParamRef({self.name!r}) has no truth value at plan-construction time - "
            "it is a description, not the value. A branch on it is "
            f"When(PARAMS.{self.name}, ...), which the interpreter decides after "
            "the gates."
        )

    def __eq__(self, _other: Any) -> bool:
        raise self._refuse("==/!= comparison")

    def __hash__(self) -> int:
        raise self._refuse("hashing (set/dict membership)")

    def __repr__(self) -> str:
        return f"ParamRef({self.name!r})"


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class DataRef(_Placeholder, Ref):
    """A late-bound read of a declared ``Data``: what ``DATA.<name>`` yields.

    A :class:`Ref` so the interpreter dereferences it with everything else, and its
    own type so the registration check can say WHICH body a bad name came from -
    ``DATA.terain`` is a Data typo, not a step nobody named.

    A PLACEHOLDER like ``ParamRef``, and it refuses the same reads: the artifact a
    ``DATA.<name>`` describes does not exist until the interpreter produces it, so
    an f-string over one puts ``DataRef('mesh')`` in front of a user and a
    construction-time ``if`` branches on a description. Equality and hashing stay
    :class:`Ref`'s: a Data name is compared and keyed by path all through
    registration.
    """

    def __repr__(self) -> str:
        return f"DataRef({self.path!r})"


class Row:
    """A row in a declaration class body: the ATTRIBUTE NAME is the row's name.

    ONE descriptor behind both bodies. ``__set_name__`` is how the name arrives, so
    a template never writes it twice, and ``__get__`` makes ``PARAMS.<row>`` /
    ``DATA.<row>`` the late-bound ref every binding block and plan step already
    speaks. Reading the body's own attribute is therefore checked by Python at
    import: a misspelled row is an ``AttributeError`` at the line that wrote it,
    and a name the template does not declare cannot be written at all.
    """

    __slots__ = ()

    #: Which field on the concrete row type holds the declared name.
    _row_attr = "row"
    #: Which ref ``__get__`` yields - the row's own late-bound description.
    _ref_type: type = Ref

    def __set_name__(self, owner: type, name: str) -> None:
        declared = getattr(self, self._row_attr, "")
        if declared and declared != name:
            raise PlanValidationError(
                f"row {declared!r} is bound to a second name {name!r}: a row is one "
                "declaration in one body. Write a fresh declaration for the second "
                "row.")
        object.__setattr__(self, self._row_attr, name)

    def __get__(self, obj: Any, owner: type | None = None) -> Any:
        name = getattr(self, self._row_attr, "")
        if not name:
            raise PlanValidationError(
                f"{self!r} was read as a row reference but carries no row name; a "
                "reference is attribute access on the body that declares it.")
        return self._ref_type(name)


def body_rows(body: Any, kind: type | tuple[type, ...]) -> tuple[Any, ...]:
    """The declared rows of a class body, in CLASS-BODY ORDER.

    The ONE read of a declaration body - the param sheet, the data chain and the
    registration factory all walk it, and order is the declaration's own because a
    ladder and a chain both read down the body. A sequence passes through, so a
    body assembled in code is still a body.
    """
    if isinstance(body, (list, tuple)):
        return tuple(body)
    return tuple(v for v in vars(body).values() if isinstance(v, kind))


class _RunMode:
    def __repr__(self) -> str:
        return "RunMode"


#: Declared read of the run's input-gate mode. A composite step that runs its OWN
#: input-review gate takes ``input_mode=RunMode`` so the lever reaches it without
#: becoming a Param - it governs whether the run pauses, it is not a physical value.
RunMode = _RunMode()


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
    charts: tuple[ChartSpec, ...] = ()
    #: Which universal stage this step belongs to (see ``STAGES``). Stamped by the
    #: engine facade's four operations; a step a template declares directly leaves
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
    ``PARAMS.<param>``, ``DATA.<data>`` or ``Ref("step.field")`` - and the interpreter
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
                "PARAMS.<param>, DATA.<data> or Ref('step.field') and the interpreter "
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
