"""The match margins are settings: a value outside the range refuses by name.
A wait is a setting and refuses a value outside its range by name."""
from __future__ import annotations

import pytest

from trid3nt_server.inputs.gate.confirm import _code_exec_approval_timeout_s
from trid3nt_server.workflows.runtime.module import SlotRefused
from trid3nt_server.server.config import _tool_choice_timeout_s
from trid3nt_server.settings import setting
from trid3nt_server.server.processing import _timeout_s

SETTING_WAITS = [
    ("CODE_EXEC_APPROVAL_TIMEOUT_S", _code_exec_approval_timeout_s, 180.0),
    ("TOOL_CHOICE_TIMEOUT_S", _tool_choice_timeout_s, 45.0),
    ("PROCESSING_TIMEOUT_S", _timeout_s, 600.0),
]


@pytest.mark.parametrize("name", ["MATCH_MARGIN", "SOURCE_MARGIN", "MIN_MATCH_SIMILARITY"])
def test_a_match_margin_reads_the_user_file_and_refuses_outside_its_range(user_settings, name):
    user_settings(**{name: 0})
    assert setting(name) == 0.0
    user_settings(**{name: -1})
    with pytest.raises(SlotRefused):
        setting(name)
    user_settings(**{name: 1.5})
    with pytest.raises(SlotRefused):
        setting(name)


@pytest.mark.parametrize("name,read,default", SETTING_WAITS)
def test_a_setting_wait_reads_the_user_file_and_refuses_outside_its_range(user_settings, name, read, default):
    assert read() == default
    user_settings(**{name: 7.5})
    assert read() == 7.5
    user_settings(**{name: 0})
    with pytest.raises(SlotRefused):
        read()
