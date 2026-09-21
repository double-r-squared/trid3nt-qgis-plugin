"""Tool-call telemetry: the sink, its one reader, and the summary over it.

One JSON line per tool call, turn, shadow selection or solve completion, to a
local JSONL sink written unconditionally. Every emitter here is fire-and-forget
and NEVER raises: telemetry must not break the dispatch, turn or solve loop. The
reading half aggregates those rows into the routing-quality summary the door
serves, so a writer and a dashboard never disagree about where the sink lives."""

from __future__ import annotations

import asyncio
import glob
import hashlib
import json
import logging
import os
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from .persistence import Persistence

logger = logging.getLogger("trid3nt_server.telemetry")

_DEFAULT_TELEMETRY_PATH = "/tmp/trid3nt_tool_call_telemetry.jsonl"

# Telemetry retention. Ephemerality is POLICY, enforced at daemon boot by
# ``cleanup_telemetry_segments``.
#
# A ``TRID3NT_TELEMETRY_PATH`` ending in ``.jsonl`` is an EXACT single-file
# override: unsegmented, never pruned. Unset, or set to a directory, the sink is
# DIRECTORY-mode - one segment per daemon boot named
# ``tool_calls.<boot_id>.jsonl`` - so a long-lived or crash-looped daemon never
# re-grows one unbounded file.

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
    """Every retained segment file in directory mode, oldest first; the boot-id
    prefix is a UTC timestamp, so lexical order is chronological.
    """
    pattern = os.path.join(_telemetry_dir(), f"{_TELEMETRY_BASENAME}.*.jsonl")
    return sorted(glob.glob(pattern))


def get_persistence() -> "Persistence | None":
    """The bound Persistence singleton, or ``None`` before the server module has
    bootstrapped or when persistence is disabled. The import is deferred to
    break the import cycle."""
    try:
        from .server import get_persistence as _server_get_persistence
        return _server_get_persistence()
    except Exception:  # noqa: BLE001
        return None


def _get_telemetry_path() -> str:
    """The JSONL WRITE path: the exact-file override, else this boot's segment
    inside the telemetry directory.
    """
    raw = os.environ.get("TRID3NT_TELEMETRY_PATH")
    if raw and _is_explicit_file_override(raw):
        return raw
    return os.path.join(
        _telemetry_dir(), f"{_TELEMETRY_BASENAME}.{_telemetry_boot_id()}.jsonl"
    )


def telemetry_read_paths(*, all_segments: bool = False) -> list[str]:
    """Resolve the JSONL file(s) a READER should consult: the explicit-file
    override alone, else the current boot's segment, or every retained segment
    when ``all_segments`` is set."""
    raw = os.environ.get("TRID3NT_TELEMETRY_PATH")
    if raw and _is_explicit_file_override(raw):
        return [raw]
    if not all_segments:
        return [_get_telemetry_path()]
    segments = _list_telemetry_segments()
    return segments if segments else [_get_telemetry_path()]


def cleanup_telemetry_segments(keep: int | None = None) -> list[str]:
    """Delete segments beyond the last ``keep`` (default
    ``TRID3NT_TELEMETRY_KEEP``), returning what was removed; a no-op in
    explicit-file mode, best-effort per segment, and it never raises."""
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


def read_jsonl_records(
    targets: str | Path | Sequence[str | Path],
    *,
    keep: str | None = None,
    drop: str | None = None,
) -> list[dict]:
    """Every JSON object on one JSONL sink, or on a list of retained segments,
    in file order - narrowed to one ``record_type`` by ``keep`` or with one
    excluded by ``drop``. A missing target, an unreadable file and a malformed
    line are each skipped: a best-effort sink is read for what it holds."""
    paths = [targets] if isinstance(targets, (str, Path)) else list(targets)
    out: list[dict] = []
    for target in paths:
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
                    rec_type = rec.get("record_type")
                    if keep is not None and rec_type != keep:
                        continue
                    if drop is not None and rec_type == drop:
                        continue
                    out.append(rec)
        except OSError:
            continue
    return out


def _hash_args(args: dict | None) -> str:
    """The SHA-256 hex digest of the JSON-serialized args, a stable fingerprint
    that keeps the full args blob out of the log; ``None`` hashes as ``{}``.
    """
    payload = json.dumps(args or {}, sort_keys=True, default=str).encode()
    return hashlib.sha256(payload).hexdigest()


async def _write_line(path: str, record: dict) -> None:
    """Append one JSON line to ``path``, through ``aiofiles`` when it is
    installed and an executor thread otherwise, so no write blocks the loop.
    Never raises: an I/O error is logged at WARNING."""
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
    """Emit one tool-call telemetry record: the write is scheduled as a
    fire-and-forget task, so the dispatch loop pays only the enqueue, and a
    failure is logged rather than raised."""
    # ``result_usable`` is NOT ``success``: a layer-producing tool can return
    # without raising and still carry no renderable layer. ``routed_ok`` is a
    # heuristic, never ground truth - False marks a call superseded within the
    # same session by a different tool for the same logical step.
    # Written unconditionally, whether or not Persistence is bound.
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
    """The SHA-256 hex digest of a tool's args dict; safe from sync contexts."""
    return _hash_args(args)


def load_tool_call_records(
    path: str | None = None,
    *,
    limit: int | None = None,
    newest_first: bool = True,
    all_segments: bool = False,
) -> list[dict]:
    """Read per-tool-call rows from the JSONL sink, newest-first by default and
    tolerant of a missing file or a malformed line. Shadow rows are EXCLUDED; an
    explicit ``path`` wins, else the current segment, or all retained segments."""
    targets = [path] if path is not None else telemetry_read_paths(
        all_segments=all_segments)
    out = read_jsonl_records(targets, drop=SHADOW_RECORD_TYPE)
    if limit is not None and len(out) > limit:
        out = out[-limit:]
    if newest_first:
        out.reverse()
    return out


# Tool-retrieval SHADOW telemetry.
#
# Shadow mode computes the WOULD-BE-visible tool set per turn without changing
# the catalog the model actually sees. Logging that set lets a recall@k
# measurement compare it against the tools the model really dispatched:
# recall = |dispatched tools that were in the retrieved set| / |dispatched|.
# The rows share the tool-call JSONL sink and carry a
# ``record_type="tool_retrieval_shadow"`` discriminator so a reader can split
# them out. Fire-and-forget; never raises.

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
    """Build the per-turn shadow-selection record (pure, no I/O); ``turn_id`` is
    the join key recall@k needs, and ``user_text`` is truncated because the full
    text is not part of that measurement."""
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
    """Emit one tool-retrieval shadow-selection record; fire-and-forget, never
    raises, and shares the tool-call sink under its ``record_type``.
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


# Solve-time telemetry: per-job autoscale measurements.
#
# At solve completion the real (active_cells, vCPU, wall_clock) triple is
# accumulated so the adaptive-grid cell cap can be re-tuned from measurements
# rather than guesses. A structured logger line fires ALWAYS, so the row lands
# in the agent log even when the JSONL sink is unwritable.

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
    """Build the structured solve-telemetry record, pure and testable."""
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
    """Emit one solve-completion record: an INFO line always, the JSONL append
    best-effort, and the record returned so provenance can fold it in. Never
    raises into the solve path."""
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


# SOLVE completion telemetry: compute meta, problem size and timing.
#
# A richer sibling to ``emit_solve_telemetry``: where that record carries the
# autoscale provenance, this one folds the compute the solve actually ran on
# together with the mesh size, so a perf model can later infer completion time
# from real measurements. A structured INFO line always fires alongside the
# JSONL append, and a ``record_type="solve"`` discriminator separates these rows
# from the per-tool rows that share the sink.

#: Dedicated structured logger so a log scrape can grep these rows out of the
#: agent log even when the JSONL file path is unwritable (mirrors solve_logger).
solve_meta_logger = logging.getLogger("trid3nt_server.solve_telemetry")


def record_solve_telemetry(record: dict) -> dict:
    """Write ONE solve-completion record: the caller supplies the merged
    compute and mesh-size fields, this writer stamps ``record_type="solve"`` and
    a ``ts`` when absent, logs an INFO line, and never raises."""
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


# PER-TURN telemetry.
#
# One record per user-message turn on its own JSONL sink plus an always-on
# structured INFO line. Token counts are SUMMED across the turn's model rounds
# from the adapter's usage events; ``reasoning_tokens`` is recorded only where
# the provider reports it and is null otherwise, NEVER fabricated.
# ``error_class`` is null on a clean turn, ``"upstream_provider"`` when the turn
# died on a transient provider failure after retries (an upstream failure is
# never internalized as ours), ``"provider_request"`` for a non-transient
# rejection, ``"internal"`` for our own bugs, and ``"cancelled"`` /
# ``"context_window"`` / ``"client_disconnect"`` for those turn endings.

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
    """Build one per-turn telemetry record, pure and testable."""
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
    """Emit ONE per-turn record: a structured INFO line always fires, the JSONL
    append is scheduled and not awaited, and nothing raises. Returns the record,
    or ``None`` when even the build failed."""
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
    """Read per-turn records from the JSONL sink in file order, keeping the last
    ``max_records``; a missing file or a malformed line yields what could be
    read rather than raising."""
    out = read_jsonl_records(path or _get_turn_telemetry_path(),
                             keep=TURN_RECORD_TYPE)
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
    """Aggregate per-turn records into the telemetry-summary section, one entry
    per model sorted by turn count; a mean covers only the turns that REPORTED
    the figure, so an unreported count never drags a mean toward zero."""
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
    """Build the LIVE solve-progress payload (pure, no I/O); ``eta_seconds`` is
    the perf model's estimate when one exists and ``None`` otherwise, and the
    field names match the at-completion record deliberately."""
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



#: Terminal solver tools -> the flow they identify. A turn is attributed to a
#: flow when it dispatched one of these, which drives the recall@k per-flow
#: breakdown. This is RETRIEVAL BENCH DATA, not a run surface: the names are
#: template names because what is being measured is whether a phrasing reaches
#: the right template. Keys must be registered engine templates; a key that no
#: longer registers reports a permanently empty flow.
_FLOW_BY_SOLVER_TOOL: dict[str, str] = {
    "telemac_dye_release": "river-plume",
    "telemac_oil_spill": "river-oil-slick",
    "telemac_bed_scour": "river-mobile-bed",
    "telemac_sediment_plume": "river-sediment-plume",
    "telemac_channel_dredging": "river-dredging",
    "telemac_do_sag": "oxygen-sag",
    "telemac_rain_on_grid": "rainfall-runoff",
    "telemac3d_stratified_flow": "stratified-flow",
    "artemis_harbor_agitation": "harbor-agitation",
}
# The flow label is a REPORTING name and no tool metadata carries one, so this
# map is its only home. The tier=template members deliberately absent from it -
# telemac_eutrophication, telemac_ice_cover, telemac_micropollutant_release,
# telemac_water_temperature, tomawac_nearshore_waves,
# tomawac_wave_driven_currents - are not reported per flow.

#: Flow order in the per-flow breakdown. Derived so a flow can never be reported
#: without a tool that produces it, nor a tool added without its flow appearing.
_FLOWS: tuple[str, ...] = tuple(dict.fromkeys(_FLOW_BY_SOLVER_TOOL.values()))


def _normalize_record(rec: dict[str, Any]) -> dict[str, Any]:
    """Coerce a single telemetry record into the summary's canonical shape,
    accepting either the ``success``/``ts`` or the ``result_ok``/``called_at_utc``
    spelling, so the summary builder does not care which writer produced it."""
    out: dict[str, Any] = {}
    out["session_id"] = rec.get("session_id") or ""
    out["tool_name"] = rec.get("tool_name") or ""
    out["source"] = rec.get("source") or "llm"
    # Either spelling of the outcome flag.
    if "result_ok" in rec:
        out["result_ok"] = bool(rec.get("result_ok"))
    else:
        out["result_ok"] = bool(rec.get("success", True))
    out["latency_ms"] = float(rec.get("latency_ms") or 0.0)
    out["error_code"] = rec.get("error_code")
    out["retry_attempt"] = int(rec.get("retry_attempt") or 0)
    out["cached_content_token_count"] = rec.get("cached_content_token_count")
    # ``result_usable`` is None when the notion does not apply, as for a meta
    # tool; ``routed_ok`` carries the routing-quality heuristic per record.
    out["result_usable"] = rec.get("result_usable")
    out["routed_ok"] = rec.get("routed_ok")
    # Timestamp under either spelling.
    out["called_at_utc"] = rec.get("called_at_utc") or rec.get("ts") or ""
    # None on a record written before the model dimension existed; the
    # aggregator buckets that as "unknown".
    out["model_id"] = rec.get("model_id")
    # The per-turn dispatch id is the recall@k join key against that turn's
    # shadow row, and recall counts only dispatches that carry one.
    out["turn_id"] = rec.get("turn_id")
    return out


def _empty_solve_telemetry() -> dict[str, Any]:
    """The zero-state solve-telemetry section: an empty ``recent`` list and zero
    percentiles until at least one solve has been logged."""
    return {
        "recent": [],
        "wall_clock_p50_s": 0.0,
        "wall_clock_p95_s": 0.0,
    }


def _empty_summary() -> dict[str, Any]:
    """Return the zero-state summary shape (no telemetry recorded yet)."""
    return {
        "total_dispatches": 0,
        "session_count": 0,
        "error_rate_overall": 0.0,
        "cache_hit_rate": 0.0,
        "average_latency_ms": 0.0,
        # Tool-accuracy fields.
        "success_rate": 0.0,
        "result_usability_rate": None,
        "routing_accuracy_rate": None,
        "latency_p50_ms": 0.0,
        "latency_p95_ms": 0.0,
        "dispatches_by_tool": [],   # [{name, count, error_rate, avg_latency_ms, ...}]
        "dispatches_by_source": {}, # {llm: int, workflow: int, manual: int}
        "error_rate_by_tool": [],   # [{name, error_rate, error_count, total}]
        "top_routing_chains": [],   # [{chain: [a, b], count}]
        "by_model": [],             # [{model_id, count, success_rate, ...}]
        "solve_telemetry": _empty_solve_telemetry(),
        # tool-retrieval shadow recall@k (folded in by build_telemetry_summary).
        "recall_at_k": _empty_recall_at_k(),
        "source": "empty",
    }


def _percentile(values: list[float], q: float) -> float:
    """The ``q``-th percentile, q in [0,1], by linear interpolation; empty input
    yields ``0.0``. Pure stdlib, matching numpy's default method so an external
    recompute agrees."""
    if not values:
        return 0.0
    ordered = sorted(values)
    n = len(ordered)
    if n == 1:
        return float(ordered[0])
    pos = q * (n - 1)
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return float(ordered[lo] + (ordered[hi] - ordered[lo]) * frac)


def _rate_over_bools(values: list[bool | None]) -> float | None:
    """Fraction of ``True`` among the non-``None`` entries, or ``None`` when
    every entry is ``None``, so a rate the data cannot support is an honest null
    rather than a misleading zero."""
    considered = [v for v in values if v is not None]
    if not considered:
        return None
    trues = sum(1 for v in considered if v)
    return trues / len(considered)


def _derive_routed_ok(records: list[dict[str, Any]]) -> dict[int, bool]:
    """Derive the routing-quality heuristic per record, keyed by ``id(rec)`` so
    two identical records score independently. A value the writer already
    supplied wins; this only fills the gap where it left one None."""
    # HEURISTIC, NOT GROUND TRUTH: a call counts as mis-routed when it FAILED
    # and the same session's NEXT call, by timestamp, is a DIFFERENT tool - the
    # model abandoned it and reached for another for the same logical step. Any
    # other completed call is routed_ok.
    out: dict[int, bool] = {}
    sess_buckets: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        sid = r.get("session_id") or ""
        if not sid:
            # No session context -- cannot judge supersession; routed_ok stays
            # absent (treated as None/unavailable downstream).
            continue
        sess_buckets.setdefault(sid, []).append(r)
    for recs in sess_buckets.values():
        recs_sorted = sorted(recs, key=lambda r: str(r.get("called_at_utc") or ""))
        for i, rec in enumerate(recs_sorted):
            preset = rec.get("routed_ok")
            if preset is not None:
                out[id(rec)] = bool(preset)
                continue
            tool = rec.get("tool_name") or ""
            failed = not rec.get("result_ok", True)
            superseded = False
            if i + 1 < len(recs_sorted):
                nxt = recs_sorted[i + 1]
                ntool = nxt.get("tool_name") or ""
                if ntool and tool and ntool != tool:
                    superseded = True
            out[id(rec)] = not (failed and superseded)
    return out


def _aggregate_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute the dashboard summary over normalized records, as a
    JSON-serializable dict; every read path funnels through here, so the
    aggregation lives in one place."""
    if not records:
        return _empty_summary()

    total = len(records)
    # Sessions present
    sessions = {r["session_id"] for r in records if r["session_id"]}
    session_count = len(sessions)

    # Routing-quality heuristic (per-record, id()-keyed). Derived here because
    # supersession is a same-session ADJACENT-chain signal, not knowable at
    # single-call emit time.
    routed_ok_by_id = _derive_routed_ok(records)

    # Per-tool aggregation
    by_tool_count: dict[str, int] = {}
    by_tool_errors: dict[str, int] = {}
    by_tool_latency_sum: dict[str, float] = {}
    by_tool_latencies: dict[str, list[float]] = {}
    by_tool_usable: dict[str, list[bool | None]] = {}
    by_tool_routed: dict[str, list[bool | None]] = {}
    by_source_count: dict[str, int] = {}
    # Per-model aggregation (in-chat model selector dimension).
    by_model_count: dict[str, int] = {}
    by_model_errors: dict[str, int] = {}
    by_model_latency_sum: dict[str, float] = {}
    by_model_latencies: dict[str, list[float]] = {}
    by_model_usable: dict[str, list[bool | None]] = {}
    by_model_routed: dict[str, list[bool | None]] = {}
    total_errors = 0
    total_latency = 0.0
    all_latencies: list[float] = []
    all_usable: list[bool | None] = []
    all_routed: list[bool | None] = []
    cache_hit_count = 0
    cache_total = 0

    for r in records:
        tool = r["tool_name"] or "unknown"
        lat = float(r["latency_ms"])
        by_tool_count[tool] = by_tool_count.get(tool, 0) + 1
        by_tool_latency_sum[tool] = by_tool_latency_sum.get(tool, 0.0) + lat
        by_tool_latencies.setdefault(tool, []).append(lat)
        if not r["result_ok"]:
            by_tool_errors[tool] = by_tool_errors.get(tool, 0) + 1
            total_errors += 1
        total_latency += lat
        all_latencies.append(lat)
        # result_usable (bool|None -- meta tools contribute None).
        usable = r.get("result_usable")
        by_tool_usable.setdefault(tool, []).append(usable)
        all_usable.append(usable)
        # routed_ok (the derived heuristic; None when no session context).
        routed = routed_ok_by_id.get(id(r))
        by_tool_routed.setdefault(tool, []).append(routed)
        all_routed.append(routed)
        src = r["source"] or "llm"
        by_source_count[src] = by_source_count.get(src, 0) + 1
        # Cache hit rate: a present cached-token count means the provider
        # reported a cached-content path, so the call counts as a hit.
        cct = r.get("cached_content_token_count")
        if cct is not None:
            cache_total += 1
            if isinstance(cct, (int, float)) and cct > 0:
                cache_hit_count += 1
        # Per-model accumulation (in-chat model selector dimension).
        # Null/missing model_id is bucketed as "unknown" so legacy records
        # still surface in the by_model section.
        mid = r.get("model_id") or "unknown"
        by_model_count[mid] = by_model_count.get(mid, 0) + 1
        by_model_latency_sum[mid] = by_model_latency_sum.get(mid, 0.0) + lat
        by_model_latencies.setdefault(mid, []).append(lat)
        if not r["result_ok"]:
            by_model_errors[mid] = by_model_errors.get(mid, 0) + 1
        by_model_usable.setdefault(mid, []).append(usable)
        by_model_routed.setdefault(mid, []).append(routed)

    by_tool_sorted: list[dict[str, Any]] = []
    error_rate_by_tool: list[dict[str, Any]] = []
    for tool, cnt in sorted(by_tool_count.items(), key=lambda kv: (-kv[1], kv[0])):
        errs = by_tool_errors.get(tool, 0)
        avg_latency = by_tool_latency_sum.get(tool, 0.0) / cnt if cnt else 0.0
        rate = (errs / cnt) if cnt else 0.0
        lats = by_tool_latencies.get(tool, [])
        usability_rate = _rate_over_bools(by_tool_usable.get(tool, []))
        routing_rate = _rate_over_bools(by_tool_routed.get(tool, []))
        by_tool_sorted.append(
            {
                "name": tool,
                "count": cnt,
                "error_count": errs,
                "error_rate": round(rate, 4),
                "avg_latency_ms": round(avg_latency, 2),
                # Tool-accuracy fields.
                "success_rate": round(1.0 - rate, 4),
                "result_usability_rate": (
                    round(usability_rate, 4) if usability_rate is not None else None
                ),
                "routing_accuracy_rate": (
                    round(routing_rate, 4) if routing_rate is not None else None
                ),
                "latency_p50_ms": round(_percentile(lats, 0.50), 2),
                "latency_p95_ms": round(_percentile(lats, 0.95), 2),
            }
        )
        error_rate_by_tool.append(
            {
                "name": tool,
                "error_rate": round(rate, 4),
                "error_count": errs,
                "total": cnt,
            }
        )

    # Routing chains: most common 2-tool sequences within a single session.
    # Group records by session_id then by their called_at_utc to walk pairs.
    chains: dict[tuple[str, str], int] = {}
    sess_buckets: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        sid = r["session_id"]
        if not sid:
            continue
        sess_buckets.setdefault(sid, []).append(r)
    for sid, recs in sess_buckets.items():
        # Sort by timestamp (ISO strings sort lexicographically when in UTC Z).
        recs_sorted = sorted(recs, key=lambda r: str(r.get("called_at_utc") or ""))
        for a, b in zip(recs_sorted[:-1], recs_sorted[1:]):
            ta = a.get("tool_name") or ""
            tb = b.get("tool_name") or ""
            if not ta or not tb or ta == tb:
                continue
            chains[(ta, tb)] = chains.get((ta, tb), 0) + 1
    top_chains = sorted(chains.items(), key=lambda kv: -kv[1])[:5]
    chains_out = [
        {"chain": [a, b], "count": cnt} for (a, b), cnt in top_chains
    ]

    error_rate_overall = (total_errors / total) if total else 0.0
    cache_hit_rate = (cache_hit_count / cache_total) if cache_total else 0.0
    avg_latency_ms = (total_latency / total) if total else 0.0
    success_rate = (1.0 - error_rate_overall) if total else 0.0
    usability_rate_overall = _rate_over_bools(all_usable)
    routing_rate_overall = _rate_over_bools(all_routed)

    # Per-model breakdown (in-chat model selector).
    # Shape: list of {model_id, count, success_rate, result_usability_rate,
    #                 routing_accuracy_rate, latency_p50_ms, latency_p95_ms}
    # Sorted descending by count; "unknown" last.
    by_model_sorted: list[dict[str, Any]] = []
    for mid, cnt in sorted(
        by_model_count.items(),
        key=lambda kv: (kv[0] == "unknown", -kv[1], kv[0]),
    ):
        m_errs = by_model_errors.get(mid, 0)
        m_rate = (m_errs / cnt) if cnt else 0.0
        m_lats = by_model_latencies.get(mid, [])
        m_usability = _rate_over_bools(by_model_usable.get(mid, []))
        m_routing = _rate_over_bools(by_model_routed.get(mid, []))
        by_model_sorted.append(
            {
                "model_id": mid,
                "count": cnt,
                "success_rate": round(1.0 - m_rate, 4),
                "result_usability_rate": (
                    round(m_usability, 4) if m_usability is not None else None
                ),
                "routing_accuracy_rate": (
                    round(m_routing, 4) if m_routing is not None else None
                ),
                "latency_p50_ms": round(_percentile(m_lats, 0.50), 2),
                "latency_p95_ms": round(_percentile(m_lats, 0.95), 2),
            }
        )

    return {
        "total_dispatches": total,
        "session_count": session_count,
        "error_rate_overall": round(error_rate_overall, 4),
        "cache_hit_rate": round(cache_hit_rate, 4),
        "average_latency_ms": round(avg_latency_ms, 2),
        # Tool-accuracy fields.
        "success_rate": round(success_rate, 4),
        "result_usability_rate": (
            round(usability_rate_overall, 4)
            if usability_rate_overall is not None
            else None
        ),
        "routing_accuracy_rate": (
            round(routing_rate_overall, 4)
            if routing_rate_overall is not None
            else None
        ),
        "latency_p50_ms": round(_percentile(all_latencies, 0.50), 2),
        "latency_p95_ms": round(_percentile(all_latencies, 0.95), 2),
        "dispatches_by_tool": by_tool_sorted,
        "dispatches_by_source": by_source_count,
        "error_rate_by_tool": error_rate_by_tool,
        "top_routing_chains": chains_out,
        # Model dimension (in-chat model selector).
        # The accuracy panel UI can compare success_rate / usability / routing
        # across model choices without a UI redesign in this job.
        "by_model": by_model_sorted,
        # solve_telemetry is folded in by build_telemetry_summary (it reads its
        # own JSONL/collection sink); seed the empty section so _aggregate_records
        # called standalone still emits the full contract shape.
        "solve_telemetry": _empty_solve_telemetry(),
        # recall_at_k is likewise folded in by build_telemetry_summary (it joins
        # the shadow rows against these dispatches); seed the empty section.
        "recall_at_k": _empty_recall_at_k(),
        "source": "telemetry",
    }


# The solve-telemetry section of the summary.
#
# The per-solve metrics - grid resolution, active cells, vCPU, wall clock,
# backend, AOI - are read from the JSONL the solve writer already maintains and
# folded into the summary.

#: How many recent solve records to surface in the ``recent`` array.
_SOLVE_RECENT_CAP = 20


def _aggregate_solve_telemetry(
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the solve-telemetry section: ``recent`` newest-first and capped,
    with percentiles over every record carrying a numeric wall clock. Empty
    input yields the zero-state section."""
    if not records:
        return _empty_solve_telemetry()
    # Newest-first by ts (ISO Z strings sort lexicographically).
    ordered = sorted(
        records, key=lambda r: str(r.get("ts") or ""), reverse=True
    )
    recent: list[dict[str, Any]] = []
    for rec in ordered[:_SOLVE_RECENT_CAP]:
        recent.append(
            {
                "run_id": rec.get("run_id"),
                "solver": rec.get("solver"),
                "grid_resolution_m": rec.get("grid_resolution_m"),
                "active_cell_count": rec.get("active_cell_count"),
                "vcpus": rec.get("vcpus"),
                "wall_clock_seconds": rec.get("wall_clock_seconds"),
                "backend": rec.get("backend"),
                "aoi_km2": rec.get("aoi_km2"),
            }
        )
    wall_clocks = [
        float(rec["wall_clock_seconds"])
        for rec in records
        if isinstance(rec.get("wall_clock_seconds"), (int, float))
        and not isinstance(rec.get("wall_clock_seconds"), bool)
    ]
    return {
        "recent": recent,
        "wall_clock_p50_s": round(_percentile(wall_clocks, 0.50), 2),
        "wall_clock_p95_s": round(_percentile(wall_clocks, 0.95), 2),
    }


def _normalize_shadow_record(rec: dict[str, Any]) -> dict[str, Any]:
    """Coerce a shadow row into the recall@k canonical shape, normalizing
    ``visible_tools`` to a set of strings."""
    vis = rec.get("visible_tools") or []
    try:
        visible = {str(t) for t in vis}
    except Exception:  # noqa: BLE001 -- a malformed row contributes an empty set
        visible = set()
    return {
        "session_id": rec.get("session_id") or "",
        "turn_id": rec.get("turn_id") or "",
        "visible_tools": visible,
        "k": rec.get("k"),
    }


def compute_recall_at_k(
    tool_records: list[dict[str, Any]],
    shadow_records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compute recall@k of the tool-retrieval shadow selection, PURE: per turn,
    the model-dispatched tools present in that turn's would-be-visible set over
    all its dispatched tools, so a dropped tool is a MISS."""
    # A turn with no shadow row is EXCLUDED: recall is defined only where a
    # would-be set was logged. The join key is (session_id, turn_id).
    # Index shadow rows by (session_id, turn_id) -> visible set.
    shadow_by_turn: dict[tuple[str, str], set[str]] = {}
    k_values: list[int] = []
    for s in shadow_records:
        norm = _normalize_shadow_record(s)
        sid = norm["session_id"]
        tid = norm["turn_id"]
        if not tid:
            continue
        # If one turn logged several shadow rows, the union is the safe choice:
        # over-inclusion never penalizes recall.
        key = (sid, tid)
        shadow_by_turn.setdefault(key, set()).update(norm["visible_tools"])
        kv = norm.get("k")
        if isinstance(kv, int):
            k_values.append(kv)

    if not shadow_by_turn:
        return {
            "overall": None,
            "turns_measured": 0,
            "dispatches_measured": 0,
            "hits": 0,
            "misses": 0,
            "k": None,
            "by_flow": [],
            "missed_tools": [],
        }

    # Group dispatched llm tools by (session_id, turn_id).
    dispatches_by_turn: dict[tuple[str, str], list[str]] = {}
    for r in tool_records:
        if (r.get("source") or "llm") != "llm":
            continue
        tid = r.get("turn_id")
        if not tid:
            continue
        sid = r.get("session_id") or ""
        tool = r.get("tool_name") or ""
        if not tool:
            continue
        dispatches_by_turn.setdefault((sid, tid), []).append(tool)

    # Determine each turn's solver flow from the terminal solver tool it
    # dispatched (if any). A turn maps to at most one flow.
    def _turn_flow(tools: list[str]) -> str | None:
        for t in tools:
            flow = _FLOW_BY_SOLVER_TOOL.get(t)
            if flow:
                return flow
        return None

    total_hits = 0
    total_misses = 0
    total_dispatches = 0
    turns_measured = 0
    # Per-flow accumulators.
    flow_hits: dict[str, int] = {}
    flow_misses: dict[str, int] = {}
    flow_dispatches: dict[str, int] = {}
    flow_turns: dict[str, int] = {}
    # Missed-tool tally: tool -> count + the flows it was missed under.
    missed_count: dict[str, int] = {}
    missed_flows: dict[str, set[str]] = {}

    for key, tools in dispatches_by_turn.items():
        visible = shadow_by_turn.get(key)
        if visible is None:
            # No shadow row for this turn -> not measurable, exclude.
            continue
        if not tools:
            continue
        turns_measured += 1
        flow = _turn_flow(tools)
        if flow is not None:
            flow_turns[flow] = flow_turns.get(flow, 0) + 1
        for tool in tools:
            total_dispatches += 1
            if flow is not None:
                flow_dispatches[flow] = flow_dispatches.get(flow, 0) + 1
            if tool in visible:
                total_hits += 1
                if flow is not None:
                    flow_hits[flow] = flow_hits.get(flow, 0) + 1
            else:
                total_misses += 1
                missed_count[tool] = missed_count.get(tool, 0) + 1
                missed_flows.setdefault(tool, set())
                if flow is not None:
                    missed_flows[tool].add(flow)
                    flow_misses[flow] = flow_misses.get(flow, 0) + 1

    overall = (
        (total_hits / total_dispatches) if total_dispatches else None
    )

    by_flow: list[dict[str, Any]] = []
    for flow in _FLOWS:
        disp = flow_dispatches.get(flow, 0)
        hits = flow_hits.get(flow, 0)
        misses = flow_misses.get(flow, 0)
        by_flow.append(
            {
                "flow": flow,
                "recall": round(hits / disp, 4) if disp else None,
                "turns": flow_turns.get(flow, 0),
                "dispatches": disp,
                "hits": hits,
                "misses": misses,
            }
        )

    missed_tools = [
        {
            "name": name,
            "count": cnt,
            "flows": sorted(missed_flows.get(name, set())),
        }
        for name, cnt in sorted(
            missed_count.items(), key=lambda kv: (-kv[1], kv[0])
        )
    ]

    # The k the shadow rows were taken at (modal value; informational only).
    k_modal: int | None = None
    if k_values:
        from collections import Counter

        k_modal = Counter(k_values).most_common(1)[0][0]

    return {
        "overall": round(overall, 4) if overall is not None else None,
        "turns_measured": turns_measured,
        "dispatches_measured": total_dispatches,
        "hits": total_hits,
        "misses": total_misses,
        "k": k_modal,
        "by_flow": by_flow,
        "missed_tools": missed_tools,
    }


def _empty_recall_at_k() -> dict[str, Any]:
    """Zero-state recall@k section (no shadow rows logged yet)."""
    return {
        "overall": None,
        "turns_measured": 0,
        "dispatches_measured": 0,
        "hits": 0,
        "misses": 0,
        "k": None,
        "by_flow": [],
        "missed_tools": [],
    }


async def build_telemetry_summary(
    *,
    last_n_sessions: int = 30,
    all_segments: bool = False,
) -> dict[str, Any]:
    """Build the routing-quality summary served by the telemetry endpoint, over
    the current boot segment by default or every retained segment; nothing found
    yields the all-zero empty summary."""
    read_targets = telemetry_read_paths(all_segments=all_segments)

    records: list[dict[str, Any]] = _recent_dispatches(
        read_targets, last_n_sessions=last_n_sessions
    )
    used_source = "file" if records else "empty"

    summary = _aggregate_records(records)
    summary["source"] = used_source

    # Fold in the tool-retrieval shadow recall@k section.
    # The would-be-visible shadow rows share the SAME JSONL sink (tagged by
    # record_type); load them and join against the dispatched llm tools above by
    # turn_id. Best-effort: a read/compute fault leaves the zero-state section
    # seeded by _aggregate_records (never breaks the dashboard).
    try:
        shadow_records = read_jsonl_records(
            read_targets, keep=SHADOW_RECORD_TYPE)
        summary["recall_at_k"] = compute_recall_at_k(records, shadow_records)
    except Exception:  # noqa: BLE001 -- never break the dashboard on recall read
        logger.warning("telemetry summary: recall@k read failed", exc_info=True)
        summary["recall_at_k"] = _empty_recall_at_k()

    # Fold in the live big-sim solve_telemetry section. Read
    # from the solve-telemetry JSONL the solve writer maintains; best-effort so
    # a missing/unreadable sink leaves the zero-state section _aggregate_records
    # already seeded. Independent of the tool-call source above -- solves are
    # logged on their own sink.
    try:
        solve_records = read_jsonl_records(_get_solve_telemetry_path())
        summary["solve_telemetry"] = _aggregate_solve_telemetry(solve_records)
    except Exception:  # noqa: BLE001 -- never break the dashboard on solve read
        logger.warning("telemetry summary: solve telemetry read failed", exc_info=True)
        summary["solve_telemetry"] = _empty_solve_telemetry()

    # Fold in the PER-TURN per-model aggregates: turns,
    # mean token counts, mean wall ms, upstream_error_count per model_id. Read
    # from the turn-telemetry JSONL sink the server's turn loop writes
    # (telemetry.emit_turn_telemetry -- its own sink, like solve telemetry);
    # the aggregation lives in telemetry.build_turn_summary. Best-effort: a
    # read/compute fault leaves the zero-state section (never breaks the
    # dashboard).
    try:
        summary["turns_by_model"] = build_turn_summary(load_turn_records())
    except Exception:  # noqa: BLE001 -- never break the dashboard on turn read
        logger.warning("telemetry summary: turn telemetry read failed", exc_info=True)
        summary["turns_by_model"] = empty_turn_summary()
    return summary


def _recent_dispatches(
    targets: Sequence[str],
    *,
    last_n_sessions: int = 30,
) -> list[dict[str, Any]]:
    """The normalized dispatch rows of the most-recent ``last_n_sessions``
    distinct sessions, newest first; shadow rows share the sink and are read
    separately, so they are dropped here."""
    out = [_normalize_record(rec) for rec in
           read_jsonl_records(targets, drop=SHADOW_RECORD_TYPE)]
    if not out:
        return out
    out.sort(key=lambda r: str(r.get("called_at_utc") or ""), reverse=True)
    seen_sessions: list[str] = []
    keep: list[dict[str, Any]] = []
    for r in out:
        sid = r.get("session_id") or ""
        if sid and sid not in seen_sessions:
            if len(seen_sessions) >= last_n_sessions:
                break
            seen_sessions.append(sid)
        keep.append(r)
    return keep


__all__ = [
    "emit_tool_call_event",
    "compute_args_hash",
    "load_tool_call_records",
    "read_jsonl_records",
    "telemetry_read_paths",
    # the reading half: the routing-quality summary over those rows.
    "build_telemetry_summary",
    "compute_recall_at_k",
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
