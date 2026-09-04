"""The RUN SNAPSHOT: what a FINISHED run leaves behind so a child can derive from it.

The step ledger records an INCOMPLETE attempt and tombstones itself the moment a
plan reaches its end - that tombstone is what keeps a ``live-no-cache`` tool from
quietly becoming a result cache, and it stays. A rerun-with-overrides is the
other thing entirely: an explicit derivation from a NAMED past run, where the
pinned inputs ARE the point. So a completed run's records are copied out here,
keyed by its run id, and nothing reaches them except a caller that names that id.

What a snapshot holds is the run's PAST: the sheet it resolved, the artifacts
handed to it, and one record per node it completed - each record carrying the
object-store URI the node produced. A child that replays a record therefore
points at the parent's own artifact, which is what makes "reused byte-identical"
a fact about the bytes rather than a claim about a re-fetch.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from trid3nt_server.persistence import DEFAULT_DATABASE, FileMCPClient

from .ledger import LedgerRecord, records_from_docs
from .params import ResolvedParam

__all__ = ["Derivation", "RunSnapshot", "read_snapshot", "write_snapshot"]

logger = logging.getLogger("trid3nt_server.workflows.runtime.snapshot")

_COLLECTION = "declarative_run_snapshots"
_SCHEMA = 1

#: How long a finished run stays derivable-from. Longer than the ledger's own
#: window, because a calibration loop returns to one parent over and over, and
#: shorter than forever, because the artifacts a record points at are
#: delete-on-whim and a snapshot outliving all of them reuses nothing. A record
#: whose artifact is already gone re-executes rather than failing, so an expired
#: snapshot costs a rerun its reuse, never its correctness.
_TTL = timedelta(days=30)


@dataclass(frozen=True, slots=True)
class Derivation:
    """A child run's link to the parent it came from, and what it changed.

    Rides the child's journal line and its narrated notes, so the chain from a
    calibration's tenth run back to the question that started it is readable
    without diffing sheets.
    """

    parent_run_id: str
    overrides: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RunSnapshot:
    """One completed run's derivable past."""

    run_id: str
    workflow: str
    input_mode: str | None
    sheet: tuple[ResolvedParam, ...]
    records: tuple[LedgerRecord, ...]
    data_records: tuple[LedgerRecord, ...]
    supplied: dict[str, Any]
    parent_run_id: str | None
    created_at: str

    @property
    def sheet_names(self) -> frozenset[str]:
        return frozenset(row.name for row in self.sheet)


async def write_snapshot(*, run_id: str | None, workflow: str,
                         input_mode: str | None,
                         sheet: Sequence[ResolvedParam],
                         records: Sequence[LedgerRecord],
                         data_records: Sequence[LedgerRecord],
                         supplied: Mapping[str, Any],
                         derived_from: Derivation | None = None) -> None:
    """Record this run as derivable-from. Best-effort: never fails a finished run.

    An analysis-only workflow has no solve and therefore no run id to key on; it
    simply leaves no snapshot, and a rerun of it refuses by name rather than
    deriving from a run nobody can point at.
    """
    if not run_id:
        return
    doc = {
        "_id": run_id,
        "schema_version": _SCHEMA,
        "workflow": workflow,
        "input_mode": input_mode,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "parent_run_id": derived_from.parent_run_id if derived_from else None,
        "sheet": [dataclasses.asdict(row) for row in sheet],
        "records": [rec.to_doc() for rec in records],
        "data_records": [rec.to_doc() for rec in data_records],
        "supplied": dict(supplied),
    }
    try:
        # The store is JSON; a value it cannot carry would fail the whole write
        # and leave the run underivable-from with no explanation. Round-tripping
        # here converts the shapes that can convert (tuples to lists) and names
        # the one that cannot.
        doc = json.loads(json.dumps(doc, default=str))
        await FileMCPClient().call_tool("update-one", {
            "database": DEFAULT_DATABASE, "collection": _COLLECTION,
            "filter": {"_id": run_id}, "update": {"$set": doc}, "upsert": True,
        })
    except Exception:  # noqa: BLE001 - the run already happened and already answered
        logger.warning("run snapshot not written for %s; that run cannot be "
                       "rerun-with-overrides", run_id, exc_info=True)


async def read_snapshot(run_id: str) -> RunSnapshot | None:
    """The named run's derivable past, or ``None`` when there is none to read."""
    try:
        client = FileMCPClient()
        await _sweep(client)
        doc = await client.call_tool("find-one", {
            "database": DEFAULT_DATABASE, "collection": _COLLECTION,
            "filter": {"_id": run_id},
        })
    except Exception as exc:  # noqa: BLE001 - answered as absent, never propagated
        logger.warning("run snapshot %s unreadable (%s)", run_id, exc)
        return None
    raw = _unwrap(doc)
    if not raw or not _fresh(raw):
        return None
    return RunSnapshot(
        run_id=str(raw.get("_id") or run_id),
        workflow=str(raw.get("workflow") or ""),
        input_mode=raw.get("input_mode"),
        sheet=tuple(ResolvedParam(**row) for row in raw.get("sheet") or []),
        records=tuple(records_from_docs(raw.get("records"))),
        data_records=tuple(records_from_docs(raw.get("data_records"))),
        supplied=dict(raw.get("supplied") or {}),
        parent_run_id=raw.get("parent_run_id"),
        created_at=str(raw.get("created_at") or ""),
    )


def _fresh(raw: Mapping[str, Any]) -> bool:
    if raw.get("schema_version") != _SCHEMA:
        return False
    stamp = raw.get("created_at")
    if not isinstance(stamp, str):
        return False
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - when <= _TTL


async def _sweep(client: Any) -> None:
    """Evict snapshots of a stale schema or past the TTL - what bounds the store."""
    doc = await client.call_tool("find", {
        "database": DEFAULT_DATABASE, "collection": _COLLECTION, "filter": {},
    })
    for raw in (doc or {}).get("documents") or []:
        if isinstance(raw, dict) and raw.get("_id") and not _fresh(raw):
            await client.call_tool("delete-one", {
                "database": DEFAULT_DATABASE, "collection": _COLLECTION,
                "filter": {"_id": raw["_id"]},
            })


def _unwrap(doc: Any) -> dict[str, Any] | None:
    if isinstance(doc, dict):
        inner = doc.get("document", doc)
        return inner if isinstance(inner, dict) else None
    return None
