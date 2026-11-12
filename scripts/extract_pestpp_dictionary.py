#!/usr/bin/env python3
"""Extract the PEST++ keyword dictionary out of the 5.2.27 users manual and the 5.2.27 binary's own record.

The manual's control-variable tables (and its run-management and control-data prose) name each option,
its type, its default and its role; the binary's record of a parse-only study prints the value each
option takes when nothing states it, and the record of a parse-only study stating every name says which
names the binary does not read. A row is a name the binary reads, its default the binary's own where it
prints one; the rows this writes are generated data, never hand edited.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

#: The release the worker image carries (``workers/pestpp/Dockerfile``), the commit its tag names, and the
#: manual's git blob at that commit.
PESTPP_VERSION = "5.2.27"
TAG_COMMIT = "5d49814962531a0f0400cf75d0f9442c0504c714"
MANUAL_BLOB = "3a30e4de6bd85dc9d3147dd0fb68b6b23f6fc43a"
MANUAL_URL = (f"https://raw.githubusercontent.com/pestpp/pestpp/{PESTPP_VERSION}/"
              "documentation/pestpp_users_manual.md")

#: The binary's records, recorded in the worker image over one tiny parse-only study: stating nothing
#: (``control.rec``, every control-data variable printed at the value it takes unstated), stating
#: ``noptmax 1`` (``pestpp.rec``, every ``++`` option so printed; an unstated noptmax of 0 resets
#: max_run_fail) and stating every name with ``forgive_unknown_args`` (``names.rec`` over
#: ``names.pst``, each name it does not read warned).
RECORDS = ROOT / "tests" / "fixtures" / "pestpp" / "defaults"

OUT = ROOT / "trid3nt_server" / "workflows" / "pestpp" / "modules" / "module_input" / "pestpp.json"

#: Each table's section and the tool reading the options it lists; an option in several tables is read by each.
_TABLES = {"s10-2-3": "pestpp-glm", "s11-2-3": "pestpp-sen", "s11-3-2": "pestpp-sen", "s12-4": "pestpp-opt",
           "s13-4": "pestpp-ies", "s14-3": "pestpp-swp", "s15-3-3": "pestpp-da", "s16-3-3": "pestpp-mou",
           "s17-3-3": "pestpp-sqp"}
#: The run manager's options, described one paragraph each, read by every tool.
_RUN_MANAGEMENT = "s9-3-5"
#: The control-data section, whose variables the keyword control file states by name.
_CONTROL_DATA = "s8-6-1"
#: Control-data variables the study's authoring writes itself: counts of what the control file lists.
_COUNTS = frozenset({"numcom"})

_TYPES = {
    "boolean": "LOGICAL", "bool": "LOGICAL",
    "text": "STRING", "string": "STRING",
    "real": "REAL", "double": "REAL", "float": "REAL", "real > 1.0": "REAL", "real < 1.0": "REAL",
    "real > 0.0": "REAL",
    "integer": "INTEGER", "int": "INTEGER", "unsigned integer": "INTEGER", "integer > 1": "INTEGER",
    "0, 1 or 2": "INTEGER",
    "set of real numbers": "ARRAY", "comma-separated reals": "ARRAY", "list of reals": "ARRAY",
    "list of ints": "ARRAY", "int or sequence of ints": "ARRAY", "float or sequence of floats": "ARRAY",
    "series of text strings": "ARRAY",
}


def _blob(body: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(body) + body).hexdigest()


def manual(path: Path | None) -> str:
    """The manual's text, from ``path`` or the release tag, refused unless it is the tag's own blob."""
    body = path.read_bytes() if path else urllib.request.urlopen(MANUAL_URL, timeout=60).read()
    if _blob(body) != MANUAL_BLOB:
        raise SystemExit(f"the manual read is blob {_blob(body)}, not {MANUAL_BLOB} at {PESTPP_VERSION}")
    return body.decode("utf-8")


def _plain(text: str) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"\\(.)", r"\1", text).replace("*", "")
    return " ".join(text.split())


def _sections(text: str) -> list[tuple[int, str, str]]:
    return [(m.start(), m.group(1), m.group(2).strip())
            for m in re.finditer(r"^#+ <a id='(s[\d-]+)' />(.*)$", text, re.M)]


def _section_at(sections: list[tuple[int, str, str]], offset: int) -> tuple[str, str]:
    held = ("", "")
    for start, anchor, title in sections:
        if start > offset:
            break
        held = (anchor, title)
    return held


def _entry(rows: dict[str, dict], name: str, default: str | None, kind: str, role: str, tool: str,
           title: str) -> None:
    row = rows.setdefault(name.lower(), {"name": name.lower(), "kinds": [], "roles": [], "tools": [],
                                         "titles": [], "defaults": []})
    row["kinds"].append(kind.lower().strip())
    row["roles"].append(_plain(role))
    if tool not in row["tools"]:
        row["tools"].append(tool)
    if title not in row["titles"]:
        row["titles"].append(title)
    row["defaults"].append(default)


def table_rows(text: str) -> dict[str, dict]:
    """Every option the manual's tables, run-management paragraphs and control-data sentences name."""
    sections = _sections(text)
    rows: dict[str, dict] = {}
    for line in re.finditer(r"^\|(.*)\|\s*$", text, re.M):
        cells = [c.strip() for c in line.group(1).split("|")]
        named = re.match(r"^\*([A-Za-z0-9_]+)\((.*)\)\*$", cells[0])
        anchor, title = _section_at(sections, line.start())
        if named and anchor in _TABLES and len(cells) >= 3:
            _entry(rows, named.group(1), named.group(2), cells[1], cells[2], _TABLES[anchor], title)
    for row in re.finditer(r"<tr[^>]*>(.*?)</tr>", text, re.S):
        cells = [_plain(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", row.group(1), re.S)]
        anchor, title = _section_at(sections, row.start())
        named = re.match(r"^([A-Za-z][A-Za-z0-9_]*)(?:\((.*)\))?$", cells[0]) if cells else None
        if named and anchor in _TABLES and len(cells) >= 3:
            _entry(rows, named.group(1), named.group(2), cells[1], cells[2], _TABLES[anchor], title)
    for para in re.finditer(r"^\*\*([a-z_/]+)(?:\(\))?\*\*\n(.*?)(?=^\*\*[a-z_/]+(?:\(\))?\*\*\n|^#)", text,
                            re.M | re.S):
        anchor, title = _section_at(sections, para.start())
        if anchor != _RUN_MANAGEMENT:
            continue
        body = _plain(para.group(2))
        said = re.search(r"(?:[Dd]efault value is|[Dd]efault is|with a default value of) "
                         r"([-+]?\d[0-9.eE+-]*|[Tt]rue|[Ff]alse)\b", body)
        for name in para.group(1).split("/"):
            _entry(rows, name, said.group(1) if said else None, _prose_kind(name, body, said), body,
                   "run management", title)
    start = next(s for s, a, _t in sections if a == _CONTROL_DATA)
    end = next(s for s, a, _t in sections if a == "s8-7")
    paragraphs = [_plain(p) for p in re.split(r"\n\s*\n", text[start:end])]
    for paragraph in paragraphs:
        for named, kind in re.findall(r"\b([A-Z][A-Z0-9]{3,})(?:, an?| is an?) (integer|real) variable", paragraph):
            if named.lower() not in _COUNTS:
                _entry(rows, named, None, kind, paragraph, "control data", "4.6 Control Data Section")
    return rows


def _prose_kind(name: str, body: str, said: Any) -> str:
    """A run-management option's type, as its own paragraph states it of that option, else as the
    default it states reads; "" where it states neither."""
    if re.search(r"\b(is|must be) (also )?an? real number", body):
        return "real"
    own = re.escape(name)
    if re.search(rf"{own}\s*\(?\)?\s*(is an? Boolean|\((true|false)\)|is set to (true|false)\b)", body) \
            or re.search(r"\bis an? Boolean\b", body):
        return "boolean"
    if re.search(r"\b(is|must be) (also )?an? integer\b", body):
        return "integer"
    if said is not None:
        value = said.group(1)
        return "boolean" if value.lower() in ("true", "false") else "integer" if value.isdigit() else "real"
    return ""


def _typed(kind: str, value: str | None) -> Any:
    """``value`` read as ``kind``; ``None`` for an empty one, and ValueError for one that is not."""
    if value is None or not value.strip():
        return None
    value = value.strip().strip('"“”')
    if kind == "LOGICAL":
        if value.lower() in ("true", "1"):
            return True
        if value.lower() in ("false", "0"):
            return False
        raise ValueError(value)
    if kind == "INTEGER":
        return int(value)
    if kind == "REAL":
        return float(value)
    if kind == "ARRAY":
        parts = [p.strip() for p in value.strip(",").split(",")]
        try:
            return [float(p) if any(c in p for c in ".eE") else int(p) for p in parts]
        except ValueError:
            if any(not re.fullmatch(r"[A-Za-z0-9_\-]+", p) for p in parts):
                raise
            return parts
    return value


def printed(record: str) -> dict[str, str]:
    """Each option the record prints in its options block -> the value printed on its own line."""
    block = record.split("PEST++ OPTIONS:", 1)[1].split("Model command line", 1)[0]
    found: dict[str, str] = {}
    for line in block.splitlines():
        held = re.match(r"^([a-z][a-z0-9_]*):\s?(.*)$", line)
        if held and held.group(2).strip():
            found[held.group(1)] = held.group(2).strip()
    return found


def controlled(record: str) -> dict[str, str]:
    """The control-data variables the record prints under "PEST Control Information" -> their values."""
    block = record.split("PEST Control Information", 1)[1].split("PEST++ OPTIONS:", 1)[0]
    return {m.group(1): m.group(2) for m in re.finditer(r"^\s+([a-z]+) = (\S+)\s*$", block, re.M)}


def stated_back(study: str) -> dict[str, str]:
    """Each option the every-name study states -> the value it states: the binary read every one of them."""
    body = study.split("* control data keyword", 1)[1].split("*", 1)[0]
    return {line.split(" ", 1)[0]: line.split(" ", 1)[1] for line in body.strip().splitlines() if " " in line}


def unread(record: str) -> set[str]:
    """The names a forgiving parse warns it does not read."""
    return {m.group(1).lower() for m in re.finditer(
        r"unrecognized '\* control data keyword' key-value pair on line: \n\s+'([A-Za-z0-9_]+)\b", record)}


def build(text: str, defaults: str, control: str, accepted: str, study: str) -> tuple[list[dict], dict[str, Any]]:
    """The dictionary rows, and what the extraction left out and why.

    A printed value is the default only where the every-name study stated that same value and the binary
    read it back; a value the binary prints but refuses when stated keeps the manual's."""
    rows, skipped, back = table_rows(text), unread(accepted), stated_back(study)
    said = {**{name: value for name, value in printed(defaults).items()
               if back.get(name, "").strip(",") == value.strip(",")}, **controlled(control)}
    out, report = [], {"not_read_by_the_binary": sorted(skipped), "default_unreadable": [],
                       "binary_differs_from_manual": []}
    for name, row in sorted(rows.items(), key=lambda item: item[0]):
        if name in skipped:
            continue
        kinds = {_TYPES.get(k) for k in row["kinds"]} - {None}
        manual_default = next((d for d in row["defaults"] if d not in (None, "")), None)
        if manual_default and "," in manual_default and kinds <= {"REAL", "INTEGER"}:
            kinds = {"ARRAY"}
        kind = next(iter(kinds)) if len(kinds) == 1 else ("ARRAY" if "ARRAY" in kinds else
                                                          "REAL" if kinds == {"REAL", "INTEGER"} else None)
        if kind is None:
            kind = _from_printed(said.get(name))
        try:
            stated = _typed(kind, manual_default)
        except ValueError:
            stated = None
            report["default_unreadable"].append({"name": name, "manual": manual_default})
        entry = {"keyword": name, "identifier": name.upper(), "type": kind, "shape": "", "size": 1,
                 "unbounded": False, "help": " ".join(dict.fromkeys(r for r in row["roles"] if r)),
                 "rubrique": row["tools"], "is_file": False, "mandatory": False,
                 "source": f"PEST++ {PESTPP_VERSION} users manual: " + "; ".join(row["titles"])}
        engine = said.get(name)
        try:
            own = _typed(kind, engine) if engine is not None else None
        except ValueError:
            own = None
        if own is not None:
            entry["default"] = own
            if stated is not None and not _same(own, stated):
                entry["manual_default"] = stated
                report["binary_differs_from_manual"].append(
                    {"name": name, "binary": own, "manual": stated})
        elif stated is not None:
            entry["default"] = stated
        if "ies_subset_how" == name:
            entry["choices"] = ["first", "last", "random", "phi_based"]
        out.append(entry)
    return out, report


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, str) and isinstance(b, str):
        return a.lower() == b.lower()
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return a == b


def _from_printed(value: str | None) -> str:
    """The type of an option whose manual type names none, off the value the binary prints for it."""
    if value is None:
        return "STRING"
    if re.fullmatch(r"[-+]?\d+", value):
        return "INTEGER"
    try:
        float(value)
        return "REAL"
    except ValueError:
        return "STRING"


def names_study(rows: dict[str, dict]) -> str:
    """The ``* control data keyword`` lines stating every manual name, for the forgiving parse."""
    lines = []
    for name, row in sorted(rows.items()):
        kinds = {_TYPES.get(k) for k in row["kinds"]} - {None}
        value = {"LOGICAL": "false", "INTEGER": "1", "REAL": "1.0", "ARRAY": "1.0"}.get(
            next(iter(kinds)) if len(kinds) == 1 else "", "1")
        lines.append(f"{name} {value}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manual", type=Path, default=None, help="a local copy of the manual; unstated, the tag's")
    ap.add_argument("--names", action="store_true", help="print the lines of the forgiving parse and stop")
    ns = ap.parse_args(argv)
    text = manual(ns.manual)
    if ns.names:
        sys.stdout.write(names_study(table_rows(text)))
        return 0
    read = {name: (RECORDS / name).read_text(encoding="utf-8")
            for name in ("pestpp.rec", "control.rec", "names.rec", "names.pst")}
    rows, report = build(text, read["pestpp.rec"], read["control.rec"], read["names.rec"], read["names.pst"])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    held = json.loads(OUT.read_text()) if OUT.is_file() else {}
    OUT.write_text(json.dumps({"module": "pestpp", "keywords": rows, "inputs": held.get("inputs") or {}},
                              indent=2) + "\n", encoding="utf-8")
    json.dump(report, sys.stderr, indent=1)
    print(f"\n{len(rows)} keywords -> {OUT.relative_to(ROOT)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
