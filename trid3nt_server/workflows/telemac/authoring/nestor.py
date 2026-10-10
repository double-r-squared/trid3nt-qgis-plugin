"""NESTOR's three input files, as content: the actions, the polygons naming their fields, and the
surface reference their levels are read from.

A field is identified by its name's three leading numerals alone and named once file-wide; the
polygon file closes with ENDFILE at column one, the reference with END; times are absolute dates
differenced against the host deck's time origin.
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Any, Mapping, Sequence

__all__ = ["ACTION_FILENAME", "POLYGON_FILENAME", "REFERENCE_FILENAME", "files"]

# The GAIA deck's NESTOR ACTION / POLYGON / SURFACE REFERENCE FILE statements carry these names.
ACTION_FILENAME = "nestor_actions.dat"
POLYGON_FILENAME = "nestor_polygons.dat"
REFERENCE_FILENAME = "nestor_reference.dat"

# The only clock the action reader takes: exactly 19 characters, against ORIGINAL DATE/HOUR OF TIME.
_STAMP = "%Y.%m.%d-%H:%M:%S"

# A field name's first three characters must be numerals; ids start past the two-digit space.
_FIRST_FIELD_ID = 101

# The reader needs more than one profile line.
_MIN_PROFILES = 2

# Every field the engine reads per action type, in the examples' order: value key -> the reader's keyword. An action states only its own type's keys.
_ACTION_KEYS: Mapping[str, tuple[tuple[str, str], ...]] = {
    "Dig_by_time": (
        ("type", "ActionType"), ("field", "FieldDig"), ("start", "TimeStart"), ("end", "TimeEnd"),
        ("volume", "DigVolume"), ("dump", "FieldDump"),
        ("dump_rate", "DumpRate")),
    "Dig_by_criterion": (
        ("type", "ActionType"), ("field", "FieldDig"), ("level", "ReferenceLevel"),
        ("start", "TimeStart"), ("repeat", "TimeRepeat"), ("end", "TimeEnd"),
        ("rate", "DigRate"), ("depth", "DigDepth"), ("crit_depth", "CritDepth"),
        ("min_volume", "MinVolume"),
        ("min_volume_radius", "MinVolumeRadius"),
        ("dump", "FieldDump"), ("dump_rate", "DumpRate")),
    "Dump_by_time": (
        ("type", "ActionType"), ("field", "FieldDump"), ("start", "TimeStart"), ("end", "TimeEnd"),
        ("volume", "DumpVolume")),
    "Backfill_to_level": (
        ("type", "ActionType"), ("field", "FieldDump"), ("level", "ReferenceLevel"),
        ("start", "TimeStart"), ("end", "TimeEnd"),
        ("crit_depth", "CritDepth")),
    "Save_water_level": (
        ("type", "ActionType"), ("start", "TimeStart"), ("level", "ReferenceLevel")),
}

# Keys whose value is an instant, stamped as a date against the run's origin.
_TIMES = frozenset(("start", "end"))
# Keys whose value is a field, written as the name the polygon file gives it.
_FIELDS = frozenset(("field", "dump"))


class NestorRefused(ValueError):
    """A dredge the engine's own readers would refuse, named before it is written."""


def files(actions: Sequence[Mapping[str, Any]], *,
          reference: Sequence[Sequence[float]],
          origin: Sequence[int]) -> dict[str, str]:
    """The three files a dredge is, by basename -> content.

    Written together so action and polygon names cannot disagree.
    """
    named = _named_fields(actions)
    return {ACTION_FILENAME: _action_text(actions, named=named, origin=origin),
            POLYGON_FILENAME: _polygon_text(named),
            REFERENCE_FILENAME: _reference_text(reference)}


def _named_fields(actions: Sequence[Mapping[str, Any]]
                  ) -> list[tuple[str, Mapping[str, Any]]]:
    """A field may be named once across the file (the reader refuses a repeat, as dig or dump); a repeated label refuses by label."""
    named: list[tuple[str, Mapping[str, Any]]] = []
    seen: set[str] = set()
    for action in actions:
        for key in ("field", "dump"):
            field = action.get(key)
            if field is None:
                continue
            label = str(field["label"])
            if label in seen:
                raise NestorRefused(
                    f"the field {label!r} is named by more than one action. "
                    "NESTOR identifies a field by the three numerals its name "
                    "opens with and refuses a file that references one twice, "
                    "as dig or as dump; give each action its own field.")
            seen.add(label)
            named.append((f"{_FIRST_FIELD_ID + len(named):03d}_{_slug(label)}",
                          field))
    if not named:
        raise NestorRefused(
            "a dredge names no field. Every action operates on a polygon the "
            "polygon file declares; supply the area to dig or dump into.")
    return named


def _slug(label: str) -> str:
    """Only the leading numerals are read."""
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", str(label)).strip("_")
    return cleaned or "field"


def _action_text(actions: Sequence[Mapping[str, Any]], *,
                 named: Sequence[tuple[str, Mapping[str, Any]]],
                 origin: Sequence[int]) -> str:
    names = {id(field): name for name, field in named}
    start = _origin(origin)
    # RESTART is demanded at least once and is FALSE: no restart file is authored, and TRUE would send the reader to a file the deck does not state.
    lines = ["/ NESTOR actions", "RESTART = FALSE"]
    for action in actions:
        kind = str(action["type"])
        lines.append("ACTION")
        for key, keyword in _ACTION_KEYS[kind]:
            value = action.get(key)
            if value is None:
                continue
            lines.append(f"   {keyword} = {_value(key, value, names, start)}")
        for fraction in action.get("grain_class") or ():
            lines.append(f"   GrainClass = {float(fraction):.6f}")
        lines.append("ENDACTION")
    # ENDFILE is read before the line is left-adjusted, so it stands at column one.
    lines.append("ENDFILE")
    return "\n".join(lines) + "\n"


def _value(key: str, value: Any, names: Mapping[int, str],
           start: _dt.datetime) -> str:
    if key == "type":
        return str(value)
    if key in _FIELDS:
        return names[id(value)]
    if key in _TIMES:
        return (start + _dt.timedelta(seconds=float(value))).strftime(_STAMP)
    if key == "level":
        return str(value)
    return f"{float(value):.6f}"


def _origin(origin: Sequence[int]) -> _dt.datetime:
    year, month, day, hour, minute, second = (int(v) for v in origin)
    return _dt.datetime(year, month, day, hour, minute, second)


def _polygon_text(named: Sequence[tuple[str, Mapping[str, Any]]]) -> str:
    """The name is read from column six, so nothing pads the keyword."""
    lines: list[str] = []
    for name, field in named:
        lines.append(f"NAME:{name}")
        vertices = [(float(x), float(y)) for x, y in field["vertices"]]
        if len(vertices) < 3:
            raise NestorRefused(
                f"the field {name!r} carries {len(vertices)} vertices, which "
                "bounds no area to dig or dump in.")
        lines += [f"{x:16.3f} {y:16.3f}" for x, y in vertices]
    lines.append("ENDFILE")
    return "\n".join(lines) + "\n"


def _reference_text(reference: Sequence[Sequence[float]]) -> str:
    """Read unconditionally by the criterion dig and the backfill."""
    rows = [[float(v) for v in row] for row in reference or ()]
    if len(rows) < _MIN_PROFILES or any(len(row) != 7 for row in rows):
        raise NestorRefused(
            f"the surface reference carries {len(rows)} profile(s) of "
            f"{sorted({len(row) for row in rows})} value(s); the reader takes "
            "more than one line of seven reals (xL yL zL xR yR zR km).")
    # The reader skips lines opening with '#'; the column names go there.
    lines = ["#-- xL yL zL xR yR zR km"]
    lines += [" ".join(f"{v:16.4f}" for v in row) for row in rows]
    lines.append("END")
    return "\n".join(lines) + "\n"
