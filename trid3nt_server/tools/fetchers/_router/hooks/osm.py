"""OSM reads through OSMnx: the mirror chain, and the error the library drops.

OSMnx owns the Overpass query, the socket, the adaptive pre-request pause sized
off the server's own ``/status``, and the element-to-geometry decode - including
the multipolygon relation assembly and the full tag bag as frame columns. Two
things it does not own ride here.

MIRRORS. OSMnx holds ONE ``settings.overpass_url``, so the three-mirror chain each
source declares is ours: set, call, catch, next, restore. It also wants the API
BASE and appends ``/interpreter`` itself, while a source row names the interpreter
URL its callers would use, so the suffix comes off here.

THE SILENT ERROR. ``osmnx._http._parse_response`` raises only when the body fails
to parse as JSON, so a non-OK response carrying a valid JSON error envelope is
RETURNED AS DATA - an honest-empty layer exactly where an upstream failure belongs.
The wrapped post raises on any non-OK status OSMnx does not handle itself, carrying
the upstream status and body verbatim.

MEASURED AND ACCEPTED for this family: on a 429 or a 504 OSMnx pauses a hardcoded
55 s (``osmnx/_overpass.py``) rather than the server's ``Retry-After``. It recovers;
it just does not obey the server's number. What is NOT accepted is that it retries
by RECURSION with no ceiling, so a mirror that keeps refusing hangs the fetch
forever: the wrapped post counts those refusals and lets the mirror chain move on.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..errors import router_upstream_error

logger = logging.getLogger("trid3nt_server.tools.fetchers._router.hooks.osm")

__all__ = ["overpass_features", "osm_id_of", "clip_to_bbox"]

#: OSMnx settings are module globals, so one source's mirror is every caller's.
_settings_lock = threading.Lock()


#: How many times one mirror may answer 429/504 before the chain gives up on it.
#: The library's own retry is a bare recursion, so the ceiling has to be here.
_MAX_THROTTLED = 3


class _RaiseOnStatus:
    """A ``requests`` stand-in whose post refuses to hand back a failed response."""

    def __init__(self, real: Any) -> None:
        self._real = real
        self.throttled = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def post(self, url: str, *args: Any, **kwargs: Any) -> Any:
        resp = self._real.post(url, *args, **kwargs)
        if resp.ok:
            return resp
        # 429 and 504 are the library's own retry path, bounded here.
        if resp.status_code in (429, 504):
            self.throttled += 1
            if self.throttled > _MAX_THROTTLED:
                raise _OverpassStatus(resp.status_code, url, resp.text)
            return resp
        raise _OverpassStatus(resp.status_code, url, resp.text)


class _OverpassStatus(RuntimeError):
    """The upstream status and body, both verbatim."""

    def __init__(self, status: int, url: str, body: str) -> None:
        super().__init__(f"Overpass {status} from {url}\nBODY: {body}")


def overpass_features(
    spec: SourceSpec,
    params: dict[str, Any],
    tags: dict[str, Any],
    *,
    timeout_s: float,
) -> Any:
    """Every OSM feature in the bbox carrying ``tags``, as a GeoDataFrame.

    The bbox is ``(west, south, east, north)``; OSMnx keeps whole geometries that
    intersect it. An area with no such feature is an EMPTY frame, which is an
    answer; every mirror failing is a typed upstream error naming the last one.
    """
    import osmnx as ox
    from osmnx import _overpass, settings

    # The row names the interpreter URL; the library appends that itself.
    mirrors = [
        (ep.url or ep.url_template or "").rstrip("/").removesuffix("/interpreter")
        for ep in spec.endpoints.values()
    ]
    bbox = tuple(float(v) for v in params["bbox"])
    last: Exception | None = None

    with _settings_lock:
        prior = (settings.overpass_url, settings.requests_timeout, settings.use_cache)
        real_requests = _overpass.requests
        wrapper = _RaiseOnStatus(real_requests)
        _overpass.requests = wrapper
        # The router owns the cache tier and the provenance that rides with it; a
        # second cache under it would age on its own rules.
        settings.use_cache = False
        # The timeout is also the QL's own [timeout:N] directive, which Overpass
        # parses as an integer number of seconds.
        settings.requests_timeout = int(timeout_s)
        try:
            for i, url in enumerate(mirrors):
                settings.overpass_url = url
                wrapper.throttled = 0
                try:
                    return ox.features_from_bbox(bbox, tags)
                except ox._errors.InsufficientResponseError:
                    # OSMnx's word for "no element matched", which is an answer.
                    import geopandas as gpd

                    return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
                except Exception as exc:  # noqa: BLE001 -- try the next mirror
                    last = exc
                    if i < len(mirrors) - 1:
                        logger.warning(
                            "router.osm: mirror %d/%d (%s) failed (%s); trying the next",
                            i + 1, len(mirrors), url, exc,
                        )
        finally:
            _overpass.requests = real_requests
            settings.overpass_url, settings.requests_timeout, settings.use_cache = prior

    raise router_upstream_error(
        spec.error_code_prefix,
        f"all {len(mirrors)} Overpass mirror(s) failed; last error: {last}",
    )


def osm_id_of(index_value: Any) -> int:
    """The element's OSM id from the frame's ``(element_type, osmid)`` index."""
    return int(index_value[1] if isinstance(index_value, tuple) else index_value)


def clip_to_bbox(geom: Any, bbox: tuple[float, float, float, float]) -> list[list[list[float]]]:
    """A way's vertices, cut to the bbox, as one coordinate list per in-AOI segment.

    A way crossing the boundary several times is several segments, each of which
    is the same way and carries the same attributes. Anything the cut leaves with
    fewer than two vertices is not a line and is dropped.
    """
    from shapely import clip_by_rect

    if geom is None or geom.is_empty:
        return []
    try:
        clipped = clip_by_rect(geom, bbox[0], bbox[1], bbox[2], bbox[3])
    except Exception:  # noqa: BLE001 -- a degenerate geometry drops the way
        return []
    parts: list[list[list[float]]] = []
    stack = [clipped]
    while stack:
        part = stack.pop(0)
        if part is None or part.is_empty:
            continue
        if part.geom_type == "LineString":
            coords = [[float(x), float(y)] for x, y in part.coords]
            if len(coords) >= 2:
                parts.append(coords)
        elif part.geom_type in ("MultiLineString", "GeometryCollection"):
            stack.extend(part.geoms)
    return parts
