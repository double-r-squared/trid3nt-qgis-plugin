"""Own the reviewed script process and forward its run through the daemon emitter."""

from __future__ import annotations

import asyncio
import logging
import os
import pickle
import signal
import socket
import sys
from multiprocessing.connection import Connection
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from trid3nt_contracts.execution import LayerURI

from trid3nt.errors import DeclarationError
from trid3nt.render.emission import current_emitter, current_turn_case
from trid3nt.render.layer_uri_emit import publish_input_layer
from trid3nt.sdk.lock import Lock
from trid3nt.sdk.script_run import ReviewRequest, ReviewResponse, save_deck
from trid3nt.settings import bind_run, clear_session_run, reset_run, run_stated, setting
from trid3nt.tools.mesh.session import _THREAD_VARIABLES
from trid3nt.workflows.runtime.script import Executed
from trid3nt.workflows.runtime.workflow import RunResult
from trid3nt.workflows.solver.solve_progress import mint_dispatch_and_sim_cards, route_sim_terminal
from trid3nt.workflows.solver.solver import DOCKER_KILL_TIMEOUT_S, _docker_kill
from trid3nt.workflows.solver.stop import Stopped, finish

logger = logging.getLogger(__name__)

_BOOT = "from trid3nt.sdk.script_run import main; main()"


def _refusal(code: str, message: str = "The reviewed script could not finish.") -> DeclarationError:
    return DeclarationError(message, error_code=code)


class _Relay:
    def __init__(self, emitter: Any, already_published: int = 0) -> None:
        self.emitter = emitter
        self.already_published = already_published
        self.stage = None
        self.label = ""
        self.checked = False
        self.sim = None
        self.cancelling = asyncio.Event()
        self.pending = None
        self.run_id: str | None = None
        self.review: asyncio.Task | None = None
        self.charts: list[dict] = []

    async def event(self, kind: str, *args: Any) -> Any:
        if kind == "checked":
            self.checked = True
        elif kind == "stage-start":
            self.label, = args
            self.stage = self.emitter.substep(self.label)
            await self.stage.__aenter__()
        elif kind == "stage-end":
            if self.label == "outputs" and args == ("complete",):
                self.pending = args
            else:
                await self.end(*args)
        elif kind == "dispatch":
            solver, module, handle, cores = args
            self.run_id = getattr(handle, "run_id", self.run_id)
            self.sim = await mint_dispatch_and_sim_cards(
                emitter=self.emitter, solver=solver, module=module, handle=handle, cores=cores)
        elif kind == "progress":
            try:
                await self.emitter.emit_solve_progress(args[0])
            except Exception:  # noqa: BLE001 - a progress tick cannot interrupt the solve
                logger.debug("SCRIPT_PROGRESS_UNDELIVERED", exc_info=True)
        elif kind == "terminal":
            if args[0] is None:
                self.cancelling.set()
            await route_sim_terminal(self.emitter, self.sim, run_result=args[0])
            self.sim = None
        elif kind == "chart":
            self.charts.append(args[0])
        elif kind in {"complete", "stopped", "refused"}:
            return (kind, *args)
        return None

    async def end(self, state: str, code: str | None = None, message: str = "") -> None:
        if self.stage is None:
            return
        stage, self.stage = self.stage, None
        error = (asyncio.CancelledError() if state == "cancelled" else
                 DeclarationError(message, error_code=code) if state == "failed" else None)
        await stage.__aexit__(type(error) if error is not None else None, error, None)

    async def publish(self, record: dict) -> None:
        clear_session_run()
        for row in record.get("outputs", ())[self.already_published:]:
            layer = LayerURI.model_validate(row)
            await publish_input_layer(self.emitter, layer, role=layer.role)
        for chart in self.charts:
            await self.emitter.emit_chart(chart)
        if self.pending is not None:
            await self.end(*self.pending)

    async def abort(self, code: str, cancelled: bool) -> None:
        if self.sim is not None:
            await route_sim_terminal(self.emitter, self.sim, run_result=None if cancelled else
                                     SimpleNamespace(status="failed", error_code=code))
        await self.end("cancelled" if cancelled else "failed", code, str(_refusal(code)))

    def cancel_review(self) -> None:
        if self.review is not None:
            self.review.cancel()


async def _stop(process: Any, reader: Any, relay: _Relay, run_id: str) -> Stopped | None:
    relay.cancel_review()
    run_id = relay.run_id or run_id
    ended = await _end(process, reader, run_id, cancelling=relay.cancelling)
    if ended is not None and ended[0] == "stopped":
        _, value, reason, record = ended
        if record is not None:
            await relay.publish(record)
        return Stopped(RunResult(value=value), value, reason, recorded=True)
    if ended is None:
        await asyncio.to_thread(_docker_kill, relay.run_id or run_id)
    await relay.abort("SCRIPT_CHILD_STOPPED", cancelled=True)
    return None


async def _messages(channel: Connection, process: Any, relay: _Relay, handoff: dict) -> Any:
    await asyncio.to_thread(channel.send, handoff)
    result = None
    while True:
        try:
            kind, *args = await asyncio.to_thread(channel.recv)
        except EOFError:
            break
        if kind == "review":
            from trid3nt.inputs.review import gate_input_review

            request = ReviewRequest.model_validate(args[0])
            relay.review = asyncio.create_task(gate_input_review(tool_name=request.tool_name, entries=request.entries,
                                                                params=request.params, mode=request.mode))
            try:
                outcome = await relay.review
            except asyncio.CancelledError:
                outcome = ReviewResponse(id=request.id, proceed=False, entries=request.entries,
                                         params=request.params, cancelled=True).outcome()
            finally:
                relay.review = None
            response = ReviewResponse.from_outcome(request.id, outcome)
            await asyncio.to_thread(channel.send, response.model_dump(mode="json"))
            continue
        found = await relay.event(kind, *args)
        if found is not None:
            result = found
    if await process.wait() != 0:
        return None
    return result


def _signal(process: Any, number: int) -> None:
    if process.returncode is None:
        try:
            os.killpg(process.pid, number)
        except ProcessLookupError:
            pass


async def _end(process: Any, reader: Any, run_id: str, *,
               cancelling: asyncio.Event | None = None) -> Any:
    """Bound the wait for native cancellation to start. Let it collect partial products once it has started."""
    _signal(process, signal.SIGTERM)
    acknowledgment = None
    try:
        if cancelling is not None:
            acknowledgment = asyncio.create_task(cancelling.wait())
            ready, _ = await asyncio.wait((reader, acknowledgment), timeout=DOCKER_KILL_TIMEOUT_S,
                                          return_when=asyncio.FIRST_COMPLETED)
            if ready:
                return await reader
            raise TimeoutError
        return await asyncio.wait_for(asyncio.shield(reader), DOCKER_KILL_TIMEOUT_S)
    except Exception:  # noqa: BLE001 - broken relay or expired cleanup requires a hard stop
        _signal(process, signal.SIGKILL)
        await process.wait()
        await asyncio.to_thread(_docker_kill, run_id)
        try:
            await reader
        except Exception:  # noqa: BLE001 - the owner already handles the failed relay
            pass
        return None
    finally:
        if acknowledgment is not None:
            acknowledgment.cancel()


def _handoff(path: Path, deck: Any, receipt: dict, case: str, cores: int) -> dict:
    lock = Lock(path.parent)
    return {"deck": save_deck(deck), "row": lock.data["models"][deck.state.workflow.name],
            "receipt": receipt, "case": case, "cores": cores, "script": path.name}


async def run(path: Path, deck: Any, receipt: dict) -> Executed:
    """Start a new interpreter and its process group for the saved case script."""
    token = bind_run(deck.state.settings)
    try:
        cores = int(setting("LOCAL_CORES"))
        budget = float(setting("SOLVE_BUDGET_S")) or None
        settings = run_stated()
    finally:
        reset_run(token)
    try:
        handoff = await asyncio.to_thread(_handoff, path, deck, receipt, current_turn_case(), cores)
        handoff["settings"] = settings
    except (AttributeError, KeyError, OSError, pickle.PickleError, TypeError, ValueError) as exc:
        raise _refusal("SCRIPT_HANDOFF_FAILED") from exc
    ours, theirs = socket.socketpair()
    env = {**os.environ, **dict.fromkeys(_THREAD_VARIABLES, str(cores))}
    with ours, theirs, Connection(ours.detach()) as channel:
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-c", _BOOT, str(theirs.fileno()),
                cwd=path.parent, env=env, pass_fds=(theirs.fileno(),), start_new_session=True)
        except OSError as exc:
            raise _refusal("SCRIPT_CHILD_START_FAILED") from exc
        theirs.close()
        relay = _Relay(current_emitter(), len(deck.said.outputs))
        reader = asyncio.create_task(_messages(channel, process, relay, handoff))
        try:
            ended = await asyncio.wait_for(asyncio.shield(reader), budget)
        except asyncio.CancelledError:
            stopped = await finish(_stop(process, reader, relay, deck.run_id))
            if stopped is not None:
                raise stopped from None
            raise
        except TimeoutError:
            relay.cancel_review()
            await finish(_end(process, reader, relay.run_id or deck.run_id))
            await relay.abort("SCRIPT_TIME_BUDGET", cancelled=False)
            raise _refusal("SCRIPT_TIME_BUDGET") from None
        except Exception as exc:  # noqa: BLE001 - reap the child even when the relay fails
            await finish(_end(process, reader, relay.run_id or deck.run_id))
            await relay.abort("SCRIPT_CHILD_DIED", cancelled=False)
            raise _refusal("SCRIPT_CHILD_DIED") from exc
    if ended is None:
        await asyncio.to_thread(_docker_kill, relay.run_id or deck.run_id)
        await relay.abort("SCRIPT_CHILD_DIED", cancelled=False)
        raise _refusal("SCRIPT_CHILD_DIED")
    if ended[0] == "refused":
        await relay.abort(ended[1], cancelled=False)
        raise _refusal(*ended[1:])
    _, value, record = ended
    if not record or not relay.checked:
        raise _refusal("SCRIPT_RECORD_MISSING")
    await relay.publish(record)
    return Executed(value)
