"""DynamoDB-backed MCP client (sprint-14-aws) — the AWS persistence substrate.

GRACE-2's persistence layer (``persistence.Persistence``) speaks ONE logical
MCP tool surface — ``insert-one`` / ``update-one`` / ``update-many`` /
``find-one`` / ``find`` — through :class:`persistence.MCPClientProtocol`. Three
backends satisfy that protocol:

  * ``MCPSurfaceTranslator``  → the live ``mongodb-mcp-server`` (GCP/Atlas).
  * ``FileMCPClient``         → JSON-on-disk dev/AWS-live fallback.
  * ``DynamoMCPClient`` (here) → Amazon DynamoDB via boto3.

This module adds the third backend WITHOUT touching the ``Persistence`` wrapper,
``FileMCPClient``, or ``MCPSurfaceTranslator``. It speaks the logical surface
NATIVELY against DynamoDB (it never routes through ``MCPSurfaceTranslator`` —
that translator exists only for the real Mongo server's renamed tools and EJSON
wrapping). Selection is gated by the env ``GRACE2_PERSISTENCE_BACKEND``
(``file`` default; ``dynamodb`` to engage), so the current AWS-live runtime
(file-backed) is unchanged until the orchestrator flips the env.

Design mirrors :class:`persistence.FileMCPClient`:

  * one DynamoDB table per logical Mongo collection (``{prefix}{alias}``),
  * a ``_matches`` query matcher (equality / ``$or`` / ``$exists`` / ``$nin``),
  * an ``_apply_update`` applicator (``$set`` / ``$setOnInsert`` / ``$push`` /
    ``$addToSet``) used for the read-modify-write path,

so the query/update semantics are IDENTICAL to the file backend and every
``Persistence`` method round-trips byte-for-byte.

Credentials + region resolve exactly like ``bedrock_adapter._bedrock_client``:
``boto3.resource("dynamodb", region_name=AWS_REGION or AWS_DEFAULT_REGION or
"us-west-2")`` — boto3 walks the standard chain (env / ~/.aws / EC2 instance
role), so there is no s3fs/GDAL-vsis3-style instance-role resolution problem.

Table layout (provisioned out-of-band — see the job's awsRunbook):

  * ``grace2_cases``      PK ``_id``      (+ GSIs ``user_id-index`` /
    ``owner_user_id-index`` so owner-scoped listing avoids a full Scan)
  * ``grace2_chat``       PK ``case_id`` + SK ``message_id``
  * ``grace2_sessions``   PK ``_id``      (holds the append-only ``charts`` LIST
    and ``project_ids`` LIST)
  * ``grace2_users``      PK ``_id``      (+ GSI ``firebase_uid-index``)
  * ``grace2_secrets``    PK ``_id``      (+ GSI ``user_id-index``)
  * ``grace2_audit``      PK ``_id``      (insert-only)
  * ``grace2_telemetry``  PK ``_id``      (dashboard reads via bounded Scan)

NOTE on key schema: this backend treats ``_id`` as the partition key for every
table EXCEPT chat. The chat table is partitioned by ``case_id`` (every chat
query filters by ``case_id``); ``_id == message_id`` is preserved as a plain
attribute, and ``message_id`` is the sort key. ``DynamoMCPClient`` is told the
per-table key schema via ``_TABLE_KEYS`` so it knows when a filter can drive a
native ``get_item`` / ``Query`` vs. a Scan fallback.
"""

from __future__ import annotations

import asyncio
import logging
import os
from decimal import Decimal
from typing import Any

logger = logging.getLogger("grace2_agent.dynamo_backend")

# Selection / configuration env vars (additive; default keeps file behavior).
PERSISTENCE_BACKEND_ENV = "GRACE2_PERSISTENCE_BACKEND"
DYNAMO_TABLE_PREFIX_ENV = "GRACE2_DYNAMO_TABLE_PREFIX"
DEFAULT_TABLE_PREFIX = "grace2_"

# Logical Mongo collection -> DynamoDB table alias (suffix after the prefix).
# Unmapped collections fall back to a sanitized collection name (so a new
# telemetry/audit collection works without editing this map). The mapping
# below covers every collection Persistence + the direct call_tool sites use.
_COLLECTION_ALIAS = {
    "projects": "cases",  # CASES_COLLECTION
    "case_chat_messages": "chat",  # CHAT_COLLECTION
    "sessions": "sessions",  # SESSIONS_COLLECTION
    "users": "users",  # USERS_COLLECTION
    "secrets": "secrets",  # SECRETS_COLLECTION
    "audit_log": "audit",  # AUDIT_COLLECTION
    "tool_call_telemetry": "telemetry",  # TELEMETRY_COLLECTION
    "case_telemetry": "case_telemetry",
    "description_audit": "description_audit",
}

# Per-table key schema, keyed by table ALIAS. ``pk`` is the partition-key
# attribute; ``sk`` is the optional sort key. Tables not listed default to a
# single ``_id`` partition key. This lets call_tool pick get_item/Query when
# the filter targets the key, falling back to Scan otherwise — mirroring
# FileMCPClient's "_id fast path vs. linear scan" split.
_TABLE_KEYS: dict[str, dict[str, str]] = {
    "chat": {"pk": "case_id", "sk": "message_id"},
}

# GSIs available per table alias: maps a single filter attribute -> GSI name.
# Used to turn an owner/firebase_uid filter into a Query instead of a Scan.
_TABLE_GSIS: dict[str, dict[str, str]] = {
    "cases": {
        "user_id": "user_id-index",
        "owner_user_id": "owner_user_id-index",
    },
    "users": {"firebase_uid": "firebase_uid-index"},
    "secrets": {"user_id": "user_id-index"},
}


def _table_prefix() -> str:
    return os.environ.get(DYNAMO_TABLE_PREFIX_ENV, DEFAULT_TABLE_PREFIX)


def _alias_for(collection: str) -> str:
    alias = _COLLECTION_ALIAS.get(collection)
    if alias is not None:
        return alias
    # Sanitize an unmapped collection into a valid table suffix (DynamoDB
    # allows [A-Za-z0-9_.-]; Mongo collection names are already in that set,
    # but normalize defensively).
    return "".join(c if (c.isalnum() or c in "_.-") else "_" for c in collection)


def _pk_attr(alias: str) -> str:
    return _TABLE_KEYS.get(alias, {}).get("pk", "_id")


def _sk_attr(alias: str) -> str | None:
    return _TABLE_KEYS.get(alias, {}).get("sk")


# --------------------------------------------------------------------------- #
# Type marshaling: JSON <-> DynamoDB resource-API item shapes
# --------------------------------------------------------------------------- #
#
# The boto3 resource API (Table.put_item / update_item / get_item) serializes
# Python values to DynamoDB attribute types via a TypeSerializer that REJECTS
# native floats and requires Decimal for numbers. Our stored documents are JSON
# model dumps (str / int / float / bool / None / list / dict), so floats appear
# in bbox tuples, Vega-Lite chart specs, etc. ``_to_ddb`` recursively coerces
# floats -> Decimal on write; ``_from_ddb`` coerces Decimal -> int/float on read
# so Pydantic validation sees normal scalars. Empty strings are preserved
# (DynamoDB has supported empty-string attribute values since 2020).


def _to_ddb(value: Any) -> Any:
    """Recursively coerce a JSON-shaped value into DynamoDB-resource form."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        # Decimal(str(x)) avoids the binary-float artifacts Decimal(float)
        # produces; DynamoDB's number precision tolerates the string form.
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_ddb(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_ddb(v) for v in value]
    return value


def _ddb_item(doc: Any, alias: str | None = None) -> Any:
    """Build a put_item Item, dropping ``None`` KEY/GSI-KEY attributes.

    DynamoDB rejects an item that carries a table/GSI KEY attribute typed NULL
    (e.g. an anonymous user whose ``firebase_uid`` is None, or a case with a
    null ``user_id``/``owner_user_id`` GSI key). Omitting such an attribute is
    the idiomatic representation — it reads back as None via ``.get()`` and the
    item simply isn't projected into that GSI.

    Only the table key (pk/sk) and GSI key attributes for ``alias`` are stripped
    when None — every other None is preserved so ``$exists`` query semantics stay
    byte-identical to the file/Mongo backend (which keeps null fields present).
    """
    converted = _to_ddb(doc)
    if not isinstance(converted, dict):
        return converted
    key_attrs = set()
    if alias is not None:
        key_attrs.add(_pk_attr(alias))
        sk = _sk_attr(alias)
        if sk:
            key_attrs.add(sk)
        key_attrs.update(_TABLE_GSIS.get(alias, {}).keys())
    return {
        k: v for k, v in converted.items()
        if not (v is None and k in key_attrs)
    }


def _from_ddb(value: Any) -> Any:
    """Recursively coerce a DynamoDB-resource value back to JSON-shaped form."""
    if isinstance(value, Decimal):
        # Integral Decimals -> int; fractional -> float. Matches what the
        # original JSON dump produced (ints stay ints, floats stay floats).
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    if isinstance(value, dict):
        return {k: _from_ddb(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_from_ddb(v) for v in value]
    # boto3 resource API returns String Sets as Python ``set``; normalize to a
    # JSON list so list-typed contract fields (e.g. project_ids) validate.
    if isinstance(value, (set, frozenset)):
        return [_from_ddb(v) for v in value]
    return value


# --------------------------------------------------------------------------- #
# DynamoMCPClient
# --------------------------------------------------------------------------- #


class DynamoMCPClient:
    """DynamoDB backend satisfying :class:`persistence.MCPClientProtocol`.

    Implements the five logical MCP tools ``Persistence`` (and the direct
    ``_mcp.call_tool`` sites) invoke — ``insert-one`` / ``update-one`` /
    ``update-many`` / ``find-one`` / ``find`` — against per-collection DynamoDB
    tables. Semantics match :class:`persistence.FileMCPClient` exactly so the
    backend is interchangeable.

    The optional ``resource`` arg injects a boto3-resource-shaped object (the
    test suite passes an in-memory fake / ``moto`` resource); production builds
    a real ``boto3.resource("dynamodb", ...)`` once at construction.
    """

    def __init__(self, *, table_prefix: str | None = None, resource: Any = None) -> None:
        self._prefix = table_prefix if table_prefix is not None else _table_prefix()
        if resource is not None:
            self._ddb = resource
        else:
            import boto3  # local import: keeps boto3 lazy for the file path
            from botocore.config import Config

            region = (
                os.environ.get("AWS_REGION")
                or os.environ.get("AWS_DEFAULT_REGION")
                or "us-west-2"
            )
            # A1 FIX 2 (BOUNDED TIMEOUTS): the agent's asyncio WS loop drives
            # these calls (via ``asyncio.to_thread`` — FIX 1). botocore's
            # DEFAULT read_timeout is 60s; a single stalled DynamoDB call would
            # otherwise pin a worker thread (and any awaiter) for a full minute,
            # well past the client's 10s pong deadline -> force-reconnect ->
            # BLINK. Cap connect at 2s / read at 3s with 2 standard-mode
            # retries so a transient blip self-heals but a true stall surfaces
            # as a fast typed error instead of freezing the turn. The
            # keyed/GSI fast paths (``_fetch_candidates``) keep normal reads off
            # the Scan path, so these short timeouts never turn a routine read
            # into a spurious timeout.
            self._ddb = boto3.resource(
                "dynamodb",
                region_name=region,
                config=Config(
                    connect_timeout=2,
                    read_timeout=3,
                    retries={"max_attempts": 2, "mode": "standard"},
                ),
            )
        # Table handle cache (one boto3 Table resource per alias).
        self._tables: dict[str, Any] = {}
        # A1 FIX 1 (NON-BLOCKING / atomicity): per-``_id`` asyncio.Lock guarding
        # the ``_update`` read-modify-write. Now that each boto3 call runs in a
        # worker thread (``asyncio.to_thread``), two coroutines updating the
        # SAME ``_id`` could interleave get_item/put_item across threads and the
        # last writer would clobber the other's ``$push``/``$addToSet``. The
        # lock serializes read-modify-write PER document key (different ids stay
        # concurrent). Keyed by ``f"{alias}:{_id}"`` so two tables sharing an id
        # don't false-share. Lazily allocated; bounded-evicted so the always-on
        # agent never grows it without limit.
        self._update_locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------ #
    # Table resolution
    # ------------------------------------------------------------------ #

    def _table(self, collection: str):
        alias = _alias_for(collection)
        tbl = self._tables.get(alias)
        if tbl is None:
            tbl = self._ddb.Table(self._prefix + alias)
            self._tables[alias] = tbl
        return tbl

    # ------------------------------------------------------------------ #
    # Query matcher + update applicator (ported verbatim from FileMCPClient
    # so semantics are identical on the Scan / read-modify-write paths)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _matches(doc: dict, filt: dict) -> bool:
        """Tiny query matcher: equality, ``$or``, ``$exists``, ``$nin``.

        Byte-for-byte the same predicate logic as
        ``FileMCPClient._matches`` (persistence.py) — including the
        Mongo-faithful ``$nin`` rule that a MISSING field matches (its value
        ``None`` is "not in" the exclusion list unless ``None`` is listed).
        """
        for k, v in filt.items():
            if k == "$or":
                if not any(DynamoMCPClient._matches(doc, sub) for sub in v):
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
                if doc.get(k) in v["$nin"]:
                    return False
                continue
            if doc.get(k) != v:
                return False
        return True

    @staticmethod
    def _apply_update(doc: dict, update: dict, *, inserting: bool) -> None:
        """Apply a Mongo update document in-place (Mongo-faithful).

        Identical operator set + semantics to
        ``FileMCPClient._apply_update``: ``$set`` / ``$setOnInsert`` (only when
        ``inserting``) / ``$push`` (append; create list if missing) /
        ``$addToSet`` (append iff not present). Unknown operators raise so the
        next gap fails loudly rather than silently dropping data.
        """
        for op, fields in update.items():
            if op == "$set":
                doc.update(fields)
            elif op == "$setOnInsert":
                if inserting:
                    for k, v in fields.items():
                        doc.setdefault(k, v)
            elif op == "$push":
                for k, v in fields.items():
                    arr = doc.get(k)
                    if not isinstance(arr, list):
                        arr = []
                        doc[k] = arr
                    arr.append(v)
            elif op == "$addToSet":
                for k, v in fields.items():
                    arr = doc.get(k)
                    if not isinstance(arr, list):
                        arr = []
                        doc[k] = arr
                    if v not in arr:
                        arr.append(v)
            else:
                raise NotImplementedError(
                    f"DynamoMCPClient update: unsupported operator {op!r} "
                    f"(supports $set / $setOnInsert / $push / $addToSet)"
                )

    # ------------------------------------------------------------------ #
    # Read primitives
    # ------------------------------------------------------------------ #

    def _key_for(self, alias: str, doc_id: Any, *, sk_value: Any = None) -> dict:
        key = {_pk_attr(alias): doc_id}
        sk = _sk_attr(alias)
        if sk is not None and sk_value is not None:
            key[sk] = sk_value
        return key

    #: Cap on the per-``_id`` update-lock table so the always-on agent process
    #: never grows it without bound over thousands of distinct case/session ids.
    _MAX_UPDATE_LOCKS = 4096

    def _lock_for_id(self, alias: str, doc_id: Any) -> asyncio.Lock:
        """Resolve (lazily allocate) the per-document update lock (A1 FIX 1).

        Serializes the ``_update`` read-modify-write for one ``{alias}:{_id}``
        so two coroutines touching the SAME doc can't interleave their
        get/put across worker threads and lose a ``$push``/``$addToSet``.
        Different documents never contend. FIFO-evicts the oldest entry past
        the cap; an evicted lock that is still held stays alive (the holder
        keeps its own reference) — only the dict slot is reclaimed, and a
        concurrent contender that lost the slot allocates a fresh lock, so
        the worst case past the cap is reduced serialization, never
        corruption (the cap is far above realistic concurrent-write fan-out).
        """
        key = f"{alias}:{doc_id}"
        lock = self._update_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._update_locks[key] = lock
            while len(self._update_locks) > self._MAX_UPDATE_LOCKS:
                # dict preserves insertion order; pop the oldest.
                self._update_locks.pop(next(iter(self._update_locks)))
        return lock

    async def _scan_all(self, table) -> list[dict]:
        """Full scan with pagination, returning JSON-shaped docs.

        A1 FIX 1: each blocking ``table.scan`` runs in a worker thread via
        ``asyncio.to_thread`` so a paginating Scan never freezes the WS event
        loop (the BLINK root cause).
        """
        items: list[dict] = []
        kwargs: dict[str, Any] = {}
        while True:
            resp = await asyncio.to_thread(lambda kw=kwargs: table.scan(**kw))
            items.extend(_from_ddb(it) for it in resp.get("Items", []))
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                break
            kwargs["ExclusiveStartKey"] = lek
        return items

    async def _query_gsi(
        self, table, index_name: str, attr: str, value: Any
    ) -> list[dict]:
        """Query a GSI by a single equality, paginating, JSON-shaped out.

        A1 FIX 1: each blocking ``table.query`` runs off the event loop.
        """
        from boto3.dynamodb.conditions import Key

        items: list[dict] = []
        kwargs: dict[str, Any] = {
            "IndexName": index_name,
            "KeyConditionExpression": Key(attr).eq(value),
        }
        while True:
            resp = await asyncio.to_thread(lambda kw=kwargs: table.query(**kw))
            items.extend(_from_ddb(it) for it in resp.get("Items", []))
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                break
            kwargs["ExclusiveStartKey"] = lek
        return items

    async def _query_pk(self, table, alias: str, pk_value: Any) -> list[dict]:
        """Query a table's partition key (used for the chat table).

        A1 FIX 1: each blocking ``table.query`` runs off the event loop.
        """
        from boto3.dynamodb.conditions import Key

        items: list[dict] = []
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": Key(_pk_attr(alias)).eq(pk_value),
        }
        while True:
            resp = await asyncio.to_thread(lambda kw=kwargs: table.query(**kw))
            items.extend(_from_ddb(it) for it in resp.get("Items", []))
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                break
            kwargs["ExclusiveStartKey"] = lek
        return items

    async def _fetch_candidates(self, collection: str, filt: dict) -> list[dict]:
        """Resolve the candidate docs for a filter, preferring keyed access.

        Strategy (mirrors FileMCPClient's "fast path vs. scan" split, plus a
        GSI/partition-key Query optimization so production avoids full Scans
        for the common owner-scoped / chat / firebase_uid reads):

        1. ``_id`` equality on an ``_id``-keyed table -> get_item.
        2. partition-key equality on a non-``_id``-keyed table (chat
           ``case_id``) -> Query.
        3. a top-level filter attr with a GSI -> Query the GSI, then apply the
           residual filter (``_matches``) client-side.
        4. ``$or`` whose branches are all single-attr GSI equalities (the
           owner-scoping ``$or:[{user_id},{owner_user_id}]``) -> Query each GSI
           and union, de-duped by ``_id``.
        5. fallback -> full Scan + ``_matches``.

        Always returns a list to which the caller applies ``_matches`` for any
        residual predicates (steps 3/4 may over-fetch; ``_matches`` narrows).
        """
        table = self._table(collection)
        alias = _alias_for(collection)
        pk = _pk_attr(alias)
        sk = _sk_attr(alias)

        # 1. _id-keyed table, _id equality -> get_item (off the event loop).
        if pk == "_id" and isinstance(filt.get("_id"), str):
            resp = await asyncio.to_thread(
                table.get_item, Key={"_id": filt["_id"]}
            )
            item = resp.get("Item")
            return [_from_ddb(item)] if item else []

        # 2. partition-key equality on a composite-key table (chat) -> Query.
        if pk != "_id" and isinstance(filt.get(pk), str) and sk is not None:
            return await self._query_pk(table, alias, filt[pk])

        gsis = _TABLE_GSIS.get(alias, {})

        # 4. owner-scoping $or over GSI-backed branches -> union of GSI Queries.
        or_clauses = filt.get("$or")
        if isinstance(or_clauses, list) and or_clauses:
            attrs = []
            ok = True
            for sub in or_clauses:
                if (
                    isinstance(sub, dict)
                    and len(sub) == 1
                    and isinstance(next(iter(sub.values())), str)
                    and next(iter(sub.keys())) in gsis
                ):
                    attrs.append(next(iter(sub.items())))
                else:
                    ok = False
                    break
            if ok and attrs:
                merged: dict[str, dict] = {}
                for attr, val in attrs:
                    for doc in await self._query_gsi(table, gsis[attr], attr, val):
                        did = doc.get("_id")
                        merged[did if did is not None else id(doc)] = doc
                return list(merged.values())

        # 3. single top-level GSI-backed attr -> Query that GSI.
        for attr, gsi_name in gsis.items():
            val = filt.get(attr)
            if isinstance(val, str):
                return await self._query_gsi(table, gsi_name, attr, val)

        # 5. fallback: full Scan.
        return await self._scan_all(table)

    # ------------------------------------------------------------------ #
    # MCP tool surface
    # ------------------------------------------------------------------ #

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        args = dict(arguments or {})
        collection = args.get("collection")
        if not collection:
            raise ValueError(
                f"DynamoMCPClient: tool {name!r} requires a 'collection' argument"
            )
        # The 'database' arg is meaningless for a per-table DynamoDB layout —
        # table naming is driven by the prefix env, NOT the database arg. Accept
        # and discard it (file backend scopes by subdir; we scope by prefix).
        table = self._table(collection)
        alias = _alias_for(collection)

        if name == "insert-one":
            doc = dict(args["document"])
            doc_id = doc.get("_id")
            if doc_id is None:
                raise ValueError("DynamoMCPClient insert-one: document missing '_id'")
            # A1 FIX 1: put_item off the event loop.
            await asyncio.to_thread(table.put_item, Item=_ddb_item(doc, alias))
            return {"insertedId": doc_id}

        if name == "update-one":
            filt = args.get("filter", {})
            update = args.get("update", {})
            upsert = bool(args.get("upsert", False))
            return await self._update(
                table, alias, filt, update, upsert=upsert, many=False
            )

        if name == "update-many":
            filt = args.get("filter", {})
            update = args.get("update", {})
            # The migrate_preauth_cases path never upserts; honor an explicit
            # upsert flag anyway for surface completeness.
            upsert = bool(args.get("upsert", False))
            return await self._update(
                table, alias, filt, update, upsert=upsert, many=True
            )

        if name == "find-one":
            filt = args.get("filter", {})
            candidates = await self._fetch_candidates(collection, filt)
            for doc in candidates:
                if self._matches(doc, filt):
                    return {"document": doc}
            return {"document": None}

        if name == "find":
            filt = args.get("filter", {})
            sort = args.get("sort", {}) or {}
            limit = args.get("limit")
            candidates = await self._fetch_candidates(collection, filt)
            results = [d for d in candidates if self._matches(d, filt)]
            if sort:
                key = next(iter(sort.keys()))
                direction = sort[key]
                results.sort(
                    key=lambda d: d.get(key, ""),
                    reverse=(direction == -1),
                )
            if isinstance(limit, int) and limit >= 0:
                results = results[:limit]
            return {"documents": results}

        raise NotImplementedError(
            f"DynamoMCPClient: unsupported MCP tool {name!r} "
            f"(supports insert-one / update-one / update-many / find-one / find)"
        )

    # ------------------------------------------------------------------ #
    # Shared update path (read-modify-write — Mongo-faithful via _apply_update)
    # ------------------------------------------------------------------ #

    async def _update(
        self,
        table,
        alias: str,
        filt: dict,
        update: dict,
        *,
        upsert: bool,
        many: bool,
    ) -> dict[str, Any]:
        """Apply an update to matching docs via read-modify-write put_item.

        We deliberately use read-modify-write (get/scan -> _apply_update ->
        put_item) rather than native UpdateExpressions so the operator
        semantics are GUARANTEED identical to FileMCPClient._apply_update —
        the single source of truth for $set/$setOnInsert/$push/$addToSet
        behavior. At demo scale the extra read is negligible; production-scale
        write contention is an OQ (documented in the job risks), not a v0.1
        concern.

        Upsert: only meaningful when the filter is an ``_id`` equality on an
        ``_id``-keyed table (the only upsert shape Persistence sends). A fresh
        doc seeded with the ``_id`` is created and ``$setOnInsert`` fires.

        A1 FIX 1 (atomicity): every boto3 call now runs in a worker thread
        (``asyncio.to_thread``) so it never freezes the WS loop. That makes the
        read-modify-write window race-prone — two coroutines updating the same
        ``_id`` (e.g. the per-turn ``touch_session`` + a concurrent chart
        ``$push`` onto the SAME session doc) could each get_item the SAME
        snapshot in different threads, apply their op, and put_item last-wins,
        DROPPING the other's mutation. The per-``_id`` ``asyncio.Lock`` below
        serializes the whole get->apply->put for one document key so the
        ``$push``/``$addToSet`` accumulators (session ``charts`` /
        ``project_ids``) stay intact; different ids never contend.
        """
        pk = _pk_attr(alias)
        target_id = filt.get("_id")
        matched = 0
        modified = 0

        if pk == "_id" and isinstance(target_id, str):
            async with self._lock_for_id(alias, target_id):
                resp = await asyncio.to_thread(
                    table.get_item, Key={"_id": target_id}
                )
                existing = resp.get("Item")
                if existing is not None:
                    doc = _from_ddb(existing)
                    self._apply_update(doc, update, inserting=False)
                    await asyncio.to_thread(
                        table.put_item, Item=_ddb_item(doc, alias)
                    )
                    return {"matchedCount": 1, "modifiedCount": 1}
                if upsert:
                    fresh: dict[str, Any] = {"_id": target_id}
                    self._apply_update(fresh, update, inserting=True)
                    await asyncio.to_thread(
                        table.put_item, Item=_ddb_item(fresh, alias)
                    )
                    return {"matchedCount": 1, "modifiedCount": 1}
                return {"matchedCount": 0, "modifiedCount": 0}

        # Non-_id filter (firebase_uid stamp, migrate_preauth_cases
        # $exists:false, secrets user_id stamp). Resolve candidates (GSI Query
        # when possible, else Scan), apply _matches, update first (update-one)
        # or all (update-many) by their primary key. Each matched doc's
        # put-back is serialized on its own ``_id`` lock so it can't race a
        # concurrent ``_id``-keyed update of the same document.
        candidates = await self._fetch_candidates_for_update(table, alias, filt)
        for doc in candidates:
            if not self._matches(doc, filt):
                continue
            doc_id = doc.get(pk)
            async with self._lock_for_id(alias, doc_id):
                self._apply_update(doc, update, inserting=False)
                await asyncio.to_thread(
                    table.put_item, Item=_ddb_item(doc, alias)
                )
            matched += 1
            modified += 1
            if not many:
                break
        return {"matchedCount": matched, "modifiedCount": modified}

    async def _fetch_candidates_for_update(
        self, table, alias: str, filt: dict
    ) -> list[dict]:
        """Candidate resolution for the non-_id update path.

        Like ``_fetch_candidates`` but bound to an already-resolved table (we
        don't have the logical collection name here, only the alias + table).
        Prefers a GSI Query for a single GSI-backed attr; else full Scan.
        ``$exists:false`` filters (migrate_preauth_cases) cannot use a GSI
        index (the attr is absent), so they correctly fall to Scan.
        """
        gsis = _TABLE_GSIS.get(alias, {})
        for attr, gsi_name in gsis.items():
            val = filt.get(attr)
            if isinstance(val, str):
                return await self._query_gsi(table, gsi_name, attr, val)
        return await self._scan_all(table)


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #


def make_dynamo_persistence(
    *, table_prefix: str | None = None, resource: Any = None
) -> "Persistence":  # noqa: F821 — forward ref, imported lazily to avoid cycle
    """Construct a ``Persistence`` backed by :class:`DynamoMCPClient`.

    Mirror of ``persistence.make_file_persistence``. ``resource`` injects a
    boto3-resource-shaped object for tests; production omits it and boto3
    resolves the EC2 instance-role creds + region (same chain as
    ``bedrock_adapter._bedrock_client``).
    """
    from .persistence import Persistence

    return Persistence(DynamoMCPClient(table_prefix=table_prefix, resource=resource))


__all__ = [
    "DynamoMCPClient",
    "make_dynamo_persistence",
    "PERSISTENCE_BACKEND_ENV",
    "DYNAMO_TABLE_PREFIX_ENV",
    "DEFAULT_TABLE_PREFIX",
]
