"""What a finished study wrote, read off its files: the engine's output table.

Each read takes the directory the study's files are in and refuses by name when the file a tool
writes is not there. PEST++ numbers its per-iteration files ``pestpp.<iteration>.*``; an
unstated iteration is the last one written. A read both ies and glm answer takes the ``tool``
that wrote the directory, ies unstated.
"""

from __future__ import annotations

import csv
import re
import struct
from pathlib import Path
from typing import Any, Callable

from trid3nt_server.store import objects as storage

from .errors import PestppError

__all__ = ["OUTPUTS", "fetch_study", "last_iteration", "read_jacobian", "read_parameter_ensemble",
           "read_parameter_iterations", "read_parameters", "read_phi", "read_residuals", "read_sensitivity"]

_PHI = {"ies": "pestpp.phi.actual.csv", "glm": "pestpp.iobj"}
_PHI_FIELDS = ("total_runs", "mean", "standard_deviation", "min", "max")
#: The columns of glm's per-iteration objective record the read keeps, by the name it keeps them under.
_IOBJ_FIELDS = {"model_runs_completed": "total_runs", "total_phi": "phi", "measurement_phi": "measurement",
                "regularization_phi": "regularization"}
#: The name widths of a binary Jacobian: PEST++'s long-name layout, then PEST's own for parameters and observations.
_JCB_NAME, _JCB_PARAMETER, _JCB_OBSERVATION = 200, 12, 20


def _missing(path: Path, what: str) -> PestppError:
    return PestppError(f"{path.name} is not among the study's files, so its {what} cannot be read",
                       error_code="PESTPP_OUTPUT_MISSING")


def _rows(path: Path, what: str) -> list[dict[str, str]]:
    if not path.is_file():
        raise _missing(path, what)
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def last_iteration(directory: Path, suffix: str) -> int:
    """The highest ``<iteration>`` of the ``pestpp.<iteration>.<suffix>`` files in ``directory``."""
    pattern = re.compile(rf"^pestpp\.(\d+)\.{re.escape(suffix)}$")
    found = [int(m.group(1)) for p in directory.iterdir() if (m := pattern.match(p.name))]
    if not found:
        raise PestppError(f"no pestpp.<iteration>.{suffix} file is among the study's files",
                          error_code="PESTPP_OUTPUT_MISSING")
    return max(found)


def _tool(tool: str) -> str:
    if tool not in _PHI:
        raise PestppError(f"the study's outputs are read for {' or '.join(_PHI)}, not {tool}",
                          error_code="PESTPP_METHOD_UNKNOWN")
    return tool


def read_phi(directory: Path, tool: str = "ies") -> list[dict[str, Any]]:
    """The objective function per iteration: ``iteration`` and the run count to it; an ensemble study's
    mean, spread and range over the realizations and the base realization's, a glm study's ``phi`` of
    its one parameter set with the measurement and regularization parts of it."""
    if _tool(tool) == "glm":
        return [{"iteration": int(row["iteration"]),
                 **{kept: (int(row[read]) if kept == "total_runs" else float(row[read]))
                    for read, kept in _IOBJ_FIELDS.items()}}
                for row in _rows(directory / _PHI[tool], "phi per iteration")]
    out = []
    for row in _rows(directory / _PHI[tool], "phi per iteration"):
        entry: dict[str, Any] = {"iteration": int(row["iteration"])}
        entry.update({key: (int(row[key]) if key == "total_runs" else float(row[key])) for key in _PHI_FIELDS})
        if row.get("base") not in (None, ""):
            entry["base"] = float(row["base"])
        out.append(entry)
    return out


def read_parameter_ensemble(directory: Path, iteration: int | None = None) -> dict[str, dict[str, float]]:
    """``{realization: {parameter: value}}`` at ``iteration``, in the values the model was run with."""
    at = last_iteration(directory, "par.csv") if iteration is None else int(iteration)
    rows = _rows(directory / f"pestpp.{at}.par.csv", "parameter ensemble")
    return {row.pop("real_name"): {name: float(value) for name, value in row.items()} for row in rows}


def read_parameters(directory: Path, iteration: int | None = None) -> dict[str, float]:
    """``{parameter: value}`` of a glm study's parameter file: the best set it found, unstated, or the set
    it reached at ``iteration``, in the values the model was run with."""
    path = directory / ("pestpp.par" if iteration is None else f"pestpp.{int(iteration)}.par")
    if not path.is_file():
        raise _missing(path, "parameters")
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return {parts[0]: float(parts[1]) for parts in (line.split() for line in lines[1:]) if len(parts) == 4}


def read_parameter_iterations(directory: Path) -> dict[int, dict[str, float]]:
    """``{iteration: {parameter: value}}`` a glm study held its one parameter set at, from its start."""
    return {int(row.pop("iteration")): {name: float(value) for name, value in row.items()}
            for row in _rows(directory / "pestpp.ipar", "parameters per iteration")}


def read_jacobian(directory: Path) -> dict[str, dict[str, float]]:
    """``{observation: {parameter: sensitivity}}`` of a glm study's last Jacobian, PEST++'s binary file: the
    derivative of each modelled value by each parameter, as PEST transforms it (log10 where log)."""
    path = directory / "pestpp.jcb"
    if not path.is_file():
        raise _missing(path, "Jacobian")
    body = path.read_bytes()
    columns, rows, count = struct.unpack_from("<iii", body)
    if columns < 0 and rows < 0:
        # PEST's own layout, which PEST++ writes while every name fits it: one column-major index from 1 per entry.
        columns, rows, record, widths = -columns, -rows, "<id", (_JCB_PARAMETER, _JCB_OBSERVATION)
        cells = [((index - 1) % rows, (index - 1) // rows, value)
                 for index, value in struct.iter_unpack(record, body[12:12 + 12 * count])]
    elif columns > 0 and rows > 0:
        record, widths = "<iid", (_JCB_NAME, _JCB_NAME)
        cells = list(struct.iter_unpack(record, body[12:12 + 16 * count]))
    else:
        raise PestppError(f"{path.name} is not a binary Jacobian PEST++ writes", error_code="PESTPP_OUTPUT_UNREAD")
    at = 12 + struct.calcsize(record) * count

    def names(n: int, width: int) -> list[str]:
        nonlocal at
        out = [body[at + k * width: at + (k + 1) * width].decode("ascii").strip().lower() for k in range(n)]
        at += n * width
        return out

    parameters, observations = names(columns, widths[0]), names(rows, widths[1])
    out: dict[str, dict[str, float]] = {name: dict.fromkeys(parameters, 0.0) for name in observations}
    for row, column, value in cells:
        out[observations[row]][parameters[column]] = value
    return out


def read_residuals(directory: Path, iteration: int | None = None, tool: str = "ies") -> list[dict[str, Any]]:
    """Per observation of the base realization at ``iteration`` (an ensemble study), or of a glm study's
    best parameter set (``iteration`` its set at that iteration): its group, measured and modelled values, the
    residual measured minus modelled, and its weight, as PEST++'s residual file states them."""
    if _tool(tool) == "glm":
        path = directory / ("pestpp.rei" if iteration is None else f"pestpp.rei{int(iteration)}")
    else:
        at = last_iteration(directory, "base.rei") if iteration is None else int(iteration)
        path = directory / f"pestpp.{at}.base.rei"
    if not path.is_file():
        raise _missing(path, "residuals")
    out, header = [], False
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split()
        if parts[:2] == ["Name", "Group"]:
            header = True
        elif header and len(parts) == 6:
            out.append({"name": parts[0], "group": parts[1], "measured": float(parts[2]),
                        "modelled": float(parts[3]), "residual": float(parts[4]), "weight": float(parts[5])})
    return out


def read_sensitivity(directory: Path) -> list[dict[str, Any]]:
    """The Method of Morris summary per parameter of a sensitivity study: the sample count and the
    mean, mean absolute and standard deviation of its elementary effects on phi."""
    return [{"parameter": row["parameter_name"], "n_samples": int(row["n_samples"]),
             "sen_mean": float(row["sen_mean"]), "sen_mean_abs": float(row["sen_mean_abs"]),
             "sen_std_dev": float(row["sen_std_dev"])}
            for row in _rows(directory / "pestpp.msn", "sensitivity")]


#: The output table: each name a finished study is read by -> the read.
OUTPUTS: dict[str, Callable[..., Any]] = {
    "phi": read_phi, "parameter_ensemble": read_parameter_ensemble, "parameters": read_parameters,
    "parameter_iterations": read_parameter_iterations, "jacobian": read_jacobian,
    "residuals": read_residuals, "sensitivity": read_sensitivity}


def fetch_study(run_id: str, dest: Path) -> Path:
    """The study's own files under the run prefix, downloaded into ``dest`` -> ``dest``."""
    s3, bucket = storage.client(), storage.runs_bucket()
    dest.mkdir(parents=True, exist_ok=True)
    page: dict[str, Any] = {"Bucket": bucket, "Prefix": f"{run_id}/pestpp"}
    while True:
        listed = s3.list_objects_v2(**page)
        for item in listed.get("Contents") or []:
            name = item["Key"].split("/", 1)[1]
            if "/" not in name:
                (dest / name).write_bytes(s3.get_object(Bucket=bucket, Key=item["Key"])["Body"].read())
        if not listed.get("IsTruncated"):
            return dest
        page["ContinuationToken"] = listed["NextContinuationToken"]
