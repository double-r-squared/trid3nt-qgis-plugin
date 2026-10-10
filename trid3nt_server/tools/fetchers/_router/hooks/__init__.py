"""The hook contract: the named, registered, PURE extension points.

A source whose bespoke-ness is one irreducible step the declarative surface cannot
express names a registered pure function in its ``source.yaml``. A hook is PURE --
no transport, cache or gate -- MINIMAL, REGISTERED under a validated name, TESTED."""

# Hook points: ``build_request`` (1..N plans, plus input validation) and ``parse_response``
# (bodies to features; raises empty, too-large and bad-body errors); chained resolution adds
# ``resolve_build``, ``resolve_parse``, ``next_page``, ``enrich_plan`` and ``enrich_merge``; envelope
# mode adds ``envelope(spec, params, layer, data)``, called LAST, whose ``uri`` / ``layer_type`` keys are dropped.

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger("trid3nt_server.tools.fetchers._router.hooks")

__all__ = [
    "RequestPlan",
    "FramePlan",
    "FrameDegraded",
    "frame_windows",
    "HOOK_REGISTRY",
    "register_hook",
    "resolve_hook",
    "has_hook",
    "HookResolutionError",
]


@dataclass(frozen=True)
class RequestPlan:
    """One request the router transport executes for a hook: PURE data; the router owns the socket, retry and typed errors."""

    # ``method`` defaults to "GET"; "POST" sends ``json_body`` as JSON or ``data`` form-encoded.

    url: str
    params: dict[str, Any] | None = None
    headers: dict[str, str] = field(default_factory=dict)
    method: str = "GET"
    json_body: Any = None
    data: dict[str, Any] | None = None


@dataclass(frozen=True)
class FramePlan:
    """One frame of a ``shape: animation_frames`` sequence: PURE data from the ``frames_plan`` hook, one ``read_through`` per frame."""

    cache_params: dict[str, Any]
    name: str
    #: The emitted LayerURI.layer_id; a per-product stem keeps sibling products in separate scrubber groups.
    layer_id: str
    bbox: tuple[float, float, float, float]
    #: The ISO-8601 UTC window this frame is valid for, stamped as the layer's fixed temporal range.
    valid_from: str | None = None
    valid_to: str | None = None
    #: OPTIONAL fetch inputs the frame_bytes hook needs that must NOT enter the read_through key.
    fetch_context: dict[str, Any] = field(default_factory=dict)
    #: OPTIONAL per-frame style row override; None falls back to the row's own.
    style: dict | None = None


def frame_windows(instants: list[str]) -> list[tuple[str | None, str | None]]:
    """Each instant's validity window runs until the next; the LAST keeps the preceding interval, and fewer than two instants states NO window."""
    from datetime import datetime

    if len(instants) < 2:
        return [(None, None)] * len(instants)
    moments = [datetime.fromisoformat(t.replace("Z", "+00:00")) for t in instants]
    ends = moments[1:] + [moments[-1] + (moments[-1] - moments[-2])]
    return [(begin, end.strftime("%Y-%m-%dT%H:%M:%SZ"))
            for begin, end in zip(instants, ends)]


class FrameDegraded(Exception):
    """Raised by a ``frame_bytes`` hook to skip ONE degraded frame (recorded, not silent); only EVERY frame degrading raises the typed EMPTY."""


class HookResolutionError(ValueError):
    """A row referenced a ``hooks.*`` name absent from the registry."""


HOOK_REGISTRY: dict[str, Callable[..., Any]] = {}


def register_hook(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a pure hook under ``<source_key>.<point>``; a duplicate name raises rather than last-wins."""

    def _wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
        if name in HOOK_REGISTRY and HOOK_REGISTRY[name] is not fn:
            raise HookResolutionError(f"duplicate hook name {name!r}")
        HOOK_REGISTRY[name] = fn
        return fn

    return _wrap


def resolve_hook(name: str) -> Callable[..., Any]:
    fn = HOOK_REGISTRY.get(name)
    if fn is None:
        raise HookResolutionError(
            f"no hook registered under {name!r}; known: {sorted(HOOK_REGISTRY)}"
        )
    return fn


def has_hook(name: str) -> bool:
    return name in HOOK_REGISTRY


# Hook modules register at import and BOTH homes are WALKED, not listed: a row's own
# ``fetchers/<group>/<spec>/hooks.py`` and the shared modules beside this one.


def _import_hook_modules() -> None:
    import importlib
    from pathlib import Path

    here = Path(__file__).resolve().parent
    fetchers_root = here.parents[1]
    fetchers_pkg = __name__.rsplit(".", 2)[0]
    for path in sorted(here.glob("*.py")):
        if path.stem.startswith("_"):
            continue
        importlib.import_module(f"{__name__}.{path.stem}")
    for path in sorted(fetchers_root.rglob("hooks.py")):
        parts = path.relative_to(fetchers_root).with_suffix("").parts
        if parts[0] == here.parent.name:
            continue
        importlib.import_module(fetchers_pkg + "." + ".".join(parts))


_import_hook_modules()
