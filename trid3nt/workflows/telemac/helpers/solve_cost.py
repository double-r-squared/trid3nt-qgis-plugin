"""The wall seconds a TELEMAC 2D solve takes, from the mesh and the clock.

The step count is the run length over the same step the settle derives; the cost is measured on this box.
"""

from __future__ import annotations

from .time_step import suggest_time_step_s

__all__ = ["solve_wall_seconds"]

# Seconds per estimated node per step on one core, and the share of a step that does not divide across cores (Amdahl form).
# Fitted to Telemac2D solves of 77k to 250k estimated nodes on 7 cores (0.016 s per step at 77k nodes, 0.069 at 250k)
# and to 1, 3 and 7 cores at 77k (0.050, 0.020, 0.016 s per step).
SECONDS_PER_NODE_STEP = 7.7e-7
SERIAL_FRACTION = 0.21


def solve_wall_seconds(nodes: int, edge_m: float, *, duration_s: float, cores: int,
                       time_step_s: float | None = None) -> float:
    """Wall seconds of a TELEMAC 2D solve of ``duration_s`` on ``nodes`` nodes at ``edge_m``, partitioned across ``cores``.

    The step is ``time_step_s`` where the run states one, else the CFL step the settle derives for that edge.
    """
    step = time_step_s if time_step_s else suggest_time_step_s(edge_m)
    per_step = SECONDS_PER_NODE_STEP * nodes * (SERIAL_FRACTION + (1.0 - SERIAL_FRACTION) / max(1, cores))
    return per_step * duration_s / step
