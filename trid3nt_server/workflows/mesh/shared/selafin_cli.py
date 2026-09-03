"""The TELEMAC geometry pair - a SELAFIN and the ``.cli`` numbered from it.

The two files are ONE artifact: the ``.cli`` rows are ordered by the geometry's
own IPOBO, so a boundary file written against any other node numbering silently
classifies the wrong nodes. They are therefore written together, in one pass,
inside ``trid3nt-local/telemac:latest`` - the only place telapy and pretel are
installed. The host stages the node arrays, mounts the driver and the rundir,
and reads back the stats the driver measured.

Shared rather than per-mesher: any mesher that can hand over nodes, cells and a
bed writes its TELEMAC geometry through here, so the numbering agreement is made
once instead of once per wrapper.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Mapping

from trid3nt_server.workflows.mesh.meshers import MeshToolError
from trid3nt_server.workflows.mesh.meshers.drivers import drivers_dir

logger = logging.getLogger("trid3nt_server.workflows.mesh.shared.selafin_cli")

__all__ = ["write_telemac_pair"]

_TELEMAC_IMAGE_DEFAULT = "trid3nt-local/telemac:latest"
_INCONTAINER_SCRIPT = "selafin_cli_driver.py"
_CONTAINER_TIMEOUT_S = 1800


def write_telemac_pair(rundir: Path | str, *, x: Any, y: Any, cells: Any,
                       bed: Any, roles: Mapping[str, Any] | None = None,
                       title: str = "TRID3NT MESH") -> dict[str, Any]:
    """Write the SELAFIN geometry and its ``.cli`` -> the two paths and the stats.

    ``roles`` maps a boundary role (``inflow``, ``outflow``, ``open``,
    ``free_exit``) to the node indices carrying it; every other boundary node is
    written as a solid wall.
    The stats carry what the driver MEASURED - the boundary node count, the
    liquid-boundary numbering AND the role of each numbered liquid boundary,
    whether the IPOBO it wrote is the permutation TELEMAC requires - so a caller
    reports the numbering rather than asserting it, and a steering file is
    authored once against the order the solver will use.
    """
    import numpy as np

    rundir = Path(rundir)
    npz = rundir / "selafin_in.npz"
    np.savez(npz, x=np.asarray(x, dtype=float), y=np.asarray(y, dtype=float),
             ikle=np.asarray(cells, dtype=np.int64),
             bottom=(np.asarray(bed, dtype=float) if bed is not None
                     else np.empty(0)))
    stats = _run_driver(rundir, {
        "mesh_npz": f"/data/{npz.name}", "geo_slf": "/data/mesh.slf",
        "cli": "/data/mesh.cli", "title": title,
        "roles": {str(role): [int(n) for n in nodes]
                  for role, nodes in (roles or {}).items()}})
    return {"geo_slf": rundir / "mesh.slf", "cli": rundir / "mesh.cli",
            "stats": stats}


def _run_driver(rundir: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    """One driver run in the TELEMAC box -> the stats it reported."""
    image = os.environ.get("TRID3NT_TELEMAC_IMAGE") or _TELEMAC_IMAGE_DEFAULT
    name = "selafin_cli_config.json"
    (rundir / name).write_text(json.dumps(dict(config)))
    argv = [
        "docker", "run", "--rm", "--network", "none",
        "-v", f"{drivers_dir()}:/drivers:ro", "-v", f"{rundir}:/data",
        image, "python",
        f"/drivers/{_INCONTAINER_SCRIPT}", f"/data/{name}", "/data"]
    logger.info("selafin_cli write: %s", " ".join(argv))
    cp = subprocess.run(argv, capture_output=True, text=True,
                        timeout=_CONTAINER_TIMEOUT_S)
    if cp.returncode != 0:
        raise MeshToolError(
            "MESH_TELAPY_FAILED",
            f"the TELEMAC geometry pair could not be written (rc={cp.returncode}):\n"
            f"{cp.stdout[-2000:]}\n{cp.stderr[-2000:]}")
    return json.loads((rundir / "selafin_cli_stats.json").read_text())
