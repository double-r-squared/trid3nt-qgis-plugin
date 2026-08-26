"""Dispatch of the REGULAR-GRID SFINCS solve to the plain upstream image.

The deck is authored in-agent (hydromt_sfincs), staged into the run directory by
the shared launcher, and solved by the stock ``deltares/sfincs-cpu`` binary over
a volume mount. That is the whole spec, and it belongs beside the quadtree one
rather than inside the dispatcher: the dispatcher's job is to look a solver up in
the registry, and SFINCS was the one engine it had to know about by name.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_SFINCS_IMAGE",
    "SFINCS_SOLVER_NAME",
    "register_sfincs_local_spec",
    "sfincs_local_spec",
]

#: The ``run_solver`` identifier for the regular-grid volume-mount path.
SFINCS_SOLVER_NAME: str = "sfincs"

#: Default image (env ``TRID3NT_SFINCS_IMAGE`` overrides). The PLAIN upstream
#: binary image: the deck is already authored, so the container needs nothing
#: but the solver.
DEFAULT_SFINCS_IMAGE: str = "deltares/sfincs-cpu:latest"


def sfincs_local_spec() -> Any:
    """The regular-grid SFINCS ``LocalSolverSpec`` -- volume mount, no network use."""
    from trid3nt_server.workflows.solver.solver import (
        LOCAL_DOCKER_WORKFLOW_NAME,
        LocalSolverSpec,
    )

    image = os.environ.get("TRID3NT_SFINCS_IMAGE") or DEFAULT_SFINCS_IMAGE

    def build_argv(run_id: str, rundir: Path, args: list[str]) -> list[str]:
        return [
            "docker", "run", "--rm", "--name", run_id,
            "-v", f"{rundir}:/data", "-w", "/data", image, *args,
        ]

    return LocalSolverSpec(
        solver=SFINCS_SOLVER_NAME,
        workflow_name=LOCAL_DOCKER_WORKFLOW_NAME,
        args_key="sfincs_args",
        build_argv=build_argv,
        stdout_name="sfincs.stdout",
        stderr_name="sfincs.stderr",
        stdout_uri_field="sfincs_stdout_uri",
        stderr_uri_field="sfincs_stderr_uri",
        exec_kind="docker",
        classify_exit=None,
    )


def register_sfincs_local_spec() -> None:
    """Register the regular-grid SFINCS spec factory (import-time)."""
    from trid3nt_server.workflows.solver.solver import register_local_solver_spec

    register_local_solver_spec(SFINCS_SOLVER_NAME, sfincs_local_spec)


register_sfincs_local_spec()
