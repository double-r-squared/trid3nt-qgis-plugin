"""The sheet -> the engine's own steering file. One function, every module.

telapy's ``TelemacCas`` is THE writer of the steering format; there is no second
one. What this holds is the sheet's resolution - which keywords the deck states
and which the dictionary supplies - the files a composite named, and the two
measured caveats the format demands, both handled in the driver telapy runs in:
a file keyword is assigned through ``values`` because telapy's ``set()`` demands
the file already exist, and a string is handed over as a ``str`` whose ``repr``
is the engine's own spelling, because Python's own repr reaches for a
double-quote delimiter as soon as a value holds an apostrophe and a
double-quoted value derails DAMOCLES on the first space inside it.

Written, the file is read straight back by the engine's own parser against the
engine's own dictionary. That round trip is where a value outside a keyword's
CHOIX is caught, and it is the reason nothing here is trusted on inspection.
"""

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
    """Write ``sheet`` into ``rundir`` as the module's steering file -> what was
    written. Engine defaults are NOT written: the dictionary supplies them.

    A COUPLED module's deck is written in the same act and by the same writer:
    it is a sheet of its own, checked against its own module's dictionary, and
    the carrier states only the three keywords it names the coupling by. One
    driver call writes them all, so a coupling can never half-arrive.
    """
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
            decks: dict[str, dict[str, Any]]) -> None:
    """``sheet`` and everything it names, onto the disk and into ``decks``.

    A file a composite named is CONTENT and is written here. A coupled body is
    not content - it is another module's sheet - so it is filled against that
    module's own catalog and joins the decks the one driver call writes.
    """
    from ..modules import fill, wrapper_for

    decks[steering] = {"module": sheet.module, "values": dict(sheet.resolved())}
    for basename, content in sheet.files.items():
        if isinstance(content, Mapping) and "slots" in content:
            _spread(fill(wrapper_for(content["module"]), **dict(content["slots"])),
                    rundir, basename, decks)
            continue
        path = rundir / basename
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(str(content))
