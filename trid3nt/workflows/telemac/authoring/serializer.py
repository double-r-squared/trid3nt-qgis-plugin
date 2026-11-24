"""The sheet -> the engine's own steering file. One function, every module.

telapy's ``TelemacCas`` is the only writer; the engine's own parser reads it back against
its dictionary, so a value outside a keyword's CHOIX is caught there.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Mapping

from .cas_validate import run_cas_driver, validate_authored_steering

logger = logging.getLogger("trid3nt.workflows.telemac.authoring.serializer")

__all__ = ["restate", "serialize"]


def serialize(sheet: Any, rundir: Path | str, *,
              steering: str | None = None, preserve_printouts: bool = False) -> dict[str, Any]:
    """Write ``sheet`` into ``rundir`` as its module's steering file.

    Engine defaults are not written; a coupled deck goes in the same driver call.
    """
    rundir = Path(rundir)
    rundir.mkdir(parents=True, exist_ok=True)
    decks: dict[str, dict[str, Any]] = {}
    _spread(sheet, rundir, steering or f"{sheet.module}.cas", decks,
            preserve_printouts=preserve_printouts)
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


def restate(rundir: Path | str, steering: str, module: str, values: Mapping[str, Any]) -> None:
    """The steering file ``steering`` already in ``rundir`` written again with ``values`` (by keyword) over what it
    states, through the same driver; the engine's parser reads it back, a refusal carrying its words."""
    run_cas_driver(rundir, {"restate": {steering: {"module": module, "values": dict(values)}}},
                   what=f"restate {steering}")
    validate_authored_steering(rundir, {steering: module})


def _spread(sheet: Any, rundir: Path, steering: str,
            decks: dict[str, dict[str, Any]], *, host: Any = None,
            preserve_printouts: bool = False) -> None:
    """``sheet`` and everything it names, onto the disk and into ``decks``.

    A coupled body is filled against its own module's dictionary.
    """
    from ..modules import fill, wrapper_for

    stated = dict(sheet.resolved())
    generated = sheet.printouts()
    stated = {**generated, **stated} if preserve_printouts else {**stated, **generated}
    decks[steering] = {"module": sheet.module,
                       "values": {**_partitioned(sheet, host), **stated}}
    for basename, content in sheet.files.items():
        if isinstance(content, Mapping) and "slots" in content:
            _spread(fill(wrapper_for(content["module"]), produced=sheet.run,
                         inputs_fill=False, **dict(content["slots"])),
                    rundir, basename, decks, host=sheet, preserve_printouts=preserve_printouts)
            continue
        path = rundir / basename
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(str(content))


# The launcher partitions the mesh and boundary files each steering names, per deck; a coupled steering naming neither dies in partel on more than one core.
_PARTITIONED = ("GEOMETRY_FILE", "GEOMETRY_FILE_FORMAT", "BOUNDARY_CONDITIONS_FILE")


def _partitioned(sheet: Any, host: Any) -> dict[str, Any]:
    """The host's mesh and boundary files, under the coupled module's spelling.

    A module whose dictionary has no such keyword takes none.
    """
    if host is None:
        return {}
    named = {}
    for identifier in _PARTITIONED:
        slot = sheet.body.MODULE_INPUT.get(identifier)
        stated = host.filled.get(identifier)
        if slot is not None and stated is not None:
            named[slot.keyword] = stated.value
    return named
