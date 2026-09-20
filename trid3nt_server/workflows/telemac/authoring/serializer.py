"""The sheet -> the engine's own steering file. One function, every module.

telapy's ``TelemacCas`` is the only writer of the format. What it writes is read
straight back by the engine's own parser against the engine's own dictionary, so
a value outside a keyword's CHOIX is caught there and never on inspection. The
variables keyword is the module's table, generated per deck as it is written."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Mapping

from .cas_validate import run_cas_driver, validate_authored_steering

logger = logging.getLogger("trid3nt_server.workflows.telemac.authoring.serializer")

__all__ = ["serialize"]


def serialize(sheet: Any, rundir: Path | str, *,
              steering: str | None = None) -> dict[str, Any]:
    """Write ``sheet`` into ``rundir`` as its module's steering file.

    Engine defaults are not written; a coupled deck goes in the same driver call."""
    rundir = Path(rundir)
    rundir.mkdir(parents=True, exist_ok=True)
    decks: dict[str, dict[str, Any]] = {}
    _spread(sheet, rundir, steering or f"{sheet.module}.cas", decks)
    run_cas_driver(rundir, {"write": decks},
                   what=f"write {', '.join(sorted(decks))}")
    written = json.loads((rundir / "telemac_cas_written.json").read_text())
    validate_authored_steering(
        rundir, {name: deck["module"] for name, deck in decks.items()})
    logger.info("telemac serialized %s", ", ".join(
        f"{name} ({len(deck['values'])} keywords)"
        for name, deck in sorted(decks.items())))
    top = steering or f"{sheet.module}.cas"
    return {"steering": top, "keywords": sorted(decks[top]["values"]),
            "files": sorted(decks), "written": written[top]}


def _spread(sheet: Any, rundir: Path, steering: str,
            decks: dict[str, dict[str, Any]], *, host: Any = None) -> None:
    """``sheet`` and everything it names, onto the disk and into ``decks``.

    A coupled body is not content: it is filled against its own module's
    dictionary. What each deck WRITES is its module's own table, generated here
    rather than restated by whoever asked the question."""
    from ..modules import fill, wrapper_for

    stated = {**dict(sheet.resolved()), **sheet.printouts()}
    decks[steering] = {"module": sheet.module,
                       "values": {**_partitioned(sheet, host), **stated}}
    for basename, content in sheet.files.items():
        if isinstance(content, Mapping) and "slots" in content:
            _spread(fill(wrapper_for(content["module"]), **dict(content["slots"])),
                    rundir, basename, decks, host=sheet)
            continue
        path = rundir / basename
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(str(content))


#: The two files the engine's launcher partitions PER DECK: it splits the mesh
#: and the boundary conditions each steering names into one piece per core, so a
#: coupled steering naming neither is handed nothing to read on more than one
#: core and the run dies in partel.
_PARTITIONED = ("GEOMETRY_FILE", "BOUNDARY_CONDITIONS_FILE")


def _partitioned(sheet: Any, host: Any) -> dict[str, Any]:
    """The host's mesh and boundary files, under the coupled module's spelling.

    A coupled body runs on the host's domain, so the files are the host's; a
    module whose dictionary has no such keyword takes none."""
    if host is None:
        return {}
    named = {}
    for identifier in _PARTITIONED:
        slot = sheet.body.MODULE_INPUT.get(identifier)
        stated = host.filled.get(identifier)
        if slot is not None and stated is not None:
            named[slot.keyword] = stated.value
    return named
