"""Every authored steering file, read by the engine's OWN parser before staging.

The author writes DAMOCLES decks from the server, where the sheet is. DAMOCLES
itself lives in the worker image, and it is unforgiving in a way that is hard to
see from a diff: a keyword one character off its dictionary spelling, a value of
the wrong type, a line that ran past the 72-character limit and derailed the
parse onto a later statement. All three read as a solve that started and then
stopped inside Fortran, blaming a keyword the author never wrote.

So the deck is parsed where the dictionary is, against the dictionary, at
AUTHORING time. A failure refuses by NAME, while a person can still read what
they asked for; the alternative is a staged run whose only account of itself is a
listing tail.

The file-existence check is OFF: the geometry and boundary files a deck names are
staged after this, by the launcher, so their absence here is the normal case.
What is checked is the grammar and the vocabulary.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Mapping

from trid3nt_server.workflows.mesh.meshers.drivers import drivers_dir

logger = logging.getLogger("trid3nt_server.workflows.telemac.authoring.cas_validate")

__all__ = ["CasParseError", "validate_authored_decks"]

_TELEMAC_IMAGE_DEFAULT = "trid3nt-local/telemac:latest"
_INCONTAINER_SCRIPT = "telemac_cas_driver.py"
_CONTAINER_TIMEOUT_S = 300


class CasParseError(RuntimeError):
    """An authored deck does not parse against its own dictionary."""

    error_code = "TELEMAC_CAS_PARSE_FAILED"


def validate_authored_decks(rundir: Path | str,
                            decks: Mapping[str, str]) -> dict[str, dict]:
    """Parse every ``{basename: module}`` deck in ``rundir`` -> what was read.

    One container round trip for the whole authoring, because the decks of one
    run are written together and a per-file launch would pay the startup cost
    once per keyword family for nothing.
    """
    rundir = Path(rundir)
    present = {name: module for name, module in decks.items()
               if (rundir / name).is_file()}
    if not present:
        return {}
    image = os.environ.get("TRID3NT_TELEMAC_IMAGE") or _TELEMAC_IMAGE_DEFAULT
    config = "telemac_cas_config.json"
    (rundir / config).write_text(json.dumps({"decks": present}))
    argv = [
        "docker", "run", "--rm", "--network", "none",
        "-v", f"{drivers_dir()}:/drivers:ro", "-v", f"{rundir}:/data",
        image, "python",
        f"/drivers/{_INCONTAINER_SCRIPT}", f"/data/{config}", "/data"]
    logger.info("telemac cas parse: %s", " ".join(argv))
    cp = subprocess.run(argv, capture_output=True, text=True,
                        timeout=_CONTAINER_TIMEOUT_S)
    if cp.returncode != 0:
        raise CasParseError(
            f"the authored decks {sorted(present)} could not be read by the "
            f"engine's own parser (rc={cp.returncode}):\n"
            f"{cp.stdout[-2000:]}\n{cp.stderr[-2000:]}")
    rows = json.loads((rundir / "telemac_cas_stats.json").read_text())
    failed = {name: row for name, row in rows.items() if not row.get("ok")}
    if failed:
        raise CasParseError(
            "the authored deck(s) do not parse against the engine's own "
            "dictionary, so the solve would stop inside DAMOCLES blaming a "
            "keyword nobody wrote: "
            + "; ".join(f"{name} ({row['module']}): {row['error']}"
                        for name, row in sorted(failed.items())))
    logger.info("telemac cas parse ok: %s",
                {name: row["keywords"] for name, row in rows.items()})
    return rows
