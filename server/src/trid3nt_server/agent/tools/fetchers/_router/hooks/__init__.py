"""Tier-3 hook contract (ADR 0056): the named, registered, PURE extension points.

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

Chained-resolution mode (ADR 0063) adds five PURE points for the resolve-then-fetch
/ bounded per-item enrichment shape; the router owns every round trip + the loops:
- ``resolve_build(spec, params) -> list[RequestPlan]`` -- round-1 name->id request(s)
  (or ``[]`` to skip); ``resolve_parse(spec, params, bodies) -> dict`` -- the resolved
  id as a params-merge (runs pre-cache-key so name+id collapse).
- ``next_page(spec, params, bodies) -> RequestPlan | None`` -- offset-paging loop
  control (next page or stop).
- ``enrich_plan(spec, params, features) -> list[(ref_key, RequestPlan)]`` -- per-item
  detail requests; ``enrich_merge(spec, params, features, results) -> list[dict]`` --
  fold the deduped/bounded/best-effort detail back in (every feature survives).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger("trid3nt_server.agent.tools.fetchers._router.hooks")

__all__ = [
    "RequestPlan",
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
    query is a body, not a query string (USACE NSI's structures POST). No I/O
    still happens in the hook: it only DESCRIBES the request.
    """

    url: str
    params: dict[str, Any] | None = None
    headers: dict[str, str] = field(default_factory=dict)
    method: str = "GET"
    json_body: Any = None


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


# Import the hook modules so their ``@register_hook`` decorators populate the
# registry at package import (registration validates names against it at load).
from . import usgs_earthquakes  # noqa: E402,F401
from . import ncei_tsunami  # noqa: E402,F401
from . import usgs_volcano  # noqa: E402,F401
from . import nws_event  # noqa: E402,F401
from . import usace_nsi  # noqa: E402,F401
# chained-resolution mode hooks (ADR 0063).
from . import gbif_occurrences  # noqa: E402,F401
from . import inaturalist_observations  # noqa: E402,F401
from . import nws_alerts_conus  # noqa: E402,F401
from . import nws_river_forecast  # noqa: E402,F401
# offset paging + boundary-service FIPS enrich (ADR 0064).
from . import openfema_disasters  # noqa: E402,F401
# directory-index resolve -> bulk gzip-CSV point decode (ADR 0064).
from . import storm_events_db  # noqa: E402,F401
# station-siblings wave (ADR 0065): multi-state discovery + station-observations +
# batched-snapshot + keyed missing-key parity, all via the existing phases.
from . import asos_metar  # noqa: E402,F401
from . import raws_weather  # noqa: E402,F401
from . import snotel_snow  # noqa: E402,F401
from . import airnow_air_quality  # noqa: E402,F401
from . import openaq_measurements  # noqa: E402,F401
