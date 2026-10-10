"""PipelineEmitter - pipeline-state and session-state emission.

Owns one session's ``PipelineSnapshot`` and its ``loaded_layers`` accumulator.
REPLACE, never reconcile: every emission carries the full current state, and
there is no merge, update-partial or apply-delta helper here to break that.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from trid3nt_contracts import new_ulid
from trid3nt_contracts.case import PersistedSubStepRecord
from trid3nt_contracts.collections import (
    PipelineSnapshot,
    PipelineStepSummary,
    ProjectLayerSummary,
)
from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.ws import (
    Envelope,
    MapCommandPayload,
    PipelineStatePayload,
    PipelineStep,
    SessionStatePayload,
    SolveProgressPayload,
    ToolIoPayload,
)

from .layer_uri_emit import emit_layer_uri, publish_for_emission

__all__ = [
    "EmitterError",
    "StepNotFoundError",
    "PipelineEmitter",
    "EmissionSink",
    "current_emitter",
    "dispatched_tool_name",
    "substep",
    "begin_substeps",
    "emit_chart_payloads",
    "ChartPersistHook",
    "ToolCardPersistHook",
    "bind_turn_case",
    "current_turn_case",
    "summary_of",
]


# The turn's pinned Case, bound at task entry; every envelope built inside the turn stamps ``case_id`` from it so
# the client routes to the owning Case after a switch. Per-task, so concurrent turns cannot cross-tag.

_TURN_CASE: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "trid3nt_turn_case", default=None
)


def bind_turn_case(case_id: str | None) -> contextvars.Token:
    """Bind the turn's owning Case for envelope tagging; returns the token."""
    return _TURN_CASE.set(case_id)


def current_turn_case() -> str | None:
    """The Case bound to the current task's turn, or None outside a turn."""
    return _TURN_CASE.get()


# The user's rubber-band rectangle (``drawn_geometry``), bound per task so a gate reads it without a new kwarg on every
# dispatch path; a ``basis="user"`` knob that overrides the model's proposal. None when nothing was drawn.

# The active emitter for one tool or workflow invocation, so a workflow body can fire a transient map verb mid-run.
# A ContextVar: sessions run concurrently and a per-task binding never leaks across tasks.

_CURRENT_EMITTER: contextvars.ContextVar["PipelineEmitter | None"] = (
    contextvars.ContextVar("trid3nt_current_emitter", default=None)
)

#: The top-level tool ``emit_tool_call`` is dispatching. The emit-on-fetch seam compares it to the fetcher's name: equal
#: is a direct chat fetch (the tool wrapper already emits the layer, the seam stays silent), different is an in-composer
#: fetch (surfaced as a role="context" input). None outside ``emit_tool_call``.
_DISPATCHED_TOOL: contextvars.ContextVar[str | None] = (
    contextvars.ContextVar("trid3nt_dispatched_tool", default=None)
)


def current_emitter() -> "PipelineEmitter | None":
    """Return the ``PipelineEmitter`` bracketing the current tool/workflow call.
    ``None`` outside an ``emit_tool_call`` scope, and every caller must handle it:
    a transient verb is never a correctness gate.
    """
    return _CURRENT_EMITTER.get()


def dispatched_tool_name() -> str | None:
    """The top-level tool name ``emit_tool_call`` is dispatching (or ``None``).

    A nested call reads the name of its DISPATCHER here, not its own.
    """
    return _DISPATCHED_TOOL.get()


@asynccontextmanager
async def substep(emitter: "PipelineEmitter | None", raw_name: str):
    """No-op-safe wrapper over ``PipelineEmitter.substep``.
    Yields ``None`` and mints nothing when no emitter is bound, so a body reads
    the same whether or not the timeline is being surfaced.
    """
    if emitter is None:
        yield None
        return
    async with emitter.substep(raw_name) as child_id:
        yield child_id


def begin_substeps(emitter: "PipelineEmitter | None", total: int | None) -> None:
    """No-op-safe wrapper over ``PipelineEmitter.begin_substeps``.

    Declares the planned child count without a None-check at the call site.
    """
    if emitter is None:
        return
    emitter.begin_substeps(total)


async def emit_chart_payloads(payloads: Any) -> None:
    """Side-emit one or more chart-emission payloads via the current emitter.
    Takes one payload, a sequence, or ``None``; a ``None`` entry is SKIPPED,
    because a builder returns one when the series it needs is absent.
    """
    emitter = current_emitter()
    if emitter is None:
        return
    if isinstance(payloads, (list, tuple)):
        items = list(payloads)
    else:
        items = [payloads]
    for payload in items:
        if isinstance(payload, dict) and payload:
            await emitter.emit_chart(payload)


logger = logging.getLogger("trid3nt_server.render.pipeline_emitter")


# The terminal pipeline-state send can raise ConnectionClosed* on a dead socket; only that class is swallowed so the
# transition completes. The import is defensive (an empty tuple) so the module imports without websockets.
try:  # pragma: no cover -- import shape, not behavior
    from websockets.exceptions import (
        ConnectionClosedError,
        ConnectionClosedOK,
    )

    _CONNECTION_CLOSED_EXC: tuple[type[BaseException], ...] = (
        ConnectionClosedError,
        ConnectionClosedOK,
    )
except Exception:  # pragma: no cover -- websockets absent in a minimal env
    _CONNECTION_CLOSED_EXC = ()


# A tool can fail or be cancelled and still return a value (a killed solver run returns a terminal RunResult); the
# return value is inspected so a dead solve does not paint a green card.

_FAILED_DICT_STATUSES = frozenset({"error", "failed", "cancelled"})


def _classify_tool_return(result: Any) -> tuple[str, str, str] | None:
    """Returns ``(terminal_state, error_code, error_message)`` or ``None`` for a healthy shape; anything ambiguous reads as success."""
    # Every shape keys off structure, never a raised exception. Shape 1: a RunResult-like object; non-"complete" is a
    # killed or timed-out run and "cancelled" maps to the cancelled card.
    status = getattr(result, "status", None)
    if (
        isinstance(status, str)
        and not isinstance(result, dict)
        and hasattr(result, "run_id")
        and hasattr(result, "handle_id")
    ):
        if status == "complete":
            return None
        code = (
            getattr(result, "error_code", None)
            or (status.upper() if status else "SOLVER_FAILED")
        )
        message = (
            getattr(result, "error_message", None)
            or getattr(result, "cancellation_reason", None)
            or f"solver run {status}"
        )
        terminal = "cancelled" if status == "cancelled" else "failed"
        return (terminal, str(code), str(message))

    # Shape 2: a dict whose ``status`` is a failed status.
    if isinstance(result, dict):
        dstatus = result.get("status")
        if isinstance(dstatus, str) and dstatus.lower() in _FAILED_DICT_STATUSES:
            code = result.get("error_code") or dstatus.upper()
            message = (
                result.get("error_message")
                or result.get("message")
                or result.get("error")
                or f"tool reported {dstatus}"
            )
            terminal = "cancelled" if dstatus.lower() == "cancelled" else "failed"
            return (terminal, str(code), str(message))

    return None


class EmitterError(RuntimeError):
    """Base class for emitter-internal errors. Distinct from tool errors."""


class StepNotFoundError(EmitterError):
    """``mark_*`` called with a step_id the emitter does not own."""


class NamelessFailureError(EmitterError):
    """A step marked failed with no code or sentence: a failure with no name is refused rather than painted red and mute."""


def _named(step_id: str, error_code: str, error_message: str) -> tuple[str, str]:
    """``(code, sentence)`` for a failure, or the refusal that it carries neither."""
    code = str(error_code or "").strip()
    sentence = str(error_message or "").strip()
    if not code or not sentence:
        raise NamelessFailureError(
            f"step {step_id!r} was marked failed carrying code {error_code!r} and "
            f"message {error_message!r}; a failed step states WHY it failed - a "
            "typed code and a sentence - and one that names an upstream service "
            "says so in both.")
    return code, sentence


#: The per-session sink the emitter pushes frames to; async so it can await ``websocket.send``.
EmissionSink = Callable[[str], Awaitable[None]]

#: Optional chart-persistence hook, closing over ``state``, so a composer-side ``emit_chart`` persists through the
#: same ``server._persist_chart_record`` the tool path uses.
ChartPersistHook = Callable[[dict], Awaitable[None]]

#: Optional hook persisting a terminal ``compute`` card as a ``role="tool"`` chat row through the same path a plain
#: tool card takes, so a reconnect replays the solve card. None on a send-only path.
ToolCardPersistHook = Callable[..., Awaitable[None]]


def _now() -> datetime:
    """UTC ``datetime`` factory. Tests can patch via ``PipelineEmitter._now_fn``."""
    return datetime.now(timezone.utc)


def _elapsed_ms(started_at: datetime | None, completed_at: datetime | None) -> int | None:
    """Whole milliseconds; ``None`` without both endpoints, clamped at 0 so clock skew never goes negative."""
    if started_at is None or completed_at is None:
        return None
    delta = (completed_at - started_at).total_seconds() * 1000.0
    if delta < 0:
        return 0
    return int(round(delta))


def summary_of(layer: LayerURI) -> ProjectLayerSummary:
    """THE mint: one client-bound ``LayerURI`` as the row a case and a session
    both carry. A layer reaches a case through this function whether a turn
    emitted it or a cold route registered it, so the two views cannot diverge."""
    return ProjectLayerSummary(
        layer_id=layer.layer_id,
        name=layer.name,
        layer_type=layer.layer_type,
        uri=layer.uri,
        visible=True,
        role=layer.role,
        origin=getattr(layer, "origin", None),
        temporal=layer.valid_from is not None,
        # Resolved style carry-over: the layer's own legend, else lifted from the stash by uri; None reaches the map unstyled.
        legend=getattr(layer, "legend", None) or _legend_for_layer_uri(layer.uri),
        # The quantity travels with the layer: matching a still to its animation by prose title ships two scales for one quantity.
        quantity=getattr(layer, "quantity", None),
        # And which tracer: position stays askable when a named release renames it.
        tracer=getattr(layer, "tracer", None),
        # Mesh CRS: an MDAL mesh reports an empty native crs(). None for raster/vector.
        crs_authid=getattr(layer, "crs_authid", None),
        # The dataset files a mesh row's derived groups were written to, and which group this row paints.
        dataset_uris=list(getattr(layer, "dataset_uris", None) or ()),
        dataset_group=(getattr(layer, "style", None) or {}).get("dataset_group"),
        # The instant the mesh's seconds are counted from.
        reference_time=getattr(layer, "reference_time", None),
        # One frame states its own validity window so the map stamps a fixed temporal range.
        valid_from=getattr(layer, "valid_from", None),
        valid_to=getattr(layer, "valid_to", None),
    )


def _legend_for_layer_uri(uri: str | None) -> Any:
    """Lift the stashed legend for a layer's uri; fail-open, an error leaves the layer unstyled rather than absent."""
    if not uri:
        return None
    try:
        from trid3nt_server.render.publish import pop_legend_for_uri

        return pop_legend_for_uri(uri)
    except Exception:  # noqa: BLE001 - legend lift is best-effort, never fatal
        return None


@dataclass
class _StepState:
    """Mutable record for one step, materialized into the wire and persistence shapes on demand."""

    step_id: str
    name: str
    tool_name: str
    state: str = "pending"
    started_at: datetime | None = None
    completed_at: datetime | None = None
    progress_percent: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    #: Authoritative elapsed milliseconds stamped on the terminal transition; None while pending/running.
    duration_ms: int | None = None
    #: Card kind: ``"tool"`` is the plain atomic-tool card, ``"compute"`` the solver card bound to a dispatched run.
    #: ``batch_status`` mirrors the backend's last-polled status verbatim. Both ids are None on a plain card.
    role: str = "tool"
    batch_job_id: str | None = None
    batch_status: str | None = None
    #: Engine and module this compute card is a run of; both None on a plain tool card.
    engine: str | None = None
    module: str | None = None
    #: The stable persisted ``message_id`` of this step's card row: set on a compute step at mint so the running write and
    #: the terminal write upsert the same row. None for a plain card, which persists once at terminal.
    card_message_id: str | None = None
    #: Nested sub-step timeline: ``parent_step_id`` marks a child the client nests under the parent; the ``substep_*``
    #: fields sit on the parent for the live breadcrumb ("fetching topobathy 2/7") and clear at its terminal transition.
    parent_step_id: str | None = None
    substep_label: str | None = None
    substep_index: int | None = None
    substep_total: int | None = None
    #: Parent-only count of substeps started; drives ``substep_index``, never serialized directly.
    substep_started_count: int = 0


class PipelineEmitter:
    """Owns one session's pipeline snapshot and loaded_layers accumulator.
    Replace-not-reconcile, structurally: every ``_emit_*`` call serializes the
    FULL current snapshot, and there is no partial-update method to call instead.
    """

    #: Maximum error_message length; the schema enforces it and the emitter truncates defensively.
    ERROR_MESSAGE_MAX_LEN = 512

    #: Time factory; patched by tests for deterministic timestamps.
    _now_fn: Callable[[], datetime] = staticmethod(_now)

    def __init__(
        self,
        session_id: str,
        sink: EmissionSink,
        *,
        chat_history: list[dict] | None = None,
        pipeline_history: list[dict] | None = None,
        map_view: dict | None = None,
        chart_persist: "ChartPersistHook | None" = None,
        tool_card_persist: "ToolCardPersistHook | None" = None,
    ) -> None:
        self.session_id = session_id
        self._sink = sink

        #: Optional async hook so a composer-side ``emit_chart`` persists through the tool path's record. None on a send-only path.
        self._chart_persist: "ChartPersistHook | None" = chart_persist

        #: Optional async hook persisting a terminal compute card as a ``role="tool"`` chat row, so it round-trips the chat-history replay.
        self._tool_card_persist: "ToolCardPersistHook | None" = tool_card_persist

        self._pipeline_id: str | None = None
        self._pipeline_started_at: datetime | None = None

        self._steps: dict[str, _StepState] = {}
        self._step_order: list[str] = []

        self._chat_history: list[dict] = list(chat_history or [])
        self._pipeline_history: list[dict] = list(pipeline_history or [])
        self._map_view: dict | None = map_view

        self._loaded_layers: list[ProjectLayerSummary] = []

        #: The loop this emitter is bracketed on, so a coroutine can be driven from an off-loaded sync fetcher's worker thread; None until the first dispatch.
        self._bound_loop: "asyncio.AbstractEventLoop | None" = None

        #: Input uris already surfaced this session; a fetched input is surfaced once even if several composers re-fetch it.
        self._emitted_input_uris: set[str] = set()

        #: Monotonic stacking counter stamped on every new layer. An in-place replace reuses the superseded layer's
        #: z_index; a reseed advances the counter past every seeded z_index.
        self._next_z: int = 0

        #: Terminal summary of the most recent dispatched step, carrying the authoritative ``started_at``/``duration_ms`` so a
        #: persisted card records the live card's duration. Read-only outside terminal transitions.
        self.last_tool_step: PipelineStepSummary | None = None

        #: Ordered child substeps of the most recent parent, captured at the terminal points while they still exist: the
        #: pipeline is cleared before the persist hook runs. [] with no children.
        self.last_tool_children: list[PersistedSubStepRecord] = []

        #: The top-level step bracketing a dispatch; a substep mints its child against it. None outside a dispatched body.
        self._current_parent_step_id: str | None = None

        #: The most recent terminal pipeline-state payload, replayed onto a reconnected socket; None until the first terminal transition.
        self._last_terminal_pipeline_payload: PipelineStatePayload | None = None


    def seed_chat_history(self, history: list[dict]) -> None:
        """Replace the chat-history mirror this emitter ships in session-state.
        A defensive COPY: the caller's list cannot later mutate the mirror. An
        emitter that is never seeded keeps the history it was constructed with.
        """
        self._chat_history = list(history or [])


    def rebind_sink(self, sink: EmissionSink) -> None:
        """Swap the wire sink this emitter pushes frames to, and replay onto it.
        Never raises out of the rebind: the replay is best-effort, because the new
        socket may itself have just cycled.
        """
        # The sink closes over the socket that launched the turn and silently drops frames once it dies; rebinding puts progress
        # and the terminal frame back on the live connection. An open pipeline replays a full snapshot of every step (the
        # terminal stash holds only the last terminal card); the stash is the fallback when none is open.
        self._sink = sink
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop: the snapshot stays stashed and a loop-bound rebind replays it.
            return
        if self._pipeline_id is not None and self._step_order:
            # An open pipeline: replay the full snapshot via _to_wire_step over _step_order so dropped running cards repaint on the new socket.
            snapshot = PipelineStatePayload(
                pipeline_id=self._pipeline_id,
                steps=[self._to_wire_step(sid) for sid in self._step_order],
            )
            loop.create_task(self._send_pipeline_state(snapshot))
            return
        # No open pipeline: fall back to the last terminal stash.
        terminal = self._last_terminal_pipeline_payload
        if terminal is None:
            return
        loop.create_task(self._send_pipeline_state(terminal))

    async def _send_pipeline_state(self, payload: PipelineStatePayload) -> None:
        """Send one pipeline-state frame; only a closed socket is swallowed, since the state is recorded and a rebind replays
        the snapshot (the client replaces a pipeline by ``pipeline_id``, so a replay is idempotent)."""
        try:
            await self._send("pipeline-state", payload)
        except _CONNECTION_CLOSED_EXC:  # type: ignore[misc]
            logger.debug(
                "emitter: pipeline-state send failed on a closed socket "
                "(best-effort drop; replays on rebind) session=%s pipeline_id=%s",
                self.session_id,
                self._pipeline_id,
            )


    @property
    def loaded_layers(self) -> list[ProjectLayerSummary]:
        """Return a defensive shallow copy of the current loaded_layers list."""
        return list(self._loaded_layers)

    async def _patch_layer(self, layer_id: str, field: str, value: Any) -> bool:
        """False when this session never loaded that layer: patching what nobody published is a refusal."""
        for summary in self._loaded_layers:
            if summary.layer_id == layer_id:
                if getattr(summary, field) != value:
                    setattr(summary, field, value)
                    await self.emit_session_state()
                return True
        return False

    async def set_layer_visible(self, layer_id: str, visible: bool) -> bool:
        """Take a published layer off the canvas, or put it back."""
        return await self._patch_layer(layer_id, "visible", visible)

    async def set_layer_name(self, layer_id: str, name: str) -> bool:
        """Rename a published layer; the row carries the new name to the client."""
        return await self._patch_layer(layer_id, "name", name)

    def reset_loaded_layers(self, layers: list[dict] | None) -> None:
        """Replace the in-memory loaded layers from a persisted snapshot.
        ``None`` or ``[]`` FLUSHES. A malformed entry is skipped rather than
        rolled back, and nothing is emitted: the caller chooses when to send.
        """
        if not layers:
            self._loaded_layers = []
            # A flush (new Case) restarts the stacking counter.
            self._next_z = 0
            return
        seeded: list[ProjectLayerSummary] = []
        for layer_dict in layers:
            if not isinstance(layer_dict, dict):
                continue
            try:
                seeded.append(ProjectLayerSummary.model_validate(layer_dict))
            except Exception:  # noqa: BLE001
                logger.warning(
                    "reset_loaded_layers: skipping malformed layer dict"
                )
                continue
        self._loaded_layers = seeded
        # Resume the counter past any seeded slot so a later append cannot collide with a persisted z_index; no z_index leaves it at 0.
        _seeded_z = [s.z_index for s in seeded if s.z_index is not None]
        self._next_z = (max(_seeded_z) + 1) if _seeded_z else 0

    def merge_loaded_layers_from(self, other: "PipelineEmitter") -> int:
        """Union ``other``'s in-memory loaded layers into THIS emitter.
        Union by uri and ``layer_id``, so a layer this emitter already holds is
        never duplicated. Returns the count of newly-merged layers.
        """
        # A rebind replays pipeline cards but not the loaded-layers frame, so a layer published while the launch socket was
        # dead was dropped; seeding from the live turn's emitter makes the reconnect's session-state carry it.
        if other is self or not other._loaded_layers:
            return 0
        existing_keys = {l.uri for l in self._loaded_layers}
        existing_ids = {l.layer_id for l in self._loaded_layers}
        merged = 0
        for layer in other._loaded_layers:
            key = layer.uri
            if key in existing_keys or layer.layer_id in existing_ids:
                continue
            new_layer = layer.model_copy(deep=True)
            if new_layer.z_index is None:
                new_layer.z_index = self._alloc_z()
            else:
                self._next_z = max(self._next_z, new_layer.z_index + 1)
            self._loaded_layers.append(new_layer)
            existing_keys.add(key)
            existing_ids.add(new_layer.layer_id)
            merged += 1
        return merged

    def current_snapshot(self) -> PipelineSnapshot | None:
        """The current ``PipelineSnapshot``, or ``None`` when none is running; always whole.
        
        The verdict refuses to read ``failed`` off a step that names no cause."""
        if self._pipeline_id is None or not self._step_order:
            return None
        final_state: str | None = None
        if all(self._steps[sid].state == "complete" for sid in self._step_order):
            final_state = "complete"
        elif any(self._steps[sid].state == "failed" for sid in self._step_order):
            final_state = "failed"
            for sid in self._step_order:
                step = self._steps[sid]
                if step.state == "failed":
                    _named(sid, step.error_code or "", step.error_message or "")
        elif any(self._steps[sid].state == "cancelled" for sid in self._step_order):
            final_state = "cancelled"
        completed_at = (
            self._now_fn() if final_state is not None else None
        )
        return PipelineSnapshot(
            pipeline_id=self._pipeline_id,
            started_at=self._pipeline_started_at or self._now_fn(),
            completed_at=completed_at,
            final_state=final_state,  # type: ignore[arg-type]
            steps=[self._to_summary(sid) for sid in self._step_order],
        )


    def start_pipeline(self) -> str:
        """Open a fresh pipeline. Returns the new ``pipeline_id``.
        Optional: ``add_step`` auto-opens one. Calling it explicitly stamps
        ``current_pipeline`` at a moment the caller chooses.
        """
        self._pipeline_id = new_ulid()
        self._pipeline_started_at = self._now_fn()
        self._steps.clear()
        self._step_order.clear()
        return self._pipeline_id

    def close_pipeline(self) -> None:
        """Archive the current snapshot into history and clear the pipeline.
        Idempotent. The closed snapshot lives on as a history entry, so a resume
        can replay it.
        """
        if self._pipeline_id is None:
            return
        snap = self.current_snapshot()
        if snap is not None:
            self._pipeline_history.append(snap.model_dump(mode="json"))
        self._pipeline_id = None
        self._pipeline_started_at = None
        self._steps.clear()
        self._step_order.clear()

    async def add_step(self, name: str, tool_name: str) -> str:
        """Append a new ``pending`` step and emit a fresh pipeline-state.

        Auto-opens a pipeline when none is open. Returns the new ``step_id``.
        """
        if self._pipeline_id is None:
            self.start_pipeline()
        step_id = new_ulid()
        self._steps[step_id] = _StepState(
            step_id=step_id, name=name, tool_name=tool_name
        )
        self._step_order.append(step_id)
        await self._emit_pipeline_state()
        return step_id

    async def mark_running(
        self, step_id: str, *, progress_percent: int | None = None
    ) -> None:
        """Flip ``step_id`` to ``running``, stamp ``started_at``, emit."""
        step = self._require_step(step_id)
        step.state = "running"
        step.started_at = self._now_fn()
        if progress_percent is not None:
            step.progress_percent = self._coerce_progress(progress_percent)
        await self._emit_pipeline_state()

    async def update_progress(self, step_id: str, progress_percent: int) -> None:
        """Bump ``progress_percent`` on a running step; emit.
        The value is workflow-attributed, passed in by the caller, and is never
        an estimate the model produced.
        """
        step = self._require_step(step_id)
        step.progress_percent = self._coerce_progress(progress_percent)
        await self._emit_pipeline_state()


    def begin_substeps(self, total: int | None) -> None:
        """Declare the planned child count for the live breadcrumb.
        ``None``, or a non-positive count, degrades the breadcrumb to label plus
        index. Emits nothing itself: the plan rides the next transition.
        """
        parent_id = self._current_parent_step_id
        if parent_id is None:
            return
        parent = self._steps.get(parent_id)
        if parent is None:
            return
        if total is not None and isinstance(total, int) and total >= 1:
            parent.substep_total = int(total)
        else:
            parent.substep_total = None

    @asynccontextmanager
    async def substep(self, raw_name: str):
        """Surface ONE internal call as a CHILD step under the current parent.
        Yields the child ``step_id``, or ``None`` when no parent is bound, in
        which case nothing is minted. Any exception is re-raised unchanged.
        """
        parent_id = self._current_parent_step_id
        if parent_id is None:
            yield None
            return
        parent = self._steps.get(parent_id)
        if parent is None:
            yield None
            return

        child_id = await self.add_step(name=raw_name, tool_name=raw_name)
        child = self._steps[child_id]
        child.parent_step_id = parent_id
        parent.substep_started_count += 1
        parent.substep_label = raw_name
        parent.substep_index = parent.substep_started_count
        # One pipeline-state frame carries both the running child and the parent's breadcrumb, never split in two.
        await self.mark_running(child_id)

        try:
            yield child_id
        except asyncio.CancelledError:
            # Cancelled is distinct from failed: the child is marked cancelled and the error re-raised; the parent's breadcrumb stands.
            await self.mark_cancelled(child_id)
            raise
        except Exception as exc:  # noqa: BLE001 -- classify-and-re-raise
            code, message = self._classify_exception(exc)
            # A failed child is red; only the parent's own terminal transition may colour the parent.
            await self.mark_failed(child_id, error_code=code, error_message=message)
            raise
        else:
            await self.mark_complete(child_id)


    async def add_compute_step(
        self,
        *,
        name: str,
        tool_name: str,
        batch_job_id: str,
        batch_status: str | None = None,
        engine: str | None = None,
        module: str | None = None,
    ) -> str:
        """Append a ``role="compute"`` step bound to a dispatched solver run; emit.
        Lands RUNNING immediately, so the card shows motion while the solver runs
        unheard from. ``batch_status`` mirrors the backend verbatim, never an estimate.
        """
        step_id = await self.add_step(name=name, tool_name=tool_name)
        step = self._require_step(step_id)
        step.role = "compute"
        step.batch_job_id = batch_job_id
        step.batch_status = batch_status
        step.engine = engine
        step.module = module
        # Pin the stable row id now so the running write and the terminal write upsert the same row.
        step.card_message_id = new_ulid()
        await self.mark_running(step_id)
        return step_id

    async def add_durable_step(self, *, name: str, tool_name: str) -> str:
        """Append a ``role="tool"`` step that starts RUNNING and is durable.
        For a server-internal action with NO dispatched run to bind to, so it
        carries none of the compute card's run fields.
        """
        step_id = await self.add_step(name=name, tool_name=tool_name)
        step = self._require_step(step_id)
        # Pin the stable row id now so the running write and the terminal write upsert the same row.
        step.card_message_id = new_ulid()
        await self.mark_running(step_id)
        return step_id

    def rename_step(self, step_id: str, *, name: str) -> None:
        """Patch a step's display ``name`` in place; a no-op on an unknown id.
        For a card whose terminal label depends on data known only once the work
        completes; the client renders ``name`` verbatim and never rephrases it.
        """
        step = self._steps.get(step_id)
        if step is None:
            return
        step.name = name

    def _clear_parent_breadcrumb(self, step: _StepState) -> None:
        step.substep_label = None
        step.substep_index = None
        step.substep_total = None

    def _mark_terminal(self, step_id: str, state: str) -> _StepState:
        """Flip ``step_id`` to a terminal state; ``duration_ms`` is the authoritative figure the client locks its ticker to,
        ``None`` for a step that failed before it ran."""
        step = self._require_step(step_id)
        step.state = state
        step.completed_at = self._now_fn()
        self._clear_parent_breadcrumb(step)
        step.duration_ms = _elapsed_ms(step.started_at, step.completed_at)
        return step

    async def mark_complete(self, step_id: str) -> None:
        """Flip ``step_id`` to ``complete``, stamp ``completed_at``, emit."""
        self._mark_terminal(step_id, "complete")
        await self._emit_pipeline_state(terminal=True)

    async def mark_failed(
        self, step_id: str, error_code: str, error_message: str
    ) -> None:
        """Flip ``step_id`` to ``failed`` with a truncated message; a failure with no code or no sentence is refused."""
        code, sentence = _named(step_id, error_code, error_message)
        step = self._mark_terminal(step_id, "failed")
        step.error_code = code
        step.error_message = self._truncate_message(sentence)
        await self._emit_pipeline_state(terminal=True)

    async def mark_cancelled(self, step_id: str) -> None:
        """Flip ``step_id`` to ``cancelled``; emit.
        A cancelled step is DISTINCT from a failed one, and the cancel chain
        calls this before the ``asyncio.CancelledError`` is re-raised.
        """
        self._mark_terminal(step_id, "cancelled")
        await self._emit_pipeline_state(terminal=True)

    async def _persist_step_card(
        self, step_id: str, *, states: tuple[str, ...]
    ) -> None:
        """Persist the step's tool-card row iff its state is in ``states``; a hook failure is swallowed."""
        # ``card_message_id`` is the stable row id so running and terminal writes upsert one row; a step without one is appended.
        if self._tool_card_persist is None:
            return
        step = self._steps.get(step_id)
        if step is None:
            return
        if step.state not in states:
            return
        started_at = step.started_at or self._now_fn()
        duration_ms = step.duration_ms if step.duration_ms is not None else 0
        try:
            await self._tool_card_persist(
                tool_name=step.tool_name,
                label=step.name,
                card_state=step.state,
                started_at_fallback=started_at,
                duration_ms_fallback=duration_ms,
                message_id=step.card_message_id,
            )
        except Exception as exc:  # noqa: BLE001 -- persistence, never break solve
            logger.warning(
                "_persist_step_card failed (non-fatal) step=%s: %s",
                step_id,
                exc,
            )

    async def persist_running_compute_card(self, step_id: str) -> None:
        """Persist the compute card the MOMENT it is minted, still running.
        Without a running row, a reconnect mid-solve replays an empty pipeline and
        the spinning card vanishes. A no-op outside the ``running`` state.
        """
        await self._persist_step_card(step_id, states=("running",))

    async def persist_terminal_compute_card(self, step_id: str) -> None:
        """Drive the compute card's persisted row to its TERMINAL state.
        UPSERTS the row minted at running, ``cancelled`` included: a stopped solve
        is a finished solve, and leaving an orphaned running row would deny it.
        """
        await self._persist_step_card(
            step_id, states=("complete", "failed", "cancelled")
        )

    async def persist_terminal_dispatch_card(self, step_id: str) -> None:
        """Persist the DISPATCH card: terminal, appended once.
        It carries no stable row id - it completes instantly at mint - so this
        appends one durable row, pairing it with the compute card on a replay.
        """
        await self._persist_step_card(step_id, states=("complete", "failed"))


    def _alloc_z(self) -> int:
        """The single source of new stacking slots, so no in-use slot is reissued."""
        z = self._next_z
        self._next_z += 1
        return z

    async def add_loaded_layer(self, layer: LayerURI) -> None:
        """Append a ``LayerURI`` to ``loaded_layers`` and emit a fresh frame, deduped by what the row paints.
        
        Two publishes of one COG name the same uri, so the fresher row replaces the older; a mesh row is its uri and its group."""
        summary = summary_of(layer)
        for i, existing in enumerate(self._loaded_layers):
            if (existing.uri, existing.dataset_group) == (summary.uri,
                                                          summary.dataset_group):
                # Reuse the superseded layer's slot so a re-publish keeps its stacking position; a fresh slot only if the old row carried none.
                summary.z_index = (
                    existing.z_index
                    if existing.z_index is not None
                    else self._alloc_z()
                )
                self._loaded_layers[i] = summary
                break
        else:
            # A new layer takes the next slot; highest z_index is the most recently added.
            summary.z_index = self._alloc_z()
            self._loaded_layers.append(summary)
        await self.emit_session_state()
        if layer.bbox is not None:
            await self.emit_map_command(
                "zoom-to",
                {"bbox": list(layer.bbox)},
            )

    async def emit_session_state(self) -> None:
        """Emit a full ``session-state`` envelope.
        A vector row carries only its store uri: the dock streams it from there.
        """
        snap = self.current_snapshot()
        payload = SessionStatePayload(
            chat_history=list(self._chat_history),
            loaded_layers=[l.model_dump(mode="json") for l in self._loaded_layers],
            pipeline_history=list(self._pipeline_history),
            current_pipeline=(snap.model_dump(mode="json") if snap is not None else None),
            map_view=self._map_view,
        )
        await self._send("session-state", payload)

    async def emit_map_command(self, command: str, args: dict) -> None:
        """Emit a ``map-command`` envelope: a TRANSIENT verb, never state.
        Anything that changes the layer set rides ``session-state`` instead, so a
        replayed command can never resurrect a layer.
        """
        payload = MapCommandPayload(command=command, args=args)  # type: ignore[arg-type]
        await self._send("map-command", payload)

    async def emit_solve_progress(self, progress: dict) -> None:
        """Emit a ``solve-progress`` envelope: live telemetry off a running solve.
        Best-effort - a malformed dict is logged and dropped, because telemetry is
        a hint on a card and never a correctness gate.
        """
        try:
            payload = SolveProgressPayload(**progress)
        except Exception as exc:  # noqa: BLE001 -- never break the solve loop
            logger.warning("emit_solve_progress: bad payload dropped: %s", exc)
            return
        await self._send("solve-progress", payload)

    async def emit_chart(self, chart_payload: dict) -> None:
        """Emit a ``chart-emission`` envelope from a workflow body, and persist it.
        An unset ``created_turn_id`` is stamped from the turn, so charts from one
        turn group together. Best-effort: a failure is logged and dropped.
        """
        if not isinstance(chart_payload, dict) or not chart_payload:
            return
        payload = dict(chart_payload)
        if not payload.get("created_turn_id"):
            payload["created_turn_id"] = self._pipeline_id or self.session_id
        # A hand-built dict: the typed envelope's payload is extra="forbid" and rejects a raw chart payload. Byte-identical to the tool path's send plus the Case tag.
        frame = {
            "type": "chart-emission",
            "session_id": self.session_id,
            "case_id": current_turn_case(),
            "payload": payload,
        }
        try:
            await self._sink(json.dumps(frame))
            logger.info(
                "chart-emission emitted (composer) session=%s chart_id=%s title=%r",
                self.session_id,
                payload.get("chart_id"),
                payload.get("title"),
            )
        except Exception:  # noqa: BLE001 - side effect, never bubble up
            logger.warning(
                "composer chart-emission send failed session=%s chart_id=%s",
                self.session_id,
                payload.get("chart_id"),
                exc_info=True,
            )
        # Best-effort persist so the chart replays on rehydration, through the tool path's record.
        if self._chart_persist is not None:
            try:
                await self._chart_persist(payload)
            except Exception:  # noqa: BLE001 - persistence must not break the loop
                logger.warning(
                    "composer chart persistence failed session=%s chart_id=%s",
                    self.session_id,
                    payload.get("chart_id"),
                    exc_info=True,
                )

    async def emit_tool_io(
        self,
        *,
        step_id: str,
        tool_name: str,
        raw_args: Any,
        function_response: Any,
        is_error: bool = False,
    ) -> None:
        """Emit a ``tool-io`` envelope: the RAW args and response for one dispatch.
        Both are stringified and TRUNCATED, with the original byte length riding
        along. Best-effort: a failure is logged and dropped.
        """
        try:
            args_str, args_trunc, args_bytes = ToolIoPayload.json_field(raw_args)
            resp_str, resp_trunc, resp_bytes = ToolIoPayload.json_field(function_response)
            payload = ToolIoPayload(
                step_id=step_id,
                tool_name=tool_name,
                raw_args=args_str,
                function_response=resp_str,
                is_error=bool(is_error),
                args_truncated=args_trunc,
                response_truncated=resp_trunc,
                args_bytes=args_bytes,
                response_bytes=resp_bytes,
            )
        except Exception as exc:  # noqa: BLE001 -- never break the dispatch loop
            logger.warning("emit_tool_io: bad payload dropped: %s", exc)
            return
        await self._send("tool-io", payload)

    async def _emit_one_layer(self, layer: LayerURI) -> None:
        """Publish, guard, then track one client-bound layer. Never raises."""
        try:
            layer = await publish_for_emission(layer)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - enrichment is never fatal
            logger.exception(
                "emit_tool_call: auto-publish raised for layer_id=%s; emitting "
                "the layer as the tool returned it.", layer.layer_id,
            )
        emit_layer = emit_layer_uri(layer)
        if emit_layer is not None:
            await self.add_loaded_layer(emit_layer)

    async def emit_tool_call(
        self,
        *,
        name: str,
        tool_name: str,
        invoke: Callable[[], Any] | Callable[[], Awaitable[Any]],
    ) -> Any:
        """Wrap a single tool invocation with pipeline-state emission.
        Returns the tool's own result untouched, and re-raises every exception
        after marking the card - cancelled and failed stay distinct.
        """
        step_id = await self.add_step(name=name, tool_name=tool_name)
        await self.mark_running(step_id)
        # Children accumulate fresh per dispatch so a prior dispatch's substeps cannot leak onto this card.
        self.last_tool_children = []
        # Bind self as the active emitter; the token unwinds the binding exactly once, on cancellation and exception paths too.
        token = _CURRENT_EMITTER.set(self)
        # Bind the dispatched tool name and the running loop, so a nested seam can tell direct from in-composer dispatch and drive coroutines back onto this loop.
        _disp_token = _DISPATCHED_TOOL.set(tool_name)
        self._bound_loop = asyncio.get_running_loop()
        # Restore the previous parent on nested invocation exit.
        _prev_parent = self._current_parent_step_id
        self._current_parent_step_id = step_id
        try:
            try:
                result = invoke()
                if asyncio.iscoroutine(result):
                    result = await result
            except asyncio.CancelledError:
                await self.mark_cancelled(step_id)
                # Record the terminal step even on a cancel, so the accessor never carries a stale prior step.
                self.last_tool_step = self._to_summary(step_id)
                self.last_tool_children = self._collect_children(step_id)
                raise
            except Exception as exc:  # noqa: BLE001 -- classify-and-re-raise
                # A declined gate card is a decision, not a fault: the card ends cancelled with no error code.
                if getattr(exc, "declined", False):
                    await self.mark_cancelled(step_id)
                else:
                    code, message = self._classify_exception(exc)
                    await self.mark_failed(
                        step_id, error_code=code, error_message=message
                    )
                self.last_tool_step = self._to_summary(step_id)
                # A failed parent card is persisted, so its children are snapshotted for the replayed card's nested timeline.
                self.last_tool_children = self._collect_children(step_id)
                raise
            # The terminal frame goes out before the layer's session-state emission so that frame carries the terminal state.
            # A tool can fail or be cancelled and still return, so the return value decides the card state.
            terminal = _classify_tool_return(result)
            if terminal is not None:
                state, error_code, error_message = terminal
                if state == "cancelled":
                    await self.mark_cancelled(step_id)
                else:
                    await self.mark_failed(
                        step_id,
                        error_code=error_code,
                        error_message=error_message,
                    )
                logger.info(
                    "emit_tool_call: tool %r RETURNED a non-success terminal "
                    "outcome state=%s code=%s; card marked %s (not complete)",
                    tool_name, state, error_code, state,
                )
            else:
                await self.mark_complete(step_id)
            self.last_tool_step = self._to_summary(step_id)
            # Snapshot the ordered children before the pipeline is closed and the steps cleared; the persisted card carries them.
            self.last_tool_children = self._collect_children(step_id)
            # LayerURI returns append to loaded_layers and emit session-state after the terminal frame, so the snapshot never shows
            # the step running. The emission seam drops a renderable raster with a raw gs:// uri (the publish-failure path) so no
            # broken layer row paints; the tool result is unaffected. Auto-emit: a raw s3:// COG raster is published here
            # (overviews, style, legend) with no per-tool call site and no opt-out; a list of layers takes the same trip per layer.
            if isinstance(result, LayerURI):
                await self._emit_one_layer(result)
            elif isinstance(result, list) and any(
                isinstance(item, LayerURI) for item in result
            ):
                for item in result:
                    if isinstance(item, LayerURI):
                        await self._emit_one_layer(item)
            return result
        finally:
            _CURRENT_EMITTER.reset(token)
            _DISPATCHED_TOOL.reset(_disp_token)
            # Restore the previous parent pointer; None on the single top-level path.
            self._current_parent_step_id = _prev_parent


    def _classify_exception(self, exc: Exception) -> tuple[str, str]:
        """Deliberately conservative: an ambiguous shape buckets into ``INTERNAL_ERROR`` rather than a fabricated code."""
        from trid3nt_server.tools.fetchers._fetch_common import UpstreamAPIError

        message = str(exc) or exc.__class__.__name__
        # An upstream provider's failure keeps the upstream code, the source's code leading the provider's message.
        if isinstance(exc, UpstreamAPIError):
            return ("UPSTREAM_API_ERROR", f"[{exc.error_code}] {message}")
        # Order matters: most specific subclass first.
        if isinstance(exc, ValueError) and "bbox" in message.lower():
            return ("BBOX_INVALID", message)
        if isinstance(exc, TimeoutError) or isinstance(
            exc, asyncio.TimeoutError
        ):  # pragma: no cover -- Py3.11+ aliases
            return ("UPSTREAM_API_ERROR", f"upstream timeout: {message}")
        if isinstance(exc, ConnectionError):
            return ("UPSTREAM_API_ERROR", message)
        if isinstance(exc, LookupError) and "geocode" in message.lower():
            return ("GEOCODE_NO_MATCH", message)
        if isinstance(exc, KeyError) and "tool" in message.lower():
            return ("TOOL_NOT_FOUND", message)
        if isinstance(exc, TypeError) or isinstance(exc, ValueError):
            return ("TOOL_PARAMS_INVALID", message)
        return ("INTERNAL_ERROR", message)

    def _require_step(self, step_id: str) -> _StepState:
        step = self._steps.get(step_id)
        if step is None:
            raise StepNotFoundError(
                f"step_id {step_id!r} not registered with this emitter"
            )
        return step

    @staticmethod
    def _coerce_progress(value: int) -> int:
        if value < 0 or value > 100:
            raise ValueError(
                f"progress_percent must be in [0,100]; got {value!r}"
            )
        return int(value)

    @classmethod
    def _truncate_message(cls, message: str) -> str:
        if len(message) <= cls.ERROR_MESSAGE_MAX_LEN:
            return message
        return message[: cls.ERROR_MESSAGE_MAX_LEN]

    def _step_fields(self, step_id: str) -> dict[str, Any]:
        """The fields a wire step and a persisted summary both carry; ``parent_step_id`` rides a child, the breadcrumb trio the parent."""
        s = self._steps[step_id]
        return {
            "step_id": s.step_id,
            "name": s.name,
            "tool_name": s.tool_name,
            "state": s.state,
            "started_at": s.started_at,
            "completed_at": s.completed_at,
            "progress_percent": s.progress_percent,
            "error_code": s.error_code,
            "error_message": s.error_message,
            "duration_ms": s.duration_ms,
            "role": s.role,
            "batch_job_id": s.batch_job_id,
            "batch_status": s.batch_status,
            "parent_step_id": s.parent_step_id,
            "substep_label": s.substep_label,
            "substep_index": s.substep_index,
            "substep_total": s.substep_total,
        }

    def _to_wire_step(self, step_id: str) -> PipelineStep:
        s = self._steps[step_id]
        return PipelineStep(
            **self._step_fields(step_id), engine=s.engine, module=s.module
        )

    def _to_summary(self, step_id: str) -> PipelineStepSummary:
        return PipelineStepSummary(**self._step_fields(step_id))

    def _collect_children(self, parent_step_id: str) -> list[PersistedSubStepRecord]:
        """Terminal (complete and failed) children in start order; must be called while they still exist."""
        # A cancelled or still-running child persists nothing, like the parent card; a failed child carries its code and
        # message. Child tool-io is not captured, so those fields stay None.
        out: list[PersistedSubStepRecord] = []
        for sid in self._step_order:
            child = self._steps.get(sid)
            if child is None or child.parent_step_id != parent_step_id:
                continue
            if child.state not in ("complete", "failed"):
                continue
            out.append(
                PersistedSubStepRecord(
                    step_id=child.step_id,
                    parent_step_id=child.parent_step_id,
                    name=child.name,
                    tool_name=child.tool_name,
                    state=child.state,  # type: ignore[arg-type]
                    duration_ms=child.duration_ms,
                    error_code=child.error_code,
                    error_message=child.error_message,
                )
            )
        return out

    async def _emit_pipeline_state(self, *, terminal: bool = False) -> None:
        """Send the full snapshot for a step transition; a terminal one is also stashed for replay. No open pipeline is a call-site error, not papered over with an empty snapshot."""
        if self._pipeline_id is None:
            raise EmitterError(
                "_emit_pipeline_state called with no open pipeline; "
                "call start_pipeline / add_step first"
            )
        payload = PipelineStatePayload(
            pipeline_id=self._pipeline_id,
            steps=[self._to_wire_step(sid) for sid in self._step_order],
        )
        if terminal:
            self._last_terminal_pipeline_payload = payload
        await self._send_pipeline_state(payload)

    async def send_envelope(self, message_type: str, payload: Any) -> None:
        """Emit ONE arbitrary typed envelope on this session's sink.
        For a sender whose message is outside the pipeline-step vocabulary; it is
        framed identically, stamped with the session and the owning Case.
        """
        await self._send(message_type, payload)

    async def _send(self, message_type: str, payload: Any) -> None:
        env = Envelope(
            type=message_type,
            session_id=self.session_id,
            # Stamp the owning Case so the client routes to the right stream after a mid-turn switch.
            case_id=current_turn_case(),
            payload=payload,
        )
        await self._sink(env.model_dump_json())
        logger.debug(
            "emitter session=%s type=%s pipeline_id=%s steps=%d layers=%d",
            self.session_id,
            message_type,
            self._pipeline_id,
            len(self._step_order),
            len(self._loaded_layers),
        )
