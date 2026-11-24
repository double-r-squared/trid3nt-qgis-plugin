"""Shared registration and keyword lookup for a bare engine declaration."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

from pydantic import BaseModel, Field
from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.payload_warning import ParamSheet, ParamSheetRow
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt.errors import DeclarationError
from trid3nt.inputs.input import Input
from trid3nt.tools.cache import read_object_bytes_s3

from .module import identify_on, keyword_floor
from .review import _entries
from .sheet import Keyword, run as solve_staged
from .workflow import Deck, RunResult, Workflow, register_workflow
from ..solver.stop import SolveStopped

NATIVE_FILES = Input(type=dict, desc="Native input files by their deck names and staged URIs.").stated()
NATIVE_EPSG = Input(type=int, desc="EPSG code of the native grid where its coordinate system is known.").stated()


class NativeResult(BaseModel):
    """The finished native run and its primary readable result."""

    run_id: str
    uri: str
    notes: list[str] = Field(default_factory=list)
    synthetic_inputs: list[SyntheticInput] = Field(default_factory=list)


class BareWorkflow(Workflow):
    bare = True
    solve_step = "solve"

    @property
    def steering(self) -> type:
        return self.template.STEERING

    def wrappers(self) -> tuple[type, ...]:
        return (self.steering,)

    def input(self, name: str) -> Any:
        declared = next((row for row in self.inputs if row.name == name), None)
        if declared is not None:
            return declared
        bodies = self.wrappers() if ":" in name else (self.steering,)
        body, identifier = identify_on(bodies, name)
        return Keyword(body, identifier, body is self.steering)

    def bodies(self) -> tuple[str, ...]:
        return tuple(body.MODULE for body in self.wrappers())

    def floor(self, keywords: Mapping[str, Any]) -> tuple[dict, dict]:
        return keyword_floor(self.wrappers(), {
            name if ":" in name else f"{self.steering.MODULE}: {name}": value
            for name, value in keywords.items()})

    def open_inputs(self, case: Any, values: Mapping[str, Any]) -> tuple[dict, dict, dict]:
        """Pin any inputs supplied as local files before the case model is built."""
        if "files" not in values:
            return dict(values), {}, {}
        files, pins = {}, {}
        for name, source in native_files(values["files"]).items():
            files[name], pins[f"files:{name}"] = case.take(source)
        return {**values, "files": files}, pins, {}

    async def _solve_native(self, deck: Deck, dispatch: Callable[..., Any]) -> RunResult:
        async def products(handle: Mapping[str, Any], partial: str | None = None) -> NativeResult:
            return NativeResult(run_id=handle["run_id"], uri=handle["uri"])

        try:
            handle = await solve_staged(deck.staged, dispatch=dispatch)
        except SolveStopped as stop:
            await self.stopped(stop, {}, products)
        return RunResult(await products(handle), {"solve": handle})

    async def review_native(self, deck: Deck) -> Deck:
        from trid3nt.inputs.review import gate_input_review

        def shown(value: Any) -> Any:
            if isinstance(value, (list, tuple)):
                return f"{len(value)} entries"
            if isinstance(value, Mapping):
                return f"{len(value)} named entries"
            return value

        rows = [ParamSheetRow(name=name, value=shown(verdict.value), origin=verdict.origin, editable=False)
                for name, verdict in deck.state.inputs.items() if verdict.value is not None]
        sheet = ParamSheet(workflow=self.name, title=f"Review the {self.name} deck", rows=rows)
        answer = await gate_input_review(tool_name=self.name, entries=_entries(rows), params={}, param_sheet=sheet)
        if not answer.proceed:
            raise DeclarationError(answer.cancel_reason or "The native deck was declined.",
                                   error_code="NO_SESSION" if answer.cancel_code == "no_session"
                                   else "USER_INPUT_CANCELLED")
        return deck


def native_name(name: Any) -> str:
    """A relative deck name that has the same resolution on the runner."""
    if not isinstance(name, str):
        raise DeclarationError("Native file names must be strings.", error_code="NATIVE_FILE_NAME")
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or ".." in path.parts or path.as_posix() != name or not all(
            re.fullmatch(r"[A-Za-z0-9 _.-]+", part) for part in path.parts):
        raise DeclarationError("Native file names must stay within the staged directory.", error_code="NATIVE_FILE_NAME")
    if path.as_posix() == "manifest.json":
        raise DeclarationError("The runner owns manifest.json.", error_code="NATIVE_FILE_NAME")
    return name


def native_files(files: Any) -> dict[str, str]:
    if files is None:
        return {}
    if not isinstance(files, Mapping) or not all(isinstance(value, str) for value in files.values()):
        raise DeclarationError("files must map deck filenames to their sources.", error_code="NATIVE_FILES")
    return {native_name(name): source for name, source in files.items()}


def native_assets(work: Path, files: dict[str, str]) -> None:
    """Copy the caller's staged files into their declared positions."""
    for name, uri in files.items():
        if not uri.startswith("s3://"):
            raise DeclarationError("The input files must be case files or staged object URIs.",
                                   error_code="NATIVE_FILES")
        dest = work / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(read_object_bytes_s3(uri))


def register_bare(workflow: type[BareWorkflow], name: str, declaration: Any) -> Any:
    return register_workflow(
        workflow, AtomicToolMetadata(name=name, engine=workflow.engine, ttl_class="live-no-cache",
                                     source_class="workflow_dispatch", tier="template"),
        declaration)
