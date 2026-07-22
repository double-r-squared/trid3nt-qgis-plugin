"""job-SOLVE-SURVIVE: a long-running solve must SURVIVE a WS disconnect.

The #1 blocker: SFINCS flood modeling produced no successful run since Fort
Myers because a WS disconnect KILLED the in-flight solve. Root cause: the
handler ``finally`` blanket-``.cancel()``-ed every not-done task on the
per-connection ``SessionState.inflight_tasks`` — including a detached
``run_model_flood_scenario`` -> ``wait_for_completion``. The web client opens
MULTIPLE sockets per session (StrictMode double-mount + reconnect), so a
transient socket swap detonated the finally and docker-killed the solve ~7s in.

These tests exercise the lifecycle WITHOUT a real solver (mock the WS + the
turn body the way the existing server tests do):

  (a) a solver turn is NOT cancelled when its launching connection's handler
      ``finally`` runs (simulated disconnect) — the task keeps running.
  (b) the explicit ``cancel`` envelope still cancels it (genuine cancellation +
      docker-kill preserved).
  (c) a new connection for the same session re-binds the emitter sink, so the
      in-flight solve's progress + terminal frames reach the new socket.
  (d) no task leak: a completed turn is removed from the module-level registry.
  (e) a non-solver (cheap LLM-only) turn behaves as before — also kept running
      across a disconnect and self-removed on completion (no leak).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from grace2_agent import server
from grace2_agent.pipeline_emitter import PipelineEmitter
from grace2_contracts.common import new_ulid
from grace2_contracts.execution import LayerURI


class FakeWS:
    """Minimal WS stand-in mirroring the existing server tests' FakeWS."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False

    async def send(self, text: str) -> None:
        if self.closed:
            # Mirror websockets: a send on a dead socket raises.
            raise ConnectionError("socket closed")
        self.sent.append(text)


@pytest.fixture(autouse=True)
def _clean_registries():
    server._SESSION_ACTIVE_CASE.clear()
    server._SESSION_LIVE_TURNS.clear()
    yield
    server._SESSION_ACTIVE_CASE.clear()
    server._SESSION_LIVE_TURNS.clear()


def _make_emitter(ws: FakeWS, session_id: str) -> PipelineEmitter:
    """Build an emitter whose sink writes to ``ws`` (matches _ensure_emitter)."""

    async def _sink(text: str) -> None:
        try:
            await ws.send(text)
        except Exception:  # noqa: BLE001 — emitter swallows dead-socket sends
            pass

    return PipelineEmitter(session_id=session_id, sink=_sink)


async def _gated_turn(release: asyncio.Event, emitter: PipelineEmitter) -> None:
    """Stand in for a detached solver turn body: emit one progress frame, then
    block on ``release`` (the 'solve in progress'), then emit a terminal frame.
    Drives everything through ``emitter`` exactly like the real solve path."""
    await emitter.emit_session_state()  # one live frame (progress proxy)
    await release.wait()
    await emitter.emit_session_state()  # terminal frame (proxy for layer publish)


def _simulate_disconnect_finally(state: server.SessionState) -> None:
    """Run the EXACT detach logic from the handler ``finally`` on ``state``.

    The real finally lives inside the closure ``_make_handler.handler``; this
    reproduces its observable contract so the test does not need a live socket
    server: for each not-done in-flight turn, ensure it is registered in the
    module-level live-turn registry (detached, kept running) — NEVER cancel."""
    for turn_key, t in list(state.inflight_tasks.items()):
        if t.done():
            continue
        if server._find_live_turn(state.session_id, turn_key) is not t:
            server._register_live_turn(state.session_id, turn_key, t, state.emitter)


# --------------------------------------------------------------------------- #
# (a) disconnect does NOT cancel a (solver) turn
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_solver_turn_survives_disconnect() -> None:
    session_id = new_ulid()
    ws = FakeWS()
    state = server.SessionState(session_id=session_id)
    state.emitter = _make_emitter(ws, session_id)

    release = asyncio.Event()
    turn_key = "case-flood"
    task = asyncio.create_task(_gated_turn(release, state.emitter))
    state.inflight_tasks[turn_key] = task
    server._register_live_turn(session_id, turn_key, task, state.emitter)
    await asyncio.sleep(0.02)  # let the turn emit its first progress frame

    # The launching socket closes — handler finally runs.
    ws.closed = True
    _simulate_disconnect_finally(state)

    # The solve task is NOT cancelled / done; it is still running.
    assert not task.cancelled()
    assert not task.done(), "disconnect must NOT kill the in-flight solve"
    # And it is durably registered keyed by (session_id, turn_key).
    assert server._find_live_turn(session_id, turn_key) is task

    # The solve eventually completes on its own.
    release.set()
    await task
    assert task.done() and not task.cancelled()


# --------------------------------------------------------------------------- #
# (b) explicit cancel still cancels the detached turn (genuine cancellation)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_explicit_cancel_still_cancels_detached_turn() -> None:
    session_id = new_ulid()
    ws = FakeWS()
    state = server.SessionState(session_id=session_id)
    state.emitter = _make_emitter(ws, session_id)

    release = asyncio.Event()
    turn_key = "case-flood"
    task = asyncio.create_task(_gated_turn(release, state.emitter))
    state.inflight_tasks[turn_key] = task
    server._register_live_turn(session_id, turn_key, task, state.emitter)
    await asyncio.sleep(0.02)

    # Disconnect detaches the turn (does not cancel it).
    ws.closed = True
    _simulate_disconnect_finally(state)
    assert not task.done()

    # The stop button reaches the detached turn through the module registry.
    cancel_task = server._find_live_turn(session_id, turn_key)
    if cancel_task is None or cancel_task.done():
        cancel_task = server._any_live_turn(session_id)
    assert cancel_task is task, "cancel must locate the detached solve"
    cancel_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()


# --------------------------------------------------------------------------- #
# (c) a new connection for the same session re-binds the emitter sink
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_reconnect_rebinds_emitter_to_new_socket() -> None:
    session_id = new_ulid()
    ws_old = FakeWS()
    state_old = server.SessionState(session_id=session_id)
    state_old.emitter = _make_emitter(ws_old, session_id)

    release = asyncio.Event()
    turn_key = "case-flood"
    task = asyncio.create_task(_gated_turn(release, state_old.emitter))
    state_old.inflight_tasks[turn_key] = task
    server._register_live_turn(session_id, turn_key, task, state_old.emitter)
    await asyncio.sleep(0.02)
    frames_before_disconnect = len(ws_old.sent)
    assert frames_before_disconnect >= 1  # first progress frame landed

    # Launching socket dies; detach (keep running).
    ws_old.closed = True
    _simulate_disconnect_finally(state_old)

    # A NEW socket for the same session connects (fresh SessionState + emitter).
    ws_new = FakeWS()
    state_new = server.SessionState(session_id=session_id)
    state_new.emitter = _make_emitter(ws_new, session_id)
    rebound = server._rebind_live_turns(session_id, state_new.emitter)
    assert rebound == 1, "the live solve's emitter must be rebound"

    # The terminal frame (proxy for the published flood layer) now reaches the
    # NEW socket, not the dead old one.
    new_before = len(ws_new.sent)
    release.set()
    await task
    assert len(ws_new.sent) > new_before, (
        "terminal solve frame must land on the reconnected socket"
    )
    # The dead socket received nothing further.
    assert len(ws_old.sent) == frames_before_disconnect


# --------------------------------------------------------------------------- #
# (c2) the TERMINAL flood-depth layer survives a reconnect that lands AFTER the
#      terminal emit was already pushed onto the dead launch socket. This is the
#      real "floods render only the input layers, never the depth output" bug:
#      the depth layer (published late, after the multi-minute solve) is emitted
#      onto the now-dead socket in the window before the browser's reconnect, and
#      the rebind only replays the pipeline CARDS -- not the loaded-layers
#      session-state -- so without the merge-seed the new socket never sees it.
# --------------------------------------------------------------------------- #


def _last_loaded_uris(ws: FakeWS) -> list[str]:
    """URIs in the LAST session-state frame the socket received (A.7 snapshot)."""
    last: list[str] = []
    for raw in ws.sent:
        try:
            env = json.loads(raw)
        except Exception:  # noqa: BLE001
            continue
        if env.get("type") != "session-state":
            continue
        layers = (env.get("payload") or {}).get("loaded_layers") or []
        last = [l.get("uri") for l in layers]
    return last


def _depth_layer() -> LayerURI:
    return LayerURI(
        layer_id="flood_depth_peak",
        name="Peak flood depth",
        layer_type="raster",
        uri="s3://runs/flood-run/peak_depth.tif?cog",
        style_preset="continuous_flood_depth",
        role="primary",
    )


def _input_layer() -> LayerURI:
    return LayerURI(
        layer_id="landcover_input",
        name="Land cover",
        layer_type="raster",
        uri="s3://runs/flood-run/landcover.tif?cog",
        style_preset="nlcd",
        role="input",
    )


@pytest.mark.asyncio
async def test_terminal_depth_layer_survives_reconnect_after_dead_socket_emit() -> None:
    """REGRESSION (job-FLOOD-TERMINAL-SURVIVE): a depth layer published AFTER the
    launch socket died but BEFORE the reconnect must still reach the new socket."""
    session_id = new_ulid()
    ws_old = FakeWS()
    state_old = server.SessionState(session_id=session_id)
    state_old.emitter = _make_emitter(ws_old, session_id)

    input_done = asyncio.Event()
    may_publish_depth = asyncio.Event()
    depth_done = asyncio.Event()
    may_finish = asyncio.Event()

    async def _flood_turn(emitter: PipelineEmitter) -> None:
        # Input layer paints EARLY (on the live launch socket).
        await emitter.add_loaded_layer(_input_layer())
        input_done.set()
        # ... multi-minute solve ... the launch socket dies during it ...
        await may_publish_depth.wait()
        # TERMINAL depth layer publishes LATE -- onto the now-dead launch sink.
        await emitter.add_loaded_layer(_depth_layer())
        depth_done.set()
        # The turn is STILL running here (metrics, summary, persist-to-Case in
        # the dispatch finally) -- the realistic window in which the browser's
        # auto-reconnect lands. Hold it open so the reconnect sees a LIVE turn.
        await may_finish.wait()

    turn_key = "case-flood"
    task = asyncio.create_task(_flood_turn(state_old.emitter))
    state_old.inflight_tasks[turn_key] = task
    server._register_live_turn(session_id, turn_key, task, state_old.emitter)
    await input_done.wait()
    assert "s3://runs/flood-run/landcover.tif?cog" in _last_loaded_uris(ws_old)

    # Launch socket dies; turn detaches (kept running).
    ws_old.closed = True
    _simulate_disconnect_finally(state_old)

    # The terminal depth emit fires NOW -- onto the dead old sink (dropped).
    may_publish_depth.set()
    await depth_done.wait()
    # The dead socket never received the depth layer.
    assert "s3://runs/flood-run/peak_depth.tif?cog" not in _last_loaded_uris(ws_old)

    # The browser reconnects WHILE the turn is still finishing: fresh
    # SessionState + emitter, then session-resume rebinds the live turn. The
    # merge-seed must carry the already-published depth layer onto the new
    # emitter so the reconnect's own session-state delivers it.
    ws_new = FakeWS()
    state_new = server.SessionState(session_id=session_id)
    state_new.emitter = _make_emitter(ws_new, session_id)
    rebound = server._rebind_live_turns(session_id, state_new.emitter)
    assert rebound == 1, "the still-running solve must rebind"
    # Mirror _handle_session_resume: the reconnect emits its own session-state.
    await state_new.emitter.emit_session_state()

    new_uris = _last_loaded_uris(ws_new)
    assert "s3://runs/flood-run/peak_depth.tif?cog" in new_uris, (
        "the terminal flood-depth layer must reach the reconnected socket"
    )
    # Inputs survive too -- the full snapshot, not just the terminal layer.
    assert "s3://runs/flood-run/landcover.tif?cog" in new_uris

    # Let the turn finish cleanly (no leak).
    may_finish.set()
    await task


@pytest.mark.asyncio
async def test_merge_loaded_layers_from_is_union_no_duplicate() -> None:
    """merge_loaded_layers_from unions by identity -- a layer the target already
    holds is never duplicated, and side-tables come across for new layers."""
    sid = new_ulid()
    live = _make_emitter(FakeWS(), sid)
    await live.add_loaded_layer(_input_layer())
    await live.add_loaded_layer(_depth_layer())

    fresh = _make_emitter(FakeWS(), sid)
    # Pre-seed the fresh emitter with ONE of the two layers (simulates a layer
    # already known to the reconnect) to prove union-not-append.
    await fresh.add_loaded_layer(_input_layer())

    merged = fresh.merge_loaded_layers_from(live)
    assert merged == 1  # only the depth layer is new
    uris = [l.uri for l in fresh.loaded_layers]
    assert uris.count("s3://runs/flood-run/landcover.tif?cog") == 1  # no dup
    assert "s3://runs/flood-run/peak_depth.tif?cog" in uris

    # Idempotent: a second merge adds nothing.
    assert fresh.merge_loaded_layers_from(live) == 0


# --------------------------------------------------------------------------- #
# (d) no leak: a completed turn is removed from the module registry
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_no_leak_after_completion() -> None:
    session_id = new_ulid()
    ws = FakeWS()
    state = server.SessionState(session_id=session_id)
    state.emitter = _make_emitter(ws, session_id)

    release = asyncio.Event()
    turn_key = "case-flood"
    task = asyncio.create_task(_gated_turn(release, state.emitter))
    server._register_live_turn(session_id, turn_key, task, state.emitter)
    await asyncio.sleep(0.02)
    assert server._find_live_turn(session_id, turn_key) is task

    release.set()
    await task
    # The done-callback runs on the next loop tick.
    await asyncio.sleep(0)
    assert server._find_live_turn(session_id, turn_key) is None
    # The whole session bucket is pruned when its last turn drops.
    assert session_id not in server._SESSION_LIVE_TURNS

    # Cross-session isolation: another session's registry is untouched.
    other = new_ulid()
    assert other not in server._SESSION_LIVE_TURNS


@pytest.mark.asyncio
async def test_no_bleed_across_sessions() -> None:
    """A turn registered under session A must never be reachable via session B."""
    sid_a, sid_b = new_ulid(), new_ulid()
    ws_a = FakeWS()
    state_a = server.SessionState(session_id=sid_a)
    state_a.emitter = _make_emitter(ws_a, sid_a)
    release = asyncio.Event()
    task = asyncio.create_task(_gated_turn(release, state_a.emitter))
    server._register_live_turn(sid_a, "case-x", task, state_a.emitter)
    await asyncio.sleep(0.02)

    assert server._find_live_turn(sid_a, "case-x") is task
    assert server._find_live_turn(sid_b, "case-x") is None
    assert server._any_live_turn(sid_b) is None
    # Rebinding session B does not touch session A's live turn.
    ws_b = FakeWS()
    emitter_b = _make_emitter(ws_b, sid_b)
    assert server._rebind_live_turns(sid_b, emitter_b) == 0

    release.set()
    await task


# --------------------------------------------------------------------------- #
# (e) a non-solver / cheap turn behaves as before (kept running + no leak)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_cheap_turn_finishes_and_no_leak_across_disconnect() -> None:
    """A short LLM-only turn is also detached (not cancelled) on disconnect and
    self-removes on its quick completion — no leak, no special-casing."""
    session_id = new_ulid()
    ws = FakeWS()
    state = server.SessionState(session_id=session_id)
    state.emitter = _make_emitter(ws, session_id)

    done = asyncio.Event()

    async def _cheap_turn() -> None:
        await state.emitter.emit_session_state()  # one quick frame, then return
        done.set()

    turn_key = server._ROOT_STREAM_KEY
    task = asyncio.create_task(_cheap_turn())
    state.inflight_tasks[turn_key] = task
    server._register_live_turn(session_id, turn_key, task, state.emitter)

    # Disconnect BEFORE the cheap turn finishes: must still not cancel it.
    ws.closed = True
    _simulate_disconnect_finally(state)
    assert not task.cancelled()

    await done.wait()
    await task
    await asyncio.sleep(0)  # let the done-callback fire
    assert not task.cancelled(), "cheap turn must finish, not be cancelled"
    assert server._find_live_turn(session_id, turn_key) is None
    assert session_id not in server._SESSION_LIVE_TURNS


# --------------------------------------------------------------------------- #
# supersede: a same-stream re-prompt cancels the prior (even detached) turn
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_same_stream_supersede_cancels_detached_prior() -> None:
    session_id = new_ulid()
    ws = FakeWS()
    state = server.SessionState(session_id=session_id)
    state.emitter = _make_emitter(ws, session_id)

    release = asyncio.Event()
    turn_key = "case-flood"
    prior = asyncio.create_task(_gated_turn(release, state.emitter))
    state.inflight_tasks[turn_key] = prior
    server._register_live_turn(session_id, turn_key, prior, state.emitter)
    await asyncio.sleep(0.02)

    # Socket swap detaches the prior turn.
    ws.closed = True
    _simulate_disconnect_finally(state)

    # New socket; same stream re-prompt → supersede policy from the recv loop:
    # look up prior (per-connection miss → module registry) and cancel it.
    ws2 = FakeWS()
    state2 = server.SessionState(session_id=session_id)
    state2.emitter = _make_emitter(ws2, session_id)
    found = state2.inflight_tasks.get(turn_key)
    if found is None or found.done():
        found = server._find_live_turn(session_id, turn_key)
    assert found is prior
    found.cancel()
    with pytest.raises(asyncio.CancelledError):
        await prior

    # The new turn registers fresh under the same key; no leak of the old one.
    new_release = asyncio.Event()
    new_task = asyncio.create_task(_gated_turn(new_release, state2.emitter))
    state2.inflight_tasks[turn_key] = new_task
    server._register_live_turn(session_id, turn_key, new_task, state2.emitter)
    await asyncio.sleep(0)
    assert server._find_live_turn(session_id, turn_key) is new_task
    new_release.set()
    await new_task
