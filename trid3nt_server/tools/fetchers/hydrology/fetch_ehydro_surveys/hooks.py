"""eHydro delegate hooks: the survey index, then the survey's own package.

The index is one ArcGIS layer over every published survey; the soundings live in
each survey's ZIP, in the geodatabase beside its XYZ. The delegate owns both round
trips and the geodatabase read, and states the datum the survey states.
"""

# WHAT THE SURVEY PUBLISHES. Beside the thinned XYZ, an eHydro package carries the
# surface as an Esri TIN and a geodatabase of derived features - there is no raster
# in it. The measured thing is the SurveyPoint feature class, and a raster bed is
# the bed slot's own survey grid over these points, not something to invent here.
#
# The values are DEPTHS BELOW the survey's own datum, positive down, in the unit the
# points state. Nothing here converts between datums: the package's own metadata
# states how far its project datum sits above a national frame, and that sentence is
# reported as it was measured - applying it is the bed slot's, never this fetch's.

from __future__ import annotations

import datetime as _dt
import logging
import math
import re
import tempfile
from pathlib import Path
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..._router.errors import router_empty_error, router_input_error, router_upstream_error
from ..._router.hooks import register_hook
from ..._router.transport import TransportError, get_bytes, get_client, get_zip

logger = logging.getLogger(__name__)

__all__ = ["validate", "read"]

#: The index fields the rows are built from.
_OUT_FIELDS = (
    "surveyjobidpk", "sdsfeaturename", "surveytype", "usacedistrictcode",
    "surveydateend", "sourcedatalocation",
)

#: One page of the index, and the most pages walked. The index is read WHOLE
#: over the bbox, because which surveys cover the reach is not answerable from
#: its newest page: a channel's southern half can be covered only by a pass five
#: years old, and a page ordered by date would never reach it.
_INDEX_PAGE = 2000
_INDEX_PAGES = 10

#: How many packages one fetch downloads. Each survey is its own multi-megabyte
#: ZIP, so the selection stops here rather than truncating silently.
_MAX_SURVEYS = 12

#: How much of the AOI a survey has to cover that no NEWER selected survey
#: already covers, as a fraction of the AOI, before its package is worth
#: downloading. A survey overlapping its neighbour by a sliver adds a package
#: and no bed.
_NEW_GROUND = 0.01

#: The feature class carrying the measured soundings, and the three fields a bed
#: cannot be built without.
_SOUNDING_LAYER = "SurveyPoint"
_DEPTH_FIELD = "Z_depth"
_DATUM_FIELD = "elevationDatum"
_UOM_FIELD = "elevationUOM"

#: WHAT A DEPTH IS COUNTED FROM, by the word the survey's own points state it in.
#: A depth is counted from a WATER SURFACE and never from a reference system: a
#: Great Lakes survey writes IGLD85 on every point and its plot sheet says "ALL
#: SOUNDINGS ARE REFERENCED TO INTERNATIONAL GREAT LAKES DATUM OF 1985 (IGLD85)
#: L.W.D.", so the zero is the Low Water Datum expressed on that system - the
#: surface NOAA VDatum serves as LWD_IGLD85, 176 m above the system's own zero on
#: Lake Huron. A word not listed here is the zero the records already state.
_DEPTH_ZERO = {"igld85": "LWD_IGLD85"}

#: Metres per stated unit. An unlisted unit refuses: a survey measured in a unit
#: nobody here can name is not a survey anyone can build a bed from.
_METRES_PER_UNIT = {
    "ussurveyfoot": 1200.0 / 3937.0,
    "ussurveyfeet": 1200.0 / 3937.0,
    "surveyfoot": 1200.0 / 3937.0,
    "foot": 0.3048,
    "feet": 0.3048,
    "ft": 0.3048,
    "meter": 1.0,
    "metre": 1.0,
    "meters": 1.0,
    "m": 1.0,
}


def _since(spec: SourceSpec, params: dict[str, Any]) -> _dt.date | None:
    """The ``since`` bound as a date, or ``None`` where the caller stated none.

    A BED is a static measurement, so age ranks a survey and never filters it:
    an old pass is the best measured bed there is over ground nothing newer
    covers. A caller comparing one dredging against another states the date and
    that statement IS the filter."""
    raw = params.get("since")
    if raw is None or not str(raw).strip():
        return None
    try:
        return _dt.date.fromisoformat(str(raw).strip())
    except ValueError as exc:
        raise router_input_error(
            spec.error_code_prefix,
            f"since={raw!r} is not an ISO date (YYYY-MM-DD): {exc}",
            spec.input_error_suffix,
        )


@register_hook("ehydro.validate")
def validate(spec: SourceSpec, params: dict[str, Any]) -> None:
    """Refuse an unparseable ``since`` before the cache read and before the network."""
    _since(spec, params)


def _index(spec: SourceSpec, params: dict[str, Any]) -> list[dict[str, Any]]:
    """EVERY survey intersecting the bbox, as GeoJSON features, paged to the end.

    Which surveys cover the reach is a question about all of them: the index is
    walked until a page comes back short."""
    import json

    sc = spec.error_code_prefix
    west, south, east, north = (float(v) for v in params["bbox"])
    query = {
        "geometry": f"{west},{south},{east},{north}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "outSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "where": "1=1",
        "outFields": ",".join(_OUT_FIELDS),
        "returnGeometry": "true",
        "orderByFields": "surveydateend DESC",
        "resultRecordCount": str(_INDEX_PAGE),
        "f": "geojson",
    }
    features: list[dict[str, Any]] = []
    for page in range(_INDEX_PAGES):
        try:
            body, _ct, _url = get_bytes(
                get_client(), str(spec.endpoints["index"].url),
                params={**query, "resultOffset": str(page * _INDEX_PAGE)})
            parsed = json.loads(body.decode("utf-8"))
        except TransportError as exc:
            raise router_upstream_error(sc, f"the eHydro survey index failed: {exc}")
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise router_upstream_error(
                sc, f"the eHydro survey index returned non-JSON: {exc}")
        found = [f for f in (parsed.get("features") or []) if isinstance(f, dict)]
        features += found
        if len(found) < _INDEX_PAGE:
            break
    return features


def _survey_date(feature: dict[str, Any]) -> _dt.date | None:
    """The survey's end date; the index publishes it as epoch milliseconds."""
    raw = (feature.get("properties") or {}).get("surveydateend")
    try:
        return _dt.datetime.fromtimestamp(float(raw) / 1000.0, tz=_dt.timezone.utc).date()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _selected(spec: SourceSpec, features: list[dict[str, Any]],
              since: _dt.date | None, bbox: Any) -> list[dict[str, Any]]:
    """The surveys that COVER the AOI, newest first, under the package cap.

    Age ranks and never filters: the newest pass over each part of the AOI is
    the bed there, and where nothing newer reached, an older one is the only
    measurement that exists. A survey covering nothing the passes above it left
    is a package that buys no bed, so it is not downloaded."""
    import shapely
    from shapely.geometry import box, shape

    sc = spec.error_code_prefix
    dated = [(f, _survey_date(f)) for f in features]
    usable = [(f, d) for f, d in dated if d is not None
              and (f.get("properties") or {}).get("sourcedatalocation")]
    if not usable:
        raise router_empty_error(
            sc,
            "no USACE eHydro survey covers this extent. eHydro maps federal "
            "navigation channels only; there is no survey here to read.",
            spec.empty_error_suffix,
        )
    usable.sort(key=lambda row: row[1], reverse=True)
    if since is not None:
        newest = usable[0][1]
        usable = [(f, d) for f, d in usable if d >= since]
        if not usable:
            raise router_empty_error(
                sc,
                f"no eHydro survey over this extent ends on or after "
                f"{since.isoformat()}; the newest one here is "
                f"{newest.isoformat()}.",
                spec.empty_error_suffix,
            )
    west, south, east, north = (float(v) for v in bbox)
    aoi = box(west, south, east, north)
    uncovered, taken = aoi, []
    for feature, _date in usable:
        if len(taken) == _MAX_SURVEYS or uncovered.is_empty:
            break
        try:
            footprint = shapely.make_valid(shape(feature["geometry"]))
        except Exception:  # noqa: BLE001 -- an unreadable footprint covers nothing
            continue
        if footprint.intersection(uncovered).area < _NEW_GROUND * aoi.area:
            continue
        uncovered = uncovered.difference(footprint)
        taken.append(feature)
    if not taken:
        raise router_empty_error(
            sc,
            f"none of the {len(usable)} eHydro surveys intersecting this extent "
            f"covers more than {_NEW_GROUND:.0%} of it, so no package here "
            "would add a bed. Widen the bbox onto the channel the surveys run "
            "along.",
            spec.empty_error_suffix,
        )
    return taken


def _package(spec: SourceSpec, url: str, survey_id: str) -> Any:
    """The survey's own ZIP, opened once: the soundings and the metadata are in it."""
    try:
        return get_zip(get_client(), url)
    except TransportError as exc:
        raise router_upstream_error(
            spec.error_code_prefix,
            f"survey {survey_id} could not be downloaded: {exc}")


def _soundings(spec: SourceSpec, archive: Any, survey_id: str) -> Any:
    """The survey package's own SurveyPoint feature class, in EPSG:4326."""
    import geopandas as gpd

    sc = spec.error_code_prefix
    names = sorted({name.split("/")[0] for name in archive.namelist()})
    gdb = next((n for n in names if n.lower().endswith(".gdb")), None)
    if gdb is None:
        raise router_upstream_error(
            sc, f"survey {survey_id} carries no geodatabase (members: {names[:8]})")
    with tempfile.TemporaryDirectory(prefix="trid3nt_ehydro_") as scratch:
        archive.extractall(scratch, members=[n for n in archive.namelist()
                                             if n.startswith(gdb)])
        try:
            points = gpd.read_file(
                f"{Path(scratch) / gdb}", layer=_SOUNDING_LAYER, engine="pyogrio")
        except Exception as exc:  # noqa: BLE001 -- any reader fault, named by survey
            raise router_upstream_error(
                sc, f"survey {survey_id}'s {_SOUNDING_LAYER} could not be read: {exc}")
    for field in (_DEPTH_FIELD, _DATUM_FIELD, _UOM_FIELD):
        if field not in points.columns:
            raise router_input_error(
                sc,
                f"survey {survey_id} states no {field}, so what its numbers are "
                "measured from is unknown and nothing here can supply it. Ask for "
                "another survey over this channel.",
                spec.input_error_suffix,
            )
    return points.to_crs(4326) if points.crs is not None else points


#: The national frames a district's metadata names its project datum against, by
#: the words the published sentence spells them in. A frame nobody here can name
#: is reported as no offset at all rather than as a guess.
_FRAME_WORDS: tuple[tuple[str, str], ...] = (
    ("north american vertical datum of 1988", "NAVD88"),
    ("navd 88", "NAVD88"),
    ("navd88", "NAVD88"),
    ("national geodetic vertical datum of 1929", "NGVD29"),
    ("ngvd 29", "NGVD29"),
    ("ngvd29", "NGVD29"),
    ("mean lower low water", "MLLW"),
    ("mllw", "MLLW"),
    ("international great lakes datum", "IGLD85"),
    ("igld 85", "IGLD85"),
)

#: The published sentence a project datum's offset is stated in: "CRD is 5.28 feet
#: above the North American Vertical Datum of 1988". The unit is the survey's own
#: word for it, and "below" is the same measurement read the other way.
_OFFSET_SENTENCE = re.compile(
    r"(?P<datum>[A-Za-z0-9 ]{2,40}?)\s+is\s+(?P<value>[0-9]+(?:\.[0-9]+)?)\s+"
    r"(?P<unit>[A-Za-z]+)\s+(?P<sense>above|below)\s+(?P<frame>[^.]{3,120})",
    re.IGNORECASE)


def _published_offset(archive: Any, survey_id: str) -> tuple[float | None, str]:
    """How far this survey's own datum sits above a national frame, off its metadata.

    A district publishes the shift as a sentence in the package's XML abstract and
    nowhere machine-readable, so the sentence is what is read. ``(None, "")``
    wherever no sentence names a frame this can spell - a shift nobody measured is
    never invented, and the bed slot refuses instead."""
    for name in archive.namelist():
        if not name.lower().endswith(".xml"):
            continue
        text = " ".join(archive.read(name).decode("utf-8", "replace").split())
        for match in _OFFSET_SENTENCE.finditer(text):
            frame_text = match.group("frame").lower()
            frame = next((frame for words, frame in _FRAME_WORDS
                          if words in frame_text), "")
            scale = _METRES_PER_UNIT.get(
                match.group("unit").lower().rstrip("s").replace(" ", ""))
            if not frame or scale is None:
                continue
            metres = float(match.group("value")) * scale
            signed = metres if match.group("sense").lower() == "above" else -metres
            logger.info("ehydro: survey %s states its datum %+.4f m on %s",
                        survey_id, signed, frame)
            return round(signed, 4), frame
    return None, ""


def _counted_from(datum: str, quantity: str) -> str:
    """The zero a survey's numbers are counted from, off the word its points state.

    A DEPTH names the water surface it hangs below; an elevation names the system
    it stands on, and one word spells both surfaces."""
    if not str(quantity or "").startswith("depth"):
        return datum
    return _DEPTH_ZERO.get(re.sub(r"[^a-z0-9]", "", datum.lower()), datum)


def _stated(spec: SourceSpec, points: Any, survey_id: str) -> tuple[str, str, float]:
    """The datum, the unit and the metres per unit the survey itself states."""
    sc = spec.error_code_prefix
    datums = sorted({str(v).strip() for v in points[_DATUM_FIELD].dropna().unique()
                     if str(v).strip()})
    units = sorted({str(v).strip() for v in points[_UOM_FIELD].dropna().unique()
                    if str(v).strip()})
    if len(datums) != 1 or len(units) != 1:
        # THE SURVEY is what cannot be read, not the ask: the bed is refused and
        # nothing chooses a zero, and a slot matched here moves on to the next
        # source rather than the whole run stopping on this one's metadata.
        raise router_empty_error(
            sc,
            f"survey {survey_id} states {datums or 'no'} vertical datum and "
            f"{units or 'no'} unit over its own points: a bed needs exactly one of "
            "each, and nothing here may choose between them.",
            "NO_STATED_DATUM",
        )
    scale = _METRES_PER_UNIT.get(units[0].lower().replace(" ", "").replace("_", ""))
    if scale is None:
        raise router_empty_error(
            sc,
            f"survey {survey_id} measures its depths in {units[0]!r}, which is not a "
            f"unit this fetch can convert (known: {sorted(_METRES_PER_UNIT)}).",
            "UNCONVERTIBLE_UNIT",
        )
    return _counted_from(datums[0], spec.normalize.quantity), units[0], scale


def _journal(surveys: list[dict[str, Any]]) -> None:
    """WHICH surveys the bed was built from and WHEN each was measured.

    A reach is often covered only by a pass several years old, so the age of the
    measurement under each part of the domain is part of the answer."""
    from trid3nt_server.workflows.runtime import journal_note

    named = ", ".join(
        f"{(f.get('properties') or {}).get('surveyjobidpk')} "
        f"({d.isoformat() if (d := _survey_date(f)) else 'undated'})"
        for f in surveys)
    journal_note(f"the bed is measured by {len(surveys)} USACE eHydro "
                 f"survey(s), newest first over the ground each covers: {named}.")


@register_hook("ehydro.read")
def read(spec: SourceSpec, params: dict[str, Any], *, timeout_s: float) -> list[dict]:
    """The selected surveys' footprints and soundings, as the rows of one artifact.
    The declared ``timeout_s`` arrives by the delegate contract and is the shared
    transport's own, which owns every socket here and enforces it."""
    since = _since(spec, params)
    surveys = _selected(spec, _index(spec, params), since, params["bbox"])
    _journal(surveys)
    rows: list[dict] = []
    for feature in surveys:
        properties = feature.get("properties") or {}
        survey_id = str(properties.get("surveyjobidpk") or "")
        archive = _package(spec, str(properties.get("sourcedatalocation")), survey_id)
        points = _soundings(spec, archive, survey_id)
        datum, uom, scale = _stated(spec, points, survey_id)
        offset_m, offset_frame = _published_offset(archive, survey_id)
        date = _survey_date(feature)
        common = {
            "survey_id": survey_id,
            "survey_date": date.isoformat() if date else None,
            "district": properties.get("usacedistrictcode"),
            "feature_name": properties.get("sdsfeaturename"),
            "survey_type": properties.get("surveytype"),
            "vertical_datum": datum,
            "source_uom": uom,
            "datum_offset_m": offset_m,
            "datum_offset_frame": offset_frame or None,
        }
        rows.append({
            "type": "Feature", "geometry": feature.get("geometry"),
            "properties": {**common, "part": "survey", "depth_below_datum_m": None,
                           "sounding_count": int(len(points))},
        })
        for depth, geometry in zip(points[_DEPTH_FIELD], points.geometry):
            if geometry is None or geometry.is_empty or depth is None or not math.isfinite(float(depth)):
                continue
            rows.append({
                "type": "Feature",
                "geometry": {"type": "Point",
                             "coordinates": [float(geometry.x), float(geometry.y)]},
                "properties": {**common, "part": "sounding", "sounding_count": None,
                               "depth_below_datum_m": round(float(depth) * scale, 4)},
            })
        logger.info("ehydro: survey %s (%s) -> %d sounding(s), datum %s, offset %s",
                    survey_id, common["survey_date"], len(points), datum,
                    f"{offset_m:+.4f} m on {offset_frame}" if offset_m is not None
                    else "unpublished")
    return rows
