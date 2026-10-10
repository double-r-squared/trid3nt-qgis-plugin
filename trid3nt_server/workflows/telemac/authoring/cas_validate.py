"""Every authored steering file, read by the engine's own parser before staging.

File-existence checking is off: geometry and boundary files are staged later by the launcher.
Only grammar and vocabulary are checked.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Mapping

from trid3nt_server.workflows.solver.image_script import run_image_script

logger = logging.getLogger("trid3nt_server.workflows.telemac.authoring.cas_validate")

__all__ = ["CasParseError", "run_cas_driver", "validate_authored_steering"]

_TELEMAC_IMAGE_DEFAULT = "trid3nt-local/telemac:latest"
_INCONTAINER_SCRIPT = "cas.py"
_CONTAINER_TIMEOUT_S = 300


class CasParseError(RuntimeError):
    """An authored steering file does not parse against its own dictionary."""

    error_code = "TELEMAC_CAS_PARSE_FAILED"


def run_cas_driver(rundir: Path | str, config: Mapping[str, Any], *,
                   what: str) -> None:
    """Shell the steering driver over ``rundir``. The one door to the image.

    A refusal carries the container's words.
    """
    image = os.environ.get("TRID3NT_TELEMAC_IMAGE") or _TELEMAC_IMAGE_DEFAULT
    name = "telemac_cas_config.json"
    (Path(rundir) / name).write_text(json.dumps(dict(config)))
    cp = run_image_script(
        image=image, engine="telemac", script=_INCONTAINER_SCRIPT,
        rundir=Path(rundir), argv=[f"/data/{name}", "/data"],
        timeout_s=_CONTAINER_TIMEOUT_S)
    if cp.returncode != 0:
        raise CasParseError(
            f"could not {what} (rc={cp.returncode}):\n"
            f"{cp.stdout[-2000:]}\n{cp.stderr[-2000:]}")


# DAMOCLES surfaces a misspelt keyword, a wrongly typed value or a line past 72 characters as a solve that dies inside Fortran blaming a keyword nobody wrote; parsing here refuses by name.
def validate_authored_steering(rundir: Path | str,
                               steering: Mapping[str, str]) -> dict[str, dict]:
    """Parse every ``{basename: module}`` file in ``rundir`` -> what was read.

    One container round trip for the whole authoring.
    """
    rundir = Path(rundir)
    present = {name: module for name, module in steering.items()
               if (rundir / name).is_file()}
    if not present:
        return {}
    run_cas_driver(rundir, {"steering": present},
                   what=f"read the authored steering files {sorted(present)}")
    rows = json.loads((rundir / "telemac_cas_stats.json").read_text())
    failed = {name: row for name, row in rows.items() if not row.get("ok")}
    if failed:
        raise CasParseError(
            "the authored steering file(s) do not parse against the engine's own "
            "dictionary, so the solve would stop inside DAMOCLES blaming a "
            "keyword nobody wrote: "
            + "; ".join(f"{name} ({row['module']}): {row['error']}"
                        for name, row in sorted(failed.items())))
    logger.info("telemac cas parse ok: %s",
                {name: row["keywords"] for name, row in rows.items()})
    return rows
