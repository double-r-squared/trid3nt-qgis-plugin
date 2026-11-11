"""The run length is the module's one clock keyword: a refusal names it, a template's description names it, a release past it refuses."""

from __future__ import annotations

import asyncio

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.workflows.runtime.module import SlotRefused
from trid3nt_server.workflows.telemac.modules import wrapper_for
from trid3nt_server.workflows.telemac.modules.telemac2d import _sources


def test_until_refuses_naming_the_keyword_the_template_states_the_run_length_in():
    out = asyncio.run(TOOL_REGISTRY["telemac_dye_release"].fn(until=7200))
    said = out["error_message"]
    assert out["error_code"] == "INPUT_UNNAMED"
    assert "'until' is not an input of telemac_dye_release (none is close)." in said
    assert 'The run length is the engine keyword DURATION (s), stated by this template as 3600: keywords={"DURATION": ...}.' in said


def test_a_module_with_no_run_length_keyword_is_refused_as_before():
    from trid3nt_server.workflows.runtime.fill import _run_length_said

    class _Workflow:
        steering = wrapper_for("tomawac")

    assert wrapper_for("tomawac").run_length() is None
    assert _run_length_said(_Workflow()) == ""


def test_the_template_description_names_the_run_length_keyword():
    workflow = TOOL_REGISTRY["telemac_dye_release"].fn.workflow
    assert "The run length is DURATION (s), 3600 here" in workflow.sheet_doc()


def _value(window, until):
    return {"q": [8.0], "tracers": [100.0], "window_s": window, "settled": {"until_s": until}}


def test_a_release_longer_than_the_run_refuses_naming_both_and_the_keyword():
    with pytest.raises(SlotRefused) as raised:
        _sources(_value(7200.0, 3600.0))
    said = str(raised.value)
    assert "7200 s" in said and "3600 s" in said and "DURATION" in said


@pytest.mark.parametrize("window", [3600.0, 600.0, None])
def test_a_release_within_the_run_passes(window):
    assert _sources(_value(window, 3600.0))[0]["SOURCES_FILE"]
