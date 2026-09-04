"""The plan validator - runs BEFORE any execution.

Ref integrity, modifier legality and gate placement, all as typed refusals.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from .data import DataDecl
from .errors import PlanValidationError
from .params import Param, doors, refuse_duplicate_params
from .plan import DataRef, Gate, ParamRef, Plan, Ref, When, declared_reads

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
    _check_gate_declarations(plan, {p.name: p for p in params})
    _check_when_conditions(plan, param_names, data_names)
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


def _check_gate_declarations(plan: Plan, params: dict[str, Param]) -> None:
    form_gates = 0
    consequential_seen: str | None = None
    self_gating = next((s.label for s in plan.declared()
                        if not isinstance(s, Gate) and s.self_gating), None)
    for step in plan.declared():
        if isinstance(step, Gate):
            if step.kind == "form" and self_gating is not None:
                raise PlanValidationError(
                    f"plan {plan.name!r}: step {self_gating!r} reviews its own inputs, "
                    "so the plan must not declare a FormGate in front of it - the "
                    "composite's own review IS the review, and a second card's edits "
                    "would land on a sheet the composite never reads."
                )
            if consequential_seen is not None:
                raise PlanValidationError(
                    f"plan {plan.name!r}: gate {step.label!r} is placed AFTER the "
                    f"consequential step {consequential_seen!r} - a gate that cannot "
                    "change the run is a dead gate."
                )
            if step.kind == "form":
                form_gates += 1
                if form_gates > 1:
                    raise PlanValidationError(
                        f"plan {plan.name!r}: more than one FormGate; the param sheet "
                        "is reviewed once."
                    )
            else:
                target = params.get(step.param or "")
                if target is None:
                    raise PlanValidationError(
                        f"plan {plan.name!r}: DrawGate names undeclared param "
                        f"{step.param!r}."
                    )
                if target.door != doors.USER:
                    raise PlanValidationError(
                        f"plan {plan.name!r}: DrawGate param {target.name!r} is "
                        f"door={target.door}; a drawn value comes through the USER door."
                    )
        elif step.consequential and consequential_seen is None:
            consequential_seen = step.label

    declared = plan.declared()
    if declared and isinstance(declared[-1], Gate):
        raise PlanValidationError(
            f"plan {plan.name!r}: gate {declared[-1].label!r} is the LAST node of the "
            "plan - nothing runs after it, so there is nothing its answer could "
            "change. A gate that cannot change the run is a dead gate."
        )


def _check_when_conditions(plan: Plan, param_names: set[str],
                           data_names: set[str]) -> None:
    """Every branch condition must be a read that RESOLVES when the branch is reached.

    A ``When`` is decided by the interpreter, after the gates - so a form gate
    revising the very value a branch reads is the point, not a contradiction. What
    is still refusable is a condition that names nothing: an undeclared param, an
    undeclared Data, or a step that is not visible on this branch (declared later,
    or named inside a sibling branch that may not fire).
    """
    _check_when_scope(plan.name, plan.steps, param_names, data_names, set())


def _check_when_scope(plan_name: str, nodes: tuple[Any, ...], param_names: set[str],
                      data_names: set[str], visible: set[str]) -> None:
    local = set(visible)
    for node in nodes:
        if isinstance(node, When):
            cond = node.condition
            if isinstance(cond, ParamRef):
                if cond.name not in param_names:
                    raise PlanValidationError(
                        f"plan {plan_name!r}: When branches on ParamRef({cond.name!r}), "
                        "which is not a declared param."
                    )
            else:
                _resolve_root(plan_name, node.label, cond, param_names, data_names,
                              local)
            _check_when_scope(plan_name, node.body, param_names, data_names, local)
            continue
        if node.name is not None:
            local.add(node.name)


def _check_refs(plan: Plan, param_names: set[str], data_names: set[str]) -> None:
    _check_refs_in_scope(plan.name, plan.steps, param_names, data_names, set())


def _check_refs_in_scope(plan_name: str, nodes: tuple[Any, ...], param_names: set[str],
                         data_names: set[str], visible: set[str]) -> None:
    """Ref integrity with BRANCH SCOPING.

    A step named inside a ``When`` body is only visible inside that body: the
    branch may not be taken, so a Ref to it from outside is a runtime
    REF_UNRESOLVED waiting to happen, not a valid plan.
    """
    local = set(visible)
    for node in nodes:
        if isinstance(node, When):
            _check_refs_in_scope(plan_name, node.body, param_names, data_names, local)
            continue
        for ref in _walk_refs(dict(node.kwargs)):
            _resolve_root(plan_name, node.label, ref, param_names, data_names, local)
        if node.name is not None:
            local.add(node.name)


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
        "named earlier on this branch."
    )


def _walk_refs(value: Any) -> Iterable[Ref]:
    yield from declared_reads(value, Ref)


def _walk_param_refs(value: Any) -> Iterable[ParamRef]:
    yield from declared_reads(value, ParamRef)
