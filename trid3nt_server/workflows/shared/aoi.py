"""AOI coercion and AOI ACQUISITION - engine-agnostic, for any workflow that
models a place.

A workflow's door-1 sheet needs exactly ONE area of interest. Turning whatever
the model sent into that one AOI is the same job for every engine, so it is
declared once here and named by the templates that need it. So is the step that
turns that one AOI into the bound DOMAIN: geocoding a place name to an extent is
not TELEMAC mechanism, and by the placement rule it lives at the highest layer
that needs no specialization.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from trid3nt_server.tools.tool_arg_normalizer import coerce_bbox_value
from trid3nt_server.workflows.lib import Step, WireArgsError

__all__ = ["AcquireAoi", "acquire_aoi", "aoi_slug", "location_or_bbox"]

logger = logging.getLogger("trid3nt_server.workflows.shared.aoi")

_SHARED = "trid3nt_server.workflows.shared.aoi"


def location_or_bbox(tool: str, *, code_prefix: str, hint: str = "",
                     location_wins: bool = True) -> Any:
    """A coercion resolving ``location`` / ``bbox`` down to exactly one AOI.

    ``code_prefix`` is REQUIRED. This file is engine-agnostic by placement, so a
    default engine prefix here would hand every future caller TELEMAC's error codes
    silently - a SWMM template refusing with ``TELEMAC_PARAMS_INVALID`` is a wrong
    answer that no test asks about. The one caller who knows the prefix is the
    template.

    Three real behaviours, each of them a bug the field taught us:

    * a NON-NUMERIC ``bbox`` is almost always a place name the model put in the
      wrong field - it shifts to ``location`` rather than dead-ending the call;
    * neither supplied REFUSES typed, naming what to send;
    * both supplied with ``location_wins`` drops the bbox - a model that
      fabricates one alongside a real place name has been observed to put it on
      open water at a river MOUTH, and the geocoded place is ground truth. A
      user-drawn AOI arrives through case state, not through this argument.
    """

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

    Geocoders answer as an object, a dict, or either of those nested under a
    ``center`` / ``geometry`` / ``location`` / ``result`` key, and a reader that
    knows only one of those shapes reports a successful geocode as a failure.
    """
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


async def acquire_aoi(*, location: str | None,
                      bbox: Any = None,
                      half_deg: float = 0.06,
                      default_name: str = "aoi",
                      code_prefix: str = "AOI") -> dict[str, Any]:
    """Resolve the modeled AOI: an explicit extent, or a geocoded place around one.

    The returned ``bbox`` is what REBINDS THE DOMAIN, so every producer after this
    step reads the AOI implicitly instead of being handed one. An explicit bbox is
    used VERBATIM - it is the user's own extent and squaring it off around its
    centre would model a different place than the one drawn.

    ``default_name`` is what an AOI with no place name is called; it becomes the
    slug the worker manifest and the published layer names are keyed on, so it is
    the caller's word ("coast", "harbor", "lake"), never a generic one invented
    here.
    """
    coerced = coerce_bbox_value(bbox) if bbox is not None else None
    if coerced is not None:
        extent = tuple(float(v) for v in coerced)
        name = str(location).strip() if (location and str(location).strip()) \
            else default_name
        lon, lat = (0.5 * (extent[0] + extent[2]), 0.5 * (extent[1] + extent[3]))
    elif location and str(location).strip():
        from trid3nt_server.tools import TOOL_REGISTRY

        entry = TOOL_REGISTRY.get("geocode_location")
        if entry is None:
            raise WireArgsError("geocode_location is not registered.",
                                error_code=f"{code_prefix}_INTERNAL_ERROR")
        import asyncio
        import inspect

        # A SYNC geocode is a network call, and running one on the loop stalls
        # the socket keepalive; an async one is awaited where it stands.
        if inspect.iscoroutinefunction(entry.fn):
            geo = await entry.fn(location)
        else:
            geo = await asyncio.to_thread(entry.fn, location)
            if inspect.isawaitable(geo):
                geo = await geo
        lon = _geo_field(geo, ("lon", "longitude", "x"))
        lat = _geo_field(geo, ("lat", "latitude", "y"))
        if lon is None or lat is None:
            raise WireArgsError(
                f"could not geocode {location!r} to an AOI.",
                error_code=f"{code_prefix}_GEOCODE_FAILED")
        h = float(half_deg)
        extent = (round(lon - h, 4), round(lat - h, 4),
                  round(lon + h, 4), round(lat + h, 4))
        name = str(location).strip()
    else:
        raise WireArgsError(
            "the domain needs a place `location` (geocoded) or an explicit `bbox`.",
            error_code=f"{code_prefix}_PARAMS_INCOMPLETE")
    return {"lon": lon, "lat": lat, "name": name,
            "slug": aoi_slug(name, default=default_name), "bbox": extent}


def AcquireAoi(*, location: Any, bbox: Any, half_deg: float = 0.06,  # noqa: N802
               default_name: str = "aoi", code_prefix: str = "AOI") -> Step:
    """Place/extent -> the modeled AOI. Refines the domain for everything after it."""
    return Step(runner=f"{_SHARED}.acquire_aoi", stage="acquire",
                kwargs={"location": location, "bbox": bbox, "half_deg": half_deg,
                        "default_name": default_name,
                        "code_prefix": code_prefix}).overrides_domain()
