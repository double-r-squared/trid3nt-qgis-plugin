"""Run a generated script with an already reviewed deck from the daemon."""

from __future__ import annotations

import asyncio
import io
import os
import pickle
import runpy
import signal
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from multiprocessing.connection import Connection
from types import MappingProxyType
from typing import Any

from trid3nt.errors import DeclarationError
from trid3nt.inputs import slots
from trid3nt.workflows.runtime import module
from trid3nt.workflows.solver.stop import Stopped

_ACTIVE: ContextVar[ReviewedRun | None] = ContextVar("reviewed_script_run", default=None)


def _slots() -> dict[str, slots.Slot]:
    return {value.name: value for value in vars(slots).values() if isinstance(value, slots.Slot)}


class _DeckWriter(pickle.Pickler):
    """Keep references to the pinned template and to the SDK's shared declarations."""

    def __init__(self, file: io.BytesIO, workflow: Any) -> None:
        super().__init__(file, protocol=pickle.HIGHEST_PROTOCOL)
        self.workflow = workflow

    def persistent_id(self, item: Any) -> tuple[str, Any] | None:
        if item is self.workflow:
            return ("workflow", None)
        if isinstance(item, slots.Slot):
            return ("slot", item.name)
        if item is module.UNSET:
            return ("unset", None)
        if isinstance(item, type) and hasattr(item, "MODULE_INPUT"):
            steering = self.workflow.steering
            if item is steering or item.__name__ == steering.__name__:
                return ("steering", item.MODULE)
        return None

    def reducer_override(self, obj: Any) -> Any:
        if isinstance(obj, MappingProxyType):
            return (_readonly, (dict(obj),))
        return NotImplemented


def _readonly(values: dict) -> MappingProxyType:
    return MappingProxyType(values)


class _DeckReader(pickle.Unpickler):
    def __init__(self, file: io.BytesIO, workflow: Any) -> None:
        super().__init__(file)
        self.workflow = workflow
        self.slots = _slots()

    def persistent_load(self, key: tuple[str, Any]) -> Any:
        kind, name = key
        if kind == "workflow":
            return self.workflow
        if kind == "slot":
            return self.slots[name]
        if kind == "unset":
            return module.UNSET
        if kind == "steering":
            steering = self.workflow.steering
            if steering.MODULE == name:
                return steering
            from trid3nt.workflows.geoclaw.modules import body_on

            return body_on(steering, name)
        raise pickle.UnpicklingError("Unknown reviewed deck reference.")


def save_deck(deck: Any) -> bytes:
    """Encode typed launch state for a privately inherited process channel."""
    data = io.BytesIO()
    _DeckWriter(data, deck.state.workflow).dump(deck)
    return data.getvalue()


def load_deck(data: bytes, workflow: Any) -> Any:
    """Restore the launch using the workflow loaded from the script's template pin."""
    return _DeckReader(io.BytesIO(data), workflow).load()


@dataclass
class ReviewedRun:
    channel: Connection
    handoff: dict[str, Any]
    record: dict[str, Any] | None = None
    value: Any = None
    failure: tuple[str, str] | None = None
    checked: bool = False

    def event(self, kind: str, *values: Any) -> None:
        self.channel.send((kind, *values))

    def verify(self) -> None:
        self.checked = True
        self.event("checked")

    def receipt(self) -> dict[str, Any]:
        return {**self.handoff["receipt"], "code": "CODEGEN_CHECKED", "checked": self.checked}


def current() -> ReviewedRun | None:
    return _ACTIVE.get()


@contextmanager
def interruptible():
    """Let the existing cancelled-task solver path kill the box and finish its record."""
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    loop.add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        yield
    finally:
        loop.remove_signal_handler(signal.SIGTERM)


def main() -> None:
    """Bootstrap the private channel before running the same script a person downloads."""
    descriptor = int(sys.argv[1])
    os.set_inheritable(descriptor, False)
    with Connection(descriptor) as channel:
        active = ReviewedRun(channel, channel.recv())
        _ACTIVE.set(active)
        from trid3nt.tools.mesh.session import _limit_to_cores

        _limit_to_cores(active.handoff["cores"])
        try:
            runpy.run_path("case.py", run_name="__main__")
        except Stopped as stop:
            active.event("stopped", stop.value, stop.why, active.record)
        except DeclarationError as exc:
            active.event("refused", exc.error_code)
        except SystemExit as exc:
            if active.failure is not None:
                active.event("refused", *active.failure)
            else:
                active.event("refused", str(exc.code))
        else:
            active.event("complete", active.value, active.record)
