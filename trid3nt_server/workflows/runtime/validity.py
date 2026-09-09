"""COUPLED VALIDITY: the rules a single ``Param`` declaration cannot express.

A rule is declared beside the params it reads and checked at resolve time on every
lane; the library owns the mechanism, the engine or the template owns the rule.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from .errors import DeclarativeError, PlanValidationError
from .params import ParamValues, ResolvedParams, wire_value

__all__ = ["CoupledValidityError", "Validity", "check_validity"]

logger = logging.getLogger("trid3nt_server.workflows.runtime.validity")


class CoupledValidityError(DeclarativeError):
    """A sheet whose values are each in range and jointly meaningless.
    A REFUSAL, never a warning: the sheet reads as ordinary while the run it
    describes is about something the caller did not ask for."""

    error_code = "COUPLED_VALIDITY_REFUSED"


@dataclass(frozen=True, slots=True)
class Validity:
    """One declared cross-param rule: what it reads, when it holds, what it says.
    ``holds`` is a predicate over the concrete sheet and ``message`` is formatted
    against ``reads``; a rule whose reads are not ALL seated is skipped."""

    name: str
    reads: tuple[str, ...]
    holds: Callable[[ParamValues], bool]
    message: str

    def __post_init__(self) -> None:
        if not self.name or not self.name.isidentifier():
            raise PlanValidationError(
                f"Validity name {self.name!r} is not an identifier.")
        if not self.reads:
            raise PlanValidationError(
                f"Validity {self.name!r} reads nothing; a coupled rule is ABOUT the "
                "params it couples, and the names are what let the refusal name them.")
        if not callable(self.holds):
            raise PlanValidationError(
                f"Validity {self.name!r}: holds {self.holds!r} is not callable.")
        if not self.message:
            raise PlanValidationError(
                f"Validity {self.name!r} declares no message; a refusal that cannot "
                "say what to change is a dead end.")


def refuse_undeclared_reads(rules: Sequence[Validity],
                            declared: Sequence[Any]) -> None:
    """A rule that reads a param the workflow does not declare, refused at import.
    It would otherwise be skipped forever, and a declaration claiming a guard that
    never fires is worse than no guard."""
    names = {getattr(p, "name", None) for p in declared}
    for rule in rules:
        missing = sorted(n for n in rule.reads if n not in names)
        if missing:
            raise PlanValidationError(
                f"Validity {rule.name!r} reads {missing}, which this workflow does "
                "not declare as params, so the rule could never fire.")


def check_validity(rules: Sequence[Validity], resolved: ResolvedParams, *,
                   workflow: str) -> None:
    """Refuse the first declared rule this sheet breaks. Runs on EVERY lane."""
    view = resolved.values_view()
    for rule in rules:
        values = {name: resolved.value_of(name) for name in rule.reads}
        if any(v is None for v in values.values()):
            continue
        if rule.holds(view):
            continue
        rendered = {k: wire_value(v) for k, v in values.items()}
        logger.info("%s refused by coupled-validity rule %s: %s",
                    workflow, rule.name, rendered)
        raise CoupledValidityError(
            f"{workflow}: {rule.message.format(**rendered)}")
