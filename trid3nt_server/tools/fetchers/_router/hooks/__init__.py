"""The hook contract: the named, registered, PURE extension points.

A source whose bespoke-ness is one irreducible step the declarative surface cannot
express names a registered pure function in its ``source.yaml``. A hook is PURE --
no transport, cache or gate -- MINIMAL, REGISTERED under a validated name, TESTED."""

# The hook points, by mode.
#
# The base pair: ``build_request(spec, params) -> list[RequestPlan]`` is the
# source-specific request construction and pre-fetch input validation, 1..N plans;
# ``parse_response(spec, params, bodies) -> list[dict]`` decodes the payloads into
# GeoJSON-ish features and raises the honest-empty, too-large and bad-body errors.
#
# Chained resolution adds five points for resolve-then-fetch with bounded per-item
# enrichment, the router owning every round trip and both loops: ``resolve_build``
# builds the round-1 name-to-id requests (or [] to skip) and ``resolve_parse``
# returns the resolved id as a params-merge, pre-cache-key, so name and id collapse
# to one entry; ``next_page`` is offset-paging loop control; ``enrich_plan`` emits
# the per-item detail requests and ``enrich_merge`` folds the deduped, bounded,
# best-effort results back in, every feature surviving.
#
# Envelope mode adds the post-emit point: ``envelope(spec, params, layer, data)`` is
# the LAST hook the router calls, computing the extra business fields for the spec's
# ``output.result_model`` subclass over the assembled layer and the produced bytes.
# It is pure, and the router drops the honesty-floor-owned ``uri`` and ``layer_type``
# keys from its return, so a hook can only enrich.

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
    """One request the router transport executes on a hook's behalf: PURE data, so the
    hook decides url, query, headers, method and body while the router owns the socket,
    the retry authority and the typed errors."""

    # ``method`` defaults to "GET". "POST" sends ``json_body`` as a JSON request body,
    # the REST shape whose query is a body rather than a query string, or, when
    # ``data`` is set instead, a form-encoded body.

    url: str
    params: dict[str, Any] | None = None
    headers: dict[str, str] = field(default_factory=dict)
    method: str = "GET"
    json_body: Any = None
    data: dict[str, Any] | None = None


@dataclass(frozen=True)
class FramePlan:
    """One frame of a ``shape: animation_frames`` sequence: PURE data, produced in
    order by the ``frames_plan`` hook with the window and subsample already applied,
    and driven one ``read_through`` per frame by the executor."""

    #: The per-frame read_through cache key.
    cache_params: dict[str, Any]
    #: The emitted LayerURI.name, which is the layer's caption.
    name: str
    #: The emitted LayerURI.layer_id; a per-product stem keeps sibling products in
    #: separate scrubber groups.
    layer_id: str
    #: The AOI bbox stamped on every frame's LayerURI.bbox.
    bbox: tuple[float, float, float, float]
    #: The ISO-8601 UTC window this frame is valid for, which the map stamps as the
    #: layer's fixed temporal range. The plan holds the instants already; spelling a
    #: time into ``name`` for a reader to parse back out is the same fact in the wrong
    #: data class.
    valid_from: str | None = None
    valid_to: str | None = None
    #: OPTIONAL fetch inputs the frame_bytes hook needs that must NOT enter the
    #: read_through key -- an opaque per-scan object key, the raw unrounded fetch args
    #: -- for a frame addressed by something other than its timestamp.
    fetch_context: dict[str, Any] = field(default_factory=dict)
    #: OPTIONAL per-frame style row override, for a source whose frames carry distinct
    #: presets a single spec-level style row cannot express. None falls back to the
    #: spec's own row.
    style: dict | None = None


def frame_windows(instants: list[str]) -> list[tuple[str | None, str | None]]:
    """Each declared instant's validity window: it runs until the next one. The LAST
    frame keeps the interval before it, the only cadence the record measured, and
    fewer than two instants states NO window rather than inventing a clock."""
    from datetime import datetime

    if len(instants) < 2:
        return [(None, None)] * len(instants)
    moments = [datetime.fromisoformat(t.replace("Z", "+00:00")) for t in instants]
    ends = moments[1:] + [moments[-1] + (moments[-1] - moments[-2])]
    return [(begin, end.strftime("%Y-%m-%dT%H:%M:%SZ"))
            for begin, end in zip(instants, ends)]


class FrameDegraded(Exception):
    """A ``frame_bytes`` hook raises this to skip ONE degraded frame: a recorded
    degradation the executor drops, never a silent gap. Only EVERY frame degrading
    raises the source's typed EMPTY, and ``message`` carries into that text."""


class HookResolutionError(ValueError):
    """A spec referenced a ``hooks.*`` name absent from :data:`HOOK_REGISTRY`."""


#: name -> pure callable. Filled by :func:`register_hook` at hook-module import.
HOOK_REGISTRY: dict[str, Callable[..., Any]] = {}


def register_hook(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a pure hook under ``name``, shaped ``<source_key>.<point>``. A
    duplicate name is a defect -- two hooks would answer one spec reference -- so it
    raises rather than silently last-wins."""

    def _wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
        if name in HOOK_REGISTRY and HOOK_REGISTRY[name] is not fn:
            raise HookResolutionError(f"duplicate hook name {name!r}")
        HOOK_REGISTRY[name] = fn
        return fn

    return _wrap


def resolve_hook(name: str) -> Callable[..., Any]:
    """Return the registered hook for ``name`` or raise :class:`HookResolutionError`."""
    fn = HOOK_REGISTRY.get(name)
    if fn is None:
        raise HookResolutionError(
            f"no hook registered under {name!r}; known: {sorted(HOOK_REGISTRY)}"
        )
    return fn


def has_hook(name: str) -> bool:
    """True iff ``name`` resolves in :data:`HOOK_REGISTRY`."""
    return name in HOOK_REGISTRY


# Hook modules register themselves at import, and BOTH homes are WALKED rather
# than listed: a spec's own ``fetchers/<group>/<spec>/hooks.py``, and the modules
# beside this one, which are the hooks SEVERAL specs share. Co-location gives a
# fetcher package the contract ``source.yaml`` and ``corpus.yaml`` already have --
# adding, moving or removing one edits no shared file.


def _import_hook_modules() -> None:
    """Import every hook module so its ``@register_hook`` decorators run."""
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
