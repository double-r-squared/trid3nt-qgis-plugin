"""A mesh builds in a child process: a cancelled turn ends it, a finished build returns its mesh."""

from __future__ import annotations

import asyncio
import os
import time

import numpy as np
import pytest

from trid3nt_server.inputs.run_notes import bind_notes, drain_notes, journal_note
from trid3nt_server.tools.mesh.meshers import Mesh, MeshToolError
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

    def endless(_recipe):
        pidfile.write_text(str(os.getpid()))
        while True:
            time.sleep(0.05)

    async def drive():
        task = asyncio.ensure_future(to_build_thread(_in_child, endless, None))
        for _ in range(400):
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
    def finished(_recipe):
        journal_note("bed: painted")
        return Mesh(points=np.zeros((3, 2)), cells=np.array([[0, 1, 2]]),
                    crs_authid="EPSG:4326", meta={"edge_notes": ["a"]})

    token = bind_notes()
    mesh = asyncio.run(to_build_thread(_in_child, finished, None))
    notes = drain_notes(token)
    assert mesh.node_count == 3 and mesh.meta["edge_notes"] == ["a"]
    assert "bed: painted" in notes


def test_a_build_that_refuses_raises_the_same_typed_error():
    def refuses(_recipe):
        raise MeshToolError("MESH_TEST", "named refusal")

    with pytest.raises(MeshToolError) as caught:
        _in_child(refuses, None)
    assert caught.value.error_code == "MESH_TEST"
