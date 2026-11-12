"""What a finished study wrote, read off its files: the engine's output table.

Each read takes the directory the study's files are in and refuses by name when the file a tool
writes is not there. PEST++ numbers its per-iteration files ``pestpp.<iteration>.*``; an
unstated iteration is the last one written.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any, Callable

from trid3nt_server.store import objects as storage

from .errors import PestppError

__all__ = ["OUTPUTS", "fetch_study", "last_iteration", "read_parameter_ensemble", "read_phi",
           "read_residuals", "read_sensitivity"]

_PHI = "pestpp.phi.actual.csv"
_PHI_FIELDS = ("total_runs", "mean", "standard_deviation", "min", "max")


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


def read_phi(directory: Path) -> list[dict[str, Any]]:
    """The objective function per iteration of an ensemble study: ``iteration`` and the run count
    to it, the mean, spread and range over the realizations, and the base realization's."""
    out = []
    for row in _rows(directory / _PHI, "phi per iteration"):
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


def read_residuals(directory: Path, iteration: int | None = None) -> list[dict[str, Any]]:
    """Per observation of the base realization at ``iteration``: its group, measured and modelled
    values, the residual measured minus modelled, and its weight, as PEST++'s residual file states them."""
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
    "phi": read_phi, "parameter_ensemble": read_parameter_ensemble,
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
