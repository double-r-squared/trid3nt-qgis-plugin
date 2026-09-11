"""The sheet -> the engine's own steering file. One function, every module.

telapy's ``TelemacCas`` is the only writer of the format. What it writes is read
straight back by the engine's own parser against the engine's own dictionary, so
a value outside a keyword's CHOIX is caught there and never on inspection."""

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
            decks: dict[str, dict[str, Any]]) -> None:
    """``sheet`` and everything it names, onto the disk and into ``decks``.

    A coupled body is not content: it is filled against its own module's dictionary."""
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
