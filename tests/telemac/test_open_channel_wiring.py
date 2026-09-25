"""WHICH QUESTIONS OPEN AS A CHANNEL: the ones that declare a discharge at all.

Whether a run CARRIES a flow is a run-time fact the match settles, so the plan
lists the stage and the author decides off the carrier it is handed: an absent
one opens no channel and hands back the level, which is the open-water base.
"""

from __future__ import annotations

import asyncio


def _steps(tool: str) -> dict[str, object]:
    from trid3nt_server.tools import TOOL_REGISTRY

    plan = TOOL_REGISTRY[tool].fn.workflow.plan
    return {step.label: step for step in plan.declared()}


def test_a_question_whose_runs_always_carry_a_flow_opens_as_a_channel():
    steps = _steps("telemac_sediment_plume")
    assert "channel" in steps
    assert steps["settled"].kwargs["level"].root == "channel"


def test_a_question_whose_discharge_may_be_absent_still_lists_the_channel():
    steps = _steps("telemac_dye_release")
    assert "channel" in steps
    assert steps["channel"].kwargs["carrier"].root == "discharge"
    assert steps["settled"].kwargs["level"].root == "channel"


def test_an_absent_carrier_authors_no_channel_and_hands_back_the_level():
    from trid3nt_server.workflows.telemac.authoring.opening import open_channel

    stage = object()
    opened = asyncio.run(open_channel(mesh={}, files={}, friction_law=4,
                                      friction_coefficient=0.03,
                                      carrier=None, stage=stage))
    assert opened is stage
