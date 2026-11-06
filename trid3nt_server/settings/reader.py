"""The one reader of every setting: resolution across the three levels, the user file, and the run's statements.

A value is the run's if the run states it, else the user file's, else the dictionary's default. Every
read is journaled on the run in progress with the level it resolved at.
"""

from __future__ import annotations

import contextvars
import difflib
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from trid3nt_server.errors import DeclarativeError

if TYPE_CHECKING:
    from trid3nt_server.workflows.runtime.module import Slot

__all__ = [
    "SettingsFileError", "bind_run", "clear_session_run", "dictionary", "entry",
    "input_dir", "listing", "reset_run", "resolve", "session_run", "setting",
    "split_keywords", "state_session_run", "typed", "user_file", "user_values",
    "write_user",
]

#: The body word a run states a setting under: ``keywords={"settings: FILL_MAX_DISTANCE": 200}``.
_QUALIFIER = "settings"


class SettingsFileError(DeclarativeError):
    """The user's settings file cannot be read or written as a mapping of setting names to values."""

    error_code = "SETTINGS_FILE_INVALID"


def input_dir() -> Path:
    """The directory holding the dictionary, which its bounds sidecar sits beside."""
    return Path(__file__).parent / "module_input"


def dictionary() -> "Mapping[str, Slot]":
    """Every setting by identifier, in the dictionary's order."""
    from trid3nt_server.workflows.runtime.module import load_module_input

    return load_module_input("settings", directory=input_dir())


def user_file() -> Path:
    """Where the user's values live: the daemon's config home, never the repo."""
    home = os.environ.get("TRID3NT_HOME")
    return (Path(home) if home else Path.home() / ".trid3nt") / "settings.json"


def entry(name: str) -> "Slot":
    """The entry ``name`` spells, by identifier or keyword in any case, or the refusal offering the nearest."""
    from trid3nt_server.workflows.runtime.module import SlotRefused

    table = dictionary()
    wanted = str(name).strip().upper()
    if wanted in table:
        return table[wanted]
    close = difflib.get_close_matches(wanted, list(table), n=3)
    raise SlotRefused(
        f"there is no setting {name!r}."
        + (f" Did you mean {', '.join(repr(c) for c in close)}?" if close else "")
        + ' describe_keywords(module="settings") lists them.')


def typed(slot: "Slot", value: Any) -> Any:
    checked = slot.check(value)
    if slot.type != "REAL":
        return checked
    return [float(v) for v in checked] if isinstance(checked, list) else float(checked)


_FILE: tuple[Path, bytes, dict[str, Any]] | None = None


def user_values() -> dict[str, Any]:
    """The file's mapping, parsed again only when its bytes changed; an absent file states nothing.

    The bytes are the key, never the file's stamp: two writes inside one clock tick at one size share a stamp."""
    global _FILE
    path = user_file()
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise SettingsFileError(f"{path} is not readable: {exc}.") from exc
    if _FILE is not None and _FILE[:2] == (path, raw):
        return _FILE[2]
    try:
        values = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise SettingsFileError(f"{path} is not readable JSON: {exc}.") from exc
    if not isinstance(values, dict):
        raise SettingsFileError(f"{path} must hold a mapping of setting names to values.")
    _FILE = (path, raw, values)
    return values


def write_user(name: str, value: Any | None) -> Path:
    """Set the user's value of ``name``, or with ``None`` drop it so the default stands -> the file.

    The value is held to the entry's type, choices and range first; the write replaces the file whole."""
    slot = entry(name)
    values = dict(user_values())
    if value is None:
        values.pop(slot.identifier, None)
    else:
        values[slot.identifier] = typed(slot, value)
    path = user_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_suffix(".json.part")
    scratch.write_text(json.dumps(values, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(scratch, path)
    return path


#: What the run in progress states, by identifier: the fill's and the launch's own statements.
_RUN: contextvars.ContextVar[Mapping[str, Any] | None] = contextvars.ContextVar(
    "trid3nt_run_settings", default=None)

#: What a session has bound to its next run, by session id, until a run's record is written.
_SESSION: dict[str, dict[str, Any]] = {}


def bind_run(stated: Mapping[str, Any]) -> contextvars.Token:
    """Make ``stated`` the run's statements until the token resets it."""
    return _RUN.set(stated)


def reset_run(token: contextvars.Token) -> None:
    _RUN.reset(token)


def _session_id() -> str | None:
    from trid3nt_server.render.pipeline_emitter import current_emitter

    emitter = current_emitter()
    return getattr(emitter, "session_id", None) if emitter is not None else None


def state_session_run(session_id: str, name: str, value: Any | None) -> None:
    """Bind ``name`` to this session's next run, or with ``None`` unbind it."""
    slot = entry(name)
    held = _SESSION.setdefault(session_id, {})
    if value is None:
        held.pop(slot.identifier, None)
    else:
        held[slot.identifier] = typed(slot, value)


def session_run(session_id: str) -> dict[str, Any]:
    """What this session has bound to its next run."""
    return dict(_SESSION.get(session_id, {}))


def clear_session_run() -> None:
    """Release what the session in progress bound: its run has been written to the journal."""
    session = _session_id()
    if session is not None:
        _SESSION.pop(session, None)


def _run_stated() -> dict[str, Any]:
    session = _session_id()
    return {**(_SESSION.get(session, {}) if session else {}), **(_RUN.get() or {})}


def resolve(name: str) -> tuple[Any, str]:
    """``(value, level)`` of one setting; the level is ``run``, ``user`` or ``default``. Not journaled."""
    slot = entry(name)
    ident = slot.identifier
    stated = _run_stated()
    if ident in stated:
        return typed(slot, stated[ident]), "run"
    held = user_values()
    if ident in held:
        return typed(slot, held[ident]), "user"
    return slot.engine_default, "default"


def setting(name: str) -> Any:
    """The value in force for ``name``, journaled on the run in progress with its level."""
    slot = entry(name)
    from trid3nt_server.workflows.runtime import journal

    value, level = resolve(slot.identifier)
    journal.setting_read(slot.identifier, value, level)
    return value


def split_keywords(keywords: Mapping[str, Any] | None
                   ) -> tuple[dict[str, Any], dict[str, str], dict[str, Any]]:
    """A run's keyword floor -> ``(settings stated, refusals by name, the rest)``.

    A name under the ``settings`` body (``"settings: FILL_MAX_DISTANCE"``) is a setting; each is held to its entry."""
    from trid3nt_server.workflows.runtime.module import SlotRefused

    stated: dict[str, Any] = {}
    refused: dict[str, str] = {}
    rest: dict[str, Any] = {}
    for name, value in (keywords or {}).items():
        head, sep, tail = str(name).partition(":")
        if not sep or head.strip().lower() != _QUALIFIER:
            rest[name] = value
            continue
        try:
            slot = entry(tail.strip())
            stated[slot.identifier] = typed(slot, value)
        except SlotRefused as exc:
            refused[name] = str(exc)
    return stated, refused, rest


def listing() -> list[dict[str, Any]]:
    """Every setting with what the daemon holds for it: the value in force and its level, the default, range, unit and source."""
    rows = []
    for ident, slot in dictionary().items():
        value, level = resolve(ident)
        rows.append({
            "name": ident, "family": slot.rubrique[0] if slot.rubrique else "",
            "type": slot.type, "value": value, "level": level,
            "default": slot.engine_default, "unit": slot.unit,
            "bounds": list(slot.bounds[0]) if slot.bounds else None,
            "choices": slot.choices, "help": slot.desc, "source": slot.source})
    return rows
