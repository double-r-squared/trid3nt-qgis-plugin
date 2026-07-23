"""Thin typed wrapper around MongoDB Atlas MCP server CRUD operations (FR-AS-4).

Pattern: agent code calls ``Persistence.upsert_case(case_dataclass)`` — this
module calls the MongoDB MCP server's ``insert-one`` / ``update-one`` /
``find-one`` / ``find`` tools and serializes/deserializes through the
``trid3nt_contracts`` ``GraceModel`` types (NEVER raw dicts at the call site).

This is the **LLM-facing DB path** per FR-AS-4 and Decision F. Worker-side
direct-driver writes (``engine``'s solver result inserts, see FR-MP-3) are a
separate seam that does NOT route through this module.

Job-0115 scope (sprint-12-mega Wave 1.5):
- ``CaseSummary`` round-trip: get / upsert / list / archive / delete
- ``CaseChatMessage`` append + ``CaseSessionState`` hydration
- ``User`` round-trip: ``get_user_by_firebase_uid`` / ``upsert_user``
- ``SecretRecord`` round-trip (vault-ref-only — Decision F): list / upsert /
  revoke
- ``append_audit`` — fire-and-forget audit log line

Containment discipline (per agent.md):
- This module does NOT open a direct PyMongo driver. Every storage call goes
  through ``mcp_client.call_tool("<mcp-method>", args)`` so the agent has a
  single LLM-facing DB seam.
- The MCP server is consumed verbatim (``mongodb-mcp-server`` npm package);
  we don't wrap it, we delegate to it. The agent code that calls this module
  passes typed ``GraceModel`` instances in and gets typed instances out — the
  ``dict``-shape MCP transport is contained here.
- The session-record write carveout (Appendix D.6, FR-AS-8) is implemented at
  the confirmation-hook layer (``server.CONFIRMATION_TRIGGERS``), not here.
  Persistence is the I/O substrate; the hook policy is per-call.

Invariants this module is responsible for:
- **Decision F (wire isolation).** ``SecretRecord`` serialization NEVER carries
  a raw key value. ``key_value`` only ever appears on the ``secret-add``
  *envelope* (cleared at the server boundary before persistence); the
  ``SecretRecord`` shape itself is vault-ref-only and is what this module
  upserts. The redaction back-stop is at the schema layer (``__repr_args__``
  on ``SecretAddEnvelopePayload``); persistence simply never receives a
  ``SecretAddEnvelopePayload`` — only ``SecretRecord``s.
- **9. No cost theater.** No quota / cost / spend fields on any record.
- **session-record carveout.** A ``sessions``-collection update (the agent's
  own session record) is NOT a confirmable write; a ``runs``-collection
  insert IS (Decision F + FR-AS-8). This module exposes both seams; the
  caller (``server.py``) is responsible for confirmation routing.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from trid3nt_contracts import new_ulid, now_utc
from trid3nt_contracts.case import (
    CaseChatMessage,
    CaseManifest,
    CaseManifestLayer,
    CaseOpenEnvelopePayload,
    CaseSessionState,
    CaseSummary,
)
from trid3nt_contracts.secrets import SecretRecord
from trid3nt_contracts.user import User

logger = logging.getLogger("trid3nt_server.persistence")

# MongoDB Atlas database used for all Case/User/Secret persistence at v0.1.
# Override via env var ``TRID3NT_MONGO_DB`` for staging / test isolation; the
# production deploy pins the database name via Secret Manager.
import os

DEFAULT_DATABASE = os.environ.get("TRID3NT_MONGO_DB", "trid3nt_dev")

# Lane A1 (pen=agent / paper=case): the durable runs bucket holds the
# materialized case-view SNAPSHOT (``case-views/{case_id}.json``) that the
# view-without-agent path serves via a pre-signed S3 GET (the agent box may be
# asleep). The bucket already holds solver decks/results and the agent already
# has S3 write creds to it (TRID3NT_RUNS_BUCKET / the EC2 instance role). Mirror
# the resolution used in ``tools/solver.py`` so a single env var moves both.
CASE_VIEWS_BUCKET = os.environ.get(
    "TRID3NT_RUNS_BUCKET", "trid3nt-runs"
)
#: Object-key prefix for materialized case-view snapshots (PRIVATE objects).
CASE_VIEWS_PREFIX = "case-views"

#: Object-key prefix for THIN per-case manifests (#165 data-island index).
#: Written ALONGSIDE the fat snapshot (dual-write) in the SAME durable runs
#: bucket so a future cold path can list cases + their layers from S3 with the
#: agent box asleep, WITHOUT downloading the fat snapshot per Case. Mirrors the
#: ``case-views`` prefix convention (PRIVATE objects, owner in S3 metadata).
CASE_MANIFESTS_PREFIX = "case-manifests"

#: Hard cap (bytes) on a single vector layer's inline GeoJSON that the snapshot
#: writer will embed cold. This is the cross-case resolve guard: a non-open-case
#: snapshot reads each vector's persisted ``.fgb`` / ``.geojson`` and embeds the
#: GeoJSON so the browser can paint it without the agent. The dense-vector
#: simplify+cap (``pipeline_emitter._densify_off_loop``) already bounds the live
#: wire payload, but a snapshot built for a NON-open case has no emitter, so we
#: re-apply a sane ceiling here. 250 MB mirrors the existing payload norm's
#: hard-block (>250 MB is never inlined; the layer stays URI-only and is logged).
CASE_VIEW_INLINE_GEOJSON_MAX_BYTES = 250 * 1024 * 1024


def case_view_snapshot_key(case_id: str) -> str:
    """Return the S3 object key for a Case's materialized view snapshot.

    Single seam so the writer (here) and the signer (infra lane's Lambda) name
    the object identically: ``case-views/{case_id}.json``.
    """
    return f"{CASE_VIEWS_PREFIX}/{case_id}.json"


def case_manifest_key(case_id: str) -> str:
    """Return the S3 object key for a Case's thin manifest (#165 data-island).

    Single seam so the writer (here) and the future cold-serve reader name the
    object identically: ``case-manifests/{case_id}.json``. Mirrors
    ``case_view_snapshot_key`` exactly, swapping the prefix.
    """
    return f"{CASE_MANIFESTS_PREFIX}/{case_id}.json"

# Collection names — pinned by Appendix D nomenclature (D.2 ``projects`` for
# Cases, D.6 ``sessions`` for chat history, D.13 ``users`` for the
# forward-looking Auth track stub, D.14 ``secrets`` for §F.3 per-Case keys,
# D.15 ``audit_log`` for the fire-and-forget audit stream).
CASES_COLLECTION = "projects"  # FR-MP-5/-6: Case <-> projects 1:1
CHAT_COLLECTION = "case_chat_messages"  # per-turn message log (FR-MP-6)
SESSIONS_COLLECTION = "sessions"  # D.6 — agent's own session records
USERS_COLLECTION = "users"  # D.13 (Auth/Users track stub)
SECRETS_COLLECTION = "secrets"  # §F.3 per-Case secrets
AUDIT_COLLECTION = "audit_log"  # fire-and-forget audit stream


# --------------------------------------------------------------------------- #
# MCP client protocol — duck-typed so tests can pass a mock
# --------------------------------------------------------------------------- #


class MCPClientProtocol(Protocol):
    """Minimal MCP client surface this module depends on.

    Matches ``trid3nt_server.mcp.MCPClient.call_tool`` so the live client (the
    stdio-launched ``mongodb-mcp-server`` subprocess) drops in without
    adaptation. Tests pass a mock implementing this single method.
    """

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        ...


# --------------------------------------------------------------------------- #
# Live-server surface translation (job-0203 / Wave 4.11 M4)
# --------------------------------------------------------------------------- #
#
# FINDING (2026-06-09, live protocol smoke against mongodb-mcp-server@latest):
# the real npm server does NOT expose ``find-one`` / ``insert-one`` /
# ``update-one`` at all. Its actual document surface is ``find`` /
# ``insert-many`` / ``update-many`` (+ ``delete-many``, ``count``, ...), and
# ``find`` results come back as EJSON wrapped in
# ``<untrusted-user-data-{uuid}>`` tags in the SECOND content entry — the
# first is a human-readable "Found N documents" banner. Every Persistence
# call written against the logical surface would have failed on first
# contact with production.
#
# Resolution: the logical surface (``find-one``/``insert-one``/
# ``update-one``/``find``) is OUR seam contract (``MCPClientProtocol``) —
# ``FileMCPClient``, every test mock, and every call site speak it. This
# translator is the single boundary that adapts the logical surface to the
# real server's tool names and response shape. When MongoDB renames tools
# again, this class is the only thing that changes.
#
# ``server.init_persistence_from_env`` wraps the live ``MCPClient`` in this
# translator before handing it to ``Persistence``.


def _ejson_normalize(value: Any) -> Any:
    """Collapse the EJSON extended-type wrappers we can encounter.

    Our documents store string ULIDs and ISO-8601 strings, so most
    round-trips are plain JSON. Mongo may still emit ``{"$date": ...}`` /
    ``{"$oid": ...}`` / ``{"$numberLong": ...}`` for fields written by
    other paths — collapse them to their plain value so Pydantic
    validation sees normal scalars.
    """
    if isinstance(value, dict):
        if len(value) == 1:
            ((k, v),) = value.items()
            if k == "$oid":
                return v
            if k == "$numberLong" or k == "$numberInt" or k == "$numberDouble":
                try:
                    return float(v) if "." in str(v) else int(v)
                except (TypeError, ValueError):
                    return v
            if k == "$date":
                # {"$date": "ISO"} or {"$date": {"$numberLong": "ms"}}
                if isinstance(v, dict) and "$numberLong" in v:
                    return v["$numberLong"]
                return v
        return {k: _ejson_normalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_ejson_normalize(v) for v in value]
    return value


import re as _re

# The warning prose MENTIONS both tags inline ("between the <tag> and
# </tag> tags may lead to...") BEFORE the actual payload block — a lazy
# match from the first mention captures the prose word "and" instead of
# the payload. The real block is newline-delimited (``<tag>\npayload\n</tag>``
# per formatUntrustedData), so the mandatory ``\n`` on both sides skips the
# prose mentions. Verified against a live mongod round-trip (evidence/).
_UNTRUSTED_RE = _re.compile(
    r"<untrusted-user-data-([0-9a-fA-F-]+)>\n(.*?)\n</untrusted-user-data-\1>",
    _re.DOTALL,
)


def _extract_untrusted_payload(raw: dict[str, Any]) -> Any | None:
    """Pull the EJSON document payload out of a real-server tool result.

    Returns the parsed (and EJSON-normalized) payload, or ``None`` when no
    untrusted-data block is present (e.g. "Found 0 documents" responses).
    """
    content = raw.get("content")
    if not isinstance(content, list):
        return None
    for entry in content:
        if not isinstance(entry, dict):
            continue
        text = entry.get("text")
        if not isinstance(text, str):
            continue
        m = _UNTRUSTED_RE.search(text)
        if not m:
            continue
        import json as _json

        try:
            return _ejson_normalize(_json.loads(m.group(2)))
        except _json.JSONDecodeError:
            logger.warning("untrusted-data block was not valid EJSON")
            return None
    return None


class MCPSurfaceTranslator:
    """Adapt the logical MCP surface to the real ``mongodb-mcp-server``.

    Implements :class:`MCPClientProtocol`. Wraps a raw client (the live
    stdio :class:`trid3nt_server.mcp.MCPClient`) whose tool names are the
    REAL server surface, and translates:

    - ``find-one``   → ``find`` with ``limit=1`` → ``{"document": doc|None}``
    - ``find``       → ``find`` with an explicit generous limit (the real
      server DEFAULTS TO limit=10 — unbounded logical reads like chat
      history would silently truncate) → ``{"documents": [...]}``
    - ``insert-one`` → ``insert-many`` with ``documents=[doc]``
    - ``update-one`` → ``update-many`` (every update in this codebase filters on a
      unique key, so the semantics coincide)

    Any other tool name passes through untouched.
    """

    #: Explicit limit injected when the logical ``find`` has none. The
    #: real server also caps responses at ``responseBytesLimit`` (1 MiB
    #: default) — we raise it for chat-history reads; documents beyond
    #: either cap surface as OQ-0203-FIND-PAGINATION.
    DEFAULT_FIND_LIMIT = 1000
    RESPONSE_BYTES_LIMIT = 8 * 1024 * 1024

    def __init__(self, raw_client: MCPClientProtocol) -> None:
        self._raw = raw_client

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        args = dict(arguments or {})

        if name == "find-one":
            real = {
                "database": args["database"],
                "collection": args["collection"],
                "filter": args.get("filter", {}),
                "limit": 1,
            }
            raw = await self._raw.call_tool("find", real)
            docs = _extract_untrusted_payload(raw)
            doc = docs[0] if isinstance(docs, list) and docs else None
            return {"document": doc}

        if name == "find":
            real = {
                "database": args["database"],
                "collection": args["collection"],
                "filter": args.get("filter", {}),
                "limit": args.get("limit", self.DEFAULT_FIND_LIMIT),
                "responseBytesLimit": self.RESPONSE_BYTES_LIMIT,
            }
            if args.get("sort"):
                real["sort"] = args["sort"]
            raw = await self._raw.call_tool("find", real)
            docs = _extract_untrusted_payload(raw)
            if docs is None:
                docs = []
            if isinstance(docs, dict):
                docs = [docs]
            return {"documents": docs}

        if name == "insert-one":
            raw = await self._raw.call_tool(
                "insert-many",
                {
                    "database": args["database"],
                    "collection": args["collection"],
                    "documents": [args["document"]],
                },
            )
            return raw if isinstance(raw, dict) else {}

        if name == "update-one":
            real = {
                "database": args["database"],
                "collection": args["collection"],
                "filter": args.get("filter", {}),
                "update": args.get("update", {}),
            }
            if args.get("upsert"):
                real["upsert"] = True
            raw = await self._raw.call_tool("update-many", real)
            return raw if isinstance(raw, dict) else {}

        return await self._raw.call_tool(name, args)


# --------------------------------------------------------------------------- #
# Persistence wrapper
# --------------------------------------------------------------------------- #


def _unwrap_mcp_result(raw: dict[str, Any]) -> Any:
    """Extract the structured payload from an MCP ``tools/call`` result.

    The MCP protocol returns results in a ``content`` array. ``mongodb-mcp-server``
    populates the first entry's ``text`` field with a JSON string for document
    operations. Best-effort: if the shape doesn't match we surface ``None`` so
    callers can branch on "no document" vs "raw dict already parsed".
    """
    if not isinstance(raw, dict):
        return raw
    # Direct dict already — e.g., when the mock test client returns a dict.
    if "content" not in raw and "document" not in raw and "documents" not in raw:
        return raw
    # mongodb-mcp-server: content[0].text is a JSON string
    content = raw.get("content")
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict) and isinstance(first.get("text"), str):
            import json as _json

            try:
                return _json.loads(first["text"])
            except _json.JSONDecodeError:
                return first["text"]
    # Some MCP variants emit ``document`` / ``documents`` directly.
    if "document" in raw:
        return raw["document"]
    if "documents" in raw:
        return raw["documents"]
    return raw


class Persistence:
    """Typed wrapper around the MongoDB Atlas MCP server.

    Construct with a live ``MCPClient`` (or any object implementing the
    ``MCPClientProtocol``). All methods are ``async`` — the underlying MCP
    transport is async stdio.
    """

    def __init__(
        self,
        mcp_client: MCPClientProtocol,
        *,
        database: str = DEFAULT_DATABASE,
    ) -> None:
        self._mcp = mcp_client
        self._db = database

    # ----- Cases (FR-MP-6) ------------------------------------------------- #

    async def get_case(self, case_id: str) -> CaseSummary | None:
        """Find one Case by id. Returns ``None`` if not found.

        Forward-compat: drops any field the ``ProjectDocument`` schema (D.2)
        carries that ``CaseSummary`` doesn't denormalize (e.g. ``deleted_at``,
        ``owner_user_id``, etc.). The Case envelope is a UI denormalization
        of the storage shape — extra storage fields are expected and ignored.
        """
        raw = await self._mcp.call_tool(
            "find-one",
            {
                "database": self._db,
                "collection": CASES_COLLECTION,
                "filter": {"_id": case_id},
            },
        )
        doc = _unwrap_mcp_result(raw)
        if not doc or not isinstance(doc, dict):
            return None
        return self._doc_to_case_summary(doc)

    @staticmethod
    def _doc_to_case_summary(doc: dict) -> CaseSummary:
        """Normalize a stored projects document into a ``CaseSummary``.

        Strips ``_id`` (rewires to ``case_id``), drops user-link fields the
        schema doesn't know, and drops any other storage-only fields the
        denormalized envelope doesn't carry — including the #147 ephemeral-case
        ``expires_at`` TTL stamp, which is storage-only and must NEVER reach the
        wire ``CaseSummary`` (the ``k not in allowed`` filter below already
        drops it, since ``expires_at`` is not a ``CaseSummary`` field).
        """
        allowed = set(CaseSummary.model_fields.keys())
        normalized: dict[str, object] = {}
        for k, v in doc.items():
            if k == "_id":
                continue
            if k in {"user_id", "owner_user_id"}:
                continue
            if k not in allowed:
                # storage-only field (e.g. user_id, expires_at TTL stamp) —
                # never surfaced to the wire CaseSummary.
                continue
            normalized[k] = v
        if "case_id" not in normalized and "_id" in doc:
            normalized["case_id"] = doc["_id"]
        return CaseSummary.model_validate(normalized)

    async def upsert_case(
        self,
        case: CaseSummary,
        *,
        owner_user_id: str | None = None,
        ephemeral: bool = False,
    ) -> CaseSummary:
        """Insert or update a Case. Returns the persisted ``CaseSummary``.

        Uses MCP ``update-one`` with ``upsert=True`` so a fresh Case lands and
        an existing one is overwritten in a single round-trip.

        job-0252 (sprint-13.5, OQ-0115-CASE-USER-LINK): when ``owner_user_id``
        is provided, it is stamped onto the document's ``user_id`` field so the
        Case belongs to its creator. ``CaseSummary`` itself carries no owner
        field (it is a UI denormalization), so ownership lives only at the
        storage layer — the read path (``_doc_to_case_summary``) deliberately
        drops it. Without this, every newly-created Case would lack a
        ``user_id`` and become invisible to ``list_cases_for_user`` now that
        the ``$exists:false`` leak clause is gone. ``owner_user_id=None``
        (the legacy / dev call shape) writes no owner — those Cases are then
        swept by the one-time ``migrate_preauth_cases`` startup step.

        The owner is written under ``$set``, so re-upserting an existing Case
        with a fresh ``owner_user_id`` updates it; passing ``None`` never
        clears an already-stamped owner (the ``user_id`` key is simply absent
        from the ``$set``).

        #147 ephemeral-cases track: ``ephemeral=True`` (only ever passed for
        ANONYMOUS / pre-Auth Cases) stamps a NUMERIC epoch-seconds
        ``expires_at`` (``int(now + CASES_ANON_TTL_SECONDS)``) so DynamoDB-native
        TTL can reap the Case after the window. This is intentionally a Number
        attribute, NOT the ISO ``expires_at`` string the sessions collection
        uses — DynamoDB TTL only honours a numeric epoch. ``expires_at`` is a
        storage-only field; ``_doc_to_case_summary`` drops it so it NEVER
        reaches the wire ``CaseSummary``.

        ``ephemeral=False`` (the DEFAULT, and the only shape authed call-sites
        ever use) writes NO ``expires_at`` at all — authed Cases are durable
        forever. This default is exactly byte-compatible with the prior
        behaviour, so the new kwarg is dormant until a call-site opts in.
        """
        body = case.model_dump(mode="json")
        body["_id"] = case.case_id  # MongoDB primary key (FR-MP-5)
        if owner_user_id:
            body["user_id"] = owner_user_id
        if ephemeral:
            from trid3nt_contracts.collections import CASES_ANON_TTL_SECONDS

            # DynamoDB-native TTL requires a NUMBER epoch-seconds attribute
            # (not the ISO string sessions use). Authed Cases never reach here.
            body["expires_at"] = int(now_utc().timestamp()) + CASES_ANON_TTL_SECONDS
        await self._mcp.call_tool(
            "update-one",
            {
                "database": self._db,
                "collection": CASES_COLLECTION,
                "filter": {"_id": case.case_id},
                "update": {"$set": body},
                "upsert": True,
            },
        )
        return case

    # ------------------------------------------------------------------ #
    # ADR 0014: per-Case short layer-handle map (storage-only field)
    # ------------------------------------------------------------------ #

    async def set_case_layer_handles(
        self, case_id: str, handles: dict[str, str]
    ) -> None:
        """Persist a Case's ``{L<n>: uri}`` short-handle map (ADR 0014).

        Storage-only ``layer_handles`` field on the cases doc — the
        ``last_active_case_id`` pattern: ``CaseSummary`` deliberately does
        NOT carry it (``_doc_to_case_summary`` drops unknown keys), so the
        wire contract stays narrow while the storage doc accretes. The
        ``upsert_case`` full-body ``$set`` never removes it (named-field
        semantics). ``upsert=False``: a deleted / never-created Case is not
        resurrected by this side-channel — the write is simply a no-op.
        Callers treat this as best-effort (wrap + log, never raise).
        """
        await self._mcp.call_tool(
            "update-one",
            {
                "database": self._db,
                "collection": CASES_COLLECTION,
                "filter": {"_id": case_id},
                "update": {"$set": {"layer_handles": dict(handles)}},
                "upsert": False,
            },
        )

    async def get_case_layer_handles(
        self, case_id: str
    ) -> dict[str, str] | None:
        """Read back the persisted ``{L<n>: uri}`` map (ADR 0014).

        Tolerant: a missing Case / absent field / malformed shape yields
        ``None`` and the registry degrades to fresh minting. Only
        str->str entries survive the shape filter.
        """
        raw = await self._mcp.call_tool(
            "find-one",
            {
                "database": self._db,
                "collection": CASES_COLLECTION,
                "filter": {"_id": case_id},
            },
        )
        doc = _unwrap_mcp_result(raw)
        if not isinstance(doc, dict):
            return None
        value = doc.get("layer_handles")
        if not isinstance(value, dict):
            return None
        out = {
            k: v
            for k, v in value.items()
            if isinstance(k, str) and k and isinstance(v, str) and v
        }
        return out or None

    async def migrate_preauth_cases(self, anon_uid: str) -> int:
        """One-time, idempotent: stamp pre-Auth Cases with ``anon_uid``.

        OQ-0115-CASE-USER-LINK (job-0252, sprint-13.5): Cases written before
        the Auth track carry no ``user_id`` field. The old
        ``{"user_id": {"$exists": False}}`` clause in ``list_cases_for_user``
        leaked every such Case to every signed-in user. This migration
        assigns ``user_id = anon_uid`` (the ``MIGRATION_ANON_UID`` sentinel)
        to every Case that lacks a ``user_id``, so a pre-Auth Case belongs to
        one synthetic owner instead of leaking.

        **Idempotent** by construction: the filter is
        ``{"user_id": {"$exists": False}}``, so a second run matches nothing
        (every Case now has a ``user_id``). Re-running is a safe no-op.

        **Non-corrupting**: a single ``$set`` of one field via the logical
        ``update-one`` surface (translated to ``update-many`` by the
        :class:`MCPSurfaceTranslator` so ALL matching orphans are stamped in
        one round-trip — ``update-one`` semantics would only touch one doc).
        No other field is read, written, or removed; sessions and chat
        histories are untouched (this method only ever writes the ``projects``
        collection).

        Returns the modified count when the backend reports one, else ``0``.
        Best-effort on count parsing — the migration's success is the absence
        of orphans on the next run, not the returned integer.
        """
        raw = await self._mcp.call_tool(
            "update-many",
            {
                "database": self._db,
                "collection": CASES_COLLECTION,
                "filter": {"user_id": {"$exists": False}},
                "update": {"$set": {"user_id": anon_uid}},
            },
        )
        # Best-effort: surface the modified count for the startup log. The
        # real server returns a text/EJSON blob; the mock/file backends return
        # a plain dict. Tolerate every shape.
        modified = 0
        payload = _unwrap_mcp_result(raw) if isinstance(raw, dict) else raw
        if isinstance(payload, dict):
            for k in ("modifiedCount", "modified_count", "nModified"):
                v = payload.get(k)
                if isinstance(v, int):
                    modified = v
                    break
        logger.info(
            "pre-Auth case migration: stamped %s orphan case(s) with user_id=%s",
            modified,
            anon_uid,
        )
        return modified

    async def adopt_cases_to_user(self, user_id: str) -> int:
        """Re-own EVERY case not already owned by ``user_id`` (local mode, F1).

        TRID3NT local-build single-user seam (live-feedback 2026-07-09): the
        old per-client anonymous users each minted their own cases, so the
        case list forked per device. When local mode collapses auth onto the
        one fixed local user (``auth_handshake.LOCAL_SINGLE_USER_ID``), this
        sweep adopts the strays -- one ``update-many`` setting ``user_id`` on
        every case doc whose owner differs (``$nin`` also matches a MISSING
        ``user_id``, Mongo-faithful, so pre-auth orphans are adopted too).

        Idempotent: after one run every case is owned by ``user_id`` and the
        filter matches nothing. ONLY called from the local-mode auth path --
        never wired on the cloud stack, where blanket adoption would be a
        cross-tenant ownership transfer.

        Returns the modified count when the backend reports one, else ``0``.
        """
        raw = await self._mcp.call_tool(
            "update-many",
            {
                "database": self._db,
                "collection": CASES_COLLECTION,
                "filter": {"user_id": {"$nin": [user_id]}},
                "update": {"$set": {"user_id": user_id}},
            },
        )
        modified = 0
        payload = _unwrap_mcp_result(raw) if isinstance(raw, dict) else raw
        if isinstance(payload, dict):
            for k in ("modifiedCount", "modified_count", "nModified"):
                v = payload.get(k)
                if isinstance(v, int):
                    modified = v
                    break
        return modified

    async def list_cases_for_user(self, user_id: str) -> list[CaseSummary]:
        """List the user's LIVE Cases (``status="active"`` only).

        v0.1 Auth-stub note: the ``projects`` collection schema does not
        currently carry a ``user_id`` field (FR-MP-5 was specified pre-Auth).
        We pass the filter anyway — once the Auth/Users track adds the field
        the query starts narrowing; until then it returns the full Case list
        for the deployment. Surfaced as OQ-0115-CASE-USER-LINK.

        job-0267 (server-side case-list hardening): soft-deleted and archived
        Cases are excluded HERE, in the query AND a post-validation guard —
        the user saw a deleted ghost in the left rail because exclusion was
        previously a client-side concern. The ``$nin`` filter still matches
        docs with no ``status`` field at all (pre-status records are live by
        definition: ``CaseSummary.status`` defaults to ``"active"``); the
        Python guard is the belt-and-suspenders for MCP backends whose filter
        dialect quietly ignores the operator.

        job-0252 (sprint-13.5, OQ-0115-CASE-USER-LINK): the
        ``{"user_id": {"$exists": False}}`` backward-compat clause is GONE.
        It used to leak every pre-Auth Case (no ``user_id``) to every
        signed-in user. The one-time startup migration
        (``migrate_preauth_cases``) now stamps those orphan Cases with
        ``MIGRATION_ANON_UID``, so a Case is visible only to its owner.
        """
        raw = await self._mcp.call_tool(
            "find",
            {
                "database": self._db,
                "collection": CASES_COLLECTION,
                "filter": {
                    "$or": [
                        {"user_id": user_id},
                        {"owner_user_id": user_id},
                    ],
                    # job-0267: tombstones never reach the wire.
                    "status": {"$nin": ["deleted", "archived"]},
                },
            },
        )
        docs = _unwrap_mcp_result(raw)
        # If the MCP server returned no filter match, ``docs`` may be empty
        # list or None. Be tolerant.
        if not docs:
            return []
        if isinstance(docs, dict):
            docs = [docs]
        cases: list[CaseSummary] = []
        for d in docs:
            if not isinstance(d, dict):
                continue
            try:
                case = self._doc_to_case_summary(d)
            except Exception:  # noqa: BLE001 — skip malformed docs
                logger.warning("skipping malformed Case doc: %s", d)
                continue
            if case.status in ("deleted", "archived"):
                # job-0267 guard: backend ignored/mangled the $nin filter.
                continue
            cases.append(case)
        return cases

    async def list_all_active_case_ids(self) -> list[str]:
        """List EVERY live Case's id, across all owners (cold-snapshot backfill).

        COLDVIEW FRESHNESS BACKFILL (box-wake): the case-view snapshot +
        thin manifest are only ever (re)written while the agent box is UP
        (the 4 mutation triggers + case-open). A Case that gained layers and
        was then left as the box auto-stopped keeps a stale/empty cold face
        until it is warm-reopened. The box-wake startup sweep
        (``_run_coldview_backfill``) re-materializes a fresh snapshot+manifest
        for every live Case so a box-off owned Case serves a CURRENT cold
        face without a live agent connection. That sweep needs the FULL live
        Case set, NOT one user's — hence this owner-agnostic enumerator.

        Returns only the ``_id`` (case_id) strings — the snapshot/manifest
        writers re-source the full doc per Case, so the sweep needs nothing
        more. Tombstones (``deleted`` / ``archived``) are excluded in the
        query AND the Python guard (same belt-and-suspenders as
        ``list_cases_for_user``). Best-effort: a malformed doc is skipped.
        """
        raw = await self._mcp.call_tool(
            "find",
            {
                "database": self._db,
                "collection": CASES_COLLECTION,
                "filter": {"status": {"$nin": ["deleted", "archived"]}},
            },
        )
        docs = _unwrap_mcp_result(raw)
        if not docs:
            return []
        if isinstance(docs, dict):
            docs = [docs]
        ids: list[str] = []
        for d in docs:
            if not isinstance(d, dict):
                continue
            # The Case id is the document _id (CASES are _id<->case_id 1:1).
            cid = d.get("_id") or d.get("case_id")
            if not isinstance(cid, str) or not cid:
                continue
            # Python guard: backend ignored/mangled the $nin filter.
            if d.get("status") in ("deleted", "archived"):
                continue
            ids.append(cid)
        return ids

    async def archive_case(self, case_id: str) -> None:
        """Soft-archive a Case (sets ``status="archived"``).

        Preserves the document for un-archive; ``delete_case`` is the hard
        path. Mirrors ``CaseStatus`` Literal in ``trid3nt_contracts.case``.
        """
        await self._mcp.call_tool(
            "update-one",
            {
                "database": self._db,
                "collection": CASES_COLLECTION,
                "filter": {"_id": case_id},
                "update": {
                    "$set": {
                        "status": "archived",
                        "updated_at": now_utc().isoformat().replace("+00:00", "Z"),
                    }
                },
            },
        )

    async def delete_case(self, case_id: str) -> None:
        """Soft-delete a Case (sets ``status="deleted"``).

        v0.1 stance: soft-delete only. A future job lands a curator-tooled
        hard delete; data-retention rules (D.2 ``deleted_at``) point this way
        anyway. Status mirrors the ``CaseStatus`` Literal tombstone value.
        """
        await self._mcp.call_tool(
            "update-one",
            {
                "database": self._db,
                "collection": CASES_COLLECTION,
                "filter": {"_id": case_id},
                "update": {
                    "$set": {
                        "status": "deleted",
                        "deleted_at": now_utc().isoformat().replace("+00:00", "Z"),
                    }
                },
            },
        )

    # ----- Chat history + session state (FR-MP-6 rehydration) ------------- #

    async def append_chat_message(self, msg: CaseChatMessage) -> None:
        """Append one persisted chat exchange to a Case's history.

        Per FR-AS-8 the chat-message collection is the agent's own session
        record (it is per-turn replay material, not a solver result), so this
        write is NOT a confirmation trigger — the caller does not need to
        gate it. The carveout is enforced at the confirmation-hook layer.
        """
        body = msg.model_dump(mode="json")
        body["_id"] = msg.message_id
        await self._mcp.call_tool(
            "insert-one",
            {
                "database": self._db,
                "collection": CHAT_COLLECTION,
                "document": body,
            },
        )

    async def upsert_chat_message(self, msg: CaseChatMessage) -> None:
        """Insert-or-replace one chat row keyed by its stable ``message_id``.

        Durable-card lifecycle (NATE "nothing transient"): an off-box SOLVE card
        is persisted ``running`` at mint and UPDATED IN PLACE to its terminal
        state. Unlike ``append_chat_message`` (always a fresh row), this upserts
        by the stable ``_id`` so the running -> terminal transition rewrites the
        SAME row — never a duplicate. ``created_at`` is pinned on first insert
        via ``$setOnInsert`` so the row KEEPS its position in the
        ``created_at``-sorted replay across the transition (the terminal update
        must not reorder the card). Every other field is ``$set`` so the terminal
        ``state`` / ``duration_ms`` / ``tool_card`` overwrite the running values.

        Routes through the SAME ``update-one`` (upsert) surface every backend
        implements. The filter carries BOTH key shapes so it targets the natural
        key on each: ``_id`` for the file/Mongo backends (chat ``_id`` ==
        ``message_id``) AND the composite ``case_id`` + ``message_id`` the live
        DynamoDB chat table is keyed by — so the get/apply/put upsert lands on
        exactly one row everywhere. Best-effort at the call sites
        (``_persist_chat_turn`` swallows write failures), matching
        ``append_chat_message``.
        """
        body = msg.model_dump(mode="json")
        body["_id"] = msg.message_id
        created_at = body.pop("created_at", None)
        update: dict[str, Any] = {"$set": body}
        if created_at is not None:
            update["$setOnInsert"] = {"created_at": created_at}
        await self._mcp.call_tool(
            "update-one",
            {
                "database": self._db,
                "collection": CHAT_COLLECTION,
                "filter": {
                    "_id": msg.message_id,
                    "case_id": msg.case_id,
                    "message_id": msg.message_id,
                },
                "update": update,
                "upsert": True,
            },
        )

    async def get_session_state(self, case_id: str) -> CaseSessionState:
        """Hydrate the rehydration envelope for a Case (FR-MP-6 resume).

        Joins the Case header (``CaseSummary``) with its ordered chat history
        from ``CHAT_COLLECTION``. ``loaded_layers`` / ``pipeline_history`` /
        ``current_pipeline`` are passed through as dicts — collections.py
        owns the concrete shapes (matches the ``SessionStatePayload`` pattern
        already in ws.py).
        """
        case = await self.get_case(case_id)
        if case is None:
            # Surface a minimal placeholder so the caller can decide how to
            # handle "Case not found" without raising through the MCP layer.
            return CaseSessionState(
                case=CaseSummary(
                    case_id=case_id,
                    title="(missing)",
                    created_at=now_utc(),
                    updated_at=now_utc(),
                    status="deleted",
                ),
            )
        # Chat history, oldest-first
        raw = await self._mcp.call_tool(
            "find",
            {
                "database": self._db,
                "collection": CHAT_COLLECTION,
                "filter": {"case_id": case_id},
                "sort": {"created_at": 1},
            },
        )
        docs = _unwrap_mcp_result(raw) or []
        if isinstance(docs, dict):
            docs = [docs]
        chat: list[CaseChatMessage] = []
        for d in docs:
            if not isinstance(d, dict):
                continue
            normalized = {k: v for k, v in d.items() if k != "_id"}
            try:
                chat.append(CaseChatMessage.model_validate(normalized))
            except Exception:  # noqa: BLE001
                logger.warning("skipping malformed CaseChatMessage doc: %s", d)
                continue
        # job-0267: deterministic replay order regardless of backend sort
        # support — the full stream (user turns, tool cards, agent narration)
        # interleaves by ``created_at``; ULID ``message_id`` breaks ties in
        # write order. Python's sort is stable, so backends that already
        # honored the ``created_at`` sort are untouched.
        chat.sort(key=lambda m: (m.created_at, m.message_id))
        # job-0172 Part B: hydrate ``loaded_layers`` from the persisted
        # ``Case.loaded_layer_summaries`` so a Case re-open repopulates the
        # LayerPanel deterministically. The PipelineEmitter holds these in
        # memory per-connection; without this hydration step a browser
        # refresh (new WS, new emitter) shows an empty LayerPanel even
        # though the layers are still published on the per-Case ``.qgs``.
        loaded_layers = list(case.loaded_layer_summaries)
        # job-0294b (sprint-14-aws): hydrate persisted charts so a Case re-open
        # replays them WITHOUT a re-run. job-0230 ``$push``es SessionChartRecords
        # onto the ``sessions`` doc (keyed by case_id == sessions._id) but the
        # read side was never wired. Pull the array, unwrap each record's
        # ``payload`` (the ChartEmissionPayload the client rehydrates), in
        # emitted_at order. Best-effort: a missing/odd doc yields no charts.
        charts: list[dict] = []
        try:
            sraw = await self._mcp.call_tool(
                "find-one",
                {
                    "database": self._db,
                    "collection": SESSIONS_COLLECTION,
                    "filter": {"_id": case_id},
                },
            )
            sdoc = _unwrap_mcp_result(sraw)
            if isinstance(sdoc, dict) and isinstance(sdoc.get("charts"), list):
                records = [r for r in sdoc["charts"] if isinstance(r, dict)]
                records.sort(key=lambda r: r.get("emitted_at") or "")
                for r in records:
                    payload = r.get("payload")
                    if isinstance(payload, dict):
                        charts.append(payload)
        except Exception:  # noqa: BLE001 — chart replay is best-effort
            logger.warning("get_session_state: chart hydration failed case=%s", case_id)
        return CaseSessionState(
            case=case, chat_history=chat, loaded_layers=loaded_layers, charts=charts,
        )

    # ----- Materialized case-view snapshot (Lane A1: view-without-agent) ---- #

    async def build_case_view_snapshot(
        self,
        case_id: str,
        *,
        inline_geojson_by_layer_id: dict[str, Any] | None = None,
        density_meta_by_layer_id: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Assemble the materialized case-view snapshot dict (no I/O).

        The snapshot is the EXACT payload ``server._emit_case_open`` ships on the
        wire — ``CaseOpenEnvelopePayload(session_state=get_session_state(...))``
        serialized with ``model_dump(mode="json")`` — so the web's existing
        ``useCases.onCaseOpen`` + ``App.tsx`` synthesize path renders it
        verbatim from S3 with the agent box OFF.

        The ONE addition is the inline vector GeoJSON: persisted vector layers
        carry no inline GeoJSON (the side-table is in-memory on the live
        emitter; ``server.reinline_vector_layers`` repopulates it only for an
        OPEN socket). For a true cold view we MERGE that GeoJSON (and any
        dense-vector ``vector_density`` tag) onto the matching ``loaded_layers``
        entries here — byte-for-byte the same merge ``emit_session_state``
        performs on the live wire (additive ``inline_geojson`` / density fields).

        Cross-case inline (job-0372 FIX B): the explicit
        ``inline_geojson_by_layer_id`` is only ever populated by the OPEN-case
        emitter, so a snapshot for a NON-open case (a cross-case mutation - e.g.
        rename Case B while Case A is open) would otherwise strand its vectors as
        an agent-only ``s3://...fgb|geojson`` handle the browser cannot read cold
        (Invariant-5). After the fast-path merge we therefore do a SECOND pass:
        for any vector layer STILL missing ``inline_geojson`` that carries a
        readable object-store URI, we READ the ``.fgb`` / ``.geojson`` from the
        store and embed the resolved GeoJSON at write time, regardless of which
        Case is open. This is the layer-handle->data-URI lesson: persist the
        resolved DATA, not a handle the agent must later resolve. Absurdly large
        GeoJSON (> ``CASE_VIEW_INLINE_GEOJSON_MAX_BYTES``) is skipped + flagged so
        the cold snapshot stays within the payload norm.

        Pure w.r.t. S3 PUT: builds and returns the dict (``write_case_view_snapshot``
        does the put). The cross-case pass may READ vector artifacts from the
        object store (best-effort per layer; a missing/oversized/corrupt artifact
        skips that layer, never raises).
        """
        session_state = await self.get_session_state(case_id)
        payload = CaseOpenEnvelopePayload(session_state=session_state)
        snapshot = payload.model_dump(mode="json")
        inline = inline_geojson_by_layer_id or {}
        density = density_meta_by_layer_id or {}
        ss = snapshot.get("session_state")
        if not isinstance(ss, dict):
            return snapshot
        layers = ss.get("loaded_layers")
        if not isinstance(layers, list):
            return snapshot
        # Pass 1 (fast path) - merge the OPEN-case emitter's inline GeoJSON /
        # density tags into loaded_layers, mirroring
        # PipelineEmitter.emit_session_state EXACTLY (same field names, same
        # best-effort density-tag handling) so a cold view paints vectors and a
        # warm case-open are indistinguishable to the client.
        for layer in layers:
            if not isinstance(layer, dict):
                continue
            lid = layer.get("layer_id")
            geojson_obj = inline.get(lid)
            if geojson_obj is not None:
                layer["inline_geojson"] = geojson_obj
            meta = density.get(lid)
            if meta is not None:
                try:
                    layer.update(meta.as_wire_tag())
                except Exception:  # noqa: BLE001 - match live merge
                    pass
        # Pass 2 (cross-case resolve) - embed inline GeoJSON for any vector layer
        # the emitter did NOT supply (i.e. this snapshot is for a non-open Case),
        # reading the artifact straight from its persisted object-store URI so a
        # cold reopen never sees a vector-URI-only entry the browser cannot read.
        await self._resolve_cross_case_vector_inline(layers, case_id=case_id)
        return snapshot

    async def _resolve_cross_case_vector_inline(
        self, layers: list[Any], *, case_id: str
    ) -> int:
        """Embed inline GeoJSON for vector layers missing it, reading from S3.

        For every VECTOR layer dict that does NOT already carry
        ``inline_geojson`` and has a readable object-store ``uri``, read the
        ``.fgb`` / ``.geojson`` artifact and embed the resolved GeoJSON
        FeatureCollection. Reuses the emitter's ``_read_vector_uri_as_geojson``
        (the SAME read+reproject+densify path the live wire uses) so a cold
        snapshot vector is byte-equivalent to a warm one. Rasters are untouched
        (their resolved TiTiler tile template already renders cold).

        Best-effort by contract: a missing / unreadable / oversized artifact
        skips that layer and is logged, never raised - a vector-inline hiccup
        must never break the snapshot write (turn-safety discipline). Returns the
        number of layers newly inlined (for tests / telemetry).
        """
        import json as _json

        # Lazy import: pipeline_emitter does NOT import persistence, so this is a
        # one-way edge (no circular import), but keep it local so importing
        # persistence stays cheap and the emitter's heavy deps load only when a
        # cross-case vector actually needs resolving.
        from .pipeline_emitter import _read_vector_uri_as_geojson

        count = 0
        for layer in layers:
            if not isinstance(layer, dict):
                continue
            if layer.get("layer_type") != "vector":
                continue
            if layer.get("inline_geojson") is not None:
                # Pass 1 already inlined it (open-case fast path) - leave as-is.
                continue
            uri = layer.get("uri")
            if not isinstance(uri, str) or not uri:
                continue
            # Scale-to-zero P3 (blueprint 2.5.7): a layer that misses this inline
            # is written to the snapshot with its bare object-store uri, which the
            # cold box-off viewer cannot fetch -- the layer is INVISIBLE until the
            # next snapshot rebuild. One transient S3 hiccup must not cause that,
            # so retry the read once before giving up on the layer.
            geojson_obj = None
            for attempt in (1, 2):
                try:
                    geojson_obj = await _read_vector_uri_as_geojson(uri)
                except Exception:  # noqa: BLE001 - per-layer best-effort
                    geojson_obj = None
                if geojson_obj is not None:
                    break
                if attempt == 1:
                    import asyncio as _aio  # local: matches this method's lazy-import style

                    await _aio.sleep(0.5)
            if geojson_obj is None:
                logger.warning(
                    "case-view-snapshot cross-case inline read failed after retry "
                    "case=%s layer_id=%s uri=%s -- layer will be invisible in the "
                    "cold box-off view until the next snapshot rebuild",
                    case_id,
                    layer.get("layer_id"),
                    uri,
                )
                continue
            # Guard absurdly large GeoJSON against the payload norm: skip + flag
            # so the cold snapshot never balloons past the hard-block ceiling.
            try:
                size = len(
                    _json.dumps(geojson_obj, separators=(",", ":")).encode("utf-8")
                )
            except Exception:  # noqa: BLE001 - un-serializable -> skip safely
                logger.warning(
                    "case-view-snapshot cross-case inline unserializable case=%s "
                    "layer_id=%s uri=%s",
                    case_id,
                    layer.get("layer_id"),
                    uri,
                )
                continue
            if size > CASE_VIEW_INLINE_GEOJSON_MAX_BYTES:
                logger.warning(
                    "case-view-snapshot cross-case inline SKIPPED (too large: "
                    "%d bytes > %d) case=%s layer_id=%s uri=%s",
                    size,
                    CASE_VIEW_INLINE_GEOJSON_MAX_BYTES,
                    case_id,
                    layer.get("layer_id"),
                    uri,
                )
                continue
            layer["inline_geojson"] = geojson_obj
            count += 1
        return count

    async def _resolve_case_owner(self, case_id: str) -> str | None:
        """Resolve a Case's owner from the RAW ``projects`` doc (best-effort).

        Reads ``owner_user_id`` (preferred) or ``user_id`` straight off the
        stored document — the same owner-link fields ``list_cases_for_user``
        filters on — BEFORE the owner-stripping ``_doc_to_case_summary`` runs.
        Those fields are deliberately dropped from the ``CaseSummary`` envelope
        (and therefore from the snapshot BODY), so the snapshot writer must read
        them from the raw doc here to carry the owner in S3 OBJECT METADATA.

        Returns ``None`` when the Case is missing or carries no owner link
        (the legacy / pre-Auth shape). Best-effort: any read hiccup yields
        ``None`` so a snapshot write is never blocked on the owner probe.
        """
        try:
            raw = await self._mcp.call_tool(
                "find-one",
                {
                    "database": self._db,
                    "collection": CASES_COLLECTION,
                    "filter": {"_id": case_id},
                },
            )
            doc = _unwrap_mcp_result(raw)
            if not isinstance(doc, dict):
                return None
            owner = doc.get("owner_user_id") or doc.get("user_id")
            return owner if isinstance(owner, str) and owner else None
        except Exception:  # noqa: BLE001 — owner probe is best-effort
            logger.warning(
                "case-view-snapshot owner probe failed case=%s", case_id
            )
            return None

    async def write_case_view_snapshot(
        self,
        case_id: str,
        *,
        inline_geojson_by_layer_id: dict[str, Any] | None = None,
        density_meta_by_layer_id: dict[str, Any] | None = None,
        s3_put: Any = None,
    ) -> bool:
        """Materialize the case-view snapshot to S3 (view-without-agent path).

        Writes ``s3://{CASE_VIEWS_BUCKET}/case-views/{case_id}.json`` (PRIVATE;
        ``content-type: application/json``) so the signer Lambda (infra lane) can
        hand out a pre-signed GET and a user can VIEW a Case with the agent box
        asleep. Called on every Case MUTATION (layer publish, per-turn persist,
        case create/rename) — idempotent, last-write-wins.

        Owner-gate carrier (adversarial-review fix): the snapshot BODY strips the
        owner-link fields (``_doc_to_case_summary`` drops ``user_id`` /
        ``owner_user_id``), so the signer could never owner-match off the body.
        We resolve the owner from the RAW ``projects`` doc and carry it in S3
        OBJECT METADATA (``owner-user-id``) — NEVER in the JSON body. The signer
        reads it cheaply via ``head_object`` (no full download). The metadata key
        is set ONLY when the Case has an owner; the BODY is byte-identical with
        or without an owner.

        Best-effort by contract: wrapped in ``try/except`` and returns ``False``
        on any failure so a snapshot hiccup NEVER breaks the user's turn (the
        same discipline as ``touch_session`` / chart persistence). Returns
        ``True`` on a successful put.

        ``s3_put`` injects a callable
        ``(bucket, key, body_bytes, metadata) -> None`` for tests (a fake S3
        capture; ``metadata`` is ``{"owner-user-id": <owner>}`` or ``{}`` when
        the Case has no owner); production lazily constructs a boto3 S3 client
        whose creds boto3 resolves from the EC2 instance role (same chain as
        the dense-vector reader).
        """
        import json

        try:
            snapshot = await self.build_case_view_snapshot(
                case_id,
                inline_geojson_by_layer_id=inline_geojson_by_layer_id,
                density_meta_by_layer_id=density_meta_by_layer_id,
            )
            body = json.dumps(snapshot, separators=(",", ":")).encode("utf-8")
            key = case_view_snapshot_key(case_id)
            # Owner lives ONLY in object metadata, never in the body. S3
            # lowercases metadata keys — use the lowercase key directly so the
            # signer's ``resp["Metadata"].get("owner-user-id")`` matches.
            owner = await self._resolve_case_owner(case_id)
            metadata: dict[str, str] = (
                {"owner-user-id": owner} if owner else {}
            )
            if s3_put is not None:
                _maybe = s3_put(CASE_VIEWS_BUCKET, key, body, metadata)
                # Allow either a sync or async injected put.
                if hasattr(_maybe, "__await__"):
                    await _maybe
            else:
                await self._default_s3_put_case_view(key, body, metadata)
            logger.debug(
                "case-view-snapshot wrote s3://%s/%s bytes=%d owner=%s",
                CASE_VIEWS_BUCKET,
                key,
                len(body),
                owner or "(none)",
            )
            return True
        except Exception:  # noqa: BLE001 — never break a turn
            logger.warning(
                "case-view-snapshot write failed case=%s bucket=%s",
                case_id,
                CASE_VIEWS_BUCKET,
            )
            return False

    @staticmethod
    async def _default_s3_put_case_view(
        key: str, body: bytes, metadata: dict[str, str] | None = None
    ) -> None:
        """Production S3 put for the case-view snapshot.

        Runs the synchronous boto3 ``put_object`` in a worker thread so the
        async turn loop is never blocked (the same off-thread discipline the
        DynamoDB backend uses). boto3 resolves creds + region from the standard
        chain (env / ~/.aws / EC2 instance role — job-0289 lesson).

        ``metadata`` is the S3 OBJECT METADATA dict (the owner-gate carrier:
        ``{"owner-user-id": <owner>}`` or ``{}`` / ``None`` when the Case has no
        owner). Passed through to boto3 ``put_object(Metadata=...)`` ONLY when
        non-empty so an owner-less snapshot stamps no metadata. The owner is
        carried here, NOT in the JSON body, so the body stays byte-identical.
        """
        import asyncio

        meta = dict(metadata or {})

        def _put() -> None:
            import boto3

            s3 = boto3.client(
                "s3", region_name=os.environ.get("AWS_REGION", "us-west-2")
            )
            kwargs: dict[str, Any] = dict(
                Bucket=CASE_VIEWS_BUCKET,
                Key=key,
                Body=body,
                ContentType="application/json",
            )
            if meta:
                kwargs["Metadata"] = meta
            s3.put_object(**kwargs)

        await asyncio.to_thread(_put)

    # ----- Thin per-case manifest (#165 data-island cold-serve index) ------ #
    #
    # SIBLINGS of the case-view snapshot writers above. Written ALONGSIDE the
    # fat snapshot (dual-write) at the SAME Case mutation call-sites; the
    # snapshot is NOT retired here (cold serving + retirement are later phases).
    # A future cold path lists cases + their layers from these thin manifests
    # WITHOUT the agent and WITHOUT downloading the fat snapshot per Case.

    @staticmethod
    def _manifest_layer_from_summary(layer: dict) -> CaseManifestLayer | None:
        """Project ONE persisted ``ProjectLayerSummary`` dict into a manifest row.

        The layer list is sourced from the Case doc's
        ``loaded_layer_summaries`` — the SAME data ``list_cases`` / the live
        ``case-list`` marshals — so the manifest never diverges from the rail.

        ``asset_url`` is the DISPLAY face the cold path serves: every
        ``observe_published_layer`` registration routes the renderable face
        (raster tile-template, vector ``.geojson`` asset, or QGIS WMS GetMap
        URL) into ``wms_url``, so we prefer that and fall back to ``uri`` only
        when no display face was registered. ``wms_url`` is carried separately
        ONLY when it is a WMS GetMap face (so a reader can tell a true WMS layer
        from a tile/geojson asset); for non-WMS display faces it stays ``None``.

        Returns ``None`` for a malformed entry (missing required keys) so a bad
        row is skipped rather than failing the whole manifest write.
        """
        if not isinstance(layer, dict):
            return None
        layer_id = layer.get("layer_id")
        name = layer.get("name")
        layer_type = layer.get("layer_type")
        style_preset = layer.get("style_preset")
        if not (layer_id and name and layer_type and style_preset is not None):
            return None
        display = layer.get("wms_url")
        uri = layer.get("uri")
        asset_url = display or uri
        if not asset_url:
            return None
        # Only carry wms_url separately when it is a genuine WMS GetMap face —
        # ``_looks_like_wms`` is the same predicate ``observe_published_layer``
        # uses to route a display URL into the wms slot.
        from .uri_registry import _looks_like_wms

        wms_url = display if (display and _looks_like_wms(display)) else None
        try:
            return CaseManifestLayer(
                layer_id=str(layer_id),
                name=str(name),
                layer_type=layer_type,
                style_preset=str(style_preset),
                asset_url=str(asset_url),
                wms_url=wms_url,
            )
        except Exception:  # noqa: BLE001 — skip a row that won't validate
            logger.warning(
                "case-manifest: skipping malformed layer row layer_id=%s",
                layer_id,
            )
            return None

    async def build_case_manifest(self, case_id: str) -> CaseManifest | None:
        """Assemble the thin ``CaseManifest`` for a Case (no I/O beyond the read).

        Sources every field from the Case doc the left rail already consumes
        (``get_case`` -> ``CaseSummary``): ``title`` / ``bbox`` /
        ``primary_hazard`` and the ``loaded_layer_summaries`` layer list.
        ``updated_at`` stamps the manifest materialization time.

        Returns ``None`` when the Case is missing (the writer then no-ops) so a
        manifest is never written for a non-existent Case.
        """
        case = await self.get_case(case_id)
        if case is None:
            return None
        layers: list[CaseManifestLayer] = []
        for entry in case.loaded_layer_summaries:
            row = self._manifest_layer_from_summary(entry)
            if row is not None:
                layers.append(row)
        return CaseManifest(
            case_id=case.case_id,
            updated_at=now_utc(),
            title=case.title,
            bbox=case.bbox,
            primary_hazard=case.primary_hazard,
            layers=layers,
        )

    async def write_case_manifest(
        self, case_id: str, *, s3_put: Any = None
    ) -> bool:
        """Materialize the thin Case manifest to S3 (#165 data-island index).

        Writes ``s3://{CASE_VIEWS_BUCKET}/case-manifests/{case_id}.json``
        (PRIVATE; ``content-type: application/json``) ALONGSIDE the fat
        case-view snapshot — a dual-write at the SAME Case mutation call-sites.
        Idempotent, last-write-wins.

        Owner-gate carrier: identical to the snapshot — the owner is resolved
        from the RAW ``projects`` doc and carried in S3 OBJECT METADATA
        (``owner-user-id``), NEVER in the JSON body, so the signer can owner-gate
        off a cheap ``head_object``. The metadata key is set ONLY when the Case
        has an owner.

        Best-effort by contract: wrapped in ``try/except`` and returns ``False``
        on ANY failure (a missing Case, a build error, an S3 error) so a manifest
        hiccup NEVER breaks the snapshot path or the user's turn — the SAME
        discipline as ``write_case_view_snapshot``. Returns ``True`` on a
        successful put.

        ``s3_put`` injects a callable
        ``(bucket, key, body_bytes, metadata) -> None`` for tests; production
        lazily constructs a boto3 S3 client (creds from the EC2 instance role).
        """
        import json

        try:
            manifest = await self.build_case_manifest(case_id)
            if manifest is None:
                # No such Case -> nothing to materialize (not an error).
                return False
            body = json.dumps(
                manifest.model_dump(mode="json"), separators=(",", ":")
            ).encode("utf-8")
            key = case_manifest_key(case_id)
            owner = await self._resolve_case_owner(case_id)
            metadata: dict[str, str] = {"owner-user-id": owner} if owner else {}
            if s3_put is not None:
                _maybe = s3_put(CASE_VIEWS_BUCKET, key, body, metadata)
                if hasattr(_maybe, "__await__"):
                    await _maybe
            else:
                await self._default_s3_put_case_manifest(key, body, metadata)
            logger.debug(
                "case-manifest wrote s3://%s/%s bytes=%d layers=%d owner=%s",
                CASE_VIEWS_BUCKET,
                key,
                len(body),
                len(manifest.layers),
                owner or "(none)",
            )
            return True
        except Exception:  # noqa: BLE001 — never break the snapshot/turn path
            logger.warning(
                "case-manifest write failed case=%s bucket=%s",
                case_id,
                CASE_VIEWS_BUCKET,
            )
            return False

    @staticmethod
    async def _default_s3_put_case_manifest(
        key: str, body: bytes, metadata: dict[str, str] | None = None
    ) -> None:
        """Production S3 put for the thin Case manifest (mirrors the snapshot put).

        Runs the synchronous boto3 ``put_object`` in a worker thread so the
        async turn loop is never blocked (no-sync-blocking-on-loop norm). boto3
        resolves creds + region from the standard chain (env / ~/.aws / EC2
        instance role). ``metadata`` is the owner-gate carrier, passed to
        ``put_object(Metadata=...)`` ONLY when non-empty.
        """
        import asyncio

        meta = dict(metadata or {})

        def _put() -> None:
            import boto3

            s3 = boto3.client(
                "s3", region_name=os.environ.get("AWS_REGION", "us-west-2")
            )
            kwargs: dict[str, Any] = dict(
                Bucket=CASE_VIEWS_BUCKET,
                Key=key,
                Body=body,
                ContentType="application/json",
            )
            if meta:
                kwargs["Metadata"] = meta
            s3.put_object(**kwargs)

        await asyncio.to_thread(_put)

    # ----- Session records (D.6 ``sessions`` collection) ------------------- #
    #
    # job-0203 (Wave 4.11 M4): the agent's own session record goes live. The
    # ``sessions`` document is the TTL-cleaned activity header (D.6 +
    # ``SESSIONS_TTL``): who/when, which Cases were touched, and — since
    # job-0230 — the append-only ``charts`` array that chart-emission
    # ``$push``es onto. Chat content canonically lives in
    # ``case_chat_messages`` (FR-MP-6); ``SessionDocument.chat_history``
    # stays empty at v0.1 so the two stores never diverge.

    async def upsert_session_record(self, doc: "SessionDocument") -> None:
        """Insert or fully overwrite a session record.

        ``$set`` of the full document body — storage-only extras a previous
        ``$push`` added (e.g. ``charts``) survive because ``$set`` of named
        fields does not remove unnamed ones.
        """
        body = doc.model_dump(mode="json", by_alias=True)
        session_id = body.pop("_id")
        await self._mcp.call_tool(
            "update-one",
            {
                "database": self._db,
                "collection": SESSIONS_COLLECTION,
                "filter": {"_id": session_id},
                "update": {"$set": body},
                "upsert": True,
            },
        )

    async def touch_session(
        self,
        session_id: str,
        *,
        client_fingerprint: str | None = None,
        case_id: str | None = None,
        ttl_seconds: int | None = None,
    ) -> None:
        """Activity heartbeat for a session — one upsert round-trip.

        - ``$set`` ``last_active_at`` + ``expires_at`` (TTL driver, D.6) so
          every interaction pushes cleanup 30 days out (``SESSIONS_TTL``).
        - ``$setOnInsert`` the immutable header (``schema_version``,
          ``created_at``) so the first touch creates a well-formed record
          and later touches never rewrite history.
        - ``$addToSet`` the active Case into ``project_ids`` when given —
          deduped, so per-turn touches stay idempotent.

        Fire-and-forget discipline at call sites (same as telemetry M3 and
        chart persistence job-0230): callers wrap in ``try/except`` or a
        task; a persistence hiccup never takes down the user's turn.
        """
        from trid3nt_contracts.collections import SESSIONS_TTL

        now = now_utc()
        ttl = ttl_seconds if ttl_seconds is not None else SESSIONS_TTL["expire_after_seconds"]
        from datetime import timedelta

        iso_now = now.isoformat().replace("+00:00", "Z")
        iso_exp = (now + timedelta(seconds=ttl)).isoformat().replace("+00:00", "Z")
        set_fields: dict[str, Any] = {
            "last_active_at": iso_now,
            "expires_at": iso_exp,
        }
        if client_fingerprint is not None:
            set_fields["client_fingerprint"] = client_fingerprint
        update: dict[str, Any] = {
            "$set": set_fields,
            "$setOnInsert": {
                "schema_version": "v1",
                "created_at": iso_now,
            },
        }
        if case_id is not None:
            update["$addToSet"] = {"project_ids": case_id}
        await self._mcp.call_tool(
            "update-one",
            {
                "database": self._db,
                "collection": SESSIONS_COLLECTION,
                "filter": {"_id": session_id},
                "update": update,
                "upsert": True,
            },
        )
        # Header repair: a session doc created by an earlier bare ``$push``
        # (chart-emission upserts before any touch — job-0230 ordering) has
        # no ``created_at``/``schema_version``, and ``$setOnInsert`` above
        # can never backfill an EXISTING doc (real Mongo semantics too).
        # Detect and repair once; ``created_at=now`` is the best available
        # approximation for a doc whose true start was never recorded.
        raw = await self._mcp.call_tool(
            "find-one",
            {
                "database": self._db,
                "collection": SESSIONS_COLLECTION,
                "filter": {"_id": session_id},
            },
        )
        doc = _unwrap_mcp_result(raw)
        if isinstance(doc, dict) and (
            "created_at" not in doc or "schema_version" not in doc
        ):
            repair: dict[str, Any] = {}
            if "created_at" not in doc:
                repair["created_at"] = iso_now
            if "schema_version" not in doc:
                repair["schema_version"] = "v1"
            await self._mcp.call_tool(
                "update-one",
                {
                    "database": self._db,
                    "collection": SESSIONS_COLLECTION,
                    "filter": {"_id": session_id},
                    "update": {"$set": repair},
                },
            )

    async def touch_case(
        self,
        case_id: str,
        *,
        ttl_seconds: int | None = None,
    ) -> None:
        """Slide the TTL window on an EPHEMERAL (anonymous) Case (#147).

        Activity heartbeat for an anonymous Case: ``$set`` a fresh NUMERIC
        epoch-seconds ``expires_at`` (``int(now) + ttl``) on the case doc so a
        Case the user is actively working in is not reaped mid-session. Mirrors
        :meth:`touch_session`, but stamps a Number epoch (DynamoDB-native TTL),
        NOT the ISO string the sessions TTL index uses.

        Only ever called for anonymous Cases. Authed Cases carry no
        ``expires_at`` and must stay durable forever — server.py simply never
        invokes ``touch_case`` for them (the kwarg defaults keep this dormant
        until the call-site is wired).

        ``ttl_seconds`` defaults to ``CASES_ANON_TTL_SECONDS``. Unlike
        ``upsert_case``, this is a bare ``$set`` with NO ``upsert`` — it only
        slides an existing Case's window and never resurrects a reaped one.

        Fire-and-forget discipline (same as ``touch_session`` / telemetry M3):
        a persistence hiccup must never take down the user's turn, so any error
        is swallowed and logged rather than raised.
        """
        from trid3nt_contracts.collections import CASES_ANON_TTL_SECONDS

        ttl = ttl_seconds if ttl_seconds is not None else CASES_ANON_TTL_SECONDS
        try:
            expires_at = int(now_utc().timestamp()) + ttl
            await self._mcp.call_tool(
                "update-one",
                {
                    "database": self._db,
                    "collection": CASES_COLLECTION,
                    "filter": {"_id": case_id},
                    "update": {"$set": {"expires_at": expires_at}},
                },
            )
        except Exception:  # noqa: BLE001
            logger.warning("touch_case failed for case_id=%s", case_id, exc_info=True)

    async def set_session_active_case(
        self, session_id: str, case_id: str | None
    ) -> None:
        """Persist the session's active-Case pointer (job-CASE-AUTHORITY).

        Writes a storage-only ``last_active_case_id`` field onto the session
        record so the active-Case pointer survives an EC2 auto-stop/restart
        (the in-memory ``_SESSION_ACTIVE_CASE`` dict in server.py is wiped on
        process death). ``SessionDocument`` deliberately does NOT carry this
        field — it is storage-only, exactly like the job-0230 ``charts`` array;
        ``get_session_record`` drops unknown fields before validation, so the
        contract model stays narrow while the storage doc accretes.

        The client-stamped ``case_id`` on ``session-resume`` /
        ``user-message`` remains the REAL authority for turn-binding + replay;
        this persisted pointer is only the cold-start cache so a reconnecting
        client that sends a bare resume (older client, no stamp) still lands on
        the Case it last worked in instead of None.

        ``$set`` (with ``upsert``) so the pointer lands even if no prior
        ``touch_session`` created the doc; ``$setOnInsert`` mirrors
        ``touch_session`` so a doc created HERE first is still well-formed.
        ``case_id=None`` clears the pointer (an explicit Case exit).
        Fire-and-forget at call sites: a persistence hiccup must never take
        down the user's turn.
        """
        now = now_utc()
        iso_now = now.isoformat().replace("+00:00", "Z")
        await self._mcp.call_tool(
            "update-one",
            {
                "database": self._db,
                "collection": SESSIONS_COLLECTION,
                "filter": {"_id": session_id},
                "update": {
                    "$set": {"last_active_case_id": case_id},
                    "$setOnInsert": {
                        "schema_version": "v1",
                        "created_at": iso_now,
                    },
                },
                "upsert": True,
            },
        )

    async def get_session_active_case(self, session_id: str) -> str | None:
        """Read back the persisted active-Case pointer (job-CASE-AUTHORITY).

        Returns the ``last_active_case_id`` written by
        ``set_session_active_case``, or ``None`` when the session has no
        record / no persisted pointer (a fresh session, or one that never
        bound a Case). Used by server.py to reload the in-memory pointer when a
        fresh ``SessionState`` is built after an EC2 restart, so the cold-start
        cache survives process death. Best-effort: any malformed shape yields
        ``None``.
        """
        raw = await self._mcp.call_tool(
            "find-one",
            {
                "database": self._db,
                "collection": SESSIONS_COLLECTION,
                "filter": {"_id": session_id},
            },
        )
        doc = _unwrap_mcp_result(raw)
        if not isinstance(doc, dict):
            return None
        value = doc.get("last_active_case_id")
        return value if isinstance(value, str) else None

    async def get_session_record(self, session_id: str) -> "SessionDocument | None":
        """Read one session record back as a typed ``SessionDocument``.

        Tolerant normalization (same discipline as ``_doc_to_case_summary``):
        storage-only extras — notably the job-0230 ``charts`` array — are
        dropped before validation so the contract model stays narrow while
        the storage document accretes.
        """
        from trid3nt_contracts.collections import SessionDocument

        raw = await self._mcp.call_tool(
            "find-one",
            {
                "database": self._db,
                "collection": SESSIONS_COLLECTION,
                "filter": {"_id": session_id},
            },
        )
        doc = _unwrap_mcp_result(raw)
        if not doc or not isinstance(doc, dict):
            return None
        allowed = set(SessionDocument.model_fields.keys())
        # ``id`` is aliased to ``_id`` — keep the alias key, drop the rest.
        normalized = {
            k: v for k, v in doc.items() if k in allowed or k == "_id"
        }
        try:
            return SessionDocument.model_validate(normalized)
        except Exception:  # noqa: BLE001
            logger.warning("malformed session doc for session_id=%s", session_id)
            return None

    # ----- Users (Auth/Users track stub) ----------------------------------- #

    async def get_user_by_firebase_uid(self, uid: str) -> User | None:
        """Find a user by Firebase / Identity Platform UID."""
        raw = await self._mcp.call_tool(
            "find-one",
            {
                "database": self._db,
                "collection": USERS_COLLECTION,
                "filter": {"firebase_uid": uid},
            },
        )
        doc = _unwrap_mcp_result(raw)
        if not doc or not isinstance(doc, dict):
            return None
        normalized = {k: v for k, v in doc.items() if k != "_id"}
        if "user_id" not in normalized and "_id" in doc:
            normalized["user_id"] = doc["_id"]
        try:
            return User.model_validate(normalized)
        except Exception:  # noqa: BLE001
            logger.warning("malformed user doc for firebase_uid=%s", uid)
            return None

    async def upsert_user(self, user: User) -> User:
        """Insert or update a user record."""
        body = user.model_dump(mode="json")
        body["_id"] = user.user_id
        await self._mcp.call_tool(
            "update-one",
            {
                "database": self._db,
                "collection": USERS_COLLECTION,
                "filter": {"_id": user.user_id},
                "update": {"$set": body},
                "upsert": True,
            },
        )
        return user

    async def get_user_by_id(self, user_id: str) -> User | None:
        """Find a user by ULID. Returns ``None`` if not found.

        job-0172 Part C: the anonymous-fallback path needs an id-based lookup
        so a reconnecting browser can re-bind to the same ephemeral User via
        the ``AuthTokenEnvelope.anonymous_user_id`` hint. Mirrors the shape
        of ``get_user_by_firebase_uid`` so the call site stays symmetric.
        """
        raw = await self._mcp.call_tool(
            "find-one",
            {
                "database": self._db,
                "collection": USERS_COLLECTION,
                "filter": {"_id": user_id},
            },
        )
        doc = _unwrap_mcp_result(raw)
        if not doc or not isinstance(doc, dict):
            return None
        normalized = {k: v for k, v in doc.items() if k != "_id"}
        if "user_id" not in normalized:
            normalized["user_id"] = user_id
        # Forward-compat: drop fields the v0.1 schema doesn't carry so a
        # future User schema bump doesn't break the existing record.
        allowed = set(User.model_fields.keys())
        normalized = {k: v for k, v in normalized.items() if k in allowed}
        try:
            return User.model_validate(normalized)
        except Exception:  # noqa: BLE001
            logger.warning("malformed user doc for user_id=%s", user_id)
            return None

    # ----- Per-Case secrets (§F.3) ----------------------------------------- #

    async def list_secrets_refs(
        self,
        user_id: str,
        case_id: str | None = None,
    ) -> list[SecretRecord]:
        """List active secret records.

        Filters on ``is_active=True`` (revoked records are still in the
        collection for audit but excluded from the listing). If ``case_id`` is
        provided the filter narrows to per-Case records; otherwise returns
        every active record for the user.

        Decision F: the result NEVER includes the raw key value — only the
        ``vault_ref``. The schema enforces this at construct time.
        """
        filt: dict[str, Any] = {"is_active": True}
        if case_id is not None:
            filt["case_id"] = case_id
        # user_id linking is enforced once Auth lands. job-0252
        # (sprint-13.5): the ``{"user_id": {"$exists": False}}`` backward-
        # compat clause is GONE — it leaked pre-Auth secret records to every
        # user. A secret record belongs only to its owner.
        if user_id:
            filt["$or"] = [
                {"user_id": user_id},
                {"owner_user_id": user_id},
            ]
        raw = await self._mcp.call_tool(
            "find",
            {
                "database": self._db,
                "collection": SECRETS_COLLECTION,
                "filter": filt,
            },
        )
        docs = _unwrap_mcp_result(raw) or []
        if isinstance(docs, dict):
            docs = [docs]
        out: list[SecretRecord] = []
        for d in docs:
            if not isinstance(d, dict):
                continue
            normalized = {k: v for k, v in d.items() if k != "_id"}
            if "secret_id" not in normalized and "_id" in d:
                normalized["secret_id"] = d["_id"]
            normalized.pop("user_id", None)
            normalized.pop("owner_user_id", None)
            # Defensive: even though the schema rejects key_value, scrub
            # anything that looks like one before validation. This is the
            # "fail closed" backstop if a malformed write ever leaked.
            for k in list(normalized):
                if "key" in k and "value" in k.lower():
                    normalized.pop(k)
            try:
                out.append(SecretRecord.model_validate(normalized))
            except Exception:  # noqa: BLE001
                logger.warning("skipping malformed SecretRecord doc")
                continue
        return out

    async def upsert_secret_ref(self, sec: SecretRecord) -> SecretRecord:
        """Insert or update a vault-ref-only secret record.

        Decision F backstop: this method takes a ``SecretRecord`` (which has
        no ``key_value`` field at all). The agent service is responsible for
        writing the raw key value to the vault BEFORE calling this method
        and clearing the value from the in-memory envelope. The schema-side
        contract ensures the persistence layer cannot accidentally accept a
        raw key value.
        """
        body = sec.model_dump(mode="json")
        body["_id"] = sec.secret_id
        # Belt-and-braces: assert no key_value sneaked in via aliasing.
        for k in list(body):
            if "key" in k and "value" in k.lower():
                raise ValueError(
                    f"persistence refuses to write a key_value-shaped field "
                    f"({k!r}) — vault-ref only (Decision F)"
                )
        await self._mcp.call_tool(
            "update-one",
            {
                "database": self._db,
                "collection": SECRETS_COLLECTION,
                "filter": {"_id": sec.secret_id},
                "update": {"$set": body},
                "upsert": True,
            },
        )
        return sec

    async def revoke_secret(self, secret_id: str) -> None:
        """Soft-revoke a secret (sets ``is_active=False``).

        The vault entry is NOT deleted — preserves audit trail and lets the
        user un-revoke without re-entering the key. Mirrors §F.3 discipline.
        """
        await self._mcp.call_tool(
            "update-one",
            {
                "database": self._db,
                "collection": SECRETS_COLLECTION,
                "filter": {"_id": secret_id},
                "update": {"$set": {"is_active": False}},
            },
        )

    async def get_secret_value(self, secret_ref: "SecretRecord") -> str:
        """Read the live key value from the local file vault (job-0124).

        Called by Tier-2 fetchers (FIRMS / eBird / ERA5 / etc.) at
        tool-invocation time to materialize the raw key for the outbound
        HTTP request — including the credential-card RETRY path. The caller
        never logs the returned value.

        TRID3NT is the local product: there is exactly ONE vault backend
        (the local file vault, ``file-vault://…``). Legacy cloud refs
        (``aws-ssm://…``, ``gcp-sm://…``, bare GCP resource names, the
        interim ``local-file://…`` JSON store) can no longer resolve —
        ``secrets_handler.read_secret_value`` raises the typed
        ``SecretNotFoundError`` for them (never a crash, never a silent
        empty value); the credential-request card re-prompts the user.

        Fail-closed semantics:

        - If the record's ``is_active`` flag is ``False`` (soft-revoked),
          we raise ``SecretRevokedError`` BEFORE touching the vault so a
          revoked secret never resurrects via stale cache.
        - Otherwise resolution delegates to
          ``secrets_handler.read_secret_value`` (the single read seam).

        Args:
            secret_ref: the persisted ``SecretRecord`` (vault-ref only).

        Returns:
            The raw key value as a string. **Caller MUST NOT log this.**

        Raises:
            SecretRevokedError: when ``secret_ref.is_active is False``.
            SecretNotFoundError: when the vault_ref cannot be resolved
                (missing, malformed, or a legacy cloud scheme).
        """
        # Local import — avoids a circular dependency between persistence
        # and secrets_handler (which imports Persistence).
        from .secrets_handler import SecretRevokedError, read_secret_value

        if not secret_ref.is_active:
            raise SecretRevokedError(
                f"secret {secret_ref.secret_id!r} has been revoked "
                f"(provider={secret_ref.provider})"
            )

        return read_secret_value(secret_ref.vault_ref)

    # ----- Audit log -------------------------------------------------------- #

    async def append_audit(self, event_type: str, payload: dict) -> None:
        """Append one fire-and-forget audit event.

        Used by Decision M (claim provenance) and §F.3 catalog-amendment
        audit. Best-effort: callers should NOT block their happy path on
        this — wrap in ``try/except`` at the call site if the audit write
        failing would otherwise abort the user's action.
        """
        body = {
            "_id": new_ulid(),
            "event_type": event_type,
            "ts": now_utc().isoformat().replace("+00:00", "Z"),
            "payload": payload,
        }
        await self._mcp.call_tool(
            "insert-one",
            {
                "database": self._db,
                "collection": AUDIT_COLLECTION,
                "document": body,
            },
        )


# --------------------------------------------------------------------------- #
# Local-dev file-backed MCP client (job-0161, Wave 4.6)
# --------------------------------------------------------------------------- #
#
# The MongoDB Atlas MCP server is the production LLM-facing DB seam (FR-AS-4).
# For LOCAL DEV without Atlas/MCP, this file-backed shim satisfies the same
# ``MCPClientProtocol`` surface so the ``Persistence`` class above doesn't
# need to know which substrate it is talking to. The Persistence singleton
# can therefore be bound at startup regardless of whether MCP is provisioned,
# so the Case-create / select / archive / delete UI surface works on a fresh
# clone without Atlas credentials.
#
# Storage layout:
#   ``~/.trid3nt/dev_persistence/<database>/<collection>.json``
#   one JSON file per collection — a dict mapping ``_id`` → document
#
# Atomicity:
#   - per-collection ``asyncio.Lock`` serializes concurrent calls
#   - writes go to a sibling ``<collection>.json.tmp`` then ``os.replace``
#     (POSIX-atomic rename on the same filesystem)
#
# Scope (matches the subset of MCP tools Persistence actually invokes):
#   ``insert-one`` / ``update-one`` (with ``$set`` + optional ``upsert``) /
#   ``find-one`` / ``find`` (with optional sort by single key, ±1 direction).
#
# This is NOT a Mongo emulator — it's just enough query semantics to round-trip
# the Persistence layer's calls. When real MCP lands the Persistence singleton
# is constructed with the live ``MCPClient`` instead, and this file-backed
# shim is never instantiated.

import asyncio as _asyncio
import json as _json_for_file
import os as _os_for_file
from pathlib import Path as _Path

DEV_PERSISTENCE_DIR_ENV = "TRID3NT_DEV_PERSISTENCE_DIR"
DEV_PERSISTENCE_ENABLED_ENV = "TRID3NT_DEV_PERSISTENCE"


def _default_dev_persistence_dir() -> _Path:
    """Resolve the on-disk directory for the file-backed dev substrate.

    Override via ``TRID3NT_DEV_PERSISTENCE_DIR`` (used by tests + CI to point
    at a tmpdir). Default is ``~/.trid3nt/dev_persistence/`` per the job-0161
    kickoff so a fresh clone gets a stable, user-scoped location.
    """
    override = _os_for_file.environ.get(DEV_PERSISTENCE_DIR_ENV)
    if override:
        return _Path(override).expanduser()
    # One-time Layer-B rename migration: a pre-rename install kept its data
    # under ``~/.grace2``. If that directory exists and ``~/.trid3nt`` does
    # not, rename it in place so existing cases survive the rebrand.
    legacy_home = _Path.home() / ".grace2"
    new_home = _Path.home() / ".trid3nt"
    if legacy_home.is_dir() and not new_home.exists():
        _os_for_file.rename(legacy_home, new_home)
        logger.info("FilePersistence: migrated legacy dir %s -> %s", legacy_home, new_home)
    return new_home / "dev_persistence"


class FileMCPClient:
    """File-backed shim that satisfies :class:`MCPClientProtocol`.

    Implements the four MCP tool methods the :class:`Persistence` wrapper
    actually invokes (``insert-one``, ``update-one``, ``find-one``, ``find``)
    against a per-collection JSON file in ``base_dir / database / coll.json``.

    The return shape mirrors what ``Persistence._unwrap_mcp_result`` expects:
    we return a plain dict for single-document operations and a
    ``{"documents": [...]}`` envelope for list operations. This keeps the
    Persistence layer agnostic of substrate — the same code paths that
    deserialize MCP-server JSON responses deserialize our file payloads.
    """

    def __init__(self, base_dir: _Path | None = None) -> None:
        self._base_dir = base_dir or _default_dev_persistence_dir()
        self._base_dir.mkdir(parents=True, exist_ok=True)
        # One-time Layer-B rename migration: the default database subdir was
        # ``grace2_dev`` before the rebrand (see DEFAULT_DATABASE). If the old
        # subdir exists and the new one does not, rename it so existing
        # cases/layers/chat survive with zero data movement.
        _legacy_db_dir = self._base_dir / "grace2_dev"
        _new_db_dir = self._base_dir / "trid3nt_dev"
        if _legacy_db_dir.is_dir() and not _new_db_dir.exists():
            _os_for_file.rename(_legacy_db_dir, _new_db_dir)
            logger.info(
                "FilePersistence: migrated legacy database dir %s -> %s",
                _legacy_db_dir,
                _new_db_dir,
            )
        # collection-path -> asyncio.Lock, lazily allocated. Per-collection
        # rather than global so reads from one collection don't block another.
        self._locks: dict[str, _asyncio.Lock] = {}

    # ------------------------------------------------------------------ #
    # Storage helpers
    # ------------------------------------------------------------------ #

    def _collection_path(self, database: str, collection: str) -> _Path:
        db_dir = self._base_dir / database
        db_dir.mkdir(parents=True, exist_ok=True)
        return db_dir / f"{collection}.json"

    def _lock_for(self, path: _Path) -> _asyncio.Lock:
        key = str(path)
        lock = self._locks.get(key)
        if lock is None:
            lock = _asyncio.Lock()
            self._locks[key] = lock
        return lock

    @staticmethod
    def _read_store(path: _Path) -> dict[str, dict]:
        # OFF-LOOP CONTRACT: this is a BLOCKING body. Callers in ``call_tool``
        # run it via ``await _asyncio.to_thread(self._read_store, path)`` (the
        # file-backend twin of the DynamoMCPClient boto3 off-loop fix) so the
        # blocking read never stalls the asyncio WS loop. The per-collection
        # async lock is still held across the await, preserving serialization.
        if not path.exists():
            return {}
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = _json_for_file.load(fh)
        except (_json_for_file.JSONDecodeError, OSError) as exc:
            logger.warning(
                "FilePersistence: failed to read %s (%s); treating as empty",
                path,
                exc,
            )
            return {}
        if not isinstance(data, dict):
            return {}
        return data

    @staticmethod
    def _atomic_write(path: _Path, store: dict[str, dict]) -> None:
        """Atomic JSON write: tmp file + os.replace (POSIX-atomic rename)."""
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            # OPEN-28: default=str so a raw datetime in any document (e.g. the
            # shadow-telemetry ``called_at_utc``) serializes instead of raising
            # ``TypeError: Object of type datetime is not JSON serializable`` -
            # which was silently dropping the model-tagged shadow rows on the
            # file substrate (the per-model recall@k slice depends on them).
            _json_for_file.dump(store, fh, indent=2, sort_keys=True, default=str)
            fh.flush()
            try:
                _os_for_file.fsync(fh.fileno())
            except OSError:
                # fsync isn't available on every filesystem; the os.replace
                # below is still atomic on POSIX so we don't escalate.
                pass
        _os_for_file.replace(tmp, path)

    # ------------------------------------------------------------------ #
    # Query matcher — same subset MockMCPClient supports in tests
    # ------------------------------------------------------------------ #

    @staticmethod
    def _matches(doc: dict, filt: dict) -> bool:
        """Tiny query matcher: equality, ``$or``, ``$exists``, ``$nin``."""
        for k, v in filt.items():
            if k == "$or":
                if not any(FileMCPClient._matches(doc, sub) for sub in v):
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
                # Mongo-faithful: a MISSING field matches $nin (the doc's
                # value, None, is "not in" the exclusion list unless None is
                # listed). job-0267 uses this for the case-list status filter
                # so pre-status Case docs stay listed.
                if doc.get(k) in v["$nin"]:
                    return False
                continue
            if doc.get(k) != v:
                return False
        return True

    # ------------------------------------------------------------------ #
    # Update-operator application (job-0203 / M4)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _apply_update(doc: dict, update: dict, *, inserting: bool) -> None:
        """Apply a Mongo update document in-place, Mongo-faithful semantics.

        Supported operators (the set Persistence + chart-emission actually
        send): ``$set``, ``$setOnInsert`` (applied ONLY when ``inserting``),
        ``$push`` (appends; creates the array if missing), ``$addToSet``
        (appends iff not already present — dict values compared by equality).

        Before job-0203 only ``$set`` was honored, which silently DROPPED the
        job-0230 chart ``$push`` on the dev substrate (the upsert created a
        bare ``{_id}`` doc and the chart vanished). Unknown operators now
        raise so the next gap fails loudly instead.
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
                    f"FileMCPClient update-one: unsupported operator {op!r} "
                    f"(supports $set / $setOnInsert / $push / $addToSet)"
                )

    # ------------------------------------------------------------------ #
    # MCP tool surface
    # ------------------------------------------------------------------ #

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        args = dict(arguments or {})
        database = args.get("database", DEFAULT_DATABASE)
        collection = args.get("collection")
        if not collection:
            raise ValueError(
                f"FileMCPClient: tool {name!r} requires a 'collection' argument"
            )
        path = self._collection_path(database, collection)
        lock = self._lock_for(path)

        if name == "insert-one":
            async with lock:
                store = await _asyncio.to_thread(self._read_store, path)
                doc = args["document"]
                doc_id = doc.get("_id")
                if doc_id is None:
                    raise ValueError(
                        "FileMCPClient insert-one: document missing '_id'"
                    )
                store[doc_id] = doc
                await _asyncio.to_thread(self._atomic_write, path, store)
                return {"insertedId": doc_id}

        if name == "update-one":
            async with lock:
                store = await _asyncio.to_thread(self._read_store, path)
                filt = args.get("filter", {})
                update = args.get("update", {})
                upsert = bool(args.get("upsert", False))
                target_id = filt.get("_id")
                matched = 0
                modified = 0
                if target_id and target_id in store:
                    self._apply_update(store[target_id], update, inserting=False)
                    matched = 1
                    modified = 1
                elif upsert and target_id:
                    fresh: dict[str, Any] = {"_id": target_id}
                    self._apply_update(fresh, update, inserting=True)
                    store[target_id] = fresh
                    matched = 1
                    modified = 1
                else:
                    # Update by non-_id filter (e.g. firebase_uid). First match wins.
                    for doc in store.values():
                        if self._matches(doc, filt):
                            self._apply_update(doc, update, inserting=False)
                            matched = 1
                            modified = 1
                            break
                await _asyncio.to_thread(self._atomic_write, path, store)
                return {"matchedCount": matched, "modifiedCount": modified}

        if name == "update-many":
            # job-0252 (sprint-13.5): the pre-Auth case migration uses the
            # real-server ``update-many`` surface directly (the translator
            # passes it through). On the dev/file substrate there is no
            # translator, so we honor it here: apply the update to EVERY
            # matching doc. No upsert (the migration never upserts).
            async with lock:
                store = await _asyncio.to_thread(self._read_store, path)
                filt = args.get("filter", {})
                update = args.get("update", {})
                matched = 0
                modified = 0
                for doc in store.values():
                    if self._matches(doc, filt):
                        self._apply_update(doc, update, inserting=False)
                        matched += 1
                        modified += 1
                if modified:
                    await _asyncio.to_thread(self._atomic_write, path, store)
                return {"matchedCount": matched, "modifiedCount": modified}

        if name == "find-one":
            async with lock:
                store = await _asyncio.to_thread(self._read_store, path)
                filt = args.get("filter", {})
                for doc in store.values():
                    if self._matches(doc, filt):
                        return {"document": doc}
                return {"document": None}

        if name == "find":
            async with lock:
                store = await _asyncio.to_thread(self._read_store, path)
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

        raise NotImplementedError(
            f"FileMCPClient: unsupported MCP tool {name!r} "
            f"(supports insert-one / update-one / update-many / find-one / find)"
        )


def is_dev_persistence_enabled() -> bool:
    """Resolve whether the file-backed dev substrate should engage.

    Order:
    - explicit ``TRID3NT_DEV_PERSISTENCE=0`` disables (escape hatch for CI
      that wants the M1 None-Persistence path even on a dev box);
    - explicit ``TRID3NT_DEV_PERSISTENCE=1`` enables;
    - if neither is set AND MongoDB MCP is not provisioned (no
      ``TRID3NT_MONGO_MCP_STDIO=1`` nor ``TRID3NT_MONGO_MCP_URL``), default ON
      so a fresh local clone gets working Case persistence with zero config.

    The MCP-provisioned check is a string read (we don't try to start the
    sidecar here); ``init_persistence_from_env`` in ``server.py`` is the
    single place that actually decides between FilePersistence and the live
    MCP-backed Persistence, and it owns the precedence (real MCP wins).
    """
    raw = _os_for_file.environ.get(DEV_PERSISTENCE_ENABLED_ENV)
    if raw is not None:
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    mcp_stdio = _os_for_file.environ.get("TRID3NT_MONGO_MCP_STDIO") == "1"
    mcp_url = bool(_os_for_file.environ.get("TRID3NT_MONGO_MCP_URL"))
    return not (mcp_stdio or mcp_url)


def make_file_persistence(base_dir: _Path | None = None) -> Persistence:
    """Construct a ``Persistence`` backed by the file-backed MCP shim.

    Convenience for ``server.init_persistence_from_env`` and tests — wraps
    the substrate selection so the call site stays a one-liner.
    """
    return Persistence(FileMCPClient(base_dir=base_dir))


# --------------------------------------------------------------------------- #
# Backend selection (local-only)
# --------------------------------------------------------------------------- #
#
# The persistence backend is FILE-only in the local-first build. A cloud
# DynamoDB backend once sat behind this same ``MCPClientProtocol`` seam
# (``TRID3NT_PERSISTENCE_BACKEND=dynamodb``); it was removed in the local-only
# slim (2026-07-21) and is preserved in git history for a future cloud re-weave.
# ``make_persistence_for_backend`` now returns ``make_file_persistence``
# unconditionally; the selection CALL lives in
# ``main._maybe_bind_dev_persistence`` / ``server.init_persistence_from_env``
# (NOT this file — see the job's crossTrackChanges).

#: Env that selects the persistence backend. Re-exported from dynamo_backend so
#: there is a single name; mirrored here for callers that only import
#: persistence. Default keeps current (file) behavior.
PERSISTENCE_BACKEND_ENV = "TRID3NT_PERSISTENCE_BACKEND"
PERSISTENCE_BACKEND_FILE = "file"


def resolve_persistence_backend() -> str:
    """Resolve the configured persistence backend name.

    TRID3NT local-only build: persistence is file-backed, always. The cloud
    DynamoDB backend was removed (preserved in git history) — this now returns
    ``"file"`` unconditionally. Retained as a function so the existing call
    sites (``main._maybe_bind_dev_persistence`` logging) stay unchanged.
    """
    return PERSISTENCE_BACKEND_FILE


def make_persistence_for_backend(
    *, base_dir: _Path | None = None
) -> Persistence:
    """Build the file-backed ``Persistence`` (TRID3NT local-only build).

    Always returns ``make_file_persistence``. The env-selected cloud backend
    (DynamoDB) was removed; the selection CALL sites
    (``main._maybe_bind_dev_persistence`` / ``server.init_persistence_from_env``)
    keep using this so the file binding is honored consistently.
    """
    return make_file_persistence(base_dir=base_dir)


__all__ = [
    "Persistence",
    "MCPClientProtocol",
    "MCPSurfaceTranslator",
    "FileMCPClient",
    "make_file_persistence",
    "make_persistence_for_backend",
    "resolve_persistence_backend",
    "is_dev_persistence_enabled",
    "DEFAULT_DATABASE",
    "DEV_PERSISTENCE_DIR_ENV",
    "DEV_PERSISTENCE_ENABLED_ENV",
    "PERSISTENCE_BACKEND_ENV",
    "PERSISTENCE_BACKEND_FILE",
    "CASES_COLLECTION",
    "CHAT_COLLECTION",
    "SESSIONS_COLLECTION",
    "USERS_COLLECTION",
    "SECRETS_COLLECTION",
    "AUDIT_COLLECTION",
    "CASE_VIEWS_BUCKET",
    "CASE_VIEWS_PREFIX",
    "case_view_snapshot_key",
]
