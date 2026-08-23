"""The step ledger: the record of an INCOMPLETE attempt, per invocation.

Resume-from-failed-step: a rerun of the SAME invocation replays nodes the failed
attempt completed. It is NOT a result cache - a plan that runs to the end reaps
its own ledger, so the next invocation of a live-no-cache tool refetches the
world. Only an attempt that died mid-plan leaves anything behind.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any

from trid3nt_server.persistence import DEFAULT_DATABASE, FileMCPClient

__all__ = ["LedgerRecord", "StepLedger", "invocation_key"]

logger = logging.getLogger("trid3nt_server.declarative.ledger")

_COLLECTION = "declarative_run_ledgers"
_SCHEMA = 2

#: How long an abandoned attempt stays resumable. Past this the world it cached
#: has moved on, so the records are reaped rather than replayed.
_TTL = timedelta(days=7)

#: The ledger index reserved for Data production, which is lazy and therefore has
#: no position in the node sequence. Data records are keyed by name instead.
_DATA_INDEX = -1


def invocation_key(workflow: str, values: dict[str, Any]) -> str:
    """Identity of THIS invocation - the same question with the same params rehashes."""
    blob = json.dumps({"w": workflow, "v": values}, sort_keys=True, default=str)
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
    _client: Any = None

    @classmethod
    async def load(cls, key: str, workflow: str) -> "StepLedger":
        client = FileMCPClient()
        records: list[LedgerRecord] = []
        data_records: list[LedgerRecord] = []
        try:
            await _sweep(client)
            doc = await client.call_tool("find-one", {
                "database": DEFAULT_DATABASE, "collection": _COLLECTION,
                "filter": {"_id": key},
            })
            raw = _unwrap(doc)
            if raw and _resumable(raw):
                records = _read_records(raw.get("records"))
                data_records = _read_records(raw.get("data_records"))
        except Exception as exc:  # noqa: BLE001 - a missing/corrupt ledger only costs a replay
            logger.warning("step ledger %s unreadable (%s); starting fresh", key, exc)
        return cls(key=key, workflow=workflow, records=records,
                   data_records=data_records, _client=client)

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

    async def record(self, rec: LedgerRecord) -> None:
        self.records = [r for r in self.records if r.index != rec.index] + [rec]
        self.records.sort(key=lambda r: r.index)
        await self._persist()

    async def record_data(self, name: str, rec: LedgerRecord) -> None:
        label = _data_label(name)
        rec = replace(rec, index=_DATA_INDEX, node=label)
        self.data_records = [r for r in self.data_records if r.node != label] + [rec]
        await self._persist()

    async def clear(self) -> None:
        """Forget the attempt entirely - nothing left to replay."""
        self.records = []
        self.data_records = []
        await self._reap()

    async def complete(self) -> None:
        """The plan reached its end, so its ledger goes.

        Keeping it would turn resume into a permanent result cache - a
        ``cacheable=False`` tool replaying a dead artifact URI forever.
        """
        logger.debug("step ledger %s complete; reaping", self.key)
        await self.clear()

    async def _reap(self) -> None:
        if self._client is None:
            return
        try:
            await _reap(self._client, self.key)
        except Exception as exc:  # noqa: BLE001 - the ledger is an optimisation, never a gate
            logger.warning("step ledger %s not reaped: %s", self.key, exc)

    async def _persist(self) -> None:
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
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "records": [r.to_doc() for r in self.records],
                    "data_records": [r.to_doc() for r in self.data_records],
                }},
                "upsert": True,
            })
        except Exception as exc:  # noqa: BLE001 - the ledger is an optimisation, never a gate
            logger.warning("step ledger %s not persisted: %s", self.key, exc)


def _data_label(name: str) -> str:
    return f"data:{name}"


def _read_records(raw: Any) -> list[LedgerRecord]:
    if not isinstance(raw, list):
        return []
    return [LedgerRecord(**{**r, "artifact_uris": tuple(r.get("artifact_uris") or ())})
            for r in raw]


def _resumable(raw: dict[str, Any]) -> bool:
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
    """Evict abandoned attempts: stale schema, or older than the resume TTL."""
    doc = await client.call_tool("find", {
        "database": DEFAULT_DATABASE, "collection": _COLLECTION, "filter": {},
    })
    for raw in (doc or {}).get("documents") or []:
        if isinstance(raw, dict) and raw.get("_id") and not _resumable(raw):
            await _reap(client, str(raw["_id"]))


def _unwrap(doc: Any) -> dict[str, Any] | None:
    if isinstance(doc, dict):
        inner = doc.get("document", doc)
        return inner if isinstance(inner, dict) else None
    return None
