"""``Solve.pyswmm`` - the headless in-process SWMM 5 solve every deck shares.

The pyswmm templates run the native engine on the box (no worker image), so the
shared step is the run loop itself: step the simulation, sample the named objects
at every step, and report the continuity error the deck earned. What varies per
question is WHICH objects are sampled, which is a declared argument.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any, Sequence

from trid3nt_server.declarative import Step

from .errors import SwmmSolveError

__all__ = ["Solve", "solve_deck"]

logger = logging.getLogger("trid3nt_server.workflows.swmm.steps.solve")

_STEPS = "trid3nt_server.workflows.swmm.steps.solve"


class Solve:
    """SWMM solves. One constructor per execution lane."""

    @staticmethod
    def pyswmm(**kwargs: Any) -> Step:
        """Run a deck through the native SWMM 5 engine in-process (host-exec)."""
        return Step(runner=f"{_STEPS}.solve_deck", kwargs=kwargs, consequential=True)


async def solve_deck(
    *,
    inp_text: str,
    nodes: Sequence[str] = (),
    subcatchments: Sequence[str] = (),
    node_attr: str = "total_inflow",
    subcatchment_attr: str = "runoff",
    label: str = "swmm",
) -> dict[str, Any]:
    """Solve one deck and return ``hours`` + the sampled series + the continuity.

    ``hours`` is real elapsed time off ``sim.current_time``: SWMM advances on a
    variable wet/dry step, so the step index is not a time axis.
    """
    import asyncio

    return await asyncio.to_thread(
        _run, inp_text, tuple(nodes), tuple(subcatchments), node_attr,
        subcatchment_attr, label,
    )


def _run(inp_text: str, nodes: tuple[str, ...], subcatchments: tuple[str, ...],
         node_attr: str, subcatchment_attr: str, label: str) -> dict[str, Any]:
    import pyswmm

    base = Path(tempfile.mkdtemp(prefix=f"swmm-{label}-"))
    inp = base / "model.inp"
    inp.write_text(inp_text, encoding="utf-8")

    hours: list[float] = []
    node_series: dict[str, list[float]] = {n: [] for n in nodes}
    sub_series: dict[str, list[float]] = {s: [] for s in subcatchments}
    try:
        with pyswmm.Simulation(str(inp)) as sim:
            node_objs = {n: pyswmm.Nodes(sim)[n] for n in nodes}
            sub_objs = {s: pyswmm.Subcatchments(sim)[s] for s in subcatchments}
            t0 = None
            for _ in sim:
                now = sim.current_time
                if t0 is None:
                    t0 = now
                hours.append((now - t0).total_seconds() / 3600.0)
                for name, obj in node_objs.items():
                    node_series[name].append(float(getattr(obj, node_attr)))
                for name, obj in sub_objs.items():
                    sub_series[name].append(float(getattr(obj, subcatchment_attr)))
            continuity = float(sim.flow_routing_error) * 100.0
    except Exception as exc:  # noqa: BLE001 - re-raised typed, cause preserved
        raise SwmmSolveError(f"{label}: the SWMM 5 engine failed: {exc}") from exc

    if not hours:
        raise SwmmSolveError(
            f"{label}: the SWMM 5 engine produced no timesteps, so there is no "
            "hydrograph to report."
        )
    logger.info("swmm %s solved: %d steps, continuity %.4f%%", label, len(hours),
                continuity)
    return {"hours": hours, "nodes": node_series, "subcatchments": sub_series,
            "flow_routing_error_pct": continuity}
