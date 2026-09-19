"""WHICH QUESTIONS OPEN AS A CHANNEL: the ones whose runs always carry a flow.

A discharge row a run may arrive without is not a channel, and a plan cannot
author one on a maybe - a tidal mouth opens on its level boundaries, which is
the open-water base.
"""

from __future__ import annotations


def _steps(tool: str) -> dict[str, object]:
    from trid3nt_server.tools import TOOL_REGISTRY

    plan = TOOL_REGISTRY[tool].fn.workflow.plan
    return {step.label: step for step in plan.declared()}


def test_a_question_whose_runs_always_carry_a_flow_opens_as_a_channel():
    steps = _steps("telemac_sediment_plume")
    assert "channel" in steps
    assert steps["settled"].kwargs["level"].root == "channel"


def test_a_question_whose_discharge_may_be_absent_opens_on_its_levels():
    steps = _steps("telemac_dye_release")
    assert "channel" not in steps
    assert steps["settled"].kwargs["level"].root == "stage"
