"""The step ledger: what one invocation may replay, and what it may not.

Every terminal state TOMBSTONES - reaching the end and failing both leave a
completion marker, so a live-no-cache tool re-executes rather than replaying.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from trid3nt_server.persistence import DEFAULT_DATABASE, FileMCPClient

__all__ = ["LedgerRecord", "StepLedger", "inputs_digest", "invocation_key",
           "records_from_docs"]

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
    ``input_mode`` is part of it: an auto attempt and a user_gated one are different
    runs, so one must never seed the other's replay."""
    from trid3nt_server.gates.input_review import resolve_input_gate_mode

    blob = json.dumps({"w": workflow, "v": values,
                       "m": resolve_input_gate_mode(input_mode)},
                      sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def inputs_digest(value: Any) -> str:
    """A STABLE digest of what a node was handed, across processes.
    A value with a ``uri`` digests as that uri, a model as its own dump, and
    anything that states neither contributes only its type."""
    return hashlib.sha256(
        json.dumps(_stable(value), sort_keys=True, default=str
                   ).encode("utf-8")).hexdigest()[:16]


def _stable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(k): _stable(v) for k, v in sorted(value.items(),
                                                      key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_stable(v) for v in value]
    uri = getattr(value, "uri", None)
    if isinstance(uri, str):
        return {"uri": uri}
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return _stable(dump(mode="json"))
        except Exception:  # noqa: BLE001 - an undumpable model states its type
            pass
    return {"is": f"{type(value).__module__}.{type(value).__name__}"}


@dataclass(frozen=True, slots=True)
class LedgerRecord:
    """One completed node: what ran, what it produced, and the domain it left behind."""

    index: int
    node: str
    runner: str
    completed_at: str
    #: A digest of the RESOLVED inputs this node ran on. A rerun inherits a
    #: record only where they are unchanged, so the first producer whose inputs
    #: moved is the cut and everything downstream of it re-executes with it.
    #: Empty means a record written before the key existed, which never replays.
    inputs_key: str = ""
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

    def replay_for(self, index: int, node: str,
                   inputs_key: str) -> LedgerRecord | None:
        """The cached record for this node, when the plan AND its inputs match.
        Position and label say the plan is the same plan; the inputs key says the
        work would be the same work. A record whose inputs moved is not a match."""
        for rec in self.records:
            if rec.index == index:
                if rec.node != node or not rec.inputs_key:
                    return None
                return rec if rec.inputs_key == inputs_key else None
        return None

    def replay_data(self, name: str) -> LedgerRecord | None:
        """The cached record for a produced Data artifact."""
        label = _data_label(name)
        return next((r for r in self.data_records if r.node == label), None)

    async def record(self, rec: LedgerRecord, *, final: bool = False) -> None:
        """Record one completed node; ``final`` TOMBSTONES the run in the same write.
        One write, because a process that died between the last record and
        :meth:`complete` would leave a finished run looking resumable."""
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
        """Plant the work a DERIVED run inherits from its parent, so the ordinary
        resume path replays it. Records arrive from the parent's run SNAPSHOT,
        never from the parent's ledger, which completion has already tombstoned."""
        # REPLACE, never merge: this key may carry a tombstone from an identical
        # earlier derivation, and inheriting past a tombstone is what was asked for.
        self.records = sorted(records, key=lambda r: r.index)
        self.data_records = list(data_records)
        self.completed = False
        await self._persist()

    async def clear(self) -> None:
        """Abandon this key: TOMBSTONE the document, then reap it.
        Tombstone first, so a delete that fails degrades into a marker that refuses
        replay rather than leaving the key replayable for the whole TTL window."""
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
        A positive marker rather than a delete, so a failed delete can never become
        a permanent result cache for a ``cacheable=False`` tool; the TTL sweep reaps it."""
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
    This is what bounds completion tombstones - they are reaped on AGE, so a
    finished run stays un-replayable for the whole window and then goes."""
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
