"""SWMM worker CLI shim — run a staged ``.inp`` headless via pyswmm.

The out-of-process (local-exec) lane of the urban-flood engine. The DEV PRIMARY
path runs pyswmm IN-PROCESS via ``workflows/run_swmm.run_swmm_local`` and never
touches this; this shim exists so the SWMM ``LocalSolverSpec`` (``exec_kind=
"exec"``) can run pyswmm against a staged ``.inp`` in a rundir — symmetric with
the SFINCS / MODFLOW worker entrypoints.

Usage:
    python run_inp.py <mesh.inp> [mesh2.inp ...]

Runs each ``.inp`` via ``pyswmm.Simulation`` (which writes the ``.rpt`` + ``.out``
alongside it) and exits 0 on a clean solve, non-zero on a pyswmm failure. The
mass-balance honesty gate is applied by the spec's ``classify_exit`` reading the
``.rpt`` Flow Routing Continuity error — NOT here, so the artifacts are always
produced for the classifier to inspect.
"""

from __future__ import annotations

import sys
from pathlib import Path


def run_one(inp_path: str) -> None:
    """Run a single ``.inp`` headless via pyswmm (writes ``.rpt`` + ``.out``)."""
    from pyswmm import Simulation

    with Simulation(inp_path) as sim:
        for _ in sim:
            pass


def main(argv: list[str]) -> int:
    inps = [a for a in argv if a.endswith(".inp")]
    if not inps:
        # Fall back to any .inp in CWD (the rundir) — the spec stages it here.
        inps = [str(p) for p in sorted(Path.cwd().glob("*.inp"))]
    if not inps:
        sys.stderr.write("run_inp.py: no .inp file given or found in CWD\n")
        return 2
    for inp in inps:
        if not Path(inp).exists():
            sys.stderr.write(f"run_inp.py: .inp not found: {inp}\n")
            return 2
        run_one(inp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
