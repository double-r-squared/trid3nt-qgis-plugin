"""The accepted topology of a mesh - what its geometry file cannot state.

Which stretch of a boundary is the inflow, which numbered liquid boundary the engine
calls each stretch, and what each prescribes; the nodes, cells and bed are the SELAFIN's.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

__all__ = ["FREE_EXIT_ROLE", "RATING_CURVE_ROLE", "boundary_topology"]

# The role whose ``.cli`` quad prescribes NOTHING (water leaves at the interior's level and
# velocity); it tells a boundary stating no condition by design from two disagreeing files.
FREE_EXIT_ROLE: str = "free_exit"

# The role whose level is read off a stage-discharge curve; its quad is the outflow's, so the
# steering author owes the curve keywords at that boundary's number.
RATING_CURVE_ROLE: str = "rating_curve"


def boundary_topology(*, roles: Mapping[str, Sequence[int]],
                      liquid_boundary_order: Sequence[str],
                      liquid_boundary_prescribes: Sequence[str],
                      ) -> dict[str, Any]:
    """The walk's roles and numbering -> the bundle every stage reads it by.

    A numbering stating what fewer boundaries prescribe than it numbers refuses.
    """
    roles = {str(r): [int(n) for n in nodes] for r, nodes in roles.items() if nodes}
    order = [str(r) for r in liquid_boundary_order]
    prescribes = [str(p) for p in liquid_boundary_prescribes]
    if len(prescribes) != len(order):
        raise ValueError(
            f"this topology states what {len(prescribes)} of its {len(order)} "
            "liquid boundaries prescribe; it was numbered before the boundary "
            "numbering was measured by the engine's own rule, so rebuild the "
            "mesh rather than author a steering file against it")
    # A walk naming no liquid boundary is a closed basin, not a gap; ``states`` carries that so a reader needing a role refuses about the role.
    return {"roles": roles, "liquid_boundary_order": order,
            "liquid_boundary_prescribes": prescribes,
            "states": ("this domain names no liquid boundary; its whole boundary "
                       "is solid wall"
                       if not order else
                       f"{len(order)} liquid boundar"
                       f"{'y' if len(order) == 1 else 'ies'}, numbered "
                       f"{', '.join(f'{i}={r}' for i, r in enumerate(order, 1))}")}
