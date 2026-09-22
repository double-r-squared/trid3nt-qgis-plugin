"""nhd_water_surface hooks: the envelope the seed and the radius make.

The service takes an ArcGIS envelope and the question states a seed and a reach,
so the one step the grammar cannot say is turning the two into a box - in degrees,
narrowed by latitude so the box is square on the ground.
"""

from __future__ import annotations

import math
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..._router.errors import router_input_error
from ..._router.hooks import RequestPlan, register_hook

__all__ = ["build_request"]

#: Degrees of latitude per kilometre. The envelope only has to CONTAIN the reach:
#: a polygon intersecting it comes back whole, so a generous box costs one query.
_DEG_PER_KM = 1.0 / 111.0

#: The smallest cosine the longitude pad is divided by, so a seed near a pole
#: does not ask for a box that wraps the planet.
_MIN_COSINE = 0.2


@register_hook("nhd_water_surface.build_request")
def build_request(spec: SourceSpec, params: dict[str, Any]) -> list[RequestPlan]:
    """One envelope query around the seed."""
    raw = params.get("seed_point")
    try:
        lon, lat = float(raw[0]), float(raw[1])
    except (TypeError, ValueError, IndexError):
        raise router_input_error(
            spec.error_code_prefix,
            f"seed_point must be (lon, lat); got {raw!r}",
            spec.input_error_suffix,
        )
    pad = float(params.get("radius_km") or 3.0) * _DEG_PER_KM
    pad_lon = pad / max(_MIN_COSINE, math.cos(math.radians(lat)))
    endpoint = spec.endpoints["data"]
    return [RequestPlan(
        url=str(endpoint.url),
        params={**endpoint.query,
                "geometry": f"{lon - pad_lon},{lat - pad},"
                            f"{lon + pad_lon},{lat + pad}"})]
