"""AOI coercions - engine-agnostic, shared by every workflow that models a place.

A workflow's door-1 sheet needs exactly ONE area of interest. Turning whatever
the model sent into that one AOI is the same job for every engine, so it is
declared once here and named by the templates that need it.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from trid3nt_server.tools.tool_arg_normalizer import coerce_bbox_value
from trid3nt_server.workflows.lib import WireArgsError

__all__ = ["location_or_bbox"]

logger = logging.getLogger("trid3nt_server.workflows.shared.aoi")


def location_or_bbox(tool: str, *, hint: str = "", code_prefix: str = "TELEMAC",
                     location_wins: bool = True) -> Any:
    """A coercion resolving ``location`` / ``bbox`` down to exactly one AOI.

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
