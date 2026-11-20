"""The builds the mesh-child tests hand ``_in_child``: the child imports its build by name, so a build
lives at module level in a module on the path, never inside a test."""

from __future__ import annotations

import os
import threading
import time

import numpy as np

from trid3nt_server.inputs.run_notes import journal_note
from trid3nt_server.tools.mesh.meshers import Mesh, MeshToolError

#: Taken by a parent thread while a child starts: a child that copied the parent's memory finds it held.
HELD = threading.Lock()


def triangle() -> Mesh:
    return Mesh(points=np.zeros((3, 2)), cells=np.array([[0, 1, 2]]),
                crs_authid="EPSG:4326", meta={"edge_notes": ["a"]})


def endless(pidfile):
    pidfile.write_text(str(os.getpid()))
    while True:
        time.sleep(0.05)


def finished(_recipe):
    journal_note("bed: painted")
    return triangle()


def refuses(_recipe):
    raise MeshToolError("MESH_TEST", "named refusal")


def takes_the_held_lock(_recipe):
    if not HELD.acquire(timeout=5):
        raise MeshToolError("MESH_TEST_LOCK_INHERITED", "the child started with a lock another thread held")
    HELD.release()
    return triangle()


def reports_cores(_recipe):
    raise MeshToolError("SEEN", f"{len(os.sched_getaffinity(0))} {os.environ['OMP_NUM_THREADS']}")


def reports_context(key_name):
    from trid3nt_server.model.credentials.resolver import stored_value
    from trid3nt_server.tools.mesh.inputs import _rasters_dir

    raise MeshToolError("SEEN", f"{_rasters_dir()} {stored_value(key_name)}")


def reports_parent(_recipe):
    raise MeshToolError("SEEN", f"parent {os.getppid()}")


def reports_late(_recipe):
    raise MeshToolError("SEEN", f"parent {os.getppid()} {os.environ['TRID3NT_TEST_LATE']} "
                                f"{len(os.sched_getaffinity(0))} {os.environ['OMP_NUM_THREADS']}")


def reports_cpus(_recipe):
    raise MeshToolError("SEEN", f"parent {os.getppid()} cpus {sorted(os.sched_getaffinity(0))}")


def a_second_per_node(nodes, edge_m):
    return float(nodes)
