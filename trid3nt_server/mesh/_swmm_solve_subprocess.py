"""Isolated SWMM solve child (Bug 2/3: process isolation + killable deadline).

Invoked as ``python -m trid3nt_server.mesh._swmm_solve_subprocess <job.json>``
by ``raster_cell_mesh._solve_swmm_in_subprocess``. A FRESH interpreter per solve
dissolves the pyswmm single-instance lock (a stuck/completed Simulation no longer
poisons the long-lived daemon), and running in a killable child lets the parent
SIGKILL a runaway at the wall-clock deadline.

Seam (thin, files-on-disk): ``job.json`` carries ``inp_path`` + grid shape +
``sample_every_steps`` + the result paths. The solve loop itself is the single
shared ``raster_cell_mesh.run_swmm_simulation`` (never duplicated). Output:
``.out``/``.rpt`` beside the ``.inp`` (pyswmm) + ``peak.npy`` + ``meta.json``.
On a solver failure ``meta.json`` carries a typed ``error_code`` and the child
exits non-zero so the parent raises the honest SWMMMeshError.
"""

from __future__ import annotations

import json
import sys


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        sys.stderr.write("usage: _swmm_solve_subprocess <job.json>\n")
        return 2
    with open(argv[1], encoding="utf-8") as fh:
        job = json.load(fh)

    import numpy as np

    from trid3nt_server.mesh.raster_cell_mesh import (
        SWMMMeshError,
        _active_cells_from_inp,
        run_swmm_simulation,
    )

    inp_path = job["inp_path"]
    meta_json = job["meta_json"]
    try:
        active_cells = _active_cells_from_inp(inp_path)
        peak_grid, n_steps, last_dt, wall = run_swmm_simulation(
            inp_path,
            int(job["nrows"]),
            int(job["ncols"]),
            active_cells,
            int(job["sample_every_steps"]),
        )
    except SWMMMeshError as exc:
        with open(meta_json, "w", encoding="utf-8") as fh:
            json.dump(
                {"error_code": exc.error_code, "error": str(exc)}, fh
            )
        return 3
    except Exception as exc:  # noqa: BLE001 -- any crash is a typed failure
        with open(meta_json, "w", encoding="utf-8") as fh:
            json.dump(
                {"error_code": "SWMM_RUN_FAILED",
                 "error": f"{type(exc).__name__}: {exc}"}, fh
            )
        return 3

    np.save(job["peak_npy"], peak_grid)
    with open(meta_json, "w", encoding="utf-8") as fh:
        json.dump(
            {"n_steps": int(n_steps), "last_dt_s": last_dt,
             "wall_seconds": float(wall)}, fh
        )
    return 0


if __name__ == "__main__":  # pragma: no cover -- exercised via subprocess
    sys.exit(main(sys.argv))
