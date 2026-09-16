"""Every declared Step, bound against the runner it names.

A plan is built once at import and its steps are not called until a run reaches
them, so a step whose kwargs the runner cannot take is a template that imports,
registers, validates - and dies at the author stage. Offline: the runners are
imported and their signatures read, and nothing here calls one.
"""

from __future__ import annotations

import inspect

import pytest

import trid3nt_server.main as _main
from trid3nt_server.workflows.runtime.interpreter import _load


def _templates() -> dict[str, object]:
    """Every registered engine template, by name -> its declared workflow."""
    _main._import_tools_registry()
    from trid3nt_server.tools import TOOL_REGISTRY

    return {name: entry.fn.workflow for name, entry in TOOL_REGISTRY.items()
            if getattr(entry.metadata, "tier", "general") == "template"
            and getattr(entry.fn, "workflow", None) is not None}


TEMPLATES = _templates()


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_every_step_binds_against_its_own_runners_signature(name):
    """The kwargs a step declares ARE the call: every required parameter of the
    runner is among them, and nothing it does not take is."""
    workflow = TEMPLATES[name]
    for step in workflow.plan.declared():
        signature = inspect.signature(_load(step.runner))
        try:
            signature.bind(**{key: None for key in step.kwargs})
        except TypeError as exc:
            pytest.fail(
                f"{name}: step {step.label!r} calls {step.runner} "
                f"({signature}) with {sorted(step.kwargs)} - {exc}")
