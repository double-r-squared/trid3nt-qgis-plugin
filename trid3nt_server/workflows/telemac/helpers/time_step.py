"""The step a mesh is solved at, and how long that solve is expected to take.

The CFL step is coupled to the edge the accepted mesh was measured at, so a
refined mesh tightens dt without anybody restating it; the wall-clock estimate
errs high by construction, because it bounds a wait rather than predicts a run."""

from __future__ import annotations

from typing import Any

__all__ = ["MESH_H_FLOOR_M", "MESH_NODE_CAP", "estimate_telemac_solve_seconds",
           "suggest_time_step_s"]

#: Absolute mesh edge-length floor (below it quality + solve cost degrade).
MESH_H_FLOOR_M: float = 3.0
#: Node ceiling for a single local-docker TELEMAC solve. It bounds how long the
#: dispatcher WAITS - the worst case it sizes the wait against - and sizes no mesh:
#: the edge a run meshes at is the explicit sheet value the mesher was handed.
MESH_NODE_CAP: int = 60000
#: The timestep MUST be coupled to the edge length or the solve diverges (CFL).
#: dt = TIMESTEP_REF_S * min(1, h / MESH_TIMESTEP_REF_M), anchored at h=20 -> 1 s
#: so the law passes through both live-proven-stable points (20, 1.0) and
#: (10, 0.5) and lands at or below the stable dt at every tested size.
TIMESTEP_REF_S: float = 1.0
MESH_TIMESTEP_REF_M: float = 20.0
#: Floor on the coupled timestep (a runaway-fine mesh cannot drive dt to zero).
TIMESTEP_FLOOR_S: float = 0.2
#: Conservative throughput in node-steps/second, calibrated on two live runs
#: (rates 0.377M and 0.618M/s; the SLOWER is taken so estimates err HIGH).
_TELEMAC_NODE_STEPS_PER_S: float = 377_000.0
#: Fixed overhead outside the node-step model (container start + fetches).
_TELEMAC_SOLVE_OVERHEAD_S: float = 45.0


def suggest_time_step_s(mesh_size_m: float, *, mesh: Any = None) -> float:
    """CFL-safe TELEMAC timestep for the mesh a run will actually solve on.

    A supplied mesh artifact wins over ``mesh_size_m``, the edge merely asked for."""
    # A built mesh knows its own shortest edge, and that is what the stability
    # criterion is about; the requested edge is all an estimate made before any
    # mesh exists can honestly use. So refining a region at the gate tightens dt
    # with it, without anybody restating the number.
    from trid3nt_server.workflows.mesh.artifact import measured_min_edge_m

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
