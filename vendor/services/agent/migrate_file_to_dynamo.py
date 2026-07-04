"""job-0296 one-shot migration: copy the file-backed dev store into DynamoDB.

Reads each /opt/grace2/data/<db>/<collection>.json (plain list of docs) and
inserts every doc through DynamoMCPClient.call_tool("insert-one", ...) so the
float->Decimal + key handling matches the live backend exactly. Idempotent:
insert-one is a put_item keyed on _id (or case_id+message_id for chat), so a
re-run overwrites rather than duplicates.
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import sys

from grace2_agent.dynamo_backend import DynamoMCPClient

DB_DIR = sys.argv[1] if len(sys.argv) > 1 else "/opt/grace2/data/grace2_dev"
PREFIX = os.environ.get("GRACE2_DYNAMO_TABLE_PREFIX", "grace2_")


async def main() -> int:
    client = DynamoMCPClient(table_prefix=PREFIX)
    total_ok = total_seen = 0
    for path in sorted(glob.glob(os.path.join(DB_DIR, "*.json"))):
        coll = os.path.basename(path)[:-5]  # strip .json
        try:
            docs = json.load(open(path))
        except Exception as exc:  # noqa: BLE001
            print(f"{coll}: SKIP (unreadable: {exc})")
            continue
        # FileMCPClient stores a dict keyed by _id -> doc (the _id may live only
        # in the key). Normalize to (id, doc) pairs and inject _id if absent.
        if isinstance(docs, dict):
            items = list(docs.items())
        elif isinstance(docs, list):
            items = [(d.get("_id") if isinstance(d, dict) else None, d) for d in docs]
        else:
            print(f"{coll}: SKIP (unexpected shape {type(docs).__name__})")
            continue
        ok = 0
        for doc_id, doc in items:
            if not isinstance(doc, dict):
                continue
            total_seen += 1
            if "_id" not in doc and doc_id is not None:
                doc = {"_id": doc_id, **doc}
            try:
                await client.call_tool("insert-one", {"collection": coll, "document": doc})
                ok += 1
            except Exception as exc:  # noqa: BLE001
                print(f"  WARN {coll} _id={doc.get('_id')!r}: {exc}")
        docs = items  # for the count print below
        total_ok += ok
        print(f"{coll}: migrated {ok}/{len(docs)}")
    print(f"TOTAL: {total_ok}/{total_seen} docs migrated")
    return 0 if total_ok == total_seen else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
