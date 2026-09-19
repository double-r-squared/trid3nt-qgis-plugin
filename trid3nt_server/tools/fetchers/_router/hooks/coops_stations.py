"""The NOAA CO-OPS water-level station listing, read once for the rows over it.

CO-OPS publishes its whole water-level network as one metadata document, which
is the bounded listing a coverage row's station set needs; the row's own rings
clip that listing to the part of the network the row is drawn over, so the ocean
coastline and the Great Lakes read the same document.
"""

from __future__ import annotations

import json

from trid3nt_contracts.coverage import CoveragePoint

from ..transport import get_client, get_bytes
from . import register_hook

__all__ = ["stations"]

_CATALOG = ("https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/"
            "stations.json?type=waterlevels&units=metric&format=json")

#: The listing states no per-station zero. A CO-OPS series is requested ON a
#: datum and comes back counted from it, so the zero is the row's and not the
#: station's, and stating one per station here would invent a second answer.


@register_hook("noaa_coops.stations")
def stations() -> list[CoveragePoint]:
    """Every CO-OPS water-level station, by the id the datagetter is called with."""
    body, _ct, _url = get_bytes(get_client(), _CATALOG,
                                headers={"Accept": "application/json"})
    rows = json.loads(body.decode("utf-8")).get("stations") or []
    return [CoveragePoint(id=str(row["id"]), lon=float(row["lng"]),
                          lat=float(row["lat"]))
            for row in rows
            if row.get("id") and row.get("lat") is not None
            and row.get("lng") is not None]
