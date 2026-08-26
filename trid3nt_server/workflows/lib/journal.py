"""The RUN JOURNAL: one append-only JSONL line per completed run.

Decoupled from artifacts ON PURPOSE. Case data is delete-on-whim and run prefixes
come and go, so the record of what was asked, what was resolved and what came back
cannot live inside them - it lives here, in the persistence directory nothing
sweeps, and outlives every artifact it describes.

One line is a RUN RECORD: the resolved sheet with its doors and bases, the ANSWER
the run published, the provenance rows, the mesh facts, the wall times, the
compute class, and where the run came from. That is the substrate for deriving a
default from prior runs, for accumulating resolution-sensitivity evidence, for
calibration priors and for regression baselines - none of which can be built from
a directory of rasters.

Written by the skeleton's publish stage, which is one seam for every engine.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

logger = logging.getLogger("trid3nt_server.workflows.lib.journal")

__all__ = ["append_record", "journal_path", "read_records", "run_origin"]

#: The env var a DRIVER sets to label its runs. A canary and a person asking a
#: question produce the same shaped record, and telemetry that learns a default
#: from a canary's pinned 600 s window would be learning from a test fixture.
ORIGIN_ENV = "TRID3NT_RUN_ORIGIN"

_FILENAME = "run_journal.jsonl"


def journal_path() -> Path:
    """Where the journal lives - the persistence dir, which no sweep touches."""
    from trid3nt_server.persistence.persistence import _default_dev_persistence_dir

    root = os.environ.get("TRID3NT_DEV_PERSISTENCE_DIR") or _default_dev_persistence_dir()
    return Path(root) / _FILENAME


def run_origin(*, live_session: bool) -> str:
    """Where this run came from: a labelled driver, a live session, or headless."""
    declared = (os.environ.get(ORIGIN_ENV) or "").strip()
    if declared:
        return declared
    return "session" if live_session else "headless"


def append_record(record: Mapping[str, Any]) -> Path | None:
    """Append one run record. Best-effort: a journal write never fails a run.

    The run already happened and its products already exist; refusing to hand the
    caller its answer because a log line could not be written would be the
    failure-retracts-something anti-pattern.
    """
    path = journal_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, default=str, separators=(",", ":"))
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception:  # noqa: BLE001 - journalled, never propagated
        logger.warning("run journal write failed for %s", record.get("run_id"),
                       exc_info=True)
        return None
    return path


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


def build_record(*, run_id: str | None, template: str, engine: str | None,
                 sheet: Sequence[Any], answer: Mapping[str, Any],
                 provenance: Sequence[Any], result: Any,
                 wall_seconds: float | None, origin: str,
                 executed: Sequence[str], replayed: Sequence[str],
                 notes: Sequence[str]) -> dict[str, Any]:
    """One run record, from what the publish stage already holds."""
    return {
        "run_id": run_id,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "template": template,
        "engine": engine,
        "origin": origin,
        "sheet": [_row(row) for row in sheet],
        "answer": {k: _small(v) for k, v in answer.items()},
        "provenance": [_provenance(row) for row in provenance],
        "mesh": {
            "mesh_size_m": getattr(result, "mesh_size_m", None),
            "mesh_node_estimate": getattr(result, "mesh_node_estimate", None),
        },
        "compute_class": next((r.value for r in sheet
                               if getattr(r, "name", "") == "compute_class"), None),
        "wall_seconds": wall_seconds,
        "executed": list(executed),
        "replayed": list(replayed),
        "notes": list(notes),
    }


def _row(row: Any) -> dict[str, Any]:
    """One resolved param, WITH its door and basis - the sheet, not just the values.

    Which door a value came through is the whole point: a discharge the user pinned
    and one the National Water Model answered are the same number and different
    evidence, and a journal that recorded only the number could not tell them apart
    later.
    """
    return {
        "name": getattr(row, "name", None),
        "value": _small(getattr(row, "value", None)),
        "door": getattr(row, "door", None),
        "basis": getattr(row, "basis", None),
        "units": getattr(row, "units", None),
        "consequence": getattr(row, "consequence", None),
        "note": getattr(row, "note", "") or None,
        "clamped_from": _small(getattr(row, "clamped_from", None)),
        "real_source": getattr(row, "real_source", None),
    }


def _provenance(row: Any) -> dict[str, Any]:
    return {
        "param": getattr(row, "param", None),
        "value": _small(getattr(row, "value", None)),
        "basis": getattr(row, "basis", None),
        "note": getattr(row, "note", None),
        "real_source": getattr(row, "real_source", None),
    }


#: How many elements of a list-valued field the record keeps. A sag curve is
#: hundreds of points; the journal wants the FACT that there was one and its
#: shape, not a second copy of the product.
_LIST_CAP = 32


def _small(value: Any) -> Any:
    if isinstance(value, (list, tuple)) and len(value) > _LIST_CAP:
        return {"length": len(value), "head": list(value[:8]),
                "truncated": True}
    if isinstance(value, (list, tuple)):
        return [_small(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
