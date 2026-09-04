"""The step ledger: what one invocation may replay, and what it may not.

Every terminal state TOMBSTONES. A plan that runs to the end leaves a completion
marker in place of its records, so the next invocation of a live-no-cache tool
refetches the world; a plan that FAILS leaves the same marker, so a re-run of the
corrected question re-executes rather than replaying artifacts the code that
produced them no longer describes.

What replay is for is therefore the work a run INHERITS: a derived rerun seeds
this ledger from its successful parent's snapshot and the ordinary resume path
walks it. Records also survive a process that died without unwinding, which is
what ``restart_clean`` discards.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any

from trid3nt_server.persistence import DEFAULT_DATABASE, FileMCPClient

__all__ = ["LedgerRecord", "StepLedger", "invocation_key", "records_from_docs"]

logger = logging.getLogger("trid3nt_server.workflows.runtime.ledger")

_COLLECTION = "declarative_run_ledgers"
_SCHEMA = 3

#: How long an abandoned attempt stays resumable, and how long a completion
#: tombstone survives. Past this the world it cached has moved on, so the document
#: is reaped rather than replayed - which is what bounds tombstone accumulation.
_TTL = timedelta(days=7)

#: The ledger index reserved for Data production, which is lazy and therefore has
#: no position in the node sequence. Data records are keyed by name instead.
_DATA_INDEX = -1


def invocation_key(workflow: str, values: dict[str, Any],
                   *, input_mode: str | None = None) -> str:
    """Identity of THIS invocation - the same question with the same params rehashes.

    ``input_mode`` is part of the identity: an auto-mode attempt and a user_gated
    one are different runs (the gated one may revise the very params the auto
    attempt cached), so a failed auto attempt must not seed a user_gated replay.
    """
    from trid3nt_server.gates.input_review import resolve_input_gate_mode

    blob = json.dumps({"w": workflow, "v": values,
                       "m": resolve_input_gate_mode(input_mode)},
                      sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class LedgerRecord:
    """One completed node: what ran, what it produced, and the domain it left behind."""

    index: int
    node: str
    runner: str
    completed_at: str
    result_kind: str = "none"
    result: Any = None
    result_type: str | None = None
    artifact_uris: tuple[str, ...] = ()
    domain: dict[str, Any] | None = None

    def to_doc(self) -> dict[str, Any]:
        doc = asdict(self)
        doc["artifact_uris"] = list(self.artifact_uris)
        return doc


@dataclass
class StepLedger:
    """One invocation's unfinished attempt. ``replay_for`` is what makes a rerun cheap."""

    key: str
    workflow: str
    records: list[LedgerRecord] = field(default_factory=list)
    data_records: list[LedgerRecord] = field(default_factory=list)
    completed: bool = False
    #: A document for this key was on disk when the ledger loaded - so abandoning
    #: the key has something to tombstone rather than a no-op write to make.
    existed: bool = False
    _client: Any = None

    @classmethod
    async def load(cls, key: str, workflow: str) -> "StepLedger":
        client = FileMCPClient()
        records: list[LedgerRecord] = []
        data_records: list[LedgerRecord] = []
        existed = False
        try:
            await _sweep(client)
            doc = await client.call_tool("find-one", {
                "database": DEFAULT_DATABASE, "collection": _COLLECTION,
                "filter": {"_id": key},
            })
            raw = _unwrap(doc)
            existed = bool(raw)
            # Replay requires the document to be PRESENT and NOT complete. A
            # tombstone is present-and-complete, so a finished run cannot replay
            # even though its document survives to be swept.
            if raw and _fresh(raw) and not raw.get("complete"):
                records = records_from_docs(raw.get("records"))
                data_records = records_from_docs(raw.get("data_records"))
        except Exception as exc:  # noqa: BLE001 - a missing/corrupt ledger only costs a replay
            logger.warning("step ledger %s unreadable (%s); starting fresh", key, exc)
        return cls(key=key, workflow=workflow, records=records,
                   data_records=data_records, existed=existed, _client=client)

    def replay_for(self, index: int, node: str) -> LedgerRecord | None:
        """The cached record for this node position, when the plan still matches."""
        for rec in self.records:
            if rec.index == index:
                return rec if rec.node == node else None
        return None

    def replay_data(self, name: str) -> LedgerRecord | None:
        """The cached record for a produced Data artifact."""
        label = _data_label(name)
        return next((r for r in self.data_records if r.node == label), None)

    async def record(self, rec: LedgerRecord, *, final: bool = False) -> None:
        """Record one completed node; ``final`` TOMBSTONES the run in the same write.

        Stamping completion together with the last node's record is what closes the
        crash window: a process that dies (or is cancelled) between the last record
        and :meth:`complete` would otherwise leave a FINISHED run looking resumable.
        """
        self.records = [r for r in self.records if r.index != rec.index] + [rec]
        self.records.sort(key=lambda r: r.index)
        if final:
            self._tombstone()
        await self._persist(completion=final)

    async def record_data(self, name: str, rec: LedgerRecord) -> None:
        label = _data_label(name)
        rec = replace(rec, index=_DATA_INDEX, node=label)
        self.data_records = [r for r in self.data_records if r.node != label] + [rec]
        await self._persist()

    async def seed(self, records: list[LedgerRecord],
                   data_records: list[LedgerRecord]) -> None:
        """Plant the work a DERIVED run inherits from its parent.

        A rerun-with-overrides starts from work its parent already did, which the
        parent's OWN ledger no longer holds - completion tombstones it, and that
        tombstone is what stops a live-no-cache tool becoming a result cache. The
        records arrive from the parent's run SNAPSHOT instead and land here, so
        the ordinary resume path replays them and nothing downstream needs to know
        a derivation happened.

        The document is REPLACED, not merged: this key may carry a tombstone from
        an identical earlier derivation, and inheriting past a tombstone is the
        whole point of being asked.
        """
        self.records = sorted(records, key=lambda r: r.index)
        self.data_records = list(data_records)
        self.completed = False
        await self._persist()

    async def clear(self) -> None:
        """Abandon this key: TOMBSTONE the document, then reap it.

        Reaping alone was enough only when the delete SUCCEEDED. A swallowed reap
        failure left the abandoned key's records sitting there ``complete: false``
        and replayable for the whole TTL window - a re-key's orphan, or a
        restart_clean's discarded attempt, back as a replay ghost. Writing the
        tombstone first degrades a failed delete into a marker that refuses replay.
        """
        self.records = []
        self.data_records = []
        if self.existed:
            self.completed = True
            await self._persist(completion=True)
        self.completed = False
        if await self._reap():
            self.existed = False

    async def complete(self) -> None:
        """The plan reached its end: its records are replaced by a completion tombstone.

        Deleting instead would make every swallowed delete failure a permanent
        result cache for a ``cacheable=False`` tool. The tombstone is a positive
        marker written through the same atomic path as the records, and the TTL
        sweep reaps it, so accumulation is bounded rather than forever.
        """
        if self.completed:
            return
        self._tombstone()
        await self._persist(completion=True)

    def _tombstone(self) -> None:
        self.completed = True
        self.records = []
        self.data_records = []

    async def _reap(self) -> bool:
        """Delete the document. Answers whether it actually went."""
        if self._client is None:
            return False
        try:
            await _reap(self._client, self.key)
            return True
        except Exception as exc:  # noqa: BLE001 - the ledger is an optimisation, never a gate
            logger.warning("step ledger %s not reaped: %s", self.key, exc)
            return False

    async def _persist(self, *, completion: bool = False) -> None:
        if self._client is None:
            return
        try:
            await self._client.call_tool("update-one", {
                "database": DEFAULT_DATABASE, "collection": _COLLECTION,
                "filter": {"_id": self.key},
                "update": {"$set": {
                    "_id": self.key,
                    "schema_version": _SCHEMA,
                    "workflow": self.workflow,
                    "complete": self.completed,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "records": [r.to_doc() for r in self.records],
                    "data_records": [r.to_doc() for r in self.data_records],
                }},
                "upsert": True,
            })
            self.existed = True
        except Exception as exc:  # noqa: BLE001 - the ledger is an optimisation, never a gate
            if not completion:
                logger.warning("step ledger %s not persisted: %s", self.key, exc)
                return
            # A LOST completion marker is the ghost class: the records already on
            # disk would make a finished run replayable. Fall back to deleting the
            # document, which says the same thing.
            logger.error("step ledger %s completion marker not persisted (%s); "
                         "falling back to deleting the document", self.key, exc)
            await self._reap()


def _data_label(name: str) -> str:
    return f"data:{name}"


def records_from_docs(raw: Any) -> list[LedgerRecord]:
    """Records back off the store - here and in the run snapshot, one reader."""
    if not isinstance(raw, list):
        return []
    return [LedgerRecord(**{**r, "artifact_uris": tuple(r.get("artifact_uris") or ())})
            for r in raw]


def _fresh(raw: dict[str, Any]) -> bool:
    """Still of this schema and inside the TTL - i.e. not yet sweepable."""
    return raw.get("schema_version") == _SCHEMA and not _aged(raw)


def _aged(raw: dict[str, Any]) -> bool:
    stamp = raw.get("updated_at")
    if not isinstance(stamp, str):
        return True
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - when > _TTL


async def _reap(client: Any, key: str) -> None:
    await client.call_tool("delete-one", {
        "database": DEFAULT_DATABASE, "collection": _COLLECTION,
        "filter": {"_id": key},
    })


async def _sweep(client: Any) -> None:
    """Evict spent documents: stale schema, or older than the TTL.

    This is what bounds completion tombstones: they are reaped on AGE, so a
    finished run stays un-replayable for the whole TTL window and then goes.
    """
    doc = await client.call_tool("find", {
        "database": DEFAULT_DATABASE, "collection": _COLLECTION, "filter": {},
    })
    for raw in (doc or {}).get("documents") or []:
        if isinstance(raw, dict) and raw.get("_id") and not _fresh(raw):
            await _reap(client, str(raw["_id"]))


def _unwrap(doc: Any) -> dict[str, Any] | None:
    if isinstance(doc, dict):
        inner = doc.get("document", doc)
        return inner if isinstance(inner, dict) else None
    return None
