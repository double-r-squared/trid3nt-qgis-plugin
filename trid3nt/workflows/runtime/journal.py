"""The RUN JOURNAL's write side: the channels a run fills, and its one line appended when it completes.

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

from trid3nt.store import journal as store_journal

logger = logging.getLogger("trid3nt.workflows.runtime.journal")

__all__ = ["append_record", "bind_choices", "bind_outputs", "bind_settings",
           "drain_choices", "drain_outputs", "drain_settings", "journal_outputs",
           "run_choices", "run_origin", "setting_read", "slot_choice"]

#: Layers the run has published, drained into the record: the one place outputs are read back from.
_OUTPUTS: contextvars.ContextVar[list[dict[str, Any]] | None] = contextvars.ContextVar(
    "trid3nt_run_outputs", default=None)


def journal_outputs(layers: Sequence[Mapping[str, Any]]) -> None:
    """Record the layers the run in progress published. Outside a run, a no-op."""
    published = _OUTPUTS.get()
    if published is not None:
        published.extend(dict(layer) for layer in layers)


def bind_outputs(held: Sequence[Mapping[str, Any]] = ()) -> contextvars.Token:
    """Open an outputs channel for one run, holding what it already published -> the token that closes it."""
    return _OUTPUTS.set([dict(layer) for layer in held])


def drain_outputs(token: contextvars.Token) -> list[dict[str, Any]]:
    """Close the channel and return the layers written into it."""
    published = _OUTPUTS.get() or []
    _OUTPUTS.reset(token)
    return list(published)


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


def bind_choices(held: Sequence[Any] = ()) -> contextvars.Token:
    """Open a choices channel for one run, holding the lists it already filled from -> the token that closes it."""
    return _CHOICES.set(list(held))


def drain_choices(token: contextvars.Token) -> list[Any]:
    """Close the channel and return the lists written into it."""
    chosen = _CHOICES.get() or []
    _CHOICES.reset(token)
    return list(chosen)


#: The settings the run has read, by name: the value in force and the level it resolved at.
_SETTINGS: contextvars.ContextVar[dict[str, dict[str, Any]] | None] = contextvars.ContextVar(
    "trid3nt_run_settings_read", default=None)


def setting_read(name: str, value: Any, level: str) -> None:
    """Record that the run in progress read ``name``. Outside a run, a no-op."""
    read = _SETTINGS.get()
    if read is not None:
        read[name] = {"value": value, "level": level}


def bind_settings(held: Mapping[str, dict[str, Any]] | None = None) -> contextvars.Token:
    """Open a settings-read channel for one run, holding what it already read -> the token that closes it."""
    return _SETTINGS.set(dict(held or {}))


def drain_settings(token: contextvars.Token) -> dict[str, dict[str, Any]]:
    """Close the channel and return the settings read."""
    read = _SETTINGS.get() or {}
    _SETTINGS.reset(token)
    return dict(read)


#: Env var a DRIVER sets to label its runs, so telemetry never learns a default from a canary's pinned window.
ORIGIN_ENV = "TRID3NT_RUN_ORIGIN"

def run_origin(*, live_session: bool) -> str:
    """Where this run came from: a labelled driver, a live session, or headless."""
    declared = (os.environ.get(ORIGIN_ENV) or "").strip()
    if declared:
        return declared
    return "session" if live_session else "headless"


def append_record(record: Mapping[str, Any]) -> Path | None:
    """Append one run record. Best-effort: a journal write never fails a run,
    because the run already happened and its outputs already exist."""
    from trid3nt.store.objects import case_relative

    path = store_journal.journal_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # A case directory's record names what it read and wrote by the path relative to the case, so the case moves whole.
        line = json.dumps(case_relative(json.loads(json.dumps(record, default=str))), separators=(",", ":"))
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception:  # noqa: BLE001 - journalled, never propagated
        logger.warning("run journal write failed for %s", record.get("run_id"),
                       exc_info=True)
        return None
    return path


def build_record(*, run_id: str | None, engine: str | None,
                 module: str | None,
                 template: str | None = None,
                 template_path: str | None = None,
                 template_sha256: str | None = None,
                 template_upstream_sha256: str | None = None,
                 case: str | None = None,
                 model: str | None = None,
                 version: str | None = None,
                 strict: bool | None = None,
                 inputs: Mapping[str, Mapping[str, Any]],
                 result: Any,
                 wall_seconds: float | None, origin: str,
                 notes: Sequence[str],
                 correct_end: bool | None = None,
                 outputs: Sequence[Mapping[str, Any]] = (),
                 mesh: Mapping[str, Any] | None = None,
                 settings: Mapping[str, Mapping[str, Any]] = {},
                 stopped: str | None = None,
                 check_steps: int | None = None,
                 study: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """One run record, from what the publish stage already holds.
    ``stopped`` is the sentence of a run the user stopped, whose outputs are partial; none on a run that ended.
    ``check_steps`` is the step count a check run stopped its solve at; none on a full-length run.
    ``study`` names the study whose best parameters the run was solved with, and keeps its last ensemble or its iterations; none on any other run.
    ``template`` is the workflow that ran, ``case`` the case it ran in; ``model`` and ``version`` name the SDK model it was
    filled from and the hash of that model's stated values, none on a call; ``strict`` whether an unresolved coverage
    shortfall refused."""
    return {
        "run_id": run_id,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "engine": engine,
        "module": module,
        "template": template,
        **({"template_path": template_path, "template_sha256": template_sha256,
            "template_upstream_sha256": template_upstream_sha256} if template_path is not None else {}),
        "case": case,
        "model": model,
        "version": version,
        "strict": strict,
        "origin": origin,
        # THE ONE MAP: every input of the run by name - a stated value, a keyword of the solved deck, a supplied or
        # matched row, an absent optional one the run derived - with who stated it and the source that names it.
        # A reproduction reads the arguments here.
        # An ingested reading also keeps its published accuracy and its gauge zero's, where the observation carries them.
        "inputs": {name: {"value": _small(row.get("value")),
                          "origin": row.get("origin"),
                          "source": row.get("source"),
                          **{k: dict(row[k]) for k in ("accuracy", "zero_accuracy") if row.get(k)}}
                   for name, row in inputs.items()},
        "correct_end": correct_end,
        "stopped": stopped,
        "check_steps": check_steps,
        # The mesh content key, and on a reuse the run that built it.
        "mesh": {"mesh_size_m": getattr(result, "mesh_size_m", None),
                 **{k: mesh.get(k) if isinstance(mesh, Mapping) else None
                    for k in ("key", "built_by")}},
        "wall_seconds": wall_seconds,
        "notes": list(notes),
        # What the run put on the map: the one place outputs are read back once artifacts and session are gone.
        "outputs": [dict(layer) for layer in outputs],
        # The settings the run read: name, the value in force and the level it resolved at (default, user or run).
        "settings": [{"name": name, "value": _small(row.get("value")), "level": row.get("level")}
                     for name, row in sorted(settings.items())],
        **({"study": dict(study)} if study else {}),
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
