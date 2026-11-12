"""A study directory, staged: the control file, its template and instruction files, the model files.

The control file is staged as ``pestpp.pst`` whatever it was called, because PEST++ names every
file it writes after it and the engine reads its record by that name. The staged directory is
checked against the control file before the manifest exists.
"""

from __future__ import annotations

import csv
import shutil
from pathlib import Path
from typing import Any

from trid3nt_contracts import new_ulid

from trid3nt_server.settings import setting
from trid3nt_server.workflows.solver.staging import new_rundir, stage_run

from .errors import PestppError

__all__ = ["CONTROL_FILENAME", "RECORD_FILENAME", "check_staged", "interface_files", "stage_study"]

CONTROL_FILENAME = "pestpp.pst"
RECORD_FILENAME = "pestpp.rec"

#: What each tool writes that a finished study is read by, beside its record; a tool not
#: listed is held to its record alone.
_RESULTS = {"ies": ("pestpp.phi.actual.csv",), "sen": ("pestpp.msn",)}

#: Everything the manager writes is named after the control file, and so is uploaded by that stem.
_OUTPUTS = ["pestpp.*", "pestpp_metrics.json"]


def _sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in text.splitlines():
        if line.startswith("*"):
            current = sections.setdefault(line[1:].strip().lower(), [])
        elif line.startswith("++"):
            current = None
        elif current is not None and line.strip():
            current.append(line.strip())
    return sections


def _external(rundir: Path, name: str) -> list[tuple[str, str]]:
    path = rundir / name
    if not path.is_file():
        raise PestppError(f"the control file lists its model files in {name}, which the study directory does "
                          "not hold", error_code="PESTPP_INTERFACE_MISSING")
    with path.open(newline="", encoding="utf-8") as fh:
        return [(row["pest_file"], row["model_file"]) for row in csv.DictReader(fh)]


def interface_files(rundir: Path, control: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """``(templates, instructions)`` the control file ``control`` names, each ``(pest file, model file)``.

    Version 1 counts them on the third control-data line; version 2 lists them in two external tables."""
    sections = _sections((rundir / control).read_text(encoding="utf-8", errors="replace"))
    if "model input/output" in sections:
        try:
            ntpl, nins = (int(v) for v in sections["control data"][2].split()[:2])
        except (KeyError, IndexError, ValueError) as exc:
            raise PestppError("the control file's control data does not state its template and instruction "
                              f"file counts: {exc}", error_code="PESTPP_CONTROL_UNREAD") from exc
        pairs = [tuple(row.split()[:2]) for row in sections["model input/output"]]
        if len(pairs) != ntpl + nins or any(len(p) != 2 for p in pairs):
            raise PestppError(f"the control file counts {ntpl} template and {nins} instruction files but its "
                              f"model input/output section lists {len(pairs)} pairs", error_code="PESTPP_CONTROL_UNREAD")
        return pairs[:ntpl], pairs[ntpl:]
    tables = [sections.get(f"model {side} external") for side in ("input", "output")]
    if all(tables):
        return _external(rundir, tables[0][0]), _external(rundir, tables[1][0])
    raise PestppError("the control file has no model input/output section the engine reads, so the files its "
                      "trials write and read cannot be checked", error_code="PESTPP_CONTROL_UNREAD")


def check_staged(rundir: Path, *, steering: str, **_: Any) -> None:
    """Every template and instruction file the control file names is staged, and every folder a
    template writes into exists: a missing one fails every trial, not the first second."""
    templates, instructions = interface_files(rundir, steering)
    missing = [pest for pest, _model in (*templates, *instructions) if not (rundir / pest).is_file()]
    if missing:
        raise PestppError(f"the control file names {missing}, which the study directory does not hold",
                          error_code="PESTPP_INTERFACE_MISSING")
    homeless = [model for _pest, model in templates if not (rundir / model).parent.is_dir()]
    if homeless:
        raise PestppError(f"the templates write {homeless}, whose folders the study directory does not hold",
                          error_code="PESTPP_INTERFACE_MISSING")


async def stage_study(source: Path, *, tool: str, control: str, name: str,
                      agents: int | None = None) -> dict[str, Any]:
    """A study directory -> the staged run the box receives.

    ``control`` is the control file's name in ``source``; ``agents`` the PEST++ agents the
    manager is given, unstated the CONTAINER_CORES setting."""
    if not (source / control).is_file():
        raise PestppError(f"{control} is not in {source}", error_code="PESTPP_CONTROL_MISSING")
    run_id = new_ulid()
    rundir = new_rundir(run_id, stem="pestpp")
    shutil.copytree(source, rundir, dirs_exist_ok=True)
    if control != CONTROL_FILENAME:
        (rundir / control).rename(rundir / CONTROL_FILENAME)
    count = int(agents or setting("CONTAINER_CORES"))
    return await stage_run(
        rundir, run_id, module=tool, steering=CONTROL_FILENAME,
        results=[RECORD_FILENAME, *_RESULTS.get(tool, ())], outputs=list(_OUTPUTS),
        mesh_inputs=[], prefix="pestpp", sheet={}, server_facts={"name": name},
        result_basename=RECORD_FILENAME, check=check_staged, cores=count)
