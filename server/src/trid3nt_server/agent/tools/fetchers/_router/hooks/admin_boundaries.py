"""admin_boundaries hooks (ADR 0067): the TIGER/Line ZIP URL planner.

The one irreducible per-source step for the ``zip_vector`` executor: turn a
``(level, bbox)`` request into the TIGER/Line 2024 ZIP URL(s) to fetch. Nationwide
levels (state / county / zcta) are one whole-US file; ``place`` fans out to the
per-state PLACE ZIP of every state whose envelope intersects the bbox (a bespoke
state-FIPS routing table + the antimeridian Aleutian tail). PURE: no I/O -- the
``zip_vector`` executor owns the download + extract + read + spatial filter + merge.
"""

from __future__ import annotations

from trid3nt_contracts.source_spec import SourceSpec

from ..errors import router_input_error
from ..hooks import RequestPlan, register_hook

_TIGER_BASE = "https://www2.census.gov/geo/tiger/TIGER2024"
_TIGER_YEAR = "2024"

# State FIPS -> approximate WGS84 envelope (min_lon, min_lat, max_lon, max_lat),
# ~10 km buffered so a bbox near a state border still routes to the right PLACE
# ZIP. Alaska's western Aleutians cross the antimeridian into positive longitudes,
# which no single WGS84 envelope can span, so AK gets a second envelope OR-ed in.
_ALASKA_FIPS = "02"
_ALASKA_ANTIMERIDIAN_BBOX = (172.0, 51.0, 180.0, 53.5)

_STATE_FIPS_BBOXES: dict[str, tuple[float, float, float, float]] = {
    "01": (-88.5, 30.1, -84.9, 35.0), "02": (-180.0, 51.0, -129.9, 71.5),
    "04": (-114.8, 31.3, -109.0, 37.0), "05": (-94.6, 33.0, -89.7, 36.5),
    "06": (-124.5, 32.5, -114.1, 42.0), "08": (-109.1, 36.9, -102.0, 41.0),
    "09": (-73.7, 40.9, -71.8, 42.1), "10": (-75.8, 38.4, -75.0, 39.8),
    "11": (-77.1, 38.8, -76.9, 39.0), "12": (-87.6, 24.4, -80.0, 31.0),
    "13": (-85.6, 30.3, -80.8, 35.0), "15": (-160.3, 18.9, -154.8, 22.2),
    "16": (-117.2, 42.0, -111.0, 49.0), "17": (-91.5, 36.9, -87.0, 42.5),
    "18": (-88.1, 37.8, -84.8, 41.8), "19": (-96.6, 40.4, -90.1, 43.5),
    "20": (-102.1, 36.9, -94.6, 40.0), "21": (-89.6, 36.5, -82.0, 39.1),
    "22": (-94.0, 28.9, -89.0, 33.0), "23": (-71.1, 43.0, -67.0, 47.5),
    "24": (-79.5, 37.9, -75.0, 39.7), "25": (-73.5, 41.2, -69.9, 42.9),
    "26": (-90.4, 41.7, -82.4, 48.3), "27": (-97.2, 43.5, -89.5, 49.4),
    "28": (-91.7, 30.1, -88.1, 35.0), "29": (-95.8, 35.9, -89.1, 40.6),
    "30": (-116.1, 44.4, -104.0, 49.0), "31": (-104.1, 40.0, -95.3, 43.0),
    "32": (-120.0, 35.0, -114.0, 42.0), "33": (-72.6, 42.7, -70.6, 45.3),
    "34": (-75.6, 38.9, -73.9, 41.4), "35": (-109.1, 31.3, -103.0, 37.0),
    "36": (-79.8, 40.5, -71.9, 45.0), "37": (-84.4, 33.8, -75.4, 36.6),
    "38": (-104.1, 45.9, -96.6, 49.0), "39": (-84.8, 38.4, -80.5, 42.3),
    "40": (-103.0, 33.6, -94.4, 37.0), "41": (-124.6, 41.9, -116.5, 46.3),
    "42": (-80.5, 39.7, -74.7, 42.3), "44": (-71.9, 41.1, -71.1, 42.0),
    "45": (-83.4, 32.0, -78.5, 35.2), "46": (-104.1, 42.5, -96.4, 45.9),
    "47": (-90.3, 35.0, -81.7, 36.7), "48": (-106.7, 25.8, -93.5, 36.5),
    "49": (-114.1, 37.0, -109.0, 42.0), "50": (-73.4, 42.7, -71.5, 45.0),
    "51": (-83.7, 36.5, -75.2, 39.5), "53": (-124.8, 45.5, -116.9, 49.0),
    "54": (-82.6, 37.2, -77.7, 40.6), "55": (-92.9, 42.5, -86.8, 47.1),
    "56": (-111.1, 40.9, -104.1, 45.0), "72": (-67.3, 17.9, -65.2, 18.6),
    "78": (-65.1, 17.6, -64.5, 18.5),
}


def _intersects(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    return a[0] <= b[2] and a[2] >= b[0] and a[1] <= b[3] and a[3] >= b[1]


def _state_fips_for_bbox(bbox: tuple[float, float, float, float]) -> list[str]:
    """Every state FIPS whose envelope intersects ``bbox`` (+ the AK Aleutian tail)."""
    out = [fips for fips, env in _STATE_FIPS_BBOXES.items() if _intersects(bbox, env)]
    if _ALASKA_FIPS not in out and _intersects(bbox, _ALASKA_ANTIMERIDIAN_BBOX):
        out.append(_ALASKA_FIPS)
    return out


def _nationwide_url(level: str) -> str:
    if level == "state":
        return f"{_TIGER_BASE}/STATE/tl_{_TIGER_YEAR}_us_state.zip"
    if level == "county":
        return f"{_TIGER_BASE}/COUNTY/tl_{_TIGER_YEAR}_us_county.zip"
    return f"{_TIGER_BASE}/ZCTA520/tl_{_TIGER_YEAR}_us_zcta520.zip"  # zcta


@register_hook("admin_boundaries.build_request")
def build_request(spec: SourceSpec, params: dict) -> list[RequestPlan]:
    """Plan the TIGER/Line ZIP URL(s) for ``(level, bbox)`` (pure)."""
    level = params["level"]
    bbox = tuple(params["bbox"])
    if level != "place":
        return [RequestPlan(url=_nationwide_url(level))]
    fips_list = _state_fips_for_bbox(bbox)
    if not fips_list:
        raise router_input_error(
            spec.error_code_prefix,
            f"bbox={bbox} is not routable to a TIGER state for level='place'; the "
            "per-state PLACE ZIPs require a bbox over US land within a state/territory "
            "envelope. Use level='county' (nationwide file) or a bbox over CONUS/AK/HI/PR/VI.",
            "LEVEL_INVALID",
        )
    return [
        RequestPlan(url=f"{_TIGER_BASE}/PLACE/tl_{_TIGER_YEAR}_{fips}_place.zip")
        for fips in fips_list
    ]
