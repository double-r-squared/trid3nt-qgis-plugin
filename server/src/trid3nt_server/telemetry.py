"""Tool-call telemetry writer (B-tel / Wave 4.10 + Wave 4.11 M3).

Wave 4.10: emits one JSON-line per LLM-initiated or workflow-initiated tool
call to a local JSONL file.

Wave 4.11 M3: swaps the backend for MongoDB MCP when the Persistence singleton
is bound (``TRID3NT_MONGO_MCP_STDIO=1``).  Falls back to the local-file path
when Persistence is unbound (dev / CI without Atlas).

Write path is fire-and-forget: ``emit_tool_call_event`` schedules an async
write task and returns immediately.  A write failure is logged at WARNING level
but never raised — telemetry must never break the tool-dispatch loop.

Configuration:
    ``TRID3NT_TELEMETRY_PATH`` env var overrides the default output path for the
    local-file fallback.  Default: ``/tmp/trid3nt_tool_call_telemetry.jsonl``

Record shape (one JSON object per line, newline-terminated — local-file path):
    {
        "session_id":                  str,
        "ts":                          str  (ISO-8601 UTC, e.g. "2026-06-09T...Z"),
        "tool_name":                   str,
        "source":                      "llm" | "workflow" | "manual",
        "args_hash":                   str  (hex digest of SHA-256 of JSON-encoded args),
        "success":                     bool,
        "latency_ms":                  float,
        "error_code":                  str | null,
        "retry_attempt":               int   (0 for first call),
        "cached_content_token_count":  int | null,
    }

MongoDB record shape (tool_call_telemetry collection — MCP-backed path):
    Maps 1:1 to ``ToolCallTelemetryDocument`` from
    ``trid3nt_contracts.mongo_collections``.  Key differences from the local
    file path:
    - ``_id`` is a ULID (time-sortable; generated on write).
    - ``called_at_utc`` is a UTC datetime (the TTL index field; 90-day expiry).
    - ``result_ok`` replaces ``success`` (BSON-friendlier naming).
    - ``session_id`` / ``tool_name`` / ``source`` / ``args_hash`` /
      ``result_ok`` / ``latency_ms`` / ``error_code`` / ``retry_attempt`` /
      ``cached_content_token_count`` map directly from the call args.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .persistence import Persistence

logger = logging.getLogger("trid3nt_server.telemetry")

_DEFAULT_TELEMETRY_PATH = "/tmp/trid3nt_tool_call_telemetry.jsonl"


def get_persistence() -> "Persistence | None":
    """Lazy wrapper around ``server.get_persistence``.

    Defined at module level so tests can patch
    ``trid3nt_server.telemetry.get_persistence`` without reaching into the
    server module.  The deferred import avoids a circular dependency at import
    time (server.py already imports from telemetry at the top level).

    Returns ``None`` if the server module hasn't finished bootstrapping yet
    (early startup) or if the Persistence singleton is unbound (M1 path).
    """
    try:
        from .server import get_persistence as _server_get_persistence
        return _server_get_persistence()
    except Exception:  # noqa: BLE001
        return None


def _get_telemetry_path() -> str:
    """Return the JSONL output path from env, falling back to the default."""
    return os.environ.get("TRID3NT_TELEMETRY_PATH", _DEFAULT_TELEMETRY_PATH)


def _hash_args(args: dict | None) -> str:
    """Return a hex-digest SHA-256 of the JSON-serialized args dict.

    Provides a stable fingerprint for dedup and tracing without storing the
    full (potentially large) args blob in the telemetry log.  Returns the
    digest of ``{}`` when ``args`` is ``None``.
    """
    payload = json.dumps(args or {}, sort_keys=True, default=str).encode()
    return hashlib.sha256(payload).hexdigest()


async def _write_line(path: str, record: dict) -> None:
    """Append one JSON-line to ``path``.

    Uses ``aiofiles`` when available (best practice for async file I/O) and
    falls back to a blocking ``open()`` + ``asyncio.get_event_loop().
    run_in_executor`` otherwise.  The fallback ensures the module works even
    if ``aiofiles`` is not installed (it is NOT in the pyproject deps; the
    executor path is the safe default until it is added).

    Never raises — any I/O error is logged at WARNING.
    """
    line = json.dumps(record, default=str) + "\n"
    try:
        aiofiles = None
        try:
            import aiofiles as _aiofiles  # type: ignore[import-not-found]
            aiofiles = _aiofiles
        except ImportError:
            pass

        if aiofiles is not None:
            async with aiofiles.open(path, mode="a", encoding="utf-8") as fh:
                await fh.write(line)
        else:
            # Fallback: blocking write via executor so the event loop is not
            # starved on slow filesystems (e.g. NFS mounts in CI).
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, _blocking_append, path, line
            )
    except Exception:  # noqa: BLE001 — telemetry must never break the call loop
        logger.warning(
            "telemetry write failed path=%s tool=%s",
            path,
            record.get("tool_name", "?"),
            exc_info=True,
        )


def _blocking_append(path: str, line: str) -> None:
    """Blocking file append — called from an executor thread only."""
    with open(path, mode="a", encoding="utf-8") as fh:
        fh.write(line)


async def _write_to_mongo(
    persistence: "Persistence",
    session_id: str,
    ts: str,
    tool_name: str,
    source: Literal["llm", "workflow", "manual"],
    args_hash: str,
    success: bool,
    latency_ms: float,
    error_code: str | None,
    retry_attempt: int,
    cached_content_token_count: int | None,
    result_usable: bool | None = None,
    routed_ok: bool | None = None,
    model_id: str | None = None,
    turn_id: str | None = None,
) -> None:
    """Emit one tool-call telemetry record to MongoDB via the MCP Persistence.

    Constructs a ``ToolCallTelemetryDocument``, validates it against the schema,
    and calls ``insert-one`` via the Persistence singleton's underlying MCP
    client.  Telemetry insert is done directly on the MCP client (bypassing the
    typed Persistence methods, which own Case/User/Secret shapes) using the
    ``tool_call_telemetry`` collection name from the contracts constant.

    Never raises — any Persistence failure is logged at WARNING.
    """
    try:
        from trid3nt_contracts import new_ulid
        from trid3nt_contracts.mongo_collections import (
            TELEMETRY_COLLECTION,
            ToolCallTelemetryDocument,
        )
        from .persistence import DEFAULT_DATABASE

        # Parse ts string to datetime for called_at_utc.  Accepts ISO-8601 with
        # trailing Z (e.g. "2026-06-09T12:34:56.789Z") or offset-aware strings.
        called_at: datetime
        if isinstance(ts, str):
            normalized = ts.replace("Z", "+00:00")
            called_at = datetime.fromisoformat(normalized)
        else:
            called_at = ts  # type: ignore[assignment]
        if called_at.tzinfo is None:
            called_at = called_at.replace(tzinfo=timezone.utc)

        doc = ToolCallTelemetryDocument(
            _id=new_ulid(),
            session_id=session_id,
            tool_name=tool_name,
            called_at_utc=called_at,
            source=source,
            args_hash=args_hash,
            result_ok=success,
            latency_ms=latency_ms,
            error_code=error_code,
            retry_attempt=retry_attempt,
            cached_content_token_count=cached_content_token_count,
            result_usable=result_usable,
            routed_ok=routed_ok,
            model_id=model_id,
        )

        body = doc.model_dump(mode="json", by_alias=True)
        # ``turn_id`` (the per-user-message dispatch / pipeline id) is the recall@k
        # join key (tool-retrieval shadow). The typed ToolCallTelemetryDocument is
        # ``extra="forbid"`` so it is NOT a model field; inject it onto the wire
        # body AFTER the validated dump so a recall@k reader can join a dispatched
        # llm tool to ITS turn's shadow-selection row without a contract change.
        if turn_id is not None:
            body["turn_id"] = turn_id

        await persistence._mcp.call_tool(
            "insert-one",
            {
                "database": DEFAULT_DATABASE,
                "collection": TELEMETRY_COLLECTION,
                "document": body,
            },
        )
    except Exception:  # noqa: BLE001 — telemetry must never break the call loop
        logger.warning(
            "telemetry mongo write failed tool=%s session=%s",
            tool_name,
            session_id,
            exc_info=True,
        )


async def emit_tool_call_event(
    session_id: str,
    ts: str,
    tool_name: str,
    source: Literal["llm", "workflow", "manual"],
    args_hash: str,
    success: bool,
    latency_ms: float,
    error_code: str | None = None,
    retry_attempt: int = 0,
    cached_content_token_count: int | None = None,
    result_usable: bool | None = None,
    routed_ok: bool | None = None,
    model_id: str | None = None,
    turn_id: str | None = None,
) -> None:
    """Emit one tool-call telemetry record (non-blocking).

    The write is scheduled as a fire-and-forget asyncio task.  The caller
    does NOT await completion — latency impact on the tool-dispatch loop is
    bounded by the time to enqueue the task (microseconds), not the actual
    I/O.

    Backend selection:
    - When the app-level ``Persistence`` singleton (from ``server.get_persistence``)
      is bound (i.e. ``TRID3NT_MONGO_MCP_STDIO=1`` or dev-file mode), the record
      is written to the ``tool_call_telemetry`` MongoDB collection via MCP.
    - When ``Persistence`` is unbound (M1 in-memory / CI without Atlas), the
      record falls back to the local-file JSONL path (``TRID3NT_TELEMETRY_PATH``
      or the default ``/tmp/trid3nt_tool_call_telemetry.jsonl``).

    Args:
        session_id: WebSocket session identifier (ULID string).
        ts: ISO-8601 UTC timestamp of the tool call start (e.g.
            ``"2026-06-09T12:34:56.789Z"``).  Callers should pass
            ``trid3nt_contracts.now_utc().isoformat()`` or equivalent.
        tool_name: Registered tool name (e.g. ``"fetch_dem"``).
        source: Where the call originated.
            - ``"llm"`` — Gemini-initiated ``function_call`` in the multi-turn
              loop (``_stream_gemini_reply``).
            - ``"workflow"`` — inside-composer dispatch (future; reserved for
              Wave 4.11+ workflow orchestration paths).
            - ``"manual"`` — ``/invoke`` directive from the debug harness or
              a test fixture.
        args_hash: Hex digest of SHA-256 over the JSON-serialized args dict.
            Use ``telemetry.compute_args_hash(args)`` to build this.
        success: ``True`` when the tool returned without raising; ``False``
            when ``dispatch_error`` was set in the call loop.
        latency_ms: Wall-clock elapsed time from dispatch to result, in
            milliseconds (float precision).
        error_code: A.6 / FR-AS-11 error code string when ``success=False``;
            ``None`` on success or when unavailable.
        retry_attempt: Zero-based retry counter.  ``0`` for the first (or
            only) attempt; ``1`` for the first retry, etc.
        cached_content_token_count: Gemini ``UsageMetadata.
            cached_content_token_count`` from the response that triggered
            this call.  ``None`` when the field is absent or the stream did
            not report usage metadata (e.g. mid-stream chunks).
        result_usable: Whether the call produced a USABLE result, distinct
            from ``success`` (tool-accuracy panel, NATE 2026-06-17). ``False``
            for a layer-producing tool whose result carried no renderable
            layer (the honesty-floor NO_RENDERABLE_LAYER case) even when
            ``success=True``; ``True`` for a real renderable / non-empty data
            result; ``None`` where the notion does not apply (meta tools).
            Derived at the dispatch chokepoint by
            ``adapter.classify_result_usable``.
        routed_ok: Routing-quality heuristic (NOT ground truth). ``False``
            when this call was immediately superseded within the same session
            by a DIFFERENT tool for the same logical step (a mis-route the
            model corrected); ``True`` when not superseded; ``None`` when the
            signal is unavailable.
    """
    # Resolve the Persistence singleton via the module-level lazy wrapper.
    # That wrapper defers the import of server.py to avoid a circular import
    # at module load time.  Tests can patch ``trid3nt_server.telemetry.
    # get_persistence`` to inject a mock without touching the server module.
    # We defensively catch any exception from get_persistence() itself so that
    # failures during early startup (e.g. ImportError) always fall through
    # to the local-file path rather than propagating.
    try:
        persistence: "Persistence | None" = get_persistence()
    except Exception:  # noqa: BLE001
        persistence = None

    if persistence is not None:
        # MCP-backed path: fire-and-forget to Mongo.
        asyncio.ensure_future(
            _write_to_mongo(
                persistence=persistence,
                session_id=session_id,
                ts=ts,
                tool_name=tool_name,
                source=source,
                args_hash=args_hash,
                success=success,
                latency_ms=latency_ms,
                error_code=error_code,
                retry_attempt=retry_attempt,
                cached_content_token_count=cached_content_token_count,
                result_usable=result_usable,
                routed_ok=routed_ok,
                model_id=model_id,
                turn_id=turn_id,
            )
        )
        return

    # Local-file fallback (v0 path — preserved for backward compat).
    record: dict = {
        "session_id": session_id,
        "ts": ts,
        "tool_name": tool_name,
        "source": source,
        "args_hash": args_hash,
        "success": success,
        "latency_ms": latency_ms,
        "error_code": error_code,
        "retry_attempt": retry_attempt,
        "cached_content_token_count": cached_content_token_count,
        "result_usable": result_usable,
        "routed_ok": routed_ok,
        "model_id": model_id,
        # turn_id (pipeline id) — recall@k join key (tool-retrieval shadow).
        # Omitted (absent, not null) when the caller did not supply it so old
        # readers + records stay byte-compatible.
        **({"turn_id": turn_id} if turn_id is not None else {}),
    }
    path = _get_telemetry_path()
    # Fire-and-forget: the event loop schedules the write; we do not await it.
    asyncio.ensure_future(_write_line(path, record))


def compute_args_hash(args: dict | None) -> str:
    """Public helper — compute the SHA-256 hex digest for a tool's args dict.

    Callers in ``server.py`` should use this rather than re-implementing the
    digest logic.  Safe to call from sync contexts (no I/O).
    """
    return _hash_args(args)


# --------------------------------------------------------------------------- #
# Tool-retrieval SHADOW telemetry (tool-retrieval kickoff — orchestrator half).
#
# Shadow mode computes the WOULD-BE-visible tool set per turn via
# ``retrieve_visible_tools`` WITHOUT changing the catalog the model actually
# sees (the model still sees the full registry). We log that would-be set so a
# recall@k measurement (tool_catalog_http.build_telemetry_summary) can compare
# it against the tools the LLM actually dispatched that turn, per North-Star
# flow. recall = |dispatched-llm-tools that WERE in the retrieved set| /
# |dispatched-llm-tools|.
#
# Same dual-sink discipline as ``emit_tool_call_event``: written to the SAME
# ``tool_call_telemetry`` MongoDB collection when Persistence is bound (carrying
# a ``record_type="tool_retrieval_shadow"`` discriminator so a reader can split
# these rows from the per-tool ``tool_call`` rows), PLUS the /tmp JSONL fallback
# when Persistence is unbound. Fire-and-forget; NEVER raises — telemetry must
# never break the dispatch loop (mirrors ``emit_tool_call_event``).
# --------------------------------------------------------------------------- #

#: The discriminator stamped on every shadow-selection record so a reader can
#: separate them from per-tool ``tool_call`` rows that share the sink.
SHADOW_RECORD_TYPE = "tool_retrieval_shadow"


def build_shadow_selection_record(
    *,
    session_id: str,
    turn_id: str,
    user_text: str,
    visible_tools: "set[str] | frozenset[str] | list[str]",
    mode: str,
    k: int,
    full_registry_size: int | None = None,
    ts: str | None = None,
    model_id: str | None = None,
) -> dict:
    """Build the per-turn shadow-selection record (pure — no I/O).

    Split out so tests can assert the record SHAPE without touching the sink.
    ``visible_tools`` is the would-be-visible set ``retrieve_visible_tools``
    returned for this turn; ``turn_id`` is the per-user-message dispatch id (the
    ``pipeline_id``) so recall@k can join a dispatched llm tool to ITS turn's set.

    ``user_text`` is truncated to keep the record bounded; the full text is not
    needed for recall (the join key is ``turn_id``).
    """
    try:
        visible_sorted = sorted({str(t) for t in (visible_tools or [])})
    except Exception:  # noqa: BLE001 — defensive; never break the dispatch loop
        visible_sorted = []
    text = user_text if isinstance(user_text, str) else ""
    return {
        "record_type": SHADOW_RECORD_TYPE,
        "session_id": session_id,
        "turn_id": turn_id,
        "ts": ts or now_iso_utc(),
        "user_text": text[:280],
        "mode": mode,
        "k": int(k),
        "visible_tools": visible_sorted,
        "visible_count": len(visible_sorted),
        "full_registry_size": full_registry_size,
        "model_id": model_id,
    }


def emit_shadow_selection_event(
    *,
    session_id: str,
    turn_id: str,
    user_text: str,
    visible_tools: "set[str] | frozenset[str] | list[str]",
    mode: str,
    k: int,
    full_registry_size: int | None = None,
    model_id: str | None = None,
) -> None:
    """Emit one tool-retrieval shadow-selection record (non-blocking).

    Fire-and-forget + NEVER raises (mirrors ``emit_tool_call_event``): the write
    is scheduled as an asyncio task and the caller does not await it. Backend
    selection mirrors the per-tool path — the bound MongoDB ``tool_call_telemetry``
    collection (with the ``record_type`` discriminator) when Persistence is bound,
    else the local-file JSONL fallback.
    """
    try:
        record = build_shadow_selection_record(
            session_id=session_id,
            turn_id=turn_id,
            user_text=user_text,
            visible_tools=visible_tools,
            mode=mode,
            k=k,
            full_registry_size=full_registry_size,
            model_id=model_id,
        )
    except Exception:  # noqa: BLE001 — telemetry must never break the dispatch loop
        logger.warning("shadow telemetry: record build failed", exc_info=True)
        return

    try:
        persistence: "Persistence | None" = get_persistence()
    except Exception:  # noqa: BLE001
        persistence = None

    if persistence is not None:
        try:
            asyncio.ensure_future(
                _write_shadow_to_mongo(persistence, record)
            )
        except Exception:  # noqa: BLE001 — fall through to the file path
            logger.warning(
                "shadow telemetry: mongo schedule failed; falling to file",
                exc_info=True,
            )
        else:
            return

    # Local-file fallback (same JSONL sink as the per-tool path).
    path = _get_telemetry_path()
    try:
        asyncio.ensure_future(_write_line(path, record))
    except Exception:  # noqa: BLE001 — telemetry must never break the dispatch loop
        logger.warning("shadow telemetry: file schedule failed", exc_info=True)


async def _write_shadow_to_mongo(
    persistence: "Persistence", record: dict
) -> None:
    """Insert one shadow-selection record into the ``tool_call_telemetry``
    collection via the MCP client. Never raises (logged at WARNING).

    The shadow record carries a ``record_type`` discriminator + a ``visible_tools``
    array that the per-tool ``ToolCallTelemetryDocument`` schema does not model, so
    it is inserted as a raw document (the summary reader keys off ``record_type``).
    """
    try:
        from trid3nt_contracts import new_ulid
        from trid3nt_contracts.mongo_collections import TELEMETRY_COLLECTION
        from .persistence import DEFAULT_DATABASE

        body = dict(record)
        body["_id"] = new_ulid()
        # The TTL index keys off ``called_at_utc`` on the per-tool rows; mirror it
        # so a shadow row expires on the same 90-day schedule.
        ts = record.get("ts")
        if isinstance(ts, str):
            normalized = ts.replace("Z", "+00:00")
            try:
                body["called_at_utc"] = datetime.fromisoformat(normalized)
            except ValueError:
                body["called_at_utc"] = datetime.now(timezone.utc)
        else:
            body["called_at_utc"] = datetime.now(timezone.utc)

        await persistence._mcp.call_tool(
            "insert-one",
            {
                "database": DEFAULT_DATABASE,
                "collection": TELEMETRY_COLLECTION,
                "document": body,
            },
        )
    except Exception:  # noqa: BLE001 — telemetry must never break the dispatch loop
        logger.warning(
            "shadow telemetry mongo write failed turn=%s session=%s",
            record.get("turn_id"),
            record.get("session_id"),
            exc_info=True,
        )


def now_iso_utc() -> str:
    """ISO-8601 UTC timestamp (millisecond precision, trailing Z)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# --------------------------------------------------------------------------- #
# Solve-time telemetry (sprint-16 — SFINCS per-job autoscale)
#
# At solve completion we accumulate real (active_cells, vCPU, wall_clock) data
# so the adaptive-grid cell cap can be re-tuned from logged measurements
# (MEASURE-then-tune). Emitted to the SAME sink discipline as tool_call
# telemetry: a structured logger line ALWAYS (so it lands in the agent log /
# routing dashboard scrape even when the JSONL sink is unwritable) PLUS the
# JSONL record. The record is intentionally NOT MCP-routed (no Mongo collection
# contract exists for it yet; the local JSONL + structured log line is the
# minimum the kickoff names — a Mongo collection can be added later without
# changing call sites).
# --------------------------------------------------------------------------- #

_DEFAULT_SOLVE_TELEMETRY_PATH = "/tmp/trid3nt_solve_telemetry.jsonl"

#: A dedicated structured logger so a routing-dashboard / log scrape can grep
#: ``trid3nt_server.solve_telemetry`` lines out of the agent log even when the
#: JSONL file path is unwritable.
solve_logger = logging.getLogger("trid3nt_server.solve_telemetry")


def _get_solve_telemetry_path() -> str:
    """JSONL output path for solve telemetry (env-overridable)."""
    return os.environ.get(
        "TRID3NT_SOLVE_TELEMETRY_PATH", _DEFAULT_SOLVE_TELEMETRY_PATH
    )


def build_solve_telemetry_record(
    *,
    run_id: str,
    backend: str,
    active_cell_count: int | None,
    grid_resolution_m: float | None,
    vcpus: int | None,
    wall_clock_seconds: float | None,
    aoi_km2: float | None,
    solver: str = "sfincs",
    estimated_solve_seconds: float | None = None,
    coarsened: bool | None = None,
    ts: str | None = None,
) -> dict:
    """Build the structured solve-telemetry record (pure — no I/O).

    Split out so tests can assert the record SHAPE without touching the sink.
    The required fields the kickoff names: ``active_cell_count``,
    ``grid_resolution_m``, ``vcpus``, ``wall_clock_seconds``, ``backend``,
    ``run_id``, ``aoi_km2``.
    """
    return {
        "kind": "solve_telemetry",
        "run_id": run_id,
        "solver": solver,
        "backend": backend,
        "active_cell_count": active_cell_count,
        "grid_resolution_m": grid_resolution_m,
        "vcpus": vcpus,
        "wall_clock_seconds": wall_clock_seconds,
        "aoi_km2": aoi_km2,
        "estimated_solve_seconds": estimated_solve_seconds,
        "coarsened": coarsened,
        "ts": ts
        or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
    }


def emit_solve_telemetry(
    *,
    run_id: str,
    backend: str,
    active_cell_count: int | None,
    grid_resolution_m: float | None,
    vcpus: int | None,
    wall_clock_seconds: float | None,
    aoi_km2: float | None,
    solver: str = "sfincs",
    estimated_solve_seconds: float | None = None,
    coarsened: bool | None = None,
) -> dict:
    """Emit one solve-completion telemetry record (structured log + JSONL).

    Synchronous + best-effort: a structured INFO line is ALWAYS logged; the
    JSONL append is wrapped so a sink failure never propagates into the solve
    path. Returns the record (so the workflow can also fold it into provenance /
    a test can assert it). Mirrors ``emit_tool_call_event``'s never-raise
    contract — telemetry must never break the solve loop.
    """
    record = build_solve_telemetry_record(
        run_id=run_id,
        backend=backend,
        active_cell_count=active_cell_count,
        grid_resolution_m=grid_resolution_m,
        vcpus=vcpus,
        wall_clock_seconds=wall_clock_seconds,
        aoi_km2=aoi_km2,
        solver=solver,
        estimated_solve_seconds=estimated_solve_seconds,
        coarsened=coarsened,
    )
    # Always log the structured line (the durable, scrape-able signal).
    solve_logger.info(
        "solve_telemetry run_id=%s backend=%s solver=%s active_cells=%s "
        "grid_res_m=%s vcpus=%s wall_clock_s=%s aoi_km2=%s est_solve_s=%s "
        "coarsened=%s",
        run_id,
        backend,
        solver,
        active_cell_count,
        grid_resolution_m,
        vcpus,
        wall_clock_seconds,
        aoi_km2,
        estimated_solve_seconds,
        coarsened,
    )
    # Best-effort JSONL append (the accumulation sink for re-tuning the cap).
    try:
        path = _get_solve_telemetry_path()
        with open(path, mode="a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except Exception:  # noqa: BLE001 — telemetry must never break the solve loop
        solve_logger.warning(
            "solve_telemetry JSONL write failed run_id=%s", run_id, exc_info=True
        )
    return record


# --------------------------------------------------------------------------- #
# SOLVE completion telemetry — Batch instance + problem size + timing (task-153)
#
# A richer sibling to ``emit_solve_telemetry`` (above). Where that record carries
# the autoscale provenance for re-tuning the adaptive cell cap, THIS record folds
# in the REAL AWS Batch compute the solve landed on — the Spot instance type +
# lifecycle + AZ + the queue-provision / compute / total timing breakdown
# (captured by ``solver._capture_batch_compute_meta``) MERGED with the mesh size
# descriptor (active_cell_count + resolution_m) — so a perf model can later infer
# completion time from real (instance, problem-size, wall-clock) measurements.
#
# Same sink discipline as the per-tool + autoscale telemetry: a structured INFO
# line ALWAYS (scrape-able out of the agent log even when the JSONL path is
# unwritable) PLUS a JSONL append, both wrapped so a sink failure never breaks
# the solve. Carries a ``record_type="solve"`` discriminator so a reader can
# distinguish these rows from the per-tool ``tool_call`` rows that share the
# accumulation sink. NOT MCP-routed yet (no Mongo collection contract for it);
# the JSONL + structured log is the minimum — a Mongo collection can be folded
# in later without changing call sites (mirrors ``emit_solve_telemetry``).
# --------------------------------------------------------------------------- #

#: Dedicated structured logger so a log scrape can grep these rows out of the
#: agent log even when the JSONL file path is unwritable (mirrors solve_logger).
solve_meta_logger = logging.getLogger("trid3nt_server.solve_telemetry")


def record_solve_telemetry(record: dict) -> dict:
    """Write ONE SOLVE-completion telemetry record (structured log + JSONL).

    The record is built by the composer (see
    ``model_flood_scenario`` / ``model_urban_flood_swmm``) by MERGING the Batch
    compute meta (``solver._capture_batch_compute_meta`` — instance + timing) with
    the mesh size descriptor + solver + terminal status + run/case/session ids.
    This writer stamps a ``record_type="solve"`` discriminator and a ``ts`` when
    absent, then emits to the SAME accumulation sink (JSONL at
    ``TRID3NT_SOLVE_TELEMETRY_PATH`` / the default) the autoscale solve telemetry
    uses, plus an ALWAYS-on structured INFO line.

    Record shape (the keys a complete row carries — every field is optional so a
    partial capture still records what it has)::

        {
            "record_type":          "solve",
            "ts":                   str  (ISO-8601 UTC; stamped if absent),
            "run_id":               str | None,
            "solver":               str | None   ("sfincs" | "swmm" | ...),
            "status":               str | None   (terminal: "complete"/"failed"/...),
            "backend":              str | None   (handle.workflow_name),
            "case_id":              str | None,
            "session_id":           str | None,
            # --- mesh size descriptor (the problem size) ---
            "active_cell_count":    int | None,
            "resolution_m":         float | None,
            # --- AWS Batch compute meta (instance + timing) ---
            "instance_type":        str | None   (e.g. "c7i.2xlarge"),
            "instance_lifecycle":   str | None   ("spot" | "on-demand"),
            "az":                   str | None   (e.g. "us-west-2d"),
            "vcpus":                int | None,
            "memory_mib":           int | None,
            "created_at_ms":        int | None,
            "started_at_ms":        int | None,
            "stopped_at_ms":        int | None,
            "queue_provision_secs": float | None (started - created),
            "compute_secs":         float | None (stopped - started),
            "total_secs":           float | None (stopped - created),
        }

    Best-effort + synchronous: mirrors ``emit_solve_telemetry``'s never-raise
    contract — telemetry must NEVER break the solve path. Returns the stamped
    record (so the composer can fold it into provenance / a test can assert it).
    """
    try:
        rec = dict(record or {})
    except Exception:  # noqa: BLE001 — defensive; never break the solve
        rec = {}
    rec.setdefault("record_type", "solve")
    rec.setdefault(
        "ts",
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
    )

    # Always log the structured line (the durable, scrape-able signal).
    solve_meta_logger.info(
        "solve_record run_id=%s solver=%s status=%s instance_type=%s "
        "lifecycle=%s az=%s vcpus=%s active_cells=%s resolution_m=%s "
        "queue_provision_s=%s compute_s=%s total_s=%s backend=%s case=%s",
        rec.get("run_id"),
        rec.get("solver"),
        rec.get("status"),
        rec.get("instance_type"),
        rec.get("instance_lifecycle"),
        rec.get("az"),
        rec.get("vcpus"),
        rec.get("active_cell_count"),
        rec.get("resolution_m"),
        rec.get("queue_provision_secs"),
        rec.get("compute_secs"),
        rec.get("total_secs"),
        rec.get("backend"),
        rec.get("case_id"),
    )
    # Best-effort JSONL append (the accumulation sink the perf model reads).
    try:
        path = _get_solve_telemetry_path()
        with open(path, mode="a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
    except Exception:  # noqa: BLE001 — telemetry must never break the solve loop
        solve_meta_logger.warning(
            "solve_record JSONL write failed run_id=%s",
            rec.get("run_id"),
            exc_info=True,
        )
    return rec


# --------------------------------------------------------------------------- #
# PER-TURN telemetry (LANE CORE, 2026-07-22).
#
# One record per user-message turn, persisted BESIDE the tool-call telemetry
# (its own JSONL sink -- follows ``record_solve_telemetry``'s own-sink pattern)
# plus an ALWAYS-on structured INFO line. Token counts come from the adapter's
# ``UsageMetadataEvent``s (openai: ``stream_options include_usage`` final
# chunk; bedrock: Converse ``metadata.usage``), SUMMED across the turn's model
# rounds; ``reasoning_tokens`` only where the provider reports the figure --
# absent is recorded as null, NEVER fabricated. ``error_class`` is null on a
# clean turn; ``"upstream_provider"`` when the turn died on a transient
# provider failure after retry exhaustion (NATE hard rule: upstream failure is
# never internalized); ``"provider_request"`` for a non-transient provider
# rejection; ``"internal"`` for our own bugs; ``"cancelled"`` /
# ``"context_window"`` / ``"client_disconnect"`` for those turn endings.
#
# Write path honors the no-sync-blocking rule: ``emit_turn_telemetry`` is
# fire-and-forget -- it schedules the JSONL append through the async
# ``_write_line`` helper (aiofiles or an executor thread) and returns
# immediately. NEVER raises (telemetry must never break the turn loop).
# --------------------------------------------------------------------------- #

_DEFAULT_TURN_TELEMETRY_PATH = "/tmp/trid3nt_turn_telemetry.jsonl"

#: Discriminator stamped on every per-turn record.
TURN_RECORD_TYPE = "turn"

#: Dedicated structured logger (scrape-able out of the agent log even when the
#: JSONL path is unwritable -- mirrors solve_logger).
turn_logger = logging.getLogger("trid3nt_server.turn_telemetry")


def _get_turn_telemetry_path() -> str:
    """JSONL output path for per-turn telemetry (env-overridable)."""
    return os.environ.get(
        "TRID3NT_TURN_TELEMETRY_PATH", _DEFAULT_TURN_TELEMETRY_PATH
    )


def build_turn_telemetry_record(
    *,
    turn_id: str,
    session_id: str,
    case_id: str | None,
    model_id: str | None,
    provider: str | None,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    reasoning_tokens: int | None,
    turn_wall_ms: float | None,
    tool_dispatch_count: int,
    error_class: str | None = None,
    ts: str | None = None,
) -> dict:
    """Build one per-turn telemetry record (pure -- no I/O; testable shape).

    Record shape (one JSON object per line, ``record_type="turn"``)::

        {turn_id, session_id, case_id, model_id, provider,
         prompt_tokens, completion_tokens, reasoning_tokens,
         turn_wall_ms, tool_dispatch_count, error_class|null, ts}
    """
    return {
        "record_type": TURN_RECORD_TYPE,
        "turn_id": turn_id,
        "session_id": session_id,
        "case_id": case_id,
        "model_id": model_id,
        "provider": provider,
        "prompt_tokens": int(prompt_tokens) if prompt_tokens is not None else None,
        "completion_tokens": (
            int(completion_tokens) if completion_tokens is not None else None
        ),
        "reasoning_tokens": (
            int(reasoning_tokens) if reasoning_tokens is not None else None
        ),
        "turn_wall_ms": (
            round(float(turn_wall_ms), 1) if turn_wall_ms is not None else None
        ),
        "tool_dispatch_count": int(tool_dispatch_count),
        "error_class": error_class,
        "ts": ts or now_iso_utc(),
    }


def emit_turn_telemetry(
    *,
    turn_id: str,
    session_id: str,
    case_id: str | None,
    model_id: str | None,
    provider: str | None,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    reasoning_tokens: int | None,
    turn_wall_ms: float | None,
    tool_dispatch_count: int,
    error_class: str | None = None,
) -> dict | None:
    """Emit ONE per-turn telemetry record (structured INFO line + async JSONL).

    Fire-and-forget + NEVER raises (mirrors ``emit_tool_call_event``): the
    JSONL append is scheduled through the async ``_write_line`` helper
    (aiofiles / executor thread -- no sync blocking on the event loop) and not
    awaited. The INFO line always fires so the record survives an unwritable
    sink. Returns the built record (for tests / callers), or ``None`` if even
    the record build failed.
    """
    try:
        record = build_turn_telemetry_record(
            turn_id=turn_id,
            session_id=session_id,
            case_id=case_id,
            model_id=model_id,
            provider=provider,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            reasoning_tokens=reasoning_tokens,
            turn_wall_ms=turn_wall_ms,
            tool_dispatch_count=tool_dispatch_count,
            error_class=error_class,
        )
    except Exception:  # noqa: BLE001 -- telemetry must never break the turn loop
        turn_logger.warning("turn telemetry: record build failed", exc_info=True)
        return None

    # Always log the structured line (the durable, scrape-able signal).
    turn_logger.info(
        "turn_telemetry turn=%s session=%s case=%s model=%s provider=%s "
        "prompt_tokens=%s completion_tokens=%s reasoning_tokens=%s "
        "wall_ms=%s tools=%s error_class=%s",
        record["turn_id"],
        record["session_id"],
        record["case_id"],
        record["model_id"],
        record["provider"],
        record["prompt_tokens"],
        record["completion_tokens"],
        record["reasoning_tokens"],
        record["turn_wall_ms"],
        record["tool_dispatch_count"],
        record["error_class"],
    )
    try:
        asyncio.ensure_future(_write_line(_get_turn_telemetry_path(), record))
    except Exception:  # noqa: BLE001 -- e.g. no running loop in a sync test
        turn_logger.warning(
            "turn telemetry: JSONL schedule failed turn=%s",
            record.get("turn_id"),
            exc_info=True,
        )
    return record


def load_turn_records(path: str | None = None, *, max_records: int = 5000) -> list[dict]:
    """Read per-turn records from the JSONL sink (newest LAST, file order).

    Tolerant reader: a missing / unreadable file or a malformed line yields
    what could be read (never raises). Only rows carrying
    ``record_type == TURN_RECORD_TYPE`` are returned. ``max_records`` bounds
    memory on a long-lived sink (the TAIL is kept -- most recent turns win).
    """
    target = path or _get_turn_telemetry_path()
    out: list[dict] = []
    try:
        with open(target, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and rec.get("record_type") == TURN_RECORD_TYPE:
                    out.append(rec)
    except OSError:
        return []
    if len(out) > max_records:
        out = out[-max_records:]
    return out


def _mean(values: list[float]) -> float | None:
    """Mean of the non-empty list, rounded; ``None`` for no data (honest --
    never fabricate a zero mean from zero observations)."""
    if not values:
        return None
    return round(sum(values) / len(values), 1)


def build_turn_summary(records: list[dict]) -> dict:
    """Aggregate per-turn records into the /api/telemetry/summary section.

    Shape::

        {
          "total_turns": int,
          "models": [
            {"model_id": str|None, "provider": str|None, "turns": int,
             "mean_prompt_tokens": float|None,
             "mean_completion_tokens": float|None,
             "mean_reasoning_tokens": float|None,
             "mean_wall_ms": float|None,
             "upstream_error_count": int,
             "error_count": int},
            ...  # sorted by turns desc
          ],
        }

    Means are computed over the turns that REPORTED the figure (token counts
    are null where a provider does not report usage -- those rows do not drag
    a mean to zero). ``upstream_error_count`` counts
    ``error_class == "upstream_provider"`` rows; ``error_count`` counts ALL
    non-null error classes.
    """
    by_model: dict[str, dict] = {}
    for rec in records or []:
        if not isinstance(rec, dict):
            continue
        key = str(rec.get("model_id"))
        bucket = by_model.setdefault(
            key,
            {
                "model_id": rec.get("model_id"),
                "provider": rec.get("provider"),
                "turns": 0,
                "_prompt": [],
                "_completion": [],
                "_reasoning": [],
                "_wall": [],
                "upstream_error_count": 0,
                "error_count": 0,
            },
        )
        bucket["turns"] += 1
        for field, acc in (
            ("prompt_tokens", "_prompt"),
            ("completion_tokens", "_completion"),
            ("reasoning_tokens", "_reasoning"),
            ("turn_wall_ms", "_wall"),
        ):
            val = rec.get(field)
            if isinstance(val, (int, float)):
                bucket[acc].append(float(val))
        err = rec.get("error_class")
        if err is not None:
            bucket["error_count"] += 1
            if err == "upstream_provider":
                bucket["upstream_error_count"] += 1

    models: list[dict] = []
    for bucket in by_model.values():
        models.append(
            {
                "model_id": bucket["model_id"],
                "provider": bucket["provider"],
                "turns": bucket["turns"],
                "mean_prompt_tokens": _mean(bucket["_prompt"]),
                "mean_completion_tokens": _mean(bucket["_completion"]),
                "mean_reasoning_tokens": _mean(bucket["_reasoning"]),
                "mean_wall_ms": _mean(bucket["_wall"]),
                "upstream_error_count": bucket["upstream_error_count"],
                "error_count": bucket["error_count"],
            }
        )
    models.sort(key=lambda m: (-m["turns"], str(m["model_id"])))
    return {"total_turns": sum(m["turns"] for m in models), "models": models}


def empty_turn_summary() -> dict:
    """Zero-state turn-summary shape (no turn telemetry recorded yet)."""
    return {"total_turns": 0, "models": []}


def build_live_solve_progress(
    *,
    run_id: str,
    solver: str,
    grid_resolution_m: float | None,
    active_cell_count: int | None,
    vcpus: int | None,
    elapsed_seconds: float,
    eta_seconds: float | None = None,
) -> dict:
    """Build the LIVE big-sim progress payload (server -> web; pure, no I/O).

    Shape (the SHARED WIRE CONTRACT, tool-accuracy panel NATE 2026-06-17)::

        {run_id, solver, grid_resolution_m, active_cell_count, vcpus,
         elapsed_seconds, eta_seconds|null}

    Emitted on the running tool/pipeline card during a solve so the user sees
    grid resolution / active-cell count / vCPU / elapsed / ETA tick on the live
    card (rather than a silent multi-minute spinner). ``eta_seconds`` comes from
    the perf model (the autoscale ``estimated_solve_seconds``) when available,
    else ``None``. Reuses the job-0359 solve-telemetry field names so the live
    envelope and the at-completion record speak the same vocabulary.

    Split out (like ``build_solve_telemetry_record``) so the wire shape can be
    asserted in tests without an emitter / websocket.
    """
    return {
        "run_id": run_id,
        "solver": solver,
        "grid_resolution_m": (
            float(grid_resolution_m) if grid_resolution_m is not None else None
        ),
        "active_cell_count": (
            int(active_cell_count) if active_cell_count is not None else None
        ),
        "vcpus": int(vcpus) if vcpus is not None else None,
        "elapsed_seconds": float(elapsed_seconds),
        "eta_seconds": float(eta_seconds) if eta_seconds is not None else None,
    }


__all__ = [
    "emit_tool_call_event",
    "compute_args_hash",
    "emit_solve_telemetry",
    "build_solve_telemetry_record",
    "record_solve_telemetry",
    "build_live_solve_progress",
    # per-turn telemetry (LANE CORE 2026-07-22)
    "TURN_RECORD_TYPE",
    "build_turn_telemetry_record",
    "emit_turn_telemetry",
    "load_turn_records",
    "build_turn_summary",
    "empty_turn_summary",
    # tool-retrieval shadow telemetry (orchestrator half).
    "SHADOW_RECORD_TYPE",
    "build_shadow_selection_record",
    "emit_shadow_selection_event",
    "now_iso_utc",
]
