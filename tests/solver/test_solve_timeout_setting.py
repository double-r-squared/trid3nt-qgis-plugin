"""SOLVE_TIMEOUT_S is the least a solve is waited on: an engine with no estimate waits it, and
TELEMAC waits it or its own estimate of a cap-sized run, whichever is longer."""
from __future__ import annotations

from trid3nt_server.workflows.telemac import engine as telemac


def test_telemac_waits_the_setting_unless_its_estimate_is_longer(user_settings):
    short = {"duration_s": 60.0, "time_step_s": 1.0}
    assert telemac._wait_seconds(short) == 7200.0
    user_settings(SOLVE_TIMEOUT_S=20000)
    assert telemac._wait_seconds(short) == 20000.0
    long_run = {"duration_s": 86400.0 * 10, "time_step_s": 0.5}
    assert telemac._wait_seconds(long_run) > 20000.0


def test_a_sheet_with_no_horizon_waits_at_least_a_day(user_settings):
    assert telemac._wait_seconds({}) == 86400.0
    user_settings(SOLVE_TIMEOUT_S=100000)
    assert telemac._wait_seconds({}) == 100000.0
