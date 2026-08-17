"""Tool-call telemetry writer.

Emits one JSON-line per LLM-initiated or workflow-initiated tool call to a
local JSONL file -- the single tool-call telemetry sink. This is the product
sink benches and routing scouts read; it is written UNCONDITIONALLY, regardless
of whether the app-level ``Persistence`` singleton is bound. (A former variant
mirrored these rows into a MongoDB ``tool_call_telemetry`` collection when
Persistence was bound; that route was cut -- telemetry is JSONL-only now.)

Write path is fire-and-forget: ``emit_tool_call_event`` schedules an async
write task and returns immediately.  A write failure is logged at WARNING level
but never raised -- telemetry must never break the tool-dispatch loop.

Configuration (deliberate retention -- session/boot-segmented, NATE decision
2026-07-30; ephemerality is POLICY, daemon-enforced, not accident):
    ``TRID3NT_TELEMETRY_PATH`` unset, or set to a DIRECTORY, selects
    directory-mode: one JSONL segment per daemon boot,
    ``<dir>/tool_calls.<boot_id>.jsonl``. Default dir: ``/tmp/trid3nt_telemetry``.
    ``main.run()`` prunes segments beyond the last ``TRID3NT_TELEMETRY_KEEP``
    (default 3) at every daemon boot (``cleanup_telemetry_segments``).
    ``TRID3NT_TELEMETRY_PATH`` set to an EXACT ``*.jsonl`` file is the legacy
    unsegmented override (back-compat for pinned test/ops paths) -- reads and
    writes go to exactly that one file, no segmentation, no cleanup.
    Readers (``load_tool_call_records``) default to the CURRENT segment;
    pass ``all_segments=True`` to read every retained segment.

Record shape (one JSON object per line, newline-terminated):
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
        "result_usable":               bool | null,
        "routed_ok":                   bool | null,
        "model_id":                    str | null,
        "turn_id":                     str | null   (omitted, not null, when absent),
    }

Tool-retrieval SHADOW rows (``record_type="tool_retrieval_shadow"``) share this
same JSONL sink; readers split the two by ``record_type`` (its ABSENCE marks a
per-tool-call row -- see ``load_tool_call_records``).
"""

from __future__ import annotations

import asyncio
import glob
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

# --------------------------------------------------------------------------- #
# Deliberate telemetry retention (session/boot-segmented sink; NATE decision
# 2026-07-30). Ephemerality is POLICY, daemon-enforced at boot
# (``cleanup_telemetry_segments``), not a platform accident.
#
# Back-compat: an explicit ``TRID3NT_TELEMETRY_PATH`` ending in ``.jsonl`` is
# an EXACT single-file override -- unsegmented, byte-identical to the prior
# behavior (every existing test pins this env var to one tmp file and reads it
# directly; that contract does not change). Unset, or set to a directory (no
# ``.jsonl`` suffix), the sink is DIRECTORY-mode: one JSONL segment per daemon
# boot, named ``tool_calls.<boot_id>.jsonl``, so a crash-looped or long-lived
# daemon never re-grows one unbounded file. Default directory stays /tmp-class.
# --------------------------------------------------------------------------- #

_DEFAULT_TELEMETRY_DIR = "/tmp/trid3nt_telemetry"
_TELEMETRY_BASENAME = "tool_calls"
_DEFAULT_TELEMETRY_KEEP = 3

#: Process-lifetime boot id (lazily generated once, cached). Override via
#: ``TRID3NT_TELEMETRY_SESSION_ID`` for deterministic test/ops control.
_BOOT_ID: str | None = None


def _is_explicit_file_override(raw: str) -> bool:
    """True when ``raw`` names an exact file (legacy unsegmented override)."""
    return raw.endswith(".jsonl")


def _telemetry_boot_id() -> str:
    override = os.environ.get("TRID3NT_TELEMETRY_SESSION_ID")
    if override:
        return override
    global _BOOT_ID
    if _BOOT_ID is None:
        _BOOT_ID = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}Z-{os.getpid()}"
    return _BOOT_ID


def _telemetry_dir() -> str:
    raw = os.environ.get("TRID3NT_TELEMETRY_PATH")
    if raw and not _is_explicit_file_override(raw):
        return raw
    return _DEFAULT_TELEMETRY_DIR


def _telemetry_keep() -> int:
    raw = os.environ.get("TRID3NT_TELEMETRY_KEEP")
    if raw is None:
        return _DEFAULT_TELEMETRY_KEEP
    try:
        val = int(raw)
        return val if val >= 1 else _DEFAULT_TELEMETRY_KEEP
    except ValueError:
        logger.warning(
            "TRID3NT_TELEMETRY_KEEP=%r is not a valid positive integer; "
            "using default %d",
            raw,
            _DEFAULT_TELEMETRY_KEEP,
        )
        return _DEFAULT_TELEMETRY_KEEP


def _list_telemetry_segments() -> list[str]:
    """Every retained segment file in directory mode, oldest-first (name-sorted;

    the boot-id prefix is a UTC timestamp so lexical order == chronological
    order). Empty when the directory does not exist yet.
    """
    pattern = os.path.join(_telemetry_dir(), f"{_TELEMETRY_BASENAME}.*.jsonl")
    return sorted(glob.glob(pattern))


def get_persistence() -> "Persistence | None":
    """Lazy wrapper around ``server.get_persistence``.

    Defined at module level so tests can patch
    ``trid3nt_server.telemetry.get_persistence`` without reaching into the
    server module.  The deferred import avoids a circular dependency at import
    time (server.py already imports from telemetry at the top level).

    Returns ``None`` if the server module hasn't finished bootstrapping yet
    (early startup) or if the Persistence singleton is unbound (the
    ``TRID3NT_DEV_PERSISTENCE=0`` no-persistence path).
    """
    try:
        from .server import get_persistence as _server_get_persistence
        return _server_get_persistence()
    except Exception:  # noqa: BLE001
        return None


def _get_telemetry_path() -> str:
    """Return the JSONL WRITE path: the legacy exact-file override, or the

    current boot's segment inside the (env-overridable) telemetry directory.
    """
    raw = os.environ.get("TRID3NT_TELEMETRY_PATH")
    if raw and _is_explicit_file_override(raw):
        return raw
    return os.path.join(
        _telemetry_dir(), f"{_TELEMETRY_BASENAME}.{_telemetry_boot_id()}.jsonl"
    )


def telemetry_read_paths(*, all_segments: bool = False) -> list[str]:
    """Resolve the JSONL file(s) a READER should consult.

    Legacy explicit-file override: always exactly that one path (a single
    file has no segments; ``all_segments`` is a no-op). Directory mode:
    the CURRENT boot's segment by default, or every retained segment
    (oldest-first) when ``all_segments=True``.
    """
    raw = os.environ.get("TRID3NT_TELEMETRY_PATH")
    if raw and _is_explicit_file_override(raw):
        return [raw]
    if not all_segments:
        return [_get_telemetry_path()]
    segments = _list_telemetry_segments()
    return segments if segments else [_get_telemetry_path()]


def cleanup_telemetry_segments(keep: int | None = None) -> list[str]:
    """Daemon-boot retention pass: delete segments beyond the last ``keep``.

    Ephemerality is POLICY, enforced HERE (call this once at daemon boot --
    ``main.run()`` does), not a platform accident. No-op (returns ``[]``) in
    legacy explicit-file mode (nothing to prune) or when fewer than ``keep``
    segments exist yet. ``keep`` defaults to ``TRID3NT_TELEMETRY_KEEP`` (3).
    Best-effort: a failure removing one segment is logged and skipped --
    retention must never raise or block boot.
    """
    raw = os.environ.get("TRID3NT_TELEMETRY_PATH")
    if raw and _is_explicit_file_override(raw):
        return []
    if keep is None:
        keep = _telemetry_keep()
    segments = _list_telemetry_segments()
    if len(segments) <= keep:
        return []
    stale = segments[: len(segments) - keep]
    removed: list[str] = []
    for seg in stale:
        try:
            os.remove(seg)
            removed.append(seg)
        except OSError:
            logger.warning(
                "telemetry retention: failed to remove segment=%s", seg, exc_info=True
            )
    if removed:
        logger.info(
            "telemetry retention: removed %d stale segment(s) (keep=%d): %s",
            len(removed),
            keep,
            removed,
        )
    return removed


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

    Never raises -- any I/O error is logged at WARNING.
    """
    line = json.dumps(record, default=str) + "\n"
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
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
    except Exception:  # noqa: BLE001 -- telemetry must never break the call loop
        logger.warning(
            "telemetry write failed path=%s tool=%s",
            path,
            record.get("tool_name", "?"),
            exc_info=True,
        )


def _blocking_append(path: str, line: str) -> None:
    """Blocking file append -- called from an executor thread only."""
    with open(path, mode="a", encoding="utf-8") as fh:
        fh.write(line)


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
    does NOT await completion -- latency impact on the tool-dispatch loop is
    bounded by the time to enqueue the task (microseconds), not the actual
    I/O.

    Sink: the record is ALWAYS written to the local-file JSONL path
    (``TRID3NT_TELEMETRY_PATH`` or the default
    ``/tmp/trid3nt_tool_call_telemetry.jsonl``), regardless of whether the
    app-level ``Persistence`` singleton is bound. The former MongoDB-mirror
    route was cut -- telemetry is JSONL-only.

    Args:
        session_id: WebSocket session identifier (ULID string).
        ts: ISO-8601 UTC timestamp of the tool call start (e.g.
            ``"2026-06-09T12:34:56.789Z"``).  Callers should pass
            ``trid3nt_contracts.now_utc().isoformat()`` or equivalent.
        tool_name: Registered tool name (e.g. ``"fetch_dem"``).
        source: Where the call originated.
            - ``"llm"`` -- Gemini-initiated ``function_call`` in the multi-turn
              loop (``_stream_model_reply``).
            - ``"workflow"`` -- inside-composer dispatch (reserved for future
              workflow orchestration paths).
            - ``"manual"`` -- ``/invoke`` directive from the debug harness or
              a test fixture.
        args_hash: Hex digest of SHA-256 over the JSON-serialized args dict.
            Use ``telemetry.compute_args_hash(args)`` to build this.
        success: ``True`` when the tool returned without raising; ``False``
            when ``dispatch_error`` was set in the call loop.
        latency_ms: Wall-clock elapsed time from dispatch to result, in
            milliseconds (float precision).
        error_code: typed error code string when ``success=False``;
            ``None`` on success or when unavailable.
        retry_attempt: Zero-based retry counter.  ``0`` for the first (or
            only) attempt; ``1`` for the first retry, etc.
        cached_content_token_count: Gemini ``UsageMetadata.
            cached_content_token_count`` from the response that triggered
            this call.  ``None`` when the field is absent or the stream did
            not report usage metadata (e.g. mid-stream chunks).
        result_usable: Whether the call produced a USABLE result, distinct
            from ``success`` (tool-accuracy panel). ``False``
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
    # JSONL-only sink: written unconditionally (the Persistence-mirror route
    # was cut). Fire-and-forget -- the event loop schedules the write; we do
    # not await it.
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
        # turn_id (pipeline id) -- recall@k join key (tool-retrieval shadow).
        # Omitted (absent, not null) when the caller did not supply it so old
        # readers + records stay byte-compatible.
        **({"turn_id": turn_id} if turn_id is not None else {}),
    }
    path = _get_telemetry_path()
    # Fire-and-forget: the event loop schedules the write; we do not await it.
    asyncio.ensure_future(_write_line(path, record))


def compute_args_hash(args: dict | None) -> str:
    """Public helper -- compute the SHA-256 hex digest for a tool's args dict.

    Callers in ``server.py`` should use this rather than re-implementing the
    digest logic.  Safe to call from sync contexts (no I/O).
    """
    return _hash_args(args)


def load_tool_call_records(
    path: str | None = None,
    *,
    limit: int | None = None,
    newest_first: bool = True,
    all_segments: bool = False,
) -> list[dict]:
    """Read per-tool-call rows from the JSONL sink (the product telemetry file).

    The shared reader for consumers that used to query the ``tool_call_telemetry``
    Persistence collection (search-tool co-occurrence / hot-set ranking): now that
    telemetry is JSONL-only, they read this file instead.

    Tolerant reader: a missing / unreadable file or a malformed line yields what
    could be read (never raises -- a missing/unreadable target is skipped, not
    fatal). Tool-retrieval SHADOW rows (``record_type == SHADOW_RECORD_TYPE``)
    share this sink and are EXCLUDED -- only per-tool-call rows (which carry no
    ``record_type``) are returned.

    ``path`` (explicit) wins over everything -- read exactly that one file
    (unchanged legacy behavior). Otherwise reads the CURRENT session/boot
    segment by default (``all_segments=False``); pass ``all_segments=True`` to
    read every retained segment (deliberate retention -- item 2). Segment
    files are read oldest-first so the overall row order (pre-sort) matches
    the single-file append order.

    With ``limit`` set, only the last ``limit`` tool-call rows are kept; with
    ``newest_first`` (default) the result is returned newest-first so a
    session-cap consumer sees recent sessions first (mirrors the old
    ``find ... sort {_id: -1}`` query the Mongo path issued).
    """
    targets = [path] if path is not None else telemetry_read_paths(all_segments=all_segments)
    out: list[dict] = []
    for target in targets:
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
                    if not isinstance(rec, dict):
                        continue
                    if rec.get("record_type") == SHADOW_RECORD_TYPE:
                        continue
                    out.append(rec)
        except OSError:
            continue
    if limit is not None and len(out) > limit:
        out = out[-limit:]
    if newest_first:
        out.reverse()
    return out


# --------------------------------------------------------------------------- #
# Tool-retrieval SHADOW telemetry (tool-retrieval kickoff -- orchestrator half).
#
# Shadow mode computes the WOULD-BE-visible tool set per turn via
# ``retrieve_visible_tools`` WITHOUT changing the catalog the model actually
# sees (the model still sees the full registry). We log that would-be set so a
# recall@k measurement (catalog_http.build_telemetry_summary) can compare
# it against the tools the LLM actually dispatched that turn, per solver
# flow. recall = |dispatched-llm-tools that WERE in the retrieved set| /
# |dispatched-llm-tools|.
#
# Same JSONL-only sink as ``emit_tool_call_event``: written to the SAME
# ``tool_calls.jsonl`` file, carrying a ``record_type="tool_retrieval_shadow"``
# discriminator so a reader can split these rows from the per-tool ``tool_call``
# rows that share the file. Fire-and-forget; NEVER raises -- telemetry must
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
    """Build the per-turn shadow-selection record (pure -- no I/O).

    Split out so tests can assert the record SHAPE without touching the sink.
    ``visible_tools`` is the would-be-visible set ``retrieve_visible_tools``
    returned for this turn; ``turn_id`` is the per-user-message dispatch id (the
    ``pipeline_id``) so recall@k can join a dispatched llm tool to ITS turn's set.

    ``user_text`` is truncated to keep the record bounded; the full text is not
    needed for recall (the join key is ``turn_id``).
    """
    try:
        visible_sorted = sorted({str(t) for t in (visible_tools or [])})
    except Exception:  # noqa: BLE001 -- defensive; never break the dispatch loop
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
    is scheduled as an asyncio task and the caller does not await it. JSONL-only
    sink -- the SAME ``tool_calls.jsonl`` file as the per-tool path, carrying the
    ``record_type`` discriminator so a reader can split shadow rows from tool-call
    rows. (The former MongoDB-mirror route was cut.)
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
    except Exception:  # noqa: BLE001 -- telemetry must never break the dispatch loop
        logger.warning("shadow telemetry: record build failed", exc_info=True)
        return

    # JSONL-only sink (same file as the per-tool path; the record_type
    # discriminator separates shadow rows from tool-call rows).
    path = _get_telemetry_path()
    try:
        asyncio.ensure_future(_write_line(path, record))
    except Exception:  # noqa: BLE001 -- telemetry must never break the dispatch loop
        logger.warning("shadow telemetry: file schedule failed", exc_info=True)


def now_iso_utc() -> str:
    """ISO-8601 UTC timestamp (millisecond precision, trailing Z)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# --------------------------------------------------------------------------- #
# Solve-time telemetry (SFINCS per-job autoscale)
#
# At solve completion we accumulate real (active_cells, vCPU, wall_clock) data
# so the adaptive-grid cell cap can be re-tuned from logged measurements
# (measure-then-tune). Emitted to the SAME sink discipline as tool_call
# telemetry: a structured logger line ALWAYS (so it lands in the agent log /
# routing dashboard scrape even when the JSONL sink is unwritable) PLUS the
# JSONL record. Not MCP-routed (no Mongo collection contract exists for it
# yet); the local JSONL + structured log line is the minimum needed -- a
# Mongo collection can be added later without changing call sites.
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
    """Build the structured solve-telemetry record (pure -- no I/O).

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
    contract -- telemetry must never break the solve loop.
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
    except Exception:  # noqa: BLE001 -- telemetry must never break the solve loop
        solve_logger.warning(
            "solve_telemetry JSONL write failed run_id=%s", run_id, exc_info=True
        )
    return record


# --------------------------------------------------------------------------- #
# SOLVE completion telemetry -- Batch instance + problem size + timing
#
# A richer sibling to ``emit_solve_telemetry`` (above): where that record
# carries the autoscale provenance for re-tuning the adaptive cell cap, this
# one folds in the real AWS Batch compute the solve ran on -- the Spot
# instance type + lifecycle + AZ + the queue-provision / compute / total
# timing breakdown (``solver._capture_batch_compute_meta``) merged with the
# mesh size descriptor (active_cell_count + resolution_m) -- so a perf model
# can later infer completion time from real (instance, problem-size,
# wall-clock) measurements.
#
# Same sink discipline as the per-tool + autoscale telemetry: a structured
# INFO line ALWAYS (scrape-able out of the agent log even when the JSONL
# path is unwritable) PLUS a JSONL append, both wrapped so a sink failure
# never breaks the solve. Carries a ``record_type="solve"`` discriminator so
# a reader can distinguish these rows from the per-tool ``tool_call`` rows
# that share the accumulation sink. Not MCP-routed yet (no Mongo collection
# contract for it); the JSONL + structured log is the minimum, mirroring
# ``emit_solve_telemetry``.
# --------------------------------------------------------------------------- #

#: Dedicated structured logger so a log scrape can grep these rows out of the
#: agent log even when the JSONL file path is unwritable (mirrors solve_logger).
solve_meta_logger = logging.getLogger("trid3nt_server.solve_telemetry")


def record_solve_telemetry(record: dict) -> dict:
    """Write ONE SOLVE-completion telemetry record (structured log + JSONL).

    The record is built by the composer (see
    ``model_flood_scenario`` / ``model_swmm_urban_flood``) by MERGING the Batch
    compute meta (``solver._capture_batch_compute_meta`` -- instance + timing) with
    the mesh size descriptor + solver + terminal status + run/case/session ids.
    This writer stamps a ``record_type="solve"`` discriminator and a ``ts`` when
    absent, then emits to the SAME accumulation sink (JSONL at
    ``TRID3NT_SOLVE_TELEMETRY_PATH`` / the default) the autoscale solve telemetry
    uses, plus an ALWAYS-on structured INFO line.

    Record shape (the keys a complete row carries -- every field is optional so a
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
    contract -- telemetry must NEVER break the solve path. Returns the stamped
    record (so the composer can fold it into provenance / a test can assert it).
    """
    try:
        rec = dict(record or {})
    except Exception:  # noqa: BLE001 -- defensive; never break the solve
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
    except Exception:  # noqa: BLE001 -- telemetry must never break the solve loop
        solve_meta_logger.warning(
            "solve_record JSONL write failed run_id=%s",
            rec.get("run_id"),
            exc_info=True,
        )
    return rec


# --------------------------------------------------------------------------- #
# PER-TURN telemetry.
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

    Shape (the SHARED WIRE CONTRACT, tool-accuracy panel)::

        {run_id, solver, grid_resolution_m, active_cell_count, vcpus,
         elapsed_seconds, eta_seconds|null}

    Emitted on the running tool/pipeline card during a solve so the user sees
    grid resolution / active-cell count / vCPU / elapsed / ETA tick on the live
    card (rather than a silent multi-minute spinner). ``eta_seconds`` comes from
    the perf model (the autoscale ``estimated_solve_seconds``) when available,
    else ``None``. Reuses the solve-telemetry field names so the live
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
    "load_tool_call_records",
    "telemetry_read_paths",
    "cleanup_telemetry_segments",
    "emit_solve_telemetry",
    "build_solve_telemetry_record",
    "record_solve_telemetry",
    "build_live_solve_progress",
    # per-turn telemetry
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
