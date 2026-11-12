"""A FINISHED RUN of this case, read off its record and the deck it staged.

A run is named by its run id or by a handle the case shows for one of its layers. It is this case's
when a layer the case holds lies under its prefix, finished when its record says it ended at its own
correct end, and readable while the deck it staged is still held where it was staged.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

__all__ = ["FinishedRun", "FinishedRunError", "case_run", "run"]

#: A run id as the runtime mints one.
_RUN_ID = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


class FinishedRunError(RuntimeError):
    """A typed refusal: the named run is not a finished run of this case that this run can read."""

    error_code: str
    retryable: bool = False

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True)
class FinishedRun:
    """One finished run: its record, the case section and files its deck was staged with, and its mesh.

    ``staged`` is each staged file as the manifest lists it (``gs_uri``, ``dest``); ``mesh`` the accepted
    mesh's record the run solved on; ``bbox`` that mesh's lon/lat box."""

    run_id: str
    engine: str
    module: str | None
    record: Mapping[str, Any]
    case: Mapping[str, Any]
    staged: tuple[Mapping[str, str], ...]
    outputs: tuple[str, ...]
    mesh: Mapping[str, Any] | None = None
    bbox: tuple[float, float, float, float] | None = None
    layers: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)


def _named(value: Any, label: str) -> str:
    """The run id ``value`` names, read off the case's layers; a run no layer of the case lies under refuses."""
    from trid3nt_server.render.uri_registry import case_layers, lookup_uri_for_handle
    from trid3nt_server.store import objects as storage

    text = str(value or "").strip()
    held = f"s3://{storage.runs_bucket()}/"
    uri = lookup_uri_for_handle(text) if text else None
    if uri and uri.startswith(held):
        return uri[len(held):].split("/", 1)[0]
    if _RUN_ID.match(text) and any(str(layer.get("uri") or "").startswith(f"{held}{text}/")
                                   for layer in case_layers()):
        return text
    raise FinishedRunError(
        "RUN_NOT_IN_CASE",
        f"{label} names {text!r}, and no layer this case holds is one of that run's: name a run of this "
        "case by its run id or by the handle the case shows for one of its layers.")


def _finished(record: Mapping[str, Any]) -> bool:
    return not (record.get("stopped") or record.get("check_steps") or record.get("correct_end") is False)


def _case_runs(engines: Sequence[str]) -> list[Mapping[str, Any]]:
    """The record of every finished run of ``engines`` a layer of this case lies under, newest first."""
    from trid3nt_server.render.uri_registry import case_layers
    from trid3nt_server.store import objects as storage
    from trid3nt_server.store.journal import read_records

    held = f"s3://{storage.runs_bucket()}/"
    ids = {str(layer.get("uri") or "")[len(held):].split("/", 1)[0] for layer in case_layers()
           if str(layer.get("uri") or "").startswith(held)}
    found: dict[str, Mapping[str, Any]] = {}
    for record in reversed(read_records()):
        run_id = str(record.get("run_id") or "")
        if run_id in ids and run_id not in found and _finished(record) \
                and (not engines or str(record.get("engine") or "") in engines):
            found[run_id] = record
    return list(found.values())


async def case_run(engines: Sequence[str], *, label: str = "run", tool: str = "") -> str:
    """The run id of this case's finished run of ``engines`` where none is named: the one there is, taken; of
    several, the one confirmed on the review card, which lists them newest first and offers the newest (AUTO
    takes it); none refuses naming the engines. What was taken is said on the run."""
    import asyncio

    from trid3nt_contracts.common import Origin, SyntheticInput

    from trid3nt_server.inputs.gate.input_review import gate_input_review
    from trid3nt_server.inputs.run_notes import journal_note

    found = await asyncio.to_thread(_case_runs, tuple(engines))
    kinds = " or ".join(engines) or "any engine"
    if not found:
        raise FinishedRunError(
            "RUN_NONE_IN_CASE",
            f"{label} is unstated, and this case holds no finished run of {kinds}, what {label} takes a "
            "run of: finish one first, or name a run by its run id or the handle the case "
            "shows for one of its layers.")
    chosen = str(found[0]["run_id"])
    if len(found) > 1:
        outcome = await gate_input_review(
            tool_name=tool or label,
            entries=[SyntheticInput(param=str(record["run_id"]),
                                    value=f"{record.get('engine')} run of {record.get('origin') or 'a template'}",
                                    basis=Origin.PRODUCER, note=f"recorded {record.get('recorded_at') or ''}".strip())
                     for record in found],
            params={label: chosen})
        if not outcome.proceed:
            raise FinishedRunError("RUN_NOT_CHOSEN", f"{label} was not chosen: "
                                   f"{outcome.cancel_reason or 'declined'}")
        chosen = str(outcome.params.get(label) or chosen)
    journal_note(f"{label} is unstated, so it is this case's finished run {chosen}"
                 + (f", the newest of the {len(found)} it holds of {kinds}"
                    if len(found) > 1 and chosen == str(found[0]["run_id"]) else
                    f", one of the {len(found)} it holds" if len(found) > 1 else
                    f", the one finished run of {kinds} it holds"))
    return chosen


async def run(value: Any, *, label: str = "run", engines: Sequence[str] = ()) -> FinishedRun:
    """A run id or a layer handle -> the finished run it names, or the refusal that says why it cannot be read.

    ``engines`` are the engines the reader takes a run of; a run of another engine refuses naming them."""
    from trid3nt_server.store import objects as storage
    from trid3nt_server.tools.mesh.step import kept_mesh, mesh_record
    from trid3nt_server.store.journal import read_records

    if isinstance(value, FinishedRun):
        return value
    run_id = _named(value, label)
    record = next((r for r in reversed(read_records()) if str(r.get("run_id") or "") == run_id), None)
    if record is None:
        raise FinishedRunError(
            "RUN_NOT_FINISHED",
            f"run {run_id} has no record: a run is recorded when it ends, so it has not finished.")
    if not _finished(record):
        why = (f"it was stopped ({record['stopped']})" if record.get("stopped") else
               f"it is a check run stopped after {record['check_steps']} engine steps"
               if record.get("check_steps") else "its solver did not reach its own correct end")
        raise FinishedRunError("RUN_NOT_FINISHED", f"run {run_id} did not finish: {why}.")
    engine = str(record.get("engine") or "")
    if engines and engine not in engines:
        raise FinishedRunError(
            "RUN_ENGINE_UNTAKEN",
            f"run {run_id} is a {engine or 'engineless'} run, and {label} takes a run of "
            f"{' or '.join(engines)}.")
    bucket = (os.environ.get("TRID3NT_CACHE_BUCKET") or "").strip()
    try:
        manifest = json.loads(storage.client().get_object(
            Bucket=bucket, Key=f"{engine}/{run_id}/manifest.json")["Body"].read())
    except Exception as exc:  # noqa: BLE001 - any miss means the deck is not held
        raise FinishedRunError(
            "RUN_DECK_GONE",
            f"the deck run {run_id} staged is no longer held where it was staged ({exc}), so it cannot "
            "be read again: run it again and name the new run.") from exc
    key = (record.get("mesh") or {}).get("key")
    art = await kept_mesh(str(key)) if key else None
    if art is None:
        raise FinishedRunError(
            "RUN_MESH_GONE",
            f"the mesh run {run_id} solved on is not kept under a key of its own, so what it solved "
            "cannot be placed again: run it again and name the new run.")
    return FinishedRun(
        run_id=run_id, engine=engine, module=record.get("module"), record=record,
        case=dict(manifest.get("case") or {}), staged=tuple(dict(row) for row in manifest.get("inputs") or ()),
        outputs=tuple(manifest.get("outputs") or ()), mesh=mesh_record(art),
        bbox=tuple(float(v) for v in art.bbox) if art.bbox else None,
        layers=tuple(dict(row) for row in record.get("outputs") or ()))
