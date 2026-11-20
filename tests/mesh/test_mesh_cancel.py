"""A mesh builds in a child process: a cancelled turn ends it, a finished build returns its mesh, a lock
another thread holds is not the child's, and the run's context reaches it."""

from __future__ import annotations

import asyncio
import os
import threading
import time

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
