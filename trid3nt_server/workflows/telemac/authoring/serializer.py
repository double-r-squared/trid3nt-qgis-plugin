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
from typing import Any

from .cas_validate import run_cas_driver, validate_authored_steering

logger = logging.getLogger("trid3nt_server.workflows.telemac.authoring.serializer")

__all__ = ["serialize"]


def serialize(sheet: Any, rundir: Path | str, *,
              steering: str | None = None) -> dict[str, Any]:
    """Write ``sheet`` into ``rundir`` as the module's steering file -> what was
    written. Engine defaults are NOT written: the dictionary supplies them."""
    rundir = Path(rundir)
    rundir.mkdir(parents=True, exist_ok=True)
    steering = steering or f"{sheet.module}.cas"
    for basename, content in sheet.files.items():
        path = rundir / basename
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(str(content))
    values = dict(sheet.resolved())
    run_cas_driver(
        rundir,
        {"write": {steering: {"module": sheet.module, "values": values}}},
        what=f"write {steering} for {sheet.module}")
    written = json.loads((rundir / "telemac_cas_written.json").read_text())
    validate_authored_steering(rundir, {steering: sheet.module})
    logger.info("telemac serialized %s: %d keywords, %d files",
                steering, len(values), len(sheet.files))
    return {"steering": steering, "keywords": sorted(values),
            "files": sorted(sheet.files), "written": written[steering]}
