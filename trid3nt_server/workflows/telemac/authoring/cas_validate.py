"""Every authored steering file, read by the engine's own parser before staging.

File-existence checking is off: geometry and boundary files are staged later by the launcher.
Only grammar and vocabulary are checked.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping


logger = logging.getLogger("trid3nt_server.workflows.telemac.authoring.cas_validate")

__all__ = ["CasParseError", "EngineHomeMissing", "engine_home", "run_cas_driver",
           "run_engine_script", "validate_authored_steering"]

_SCRIPTS = Path(__file__).resolve().parent / "engine_scripts"
_REPO_HOME = Path(__file__).resolve().parents[4] / "third_party" / "opentelemac"
_TIMEOUT_S = 300


class CasParseError(RuntimeError):
    """An authored steering file does not parse against its own dictionary."""

    error_code = "TELEMAC_CAS_PARSE_FAILED"


class EngineHomeMissing(CasParseError):
    """The opentelemac tree the steering scripts read is not installed."""

    error_code = "TELEMAC_ENGINE_HOME_MISSING"


def engine_home() -> Path:
    """The opentelemac tree (``TRID3NT_OPENTELEMAC_HOME``, else ``third_party/opentelemac``), or the refusal naming the script that installs it."""
    home = Path(os.environ.get("TRID3NT_OPENTELEMAC_HOME") or _REPO_HOME)
    if not (home / "scripts" / "python3" / "execution" / "telemac_cas.py").is_file():
        raise EngineHomeMissing(
            f"the opentelemac steering tree is not at {home}; run scripts/fetch_opentelemac.sh "
            "to install it, or set TRID3NT_OPENTELEMAC_HOME to where it lives")
    return home


def run_engine_script(script: str, rundir: Path | str, config_name: str, *,
                      timeout_s: float) -> subprocess.CompletedProcess:
    """Run ``engine_scripts/<script>`` over ``rundir`` in its own interpreter, HOMETEL set to the engine tree."""
    env = {**os.environ, "HOMETEL": str(engine_home())}
    return subprocess.run(
        [sys.executable, "-I", str(_SCRIPTS / script),
         str(Path(rundir) / config_name), str(rundir)],
        capture_output=True, text=True, timeout=timeout_s, env=env)


def run_cas_driver(rundir: Path | str, config: Mapping[str, Any], *,
                   what: str) -> None:
    """Run the steering driver over ``rundir``. A refusal carries the driver's words."""
    name = "telemac_cas_config.json"
    (Path(rundir) / name).write_text(json.dumps(dict(config)))
    cp = run_engine_script("cas.py", rundir, name, timeout_s=_TIMEOUT_S)
    if cp.returncode != 0:
        raise CasParseError(
            f"could not {what} (rc={cp.returncode}):\n"
            f"{cp.stdout[-2000:]}\n{cp.stderr[-2000:]}")


# DAMOCLES surfaces a misspelt keyword, a wrongly typed value or a line past 72 characters as a solve that dies inside Fortran blaming a keyword nobody wrote; parsing here refuses by name.
def validate_authored_steering(rundir: Path | str,
                               steering: Mapping[str, str]) -> dict[str, dict]:
    """Parse every ``{basename: module}`` file in ``rundir`` -> what was read.

    One driver run for the whole authoring.
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
