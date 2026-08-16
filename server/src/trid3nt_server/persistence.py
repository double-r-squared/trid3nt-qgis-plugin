"""Thin typed wrapper over the persistence MCP surface.

Agent code calls ``Persistence.upsert_case(case_dataclass)``; this module
issues logical ``insert-one`` / ``update-one`` / ``find-one`` / ``find`` calls
through an ``MCPClientProtocol`` client and serializes/deserializes through
``trid3nt_contracts`` ``GraceModel`` types (never raw dicts at the call site).

Live backend (local-first build): the file-backed twin ``FileMCPClient`` --
a small JSON-document store that implements the same logical MCP surface with
Mongo-faithful filter/update semantics. It is bound by
``main._maybe_bind_dev_persistence`` / ``server.init_persistence_from_env``.
Another document-store client implementing the same ``MCPClientProtocol`` drops
in unchanged, but the local stack binds only the file backend.

Supports ``CaseSummary`` round-trip (get/upsert/list/archive/delete),
``CaseChatMessage`` append + ``CaseSessionState`` hydration, and ``User``
round-trip (``get_user_by_id``/``upsert_user``). API-key credentials do not
persist here: the plugin brokers key values over the ``secret-add`` WS
seam into the in-memory ``credentials.resolver`` session cache, with env vars
the headless / dev floor.

Containment: every storage call goes through
``mcp_client.call_tool("<mcp-method>", args)``, a single seam; callers pass
typed ``GraceModel`` instances in and get typed instances out, and the
``dict``-shape MCP transport is contained here. Persistence is the I/O
substrate; any confirmation policy lives at the ``server.py`` call sites.

Invariants: no quota/cost/spend fields on any record.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from trid3nt_contracts import now_utc
from trid3nt_contracts.case import (
    CaseChatMessage,
    CaseSessionState,
    CaseSummary,
)
from trid3nt_contracts.user import User

logger = logging.getLogger("trid3nt_server.persistence")

# Logical database name for all Case/User/Secret persistence. The file backend
# uses it as a namespace prefix; another document-store client would use it as
# the DB name.
# Override via env var ``TRID3NT_MONGO_DB`` for staging / test isolation.
import os

DEFAULT_DATABASE = os.environ.get("TRID3NT_MONGO_DB", "trid3nt_dev")

# Collection names -- pinned nomenclature: "projects" for Cases, "sessions"
# for chat history, "users" for the forward-looking Auth track stub,
# "secrets" for per-Case keys.
CASES_COLLECTION = "projects"  # Case <-> projects 1:1
CHAT_COLLECTION = "case_chat_messages"  # per-turn message log
SESSIONS_COLLECTION = "sessions"  # agent's own session records
USERS_COLLECTION = "users"  # Auth/Users track stub


# --------------------------------------------------------------------------- #
# MCP client protocol -- duck-typed so tests can pass a mock
# --------------------------------------------------------------------------- #


class MCPClientProtocol(Protocol):
    """Minimal MCP client surface this module depends on.

    The file-backed ``FileMCPClient`` implements it; another document-store
    client that speaks the same logical ``call_tool`` surface drops in without
    adaptation. Tests pass a mock implementing this single method.
    """

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        ...


# --------------------------------------------------------------------------- #
# Persistence wrapper
# --------------------------------------------------------------------------- #


def _unwrap_mcp_result(raw: dict[str, Any]) -> Any:
    """Extract the structured payload from an MCP ``tools/call`` result.

    An MCP document-store client returns results in a ``content`` array with a
    JSON string in the first entry's ``text`` field for document operations.
    Best-effort: if the shape doesn't match we surface ``None`` so callers can
    branch on "no document" vs "raw dict already parsed".
    """
    if not isinstance(raw, dict):
        return raw
    # Direct dict already -- e.g., when the mock test client returns a dict.
    if "content" not in raw and "document" not in raw and "documents" not in raw:
        return raw
    # MCP document-store: content[0].text is a JSON string
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
    """Typed wrapper over the persistence MCP surface (``MCPClientProtocol``).

    Construct with any object implementing ``MCPClientProtocol`` -- on this
    stack that is the file-backed ``FileMCPClient``; another document-store
    client drops in unchanged. All methods are ``async`` (the file backend
    off-loads its blocking I/O; a remote client's transport is async).
    """

    def __init__(
        self,
        mcp_client: MCPClientProtocol,
        *,
        database: str = DEFAULT_DATABASE,
    ) -> None:
        self._mcp = mcp_client
        self._db = database

    # ----- Cases --------------------------------------------------------- #

    async def get_case(self, case_id: str) -> CaseSummary | None:
        """Find one Case by id. Returns ``None`` if not found.

        Forward-compat: drops any field the ``ProjectDocument`` schema
        carries that ``CaseSummary`` doesn't denormalize (e.g. ``deleted_at``,
        ``owner_user_id``, etc.). The Case envelope is a UI denormalization
        of the storage shape -- extra storage fields are expected and ignored.
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
        denormalized envelope doesn't carry -- including the ephemeral-case
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
                # storage-only field (e.g. user_id, expires_at TTL stamp) --
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
    ) -> CaseSummary:
        """Insert or update a Case. Returns the persisted ``CaseSummary``.

        Uses MCP ``update-one`` with ``upsert=True`` so a fresh Case lands and
        an existing one is overwritten in a single round-trip.

        when ``owner_user_id``
        is provided, it is stamped onto the document's ``user_id`` field so the
        Case belongs to its creator. ``CaseSummary`` itself carries no owner
        field (it is a UI denormalization), so ownership lives only at the
        storage layer -- the read path (``_doc_to_case_summary``) deliberately
        drops it. Without this, every newly-created Case would lack a
        ``user_id`` and become invisible to ``list_cases_for_user``.
        ``owner_user_id=None`` (the legacy / dev call shape) writes no owner.

        The owner is written under ``$set``, so re-upserting an existing Case
        with a fresh ``owner_user_id`` updates it; passing ``None`` never
        clears an already-stamped owner (the ``user_id`` key is simply absent
        from the ``$set``).

        Cases are durable: no ``expires_at`` TTL stamp is written. A legacy Case
        doc that still carries a stored ``expires_at`` reads back fine --
        ``_doc_to_case_summary`` drops the storage-only key so it never reaches
        the wire ``CaseSummary``.
        """
        body = case.model_dump(mode="json")
        body["_id"] = case.case_id  # the ``_id`` primary key
        if owner_user_id:
            body["user_id"] = owner_user_id
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
    # Per-Case short layer-handle map (storage-only field)
    # ------------------------------------------------------------------ #

    async def set_case_layer_handles(
        self, case_id: str, handles: dict[str, str]
    ) -> None:
        """Persist a Case's ``{L<n>: uri}`` short-handle map.

        Storage-only ``layer_handles`` field on the cases doc -- the
        ``last_active_case_id`` pattern: ``CaseSummary`` deliberately does
        NOT carry it (``_doc_to_case_summary`` drops unknown keys), so the
        wire contract stays narrow while the storage doc accretes. The
        ``upsert_case`` full-body ``$set`` never removes it (named-field
        semantics). ``upsert=False``: a deleted / never-created Case is not
        resurrected by this side-channel -- the write is simply a no-op.
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
        """Read back the persisted ``{L<n>: uri}`` map.

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

    async def list_cases_for_user(self, user_id: str) -> list[CaseSummary]:
        """List the user's LIVE Cases (``status="active"`` only).

        The ``projects`` collection schema does not yet carry a ``user_id``
        field yet (it was specified pre-Auth); we pass the filter anyway --
        once the Auth/Users track adds the field the query starts narrowing,
        until then it returns the full Case list for the deployment.

        Soft-deleted and archived Cases are excluded both in the query and
        by a post-validation guard (belt-and-suspenders for MCP backends
        whose filter dialect quietly ignores the operator); the ``$nin``
        filter still matches docs with no ``status`` field at all (pre-status
        records are live by definition: ``CaseSummary.status`` defaults to
        ``"active"``).
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
                    # tombstones never reach the wire.
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
            except Exception:  # noqa: BLE001 -- skip malformed docs
                logger.warning("skipping malformed Case doc: %s", d)
                continue
            if case.status in ("deleted", "archived"):
                # guard: backend ignored/mangled the $nin filter.
                continue
            cases.append(case)
        return cases

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

        v0.1 stance: soft-delete only. A curator-tooled hard delete is a future
        addition; data-retention rules (the ``deleted_at`` stamp) point this way
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

    # ----- Chat history + session state (rehydration) --------------------- #

    async def append_chat_message(self, msg: CaseChatMessage) -> None:
        """Append one persisted chat exchange to a Case's history.

        The chat-message collection is the agent's own session
        record (it is per-turn replay material, not a solver result), so this
        write is NOT a confirmation trigger -- the caller does not need to
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

        Durable-card lifecycle (nothing about a solve is transient): an off-box SOLVE card
        is persisted ``running`` at mint and UPDATED IN PLACE to its terminal
        state. Unlike ``append_chat_message`` (always a fresh row), this upserts
        by the stable ``_id`` so the running -> terminal transition rewrites the
        SAME row -- never a duplicate. ``created_at`` is pinned on first insert
        via ``$setOnInsert`` so the row KEEPS its position in the
        ``created_at``-sorted replay across the transition (the terminal update
        must not reorder the card). Every other field is ``$set`` so the terminal
        ``state`` / ``duration_ms`` / ``tool_card`` overwrite the running values.

        Routes through the SAME ``update-one`` (upsert) surface every backend
        implements. The filter carries BOTH key shapes so it targets the natural
        key on each: ``_id`` for the file backend (chat ``_id`` ==
        ``message_id``), plus the composite ``case_id`` + ``message_id`` shape a
        keyed cloud backend would use -- so the get/apply/put upsert lands on
        exactly one row on either. Best-effort at the call sites
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
        """Hydrate the rehydration envelope for a Case (resume).

        Joins the Case header (``CaseSummary``) with its ordered chat history
        from ``CHAT_COLLECTION``. ``loaded_layers`` / ``pipeline_history`` /
        ``current_pipeline`` are passed through as dicts -- collections.py
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
        # deterministic replay order regardless of backend sort
        # support -- the full stream (user turns, tool cards, agent narration)
        # interleaves by ``created_at``; ULID ``message_id`` breaks ties in
        # write order. Python's sort is stable, so backends that already
        # honored the ``created_at`` sort are untouched.
        chat.sort(key=lambda m: (m.created_at, m.message_id))
        # Part B: hydrate ``loaded_layers`` from the persisted
        # ``Case.loaded_layer_summaries`` so a Case re-open repopulates the
        # LayerPanel deterministically. The PipelineEmitter holds these in
        # memory per-connection; without this hydration step a browser
        # refresh (new WS, new emitter) shows an empty LayerPanel even
        # though the layers are still published on the per-Case ``.qgs``.
        loaded_layers = list(case.loaded_layer_summaries)
        # hydrate persisted charts so a Case re-open
        # replays them WITHOUT a re-run. ``$push``es SessionChartRecords
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
        except Exception:  # noqa: BLE001 -- chart replay is best-effort
            logger.warning("get_session_state: chart hydration failed case=%s", case_id)
        return CaseSessionState(
            case=case, chat_history=chat, loaded_layers=loaded_layers, charts=charts,
        )

    # ----- Session records (``sessions`` collection) ----------------------- #
    # The ``sessions`` document is the TTL-cleaned activity header
    # (``SESSIONS_TTL``): who/when, which Cases were touched, and the
    # append-only ``charts`` array that chart-emission ``$push``es onto. Chat
    # content canonically lives in ``case_chat_messages``;
    # ``SessionDocument.chat_history`` stays empty at v0.1 so the two stores
    # never diverge.

    async def upsert_session_record(self, doc: "SessionDocument") -> None:
        """Insert or fully overwrite a session record.

        ``$set`` of the full document body -- storage-only extras a previous
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
        """Activity heartbeat for a session -- one upsert round-trip.

        - ``$set`` ``last_active_at`` + ``expires_at`` (TTL driver) so
          every interaction pushes cleanup 30 days out (``SESSIONS_TTL``).
        - ``$setOnInsert`` the immutable header (``schema_version``,
          ``created_at``) so the first touch creates a well-formed record
          and later touches never rewrite history.
        - ``$addToSet`` the active Case into ``project_ids`` when given --
          deduped, so per-turn touches stay idempotent.

        Fire-and-forget discipline at call sites (same as telemetry and
        chart persistence): callers wrap in ``try/except`` or a
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
        # (chart-emission upserts before any touch -- ordering) has
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

    async def set_session_active_case(
        self, session_id: str, case_id: str | None
    ) -> None:
        """Persist the session's active-Case pointer.

        Writes a storage-only ``last_active_case_id`` field onto the session
        record so the active-Case pointer survives a process restart
        (the in-memory ``_SESSION_ACTIVE_CASE`` dict in server.py is wiped on
        process death). ``SessionDocument`` deliberately does NOT carry this
        field -- it is storage-only, exactly like the ``charts`` array;
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
        """Read back the persisted active-Case pointer.

        Returns the ``last_active_case_id`` written by
        ``set_session_active_case``, or ``None`` when the session has no
        record / no persisted pointer (a fresh session, or one that never
        bound a Case). Used by server.py to reload the in-memory pointer when a
        fresh ``SessionState`` is built after a process restart, so the cold-start
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
        storage-only extras -- notably the ``charts`` array -- are
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
        # ``id`` is aliased to ``_id`` -- keep the alias key, drop the rest.
        normalized = {
            k: v for k, v in doc.items() if k in allowed or k == "_id"
        }
        try:
            return SessionDocument.model_validate(normalized)
        except Exception:  # noqa: BLE001
            logger.warning("malformed session doc for session_id=%s", session_id)
            return None

    # ----- Users (Auth/Users track stub) ----------------------------------- #

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

        Part C: the anonymous-fallback path needs an id-based lookup
        so a reconnecting client can re-bind to the same User via the
        ``AuthTokenEnvelope.anonymous_user_id`` hint.
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


# --------------------------------------------------------------------------- #
# Local-dev file-backed MCP client
# --------------------------------------------------------------------------- #
# The file-backed shim is the LIVE persistence substrate on this stack. It
# satisfies the same ``MCPClientProtocol`` surface a cloud MCP client would, so
# ``Persistence`` doesn't need to know which substrate it is talking to, and
# can be bound at startup either way.
#
# Storage: ``~/.trid3nt/dev_persistence/<database>/<collection>.json``, one
# JSON file per collection (dict mapping ``_id`` -> document). Atomicity: a
# per-collection ``asyncio.Lock`` serializes concurrent calls; writes go to a
# sibling ``<collection>.json.tmp`` then ``os.replace`` (POSIX-atomic
# rename). Scope matches the MCP tool subset Persistence actually invokes:
# ``insert-one``/``update-one`` (``$set`` + optional ``upsert``)/``find-one``
# /``find`` (optional single-key sort). Not a Mongo emulator -- just enough
# query semantics to round-trip Persistence's calls.

import asyncio as _asyncio
import json as _json_for_file
import os as _os_for_file
from pathlib import Path as _Path

DEV_PERSISTENCE_DIR_ENV = "TRID3NT_DEV_PERSISTENCE_DIR"
DEV_PERSISTENCE_ENABLED_ENV = "TRID3NT_DEV_PERSISTENCE"


def _default_dev_persistence_dir() -> _Path:
    """Resolve the on-disk directory for the file-backed dev substrate.

    Override via ``TRID3NT_DEV_PERSISTENCE_DIR`` (used by tests + CI to point
    at a tmpdir). Default is ``~/.trid3nt/dev_persistence/`` so a fresh
    clone gets a stable, user-scoped location.
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
    Persistence layer agnostic of substrate -- the same code paths that
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
        # run it via ``await _asyncio.to_thread(self._read_store, path)`` so the
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
            # default=str so a raw datetime in any document (e.g. the
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
    # Query matcher -- same subset MockMCPClient supports in tests
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
                # listed). uses this for the case-list status filter
                # so pre-status Case docs stay listed.
                if doc.get(k) in v["$nin"]:
                    return False
                continue
            if doc.get(k) != v:
                return False
        return True

    # ------------------------------------------------------------------ #
    # Update-operator application
    # ------------------------------------------------------------------ #

    @staticmethod
    def _apply_update(doc: dict, update: dict, *, inserting: bool) -> None:
        """Apply a Mongo update document in-place, Mongo-faithful semantics.

        Supported operators (the set Persistence + chart-emission actually
        send): ``$set``, ``$setOnInsert`` (applied ONLY when ``inserting``),
        ``$push`` (appends; creates the array if missing), ``$addToSet``
        (appends iff not already present -- dict values compared by equality).

        Before only ``$set`` was honored, which silently DROPPED the
        chart ``$push`` on the dev substrate (the upsert created a
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
                    # Update by a non-``_id`` filter. First match wins.
                    for doc in store.values():
                        if self._matches(doc, filt):
                            self._apply_update(doc, update, inserting=False)
                            matched = 1
                            modified = 1
                            break
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
    """Resolve whether the file-backed substrate should engage.

    Order:
    - explicit ``TRID3NT_DEV_PERSISTENCE=0`` disables (escape hatch for CI
      that wants the in-memory, no-persistence path);
    - explicit ``TRID3NT_DEV_PERSISTENCE=1`` enables;
    - unset → default ON so a fresh local clone gets working Case persistence
      with zero config.

    The env read is a string check (nothing is started here);
    ``main._maybe_bind_dev_persistence`` is the single place that binds the
    file backend when this returns True.
    """
    raw = _os_for_file.environ.get(DEV_PERSISTENCE_ENABLED_ENV)
    if raw is not None:
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return True


def make_file_persistence(base_dir: _Path | None = None) -> Persistence:
    """Construct a ``Persistence`` backed by the file-backed MCP shim.

    Convenience for ``server.init_persistence_from_env`` and tests -- wraps
    the substrate selection so the call site stays a one-liner.
    """
    return Persistence(FileMCPClient(base_dir=base_dir))


# --------------------------------------------------------------------------- #
# Backend selection (local-only)
# --------------------------------------------------------------------------- #
#
# ``file`` is the ONLY persistence backend. ``TRID3NT_PERSISTENCE_BACKEND``
# unset (or ``file``) binds the file backend; any other value is an explicit
# request for an unsupported backend and raises a typed error naming ``file``.

#: Env that selects the persistence backend. Unset defaults to ``file``.
PERSISTENCE_BACKEND_ENV = "TRID3NT_PERSISTENCE_BACKEND"
PERSISTENCE_BACKEND_FILE = "file"


class UnsupportedPersistenceBackendError(RuntimeError):
    """``TRID3NT_PERSISTENCE_BACKEND`` names a backend other than ``file``."""


def resolve_persistence_backend() -> str:
    """Resolve the configured persistence backend name.

    ``file`` is the only supported backend. An unset (or ``file``) env resolves
    to ``file``; any other value raises ``UnsupportedPersistenceBackendError``
    rather than silently falling back, so a stale cloud selection surfaces
    loudly.
    """
    selected = (os.environ.get(PERSISTENCE_BACKEND_ENV) or PERSISTENCE_BACKEND_FILE).strip().lower()
    if selected != PERSISTENCE_BACKEND_FILE:
        raise UnsupportedPersistenceBackendError(
            f"{PERSISTENCE_BACKEND_ENV}={selected!r} is not supported. "
            f"The only persistence backend is {PERSISTENCE_BACKEND_FILE!r}."
        )
    return PERSISTENCE_BACKEND_FILE


def make_persistence_for_backend(
    *, base_dir: _Path | None = None
) -> Persistence:
    """Build the file-backed ``Persistence``.

    Validates the configured backend first (``resolve_persistence_backend``
    raises on any non-``file`` selection), then returns ``make_file_persistence``.
    """
    resolve_persistence_backend()
    return make_file_persistence(base_dir=base_dir)


__all__ = [
    "Persistence",
    "MCPClientProtocol",
    "FileMCPClient",
    "make_file_persistence",
    "make_persistence_for_backend",
    "resolve_persistence_backend",
    "UnsupportedPersistenceBackendError",
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
]
