"""The two core settings: the daemon's own cores, and the cores each solver container runs on."""

from __future__ import annotations

import asyncio
import os
import subprocess
from types import SimpleNamespace

import pytest

from trid3nt_server import settings
from trid3nt_server.inputs.run_notes import bind_notes, drain_notes
from trid3nt_server.workflows.runtime.module import SlotRefused
from trid3nt_server.workflows.runtime.sheet import fill
from trid3nt_server.workflows.solver import cores
from trid3nt_server.workflows.solver.solver import _with_cpu_limit
from trid3nt_server.workflows.telemac.modules import wrapper_for
from trid3nt_server.workflows.telemac.modules.sheet import stage

T2D = wrapper_for("telemac2d")
ART = wrapper_for("artemis")
PROCESSORS = "PARALLEL_PROCESSORS"


def _host(monkeypatch, count: int, here: bool) -> None:
    monkeypatch.setattr(cores, "_HOST", (count, here))


def _staged(monkeypatch, tmp_path, sheet, **run):
    from trid3nt_server.workflows.solver import staging
    from trid3nt_server.workflows.telemac.authoring import serializer

    seen: dict = {}

    async def _stage_run(rundir, run_id, **kwargs):
        seen.update(kwargs)
        return {"run_id": run_id, "case": {}}

    monkeypatch.setattr(serializer, "serialize", lambda sheet, rundir, *, steering=None: {"steering": "t2d.cas"})
    monkeypatch.setattr(staging, "stage_run", _stage_run)
    monkeypatch.setattr(staging, "new_rundir", lambda run_id, **_: tmp_path)
    token = bind_notes()
    try:
        asyncio.run(stage(sheet, run_id="TAG", mesh_inputs=(), outputs=("r.slf",), results=("r.slf",),
                          prefix="telemac", server_facts={}))
        notes = drain_notes(token)
    except BaseException:
        drain_notes(token)
        raise
    return seen["cores"], notes


def _deck(**stated):
    return fill(T2D, GEOMETRY_FILE="g.slf", BOUNDARY_CONDITIONS_FILE="g.cli", LAW_OF_BOTTOM_FRICTION=3,
                GRAPHIC_PRINTOUT_PERIOD=100, **stated)


def test_the_docker_host_is_read_once_and_compared_by_name(monkeypatch):
    calls = []

    def _run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(stdout=f"16\t{os.uname().nodename}\n")

    monkeypatch.setattr(cores, "_HOST", None)
    monkeypatch.setattr(subprocess, "run", _run)
    assert cores.docker_host() == (16, True) and cores.docker_host() == (16, True)
    assert len(calls) == 1 and calls[0][:2] == ["docker", "info"]


def test_a_docker_host_by_another_name_is_not_this_box(monkeypatch):
    monkeypatch.setattr(cores, "_HOST", None)
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: SimpleNamespace(stdout="32\tsome-other-host\n"))
    assert cores.docker_host() == (32, False)


def test_container_default_keeps_a_core_on_this_box_and_takes_all_on_another(monkeypatch):
    _host(monkeypatch, 8, True)
    assert cores.container_default() == 7
    _host(monkeypatch, 1, True)
    assert cores.container_default() == 1
    _host(monkeypatch, 8, False)
    assert cores.container_default() == 8


def test_the_core_settings_take_their_default_and_range_from_the_hosts(monkeypatch):
    _host(monkeypatch, 8, True)
    table = settings.dictionary()
    assert table["CONTAINER_CORES"].engine_default == 7 and table["CONTAINER_CORES"].bounds == ((1.0, 8.0),)
    assert table["LOCAL_CORES"].engine_default == cores.local_cores()
    assert table["LOCAL_CORES"].bounds == ((1.0, float(cores.local_cores())),)
    with pytest.raises(SlotRefused):
        settings.typed(table["CONTAINER_CORES"], 9)
    with pytest.raises(SlotRefused):
        settings.typed(table["LOCAL_CORES"], 0)


def test_the_launch_line_carries_docker_cpu_limit():
    assert _with_cpu_limit(["docker", "run", "--rm", "img"], 3) == ["docker", "run", "--cpus", "3", "--rm", "img"]
    assert _with_cpu_limit(["python", "x.py"], 3) == ["python", "x.py"]


def test_a_run_that_states_no_count_takes_container_cores_and_says_so(monkeypatch, tmp_path):
    _host(monkeypatch, 8, True)
    count, notes = _staged(monkeypatch, tmp_path, _deck())
    assert count == 7
    assert any("PARALLEL PROCESSORS 7 from CONTAINER_CORES" in note for note in notes)


def test_on_a_remote_docker_host_the_default_is_all_its_cores(monkeypatch, tmp_path):
    _host(monkeypatch, 16, False)
    assert _staged(monkeypatch, tmp_path, _deck())[0] == 16


def test_a_stated_count_wins_over_the_setting(monkeypatch, tmp_path):
    _host(monkeypatch, 8, True)
    count, notes = _staged(monkeypatch, tmp_path, _deck(PARALLEL_PROCESSORS=2))
    assert count == 2 and not any("CONTAINER_CORES" in note for note in notes)


def test_a_stated_count_over_the_docker_host_refuses(monkeypatch):
    _host(monkeypatch, 8, True)
    with pytest.raises(SlotRefused, match="8 cores the docker host has"):
        fill(T2D, **{PROCESSORS: 9})


def test_a_run_binds_container_cores(monkeypatch, tmp_path):
    _host(monkeypatch, 8, True)
    token = settings.bind_run({"CONTAINER_CORES": 3})
    try:
        assert _staged(monkeypatch, tmp_path, _deck())[0] == 3
    finally:
        settings.reset_run(token)


def test_one_container_core_leaves_the_deck_serial(monkeypatch, tmp_path):
    _host(monkeypatch, 8, True)
    token = settings.bind_run({"CONTAINER_CORES": 1})
    try:
        count, notes = _staged(monkeypatch, tmp_path, _deck())
    finally:
        settings.reset_run(token)
    assert count == 1 and not notes


def test_a_direct_solve_stays_serial_and_takes_no_default(monkeypatch, tmp_path):
    _host(monkeypatch, 8, True)
    sheet = fill(ART, GEOMETRY_FILE="g.slf", BOUNDARY_CONDITIONS_FILE="g.cli")
    assert ART.MODULE_INPUT["SOLVER"].engine_default == 8
    from trid3nt_server.workflows.telemac.modules.sheet import _with_container_cores

    assert PROCESSORS not in _with_container_cores(sheet).filled


def test_geoclaw_bouss_takes_container_cores_over_its_threads(monkeypatch):
    from trid3nt_server.workflows.geoclaw.workflow import _processes

    _host(monkeypatch, 8, True)
    assert _processes({}, "geoclaw_bouss") == 7
    assert _processes({"threads": 2}, "geoclaw_bouss") == 3
    assert _processes({"cores": 2}, "geoclaw_bouss") == 2
    assert _processes({}, "geoclaw") == 1


def test_local_cores_bounds_the_mesh_child(tmp_path):
    from trid3nt_server.tools.mesh.meshers import MeshToolError
    from trid3nt_server.tools.mesh.session import _in_child

    def _report(recipe):
        raise MeshToolError("SEEN", f"{len(os.sched_getaffinity(0))} {os.environ['OMP_NUM_THREADS']}")

    token = settings.bind_run({"LOCAL_CORES": 1})
    try:
        with pytest.raises(MeshToolError) as seen:
            _in_child(_report, None)
    finally:
        settings.reset_run(token)
    assert str(seen.value).endswith("1 1")
