"""Tier-3 hook contract: the named, registered, PURE extension points.

A source whose bespoke-ness is a single clean irreducible step the declarative
param/ingest surface cannot express references a registered pure function by name
in its ``source.yaml`` (``hooks.build_request`` / ``hooks.parse_response``). This
package is that function set: a name -> callable table (:data:`HOOK_REGISTRY`), the
:func:`register_hook` decorator that fills it, and :func:`resolve_hook` /
:func:`has_hook` the router + registration read.

DOCTRINE (data-router-fold.md, tier-3): hooks are PURE, MINIMAL, REGISTERED,
TESTED. Pure = no I/O (transport, caching, gates, stamps, and the typed-error
FACTORY machinery stay router-owned; a hook only computes and MAY call a shared
``router_*_error`` factory to raise a source-stamped typed error). Minimal = a
hook point exists only because a real source needs it. Registered = referenced by
a name string a spec load validates. Tested = each hook module carries its own
unit tests.

Hook signatures:
- ``build_request(spec, params) -> list[RequestPlan]`` -- source-specific
  request construction + bespoke pre-fetch input validation. 1..N plans.
- ``parse_response(spec, params, bodies: list[bytes]) -> list[dict]`` -- decode
  the source payload(s) into GeoJSON-ish point features; raise the honest-empty /
  too-large / bad-body typed errors.

Chained-resolution mode adds five PURE points for the resolve-then-fetch
/ bounded per-item enrichment shape; the router owns every round trip + the loops:
- ``resolve_build(spec, params) -> list[RequestPlan]`` -- round-1 name->id request(s)
  (or ``[]`` to skip); ``resolve_parse(spec, params, bodies) -> dict`` -- the resolved
  id as a params-merge (runs pre-cache-key so name+id collapse).
- ``next_page(spec, params, bodies) -> RequestPlan | None`` -- offset-paging loop
  control (next page or stop).
- ``enrich_plan(spec, params, features) -> list[(ref_key, RequestPlan)]`` -- per-item
  detail requests; ``enrich_merge(spec, params, features, results) -> list[dict]`` --
  fold the deduped/bounded/best-effort detail back in (every feature survives).

Envelope mode adds the POST-EMIT point for a LayerURI-SUBCLASS result:
- ``envelope(spec, params, layer, data: bytes) -> dict`` -- the last hook the router
  calls; over the assembled ``LayerURI`` + the produced bytes it computes the extra
  business fields (breakdowns / caveats / notes) for the spec's
  ``output.result_model`` subclass. PURE (no transport); the router drops the
  honesty-floor-owned ``uri`` / ``layer_type`` keys so a hook can only enrich.
"""

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
    """One request the router transport executes on a ``build_request`` hook's behalf.

    PURE data (no socket): the hook decides the URL / query params / headers /
    method / JSON body; the router owns the actual GET or POST, its retry
    authority, and typed transport errors.

    ``method`` defaults to ``"GET"`` (every prior hook). ``"POST"`` sends
    ``json_body`` as a JSON request body -- the write-method REST shape whose
    query is a body, not a query string (USACE NSI's structures POST) -- or, when
    ``data`` is set instead, a form-encoded body (the Overpass interpreter reads
    its QL from the ``data`` form field). No I/O still happens in the hook: it
    only DESCRIBES the request.
    """

    url: str
    params: dict[str, Any] | None = None
    headers: dict[str, str] = field(default_factory=dict)
    method: str = "GET"
    json_body: Any = None
    data: dict[str, Any] | None = None


@dataclass(frozen=True)
class FramePlan:
    """One frame of a ``shape: animation_frames`` sequence.

    PURE data: the ``frames_plan`` hook produces the ORDERED list of these (the
    pre-loop window/subsample already applied), and the ``animation_frames``
    executor drives one ``read_through`` per frame + emits a ``LayerURI`` per frame.

    - ``cache_params`` -- the per-frame read_through cache key (byte-identical to
      the hand-written twin's per-frame params so the fold reuses cached frames).
    - ``name`` -- the emitted ``LayerURI.name``, the layer's caption.
    - ``valid_from`` / ``valid_to`` -- the ISO-8601 UTC window this frame is
      valid for, which the map stamps as the layer's fixed temporal range. The
      plan holds the instants already; spelling a time into ``name`` for a
      reader to parse back out is the same fact in the wrong data class.
    - ``layer_id`` -- the emitted ``LayerURI.layer_id`` (a per-product stem keeps
      sibling products in separate scrubber groups).
    - ``bbox`` -- the AOI bbox stamped on every frame's ``LayerURI.bbox``.
    - ``fetch_context`` -- OPTIONAL out-of-cache-key fetch inputs the frame_bytes
      hook needs but that must NOT enter the read_through key: the raw
      MCMIPC S3 object key + the raw (unrounded) fetch args for an archive frame,
      which is addressed by an opaque per-scan key the ts-addressed SLIDER frames
      never carried. Defaulted empty -> a strict no-op for the wave-1 SLIDER frames.
    """

    cache_params: dict[str, Any]
    name: str
    layer_id: str
    bbox: tuple[float, float, float, float]
    valid_from: str | None = None
    valid_to: str | None = None
    fetch_context: dict[str, Any] = field(default_factory=dict)
    #: OPTIONAL per-frame style row override. The archive source emits
    #: distinct bands with distinct presets (goes_rgb_animation for the RGB composites,
    #: goes_fire_hotspots_rgba for the transparent hotspot RGBA) that a single
    #: the spec's own style row cannot carry; None -> the executor falls back
    #: to the spec's row.
    style: dict | None = None


def frame_windows(instants: list[str]) -> list[tuple[str | None, str | None]]:
    """Each declared instant's validity window: it runs until the next one.

    The LAST frame has no successor, so it keeps the interval before it - the
    only cadence the record actually measured. A sequence of fewer than two
    instants has no measured cadence at all and states no window rather than
    inventing one, which is what a synthetic one-hour-per-step clock was.
    """
    from datetime import datetime

    if len(instants) < 2:
        return [(None, None)] * len(instants)
    moments = [datetime.fromisoformat(t.replace("Z", "+00:00")) for t in instants]
    ends = moments[1:] + [moments[-1] + (moments[-1] - moments[-2])]
    return [(begin, end.strftime("%Y-%m-%dT%H:%M:%SZ"))
            for begin, end in zip(instants, ends)]


class FrameDegraded(Exception):
    """A ``frame_bytes`` hook raises this to skip ONE degraded frame.

    A transparent / off-swath / upstream-failed single frame is a RECORDED
    degradation the executor drops (never a silent gap); the executor's honesty
    floor raises the source's typed EMPTY error only when EVERY frame degrades.
    ``message`` is preserved for the all-frames-failed error text.
    """


class HookResolutionError(ValueError):
    """A spec referenced a ``hooks.*`` name absent from :data:`HOOK_REGISTRY`."""


#: name -> pure callable. Filled by :func:`register_hook` at hook-module import.
HOOK_REGISTRY: dict[str, Callable[..., Any]] = {}


def register_hook(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a pure hook under ``name`` (``<source_key>.<point>``).

    A duplicate name is a defect (two hooks would answer one spec reference), so
    it raises rather than silently last-wins.
    """

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
