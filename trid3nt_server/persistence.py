"""Thin typed wrapper over the document store.

Callers pass typed ``trid3nt_contracts`` models in and get typed models out;
the ``dict`` transport is contained behind one ``client.call_tool`` seam. No
quota, cost or spend field is persisted, and API keys never reach this store."""

from __future__ import annotations

import logging
import os
from typing import Any, Protocol

from trid3nt_contracts import now_utc
from trid3nt_contracts.case import (
    CaseChatMessage,
    CaseSessionState,
    CaseSummary,
)
from trid3nt_contracts.user import User

logger = logging.getLogger("trid3nt_server.persistence")

# Logical database name for all Case/User/Secret persistence: the file backend
# uses it as the namespace subdirectory under the dev-persistence root. Test
# isolation goes through ``TRID3NT_DEV_PERSISTENCE_DIR``, which relocates the
# whole root rather than renaming one namespace inside it.
DEFAULT_DATABASE = "trid3nt_dev"

# Collection names -- pinned nomenclature: "projects" for Cases, "sessions"
# for chat history, "users" for the forward-looking Auth track stub,
# "secrets" for per-Case keys.
CASES_COLLECTION = "projects"  # Case <-> projects 1:1
CHAT_COLLECTION = "case_chat_messages"  # per-turn message log
SESSIONS_COLLECTION = "sessions"  # agent's own session records
USERS_COLLECTION = "users"  # Auth/Users track stub


# --------------------------------------------------------------------------- #
# Store client protocol -- duck-typed so tests can pass a mock
# --------------------------------------------------------------------------- #


class MCPClientProtocol(Protocol):
    """Minimal store-client surface this module depends on."""

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        ...


# --------------------------------------------------------------------------- #
# Persistence wrapper
# --------------------------------------------------------------------------- #


def _unwrap_result(raw: dict[str, Any]) -> Any:
    """Extract the payload from one store-client result: ``document`` or
    ``documents`` when present, else the raw result, so a caller branches on
    ``None`` rather than on a shape."""
    if not isinstance(raw, dict):
        return raw
    if "document" in raw:
        return raw["document"]
    if "documents" in raw:
        return raw["documents"]
    return raw


class Persistence:
    """Typed wrapper over the document store; every method is ``async``
    because the file backend off-loads its blocking I/O to a thread.
    """

    def __init__(
        self,
        client: MCPClientProtocol,
        *,
        database: str = DEFAULT_DATABASE,
    ) -> None:
        self._store = client
        self._db = database

    # ----- Cases --------------------------------------------------------- #

    async def get_case(self, case_id: str) -> CaseSummary | None:
        """Find one Case by id, or ``None``; a storage field the wire
        ``CaseSummary`` does not carry is dropped rather than refused.
        """
        raw = await self._store.call_tool(
            "find-one",
            {
                "database": self._db,
                "collection": CASES_COLLECTION,
                "filter": {"_id": case_id},
            },
        )
        doc = _unwrap_result(raw)
        if not doc or not isinstance(doc, dict):
            return None
        return self._doc_to_case_summary(doc)

    @staticmethod
    def _doc_to_case_summary(doc: dict) -> CaseSummary:
        """Normalize a stored projects document into a ``CaseSummary``: ``_id``
        rewires to ``case_id`` and every storage-only field is dropped, so an
        owner link or an ``expires_at`` stamp never reaches the wire."""
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
        """Insert or update a Case; returns the persisted ``CaseSummary``.
        ``owner_user_id`` stamps the storage-only ``user_id`` field and a later
        ``None`` never clears it; Cases carry no ``expires_at`` TTL stamp."""
        body = case.model_dump(mode="json")
        body["_id"] = case.case_id  # the ``_id`` primary key
        if owner_user_id:
            body["user_id"] = owner_user_id
        await self._store.call_tool(
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
        """Persist a Case's storage-only ``{L<n>: uri}`` short-handle map.
        ``upsert=False``: a deleted or never-created Case is not resurrected
        by this side channel, and the caller treats the write as best-effort."""
        await self._store.call_tool(
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
        """Read back the persisted ``{L<n>: uri}`` map, or ``None`` when the
        Case, the field or the shape is missing; only ``str -> str`` entries
        survive, and the caller falls back to minting fresh handles."""
        raw = await self._store.call_tool(
            "find-one",
            {
                "database": self._db,
                "collection": CASES_COLLECTION,
                "filter": {"_id": case_id},
            },
        )
        doc = _unwrap_result(raw)
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
        """List the user's LIVE Cases: archived and deleted are excluded by the
        query AND by a post-validation guard, since a backend may ignore the
        ``$nin``; a document with no ``status`` at all is live."""
        raw = await self._store.call_tool(
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
        docs = _unwrap_result(raw)
        # A store with no filter match returns an empty list or None.
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
        """Soft-archive a Case, setting ``status="archived"``; the document
        survives so an un-archive can restore it.
        """
        await self._store.call_tool(
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
        """Soft-delete a Case: the document survives with ``status="deleted"``
        and a ``deleted_at`` stamp; there is no hard-delete path here.
        """
        await self._store.call_tool(
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
        """Append one chat exchange to a Case's history; the chat log is the
        agent's own record, not a solver result, so the write never triggers a
        confirmation gate."""
        body = msg.model_dump(mode="json")
        body["_id"] = msg.message_id
        await self._store.call_tool(
            "insert-one",
            {
                "database": self._db,
                "collection": CHAT_COLLECTION,
                "document": body,
            },
        )

    async def upsert_chat_message(self, msg: CaseChatMessage) -> None:
        """Insert-or-replace one chat row keyed by its stable ``message_id``, so
        a running card and its terminal state rewrite the SAME row; ``created_at``
        is pinned on insert so the transition never reorders the replay."""
        body = msg.model_dump(mode="json")
        body["_id"] = msg.message_id
        created_at = body.pop("created_at", None)
        update: dict[str, Any] = {"$set": body}
        if created_at is not None:
            update["$setOnInsert"] = {"created_at": created_at}
        await self._store.call_tool(
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
        """Hydrate a Case's resume envelope: its header joined with the ordered
        chat history; layers, pipeline history and charts pass through as dicts
        because ``trid3nt_contracts`` owns those shapes."""
        case = await self.get_case(case_id)
        if case is None:
            # Surface a minimal placeholder so the caller can decide how to
            # handle "Case not found" without raising through the store layer.
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
        raw = await self._store.call_tool(
            "find",
            {
                "database": self._db,
                "collection": CHAT_COLLECTION,
                "filter": {"case_id": case_id},
                "sort": {"created_at": 1},
            },
        )
        docs = _unwrap_result(raw) or []
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
        # Deterministic replay order whatever the backend's sort support: the
        # stream interleaves by ``created_at`` and the ULID ``message_id``
        # breaks ties in write order.
        chat.sort(key=lambda m: (m.created_at, m.message_id))
        # A re-open repopulates the layer panel from the persisted summaries:
        # the emitter's copy is per-connection and dies with the socket.
        loaded_layers = list(case.loaded_layer_summaries)
        # Charts persist as ``SessionChartRecord``s pushed onto the sessions
        # doc; replay unwraps each record's ``payload`` in emitted_at order.
        # Best-effort: a missing or malformed doc yields no charts.
        charts: list[dict] = []
        try:
            sraw = await self._store.call_tool(
                "find-one",
                {
                    "database": self._db,
                    "collection": SESSIONS_COLLECTION,
                    "filter": {"_id": case_id},
                },
            )
            sdoc = _unwrap_result(sraw)
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
    # The ``sessions`` document is the TTL-cleaned activity header: who and when,
    # which Cases were touched, and the append-only ``charts`` array. Chat content
    # lives in ``case_chat_messages`` and never duplicates into this document.

    async def upsert_session_record(self, doc: "SessionDocument") -> None:
        """Insert or fully overwrite a session record; ``$set`` of the named
        fields leaves storage-only extras such as ``charts`` in place.
        """
        body = doc.model_dump(mode="json", by_alias=True)
        session_id = body.pop("_id")
        await self._store.call_tool(
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
        """Activity heartbeat in one upsert: ``last_active_at`` and the TTL
        ``expires_at`` are set, the immutable header lands only on insert, and a
        given ``case_id`` is deduped into ``project_ids``. Never raises upward."""
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
        await self._store.call_tool(
            "update-one",
            {
                "database": self._db,
                "collection": SESSIONS_COLLECTION,
                "filter": {"_id": session_id},
                "update": update,
                "upsert": True,
            },
        )
        # Header repair: a doc first created by a bare chart ``$push`` carries no
        # ``created_at``/``schema_version`` and ``$setOnInsert`` cannot backfill
        # an existing doc, so repair once with ``created_at=now``.
        raw = await self._store.call_tool(
            "find-one",
            {
                "database": self._db,
                "collection": SESSIONS_COLLECTION,
                "filter": {"_id": session_id},
            },
        )
        doc = _unwrap_result(raw)
        if isinstance(doc, dict) and (
            "created_at" not in doc or "schema_version" not in doc
        ):
            repair: dict[str, Any] = {}
            if "created_at" not in doc:
                repair["created_at"] = iso_now
            if "schema_version" not in doc:
                repair["schema_version"] = "v1"
            await self._store.call_tool(
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
        """Persist the session's storage-only ``last_active_case_id`` so the
        pointer survives a restart; ``None`` clears it. The client-stamped
        ``case_id`` on a turn stays the authority - this is the cold-start cache."""
        now = now_utc()
        iso_now = now.isoformat().replace("+00:00", "Z")
        await self._store.call_tool(
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
        """Read back the persisted ``last_active_case_id``, or ``None`` for a
        session with no record, no pointer or a malformed shape.
        """
        raw = await self._store.call_tool(
            "find-one",
            {
                "database": self._db,
                "collection": SESSIONS_COLLECTION,
                "filter": {"_id": session_id},
            },
        )
        doc = _unwrap_result(raw)
        if not isinstance(doc, dict):
            return None
        value = doc.get("last_active_case_id")
        return value if isinstance(value, str) else None

    async def get_session_record(self, session_id: str) -> "SessionDocument | None":
        """Read one session record back as a typed ``SessionDocument``; storage-
        only extras such as the ``charts`` array are dropped before validation,
        and a malformed document yields ``None``."""
        from trid3nt_contracts.collections import SessionDocument

        raw = await self._store.call_tool(
            "find-one",
            {
                "database": self._db,
                "collection": SESSIONS_COLLECTION,
                "filter": {"_id": session_id},
            },
        )
        doc = _unwrap_result(raw)
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
        await self._store.call_tool(
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
        """Find a user by ULID, or ``None`` when no record exists."""
        raw = await self._store.call_tool(
            "find-one",
            {
                "database": self._db,
                "collection": USERS_COLLECTION,
                "filter": {"_id": user_id},
            },
        )
        doc = _unwrap_result(raw)
        if not doc or not isinstance(doc, dict):
            return None
        normalized = {k: v for k, v in doc.items() if k != "_id"}
        if "user_id" not in normalized:
            normalized["user_id"] = user_id
        # Drop fields the current User schema does not carry, so a schema bump
        # never breaks an existing record.
        allowed = set(User.model_fields.keys())
        normalized = {k: v for k, v in normalized.items() if k in allowed}
        try:
            return User.model_validate(normalized)
        except Exception:  # noqa: BLE001
            logger.warning("malformed user doc for user_id=%s", user_id)
            return None


# --------------------------------------------------------------------------- #
# The file-backed store
# --------------------------------------------------------------------------- #
# Storage is ``~/.trid3nt/dev_persistence/<database>/<collection>.json``, one
# JSON file per collection mapping ``_id`` to document. A per-collection
# ``asyncio.Lock`` serializes concurrent calls and writes land through a sibling
# ``.tmp`` plus ``os.replace``. The supported query surface is exactly what
# ``Persistence`` invokes: ``insert-one``, ``update-one`` (``$set`` plus optional
# ``upsert``), ``delete-one``, ``find-one`` and ``find`` with a single-key sort.

import asyncio as _asyncio
import contextlib as _contextlib
import json as _json_for_file
import os as _os_for_file
import weakref as _weakref
from pathlib import Path as _Path

try:  # POSIX advisory locking; absent on Windows, where the flock is a no-op.
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - this box is Linux
    _fcntl = None  # type: ignore[assignment]

DEV_PERSISTENCE_DIR_ENV = "TRID3NT_DEV_PERSISTENCE_DIR"
DEV_PERSISTENCE_ENABLED_ENV = "TRID3NT_DEV_PERSISTENCE"

#: running loop -> {collection path -> asyncio.Lock}, shared by EVERY
#: FileMCPClient. The store is the FILE, not the instance: two clients over one
#: collection must serialize, or a read-modify-write from one resurrects what the
#: other deleted. Keyed by loop because a Lock can only ever be waited on from the
#: loop that first suspended on it, and weakly so a finished loop's locks go too.
_COLLECTION_LOCKS: "_weakref.WeakKeyDictionary[Any, dict[str, _asyncio.Lock]]" = \
    _weakref.WeakKeyDictionary()


def _collection_lock(path: _Path) -> _asyncio.Lock:
    locks = _COLLECTION_LOCKS.setdefault(_asyncio.get_running_loop(), {})
    key = str(path)
    lock = locks.get(key)
    if lock is None:
        lock = _asyncio.Lock()
        locks[key] = lock
    return lock


@_contextlib.contextmanager
def _file_lock(path: _Path):
    """Exclusive advisory lock on a sidecar, held across one read-modify-write.
    BLOCKING - runs inside ``to_thread``. The sidecar, because ``_atomic_write``
    replaces the store's inode; cross-PROCESS only, asyncio serializes in-process."""
    if _fcntl is None:  # pragma: no cover - this box is Linux
        yield
        return
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+") as fh:
        _fcntl.flock(fh.fileno(), _fcntl.LOCK_EX)
        try:
            yield
        finally:
            _fcntl.flock(fh.fileno(), _fcntl.LOCK_UN)


def _default_dev_persistence_dir() -> _Path:
    """Resolve the on-disk directory for the file-backed substrate:
    ``TRID3NT_DEV_PERSISTENCE_DIR`` when set, else ``~/.trid3nt/dev_persistence/``.
    """
    override = _os_for_file.environ.get(DEV_PERSISTENCE_DIR_ENV)
    if override:
        return _Path(override).expanduser()
    # One-time rename migration: data kept under ``~/.grace2`` moves to
    # ``~/.trid3nt`` when the new directory does not yet exist.
    legacy_home = _Path.home() / ".grace2"
    new_home = _Path.home() / ".trid3nt"
    if legacy_home.is_dir() and not new_home.exists():
        _os_for_file.rename(legacy_home, new_home)
        logger.info("FilePersistence: migrated legacy dir %s -> %s", legacy_home, new_home)
    return new_home / "dev_persistence"


class FileMCPClient:
    """The file-backed store, satisfying :class:`MCPClientProtocol` over one
    JSON file per collection; reads return a ``{"document": ...}`` or
    ``{"documents": [...]}`` envelope and writes return a counts dict."""

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

    # ------------------------------------------------------------------ #
    # Storage helpers
    # ------------------------------------------------------------------ #

    def _collection_path(self, database: str, collection: str) -> _Path:
        db_dir = self._base_dir / database
        db_dir.mkdir(parents=True, exist_ok=True)
        return db_dir / f"{collection}.json"

    def _lock_for(self, path: _Path) -> _asyncio.Lock:
        """The PROCESS-WIDE lock for this collection - never a per-instance one."""
        return _collection_lock(path)

    def _cycle(self, path: _Path, apply: Any) -> Any:
        """BLOCKING: one flocked read-modify-write over the CURRENT store; the
        read happens inside the lock, so a whole-store write can never be
        computed from a snapshot another writer has already superseded."""
        with _file_lock(path):
            store = self._read_store(path)
            result, dirty = apply(store)
            if dirty:
                self._atomic_write(path, store)
        return result

    @staticmethod
    def _read_store(path: _Path) -> dict[str, dict]:
        # OFF-LOOP CONTRACT: this is a BLOCKING body, reached through
        # ``_cycle`` inside ``to_thread`` so it never stalls the asyncio WS loop.
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
            # default=str so a raw datetime in any document serializes instead
            # of raising ``TypeError`` and dropping the row.
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
    # Query matcher -- the same subset the test mock supports
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
                # A MISSING field matches $nin: the document's value, None, is
                # "not in" the exclusion list unless None is itself listed, so a
                # Case doc written before the status field stays listed.
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
        """Apply an update document in place, supporting ``$set``,
        ``$setOnInsert`` (only when ``inserting``), ``$push`` and ``$addToSet``;
        an unknown operator raises rather than dropping the write."""
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
    # The call surface
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

        apply = self._operation(name, args)
        async with lock:
            return await _asyncio.to_thread(self._cycle, path, apply)

    def _operation(self, name: str, args: dict[str, Any]) -> Any:
        """The mutation for one call: ``(store) -> (result, dirty)``.

        Built OUTSIDE the lock, applied INSIDE it against the store as it is then."""
        if name == "insert-one":
            doc = args["document"]
            if doc.get("_id") is None:
                raise ValueError("FileMCPClient insert-one: document missing '_id'")

            def _insert(store: dict[str, dict]):
                store[doc["_id"]] = doc
                return {"insertedId": doc["_id"]}, True

            return _insert

        if name == "update-one":
            filt = args.get("filter", {})
            update = args.get("update", {})
            upsert = bool(args.get("upsert", False))
            target_id = filt.get("_id")

            def _update(store: dict[str, dict]):
                if target_id and target_id in store:
                    self._apply_update(store[target_id], update, inserting=False)
                elif upsert and target_id:
                    fresh: dict[str, Any] = {"_id": target_id}
                    self._apply_update(fresh, update, inserting=True)
                    store[target_id] = fresh
                else:
                    # Update by a non-``_id`` filter. First match wins.
                    for doc in store.values():
                        if self._matches(doc, filt):
                            self._apply_update(doc, update, inserting=False)
                            break
                    else:
                        return {"matchedCount": 0, "modifiedCount": 0}, False
                return {"matchedCount": 1, "modifiedCount": 1}, True

            return _update

        if name == "find-one":
            filt = args.get("filter", {})

            def _find_one(store: dict[str, dict]):
                for doc in store.values():
                    if self._matches(doc, filt):
                        return {"document": doc}, False
                return {"document": None}, False

            return _find_one

        if name == "delete-one":
            filt = args.get("filter", {})
            target_id = filt.get("_id")

            def _delete_one(store: dict[str, dict]):
                doc_id = target_id if target_id in store else next(
                    (k for k, d in store.items() if self._matches(d, filt)), None
                )
                if doc_id is None:
                    return {"deletedCount": 0}, False
                del store[doc_id]
                return {"deletedCount": 1}, True

            return _delete_one

        if name == "find":
            filt = args.get("filter", {})
            sort = args.get("sort", {})

            def _find(store: dict[str, dict]):
                results = [d for d in store.values() if self._matches(d, filt)]
                if sort:
                    key = next(iter(sort.keys()))
                    results.sort(key=lambda d: d.get(key, ""),
                                 reverse=(sort[key] == -1))
                return {"documents": results}, False

            return _find

        raise NotImplementedError(
            f"FileMCPClient: unsupported method {name!r} "
            f"(supports insert-one / update-one / update-many / delete-one / "
            f"find-one / find)"
        )


def is_dev_persistence_enabled() -> bool:
    """Resolve whether the file-backed substrate engages: an explicit
    ``TRID3NT_DEV_PERSISTENCE`` value decides, and unset defaults ON so a fresh
    clone gets working Case persistence with no config."""
    raw = _os_for_file.environ.get(DEV_PERSISTENCE_ENABLED_ENV)
    if raw is not None:
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return True


def make_file_persistence(base_dir: _Path | None = None) -> Persistence:
    """Construct a ``Persistence`` backed by the file store."""
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
    """Resolve the configured persistence backend name; ``file`` is the only
    supported value and any other raises rather than falling back silently.
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
    """Build the file-backed ``Persistence``, refusing first if the configured
    backend is not ``file``.
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
