"""The plan VALUE: steps, refs, modifiers, charts. Nothing here executes.

A plan is STATIC, built once at registration and walked on every run; every read
is a late-bound description whose typo is an ``AttributeError`` at import.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Callable, Iterable, Literal, Mapping

from .errors import ModifierIllegalError, PlanValidationError

__all__ = [
    "ChartSpec",
    "DataRef",
    "ParamRef",
    "Plan",
    "Ref",
    "Row",
    "RawKeywords",
    "RunMode",
    "Step",
    "body_rows",
    "declared_reads",
]


def declared_reads(value: Any, kind: type) -> Iterable[Any]:
    """Every declared read of ``kind`` sitting inside a declared container.
    The ONE walk: the validator, the interpreter and the derivations all read a
    plan value through it and must agree about what it reads."""
    # Mapping, not dict: a deep-frozen binding block is a MappingProxyType, so a
    # walk that descended dicts alone would miss a ref hidden inside one. Sets and
    # frozensets are walked for the same reason.
    if isinstance(value, kind):
        yield value
    elif isinstance(value, Mapping):
        for v in value.values():
            yield from declared_reads(v, kind)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for v in value:
            yield from declared_reads(v, kind)


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
    Truthiness, ``str()`` and f-string interpolation all refuse - they are the three
    ways a description silently becomes data. ``repr`` stays live for diagnostics."""

    __slots__ = ()

    def _refuse(self, operation: str) -> "PlanValidationError":
        return PlanValidationError(
            f"{self!r} does not support {operation} at plan-construction time - it "
            "is a description of a late-bound read, not the value. Leave the ref in "
            "the plan and let the interpreter substitute it."
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
    Resolved against the CURRENT param state, so a form-gate revision reaches the
    run; truthiness, ``str``/``format``, equality and hashing all refuse."""

    name: str

    def __post_init__(self) -> None:
        if not self.name or not self.name.isidentifier():
            raise PlanValidationError(f"ParamRef({self.name!r}) has no identifier name.")

    def __bool__(self) -> bool:
        raise self._refuse("truth-value testing")

    def __eq__(self, _other: Any) -> bool:
        raise self._refuse("==/!= comparison")

    def __hash__(self) -> int:
        raise self._refuse("hashing (set/dict membership)")

    def __repr__(self) -> str:
        return f"ParamRef({self.name!r})"


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class DataRef(_Placeholder, Ref):
    """A late-bound read of a declared ``Data``: what ``DATA.<name>`` yields.
    Its own type so a bad name is reported against the Data body; a placeholder like
    :class:`ParamRef`, but equality and hashing stay :class:`Ref`'s."""

    def __repr__(self) -> str:
        return f"DataRef({self.path!r})"


class Row:
    """A row in a declaration class body: the ATTRIBUTE NAME is the row's name.
    ``__set_name__`` supplies the name and ``__get__`` yields the late-bound ref, so
    a misspelled row is an ``AttributeError`` at the line that wrote it."""

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
    The ONE read of a declaration body. A sequence passes through, so a body
    assembled in code is still a body."""
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


class _RawKeywords:
    def __repr__(self) -> str:
        return "RawKeywords"


#: Declared read of the run's RAW KEYWORD floor - the ``keywords={NAME: value}``
#: argument every wire carries. A step that fills a sheet takes
#: ``keywords=RawKeywords`` so the floor reaches it without becoming a Param:
#: it names the engine's own keywords, and what those mean is the dictionary's.
RawKeywords = _RawKeywords()


@dataclass(frozen=True, slots=True)
class ChartSpec:
    """A declared chart: the SPEC is the product; the plugin dock is the renderer.
    ``builder`` is the FUNCTION ITSELF, a ``(result, params) -> payload dict`` that
    owns its own encodings; a dotted string is refused."""

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
    #: Which stage of the run this step belongs to, as the card and the printer
    #: read it. A step that names none simply reads as unstaged.
    stage: str = ""

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

    def chart(self, name: str, *, builder: Callable[..., Any]) -> "Step":
        """Declare a chart SPEC built from this step's result by ``builder`` itself."""
        return replace(self, charts=self.charts + (ChartSpec(name=name, builder=builder),))


@dataclass(frozen=True, slots=True)
class Plan:
    """A workflow's step sequence - a pure value the interpreter walks.
    Built once, from the Door the template hands over; the name and the engine are
    the workflow's, not something a template restates."""

    name: str
    engine: str | None
    steps: tuple[Step, ...]

    def __post_init__(self) -> None:
        if not self.name:
            raise PlanValidationError("Plan declares no name.")
        steps = tuple(self.steps)
        for node in steps:
            if not isinstance(node, Step):
                raise PlanValidationError(f"plan node {node!r} is not a Step.")
        object.__setattr__(self, "steps", steps)

    def declared(self) -> tuple[Step, ...]:
        """Every step - what the validator and the printer read."""
        return self.steps

    def describe(self) -> list[str]:
        lines = [f"{self.name} (engine={self.engine or '-'})"]
        for i, step in enumerate(self.steps, 1):
            bits = [step.label]
            if step.stage:
                bits.append(f"[{step.stage}]")
            if step.rebinds_domain:
                bits.append("[overrides domain]")
            for c in step.charts:
                bits.append(f"[chart {c.name}]")
            lines.append(f"  {i}. {' '.join(bits)}")
        return lines
