"""VDatum hooks: the request one conversion needs, and the offset read off it.

The service couples each vertical frame to the horizontal frame and the geoid
model it is served under, and answers a mismatched pairing with a number that
reads plausible and is not the offset, so the pairing is PINNED here rather than
searched. The height sent is zero, so the height that comes back is the offset.
Which of the service's regional grids answers is a fact of the POINT and is
chosen here, because the service serves no lookup from one to the other.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..._router.errors import router_empty_error, router_input_error, router_upstream_error
from ..._router.hooks import RequestPlan, register_hook

logger = logging.getLogger(__name__)

__all__ = ["build_request", "record"]

#: What VDatum serves each vertical frame under: its horizontal frame and its
#: geoid model. A pairing the service does not couple is answered with an
#: ELLIPSOID height rather than a refusal - NGVD29 asked for under NAD83_2011
#: comes back tens of metres off - so a frame is only offered here with the
#: pairing it was verified under.
_SERVED_AS: dict[str, tuple[str, str]] = {
    "NAVD88": ("NAD83_2011", "geoid18"),
    "NGVD29": ("NAD27", "geoid18"),
    "EGM2008": ("WGS84_G1674", "egm2008"),
    "MLLW": ("NAD83_2011", "geoid18"),
    "MLW": ("NAD83_2011", "geoid18"),
    "MTL": ("NAD83_2011", "geoid18"),
    "LMSL": ("NAD83_2011", "geoid18"),
    "MHW": ("NAD83_2011", "geoid18"),
    "MHHW": ("NAD83_2011", "geoid18"),
    "IGLD85": ("NAD83_2011", "geoid18"),
    "LWD_IGLD85": ("NAD83_2011", "geoid18"),
}

#: What VDatum writes where a grid does not reach the point. It is a value in a
#: successful response, not an error, so a reader that does not know it treats a
#: miss as a 999 km offset.
_NO_COVERAGE = -999999.0

#: The ground each VDatum region's grids are published over, as coarse boxes,
#: MOST SPECIFIC FIRST: a tidal coast and an island each stand ahead of the
#: inland grid that spans them, because the region that owns a point's water
#: carries surfaces the wider one does not. A box is a region's coarse reach and
#: not its grid - which point inside one is covered is the service's own answer,
#: which it gives as its sentinel.
_REGION_BOXES: tuple[tuple[str, tuple[tuple[float, float, float, float], ...]], ...] = (
    ("as", ((-171.5, -15.2, -168.0, -13.8),)),
    ("gcnmi", ((144.0, 13.0, 146.5, 21.0),)),
    ("hi", ((-161.0, 18.0, -154.0, 23.0),)),
    ("prvi", ((-68.5, 17.0, -64.0, 19.0),)),
    ("sgi", ((-170.0, 56.4, -169.3, 56.7),)),
    ("spi", ((-170.6, 57.0, -170.0, 57.4),)),
    ("sli", ((-172.0, 62.8, -167.8, 64.0),)),
    ("seak", ((-141.0, 54.0, -130.0, 60.5),)),
    ("ak", ((-180.0, 51.0, -129.0, 72.0),)),
    ("westcoast", ((-130.0, 36.0, -121.0, 49.5),
                   (-123.0, 32.3, -117.0, 36.5))),
    ("chesapeak_delaware", ((-77.5, 36.5, -74.5, 40.3),)),
    ("wgom", ((-97.8, 25.8, -88.0, 30.7),)),
    ("contiguous", ((-125.0, 24.0, -66.5, 49.5),)),
)


def _region(spec: SourceSpec, lon: float, lat: float) -> str:
    """The VDatum region whose grids stand over this point, or a typed refusal.

    The service serves no lookup from a point to its region and answers the
    wrong one with a refusal or a sentinel, so the point chooses: the first
    region whose published ground covers it, coastal and island grids ahead of
    the inland one they stand inside of."""
    for name, boxes in _REGION_BOXES:
        if any(west <= lon <= east and south <= lat <= north
               for west, south, east, north in boxes):
            return name
    raise router_input_error(
        spec.error_code_prefix,
        f"no NOAA VDatum region stands over ({lon}, {lat}): the service covers "
        f"the US and its territories only ({', '.join(n for n, _ in _REGION_BOXES)}), "
        "and an offset between two vertical frames outside them is not published.",
        spec.input_error_suffix,
    )


def _frame(spec: SourceSpec, params: dict[str, Any], key: str) -> str:
    """One frame param as the name VDatum knows it by, or a typed refusal."""
    name = str(params.get(key) or "").strip().upper()
    if name not in _SERVED_AS:
        raise router_input_error(
            spec.error_code_prefix,
            f"{key}={params.get(key)!r} is not a vertical frame this fetch "
            f"serves (known: {sorted(_SERVED_AS)}). A local project datum - a "
            "river datum, a district's station datum - is not one of NOAA "
            "VDatum's frames; its offset is published on the survey that uses it.",
            spec.input_error_suffix,
        )
    return name


@register_hook("vdatum.build_request")
def build_request(spec: SourceSpec, params: dict[str, Any]) -> list[RequestPlan]:
    """The one conversion request: a zero height on the source frame at the point."""
    source = _frame(spec, params, "from_frame")
    target = _frame(spec, params, "to_frame")
    lon, lat = (float(v) for v in params["point"])
    s_h, s_geoid = _SERVED_AS[source]
    t_h, t_geoid = _SERVED_AS[target]
    return [RequestPlan(url=str(spec.endpoints["convert"].url), params={
        "s_x": f"{lon:.7f}", "s_y": f"{lat:.7f}", "s_z": "0.0",
        "region": _region(spec, lon, lat),
        "s_coor": "geo", "s_h_frame": s_h, "s_v_frame": source,
        "s_v_geoid": s_geoid, "s_v_unit": "m",
        "t_coor": "geo", "t_h_frame": t_h, "t_v_frame": target,
        "t_v_geoid": t_geoid, "t_v_unit": "m",
    })]


@register_hook("vdatum.record")
def record(spec: SourceSpec, params: dict[str, Any],
           bodies: list[bytes]) -> dict[str, Any] | None:
    """The offset the conversion states, or the typed refusal it amounts to.

    VDatum answers a rejected request with HTTP 200 and an error envelope, and an
    uncovered point with its own sentinel height, so both are read here."""
    sc = spec.error_code_prefix
    try:
        answer = json.loads((bodies[0] if bodies else b"").decode("utf-8"))
    except (IndexError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise router_upstream_error(sc, f"NOAA VDatum returned non-JSON: {exc}")
    if not isinstance(answer, dict):
        raise router_upstream_error(
            sc, f"NOAA VDatum's answer is not a record: {type(answer).__name__}")
    source = _frame(spec, params, "from_frame")
    target = _frame(spec, params, "to_frame")
    lon, lat = (float(v) for v in params["point"])
    region = _region(spec, lon, lat)
    if "errorCode" in answer:
        raise router_input_error(
            sc,
            f"NOAA VDatum refused {source} -> {target} in region {region!r}, "
            f"the grid that stands over ({lon}, {lat}): {answer.get('message')!r}. "
            "Name two frames the service transforms between there.",
            spec.input_error_suffix,
        )
    try:
        offset = float(answer["t_z"])
    except (KeyError, TypeError, ValueError):
        raise router_upstream_error(
            sc, f"NOAA VDatum's answer carries no converted height: {answer!r}")
    if offset == _NO_COVERAGE:
        raise router_empty_error(
            sc,
            f"NOAA VDatum's {region} grids do not reach ({lon}, {lat}), so the "
            f"offset between {source} and {target} there is not published. "
            f"{region} is the region that stands over the point, and its grids "
            "leave gaps; ask at the water the question is about.",
            spec.empty_error_suffix,
        )
    uncertainty = _metres(answer.get("uncertainty"))
    logger.info("vdatum: %s -> %s at (%.5f, %.5f) in %s = %+.3f m",
                source, target, lon, lat, region, offset)
    return {
        "lon": lon, "lat": lat, "region": region,
        "from_frame": source, "to_frame": target,
        "offset_m": round(offset, 4),
        "uncertainty_m": uncertainty,
        "source": "NOAA VDatum",
    }


def _metres(stated: Any) -> float | None:
    """VDatum's own uncertainty as metres, or ``None`` where it reports none.

    An EGM2008 conversion runs through the ellipsoid and is answered with the
    string ``NaN``, which is the service saying it has no figure rather than a
    figure of zero."""
    try:
        value = float(str(stated).strip())
    except (TypeError, ValueError):
        return None
    return round(value, 4) if value == value else None
