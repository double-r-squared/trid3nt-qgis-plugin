"""A TELEMAC 2D solve's wall seconds is estimated from the nodes, the clock, the step the settle derives and the cores."""

from __future__ import annotations

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.workflows.telemac.helpers.solve_cost import (
    SECONDS_PER_NODE_STEP, SERIAL_FRACTION, solve_wall_seconds)
from trid3nt_server.workflows.telemac.helpers.time_step import suggest_time_step_s


def _workflow(name="telemac_dye_release"):
    return TOOL_REGISTRY[name].fn.workflow


def test_the_steps_are_the_run_length_over_the_settles_own_step():
    nodes, edge, hours = 100_000, 12.0, 3600.0
    got = solve_wall_seconds(nodes, edge, duration_s=hours, cores=1)
    assert got == pytest.approx(
        SECONDS_PER_NODE_STEP * nodes * hours / suggest_time_step_s(edge))


def test_a_stated_time_step_is_honoured():
    free = solve_wall_seconds(100_000, 3.0, duration_s=600.0, cores=4)
    stated = solve_wall_seconds(100_000, 3.0, duration_s=600.0, cores=4, time_step_s=0.1)
    assert stated == pytest.approx(free * suggest_time_step_s(3.0) / 0.1)


def test_more_cores_shorten_the_solve_and_the_serial_share_stays():
    one = solve_wall_seconds(50_000, 3.0, duration_s=600.0, cores=1)
    many = solve_wall_seconds(50_000, 3.0, duration_s=600.0, cores=10_000)
    assert many == pytest.approx(one * SERIAL_FRACTION, rel=1e-3)
    assert solve_wall_seconds(50_000, 3.0, duration_s=600.0, cores=4) < one


def test_the_measured_hour_on_seven_cores_is_recovered():
    # 250k estimated nodes, 3 m edge, one hour on 7 cores took about 1060 s to solve
    got = solve_wall_seconds(250_000, 3.0, duration_s=3600.0, cores=7)
    assert got == pytest.approx(1060.0, rel=0.15)


def test_the_workflow_hands_the_mesh_an_estimate_on_the_run_clock_and_cores(user_settings):
    user_settings(CONTAINER_CORES=3)
    estimate = _workflow()._solve_estimate({"DURATION": 600.0})
    assert estimate(50_000, 3.0) == pytest.approx(
        solve_wall_seconds(50_000, 3.0, duration_s=600.0, cores=3))
    stated = _workflow()._solve_estimate(
        {"DURATION": 600.0, "PARALLEL_PROCESSORS": 5, "TIME_STEP": 0.4})
    assert stated(50_000, 3.0) == pytest.approx(
        solve_wall_seconds(50_000, 3.0, duration_s=600.0, cores=5, time_step_s=0.4))


def test_a_direct_solve_is_one_core(user_settings):
    user_settings(CONTAINER_CORES=7)
    estimate = _workflow()._solve_estimate({"DURATION": 600.0, "SOLVER": 8})
    assert estimate(50_000, 3.0) == pytest.approx(
        solve_wall_seconds(50_000, 3.0, duration_s=600.0, cores=1))


def test_a_module_with_no_measured_estimate_hands_none():
    assert _workflow("telemac3d_stratified_flow")._solve_estimate({"DURATION": 600.0}) is None


@pytest.mark.asyncio
async def test_the_mesh_step_hands_the_estimate_to_the_session(monkeypatch):
    from trid3nt_server.tools.mesh import gate as gate_mod
    from trid3nt_server.tools.mesh import session as session_mod
    from trid3nt_server.tools.mesh import step as mesh_step

    seen = {}

    def _session(*_a, **kw):
        seen.update(kw)

    async def _stop(_session, **_kw):
        raise RuntimeError("stop")

    monkeypatch.setattr(session_mod, "MeshSession", _session)
    monkeypatch.setattr(gate_mod, "gate_mesh_build", _stop)
    estimate = lambda nodes, edge: 1.0  # noqa: E731
    with pytest.raises(RuntimeError, match="stop"):
        await mesh_step.build_declared_mesh(
            mesh={"mesher": "reg_grid", "kind": None, "extent": None,
                  "resolution_m": 100.0, "ops": []}, solve_estimate=estimate)
    assert seen["solve_estimate"] is estimate
