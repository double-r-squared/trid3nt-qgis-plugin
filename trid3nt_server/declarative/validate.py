"""The plan validator - runs BEFORE any execution.

Ref integrity, modifier legality and gate placement, all as typed refusals.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from .data import DataDecl
from .errors import PlanValidationError
from .params import Param, doors, refuse_duplicate_params
from .plan import Gate, ParamRef, Plan, Ref, When

__all__ = ["validate_plan"]


def validate_plan(plan: Plan, params: Sequence[Param],
                  data: Sequence[DataDecl] = (), *, sheet: Any = None) -> None:
    """Refuse a plan that cannot possibly execute. Raises :class:`PlanValidationError`.

    ``sheet`` is the resolved :class:`ResolvedParams` the plan was built from, when
    the caller has it. It carries which params the plan read as CONCRETE values,
    which is what makes the revisable-branch check (below) possible.
    """
    refuse_duplicate_params(params)
    param_names = {p.name for p in params}
    data_names = {d.name for d in data}
    _check_duplicate_names(plan)
    _check_gate_declarations(plan, {p.name: p for p in params})
    _check_revisable_branches(plan, {p.name: p for p in params}, sheet)
    _check_refs(plan, param_names, data_names)
    _check_param_refs(plan, param_names)
    _check_data_refs(data, param_names, data_names)


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


def _check_revisable_branches(plan: Plan, params: dict[str, Param], sheet: Any) -> None:
    """A FormGate plan may not branch on a value that gate can revise.

    ``When`` is decided when the plan VALUE is built, which is before any gate
    runs, so the branch is frozen against the pre-review sheet. If the user then
    revises the very param the branch was decided from, the plan shape cannot
    honor the revision - the run would take one branch while its provenance claims
    the other. That contradiction is refused at validation rather than executed.

    Revisable == any door but CONSTANT: a constant is not on the form as an
    editable value, so branching on one is stable across the review.
    """
    if sheet is None or not _declares_form_gate(plan) or not _has_when(plan.steps):
        return
    reads = getattr(sheet, "concrete_reads", None)
    revisable = sorted(
        name for name in (reads() if callable(reads) else ())
        if name in params and params[name].door != doors.CONSTANT
    )
    if not revisable:
        return
    raise PlanValidationError(
        f"plan {plan.name!r}: it declares a FormGate AND branches (When) on "
        + ", ".join(revisable)
        + " - values that gate can revise. A When is decided when the plan value is "
        "built, i.e. BEFORE the review, so an approved revision could not change "
        "which branch runs. Branch on a CONSTANT-door param, or drop the FormGate "
        "and let the step that owns the decision review its own inputs."
    )


def _declares_form_gate(plan: Plan) -> bool:
    return any(isinstance(s, Gate) and s.kind == "form" for s in plan.declared())


def _has_when(nodes: tuple[Any, ...]) -> bool:
    return any(isinstance(n, When) for n in nodes)


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
    """A late-bound ``p.<name>`` read must name a param the workflow declares."""
    for step in plan.declared():
        for ref in _walk_param_refs(dict(step.kwargs)):
            if ref.name not in param_names:
                raise PlanValidationError(
                    f"plan {plan.name!r} step {step.label!r}: ParamRef({ref.name!r}) "
                    "is not a declared param."
                )


def _check_data_refs(data: Sequence[DataDecl], param_names: set[str],
                     data_names: set[str]) -> None:
    for decl in data:
        for ref in _walk_refs(dict(decl.producer.kwargs)):
            if ref.root not in param_names and ref.root not in data_names:
                raise PlanValidationError(
                    f"Data {decl.name!r} producer Refs {ref.path!r}, which is neither "
                    "a declared param nor a declared Data."
                )
        for pref in _walk_param_refs(dict(decl.producer.kwargs)):
            if pref.name not in param_names:
                raise PlanValidationError(
                    f"Data {decl.name!r} producer reads ParamRef({pref.name!r}), "
                    "which is not a declared param."
                )


def _resolve_root(plan_name: str, step_label: str, ref: Ref, param_names: set[str],
                  data_names: set[str], available: set[str]) -> None:
    if ref.root in param_names or ref.root in data_names or ref.root in available:
        return
    raise PlanValidationError(
        f"plan {plan_name!r} step {step_label!r}: Ref({ref.path!r}) resolves to "
        "nothing - it is not a declared param, not a declared Data, and not a step "
        "named earlier on this branch."
    )


def _walk_refs(value: Any) -> Iterable[Ref]:
    yield from _walk(value, Ref)


def _walk_param_refs(value: Any) -> Iterable[ParamRef]:
    yield from _walk(value, ParamRef)


def _walk(value: Any, kind: type) -> Iterable[Any]:
    if isinstance(value, kind):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _walk(v, kind)
    elif isinstance(value, (list, tuple, set)):
        for v in value:
            yield from _walk(v, kind)
