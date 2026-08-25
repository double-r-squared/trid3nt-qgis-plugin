"""The coastal water-level FORCING resolver: a CO-OPS gauge series, re-based to t=0.

Engine-agnostic by placement. A tide/surge boundary series is what the world
does at the coast, not what TELEMAC does with it - SCHISM, ADCIRC and a simple
stage-frequency analysis all want the same payload - so by the placement rule it
sits in the shared domain tier beside the other forcing resolvers.

The FETCH itself is the router's (``fetch_noaa_coops_tides``): its cache, its
ladders, its provenance and its typed refusals live there once and are
authoritative. This module does the one thing the router does not - pick the
STATION the question is about out of the returned collection and turn its inline
``time_series_csv`` into the ``[[seconds_from_start, level_m], ...]`` a boundary
file is written from.
"""

from __future__ import annotations

import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trid3nt_server.workflows.lib import DeclarativeError

logger = logging.getLogger("trid3nt_server.workflows.shared.tide_series")

__all__ = ["TideSeriesError", "iso_to_epoch_s", "resolve_tide_series"]

#: How far outside the modeled extent a station may sit and still be the one the
#: question is about. CO-OPS gauges stand at the shoreline, sometimes just beyond
#: a tight coastal strip, so a fetch bbox equal to the domain finds nothing.
_STATION_SEARCH_PAD_DEG = 0.25

#: Which CO-OPS product answers which question class. ``observed`` is the real
#: record (tide + surge); ``prediction`` is the astronomical tide alone - the
#: control that isolates the surge.
_PRODUCT_FOR_SERIES: dict[str, str] = {
    "observed": "water_level",
    "prediction": "predictions",
}


class TideSeriesError(DeclarativeError):
    """The water-level series could not be resolved over this domain."""

    error_code = "TIDE_SERIES_UNAVAILABLE"


def iso_to_epoch_s(iso: str) -> float | None:
    """A CO-OPS stamp (``YYYY-MM-DDTHH:MMZ`` / ``YYYY-MM-DD HH:MM``) -> epoch seconds."""
    text = str(iso).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00").replace(" ", "T", 1))
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text.replace("Z", ""), fmt).replace(
                tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None


async def resolve_tide_series(*, series_type: str = "observed",
                              station: str | None = None,
                              start_date: str | None = None,
                              end_date: str | None = None,
                              fallback: tuple[str, ...] = (),
                              temporal: Any = None) -> dict[str, Any]:
    """The water-level series over the CURRENT DOMAIN, re-based so t=0 is its start.

    Returns the series plus the station's own identity, because a boundary forced
    by an unnamed gauge is a number nobody can check. Refuses typed rather than
    substituting: an empty fetch, a named station that is not in the collection,
    or a record with fewer than two finite points are all things a caller has to
    hear about, not silently model around.
    """
    from trid3nt_server.workflows.lib import current_domain

    domain = current_domain()
    if domain is None or domain.bbox is None:
        raise TideSeriesError(
            "the water-level series cannot be fetched: no domain is bound.")
    if not (start_date and end_date):
        raise TideSeriesError(
            "the water-level series needs a start_date and an end_date; a window "
            "nobody asked for is not invented.")
    product = _PRODUCT_FOR_SERIES.get(str(series_type).strip().lower(), "water_level")
    pad = _STATION_SEARCH_PAD_DEG
    search = [round(domain.bbox[0] - pad, 4), round(domain.bbox[1] - pad, 4),
              round(domain.bbox[2] + pad, 4), round(domain.bbox[3] + pad, 4)]

    from trid3nt_server.tools import TOOL_REGISTRY

    entry = TOOL_REGISTRY.get("fetch_noaa_coops_tides")
    if entry is None:
        raise TideSeriesError("fetch_noaa_coops_tides is not registered.")

    import asyncio

    layer = await asyncio.to_thread(
        lambda: entry.fn(bbox=search, start_date=start_date, end_date=end_date,
                         product=product, purpose="coastal tide/surge boundary"))
    uri = getattr(layer, "uri", None)
    if not uri:
        raise TideSeriesError(
            f"fetch_noaa_coops_tides returned no layer for bbox={search} window "
            f"{start_date}..{end_date} product={product}.")
    centre = (0.5 * (domain.bbox[0] + domain.bbox[2]),
              0.5 * (domain.bbox[1] + domain.bbox[3]))
    series, meta = await asyncio.to_thread(_read_station_series, str(uri), station,
                                           centre)
    return {"series": series, "series_type": str(series_type), "product": product,
            "window": f"{start_date}..{end_date}", "uri": str(uri), **meta}


def _read_station_series(fgb_uri: str, station_id: str | None,
                         centre: tuple[float, float]) -> tuple[list[list[float]],
                                                               dict[str, Any]]:
    """Pick the station (named, else nearest to centre) and parse its series."""
    import geopandas as gpd

    from trid3nt_server.tools.cache import read_object_bytes_s3

    tmp = tempfile.mkdtemp(prefix="coops-")
    local = os.path.join(tmp, "coops.fgb")
    try:
        data = read_object_bytes_s3(fgb_uri) if str(fgb_uri).startswith("s3://") else None
        if data is None:
            with open(str(fgb_uri), "rb") as fh:      # a local path (offline)
                data = fh.read()
        with open(local, "wb") as fh:
            fh.write(data)
        gdf = gpd.read_file(local)
    except Exception as exc:  # noqa: BLE001
        raise TideSeriesError(
            f"could not read the CO-OPS tide collection {fgb_uri!r}: {exc}") from exc
    finally:
        Path(local).unlink(missing_ok=True)

    if len(gdf) == 0 or "time_series_csv" not in gdf.columns:
        raise TideSeriesError(
            f"the CO-OPS fetch returned no station with a time series over this "
            f"domain ({fgb_uri!r}); widen the AOI or name a documented station.")

    row = _pick_station(gdf, station_id, centre)
    pairs: list[tuple[float, float]] = []
    for line in str(row.get("time_series_csv") or "").splitlines():
        stamp, sep, value = line.strip().partition(",")
        if not sep:
            continue
        seconds = iso_to_epoch_s(stamp)
        try:
            level = float(value.strip())
        except (TypeError, ValueError):
            continue
        if seconds is not None and level == level:      # finite
            pairs.append((seconds, level))
    if len(pairs) < 2:
        raise TideSeriesError(
            f"station {station_id or 'nearest'} carried fewer than 2 finite "
            "water-level points; name a station and window with a real record.")
    pairs.sort(key=lambda pair: pair[0])
    origin = pairs[0][0]
    series = [[round(t - origin, 1), round(v, 4)] for t, v in pairs]
    return series, {
        "station_id": str(_column(gdf, row, "station_id", "id") or station_id or ""),
        "station_name": str(_column(gdf, row, "station_name", "name") or ""),
        "series_datum": str(_column(gdf, row, "datum") or "MLLW"),
        "n_points": len(series),
        "span_s": series[-1][0],
    }


def _column(gdf: Any, row: Any, *names: str) -> Any:
    for name in names:
        if name in gdf.columns and row.get(name) is not None:
            return row.get(name)
    return None


def _pick_station(gdf: Any, station_id: str | None,
                  centre: tuple[float, float]) -> Any:
    """The NAMED station, or the one nearest the domain centre."""
    if station_id:
        wanted = str(station_id).strip()
        for _, row in gdf.iterrows():
            if str(_column(gdf, row, "station_id", "id") or "").strip() == wanted:
                return row
        available = sorted({str(_column(gdf, row, "station_id", "id"))
                            for _, row in gdf.iterrows()})
        raise TideSeriesError(
            f"CO-OPS station {station_id!r} is not among the stations over this "
            f"domain {available}; drop `station` to use the nearest, or widen the AOI.")
    best, best_distance = None, None
    for _, row in gdf.iterrows():
        try:
            geom = row.geometry
            distance = (geom.x - centre[0]) ** 2 + (geom.y - centre[1]) ** 2
        except Exception:  # noqa: BLE001 - fall back to the flat lon/lat columns
            lon, lat = _column(gdf, row, "lon"), _column(gdf, row, "lat")
            if lon is None or lat is None:
                continue
            distance = (float(lon) - centre[0]) ** 2 + (float(lat) - centre[1]) ** 2
        if best_distance is None or distance < best_distance:
            best, best_distance = row, distance
    if best is None:
        raise TideSeriesError(
            "no CO-OPS station in the collection carried a usable position.")
    return best
