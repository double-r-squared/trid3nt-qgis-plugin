"""Detached-turn registry: the _LiveTurn handle + session-keyed live-turn bindings."""

from __future__ import annotations

from dataclasses import dataclass

# Module-level live-turn registry keyed by session and stream, so an in-flight
# turn OUTLIVES the per-connection state: a closing connection drops only that
# connection's references instead of cancelling, and a cheap turn finishes.
#
# Each entry carries the running task AND the emitter it drives, so a
# reconnecting socket can rebind that emitter's sink and receive the live
# solve's progress and terminal frames. A done-callback removes the entry on
# completion, so nothing leaks, and the registry is bounded by session count.
@dataclass
class _LiveTurn:
    """An in-flight turn detached from its launching connection; the emitter it
    drives may still point at a now-dead socket until a reconnect rebinds it."""

    task: "asyncio.Task"
    emitter: "PipelineEmitter | None"

#: session_id -> {turn_key -> _LiveTurn}. Populated when a connection closes with
#: a still-running turn (handler ``finally``); consulted by the cancel envelope
#: (so the stop button still kills a detached solve) and by a reconnecting
#: connection (so its emitter sink is rebound to the live turn).
_SESSION_LIVE_TURNS: dict[str, dict[str, _LiveTurn]] = {}

_SESSION_LIVE_TURNS_CAP = 4096

def _register_live_turn(
    session_id: str, turn_key: str, task: "asyncio.Task", emitter: "PipelineEmitter | None"
) -> None:
    """Detach ``task`` into the live-turn registry, with a done-callback that
    removes the entry on completion so nothing lingers; calling it twice for one
    task is safe, because the callback de-dups on identity."""
    if (
        session_id not in _SESSION_LIVE_TURNS
        and len(_SESSION_LIVE_TURNS) >= _SESSION_LIVE_TURNS_CAP
    ):
        # Evict the oldest bucket whose turns are ALL done, or the oldest
        # regardless when none is fully done: bounded memory, and a live solve
        # is never silently dropped at normal session counts.
        for sid in list(_SESSION_LIVE_TURNS):
            if all(lt.task.done() for lt in _SESSION_LIVE_TURNS[sid].values()):
                _SESSION_LIVE_TURNS.pop(sid, None)
                break
        else:
            _SESSION_LIVE_TURNS.pop(next(iter(_SESSION_LIVE_TURNS)), None)
    bucket = _SESSION_LIVE_TURNS.setdefault(session_id, {})
    bucket[turn_key] = _LiveTurn(task=task, emitter=emitter)

    def _drop(_t: "asyncio.Task") -> None:
        b = _SESSION_LIVE_TURNS.get(session_id)
        if b is None:
            return
        lt = b.get(turn_key)
        # Only drop when THIS task still owns the slot: a same-stream supersede
        # may have replaced it, and the newer turn must not be evicted.
        if lt is not None and lt.task is _t:
            b.pop(turn_key, None)
        if not b:
            _SESSION_LIVE_TURNS.pop(session_id, None)

    task.add_done_callback(_drop)

def _rebind_live_turns(
    session_id: str,
    emitter: "PipelineEmitter | None",
    *,
    only_turn_key: str | None = None,
) -> int:
    """Point every still-running turn of ``session_id`` at ``emitter``'s sink,
    so its progress and terminal frames reach the live connection, and return
    how many were rebound; ``only_turn_key`` restricts it to one stream."""
    # A done or cancelled turn is skipped and pruned. Restricting by stream lets
    # a case-open rebind only that Case's live solve, leaving a concurrent solve
    # in another Case to be rebound by its own resume or open.
    bucket = _SESSION_LIVE_TURNS.get(session_id)
    if not bucket or emitter is None:
        return 0
    rebound = 0
    for turn_key in list(bucket):
        if only_turn_key is not None and turn_key != only_turn_key:
            continue
        lt = bucket.get(turn_key)
        if lt is None:
            continue
        if lt.task.done():
            bucket.pop(turn_key, None)
            continue
        if lt.emitter is not None and lt.emitter is not emitter:
            lt.emitter.rebind_sink(emitter._sink)
            # Rebinding the sink recovers only FUTURE frames, not a session
            # state emitted onto the now-dead launch socket before this
            # reconnect. Seeding this emitter from the live turn's accumulated
            # layers makes the caller's emit carry the full snapshot; the union
            # is by identity, so nothing duplicates and the live turn's later
            # superset emits never regress it.
            emitter.merge_loaded_layers_from(lt.emitter)
            rebound += 1
    if not bucket:
        _SESSION_LIVE_TURNS.pop(session_id, None)
    return rebound

def _find_live_turn(session_id: str, turn_key: str) -> "asyncio.Task | None":
    """Return the live, not-done task for ``(session_id, turn_key)`` or None."""
    bucket = _SESSION_LIVE_TURNS.get(session_id)
    if not bucket:
        return None
    lt = bucket.get(turn_key)
    if lt is not None and not lt.task.done():
        return lt.task
    return None

def _any_live_turn(session_id: str) -> "asyncio.Task | None":
    """Return any live detached turn for ``session_id``, else ``None``: the
    cancel fallback for when the keyed lookup misses because the binding moved."""
    bucket = _SESSION_LIVE_TURNS.get(session_id)
    if not bucket:
        return None
    for lt in bucket.values():
        if not lt.task.done():
            return lt.task
    return None
