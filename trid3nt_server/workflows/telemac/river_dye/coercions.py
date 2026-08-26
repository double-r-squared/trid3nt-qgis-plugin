"""``telemac_river_dye``'s own wire POLICY: which point seeds the reach.

The SHAPES are the library's (``workflows/lib/user_input``); what lives here is
the decision only this question has to make - a release point supplied on the
call also seeds the meshed reach, while one picked at the draw gate moves the
SOURCE alone. Re-seeding from the click would mesh a different reach than the one
the user approved.
"""

from __future__ import annotations

from typing import Any

from trid3nt_server.workflows.lib import user_input
from trid3nt_server.workflows.telemac.steps import TelemacDyeScenarioError

__all__ = ["release_points"]

_CODE = "TELEMAC_PARAMS_INVALID"


def release_points(args: dict[str, Any]) -> dict[str, Any]:
    """The release point, and the point the WORKER seeds the reach from.

    Models split the same value across three shapes - an explicit pair, split
    lon/lat, or one "lat,lon" string - and dropping the ones the signature does
    not name is the silent-swallow class.
    """
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

    if args.get("_seed_release_lon") is not None \
            or args.get("_seed_release_lat") is not None:
        seed = [args.get("_seed_release_lon"), args.get("_seed_release_lat")]
    else:
        seed = None if args.get("_release_seeds_reach") is False else release
    return {
        "release_coords": user_input.lonlat_point(release, label="release point",
                                                  code=_CODE),
        "reach_seed_coords": user_input.lonlat_point(seed, label="reach seed point",
                                                     code=_CODE),
    }
