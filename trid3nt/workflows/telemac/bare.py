"""A bare TELEMAC deck staged through the module's own sheet and solver."""

from __future__ import annotations

import asyncio
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from trid3nt_contracts import new_ulid
from trid3nt_contracts.common import Origin

from trid3nt.inputs.input import Input
from trid3nt.tools.cache import read_object_bytes_s3

from ..runtime.bare import BareWorkflow, native_files, native_name
from ..runtime.fill import MISSING, REJECTED, Fill, Verdict, production
from ..runtime.module import SlotRefused
from ..runtime.review import fill_sheet
from ..runtime.workflow import Deck, RunResult
from ..solver.staging import discard_run
from .authoring.cas_validate import CasParseError, import_steering
from .errors import TelemacError
from .modules import WRAPPERS
from .modules.coupling import bare_coupling
from .modules.sheet import TelemacSheet, stage
from .workflow import _ALWAYS_READABLE, _DISPATCH as _DISPATCH, publish_outputs, run_sheet

NATIVE_STEERING = Input(type=str, desc="Existing steering file. State its path or staged URI.").stated()
COUPLING = Input(type=list[dict],
                 desc="Coupled bodies. State module and any required process. The steering filename is optional.").stated()


class BareTelemacWorkflow(BareWorkflow):
    engine = "telemac"

    def sheet_doc(self) -> str:
        body = self.steering
        processor = body.slot(body.PARTITION_KEYWORD)
        return f"Sheet: {body.MODULE}. State the process count with {processor.keyword}."

    def wrappers(self) -> tuple[type, ...]:
        own = self.steering
        return (own, *(wrapper for module, wrapper in WRAPPERS.items()
                        if module != own.MODULE and "coupling" in own.COMPOSITES
                        and f"{module.upper()}_STEERING_FILE" in own.MODULE_INPUT))

    def _read(self, path: Path, work: Path) -> tuple[dict, tuple[str, ...]]:
        values, files = import_steering(path, self.steering.MODULE, work)
        return {self.input(name).name: value for name, value in values.items()}, files

    def open_inputs(self, case: Any, values: Mapping[str, Any]) -> tuple[dict, dict, dict]:
        values = dict(values)
        locations = native_files(values.pop("files", {}))
        imported: dict[str, Any] = {}
        taken: dict[str, Any] = {}
        steering = values.get("steering")
        if steering:
            steering = str(steering)
            held, taken["steering"] = case.take(steering)
            values["steering"] = held
            source = Path(steering).expanduser() if not urlsplit(steering).scheme else None
            with tempfile.TemporaryDirectory(dir=case.root) as scratch:
                imported, files = self._read(case.path(held), Path(scratch))
            for keyword in files:
                name = imported[self.input(keyword).name]
                if source is not None and isinstance(name, str) and name.strip() and name not in locations:
                    locations[native_name(name)] = str(source.parent / name)
        if locations:
            values["files"] = {}
            for name, location in locations.items():
                uri, taken[f"files:{name}"] = case.take(str(location))
                values["files"][name] = uri
        return values, taken, imported

    async def filled(self, wire: Mapping[str, Any], **options: Any) -> Fill:
        steering = wire.get("steering")
        if not steering:
            return await super().filled(wire, **options)
        if not isinstance(steering, str) or not steering.startswith("s3://"):
            return Fill(self, inputs={"steering": Verdict(
                REJECTED, code="TELEMAC_NATIVE_STEERING",
                reason="Steering must be a case file or a staged object URI.")})

        def read() -> dict:
            from ..solver.staging import new_rundir

            work = new_rundir(new_ulid(), stem=self.engine)
            try:
                path = work / "source.cas"
                path.write_bytes(read_object_bytes_s3(steering))
                return self._read(path, work)[0]
            finally:
                discard_run(work.name, self.engine, stem=self.engine)

        try:
            imported = await asyncio.to_thread(read)
        except (CasParseError, SlotRefused) as error:
            return Fill(self, inputs={"steering": Verdict(
                REJECTED, code=getattr(error, "error_code", "TELEMAC_NATIVE_STEERING"),
                reason=str(error))})
        own = dict(wire.get("keywords") or {})
        asserted = {self.input(name).name for name in own}
        file_values = {name: value for name, value in imported.items() if name not in asserted and name not in wire}
        state = await super().filled({**wire, "keywords": {**file_values, **own}}, **options)
        for name in file_values.keys() & state.inputs.keys():
            state.inputs[name] = replace(state.inputs[name], origin=Origin.FILE, source=steering)
        return state

    def _answers(self, state: Fill) -> tuple[dict, dict, list]:
        own, coupled = self.floor(state.keywords)
        held = state.inputs.get("coupling")
        declarations = bare_coupling(self.steering, held.value) if held and held.value is not None else []
        missing = set(coupled) - {body["module"] for body in declarations}
        if missing:
            raise SlotRefused(f"Attach {', '.join(sorted(missing))} before stating its keywords.")
        if declarations:
            if {"coupling", "COUPLING_WITH", *(f"{b['module'].upper()}_STEERING_FILE" for b in declarations)} & own.keys():
                raise SlotRefused("State each body once using the coupling input.")
            own["coupling"] = declarations
        return own, coupled, declarations

    def judged(self, state: Fill) -> dict[str, Verdict]:
        refused = super().judged(state)
        try:
            own, _, _ = self._answers(state)
        except SlotRefused as error:
            return {**refused, "coupling": Verdict(REJECTED, code="TELEMAC_COUPLING_REFUSED", reason=str(error))}
        body = self.steering
        required = {key for key, slot in body.MODULE_INPUT.items() if slot.is_required}
        required.update({"GEOMETRY_FILE", "BOUNDARY_CONDITIONS_FILE", body.RESULT_KEYWORD})
        required.update(name for name in body.CLOCK if body.slot(name).is_open)
        if body.CADENCE:
            required.add(body.CADENCE)
        for key in sorted(required):
            if own.get(key) not in (None, ""):
                continue
            slot = body.slot(key)
            refused[key] = Verdict(MISSING, code="TELEMAC_SHEET_INCOMPLETE", reason=f"State {slot.keyword}.")
        return refused

    async def prepare(self, state: Fill) -> Deck:
        env = production(state)
        own, coupled, _ = self._answers(state)
        run_id = new_ulid()
        staged: dict[str, Any] = {}
        files = native_files(env.run.get("files"))
        inputs = []
        for name, uri in files.items():
            if not uri.startswith("s3://"):
                raise TelemacError("A deck input must be a relative file with a staged URI.",
                                   error_code="TELEMAC_NATIVE_FILES")
            inputs.append({"dest": name, "gs_uri": uri})

        async def write(sheet: TelemacSheet) -> None:
            result = sheet.stated().get(sheet.body.RESULT_KEYWORD)
            if not result:
                name = sheet.body.slot(sheet.body.RESULT_KEYWORD).keyword
                raise TelemacError(f"State {name} to name the result file.", error_code="TELEMAC_RESULT_UNSTATED")
            results = [native_name(result)]
            for row in sheet.filled.values():
                if row.slot.file_role == "output" and row.value and row.value not in results:
                    results.append(native_name(row.value))
            coupling = sheet.stated().get("COUPLING_WITH")
            staged.update(await stage(
                sheet, run_id=run_id, mesh_inputs=inputs, outputs=[*results, *_ALWAYS_READABLE],
                results=results, prefix=self.engine,
                server_facts={"name": self.name, "utm_epsg": env.run.get("epsg")},
                coupling=str(coupling).split(";")[0].strip().lower() if coupling else None,
                preserve_printouts=True))

        try:
            env.run["sheet"] = await fill_sheet(
                engine=self.engine, floor=lambda *_: (own, coupled), steering=self.steering,
                produced=env.run, params={}, workflow=self.name, title="", keywords={}, write=write)
        except BaseException:
            await asyncio.to_thread(discard_run, run_id, self.engine, stem=self.engine)
            raise
        return Deck(state=state, run_id=run_id, prefix=self.engine, staged=staged)

    async def solve(self, deck: Deck) -> RunResult:
        from .engine import solve_case

        if not deck.staged["case"]["server_facts"].get("utm_epsg"):
            return await self._solve_native(deck, solve_case)
        from ..solver.stop import SolveStopped

        sheet = deck.state.env.run["sheet"]

        async def products(solved: Mapping[str, Any], partial: str | None = None):
            return await publish_outputs(run=solved, outputs=[], captions={}, params={}, partial=partial)

        try:
            solved = await run_sheet(
                sheet=sheet, staged=deck.staged, settled={"name": self.name}, dispatch=_DISPATCH,
                display=sheet.stated().get("ED_RESULT_FILE", ""))
        except SolveStopped as stop:
            await self.stopped(stop, {}, products)
        return RunResult(await products(solved), {"solve": solved})
