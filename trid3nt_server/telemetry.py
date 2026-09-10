"""Tool-call telemetry writer.

One JSON line per tool call, turn, shadow selection or solve completion, to a
local JSONL sink written unconditionally. Every emitter here is fire-and-forget
and NEVER raises: telemetry must not break the dispatch, turn or solve loop."""

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
# Telemetry retention. Ephemerality is POLICY, enforced at daemon boot by
# ``cleanup_telemetry_segments``.
#
# A ``TRID3NT_TELEMETRY_PATH`` ending in ``.jsonl`` is an EXACT single-file
# override: unsegmented, never pruned. Unset, or set to a directory, the sink is
# DIRECTORY-mode - one segment per daemon boot named
# ``tool_calls.<boot_id>.jsonl`` - so a long-lived or crash-looped daemon never
# re-grows one unbounded file.
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
# Tool-retrieval SHADOW telemetry.
#
# Shadow mode computes the WOULD-BE-visible tool set per turn without changing
# the catalog the model actually sees. Logging that set lets a recall@k
# measurement compare it against the tools the model really dispatched:
# recall = |dispatched tools that were in the retrieved set| / |dispatched|.
# The rows share the tool-call JSONL sink and carry a
# ``record_type="tool_retrieval_shadow"`` discriminator so a reader can split
# them out. Fire-and-forget; never raises.
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


# --------------------------------------------------------------------------- #
# Solve-time telemetry: per-job autoscale measurements.
#
# At solve completion the real (active_cells, vCPU, wall_clock) triple is
# accumulated so the adaptive-grid cell cap can be re-tuned from measurements
# rather than guesses. A structured logger line fires ALWAYS, so the row lands
# in the agent log even when the JSONL sink is unwritable.
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


# --------------------------------------------------------------------------- #
# SOLVE completion telemetry: compute meta, problem size and timing.
#
# A richer sibling to ``emit_solve_telemetry``: where that record carries the
# autoscale provenance, this one folds the compute the solve actually ran on
# together with the mesh size, so a perf model can later infer completion time
# from real measurements. A structured INFO line always fires alongside the
# JSONL append, and a ``record_type="solve"`` discriminator separates these rows
# from the per-tool rows that share the sink.
# --------------------------------------------------------------------------- #

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


# --------------------------------------------------------------------------- #
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
