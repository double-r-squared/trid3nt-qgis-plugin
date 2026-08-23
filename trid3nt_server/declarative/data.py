"""``Data`` - a declared ARTIFACT and its PRODUCER. Modifier legality is the rule
surface: a REFERENCE producer (fetch-fresh world data) simply has no ``.byo()``."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Mapping

from .errors import PlanValidationError

__all__ = [
    "AuthoredProducer",
    "Build",
    "CoversAOI",
    "Data",
    "DataDecl",
    "Fetch",
    "Producer",
    "ReferenceProducer",
]


class _CoversAOI:
    """Validator sentinel: a BYO artifact must cover the current domain."""

    def __repr__(self) -> str:
        return "CoversAOI"


CoversAOI = _CoversAOI()


@dataclass(frozen=True, slots=True)
class Producer:
    """How an artifact comes into being: a dotted runner path plus its declared args."""

    runner: str
    kwargs: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    ladder_rungs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.runner:
            raise PlanValidationError("Producer declares no runner path.")
        object.__setattr__(self, "kwargs", MappingProxyType(dict(self.kwargs)))

    def ladder(self, *rungs: str) -> "Producer":
        """Declare the fallback rungs this producer may degrade through, in order."""
        if not rungs:
            raise PlanValidationError(f"{self.runner}: .ladder() declares no rungs.")
        return replace(self, ladder_rungs=self.ladder_rungs + tuple(rungs))


@dataclass(frozen=True, slots=True)
class ReferenceProducer(Producer):
    """Canonical world data - fetched fresh for the domain. Deliberately NO ``.byo()``."""


@dataclass(frozen=True, slots=True)
class AuthoredProducer(Producer):
    """An artifact a user could have authored (mesh, network, deck, edited layer)."""

    byo_uri: str | None = None
    byo_validate: Any = None

    def byo(self, uri: str | None = None, *, validate: Any = CoversAOI) -> "AuthoredProducer":
        """Accept a user-supplied artifact instead of building one (coverage-validated)."""
        return replace(self, byo_uri=uri, byo_validate=validate)


class Fetch:
    """Reference-data producers: fetch-fresh for the domain, never BYO-able."""

    @staticmethod
    def tool(name: str, **kwargs: Any) -> ReferenceProducer:
        return ReferenceProducer(runner=name, kwargs=kwargs)


class Build:
    """Authored-artifact producers: BYO-able, coverage-validated at resolution."""

    @staticmethod
    def tool(name: str, **kwargs: Any) -> AuthoredProducer:
        return AuthoredProducer(runner=name, kwargs=kwargs)


@dataclass(frozen=True, slots=True)
class DataDecl:
    """A declared artifact: a name the plan Refs and the producer that satisfies it."""

    name: str
    producer: Producer

    def __post_init__(self) -> None:
        if not self.name or not self.name.isidentifier():
            raise PlanValidationError(f"Data name {self.name!r} is not an identifier.")

    @property
    def is_byo(self) -> bool:
        return getattr(self.producer, "byo_uri", None) is not None


def Data(name: str, producer: Producer) -> DataDecl:  # noqa: N802 - a value constructor
    """Declare an artifact. The producer's kind decides which modifiers are legal."""
    if not isinstance(producer, Producer):
        raise PlanValidationError(
            f"Data {name!r}: producer must be a Producer, got {type(producer).__name__}."
        )
    return DataDecl(name=name, producer=producer)
