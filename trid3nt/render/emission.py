"""The emission hook: what a run hands the map, when something is bound to receive it.

The bindings are per-task context variables a host sets around one tool or workflow call; the SDK sets
none, so with nothing bound every accessor answers ``None`` and nothing reaches a map. Products are
still written and recorded exactly as they are with a receiver bound.
"""

from __future__ import annotations

import contextvars
from contextlib import asynccontextmanager
from typing import Any

__all__ = [
    "current_emitter",
    "dispatched_tool_name",
    "substep",
    "begin_substeps",
    "emit_chart_payloads",
    "bind_turn_case",
    "current_turn_case",
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

_CURRENT_EMITTER: contextvars.ContextVar[Any] = (
    contextvars.ContextVar("trid3nt_current_emitter", default=None)
)

#: The top-level tool ``emit_tool_call`` is dispatching. The emit-on-fetch seam compares it to the fetcher's name: equal
#: is a direct chat fetch (the tool wrapper already emits the layer, the seam stays silent), different is an in-composer
#: fetch (surfaced as a role="context" input). None outside ``emit_tool_call``.
_DISPATCHED_TOOL: contextvars.ContextVar[str | None] = (
    contextvars.ContextVar("trid3nt_dispatched_tool", default=None)
)


def current_emitter() -> Any:
    """Return the emitter bracketing the current tool/workflow call.
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
async def substep(emitter: Any, raw_name: str):
    """No-op-safe wrapper over the emitter's ``substep``.
    Yields ``None`` and mints nothing when no emitter is bound, so a body reads
    the same whether or not the timeline is being surfaced.
    """
    if emitter is not None:
        async with emitter.substep(raw_name) as child_id:
            yield child_id
        return
    from trid3nt.sdk.script_run import current

    child = current()
    if child is None:
        yield None
        return
    import asyncio

    child.event("stage-start", raw_name)
    try:
        yield None
    except asyncio.CancelledError:
        child.event("stage-end", "cancelled")
        raise
    except Exception as exc:
        child.event("stage-end", "failed", getattr(exc, "error_code", None), str(exc))
        raise
    else:
        child.event("stage-end", "complete")


def begin_substeps(emitter: Any, total: int | None) -> None:
    """No-op-safe wrapper over the emitter's ``begin_substeps``.

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
    from trid3nt.sdk.script_run import current

    child = current()
    if emitter is None and child is None:
        return
    if isinstance(payloads, (list, tuple)):
        items = list(payloads)
    else:
        items = [payloads]
    for payload in items:
        if isinstance(payload, dict) and payload:
            if child is not None:
                child.event("chart", payload)
            else:
                await emitter.emit_chart(payload)
