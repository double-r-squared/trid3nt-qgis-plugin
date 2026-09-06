"""The plan validator - runs BEFORE any execution.

Ref integrity and modifier legality, as typed refusals.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from .data import DataDecl
from .errors import PlanValidationError
from .params import Param, refuse_duplicate_params
from .plan import DataRef, ParamRef, Plan, Ref, declared_reads

__all__ = ["validate_plan"]


def validate_plan(plan: Plan, params: Sequence[Param],
                  data: Sequence[DataDecl] = ()) -> None:
    """Refuse a plan that cannot possibly execute. Raises :class:`PlanValidationError`.

    The plan is STATIC - it reads no concrete value - so validation needs no sheet
    and runs at REGISTRATION, before any invocation exists.
    """
    refuse_duplicate_params(params)
    param_names = {p.name for p in params}
    data_names = {d.name for d in data}
    _check_duplicate_names(plan)
    _check_refs(plan, param_names, data_names)
    _check_param_refs(plan, param_names)
    _check_data_refs(data, param_names, data_names, plan)


def _check_duplicate_names(plan: Plan) -> None:
    seen: set[str] = set()
    for step in plan.declared():
        if step.name is None:
            continue
        if step.name in seen:
            raise PlanValidationError(
                f"plan {plan.name!r}: two steps are .named({step.name!r})."
            )
        seen.add(step.name)


def _check_refs(plan: Plan, param_names: set[str], data_names: set[str]) -> None:
    """Ref integrity, in DECLARATION ORDER.

    A step may read a param, a declared Data, or a step named BEFORE it: the
    sequence is what makes a read resolvable, so a ref to a step declared later is
    a runtime REF_UNRESOLVED waiting to happen rather than a valid plan.
    """
    visible: set[str] = set()
    for node in plan.steps:
        for ref in _walk_refs(dict(node.kwargs)):
            _resolve_root(plan.name, node.label, ref, param_names, data_names,
                          visible)
        if node.name is not None:
            visible.add(node.name)


def _check_param_refs(plan: Plan, param_names: set[str]) -> None:
    """A late-bound ``PARAMS.<name>`` read must name a param the workflow declares."""
    for step in plan.declared():
        for ref in _walk_param_refs(dict(step.kwargs)):
            if ref.name not in param_names:
                raise PlanValidationError(
                    param_name_refusal(ref, param_names,
                                       f"plan {plan.name!r} step {step.label!r}")
                )


def param_name_refusal(ref: ParamRef, param_names: set[str], where: str) -> str:
    """A ``ParamRef`` that names no declared param, said with the nearest spellings.

    A ref written as ``PARAMS.<name>`` cannot reach here - the body refuses the
    name at import - so what this catches is a ref BUILT from a string, where the
    candidate list runs to forty names and the nearest spellings are the whole
    value of the message.
    """
    import difflib

    close = difflib.get_close_matches(ref.name, sorted(param_names), n=3, cutoff=0.6)
    return (
        f"{where}: ParamRef({ref.name!r}) names no declared param"
        + (f". Closest declared: {', '.join(close)}." if close
           else f" (declared: {sorted(param_names)}).")
    )


def _check_data_refs(data: Sequence[DataDecl], param_names: set[str],
                     data_names: set[str], plan: Plan) -> None:
    """What a Data producer may read: a param, another Data, or a named step.

    The step case is what the interpreter has always resolved - ``_deref`` looks in
    the step results FIRST - and it is what a chained domain needs: the acquired
    AOI is a step result, so a producer that narrows the domain has to be able to
    name it. Demand-pull is what makes it sound: a Data is produced when a step
    that reads it runs, which is after the step it names.
    """
    named = {step.name for step in plan.declared() if step.name is not None}
    for decl in data:
        for ref in _walk_refs(dict(decl.producer_kwargs)):
            if (ref.root not in param_names and ref.root not in data_names
                    and ref.root not in named):
                raise PlanValidationError(
                    f"Data {decl.name!r} producer Refs {ref.path!r}, which is neither "
                    "a declared param, a declared Data, nor a step the plan names."
                )
        for pref in _walk_param_refs(dict(decl.producer_kwargs)):
            if pref.name not in param_names:
                raise PlanValidationError(
                    param_name_refusal(pref, param_names,
                                       f"Data {decl.name!r} producer")
                )


def _resolve_root(plan_name: str, step_label: str, ref: Ref, param_names: set[str],
                  data_names: set[str], available: set[str]) -> None:
    if ref.root in param_names or ref.root in data_names or ref.root in available:
        return
    if isinstance(ref, DataRef):
        raise PlanValidationError(
            f"plan {plan_name!r} step {step_label!r}: DataRef({ref.root!r}) names no "
            f"declared Data. Declared Data: {sorted(data_names)}."
        )
    raise PlanValidationError(
        f"plan {plan_name!r} step {step_label!r}: Ref({ref.path!r}) resolves to "
        "nothing - it is not a declared param, not a declared Data, and not a step "
        "named earlier in the sequence."
    )


def _walk_refs(value: Any) -> Iterable[Ref]:
    yield from declared_reads(value, Ref)


def _walk_param_refs(value: Any) -> Iterable[ParamRef]:
    yield from declared_reads(value, ParamRef)
