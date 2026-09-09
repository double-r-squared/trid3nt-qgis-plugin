"""``telemac_river_dye``'s own wire POLICY: which point seeds the reach.

A release point supplied on the CALL also seeds the meshed reach, while one
picked at the draw gate moves the SOURCE alone. The split is structural:
coercions run on the wire args, before any door and therefore before any gate."""

from __future__ import annotations

from typing import Any

from trid3nt_server.workflows.runtime import user_input
from trid3nt_server.workflows.telemac.helpers.errors import TelemacDyeScenarioError

__all__ = ["release_points"]

_CODE = "TELEMAC_PARAMS_INVALID"


def release_points(args: dict[str, Any]) -> dict[str, Any]:
    """The release point, and the point the WORKER seeds the reach from.

    Accepts an explicit pair, split lon/lat, or one "lat,lon" string."""
    release = args.get("release_coords")
    if release is None:
        lat, lon = args.get("release_lat"), args.get("release_lon")
        if lat is None and lon is None and args.get("spill_location_latlon"):
            try:
                lat_s, lon_s = str(args["spill_location_latlon"]).split(",", 1)
                lat, lon = float(lat_s), float(lon_s)
            except (ValueError, TypeError):
                raise TelemacDyeScenarioError(
                    _CODE,
                    f"spill_location_latlon={args['spill_location_latlon']!r} is not "
                    "'lat,lon'. Supply release_coords as (lon, lat) instead.") from None
        release = None if (lat is None and lon is None) else [lon, lat]

    return {
        "release_coords": user_input.lonlat_point(release, label="release point",
                                                  code=_CODE),
        # The SAME point, and only because it arrived on the call: what the wire
        # named is a statement about which reach to model, and a click made later
        # is a statement about where the substance enters the one already meshed.
        "reach_seed_coords": user_input.lonlat_point(release,
                                                     label="reach seed point",
                                                     code=_CODE),
    }
