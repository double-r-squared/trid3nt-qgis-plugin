"""The RUN JOURNAL: one append-only JSONL line per completed run.

Decoupled from artifacts on purpose - the record lives in the persistence
directory nothing sweeps, and outlives every artifact it describes.
"""

from __future__ import annotations

import contextvars
import dataclasses
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

logger = logging.getLogger("trid3nt_server.workflows.runtime.journal")

__all__ = ["append_record", "bind_choices", "bind_coverage", "bind_notes",
           "bind_outputs", "cut_coverage", "drain_choices", "drain_coverage",
           "drain_notes", "drain_outputs", "journal_note", "journal_outputs",
           "journal_path", "read_records", "run_choices", "run_coverage",
           "run_origin", "run_outputs", "slot_choice"]

#: Notes from the step now running for THIS run's record; a note that only reached a log line dies with the process.
_NOTES: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "trid3nt_run_notes", default=None)


def journal_note(text: str) -> None:
    """Record ``text`` on the run in progress. Outside a run it only logs.
    For what a run has to SAY rather than return; the note travels to the journal
    and onto the published layer beside the run's own fallback notes."""
    logger.info("run note: %s", text)
    notes = _NOTES.get()
    if notes is not None:
        notes.append(str(text))


#: Layers the run has published, drained into the record: the one place outputs are read back from.
_OUTPUTS: contextvars.ContextVar[list[dict[str, Any]] | None] = contextvars.ContextVar(
    "trid3nt_run_outputs", default=None)


def journal_outputs(layers: Sequence[Mapping[str, Any]]) -> None:
    """Record the layers the run in progress published. Outside a run, a no-op."""
    published = _OUTPUTS.get()
    if published is not None:
        published.extend(dict(layer) for layer in layers)


def bind_outputs() -> contextvars.Token:
    """Open an outputs channel for one plan run -> the token that closes it."""
    return _OUTPUTS.set([])


def drain_outputs(token: contextvars.Token) -> list[dict[str, Any]]:
    """Close the channel and return the layers written into it."""
    published = _OUTPUTS.get() or []
    _OUTPUTS.reset(token)
    return list(published)


def bind_notes() -> contextvars.Token:
    """Open a note channel for one plan run -> the token that closes it."""
    return _NOTES.set([])


def drain_notes(token: contextvars.Token) -> list[str]:
    """Close the channel and return what was written into it."""
    notes = _NOTES.get() or []
    _NOTES.reset(token)
    return list(notes)

#: What the cut covers (share of water and land painted, what nothing measured); the card reads it before the solve, the journal after.
_COVERAGE: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "trid3nt_run_coverage", default=None)


def cut_coverage(text: str) -> None:
    """State what the cut covers, on the journal AND on the run's own card."""
    journal_note(text)
    covered = _COVERAGE.get()
    if covered is not None:
        covered.append(str(text))


def run_coverage() -> list[str]:
    """What the run in progress has said its cut covers, in the order it said it."""
    return list(_COVERAGE.get() or ())


def bind_coverage() -> contextvars.Token:
    """Open a coverage channel for one plan run -> the token that closes it."""
    return _COVERAGE.set([])


def drain_coverage(token: contextvars.Token) -> list[str]:
    """Close the channel and return what was written into it."""
    covered = _COVERAGE.get() or []
    _COVERAGE.reset(token)
    return list(covered)


#: The ranked list each matched slot was filled from; card and record read one object.
_CHOICES: contextvars.ContextVar[list[Any] | None] = contextvars.ContextVar(
    "trid3nt_run_choices", default=None)


def slot_choice(choice: Any) -> None:
    """Record the list one slot was filled from. Outside a run, a no-op."""
    chosen = _CHOICES.get()
    if chosen is not None:
        chosen.append(choice)


def run_choices() -> list[Any]:
    """Every list the run in progress has filled a slot from, in slot order."""
    return list(_CHOICES.get() or ())


def bind_choices() -> contextvars.Token:
    """Open a choices channel for one plan run -> the token that closes it."""
    return _CHOICES.set([])


def drain_choices(token: contextvars.Token) -> list[Any]:
    """Close the channel and return the lists written into it."""
    chosen = _CHOICES.get() or []
    _CHOICES.reset(token)
    return list(chosen)


#: Env var a DRIVER sets to label its runs, so telemetry never learns a default from a canary's pinned window.
ORIGIN_ENV = "TRID3NT_RUN_ORIGIN"

_FILENAME = "run_journal.jsonl"


def journal_path() -> Path:
    """Where the journal lives - the persistence dir, which no sweep touches."""
    from trid3nt_server.store.cases import _default_dev_persistence_dir

    root = os.environ.get("TRID3NT_DEV_PERSISTENCE_DIR") or _default_dev_persistence_dir()
    return Path(root) / _FILENAME


def run_origin(*, live_session: bool) -> str:
    """Where this run came from: a labelled driver, a live session, or headless."""
    declared = (os.environ.get(ORIGIN_ENV) or "").strip()
    if declared:
        return declared
    return "session" if live_session else "headless"


def append_record(record: Mapping[str, Any]) -> Path | None:
    """Append one run record. Best-effort: a journal write never fails a run,
    because the run already happened and its products already exist."""
    path = journal_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, default=str, separators=(",", ":"))
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception:  # noqa: BLE001 - journalled, never propagated
        logger.warning("run journal write failed for %s", record.get("run_id"),
                       exc_info=True)
        return None
    return path


def read_records(path: Path | None = None) -> list[dict[str, Any]]:
    """Every record on file, oldest first. A malformed line is skipped, not fatal."""
    target = path or journal_path()
    if not target.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            logger.warning("run journal: skipping a malformed line in %s", target)
    return out


def build_record(*, run_id: str | None, engine: str | None,
                 module: str | None,
                 sheet: Sequence[Any],
                 provenance: Sequence[Any], result: Any,
                 wall_seconds: float | None, origin: str,
                 notes: Sequence[str],
                 fill: Mapping[str, Mapping[str, Any]] | None = None,
                 correct_end: bool | None = None,
                 keywords: Mapping[str, Any] | None = None,
                 supplied: Mapping[str, Any] | None = None,
                 sources: Sequence[Any] = (),
                 outputs: Sequence[Mapping[str, Any]] = (),
                 mesh: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """One run record, from what the publish stage already holds."""
    return {
        "run_id": run_id,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "engine": engine,
        "module": module,
        "origin": origin,
        "sheet": [_row(row) for row in sheet],
        # The raw keyword floor is no Param and on no sheet row; a reproduction from arguments alone would run a different deck.
        "keywords": {k: _small(v) for k, v in (keywords or {}).items()},
        # Slots handed to the run are part of the invocation and on no param sheet.
        "supplied": {k: _small(v) for k, v in (supplied or {}).items()},
        # Which source filled each matched slot and why: the pick and its reason as the card showed them.
        "sources": {choice.slot: {"picked": choice.picked,
                                  "reason": choice.sentence}
                    for choice in sources},
        "provenance": [_provenance(row) for row in provenance],
        # Each slot of the solved deck: value and origin; the template survives here and nowhere else on the line.
        "fill": {name: {"value": _small(row.get("value")), "from": row.get("from")}
                 for name, row in (fill or {}).items()},
        "correct_end": correct_end,
        # The mesh content key, and on a reuse the run that built it.
        "mesh": {"mesh_size_m": getattr(result, "mesh_size_m", None),
                 **{k: mesh.get(k) if isinstance(mesh, Mapping) else None
                    for k in ("key", "built_by")}},
        "cores": next((r.value for r in sheet
                       if getattr(r, "name", "") == "cores"), None),
        "wall_seconds": wall_seconds,
        "notes": list(notes),
        # What the run put on the map: the one place outputs are read back once artifacts and session are gone.
        "outputs": [dict(layer) for layer in outputs],
    }


def _row(row: Any) -> dict[str, Any]:
    """One resolved param with its door and basis; a pinned value and a dataset-answered one are different evidence."""
    return {
        "name": getattr(row, "name", None),
        "value": _small(getattr(row, "value", None)),
        "door": getattr(row, "door", None),
        "basis": getattr(row, "basis", None),
        "units": getattr(row, "units", None),
        "consequence": getattr(row, "consequence", None),
        "note": getattr(row, "note", "") or None,
        "real_source": getattr(row, "real_source", None),
    }


def _provenance(row: Any) -> dict[str, Any]:
    return {
        "param": getattr(row, "param", None),
        "value": _small(getattr(row, "value", None)),
        "basis": getattr(row, "basis", None),
        "note": getattr(row, "note", None),
        "real_source": getattr(row, "real_source", None),
    }


#: Elements of a list-valued field the record keeps: the fact and shape of a curve, not a copy.
_LIST_CAP = 32


def _small(value: Any) -> Any:
    if isinstance(value, (list, tuple)) and len(value) > _LIST_CAP:
        return {"length": len(value), "head": list(value[:8]),
                "truncated": True}
    if isinstance(value, (list, tuple)):
        return [_small(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    # A slot's value is written as the shape that slot ingests, at full precision: a rounded coordinate is a different place.
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {key: _small(field) for key, field
                in dataclasses.asdict(value).items()}
    return str(value)


def run_outputs(run_id: str) -> list[dict[str, Any]]:
    """The layers the named run published, off its own record; ``[]`` when the
    journal has no line for it. The record is APPEND-ONLY, so the last line for
    a run id is the one that stands."""
    for record in reversed(read_records()):
        if str(record.get("run_id") or "") == str(run_id):
            published = record.get("outputs")
            return [dict(row) for row in published if isinstance(row, dict)] \
                if isinstance(published, list) else []
    return []
