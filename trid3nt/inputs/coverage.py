"""COVERAGE as data: the steps an input declares to answer what its data does not cover, and what is left short.

A step is a name and its arguments, the name of the derive that runs it; a shortfall is a code, counts and its
remedies, each a step. Whether an unresolved shortfall refuses is one setting, ``strict``: off a shortfall is a
warning and the run goes on where the engine can, on it refuses naming its code and remedies. A value the engine
cannot run without at all refuses whatever ``strict`` says, where it is read.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Mapping, Sequence

from trid3nt.errors import DeclarationError

__all__ = ["ACCEPT", "Coverage", "INTERPOLATE", "Interpolation", "MERGE", "STEPS", "Shortfall", "Step", "accepted",
           "bind_reports", "drain_reports", "interpolation", "is_strict", "named", "refuses", "register_strict",
           "report", "reported", "step", "steps", "strictness"]

#: The steps an input may declare: the two derives by their tool names, and the line that accepts a shortfall's code.
MERGE = "merge_surfaces"
INTERPOLATE = "interpolate_surface"
ACCEPT = "accept"
#: The arguments each step takes; one it does not take refuses where the step is written.
STEPS: Mapping[str, frozenset[str]] = {
    MERGE: frozenset({"source"}),
    INTERPOLATE: frozenset({"algorithm", "max_distance"}),
    ACCEPT: frozenset({"code"}),
}


@dataclass(frozen=True)
class Step:
    """One declared step: its name and its arguments, as ``(name, value)`` pairs in name order."""

    name: str
    args: tuple[tuple[str, Any], ...] = ()

    def arg(self, name: str) -> Any:
        return dict(self.args).get(name)

    def __repr__(self) -> str:
        return f"{self.name}({', '.join(f'{k}={v!r}' for k, v in self.args)})"


def step(name: str, **args: Any) -> Step:
    """The step ``name`` with ``args``; a name or an argument no step takes refuses, and so does an accept naming
    no code."""
    if name not in STEPS:
        raise DeclarationError(f"{name!r} is not a step; the steps are {sorted(STEPS)}.")
    unknown = sorted(set(args) - STEPS[name])
    if unknown:
        raise DeclarationError(f"the step {name} takes {sorted(STEPS[name])}, not {unknown}.")
    if name == ACCEPT and not str(args.get("code") or "").strip():
        raise DeclarationError("the step accept names the code of the shortfall it accepts.")
    if name == INTERPOLATE:
        _checked(args)
    return Step(name, tuple(sorted((k, v) for k, v in args.items() if v is not None)))


def _checked(args: Mapping[str, Any]) -> None:
    """An interpolation's algorithm and reach held to the settings' own entries for them."""
    from trid3nt.settings.reader import entry, typed

    for arg, name in (("algorithm", "FILL_ALGORITHM"), ("max_distance", "FILL_MAX_DISTANCE")):
        if args.get(arg) is not None:
            try:
                typed(entry(name), args[arg])
            except Exception as refused:  # noqa: BLE001 - the entry's own refusal names the value
                raise DeclarationError(f"the step {INTERPOLATE} {arg}: {refused}") from None


def steps(declared: Sequence[Any]) -> tuple[Step, ...]:
    """Steps as written - a step, its name, or ``(name, {args})`` - each checked by :func:`step`."""
    out = []
    for item in declared or ():
        if isinstance(item, Step):
            out.append(step(item.name, **dict(item.args)))
        elif isinstance(item, str):
            out.append(step(item))
        elif isinstance(item, Mapping) and "step" in item:
            out.append(step(str(item["step"]), **dict(item.get("args") or {})))
        elif isinstance(item, (list, tuple)) and len(item) == 2 and isinstance(item[1], Mapping):
            out.append(step(str(item[0]), **dict(item[1])))
        else:
            raise DeclarationError(f"{item!r} is not a step: write a step's name, or (name, {{args}}).")
    return tuple(out)


def named(declared: Sequence[Step], name: str) -> Step | None:
    """The first step of ``declared`` called ``name``, or ``None``."""
    return next((s for s in declared if s.name == name), None)


@dataclass(frozen=True)
class Interpolation:
    """The interpolation a run declared: its algorithm and its reach in pixels, each unstated one the setting in force,
    read when it is first asked for."""

    stated_algorithm: str | None = None
    stated_max_distance: float | None = None

    @property
    def algorithm(self) -> str:
        from trid3nt.settings import setting

        return str(setting("FILL_ALGORITHM") if self.stated_algorithm is None else self.stated_algorithm)

    @property
    def max_distance(self) -> float:
        from trid3nt.settings import setting

        return float(setting("FILL_MAX_DISTANCE") if self.stated_max_distance is None else self.stated_max_distance)


def interpolation(declared: Sequence[Step] | None) -> Interpolation | None:
    """The interpolation ``declared`` states, or ``None`` where it states none. ``None`` steps is a caller outside a
    fill: the settings' own."""
    found = Step(INTERPOLATE) if declared is None else named(declared, INTERPOLATE)
    if found is None:
        return None
    return Interpolation(found.arg("algorithm"), found.arg("max_distance"))


def accepted(declared: Sequence[Step] | None, code: str) -> bool:
    """Does ``declared`` carry the accept line naming ``code``?"""
    return any(s.name == ACCEPT and s.arg("code") == code for s in declared or ())


@dataclass(frozen=True)
class Shortfall:
    """What an input's data leaves short after its declared steps: the code, the input, the counts (``cells`` and
    the ``share`` of the cut they are), the distance in pixels from the farthest of them to a valued cell, and the
    remedies, each a step; ``accepted`` where an accept line names the code."""

    code: str
    input: str
    cells: int
    share: float
    farthest_px: int
    remedies: tuple[Step, ...] = ()
    accepted: bool = False


@dataclass(frozen=True)
class Coverage:
    """What one input's data covers: the coverage lines its ingestion said, the share each source valued (``filled``
    for an interpolated cell), the share nothing valued, the steps declared for it, and its shortfall, if any."""

    input: str
    text: tuple[str, ...] = ()
    shares: tuple[tuple[str, float], ...] = ()
    unmeasured: float = 0.0
    steps: tuple[Step, ...] = ()
    shortfall: Shortfall | None = None


#: Whether an unresolved shortfall refuses where nothing binds it: off in the SDK; a host registers its own.
_REGISTERED = {"strict": False}
_STRICT: contextvars.ContextVar[bool | None] = contextvars.ContextVar("trid3nt_strict", default=None)


def register_strict(on: bool) -> None:
    """Install whether an unresolved shortfall refuses where no caller binds it; a later one replaces it."""
    _REGISTERED["strict"] = bool(on)


@contextmanager
def strictness(on: bool) -> Iterator[None]:
    """Hold this task to ``on``: an unresolved shortfall refuses where it is True, warns where it is False."""
    token = _STRICT.set(bool(on))
    try:
        yield
    finally:
        _STRICT.reset(token)


def is_strict() -> bool:
    """The strictness in force: the task's own, else the registered one."""
    bound = _STRICT.get()
    return _REGISTERED["strict"] if bound is None else bound


def refuses(shortfall: Shortfall) -> bool:
    """Does this shortfall refuse: unaccepted, under strict."""
    return not shortfall.accepted and is_strict()


#: What each input's ingestion reported of its coverage in the fill in progress, the last report by input.
_REPORTS: contextvars.ContextVar[dict[str, Coverage] | None] = contextvars.ContextVar(
    "trid3nt_coverage_reports", default=None)


def report(coverage: Coverage) -> None:
    """Hold ``coverage`` as its input's on the fill in progress; outside one it is dropped."""
    held = _REPORTS.get()
    if held is not None:
        held[coverage.input] = coverage


def reported(name: str) -> Coverage | None:
    """What the fill in progress holds of one input's coverage."""
    return (_REPORTS.get() or {}).get(name)


def bind_reports() -> contextvars.Token:
    """Open the coverage reports of one fill -> the token that closes them."""
    return _REPORTS.set({})


def drain_reports(token: contextvars.Token) -> dict[str, Coverage]:
    """Close the reports and return them by input."""
    held = dict(_REPORTS.get() or {})
    _REPORTS.reset(token)
    return held
