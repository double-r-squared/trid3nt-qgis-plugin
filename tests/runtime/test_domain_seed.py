"""A domain found from a point: with no point stated it refuses naming the need, and is never guessed."""

from __future__ import annotations

import asyncio
import dataclasses

import pytest

from trid3nt_server.inputs.input import Input
from trid3nt_server.inputs.slots import DOMAIN
from trid3nt_server.workflows.runtime import fill
from trid3nt_server.workflows.runtime.errors import StepFailedError


def _domain():
    return dataclasses.replace(Input(DOMAIN).need("hydrography", at="seed"), name="domain")


def test_a_domain_the_match_must_locate_with_no_seed_refuses_naming_it():
    env = fill._Env(data={}, run={"seed": None})
    with pytest.raises(StepFailedError) as caught:
        asyncio.run(fill._matched(env, _domain()))
    assert caught.value.error_code == "DATA_NEED_UNPLACED"
    assert "'seed'" in str(caught.value) and "draw or supply domain" in str(caught.value)


def test_with_the_seed_stated_the_match_is_asked(monkeypatch):
    asked = []

    async def _somewhere(env):
        asked.append("asked")
        raise RuntimeError("stop here: the match was reached")

    monkeypatch.setattr(fill, "_somewhere_to_ask", _somewhere)
    env = fill._Env(data={}, run={"seed": (-122.65, 45.53)})
    with pytest.raises(RuntimeError, match="the match was reached"):
        asyncio.run(fill._matched(env, _domain()))
    assert asked == ["asked"]


@pytest.mark.parametrize("tool", [
    "telemac_water_temperature", "telemac_ice_cover", "telemac3d_stratified_flow",
    "telemac_eutrophication", "telemac_channel_dredging"])
def test_a_pure_seed_is_stated_never_required(tool):
    from trid3nt_server.tools import TOOL_REGISTRY

    workflow = TOOL_REGISTRY[tool].fn.workflow
    name = "seed_point" if tool == "telemac_channel_dredging" else "seed"
    row = workflow.input(name)
    assert row.stated_only and not row.required
