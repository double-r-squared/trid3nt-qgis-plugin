"""EVERY DOTTED RUNNER a TELEMAC plan carries, resolved by the one attribute
lookup the interpreter makes of it.

A runner is a string, so a module that moves leaves it pointing at nothing and
nothing says so until a run reaches that stage. This asks the same question of
every stage of every declared template at import time instead.
"""

from __future__ import annotations

from importlib import import_module


def _runners() -> dict[str, set[str]]:
    from trid3nt_server.tools import TOOL_REGISTRY

    found: dict[str, set[str]] = {}
    for name, row in TOOL_REGISTRY.items():
        workflow = getattr(row.fn, "workflow", None)
        if workflow is None or row.metadata.engine != "telemac":
            continue
        found[name] = {step.runner for step in workflow.plan.declared()
                       if getattr(step, "runner", None)}
    return found


def test_every_declared_telemac_template_states_at_least_one_runner():
    """The guard is worth nothing over an empty roster."""
    runners = _runners()
    assert runners, "no TELEMAC template is registered"
    assert all(found for found in runners.values())


def test_every_runner_of_every_telemac_plan_resolves_to_a_callable():
    unresolved = []
    for tool, runners in _runners().items():
        for runner in sorted(runners):
            module, _, attr = runner.rpartition(".")
            op = getattr(import_module(module), attr, None)
            if not callable(op):
                unresolved.append(f"{tool}: {runner}")
    assert unresolved == []


def test_a_measurement_the_module_does_not_carry_refuses_where_it_is_stated():
    """A template states the op name; the lookup is what says the name is wrong,
    and it says so at the template rather than at the stage."""
    import pytest

    from trid3nt_server.workflows.runtime import PlanValidationError
    from trid3nt_server.workflows.telemac.workflow import _signature

    with pytest.raises(PlanValidationError, match="carries no 'settle_nothing'"):
        _signature("trid3nt_server.workflows.telemac.authoring.opening"
                   ".settle_nothing")
