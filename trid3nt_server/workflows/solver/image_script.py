"""One round trip through an engine's own image: a script in, a rundir out.

Every engine mounts its own ``scripts_dir()`` read-only at ``/drivers`` and a
rundir at ``/data``, runs with no network, and reads back whatever the script
left; the script itself imports nothing from trid3nt, so the mount is the only
connection. This is the short round trip - a sibling to ``run_solver``'s
long-run dispatch, never a change to it."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Sequence

__all__ = ["scripts_dir", "run_image_script"]

logger = logging.getLogger("trid3nt_server.workflows.solver.image_script")


def scripts_dir(engine: str) -> Path:
    """The directory of ``engine``'s in-image scripts, mounted as ``/drivers``.

    The scripts live beside their engine's Dockerfile under ``workers/`` and
    import nothing from trid3nt, so the mount is the only connection and this
    resolver is the only place the layout is named."""
    return Path(__file__).resolve().parents[3] / "workers" / engine / "scripts"


def run_image_script(*, image: str, engine: str, script: str, rundir: Path,
                     argv: Sequence[str], extra_mounts: Sequence[tuple[Path, str]] = (),
                     timeout_s: float,
                     entrypoint_override: bool = False) -> subprocess.CompletedProcess:
    """Run ``script`` inside ``image`` against ``rundir`` -> the finished process.

    ``extra_mounts`` are ``(host, container)`` pairs bound between the scripts
    mount and the rundir mount, in the order given. ``entrypoint_override``
    shells ``python`` as the container's entrypoint rather than its first
    argument - both reach the same script, per the image's own default."""
    cmd = ["docker", "run", "--rm", "--network", "none",
          "-v", f"{scripts_dir(engine)}:/drivers:ro"]
    for host, target in extra_mounts:
        cmd += ["-v", f"{host}:{target}"]
    cmd += ["-v", f"{rundir}:/data"]
    if entrypoint_override:
        cmd += ["--entrypoint", "python", image]
    else:
        cmd += [image, "python"]
    cmd += [f"/drivers/{script}", *argv]
    logger.info("%s: %s", script, " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
