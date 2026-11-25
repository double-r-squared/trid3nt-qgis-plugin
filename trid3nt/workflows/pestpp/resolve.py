"""A native MODFLOW solve of an explicit decision vector, read back through its OBS CSVs."""

from __future__ import annotations

import asyncio
import csv
import json
import math
import shutil
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from trid3nt_contracts import new_ulid

from trid3nt.store import objects
from trid3nt.tools.cache import read_object_bytes_s3
from trid3nt.workflows.modflow import engine as modflow
from trid3nt.workflows.modflow.authoring.validate import validate_authored_simulation

from ..runtime.sheet import run as solve_staged
from ..solver.staging import discard_run, new_rundir, stage_run
from . import staging
from .authoring.control import Parameter, substitute
from .errors import PestppError


async def solve_decisions(*, manifest_uri: str, templates: Sequence[tuple[str, str]],
                          decisions: Sequence[Mapping[str, Any]], readings: Mapping[str, tuple[str, int]],
                          label: str) -> tuple[dict[str, Any], dict[str, float]]:
    """Stage the study's pinned deck with ``decisions`` written in, solve it as an ordinary MODFLOW run, and
    read each observation's native OBS CSV -> ``(handle, {observation: simulated})``."""
    manifest = json.loads(await asyncio.to_thread(read_object_bytes_s3, manifest_uri))
    run_id = new_ulid()
    directory = new_rundir(run_id, stem="modflow")
    try:
        await asyncio.to_thread(staging.fetch_deck, manifest["inputs"], directory)
        values = {str(row["name"]).casefold(): row["value"] for row in decisions}
        parameters = [Parameter(str(row["name"]).casefold(), row["start"], row["lower"], row["upper"],
                                "well_rates", transform="none") for row in decisions]
        await asyncio.to_thread(substitute, directory, templates, parameters, values)
        observed_csvs = sorted({reading[0] for reading in readings.values()})

        def check(work: Path, **_: Any) -> None:
            with tempfile.TemporaryDirectory(prefix="pestpp-decision-validation-") as validation:
                shutil.copytree(work, validation, dirs_exist_ok=True)
                validate_authored_simulation(Path(validation))

        staged = await stage_run(
            directory, run_id, module="gwf", steering="mfsim.nam", results=["mfsim.lst"],
            outputs=[*observed_csvs, "modflow_metrics.json"], mesh_inputs=[],
            prefix="modflow", sheet={}, server_facts={"name": label},
            result_basename="mfsim.lst", check=check)
        handle = await solve_staged(staged, dispatch=modflow.solve_case)
        samples: dict[str, float] = {}
        for file in observed_csvs:
            uri = f"s3://{objects.runs_bucket()}/{run_id}/{file}"
            text = (await asyncio.to_thread(read_object_bytes_s3, uri)).decode("utf-8")
            rows = [{name.casefold(): value for name, value in row.items()}
                    for row in csv.DictReader(text.splitlines())]
            for name, (filename, ordinal) in readings.items():
                if filename != file:
                    continue
                try:
                    value = float(rows[ordinal - 1][name])
                except (IndexError, KeyError, ValueError) as error:
                    raise PestppError(f"The native solve did not write {name} at output row {ordinal}.",
                                      error_code="PESTPP_DECISION_OUTPUT") from error
                if not math.isfinite(value) or value == 3.0e30:
                    raise PestppError(f"The native solve wrote no simulated value for {name}.",
                                      error_code="PESTPP_DECISION_OUTPUT")
                samples[name] = value
    except BaseException:
        await asyncio.to_thread(discard_run, run_id, "modflow", stem="modflow")
        raise
    return handle, samples
