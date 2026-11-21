"""The NOAA CO-OPS water-level station listing, read once for the rows over it.

CO-OPS publishes its whole network as one metadata document; each row's own rings clip it,
so the ocean coastline and the Great Lakes read the same document.
"""

from __future__ import annotations

import json

from trid3nt_contracts.coverage import CoveragePoint

from ..transport import get_client, get_bytes
from . import register_hook

__all__ = ["stations"]

_CATALOG = ("https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/"
            "stations.json?type=waterlevels&units=metric&format=json")

#: The listing states no per-station zero: a series is requested ON a datum, so the zero is the row's.


@register_hook("noaa_coops.stations")
def stations() -> list[CoveragePoint]:
    body, _ct, _url = get_bytes(get_client(), _CATALOG,
                                headers={"Accept": "application/json"})
    rows = json.loads(body.decode("utf-8")).get("stations") or []
    return [CoveragePoint(id=str(row["id"]), lon=float(row["lng"]),
                          lat=float(row["lat"]))
            for row in rows
            if row.get("id") and row.get("lat") is not None
            and row.get("lng") is not None]
