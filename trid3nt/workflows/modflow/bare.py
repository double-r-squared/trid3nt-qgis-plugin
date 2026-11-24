"""A MODFLOW model authored or loaded through its native package layout."""

from __future__ import annotations

import asyncio
import shlex
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping
from urllib.parse import urlsplit

from trid3nt_contracts import new_ulid
from trid3nt_contracts.common import Origin

from trid3nt.inputs.input import Input
from trid3nt.tools.cache import read_object_bytes_s3

from ..runtime.bare import BareWorkflow, NativeResult
from ..runtime.fill import ACCEPTED, MISSING, Fill, Verdict, production
from ..runtime.workflow import Deck, RunResult
from ..runtime.sheet import Filled, Provenance
from ..solver.staging import discard_run, new_rundir, stage_run
from .authoring import native
from .authoring.simulation import _REQUIRED_PACKAGES
from .errors import ModflowError
from .modules import GWE, GWT, PRT
from .workflow import _DISPATCH as _DISPATCH, _validated

NATIVE_SIMULATION = Input(type=str, desc=(
    "Existing mfsim.nam on the SDK or the URI of a staged ZIP of the native simulation.")).stated()


class BareModflowWorkflow(BareWorkflow):
    engine = "modflow"

    def wrappers(self) -> tuple[type, ...]:
        return (self.steering, GWT, GWE, PRT)

    def open_inputs(self, case: Any, values: Mapping[str, Any]) -> tuple[dict, dict, dict]:
        source = values.get("simulation")
        if not source:
            return super().open_inputs(case, values)
        with tempfile.TemporaryDirectory(dir=case.root) as directory:
            work = Path(directory)
            if urlsplit(str(source)).scheme:
                uri, pin = case.take(str(source))
                simulation = native.unpack(case.path(uri), work)
            else:
                simulation = native.load(Path(source))
                simulation.pack(work / "simulation.zip")
                uri, pin = case.take(str(work / "simulation.zip"))
                pin["request"] = str(source)
            first_flow = next((simulation.values[name] for name, module in simulation.modules.items()
                               if module == "gwf"), {})
            rows = {key: value for packages in (simulation.values[""], first_flow)
                    for fields in packages.values() for key, value in fields.items()}
        return {**values, "simulation": uri}, {"simulation": pin}, rows

    def judged(self, state: Fill) -> dict[str, Verdict]:
        refused = super().judged(state)
        if "simulation" in state.inputs:
            return refused
        own, coupled = self.floor(state.keywords)
        grids = {name.partition("_")[0] for name in own} & {"DIS", "DISV", "DISU"}
        if len(grids) != 1:
            refused["grid"] = Verdict(MISSING, code="MODFLOW_GRID_UNSTATED",
                                      reason="State exactly one native grid using DIS, DISV or DISU keywords.")
        for body in self.wrappers():
            if body is self.steering:
                held = own
                packages = {"IC", "NPF", "TDIS", "IMS"} | grids
            elif body.MODULE in coupled:
                held = coupled[body.MODULE]
                packages = set(_REQUIRED_PACKAGES[body.MODULE])
            else:
                continue
            packages |= {name.partition("_")[0] for name in held} - {"NAM"}
            for key, slot in body.MODULE_INPUT.items():
                if slot.is_required and key.partition("_")[0] in packages and key not in held:
                    name = key if body is self.steering else f"{body.MODULE}: {key}"
                    refused[name] = Verdict(MISSING, code="MODFLOW_KEYWORD_UNSTATED",
                                           reason=f"State {name}.")
        return refused

    async def prepare(self, state: Fill) -> Deck:
        env = production(state)
        run_id = new_ulid()
        work = new_rundir(run_id, stem=self.engine)
        own, coupled = self.floor(state.keywords)
        supplied = env.run.get("simulation")

        def author() -> tuple[native.NativeSimulation, list[str]]:
            if supplied:
                if not isinstance(supplied, str) or not supplied.startswith("s3://"):
                    raise ModflowError("simulation must be an uploaded object or case file.",
                                       error_code="MODFLOW_NATIVE_SIMULATION_UNREAD")
                with tempfile.TemporaryDirectory(dir=work.parent) as directory:
                    archive = Path(directory) / "simulation.zip"
                    archive.write_bytes(read_object_bytes_s3(supplied))
                    imported = native.unpack(archive, work)
                if own or coupled:
                    imported = native.override(imported, own, coupled)
            else:
                imported = native.author(work, own, coupled)
            _validated(work)
            return imported, _outputs(imported)

        try:
            imported, outputs = await asyncio.to_thread(author)
            grid = await asyncio.to_thread(native.mapping_grid, work) if env.run.get("epsg") else None
            if supplied:
                for name, packages in imported.values.items():
                    for package, fields in packages.items():
                        for key, value in fields.items():
                            module = imported.modules.get(name, "gwf")
                            if key in (own if module == "gwf" else coupled.get(module, {})):
                                continue
                            state.inputs[f"{name or 'simulation'}/{package}: {key}"] = Verdict(
                                ACCEPTED, value, origin=Origin.FILE, source=supplied)
            staged = await stage_run(
                work, run_id, module="gwf", steering="mfsim.nam", results=["mfsim.lst"],
                outputs=[*outputs, "mfsim.lst", "modflow_metrics.json"], mesh_inputs=[],
                prefix=self.engine, sheet={}, server_facts={"name": self.name, "utm_epsg": env.run.get("epsg")},
                result_basename=next(iter(outputs), "mfsim.lst"), check=_check_native)
            return await self.review_native(Deck(state, run_id=run_id, prefix=self.engine, staged=staged,
                                                 held={"grid": grid, "values": imported.values} if grid else {}))
        except BaseException:
            await asyncio.to_thread(discard_run, run_id, self.engine, stem=self.engine)
            raise

    async def solve(self, deck: Deck) -> RunResult:
        from .engine import solve_case

        result = await self._solve_native(deck, solve_case)
        if not deck.held.get("grid"):
            return result
        result.value = await self._publish_grid(deck, result.results["solve"])
        return result

    async def _publish_grid(self, deck: Deck, solved: Mapping[str, Any]):
        import numpy as np

        from ..runtime.primitives import publish_reads
        from ..runtime.sheet import reference_time
        from .modules.outputs import Solved
        from .workflow import _record, _table

        topology = deck.held["grid"]
        values = deck.held["values"]
        stated = {key: value for group in (values[""], values[topology["model"]])
                  for package in group.values() for key, value in package.items()}
        units = stated.get("DIS_LENGTH_UNITS")
        if units and "DISV_LENGTH_UNITS" not in stated:
            stated["DISV_LENGTH_UNITS"] = units
        body = self.steering
        fields = {name: Filled(slot=body.slot(name), value=value, provenance=Provenance(Origin.FILE))
                  for name, value in stated.items() if name in body.MODULE_INPUT}
        sheet = body.sheet(fields, {}, {})
        grid = SimpleNamespace(**{key: np.asarray(value) for key, value in topology.items()
                                  if key != "model"})
        if "OC_HEAD_FILERECORD" not in fields:
            return NativeResult(run_id=solved["run_id"], uri=solved["uri"])
        if "TDIS_TIME_UNITS" not in fields:
            return NativeResult(run_id=solved["run_id"], uri=solved["uri"],
                                notes=["State TDIS_TIME_UNITS to publish time-varying grid outputs."])
        run = {**solved, "sheet": sheet, "mesh": {}, "module": "gwf", "name": self.name,
               "reference_time": reference_time(None, solved)}
        return await publish_reads(
            engine=self.engine, run=run, outputs=[], captions={}, params={}, anchors=[],
            solved=lambda _: Solved(run, body, grid=grid),
            table=lambda open_model: _table(open_model, "gwf", ()), record=_record)

    def _inputs(self, run: RunResult, supplied: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        native = {name: verdict for name, verdict in run.inputs.items() if "/" in name}
        inputs = super()._inputs(replace(run, inputs={name: verdict for name, verdict in run.inputs.items()
                                                     if name not in native}), supplied)
        return {**inputs, **{name: {"value": row.value, "origin": row.origin.value, "source": row.source}
                             for name, row in native.items()}}


def _outputs(imported: native.NativeSimulation) -> list[str]:
    outputs = set()
    for name in imported.files:
        if Path(name).suffix.lower() not in {".oc", ".nam", ".obs"}:
            continue
        for line in (imported.root / name).read_text().splitlines():
            if "FILEOUT" not in line.upper():
                continue
            words = shlex.split(line, comments=True)
            for key, value in zip(words, words[1:]):
                if key.upper() == "FILEOUT" and not Path(value).is_absolute() and ".." not in Path(value).parts:
                    outputs.add(value)
    return sorted(outputs)


def _check_native(work: Path, *, inputs: list[dict[str, str]], **_: Any) -> None:
    from .authoring.staged_check import _inputs_match_the_staged

    _inputs_match_the_staged(work, inputs)
