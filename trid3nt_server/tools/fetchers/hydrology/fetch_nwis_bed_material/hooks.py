"""NWIS bed-material delegate hooks: the WQP Result and Station calls.

The Result service publishes a sample's percent-finer readings as separate rows,
one per grain-size class, with the class itself as FREE TEXT
(``ResultParticleSizeBasisText``) and no coordinates at all; the Station service
publishes the site's location and its stated datums. Grouping the Result rows into
one feature per sample and joining the Station geometry onto it is what the
declarative surface cannot express, so this hook owns both round trips through the
same ``dataretrieval.wqp`` client the water-quality fetch delegates to.
"""

from __future__ import annotations

import logging
import math
import re
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..._router.errors import router_empty_error, router_input_error, router_upstream_error
from ..._router.hooks import register_hook

logger = logging.getLogger(__name__)

__all__ = ["validate", "read"]

#: The standard half-phi grain-size thresholds the USGS Sediment Laboratory
#: reports bed-material analyses at, millimetres. ``ResultParticleSizeBasisText``
#: values outside this set are published but not one of these classes and are
#: skipped for that one result rather than forced onto the nearest one.
_STANDARD_MM: tuple[float, ...] = (
    0.004, 0.008, 0.016, 0.031, 0.0625, 0.125, 0.25, 0.5, 1.0, 2.0, 2.8, 4.0,
    4.75, 5.6, 8.0, 9.5, 11.2, 16.0, 19.0, 22.4, 32.0,
)
def _fmt_mm(mm: float) -> str:
    """``1.0`` -> ``"1"``, ``0.0625`` -> ``"0p0625"``: a whole threshold drops its
    decimal so the column reads ``pct_finer_1mm`` rather than ``_1p0mm``."""
    text = str(int(mm)) if mm == int(mm) else str(mm)
    return text.replace(".", "p")


#: Column names are stated once here and mirrored in ``source.yaml``'s
#: ``ingest.properties`` / ``coverage.units`` - the test pins the two lists together.
_MM_TO_COLUMN = {mm: f"pct_finer_{_fmt_mm(mm)}mm" for mm in _STANDARD_MM}

#: ``"< 2 mm"`` / ``"<1 mm"`` / ``"<0.0625mm"`` / ``"< 0.0625 mm"`` - the spacing
#: is the only thing that varies; a basis stated any other way (a range, a phi
#: value) does not match and its result is skipped.
_LESS_THAN_MM = re.compile(r"^<\s*([0-9]+(?:\.[0-9]+)?)\s*mm$", re.IGNORECASE)

_RESULT_SUBDIVISION = "Bottom material"
_RESULT_UNIT = "%"


def _bbox_str(bbox: list[float]) -> str:
    return ",".join(str(v) for v in bbox)


def _str_or_none(v: Any) -> str | None:
    """Trimmed str, or None for pandas NaN / None / empty."""
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    s = str(v).strip()
    return s or None


def _basis_column(basis: str) -> str | None:
    """The ``pct_finer_<x>mm`` column this basis text names, or None if it is
    not the standard less-than phrasing at a recognized threshold."""
    match = _LESS_THAN_MM.match(basis.strip())
    if match is None:
        return None
    mm = round(float(match.group(1)), 4)
    return _MM_TO_COLUMN.get(mm)


@register_hook("nwis_bed_material.validate")
def validate(spec: SourceSpec, params: dict[str, Any]) -> None:
    """Refuse a missing bbox or empty characteristic before the cache read and
    before the network."""
    prefix = spec.error_code_prefix
    if params.get("bbox") is None:
        raise router_input_error(
            prefix,
            "fetch_nwis_bed_material requires bbox=(west, south, east, north) "
            "in EPSG:4326.",
            spec.input_error_suffix,
        )
    characteristic = params.get("characteristic")
    if not characteristic or not str(characteristic).strip():
        raise router_input_error(
            prefix,
            "characteristic is required (e.g. 'Bed sediment particle size', "
            "'Bedload sediment particle size')",
            spec.input_error_suffix,
        )


def _stations(spec: SourceSpec, params: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Site id -> location + the datums the Station record states for it."""
    import dataretrieval.wqp as wqp
    from dataretrieval.exceptions import DataRetrievalError

    prefix = spec.error_code_prefix
    try:
        df, _ = wqp.what_sites(
            bBox=_bbox_str(params["bbox"]),
            characteristicName=params["characteristic"],
            providers="NWIS",
        )
    except DataRetrievalError as exc:
        raise router_upstream_error(prefix, f"WQP Station search failed: {exc}")
    out: dict[str, dict[str, Any]] = {}
    if df is None or not len(df):
        return out
    for rd in df.to_dict(orient="records"):
        site_id = _str_or_none(rd.get("MonitoringLocationIdentifier"))
        if not site_id:
            continue
        try:
            lon = float(rd.get("LongitudeMeasure"))
            lat = float(rd.get("LatitudeMeasure"))
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(lon) and math.isfinite(lat)):
            continue
        out[site_id] = {
            "lon": lon, "lat": lat,
            "site_name": _str_or_none(rd.get("MonitoringLocationName")) or "",
            "site_type": _str_or_none(rd.get("MonitoringLocationTypeName")) or "",
            "huc8": _str_or_none(rd.get("HUCEightDigitCode")) or "",
            "horizontal_datum": _str_or_none(
                rd.get("HorizontalCoordinateReferenceSystemDatumName")) or "",
            "vertical_datum": _str_or_none(
                rd.get("VerticalCoordinateReferenceSystemDatumName")) or "",
            "vertical_elevation": _str_or_none(rd.get("VerticalMeasure/MeasureValue")),
            "vertical_elevation_unit": _str_or_none(
                rd.get("VerticalMeasure/MeasureUnitCode")) or "",
        }
    return out


def _samples(spec: SourceSpec, params: dict[str, Any]) -> tuple[dict[tuple[str, str], dict], int, int, int]:
    """Bed-material sample groups, keyed by (site_id, activity_id); plus the
    dropped-row counts (non-bottom-material medium, non-percent unit,
    unrecognized size-class text) - never folded silently into the count."""
    import dataretrieval.wqp as wqp
    from dataretrieval.exceptions import DataRetrievalError

    prefix = spec.error_code_prefix
    window: dict[str, str] = {}
    if params.get("start_date"):
        window["startDateLo"] = str(params["start_date"])
    if params.get("end_date"):
        window["startDateHi"] = str(params["end_date"])
    try:
        df, _ = wqp.get_results(
            bBox=_bbox_str(params["bbox"]),
            characteristicName=params["characteristic"],
            providers="NWIS",
            dataProfile="resultPhysChem",
            **window,
        )
    except DataRetrievalError as exc:
        raise router_upstream_error(prefix, f"WQP Result search failed: {exc}")

    groups: dict[tuple[str, str], dict[str, Any]] = {}
    dropped_medium = dropped_unit = dropped_basis = 0
    if df is None or not len(df):
        return groups, dropped_medium, dropped_unit, dropped_basis

    for rd in df.to_dict(orient="records"):
        site_id = _str_or_none(rd.get("MonitoringLocationIdentifier"))
        activity_id = _str_or_none(rd.get("ActivityIdentifier"))
        if not site_id or not activity_id:
            continue
        if _str_or_none(rd.get("ActivityMediaSubdivisionName")) != _RESULT_SUBDIVISION:
            dropped_medium += 1
            continue
        if _str_or_none(rd.get("ResultMeasure/MeasureUnitCode")) != _RESULT_UNIT:
            dropped_unit += 1
            continue
        raw_val = _str_or_none(rd.get("ResultMeasureValue"))
        try:
            value = float(raw_val) if raw_val is not None else None
        except (TypeError, ValueError):
            value = None
        basis = _str_or_none(rd.get("ResultParticleSizeBasisText"))
        column = _basis_column(basis) if basis else None
        key = (site_id, activity_id)
        group = groups.get(key)
        if group is None:
            group = {
                "site_id": site_id, "activity_id": activity_id,
                "sample_date": _str_or_none(rd.get("ActivityStartDate")) or "",
                "characteristic": _str_or_none(rd.get("CharacteristicName")) or "",
                "collection_equipment": _str_or_none(
                    rd.get("SampleCollectionEquipmentName")) or "",
                "sizes": {},
            }
            groups[key] = group
        if column is None or value is None or not math.isfinite(value):
            if basis is not None and column is None:
                dropped_basis += 1
            continue
        group["sizes"][column] = value
    return groups, dropped_medium, dropped_unit, dropped_basis


@register_hook("nwis_bed_material.read")
def read(spec: SourceSpec, params: dict[str, Any], *, timeout_s: float) -> list[dict]:
    """The bed-material samples in scope, as the rows of one artifact: the
    declared ``timeout_s`` arrives by the delegate contract, and ``dataretrieval``
    owns the socket it bounds."""
    prefix = spec.error_code_prefix
    stations = _stations(spec, params)
    groups, dropped_medium, dropped_unit, dropped_basis = _samples(spec, params)

    rows: list[dict] = []
    dropped_no_station = 0
    for (site_id, _activity_id), group in groups.items():
        station = stations.get(site_id)
        if station is None:
            dropped_no_station += 1
            continue
        properties: dict[str, Any] = {
            "site_id": site_id,
            "site_name": station["site_name"],
            "site_type": station["site_type"],
            "huc8": station["huc8"],
            "activity_id": group["activity_id"],
            "sample_date": group["sample_date"],
            "characteristic": group["characteristic"],
            "collection_equipment": group["collection_equipment"],
            "horizontal_datum": station["horizontal_datum"],
            "vertical_datum": station["vertical_datum"],
            "vertical_elevation": station["vertical_elevation"],
            "vertical_elevation_unit": station["vertical_elevation_unit"],
            "n_size_classes": len(group["sizes"]),
        }
        for column in _MM_TO_COLUMN.values():
            properties[column] = group["sizes"].get(column)
        rows.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [station["lon"], station["lat"]]},
            "properties": properties,
        })

    logger.info(
        "nwis_bed_material: %d sample(s); dropped %d non-Bottom-material, "
        "%d non-percent-unit, %d unrecognized-size-class result(s), "
        "%d sample(s) with no matching Station record",
        len(rows), dropped_medium, dropped_unit, dropped_basis, dropped_no_station,
    )
    if not rows:
        raise router_empty_error(
            prefix,
            "no USGS bed-material particle-size samples (Bottom material, percent "
            "by weight, with a matching Station location) in this bbox. NWIS "
            "bed-material sampling is sparse; most reaches carry none.",
            spec.empty_error_suffix,
        )
    return rows
