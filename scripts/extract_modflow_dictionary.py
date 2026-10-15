#!/usr/bin/env python3
"""Extract every exposed MODFLOW body's keyword dictionary out of the mf6 definition files.

The DFNs are the engine's own publication of its keyword surface, read at the
release the worker carries; the committed JSON this writes is generated data,
never hand edited.
"""

from __future__ import annotations

import ast
import json
import re
import sys
import urllib.request
from functools import lru_cache
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

#: The mf6 release the worker carries (``workers/modflow/Dockerfile`` MF6_VERSION); the DFNs are that release's own.
MF6_REF = "6.7.0"

#: The bodies this exposes. PRT rides with a later leg.
BODIES: tuple[str, ...] = ("gwf", "gwt", "gwe")

#: The simulation's own packages every body carries beside its model's.
SHARED: tuple[str, ...] = ("sim-tdis", "sln-ims")

#: The mf6 source this reads the definition files out of.
_REPO = "https://github.com/MODFLOW-ORG/modflow6"
_DFN_DIR = "doc/mf6io/mf6ivar/dfn"
#: GitHub's API refuses a request carrying no user agent.
_AGENT = {"User-Agent": "trid3nt-modflow-dictionary"}


def _get(url: str) -> bytes:
    with urllib.request.urlopen(
            urllib.request.Request(url, headers=_AGENT)) as response:
        return response.read()


def module_input_dir() -> Path:
    """Where the committed module inputs live."""
    return (Path(__file__).resolve().parents[1] / "trid3nt_server" / "workflows"
            / "modflow" / "modules" / "module_input")


@lru_cache(maxsize=None)
def _dfn_names() -> tuple[str, ...]:
    """Every package definition file at the pinned release."""
    listing = json.loads(_get(
        f"https://api.github.com/repos/MODFLOW-ORG/modflow6/contents/"
        f"{_DFN_DIR}?ref={MF6_REF}"))
    return tuple(sorted(entry["name"][:-len(".dfn")] for entry in listing
                        if entry["name"].endswith(".dfn")))


def fetch_dfn(stem: str) -> str:
    """One package's definition file text at the pinned release."""
    return _get(f"{_REPO}/raw/{MF6_REF}/{_DFN_DIR}/{stem}.dfn").decode()


def parse(text: str) -> list[dict[str, str]]:
    """The file's entries in the engine's order, each its block and its own key/value pairs.

    A ``name`` line starts an entry; the lines before the first one are the
    file's header comments, which name no keyword.
    """
    entries: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    block = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition(" ")
        key = key.lower()
        if key == "block":
            block = value.strip()
        elif key == "name":
            current = {"block": block, "name": value.strip()}
            entries.append(current)
        elif current is not None:
            current[key] = value.strip()
    return entries


#: A shape naming the grid or an axis of it: the value is a constant, one per layer, or the NAME of an input holding the array.
_ARRAY_SHAPE = re.compile(r"nrow|ncol|ncpl|nlay|nodes|naux")

#: What each DFN type word is in the dictionary; a record, recarray or keystring is one structured block.
_SCALAR = {"keyword": "LOGICAL", "integer": "INTEGER",
           "double precision": "REAL", "string": "STRING"}
_STRUCTURED = frozenset({"record", "recarray", "keystring"})


def _type_of(entry: dict[str, str]) -> str:
    """The dictionary type word this entry's declared DFN type is read as."""
    declared = entry.get("type", "").strip()
    if declared.split(None, 1)[0].lower() in _STRUCTURED:
        return "STRUCT"
    if _ARRAY_SHAPE.search(entry.get("shape", "")):
        return "ARRAY"
    kind = _SCALAR.get(declared)
    if kind is None:
        raise ValueError(
            f"{entry['name']}: the DFN declares the unknown type {declared!r}")
    return kind


def _optional(entry: dict[str, str]) -> bool:
    """Whether the DFN marks this entry optional; a bare ``optional`` flag is the engine's own true."""
    return entry.get("optional", "false").lower() in ("", "true")


def _engine_default(entry: dict[str, str]) -> object:
    """The value the DFN's default spells, or ``None`` where it gives none.

    A bare word the literal reader cannot parse is that keyword's own string
    default (``eager``); any other unreadable default is refused, never guessed.
    """
    raw = entry.get("default_value")
    if raw is None:
        return None
    if raw.lower() in ("true", "false"):
        return raw.lower() == "true"
    try:
        return ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        if raw.isalnum():
            return raw
        raise ValueError(
            f"{entry['name']}: cannot read the default {raw!r}") from None


def _help(entry: dict[str, str]) -> str:
    """The entry's own prose: its long name before its description, a REPLACE template standing for no prose."""
    longname = entry.get("longname", "").strip()
    description = entry.get("description", "").strip()
    if description.upper().startswith("REPLACE"):
        description = ""
    if not description or description.lower() == "none":
        return longname
    return f"{longname}. {description}" if longname else description


def _row(entry: dict[str, str], package: str) -> dict[str, object]:
    """One DFN entry as a dictionary row."""
    name = entry["name"]
    default = _engine_default(entry)
    choices = entry.get("valid", "").split()
    row: dict[str, object] = {
        "keyword": name.upper().replace("-", "_"),
        "identifier": f"{package}_{name.upper().replace('-', '_')}",
        "type": _type_of(entry),
        "size": 1,
        "unbounded": False,
        "help": _help(entry),
        "rubrique": [entry["block"].upper()],
        "is_file": False,
        "mandatory": not _optional(entry) and default is None,
    }
    if default is not None:
        row["default"] = default
    if choices:
        row["choices"] = choices
    return row


def body_rows(body: str) -> list[dict[str, object]]:
    """Every dictionary row of one body, its model's packages first and the shared TDIS and IMS after."""
    stems = [stem for stem in _dfn_names() if stem.startswith(f"{body}-")] \
        + list(SHARED)
    rows = [_row(entry, package=stem.split("-", 1)[1].upper())
            for stem in stems for entry in parse(fetch_dfn(stem))
            if "in_record" not in entry and "block_variable" not in entry]
    identifiers = [row["identifier"] for row in rows]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError(f"{body}: the DFNs spell a keyword twice")
    return rows


def extract(dest: Path, bodies: tuple[str, ...] = BODIES
            ) -> dict[str, int]:
    """Write ``<body>.json`` for every body into ``dest`` -> keyword counts."""
    dest.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for body in bodies:
        rows = body_rows(body)
        (dest / f"{body}.json").write_text(
            json.dumps({"module": body, "keywords": rows}, indent=2) + "\n")
        counts[body] = len(rows)
    return counts


def main() -> int:
    counts = extract(module_input_dir())
    print(json.dumps(counts, indent=2))
    print("total keywords:", sum(counts.values()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
