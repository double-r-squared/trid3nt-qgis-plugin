"""eHydro delegate hooks: the survey index, then the survey's own package.

The index is one ArcGIS layer over every published survey; the soundings live in
each survey's ZIP, in the geodatabase beside its XYZ. The delegate owns both round
trips and the geodatabase read, and states the datum the survey states.
"""

# WHAT THE SURVEY PUBLISHES. Beside the thinned XYZ, an eHydro package carries the
# surface as an Esri TIN and a geodatabase of derived features - there is no raster
# in it. The measured thing is the SurveyPoint feature class, and a raster bed is
# ``derive_survey_surface`` over these points, not something to invent here.
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

#: How many of the newest surveys the index query asks for, and how many a ``since``
#: window may return. The cap is a refusal rather than a truncation: each survey is
#: its own multi-megabyte package, and a silently dropped one is a bed with a hole.
#: It sits above what the default window returns over a long reach - a district
#: surveys a busy channel in overlapping passes, and twelve kilometres of one
#: holds seven in a year - so the refusal is for a window somebody widened by
#: hand, never for the one this fetch asks on its own.
_INDEX_RECORDS = 25
_MAX_SURVEYS = 12

#: How far back an unstated window reaches. ONE year: a dredged channel is
#: resurveyed on that order, so a year holds the passes that together cover the
#: reach without asking for more packages than the cap allows.
_WINDOW_DAYS = 365

#: The feature class carrying the measured soundings, and the three fields a bed
#: cannot be built without.
_SOUNDING_LAYER = "SurveyPoint"
_DEPTH_FIELD = "Z_depth"
_DATUM_FIELD = "elevationDatum"
_UOM_FIELD = "elevationUOM"

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


def _since(spec: SourceSpec, params: dict[str, Any]) -> _dt.date:
    """The ``since`` bound as a date - the stated one, else the default window."""
    raw = params.get("since")
    if raw is None or not str(raw).strip():
        return _dt.date.today() - _dt.timedelta(days=_WINDOW_DAYS)
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
    """The newest surveys intersecting the bbox, as GeoJSON features."""
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
        "resultRecordCount": str(_INDEX_RECORDS),
        "f": "geojson",
    }
    try:
        body, _ct, _url = get_bytes(get_client(), str(spec.endpoints["index"].url), params=query)
        parsed = json.loads(body.decode("utf-8"))
    except TransportError as exc:
        raise router_upstream_error(sc, f"the eHydro survey index failed: {exc}")
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise router_upstream_error(sc, f"the eHydro survey index returned non-JSON: {exc}")
    return [f for f in (parsed.get("features") or []) if isinstance(f, dict)]


def _survey_date(feature: dict[str, Any]) -> _dt.date | None:
    """The survey's end date; the index publishes it as epoch milliseconds."""
    raw = (feature.get("properties") or {}).get("surveydateend")
    try:
        return _dt.datetime.fromtimestamp(float(raw) / 1000.0, tz=_dt.timezone.utc).date()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _selected(spec: SourceSpec, features: list[dict[str, Any]],
              since: _dt.date) -> list[dict[str, Any]]:
    """The surveys whose window this request asks for, newest first."""
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
    window = [f for f, d in usable if d >= since]
    if not window:
        raise router_empty_error(
            sc,
            f"no eHydro survey over this extent ends on or after {since.isoformat()}; "
            f"the newest one here is {usable[0][1].isoformat()}.",
            spec.empty_error_suffix,
        )
    if len(window) > _MAX_SURVEYS:
        # The index answered with its newest page, so a window that fills the page
        # holds at least that many and the count is stated as the floor it is.
        floor = "at least " if len(features) >= _INDEX_RECORDS else ""
        counted = f"{floor}{len(window)}"
        raise router_input_error(
            sc,
            f"{counted} surveys end on or after {since.isoformat()} over this "
            f"extent, more than the {_MAX_SURVEYS} this fetch will download. Move "
            "since forward, or narrow the bbox.",
            spec.input_error_suffix,
        )
    return window


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


def _stated(spec: SourceSpec, points: Any, survey_id: str) -> tuple[str, str, float]:
    """The datum, the unit and the metres per unit the survey itself states."""
    sc = spec.error_code_prefix
    datums = sorted({str(v).strip() for v in points[_DATUM_FIELD].dropna().unique()
                     if str(v).strip()})
    units = sorted({str(v).strip() for v in points[_UOM_FIELD].dropna().unique()
                    if str(v).strip()})
    if len(datums) != 1 or len(units) != 1:
        raise router_input_error(
            sc,
            f"survey {survey_id} states {datums or 'no'} vertical datum and "
            f"{units or 'no'} unit over its own points: a bed needs exactly one of "
            "each, and nothing here may choose between them.",
            spec.input_error_suffix,
        )
    scale = _METRES_PER_UNIT.get(units[0].lower().replace(" ", "").replace("_", ""))
    if scale is None:
        raise router_input_error(
            sc,
            f"survey {survey_id} measures its depths in {units[0]!r}, which is not a "
            f"unit this fetch can convert (known: {sorted(_METRES_PER_UNIT)}).",
            spec.input_error_suffix,
        )
    return datums[0], units[0], scale


@register_hook("ehydro.read")
def read(spec: SourceSpec, params: dict[str, Any], *, timeout_s: float) -> list[dict]:
    """The selected surveys' footprints and soundings, as the rows of one artifact.
    The declared ``timeout_s`` arrives by the delegate contract and is the shared
    transport's own, which owns every socket here and enforces it."""
    since = _since(spec, params)
    surveys = _selected(spec, _index(spec, params), since)
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
