"""The authored simulation, read by the engine's own mf6 before anything stages.

Grammar, vocabulary and dimension: what mf6's parser refuses when it reads ``mfsim.nam``
- a missing file, a non-numeric value, an unknown tag, a dimension mismatch. What it
does not catch - a value outside its domain, a clock against the run's window - is the
staged check's beside it.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from trid3nt_server.workflows.solver.image_script import run_image_script

from ..engine import _modflow_image
from ..errors import ModflowError

logger = logging.getLogger("trid3nt_server.workflows.modflow.authoring.validate")

__all__ = ["validate_authored_simulation"]

#: The in-image driver beside the worker; one container round trip for the whole directory.
_INCONTAINER_SCRIPT = "validate.py"
_TIMEOUT_S = 300


def validate_authored_simulation(rundir: Path | str) -> dict[str, Any]:
    """Run mf6's own ``--mode validate`` over the directory -> the exit it passed with.

    mf6's own words ride the refusal, so a bad file names itself here rather than as a
    solve that stops inside the engine with a message nobody can act on.
    """
    cp = run_image_script(
        image=_modflow_image(), engine="modflow", script=_INCONTAINER_SCRIPT,
        rundir=Path(rundir), argv=[], timeout_s=_TIMEOUT_S)
    out = f"{cp.stdout}\n{cp.stderr}"
    if cp.returncode != 0 or "Normal termination of simulation." not in out:
        raise ModflowError(
            f"mf6 does not accept this run directory (rc={cp.returncode}); its own "
            f"parser refuses what the solve would stop on:\n{out[-2000:]}",
            error_code="MODFLOW_VALIDATE_FAILED")
    logger.info("modflow mf6 validate ok over %s", rundir)
    return {"rc": int(cp.returncode)}
