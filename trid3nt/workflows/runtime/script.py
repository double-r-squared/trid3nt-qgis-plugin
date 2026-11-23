"""Capture the evidence a live workflow host needs to publish its reviewed script."""

from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterator, Mapping

from trid3nt.tools.fetchers._router.router import bind_pin, reset_pin

logger = logging.getLogger(__name__)
_HOST: Callable[[], Callable[..., dict[str, Any]] | None] | None = None
_RUN: Callable[..., Awaitable[Executed]] | None = None
_CURRENT: ContextVar[Capture | None] = ContextVar("workflow_script_capture", default=None)


@dataclass
class Capture:
    wire: dict[str, Any]
    save: Callable[..., dict[str, Any]]
    fetched: list[tuple[str, dict[str, Any], Any]] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)
    keywords: dict[str, Any] = field(default_factory=dict)
    seeds: dict[str, str] = field(default_factory=dict)
    banks: Any = None
    receipt: dict[str, Any] | None = None
    path: Path | None = None

    def fetch(self, name: str, request: dict[str, Any], fetch: Callable[[], Any]) -> Any:
        value = fetch()
        self.fetched.append((name, dict(request), value))
        return value


@dataclass(frozen=True)
class Executed:
    """A reviewed script returned a value and already wrote its run record."""

    value: Any


def register(host: Callable[[], Callable[..., dict[str, Any]] | None],
             run: Callable[..., Awaitable[Executed]] | None = None) -> None:
    """The server supplies the archive writer and runner for a live case turn."""
    global _HOST, _RUN
    _HOST, _RUN = host, run


def current() -> Capture | None:
    return _CURRENT.get()


@contextmanager
def capture(wire: Mapping[str, Any]) -> Iterator[Capture | None]:
    save = _HOST() if _HOST is not None else None
    if save is None:
        yield None
        return
    active = Capture(dict(wire), save)
    token = _CURRENT.set(active)
    pin = bind_pin(active.fetch)
    try:
        yield active
    finally:
        reset_pin(pin)
        _CURRENT.reset(token)


async def archive(deck: Any) -> None:
    active = current()
    if active is None:
        return
    try:
        active.receipt = await asyncio.to_thread(active.save, active, deck)
    except Exception as error:  # noqa: BLE001 - script publication cannot void a run
        logger.warning("Case script publication refused: %s", type(error).__name__)
        active.receipt = {"code": "CODEGEN_WRITE_ERROR"}


async def execute(deck: Any) -> Executed | None:
    active = current()
    if _RUN is None or active is None or active.path is None or not active.receipt:
        return None
    return await _RUN(active.path, deck, active.receipt)
