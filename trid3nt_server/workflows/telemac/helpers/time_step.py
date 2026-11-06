"""The step a mesh is solved at.

The CFL step follows the edge the accepted mesh was measured at.
"""

from __future__ import annotations

from typing import Any

__all__ = ["MESH_H_FLOOR_M", "suggest_time_step_s"]

# Absolute mesh edge-length floor.
MESH_H_FLOOR_M: float = 3.0
# dt = TIMESTEP_REF_S * min(1, h / MESH_TIMESTEP_REF_M); the timestep must follow the edge
# length or the solve diverges (CFL). Anchored at h=20 -> 1 s, through the live-stable
# points (20, 1.0) and (10, 0.5).
TIMESTEP_REF_S: float = 1.0
MESH_TIMESTEP_REF_M: float = 20.0
# Floor on the coupled timestep.
TIMESTEP_FLOOR_S: float = 0.2


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
