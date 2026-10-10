"""The step a mesh is solved at, and how long that solve is expected to take.

The CFL step follows the edge the accepted mesh was measured at; the wall-clock
estimate errs high because it bounds a wait rather than predicts a run.
"""

from __future__ import annotations

from typing import Any

__all__ = ["MESH_H_FLOOR_M", "MESH_NODE_CAP", "estimate_telemac_solve_seconds",
           "suggest_time_step_s"]

# Absolute mesh edge-length floor.
MESH_H_FLOOR_M: float = 3.0
# Node ceiling for a local-docker solve; it sizes the dispatcher wait, not the mesh.
MESH_NODE_CAP: int = 60000
# dt = TIMESTEP_REF_S * min(1, h / MESH_TIMESTEP_REF_M); the timestep must follow the edge
# length or the solve diverges (CFL). Anchored at h=20 -> 1 s, through the live-stable
# points (20, 1.0) and (10, 0.5).
TIMESTEP_REF_S: float = 1.0
MESH_TIMESTEP_REF_M: float = 20.0
# Floor on the coupled timestep.
TIMESTEP_FLOOR_S: float = 0.2
# Conservative node-steps/second from two live runs (0.377M and 0.618M/s); the slower is taken.
_TELEMAC_NODE_STEPS_PER_S: float = 377_000.0
# Fixed overhead outside the node-step model (container start + fetches).
_TELEMAC_SOLVE_OVERHEAD_S: float = 45.0


def suggest_time_step_s(mesh_size_m: float, *, mesh: Any = None) -> float:
    """CFL-safe TELEMAC timestep for the mesh a run will actually solve on.

    A supplied mesh artifact wins over ``mesh_size_m``.
    """
    # A built mesh's shortest edge is what stability depends on; the requested edge is only the pre-mesh estimate.
    from trid3nt_server.tools.mesh.artifact import measured_min_edge_m

    measured = measured_min_edge_m(mesh)
    h = max(float(mesh_size_m if measured is None else measured), MESH_H_FLOOR_M)
    dt = TIMESTEP_REF_S * min(1.0, h / MESH_TIMESTEP_REF_M)
    return round(max(dt, TIMESTEP_FLOOR_S), 3)


def estimate_telemac_solve_seconds(
    npoin: int, sim_duration_s: float, time_step_s: float
) -> float:
    """Conservative wall-clock estimate for a full TELEMAC solve. Errs high by design."""
    steps = max(float(sim_duration_s), 0.0) / max(float(time_step_s), 1e-6)
    est = max(int(npoin), 0) * steps / _TELEMAC_NODE_STEPS_PER_S
    return round(est + _TELEMAC_SOLVE_OVERHEAD_S, 1)
