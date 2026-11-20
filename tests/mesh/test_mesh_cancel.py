"""A mesh builds in a child forked from the preloaded forkserver: a cancelled turn ends it, a finished build
returns its mesh, a lock another thread holds is not the child's, the run's context - its environment and
cores as they are at the build, not at the server's start - reaches it, and a dead server starts again."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import threading
import time
from multiprocessing import forkserver

import mesh_builds
import pytest

from trid3nt_server.inputs.run_notes import bind_notes, drain_notes
from trid3nt_server.model.credentials.resolver import bind_session, clear_session, set_session_credential, unbind_session
from trid3nt_server.tools.mesh.inputs import raster_scope, solve_estimated_by
from trid3nt_server.tools.mesh.meshers import MeshToolError
from trid3nt_server.tools.mesh.session import _in_child, to_build_thread


def _gone(pid: int, within_s: float = 5.0) -> bool:
    deadline = time.monotonic() + within_s
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def test_a_cancelled_build_ends_its_child_and_returns_no_mesh(tmp_path):
    pidfile = tmp_path / "pid"

    async def drive():
        task = asyncio.ensure_future(to_build_thread(_in_child, mesh_builds.endless, pidfile))
        for _ in range(3000):
            if pidfile.exists() and pidfile.read_text():
                break
            await asyncio.sleep(0.01)
        pid = int(pidfile.read_text())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return pid

    pid = asyncio.run(drive())
    assert pid != os.getpid()
    assert _gone(pid), "the cancelled build's child is still running"


def test_a_build_that_is_not_cancelled_returns_its_mesh_and_its_notes():
    token = bind_notes()
    mesh = asyncio.run(to_build_thread(_in_child, mesh_builds.finished, None))
    notes = drain_notes(token)
    assert mesh.node_count == 3 and mesh.meta["edge_notes"] == ["a"]
    assert "bed: painted" in notes


def test_a_build_that_refuses_raises_the_same_typed_error():
    with pytest.raises(MeshToolError) as caught:
        _in_child(mesh_builds.refuses, None)
    assert caught.value.error_code == "MESH_TEST"


def test_a_lock_another_thread_holds_does_not_hold_the_child():
    taken, done = threading.Event(), threading.Event()

    def hold():
        with mesh_builds.HELD:
            taken.set()
            done.wait(30)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    taken.wait(5)
    try:
        mesh = _in_child(mesh_builds.takes_the_held_lock, None)
    finally:
        done.set()
        holder.join()
    assert mesh.node_count == 3


def test_the_run_context_reaches_the_child():
    session = bind_session("session-mesh-child")
    set_session_credential("session-mesh-child", "test_key", "the-key")
    try:
        with raster_scope() as scratch, pytest.raises(MeshToolError) as seen:
            _in_child(mesh_builds.reports_context, "test_key")
    finally:
        clear_session("session-mesh-child")
        unbind_session(session)
    assert str(seen.value).endswith(f"{scratch} the-key")


def test_a_mesh_input_that_cannot_reach_the_child_refuses_by_name():
    with solve_estimated_by(lambda nodes, edge: 1.0), pytest.raises(MeshToolError) as refused:
        _in_child(mesh_builds.finished, None)
    assert refused.value.error_code == "MESH_BUILD_INPUT_UNCROSSABLE" and "_SOLVE" in str(refused.value)


def _started() -> int:
    _in_child(mesh_builds.finished, None)
    return forkserver._forkserver._forkserver_pid


def _seen(build) -> str:
    with pytest.raises(MeshToolError) as seen:
        _in_child(build, None)
    return str(seen.value)


def test_a_build_forks_from_the_server_that_preloaded_the_registry():
    server = _started()
    assert "trid3nt_server.tools" in forkserver._forkserver._preload_modules
    assert _seen(mesh_builds.reports_parent).endswith(f"parent {server}")


def test_the_environment_stated_after_the_server_started_reaches_the_child(monkeypatch, tmp_path):
    server = _started()
    (tmp_path / "settings.json").write_text(json.dumps({"LOCAL_CORES": 1}))
    monkeypatch.setenv("TRID3NT_HOME", str(tmp_path))
    monkeypatch.setenv("TRID3NT_TEST_LATE", "late")
    assert _seen(mesh_builds.reports_late).endswith(f"parent {server} late 1 1")


def test_the_cores_the_build_thread_holds_after_the_server_started_bound_the_child():
    server = _started()
    mine = os.sched_getaffinity(0)
    one = {min(mine)}
    os.sched_setaffinity(0, one)
    try:
        seen = _seen(mesh_builds.reports_cpus)
    finally:
        os.sched_setaffinity(0, mine)
    assert seen.endswith(f"parent {server} cpus {sorted(one)}")


def _zombie(pid: int, within_s: float = 5.0) -> bool:
    deadline = time.monotonic() + within_s
    while time.monotonic() < deadline:
        with open(f"/proc/{pid}/stat") as stat:
            if stat.read().rsplit(")", 1)[1].split()[0] == "Z":
                return True
        time.sleep(0.05)
    return False


def test_a_dead_server_is_started_again_by_the_next_build():
    old = _started()
    os.kill(old, signal.SIGKILL)
    assert _zombie(old), "the killed forkserver did not end"
    seen = _seen(mesh_builds.reports_parent)
    new = forkserver._forkserver._forkserver_pid
    assert new != old and seen.endswith(f"parent {new}")
