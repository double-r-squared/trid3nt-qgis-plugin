"""The RUN JOURNAL on disk: where it lives, and every record read back off it.

One append-only JSONL line per completed run, in the persistence directory nothing sweeps, so the record
outlives every artifact it describes. The run in progress writes its line; this module only finds and reads.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger("trid3nt_server.store.journal")

__all__ = ["journal_path", "read_records", "run_outputs"]

_FILENAME = "run_journal.jsonl"


def journal_path() -> Path:
    """Where the journal lives - the persistence dir, which no sweep touches."""
    from trid3nt_server.store.cases import _default_dev_persistence_dir

    root = os.environ.get("TRID3NT_DEV_PERSISTENCE_DIR") or _default_dev_persistence_dir()
    return Path(root) / _FILENAME


def read_records(path: Path | None = None) -> list[dict[str, Any]]:
    """Every record on file, oldest first. A malformed line is skipped, not fatal."""
    target = path or journal_path()
    if not target.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            logger.warning("run journal: skipping a malformed line in %s", target)
    return out


def run_outputs(run_id: str) -> list[dict[str, Any]]:
    """The layers the named run published, off its own record; ``[]`` when the
    journal has no line for it. The record is APPEND-ONLY, so the last line for
    a run id is the one that stands."""
    for record in reversed(read_records()):
        if str(record.get("run_id") or "") == str(run_id):
            published = record.get("outputs")
            return [dict(row) for row in published if isinstance(row, dict)] \
                if isinstance(published, list) else []
    return []
