"""AOI coercion and AOI ACQUISITION - engine-agnostic, for any workflow that models a place.

A door-1 sheet needs exactly ONE area of interest, and turning what the model sent
into that AOI, then into the bound DOMAIN, is the same job for every engine.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from trid3nt_server.tools.tool_arg_normalizer import coerce_bbox_value
from trid3nt_server.workflows.runtime import Step, WireArgsError

__all__ = ["AcquireAoi", "acquire_aoi", "aoi_slug", "geocode_place",
           "location_or_bbox"]

logger = logging.getLogger("trid3nt_server.workflows.inputs.aoi")

_HERE = "trid3nt_server.workflows.inputs.aoi"


def location_or_bbox(tool: str, *, code_prefix: str, hint: str = "",
                     location_wins: bool = True) -> Any:
    """A coercion resolving ``location`` / ``bbox`` down to exactly one AOI.
    ``code_prefix`` is REQUIRED: this module is engine-agnostic, so it has no error
    codes of its own and the template is the one caller that knows them."""

    # Three behaviours, each of them a wrong answer if it went the other way: a
    # NON-NUMERIC bbox is almost always a place name in the wrong field and shifts
    # to ``location``; neither supplied REFUSES typed, naming what to send; both
    # supplied with ``location_wins`` drops the bbox, because a fabricated box
    # beside a real place name has been observed on open water at a river mouth.
    # A user-drawn AOI arrives through case state, not through this argument.
    def _coerce(args: Mapping[str, Any]) -> dict[str, Any]:
        location, bbox = args.get("location"), args.get("bbox")
        coerced: tuple[float, float, float, float] | None = None
        if bbox is not None:
            cb = coerce_bbox_value(bbox)
            if cb is None:
                if isinstance(bbox, str) and any(c.isalpha() for c in bbox) \
                        and not (location and str(location).strip()):
                    logger.warning("%s: bbox %r is a place name - using as location",
                                   tool, bbox)
                    location, bbox = bbox, None
                else:
                    raise WireArgsError(
                        "invalid bbox (expected 4 numbers min_lon,min_lat,max_lon,"
                        f"max_lat): {bbox!r}",
                        error_code=f"{code_prefix}_PARAMS_INVALID")
            else:
                coerced = tuple(cb)  # type: ignore[assignment]

        has_loc = bool(location and str(location).strip())
        if not has_loc and coerced is None:
            raise WireArgsError(
                f"{tool} needs a place `location` (geocoded) or an explicit `bbox` "
                "AOI." + (f" {hint}" if hint else ""),
                error_code=f"{code_prefix}_PARAMS_INCOMPLETE")
        if has_loc and coerced is not None and location_wins:
            logger.warning("%s: both location and bbox supplied - dropping the bbox "
                           "%s in favour of geocoding %r", tool, coerced, location)
            coerced = None
        return {"location": location if has_loc else None, "bbox": coerced}

    _coerce.__name__ = "location_or_bbox"
    return _coerce


def aoi_slug(name: str, *, default: str) -> str:
    """A safe ASCII slug for a domain name - what a worker manifest is keyed on."""
    keep = "".join(c.lower() if c.isalnum() else "_" for c in str(name or default))
    out = "_".join(part for part in keep.split("_") if part)
    return (out or default)[:48]


def _geo_field(geo: Any, keys: tuple[str, ...]) -> float | None:
    """One coordinate off whatever shape the geocoder answered with.
    An object, a dict, or either nested under ``center`` / ``geometry`` /
    ``location`` / ``result``; a reader of one shape alone would report a failure."""
    if geo is None:
        return None
    for key in keys:
        value = getattr(geo, key, None)
        if value is None and isinstance(geo, dict):
            value = geo.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    for sub in ("center", "geometry", "location", "result"):
        nested = getattr(geo, sub, None) or (
            geo.get(sub) if isinstance(geo, dict) else None)
        if nested is not None:
            found = _geo_field(nested, keys)
            if found is not None:
                return found
    return None


async def geocode_place(name: str) -> tuple[float, float] | None:
    """A place name -> ``(lon, lat)`` through the registered geocoder; ``None``
    when it answers nothing. A SYNC geocode is a network call, and running one
    on the loop stalls the socket keepalive, so it runs in a thread."""
    from trid3nt_server.tools import TOOL_REGISTRY

    entry = TOOL_REGISTRY.get("geocode_location")
    if entry is None:
        raise WireArgsError("geocode_location is not registered.",
                            error_code="AOI_INTERNAL_ERROR")
    import asyncio
    import inspect

    if inspect.iscoroutinefunction(entry.fn):
        geo = await entry.fn(name)
    else:
        geo = await asyncio.to_thread(entry.fn, name)
        if inspect.isawaitable(geo):
            geo = await geo
    lon = _geo_field(geo, ("lon", "longitude", "x"))
    lat = _geo_field(geo, ("lat", "latitude", "y"))
    return None if lon is None or lat is None else (lon, lat)


async def acquire_aoi(*, location: str | None,
                      bbox: Any = None,
                      half_deg: float | tuple[float, float] = 0.06,
                      default_name: str = "aoi",
                      code_prefix: str = "AOI",
                      around: Any = None) -> dict[str, Any]:
    """Resolve the modeled AOI: an explicit extent used VERBATIM, the box around a
    Point, or a geocoded place around one. Rebinds the DOMAIN; ``half_deg`` takes
    a ``(dlon, dlat)`` pair where the question is not square; ``default_name``
    becomes the AOI slug; ``around`` wins over a place, because the place only
    NAMES the run while the Point decides what is modelled."""
    coerced = coerce_bbox_value(bbox) if bbox is not None else None
    named = str(location).strip() if (location and str(location).strip()) else ""
    if coerced is not None:
        extent = tuple(float(v) for v in coerced)
        name = named or default_name
        lon, lat = (0.5 * (extent[0] + extent[2]), 0.5 * (extent[1] + extent[3]))
    elif around is not None:
        lon, lat = float(around.lon), float(around.lat)
        dlon, dlat = _half(half_deg)
        extent = (max(lon - dlon, -180.0), max(lat - dlat, -90.0),
                  min(lon + dlon, 180.0), min(lat + dlat, 90.0))
        name = named or default_name
    elif named:
        found = await geocode_place(str(location).strip())
        lon, lat = found if found is not None else (None, None)
        if lon is None or lat is None:
            raise WireArgsError(
                f"could not geocode {location!r} to an AOI.",
                error_code=f"{code_prefix}_GEOCODE_FAILED")
        dlon, dlat = _half(half_deg)
        extent = (round(lon - dlon, 4), round(lat - dlat, 4),
                  round(lon + dlon, 4), round(lat + dlat, 4))
        name = named
    else:
        raise WireArgsError(
            "the domain needs a place `location` (geocoded) or an explicit `bbox`.",
            error_code=f"{code_prefix}_PARAMS_INCOMPLETE")
    return {"lon": lon, "lat": lat, "name": name,
            "slug": aoi_slug(name, default=default_name), "bbox": extent}


def _half(half_deg: float | tuple[float, float]) -> tuple[float, float]:
    return ((float(half_deg[0]), float(half_deg[1]))
            if isinstance(half_deg, (list, tuple))
            else (float(half_deg), float(half_deg)))


def AcquireAoi(*, location: Any, bbox: Any,  # noqa: N802
               half_deg: float | tuple[float, float] = 0.06,
               default_name: str = "aoi", code_prefix: str = "AOI",
               around: Any = None) -> Step:
    """Place, extent or Point -> the modeled AOI. Refines the domain for everything after it."""
    return Step(runner=f"{_HERE}.acquire_aoi", stage="acquire",
                kwargs={"location": location, "bbox": bbox, "half_deg": half_deg,
                        "default_name": default_name, "code_prefix": code_prefix,
                        "around": around}).overrides_domain()
