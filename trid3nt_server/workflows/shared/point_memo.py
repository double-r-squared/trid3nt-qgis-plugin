"""Memoize a ``(lat, lon) -> (fit, meta)`` point derivation ON SUCCESS ONLY.

A fit at a point is a fixed fact worth remembering for the life of the process.
A FAILURE is not a fact about the point: a fetch that timed out, rate-limited or
5xx'd says nothing about the soil under it, and remembering it turns one
transient upstream error into a sticky refusal every later run at that point
inherits. Only a resolved fit enters the cache; an unresolved one is re-attempted
on the next call.

Bounded FIFO - the daemon is long-lived, so a point cache with no ceiling leaks.
"""

from __future__ import annotations

from functools import wraps
from typing import Any, Callable

__all__ = ["memo_on_success"]

PointFit = Callable[[float, float], "tuple[Any, dict[str, Any]]"]


def memo_on_success(fn: PointFit | None = None, *, maxsize: int = 64) -> Any:
    """Cache ``fn``'s result per exact point, keeping only resolved fits.

    The key is the coordinate pair AS PASSED: these derivations sample a window
    around the point, so rounding the key would move the fit and the solve with
    it. The wrapper exposes ``cache_clear``.
    """
    def decorate(inner: PointFit) -> PointFit:
        cache: dict[tuple[float, float], tuple[Any, dict[str, Any]]] = {}

        @wraps(inner)
        def wrapper(lat: float, lon: float) -> tuple[Any, dict[str, Any]]:
            key = (lat, lon)
            hit = cache.get(key)
            if hit is not None:
                return hit
            result = inner(lat, lon)
            if result[0] is not None:
                if len(cache) >= maxsize:
                    cache.pop(next(iter(cache)))
                cache[key] = result
            return result

        wrapper.cache_clear = cache.clear  # type: ignore[attr-defined]
        return wrapper

    return decorate if fn is None else decorate(fn)
