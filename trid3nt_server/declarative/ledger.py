"""The step ledger: an ordered record of completed nodes, per invocation.

Powers resume-from-failed-step now (a rerun of the SAME invocation replays
completed nodes from their cached artifacts); shaped for full pause/resume later.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from trid3nt_server.persistence import DEFAULT_DATABASE, FileMCPClient

__all__ = ["LedgerRecord", "StepLedger", "invocation_key"]

logger = logging.getLogger("trid3nt_server.declarative.ledger")

_COLLECTION = "declarative_run_ledgers"
_SCHEMA = 1


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
    """The per-invocation record. ``replay_for`` is what makes a rerun cheap."""

    key: str
    workflow: str
    records: list[LedgerRecord] = field(default_factory=list)
    _client: Any = None

    @classmethod
    async def load(cls, key: str, workflow: str) -> "StepLedger":
        client = FileMCPClient()
        records: list[LedgerRecord] = []
        try:
            doc = await client.call_tool("find-one", {
                "database": DEFAULT_DATABASE, "collection": _COLLECTION,
                "filter": {"_id": key},
            })
            raw = _unwrap(doc)
            if raw and raw.get("schema_version") == _SCHEMA:
                records = [LedgerRecord(**{**r, "artifact_uris": tuple(
                    r.get("artifact_uris") or ())}) for r in raw.get("records", [])]
        except Exception as exc:  # noqa: BLE001 - a missing/corrupt ledger only costs a replay
            logger.warning("step ledger %s unreadable (%s); starting fresh", key, exc)
        return cls(key=key, workflow=workflow, records=records, _client=client)

    def replay_for(self, index: int, node: str) -> LedgerRecord | None:
        """The cached record for this node position, when the plan still matches."""
        for rec in self.records:
            if rec.index == index:
                return rec if rec.node == node else None
        return None

    async def record(self, rec: LedgerRecord) -> None:
        self.records = [r for r in self.records if r.index != rec.index] + [rec]
        self.records.sort(key=lambda r: r.index)
        await self._persist()

    async def truncate_from(self, index: int) -> None:
        """Drop records at or after ``index`` - the plan changed shape under them."""
        self.records = [r for r in self.records if r.index < index]
        await self._persist()

    async def clear(self) -> None:
        self.records = []
        await self._persist()

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
                }},
                "upsert": True,
            })
        except Exception as exc:  # noqa: BLE001 - the ledger is an optimisation, never a gate
            logger.warning("step ledger %s not persisted: %s", self.key, exc)


def _unwrap(doc: Any) -> dict[str, Any] | None:
    if isinstance(doc, dict):
        inner = doc.get("document", doc)
        return inner if isinstance(inner, dict) else None
    return None
