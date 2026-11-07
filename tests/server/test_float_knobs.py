"""The ambiguity margin is a setting: zero turns the ask off and a value outside its range refuses by name.
A wait is a setting and refuses a value outside its range by name."""
from __future__ import annotations

import pytest

from trid3nt_server.inputs.gate.confirm import _code_exec_approval_timeout_s
from trid3nt_server.workflows.runtime.module import SlotRefused
from trid3nt_server.server.config import _ambiguity_margin_threshold, _tool_choice_timeout_s
from trid3nt_server.server.processing import _timeout_s

SETTING_WAITS = [
    ("CODE_EXEC_APPROVAL_TIMEOUT_S", _code_exec_approval_timeout_s, 180.0),
    ("TOOL_CHOICE_TIMEOUT_S", _tool_choice_timeout_s, 45.0),
    ("PROCESSING_TIMEOUT_S", _timeout_s, 600.0),
]


def test_ambiguity_margin_zero_turns_the_ask_off(user_settings):
    assert _ambiguity_margin_threshold() == 0.01
    user_settings(AMBIGUITY_MARGIN=0)
    assert _ambiguity_margin_threshold() == 0.0
    user_settings(AMBIGUITY_MARGIN=-1)
    with pytest.raises(SlotRefused):
        _ambiguity_margin_threshold()


@pytest.mark.parametrize("name,read,default", SETTING_WAITS)
def test_a_setting_wait_reads_the_user_file_and_refuses_outside_its_range(user_settings, name, read, default):
    assert read() == default
    user_settings(**{name: 7.5})
    assert read() == 7.5
    user_settings(**{name: 0})
    with pytest.raises(SlotRefused):
        read()
