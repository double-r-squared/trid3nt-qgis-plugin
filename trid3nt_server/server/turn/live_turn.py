"""Detached-turn registry: the _LiveTurn handle + session-keyed live-turn bindings."""

from __future__ import annotations

from dataclasses import dataclass

# Module-level live-turn registry keyed by ``(session_id, turn_key)`` --
# mirrors ``_SESSION_ACTIVE_CASE``'s session-scoped discipline so an
# in-flight turn OUTLIVES the per-connection ``SessionState``. Keying the
# running task by ``session_id`` lets it survive the death of any one
# socket; a closing connection's handler ``finally`` only drops that
# connection's references (letting cheap turns finish) instead of
# cancelling. ``wait_for_completion``'s own 1800s budget bounds a truly
# stuck solve.
#
# Each entry carries the running ``asyncio.Task`` AND the ``PipelineEmitter``
# the task is driving (so a reconnecting socket can rebind the emitter's
# sink and receive the live solve's progress + terminal frames -- see
# ``_rebind_live_turns``). A done-callback removes the entry on
# completion/cancellation (no leak). Bounded by session-count; the value is
# one task+emitter pair per live turn.
@dataclass
class _LiveTurn:
    """An in-flight turn that has been detached from its launching connection.

    ``task`` is the running ``asyncio.Task``; ``emitter`` is the
    ``PipelineEmitter`` it drives (its ``_sink`` may point at a now-dead socket
    until a reconnecting socket rebinds it via ``_rebind_live_turns``)."""

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
    """Detach ``task`` into the module-level live-turn registry.

    Installs a done-callback that removes the entry on completion/cancellation
    so a completed/cancelled task never lingers (Requirement 4: NO leak). Safe
    to call more than once for the same task (the callback de-dups on identity).
    """
    if (
        session_id not in _SESSION_LIVE_TURNS
        and len(_SESSION_LIVE_TURNS) >= _SESSION_LIVE_TURNS_CAP
    ):
        # Evict the oldest session bucket whose turns are ALL done; if none are
        # fully-done, evict the oldest regardless (bounded memory -- a live solve
        # is never silently dropped under normal session counts).
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
        # Only drop if THIS task still owns the slot (a same-stream supersede may
        # have replaced it with a fresh task -- don't evict the newer turn).
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
    """Rebind live turn(s) of ``session_id`` onto ``emitter``'s sink.

    When a new socket for the same session connects, point the
    still-running turn's emitter at the new socket so its progress +
    terminal frames reach the live connection. Returns the number of turns
    rebound. No-op when no live turns exist or ``emitter`` is None.

    The new connection's emitter IS the wire face (its ``_sink`` closes over
    the live socket's ``send``). We swap the LIVE turn's emitter sink to
    that same sink. Done/cancelled turns are skipped + pruned.

    ``only_turn_key`` restricts the rebind to a single stream -- used by the
    case-open path so opening Case A only rebinds Case A's live solve onto
    the new socket (a concurrent Case B solve keeps emitting through its
    own -- soon its OWN socket-resume / case-open rebinds it, or it lands
    fully-detached and its layer rehydrates on the next case-open)."""
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
            # Rebinding the live turn's emitter onto the new sink only
            # recovers FUTURE frames + pipeline CARDS -- not a loaded-layers
            # session-state emitted onto the now-dead launch socket before
            # this reconnect (e.g. a terminal flood-depth layer published
            # late after a multi-minute solve). Seed this reconnect's fresh
            # emitter from the live turn's accumulated layers so the
            # caller's emit_session_state carries the full snapshot to the
            # new socket. Union-by-identity: no duplicate, and the live
            # turn's later (superset) emits never regress it.
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
    """Return any live (not-done) detached turn for ``session_id`` or None.

    Cancel fallback: when the keyed lookup misses (the binding moved), the stop
    button still needs to reach a detached solver turn."""
    bucket = _SESSION_LIVE_TURNS.get(session_id)
    if not bucket:
        return None
    for lt in bucket.values():
        if not lt.task.done():
            return lt.task
    return None
