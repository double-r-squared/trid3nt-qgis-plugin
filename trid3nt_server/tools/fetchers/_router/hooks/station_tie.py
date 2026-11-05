"""The NOAA CO-OPS station tie: a published pair of a geoid frame and a tidal zero, nearest the ask.

CO-OPS states each water-level station's datum heights above its own Station Datum on one page; a station
publishing BOTH NAVD88 and a tidal frame (MHW and its kin) measures the tie the local PROJ grids cannot -
they carry no tidal surface - and this module finds it: the station catalog read whole, the in-reach
stations ordered by great-circle distance, each datums page read until one carries the pair asked for.
Two rows name it: ``fetch_noaa_coops_datums`` resolves the tie strictly and refuses when none exists, and
``fetch_vertical_datum_offset`` resolves it best-effort as VDatum's fallback, never failing a pair VDatum
itself can answer. The resolved tie rides ``params["tie"]`` into the cache key, shaped as::

    {"station_id", "station_name", "lon", "lat", "distance_km",
     "datums": {name: metres-above-STND, ...}, "epoch", "units"}

and a search that found no tie rides ``params["tie_candidates"]`` (the station ids checked, empty where
none stood in reach) so a later refusal can name what was looked at.
"""

from __future__ import annotations

import json
import logging
import math
from typing import Any, NamedTuple

from trid3nt_contracts.source_spec import SourceSpec

from ..errors import (
    RouterError,
    router_empty_error,
    router_input_error,
    router_upstream_error,
)
from ..transport import TransportError, get_bytes, get_client
from . import register_hook

logger = logging.getLogger(__name__)

__all__ = [
    "GEODETIC_FRAMES",
    "TIDAL_FRAMES",
    "REACH_KM",
    "MAX_STATIONS",
    "distance_km",
    "msl_note",
    "search_note",
    "tie_at",
    "tie_for_offset",
]

#: The frames one side of a station-tie pair must name: what the local PROJ grids
#: convert between (the ellipsoid is a surface, not a zero a tie reads).
GEODETIC_FRAMES = frozenset({"NAVD88", "NGVD29", "EGM2008"})

#: The frames the other side must name: the tidal zeros a CO-OPS station publishes
#: beside NAVD88. Great Lakes frames (IGLD85, LWD_IGLD85) are not these.
TIDAL_FRAMES = frozenset({"MLLW", "MLW", "MTL", "LMSL", "MHW", "MHHW"})

#: How far a tie may stand from the question: a datum relation measured at a
#: station hundreds of kilometres away is not this place's measurement.
REACH_KM = 500.0

#: How many in-reach stations' datums pages are read before giving up, nearest
#: first; a dense coast has many stations and one page is one small JSON.
MAX_STATIONS = 10

#: IUGG mean Earth radius, the sphere the great-circle distances are on.
_EARTH_RADIUS_KM = 6371.0088


class _Selection(NamedTuple):
    """One tie search: the in-reach ids, the ids whose datums page was read, the tie or none, the last read failure."""

    near: list[str]
    checked: list[str]
    tie: dict[str, Any] | None
    failure: Exception | None


def distance_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Great-circle distance between two EPSG:4326 points, in kilometres."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    a = (math.sin(d_phi / 2.0) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2.0) ** 2)
    return 2.0 * _EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def _catalog(spec: SourceSpec) -> list[dict[str, Any]]:
    """Every water-level station the catalog publishes, with the coordinates the distance is measured on."""
    endpoint = spec.endpoints["stations_catalog"]
    body, _ct, _url = get_bytes(
        get_client(), str(endpoint.url), params=dict(endpoint.query or {}),
        headers={"Accept": "application/json"})
    rows = json.loads(body.decode("utf-8")).get("stations") or []
    return [row for row in rows
            if row.get("id") and row.get("lat") is not None
            and row.get("lng") is not None]


def _datums_page(spec: SourceSpec, station_id: str) -> tuple[dict[str, float], str, str]:
    """One station's datum heights (metres above its STND), its epoch and its units."""
    endpoint = spec.endpoints["station_datums"]
    url = str(endpoint.url_template).format(station=station_id)
    body, _ct, _url = get_bytes(
        get_client(), url, params=dict(endpoint.query or {}),
        headers={"Accept": "application/json"})
    try:
        doc = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise router_upstream_error(
            spec.error_code_prefix,
            f"NOAA CO-OPS datums for station {station_id} are not JSON: {exc}") from exc
    entries = doc.get("datums") if isinstance(doc, dict) else None
    if not isinstance(entries, list):
        raise router_upstream_error(
            spec.error_code_prefix,
            f"NOAA CO-OPS datums for station {station_id} carry no datums list: "
            f"{str(doc)[:200]}")
    heights: dict[str, float] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("name"):
            continue
        try:
            heights[str(entry["name"])] = float(entry["value"])
        except (KeyError, TypeError, ValueError):
            continue
    return heights, str(doc.get("epoch") or ""), str(doc.get("units") or "meters")


def _carrying_tie(spec: SourceSpec, lon: float, lat: float,
                  required: frozenset[str] | set[str]) -> _Selection:
    """The nearest in-reach station publishing every frame in ``required``, reading nearest-first.

    A station whose page cannot be read is skipped and recorded as the last failure rather than
    aborting the search; the caller decides whether a search with no page read is an error."""
    rows = _catalog(spec)
    scored: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        found = distance_km(lon, lat, float(row["lng"]), float(row["lat"]))
        if found <= REACH_KM:
            scored.append((found, row))
    scored.sort(key=lambda pair: pair[0])
    near = [str(row["id"]) for _dist, row in scored[:MAX_STATIONS]]
    checked: list[str] = []
    failure: Exception | None = None
    for distance, row in scored[:MAX_STATIONS]:
        station_id = str(row["id"])
        try:
            heights, epoch, units = _datums_page(spec, station_id)
        except (TransportError, RouterError) as exc:
            logger.info("station_tie: station %s datums unreadable, skipping: %s",
                        station_id, exc)
            failure = exc
            continue
        checked.append(station_id)
        if not required <= heights.keys():
            continue
        return _Selection(near, checked, {
            "station_id": station_id,
            "station_name": str(row.get("name") or ""),
            "lon": float(row["lng"]),
            "lat": float(row["lat"]),
            "distance_km": round(distance, 1),
            "datums": heights,
            "epoch": epoch,
            "units": units,
        }, failure)
    return _Selection(near, checked, None, failure)


def msl_note(tidal: str) -> str:
    """The approximation that stands where the pair does not, named so no refusal hides it.

    Mean sea level stands in for the geoid and the station's ``tidal``-above-MSL
    for the pair; a run never takes that silently, and this fetch does not take
    it at all - the note is how a reader learns the fallback exists."""
    return (f"MSL standing in for the geoid with a station's {tidal} above MSL "
            "is the available approximation, and it is never taken without the "
            "user's explicit gate; this fetch does not take it")


def search_note(params: dict[str, Any], tidal: str) -> str:
    """What the offset row's station search left behind, for a refusal to append to VDatum's words.

    Empty where no search ran: a direct call without a resolve phase carries neither ``tie`` nor
    ``tie_candidates``, and that refusal must read exactly as it always has."""
    if "tie_candidates" not in params or not tidal:
        return ""
    checked = params.get("tie_candidates") or []
    if not checked:
        return (f"no NOAA CO-OPS water-level station stands within "
                f"{REACH_KM:.0f} km of the point, so no station tie is published "
                "for it")
    listed = ", ".join(str(station) for station in checked)
    return (f"none of the NOAA CO-OPS stations checked ({listed}) publishes "
            f"both {tidal} and NAVD88, so the station tie is unavailable; "
            f"{msl_note(tidal)}")


@register_hook("station_tie.tie_at")
def tie_at(spec: SourceSpec, params: dict[str, Any]) -> dict[str, Any]:
    """``fetch_noaa_coops_datums``' pre-resolve: the nearest in-reach station publishing MHW AND NAVD88, or a typed refusal.

    The search runs before the cache key so a resolved tie and an unresolved point never share an
    entry; every failure here is typed on this row rather than left for a later phase to guess."""
    lon, lat = (float(v) for v in params["point"])
    try:
        selection = _carrying_tie(spec, lon, lat, {"NAVD88", "MHW"})
    except TransportError as exc:
        raise router_upstream_error(
            spec.error_code_prefix,
            f"the NOAA CO-OPS station catalog could not be read: {exc}",
            exc.retryable) from exc
    if not selection.near:
        raise router_input_error(
            spec.error_code_prefix,
            f"no NOAA CO-OPS water-level station stands within {REACH_KM:.0f} km "
            f"of ({lon}, {lat}), so no station tie is published for this point; "
            "the network covers the US ocean coastline, the Great Lakes shore and "
            "the territories.",
            spec.input_error_suffix)
    if not selection.checked:
        raise router_upstream_error(
            spec.error_code_prefix,
            f"the datums of every station in reach could not be read "
            f"({', '.join(selection.near)}): {selection.failure}")
    if selection.tie is None:
        checked = ", ".join(selection.checked) or "none"
        raise router_empty_error(
            spec.error_code_prefix,
            f"the NOAA CO-OPS stations within {REACH_KM:.0f} km publish no pair "
            f"of MHW and NAVD88 (checked: {checked}); a station tie needs both "
            f"heights on one page, and none of these states them together; "
            f"{msl_note('MHW')}",
            spec.empty_error_suffix)
    return {"tie": selection.tie}


@register_hook("station_tie.tie_for_offset")
def tie_for_offset(spec: SourceSpec, params: dict[str, Any]) -> dict[str, Any]:
    """``fetch_vertical_datum_offset``'s pre-resolve: the station tie a VDatum refusal would fall back to, best-effort.

    Only a geodetic-by-tidal pair can be composed from a station, so every other pair - identity, two
    geodetic frames, two tidal frames, the Great Lakes frames - resolves to nothing with no request.
    A catalog or page it cannot read resolves to nothing too: the offset ask then proceeds on VDatum
    alone and its refusal is the honest answer, never a failure caused by an optional fallback."""
    source = str(params.get("from_frame") or "").strip().upper()
    target = str(params.get("to_frame") or "").strip().upper()
    if not source or source == target:
        return {}
    straddling = (({source, target} & GEODETIC_FRAMES),
                  ({source, target} & TIDAL_FRAMES))
    if len(straddling[0]) != 1 or len(straddling[1]) != 1:
        return {}
    tidal = next(iter(straddling[1]))
    lon, lat = (float(v) for v in params["point"])
    try:
        selection = _carrying_tie(spec, lon, lat, {"NAVD88", tidal})
    except TransportError as exc:
        logger.warning(
            "station_tie: catalog unreachable at (%.5f, %.5f), the offset falls "
            "back to VDatum alone: %s", lon, lat, exc)
        return {}
    if selection.tie is not None:
        return {"tie": selection.tie}
    if not selection.near:
        return {"tie_candidates": []}
    if not selection.checked:
        logger.warning(
            "station_tie: no datums page readable in reach of (%.5f, %.5f), the "
            "offset falls back to VDatum alone: %s", lon, lat, selection.failure)
        return {}
    return {"tie_candidates": selection.checked}
