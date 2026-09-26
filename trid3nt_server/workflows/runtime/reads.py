"""Late-bound READS: a template names a value the run has not measured yet.

A read is bound when the run holds the value; a typo in one is an
``AttributeError`` at import. Nothing here executes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .errors import PlanValidationError

__all__ = ["DataRef", "ParamRef", "Ref", "Row", "body_rows", "declared_reads"]


def declared_reads(value: Any, kind: type) -> Iterable[Any]:
    """Every declared read of ``kind`` sitting inside a declared container."""
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
    """A read of a declared param, a declared Data, or a value the run measured.
    Dotted (``Ref("reach.seed")``) reads a field off what the root names."""

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
    """A DESCRIPTION of a read, and the operations that must not read it.
    Truthiness, ``str()`` and f-string interpolation all refuse - they are the three
    ways a description silently becomes data. ``repr`` stays live for diagnostics."""

    __slots__ = ()

    def _refuse(self, operation: str) -> "PlanValidationError":
        return PlanValidationError(
            f"{self!r} does not support {operation} at declaration time - it "
            "is a description of a late-bound read, not the value. Leave the read "
            "where it is and let the run bind it."
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
