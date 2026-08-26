"""``Data`` - a declared ARTIFACT and its PRODUCER. Modifier legality is the rule
surface: a REFERENCE producer (fetch-fresh world data) simply has no ``.byo()``.

``.resample()`` / ``.normalize()`` ride the declaration too: the cadence and the
units an artifact ARRIVES in are part of what it is, and declaring them is what
keeps consumer-side realignment from happening silently (see ``temporal.py``)."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Mapping

from .errors import PlanValidationError
from .temporal import TemporalSpec, spec_from

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
    temporal: TemporalSpec | None = None

    def __post_init__(self) -> None:
        if not self.runner:
            raise PlanValidationError("Producer declares no runner path.")
        object.__setattr__(self, "kwargs", MappingProxyType(dict(self.kwargs)))

    def ladder(self, *rungs: str) -> "Producer":
        """Declare the fallback rungs this producer may degrade through, in order."""
        if not rungs:
            raise PlanValidationError(f"{self.runner}: .ladder() declares no rungs.")
        return replace(self, ladder_rungs=self.ladder_rungs + tuple(rungs))

    def resample(self, *, to: str, method: str | None = None,
                 max_gap: str = "native*3") -> "Producer":
        """Declare the cadence this artifact is delivered at, and how it gets there.

        ``method`` unset takes the producer's quantity-class default (rates
        conservative, states linear, categorical nearest). A hole wider than
        ``max_gap`` refuses rather than being bridged.
        """
        return replace(self, temporal=spec_from(to, method, max_gap, None,
                                                self.temporal))

    def normalize(self, *, units: str) -> "Producer":
        """Declare the units this artifact is delivered in (explicit table, no guessing)."""
        return replace(self, temporal=spec_from(None, None, "native*3", units,
                                                self.temporal))


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
    """A declared artifact: a name the plan Refs, and what satisfies it.

    A PRODUCER-LESS declaration (``producer=None``) is a CONTEXT SLOT: the
    template names the artifact it can use and says nothing about where it comes
    from, because naming a default fetcher for a breakwater or a clip zone is an
    opinion the question does not carry. What satisfies it arrives from outside -
    a layer the user already has, a file URI, a gate's answer - or nothing does,
    and ``.optional()`` says that absence is legal.
    """

    name: str
    producer: Producer | None
    #: Absence is legal. Only meaningful on a producer-less slot; a declared
    #: producer either produces or fails.
    is_optional: bool = False
    #: How a supplied artifact is checked against the domain.
    byo_validate: Any = CoversAOI

    def __post_init__(self) -> None:
        if not self.name or not self.name.isidentifier():
            raise PlanValidationError(f"Data name {self.name!r} is not an identifier.")
        if self.is_optional and self.producer is not None:
            raise PlanValidationError(
                f"Data {self.name!r} declares a producer AND .optional(): a producer "
                "either produces the artifact or fails typed, so there is no absence "
                "for optional to describe. Drop the producer to make it a context "
                "slot, or drop .optional()."
            )

    @property
    def is_byo(self) -> bool:
        return getattr(self.producer, "byo_uri", None) is not None

    @property
    def producer_kwargs(self) -> Mapping[str, Any]:
        """The reads the producer declares - empty for a producer-less slot."""
        return {} if self.producer is None else self.producer.kwargs

    def optional(self) -> "DataDecl":
        """Absence is legal, and LABELLED: the run says the slot went unfilled."""
        return replace(self, is_optional=True)


def Data(name: str, producer: Producer | None = None) -> DataDecl:  # noqa: N802
    """Declare an artifact. The producer's kind decides which modifiers are legal.

    No producer declares a CONTEXT SLOT - see :class:`DataDecl`.
    """
    if producer is not None and not isinstance(producer, Producer):
        raise PlanValidationError(
            f"Data {name!r}: producer must be a Producer, got {type(producer).__name__}."
        )
    return DataDecl(name=name, producer=producer)
