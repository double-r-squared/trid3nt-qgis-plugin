"""The doubles more than one test module stands its subject up with.

A fake defined inside a test module makes that module an import target for its
siblings; the shared ones live here so no test file is another test's library.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from trid3nt_contracts.case import CaseSummary
from trid3nt_contracts.common import new_ulid


class MockMCPClient:
    """In-memory mock of the MongoDB MCP server.

    Implements the tool surface ``Persistence`` calls into - ``find-one`` / ``find``
    / ``insert-one`` / ``update-one`` - and records every call for routing asserts."""

    def __init__(self) -> None:
        # collection -> id -> document
        self._store: dict[str, dict[str, dict]] = {}
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name, arguments=None):  # noqa: D401
        args = dict(arguments or {})
        self.calls.append((name, args))
        coll = args.get("collection") or "_default"
        store = self._store.setdefault(coll, {})

        if name == "insert-one":
            doc = args["document"]
            store[doc["_id"]] = doc
            return {"insertedId": doc["_id"]}

        if name == "update-one":
            filt = args.get("filter", {})
            update = args.get("update", {})
            set_ = update.get("$set", {})
            upsert = args.get("upsert", False)
            target_id = filt.get("_id")
            if target_id and target_id in store:
                store[target_id].update(set_)
            elif upsert and target_id:
                store[target_id] = {**set_, "_id": target_id}
            elif filt:
                # Update by a non-_id filter (first match wins).
                for doc in store.values():
                    if all(doc.get(k) == v for k, v in filt.items()):
                        doc.update(set_)
                        break
            return {"matchedCount": 1, "modifiedCount": 1}

        if name == "find-one":
            filt = args.get("filter", {})
            for doc in store.values():
                if self._matches(doc, filt):
                    return {"document": doc}
            return {"document": None}

        if name == "find":
            filt = args.get("filter", {})
            sort = args.get("sort", {})
            results = [d for d in store.values() if self._matches(d, filt)]
            if sort:
                key = next(iter(sort.keys()))
                direction = sort[key]
                results.sort(
                    key=lambda d: d.get(key, ""),
                    reverse=(direction == -1),
                )
            return {"documents": results}

        raise NotImplementedError(f"mock MCP: unknown tool {name!r}")

    @staticmethod
    def _matches(doc: dict, filt: dict) -> bool:
        """Tiny query matcher: equality, ``$or``, ``$exists=False``, ``$nin``."""
        for k, v in filt.items():
            if k == "$or":
                if not any(MockMCPClient._matches(doc, sub) for sub in v):
                    return False
                continue
            if isinstance(v, dict) and "$exists" in v:
                present = k in doc
                if v["$exists"] is False and present:
                    return False
                if v["$exists"] is True and not present:
                    return False
                continue
            if isinstance(v, dict) and "$nin" in v:
                # job-0267: mirrors FileMCPClient — a missing field matches
                # (doc.get returns None, which is "not in" the exclusion
                # list unless None is listed).
                if doc.get(k) in v["$nin"]:
                    return False
                continue
            if doc.get(k) != v:
                return False
        return True


def _fresh_case_summary() -> CaseSummary:
    return CaseSummary(
        case_id=new_ulid(),
        title="Hurricane Ian — Fort Myers flood scenario",
        created_at=datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc),
        status="active",
        bbox=(-82.0, 26.5, -81.8, 26.7),
        primary_hazard="flood",
        layer_summary=["nlcd-fort-myers", "flood-depth-01HX"],
    )


class MockWebSocket:
    """Collects every envelope ``send`` would have written to the wire.

    Each entry is the parsed envelope as a dict. The tests assert on
    ``type`` + ``payload`` fields.
    """

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, raw: Any) -> None:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        if isinstance(raw, str):
            self.sent.append(json.loads(raw))
        else:
            self.sent.append(raw)
