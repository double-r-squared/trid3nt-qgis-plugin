"""Fallback ladders as DATA: the rung schema and the ladder registry.

A ladder is an ordered list of rungs a capability may descend when its declared
first choice cannot serve a request. Every rung names the ACTUAL alternative
(a registered source, a dotted-path callable, or an override of the primary
request) and the CONSEQUENCE of taking it. The terminal rung is always REFUSE.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, Mapping

__all__ = [
    "Consequence",
    "DEGRADATION_CLASSES",
    "Rung",
    "REFUSE",
    "Ladder",
    "register_ladder",
    "get_ladder",
    "registered_ladders",
]

#: What descending to a rung costs. ``primary`` is the declared first choice and
#: ``user_supplied`` the caller's own data -- neither is a degradation, so the
#: loudness floor ignores them. ``refuse`` belongs to the terminal rung alone.
Consequence = Literal[
    "primary", "user_supplied", "same_data", "cross_dataset", "synthetic", "refuse"
]

#: The classes the loudness floor keys on (rule 4 of the ladder contract).
DEGRADATION_CLASSES: frozenset[str] = frozenset(
    {"same_data", "cross_dataset", "synthetic"}
)

_EMPTY: Mapping[str, Any] = MappingProxyType({})


@dataclass(frozen=True)
class Rung:
    """One alternative on a ladder.

    ``describes`` is user-facing text: it is what the narration and the gate card
    say the alternative IS. Exactly one invocation form is declared: ``source``
    (a registered tool name), ``call`` (a ``module:function`` dotted path), or
    neither -- meaning "the primary request again, with ``params`` merged in",
    the form a composite source uses to switch one of its own legs on.
    ``supplies_param`` marks the user-supplied rung: the request param whose
    presence means the caller brought their own data.
    """

    name: str
    consequence: Consequence
    describes: str
    source: str | None = None
    call: str | None = None
    params: Mapping[str, Any] = field(default_factory=lambda: _EMPTY)
    supplies_param: str | None = None

    def __post_init__(self) -> None:
        if self.source and self.call:
            raise ValueError(
                f"rung {self.name!r} declares BOTH source and call; a rung has "
                "exactly one invocation form"
            )
        if self.consequence == "user_supplied" and not self.supplies_param:
            raise ValueError(
                f"rung {self.name!r} is user_supplied but names no supplies_param; "
                "the rung cannot tell whether the user provided anything"
            )


#: The terminal rung of EVERY ladder, stated explicitly rather than implied by
#: falling off the end: when no permitted rung can serve the request the
#: capability raises its typed error naming what is missing.
REFUSE = Rung(
    name="refuse",
    consequence="refuse",
    describes=(
        "refuse: no permitted rung can serve this request, so the capability "
        "raises its typed error naming the gap instead of inventing a value"
    ),
)


@dataclass(frozen=True)
class Ladder:
    """A capability's declared degradation path.

    ``rungs`` is ordered top-down: an optional ``user_supplied`` rung first, then
    exactly one ``primary``, then the alternatives a call site may permit by name
    through ``fallback=``. ``refuse_error_code`` is the typed code the terminal
    rung raises, so a refusal keeps the capability's own error vocabulary.
    """

    capability: str
    rungs: tuple[Rung, ...]
    refuse_error_code: str
    terminal: Rung = REFUSE

    def __post_init__(self) -> None:
        names = [r.name for r in self.rungs]
        if len(names) != len(set(names)):
            raise ValueError(f"ladder {self.capability!r} has duplicate rung names")
        primaries = [i for i, r in enumerate(self.rungs) if r.consequence == "primary"]
        if len(primaries) != 1:
            raise ValueError(
                f"ladder {self.capability!r} must declare exactly ONE primary rung"
            )
        users = [i for i, r in enumerate(self.rungs) if r.consequence == "user_supplied"]
        if users and users != [0]:
            raise ValueError(
                f"ladder {self.capability!r}: the user_supplied rung is the TOP rung "
                "-- user data wins over every derived rung"
            )
        if primaries[0] != len(users):
            raise ValueError(
                f"ladder {self.capability!r}: the primary rung must follow the "
                "user_supplied rung and precede every alternative"
            )
        for r in self.rungs[primaries[0] + 1:]:
            if r.consequence not in DEGRADATION_CLASSES:
                raise ValueError(
                    f"ladder {self.capability!r} rung {r.name!r}: an alternative "
                    f"must be {sorted(DEGRADATION_CLASSES)}, got {r.consequence!r}"
                )
        if self.terminal.consequence != "refuse":
            raise ValueError(f"ladder {self.capability!r}: terminal rung must REFUSE")

    @property
    def user_rung(self) -> Rung | None:
        return self.rungs[0] if self.rungs[0].consequence == "user_supplied" else None

    @property
    def primary_rung(self) -> Rung:
        return next(r for r in self.rungs if r.consequence == "primary")

    @property
    def alternatives(self) -> tuple[Rung, ...]:
        return tuple(r for r in self.rungs if r.consequence in DEGRADATION_CLASSES)

    def alternative(self, name: str) -> Rung | None:
        return next((r for r in self.alternatives if r.name == name), None)


_LADDERS: dict[str, Ladder] = {}


def register_ladder(ladder: Ladder) -> Ladder:
    """Register a capability's ladder (idempotent per capability name)."""
    existing = _LADDERS.get(ladder.capability)
    if existing is not None and existing != ladder:
        raise ValueError(
            f"a DIFFERENT ladder is already registered for {ladder.capability!r}"
        )
    _LADDERS[ladder.capability] = ladder
    return ladder


def get_ladder(capability: str) -> Ladder | None:
    """The ladder governing ``capability``, or None when none is declared."""
    return _LADDERS.get(capability)


def registered_ladders() -> dict[str, Ladder]:
    return dict(_LADDERS)
