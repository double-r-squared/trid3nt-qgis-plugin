"""Datum-offset hooks: the local PROJ-grid answer for geodetic frames, else the request VDatum needs.

A pair of two geodetic frames is answered from PROJ's grids where they cover the point, with no request. Tidal
frames, Great Lakes frames and a point the grids do not cover go to VDatum, which couples each vertical frame to
the horizontal frame and the geoid model it is served under and answers a mismatched pairing with a number that
reads plausible and is not the offset, so the pairing is PINNED rather than searched. The height sent is zero, so
the height that comes back is the offset; the service's regional grid is chosen here from the point. A
geodetic-by-tidal pair VDatum refuses falls back to the station tie the resolve phase resolved onto
``params["tie"]`` - the grids' step to NAVD88 plus the CO-OPS station's published height - and a frame asked
on both sides is one zero, answering 0.0 with no call."""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ... import _proj_grids
from ..._router.errors import router_empty_error, router_input_error, router_upstream_error
from ..._router.hooks import RequestPlan, register_hook
from ..._router.hooks.station_tie import search_note

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

#: The frames the local PROJ grids convert between; a pair with any other frame is VDatum's.
_GEODETIC = frozenset({"NAVD88", "NGVD29", "EGM2008"})

#: The frames VDatum serves off a TIDAL grid rather than off a geoid model. A
#: region may demand its own horizontal frame for these and for no other.
_TIDAL = frozenset({"MLLW", "MLW", "MTL", "LMSL", "MHW", "MHHW"})

#: What a REGION demands of a tidal frame's horizontal frame, where it demands
#: something other than what ``_SERVED_AS`` pairs that frame with everywhere
#: else. West-coast and West Gulf Coast tidal grids are published on IGS14 and the
#: service refuses the pairing in its own words - "For West Coast Region, Target
#: Horizontal Frame should be IGS14 for Tidal" - whichever side of the conversion
#: the tidal frame sits on.
_TIDAL_HORIZONTAL: dict[str, str] = {"westcoast": "IGS14", "wgom": "IGS14"}

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
        f"and an offset between two vertical frames outside them is not published; "
        f"{_served_note(lon, lat)}.",
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


def _horizontal(region: str, frame: str, served: str) -> str:
    """The horizontal frame ``region`` serves this vertical frame under."""
    demanded = _TIDAL_HORIZONTAL.get(region)
    return demanded if demanded is not None and frame in _TIDAL else served


@lru_cache(maxsize=256)
def _local(source: str, target: str, lon: float, lat: float) -> tuple[float, list[str]] | None:
    """The PROJ-grid offset and the grid files it read, or ``None`` where no grid covers the point."""
    try:
        return (float(_proj_grids.geodetic_offset(source, target, lon, lat)),
                _proj_grids.grids_used(source, target, lon, lat))
    except _proj_grids.NoGridCoverage:
        return None


def _answered_locally(spec: SourceSpec, params: dict[str, Any]):
    source = _frame(spec, params, "from_frame")
    target = _frame(spec, params, "to_frame")
    if not {source, target} <= _GEODETIC:
        return None
    lon, lat = (float(v) for v in params["point"])
    try:
        return _local(source, target, lon, lat)
    except _proj_grids.DatumGridUnavailable as exc:
        raise router_upstream_error(spec.error_code_prefix, str(exc)) from exc


@register_hook("vdatum.build_request")
def build_request(spec: SourceSpec, params: dict[str, Any]) -> list[RequestPlan]:
    """The one conversion request: a zero height on the source frame at the point; none where PROJ's grids answer."""
    source = _frame(spec, params, "from_frame")
    target = _frame(spec, params, "to_frame")
    if source == target:
        return []
    if _answered_locally(spec, params) is not None:
        return []
    lon, lat = (float(v) for v in params["point"])
    region = _region(spec, lon, lat)
    s_h, s_geoid = _SERVED_AS[source]
    t_h, t_geoid = _SERVED_AS[target]
    s_h, t_h = (_horizontal(region, source, s_h), _horizontal(region, target, t_h))
    return [RequestPlan(url=str(spec.endpoints["convert"].url), params={
        "s_x": f"{lon:.7f}", "s_y": f"{lat:.7f}", "s_z": "0.0",
        "region": region,
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
    uncovered point with its own sentinel height, so both are read here. A
    geodetic-by-tidal pair it refuses falls to the station tie resolved onto
    ``params["tie"]``, composing the grids' step to NAVD88 with the station's
    published heights; a refusal with no tie to fall to names the stations the
    search checked."""
    sc = spec.error_code_prefix
    source = _frame(spec, params, "from_frame")
    target = _frame(spec, params, "to_frame")
    lon, lat = (float(v) for v in params["point"])
    if source == target:
        return {
            "lon": lon, "lat": lat, "region": "identity",
            "from_frame": source, "to_frame": target,
            "offset_m": 0.0, "uncertainty_m": None,
            "source": "the same frame on both sides: one zero, nothing to convert",
        }
    local = _answered_locally(spec, params)
    if local is not None:
        offset, grids = local
        return {
            "lon": lon, "lat": lat, "region": "proj-grids",
            "from_frame": source, "to_frame": target,
            "offset_m": round(offset, 4), "uncertainty_m": None,
            "source": f"PROJ grids ({', '.join(grids)})",
        }
    try:
        answer = json.loads((bodies[0] if bodies else b"").decode("utf-8"))
    except (IndexError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise router_upstream_error(sc, f"NOAA VDatum returned non-JSON: {exc}")
    if not isinstance(answer, dict):
        raise router_upstream_error(
            sc, f"NOAA VDatum's answer is not a record: {type(answer).__name__}")
    if "errorCode" in answer:
        composed = _from_tie(spec, params, source, target, lon, lat)
        if composed is not None:
            return composed
        region = _region(spec, lon, lat)
        note = search_note(params, _tidal_of(source, target))
        raise router_input_error(
            sc,
            f"NOAA VDatum refused {source} -> {target} in region {region!r}, "
            f"the grid that stands over ({lon}, {lat}): {answer.get('message')!r}. "
            f"{_served_note(lon, lat)}{_joined(note)}.",
            spec.input_error_suffix,
        )
    try:
        offset = float(answer["t_z"])
    except (KeyError, TypeError, ValueError):
        raise router_upstream_error(
            sc, f"NOAA VDatum's answer carries no converted height: {answer!r}")
    if offset == _NO_COVERAGE:
        composed = _from_tie(spec, params, source, target, lon, lat)
        if composed is not None:
            return composed
        region = _region(spec, lon, lat)
        note = search_note(params, _tidal_of(source, target))
        raise router_empty_error(
            sc,
            f"NOAA VDatum's {region} grids do not reach ({lon}, {lat}), so the "
            f"offset between {source} and {target} there is not published; "
            f"{_served_note(lon, lat)}{_joined(note)}.",
            spec.empty_error_suffix,
        )
    region = _region(spec, lon, lat)
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


def _tidal_of(source: str, target: str) -> str:
    """The tidal frame of a pair, or ``""`` where the pair carries none to search a tie for."""
    if source in _TIDAL:
        return source
    return target if target in _TIDAL else ""


def _joined(note: str) -> str:
    """The station-search clause beside what the grids convert, or ``""`` where no search ran."""
    return f"; {note}" if note else ""


def _seam_to_navd88(spec: SourceSpec, frame: str, lon: float, lat: float) -> tuple[float, list[str]]:
    """The local grids' step from ``frame`` to NAVD88 at the point, and the grid files read; NAVD88 is its own seam.

    The station tie alone is not the conversion: where the grids cannot step to
    NAVD88 the composition refuses by naming that gap rather than answering with
    the station's half of it."""
    if frame == "NAVD88":
        return 0.0, []
    try:
        found = _local(frame, "NAVD88", lon, lat)
    except _proj_grids.DatumGridUnavailable as exc:
        raise router_upstream_error(spec.error_code_prefix, str(exc)) from exc
    if found is None:
        raise router_empty_error(
            spec.error_code_prefix,
            f"a NOAA CO-OPS station tie reaches ({lon}, {lat}), but the local "
            f"PROJ grids do not convert {frame} -> NAVD88 there, so the step the "
            f"tie needs is not published; {_served_note(lon, lat)}.",
            spec.empty_error_suffix)
    return found


def _from_tie(spec: SourceSpec, params: dict[str, Any], source: str, target: str,
              lon: float, lat: float) -> dict[str, Any] | None:
    """The station-tie offset for a pair VDatum refused, or ``None`` where no tie reached this ask.

    Composes the grids' step to NAVD88 with the station's published NAVD88 and
    tidal heights: geodetic -> tidal is seam + (NAVD88 - tidal), tidal ->
    geodetic is its inverse. A tie missing the pair asked for answers nothing."""
    tie = params.get("tie")
    if not isinstance(tie, dict):
        return None
    if source in _GEODETIC and target in _TIDAL:
        geodetic, tidal = source, target
    elif target in _GEODETIC and source in _TIDAL:
        geodetic, tidal = target, source
    else:
        return None
    datums = tie.get("datums") or {}
    try:
        navd88 = float(datums["NAVD88"])
        tidal_height = float(datums[tidal])
    except (KeyError, TypeError, ValueError):
        return None
    seam, grids = _seam_to_navd88(spec, geodetic, lon, lat)
    offset = seam + (navd88 - tidal_height) if source == geodetic \
        else (tidal_height - navd88) - seam
    station_id = str(tie.get("station_id") or "")
    station_name = str(tie.get("station_name") or "")
    distance = tie.get("distance_km")
    if grids:
        provenance = (f"PROJ grids ({', '.join(grids)}) + NOAA CO-OPS station "
                      f"{station_id} ({station_name}, {distance} km)")
    else:
        provenance = f"NOAA CO-OPS station {station_id} ({station_name}, {distance} km)"
    logger.info("station_tie: %s -> %s at (%.5f, %.5f) = %+.4f m via %s",
                source, target, lon, lat, offset, provenance)
    return {
        "lon": lon, "lat": lat, "region": "station-tie",
        "from_frame": source, "to_frame": target,
        "offset_m": round(offset, 4), "uncertainty_m": None,
        "source": provenance,
        "tie_station_id": station_id,
        "tie_distance_km": distance,
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


def _served_note(lon: float, lat: float) -> str:
    """What the local grids convert between at this point, for a refusal to name alongside VDatum's."""
    pairs = _proj_grids.served_pairs(lon, lat)
    if pairs is None:
        return "the local PROJ grids could not be checked at this point"
    if not pairs:
        return ("the local PROJ grids convert no pair of NGVD29, NAVD88 and "
                "EGM2008 at this point")
    spelled = ", ".join(f"{a} <-> {b}" for a, b in pairs)
    return f"the local PROJ grids at this point convert {spelled}"
