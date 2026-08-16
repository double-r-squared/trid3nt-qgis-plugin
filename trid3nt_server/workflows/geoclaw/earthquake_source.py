"""Resolve a REAL earthquake (USGS ComCat / FDSN) into GeoClaw tsunami-source
parameters -- the Okada-dtopo front's real-event front door.

The tsunami question class is "given an earthquake, what seafloor deformation does
Okada predict and what tsunami does it drive". The worker already synthesizes a
single-subfault Okada dtopo from ``source_lonlat`` + ``source_magnitude`` (+ the
optional user-gated fault geometry). What was missing is the ability to pin those
numbers to a NAMED real event instead of hand-typed coordinates. This module reuses
``fetch_usgs_earthquakes`` (the offline-first repo driver -- never a bespoke FDSN
call) to resolve a seismic region + window + magnitude floor to the largest matching
catalog event, and returns its epicenter / depth / Mw.

HONESTY (the provenance story the composer surfaces):
  * epicenter (lon/lat), focal DEPTH, and moment magnitude Mw are REAL catalog
    values (USGS ComCat via FDSN);
  * the fault MECHANISM (strike / dip / rake) is NOT in the FDSN summary feed, so
    it is DERIVED -- a shallow subduction-interface THRUST assumption
    (``SUBDUCTION_INTERFACE_*``) unless the user supplies explicit fault geometry.
    The composer labels this a ``basis="derived"`` synthetic input so the run never
    silently fabricates a site-specific mechanism.

The seismic-region -> search-bbox geocode + the FGB read are the I/O boundary;
``select_largest_event`` is a PURE selector (unit-testable on synthetic features).
"""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(
    "trid3nt_server.workflows.geoclaw.earthquake_source")

__all__ = [
    "ResolvedEarthquake",
    "EarthquakeSourceError",
    "select_largest_event",
    "resolve_earthquake_source",
    "SUBDUCTION_INTERFACE_STRIKE_HINT",
    "SUBDUCTION_INTERFACE_DIP_DEG",
    "SUBDUCTION_INTERFACE_RAKE_DEG",
]

#: Derived shallow subduction-interface mechanism defaults (a megathrust THRUST):
#: a shallow dip and pure-thrust rake are the tsunami-relevant interface geometry
#: when the catalog carries no moment tensor. Strike is left to the worker's
#: synthetic default (the trench strike is site-specific and unknown here) -- only
#: dip/rake are pinned to the interface-thrust assumption. LOUDLY labeled derived.
SUBDUCTION_INTERFACE_STRIKE_HINT: float | None = None
SUBDUCTION_INTERFACE_DIP_DEG: float = 15.0
SUBDUCTION_INTERFACE_RAKE_DEG: float = 90.0

#: Default seismic-region search buffer (deg) around the geocoded centroid when the
#: geocoder returns a point rather than a bbox.
_REGION_BUFFER_DEG: float = 3.0


class EarthquakeSourceError(RuntimeError):
    """A typed earthquake-source resolution failure (never a silent dead-end)."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass
class ResolvedEarthquake:
    """A real catalog earthquake resolved to GeoClaw tsunami-source parameters.

    ``lon`` / ``lat`` / ``depth_km`` / ``magnitude`` are REAL USGS ComCat values;
    the fault mechanism is NOT carried here (it is derived downstream + labeled)."""

    lon: float
    lat: float
    magnitude: float
    depth_km: float | None
    event_id: str
    place: str | None
    time: str | None
    mag_type: str | None = None

    @property
    def provenance_label(self) -> str:
        pid = self.event_id or "?"
        where = self.place or f"({self.lon:.3f}, {self.lat:.3f})"
        d = f", depth {self.depth_km:.0f} km" if self.depth_km is not None else ""
        return (
            f"USGS ComCat event {pid}: M{self.magnitude:.1f} {where}{d}"
            f"{f' ({self.time})' if self.time else ''}"
        )


def _feature_props(feat: Any) -> tuple[dict[str, Any], float | None, float | None]:
    """Extract ``(properties, lon, lat)`` from a GeoJSON-ish feature or a gdf row
    dict. Tolerates both the raw FDSN feature shape and a geopandas row."""
    props = feat.get("properties") if isinstance(feat.get("properties"), dict) else feat
    lon = lat = None
    geom = feat.get("geometry")
    coords = geom.get("coordinates") if isinstance(geom, dict) else None
    if isinstance(coords, (list, tuple)) and len(coords) >= 2:
        try:
            lon, lat = float(coords[0]), float(coords[1])
        except (TypeError, ValueError):
            lon = lat = None
    if lon is None and props.get("longitude") is not None:
        try:
            lon, lat = float(props["longitude"]), float(props["latitude"])
        except (TypeError, ValueError, KeyError):
            lon = lat = None
    return props, lon, lat


def select_largest_event(features: list[Any]) -> ResolvedEarthquake | None:
    """Pick the LARGEST-magnitude event from a list of catalog features.

    Pure: takes FDSN-shaped features (or geopandas row dicts) carrying ``mag`` +
    ``depth_km`` + coordinates, returns the max-``mag`` ``ResolvedEarthquake`` (a
    tie breaks toward the SHALLOWER event -- more tsunamigenic). ``None`` when no
    feature has a finite magnitude + a valid epicenter."""
    import math

    best: ResolvedEarthquake | None = None
    best_key: tuple[float, float] = (-math.inf, -math.inf)
    for feat in features:
        if not isinstance(feat, dict):
            continue
        props, lon, lat = _feature_props(feat)
        if lon is None or lat is None:
            continue
        mag = props.get("mag")
        try:
            magf = float(mag)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(magf):
            continue
        depth = props.get("depth_km")
        try:
            depthf: float | None = float(depth)
            if not math.isfinite(depthf):
                depthf = None
        except (TypeError, ValueError):
            depthf = None
        # rank by (magnitude, shallower-is-larger): a tie prefers the shallower.
        key = (magf, -(depthf if depthf is not None else 1e3))
        if key > best_key:
            best_key = key
            best = ResolvedEarthquake(
                lon=lon, lat=lat, magnitude=magf, depth_km=depthf,
                event_id=str(props.get("id") or "").strip(),
                place=str(props.get("place") or "").strip() or None,
                time=str(props.get("time") or "").strip() or None,
                mag_type=str(props.get("mag_type") or props.get("magType") or "").strip() or None,
            )
    return best


def _region_bbox(region: str) -> tuple[float, float, float, float]:
    """Geocode a seismic-region name -> an EPSG:4326 search bbox (buffered)."""
    from trid3nt_server.data import TOOL_REGISTRY

    geo = TOOL_REGISTRY["geocode_location"].fn(query=region)
    bbox = getattr(geo, "bbox", None) or (geo.get("bbox") if isinstance(geo, dict) else None)
    if bbox and len(bbox) == 4:
        return tuple(float(v) for v in bbox)  # type: ignore[return-value]
    lon = getattr(geo, "lon", None) or (geo.get("lon") if isinstance(geo, dict) else None)
    lat = getattr(geo, "lat", None) or (geo.get("lat") if isinstance(geo, dict) else None)
    if lon is None or lat is None:
        raise EarthquakeSourceError(
            "EARTHQUAKE_REGION_UNGEOCODED",
            f"could not geocode the seismic region {region!r} to a search area.",
        )
    b = _REGION_BUFFER_DEG
    return (float(lon) - b, float(lat) - b, float(lon) + b, float(lat) + b)


def _read_fgb_features(uri: str) -> list[dict[str, Any]]:
    """Download the ``s3://`` FGB the fetcher produced + read it into feature dicts.

    Mirrors ``nid_dams._download_fgb_to_local`` (the solver's boto3 client honors
    ``AWS_ENDPOINT_URL`` for MinIO). Returns FDSN-shaped feature dicts so
    ``select_largest_event`` consumes them uniformly."""
    from trid3nt_server.data.simulation.solver.solver import (
        _get_s3_client,
        _split_object_uri,
    )
    import geopandas as gpd  # lazy: never on the offline import path

    _scheme, bucket, key = _split_object_uri(uri)
    fd, local = tempfile.mkstemp(prefix="eq-src-", suffix=os.path.splitext(key)[1] or ".fgb")
    os.close(fd)
    try:
        resp = _get_s3_client().get_object(Bucket=bucket, Key=key)
        with open(local, "wb") as fh:
            fh.write(resp["Body"].read())
        gdf = gpd.read_file(local, engine="pyogrio")
        feats: list[dict[str, Any]] = []
        for _idx, row in gdf.iterrows():
            props = {k: row[k] for k in gdf.columns if k != "geometry"}
            geom = row.get("geometry")
            lon = lat = None
            if geom is not None and getattr(geom, "geom_type", "") == "Point":
                lon, lat = float(geom.x), float(geom.y)
            feats.append({
                "properties": props,
                "geometry": {"type": "Point", "coordinates": [lon, lat]}
                if lon is not None else None,
            })
        return feats
    finally:
        if os.path.exists(local):
            try:
                os.unlink(local)
            except OSError:
                pass


def resolve_earthquake_source(
    region: str,
    *,
    min_magnitude: float = 7.0,
    start_date: str | None = None,
    end_date: str | None = None,
) -> ResolvedEarthquake:
    """Resolve a named seismic region + window -> the LARGEST real catalog event.

    Geocodes ``region`` to a search bbox, queries ``fetch_usgs_earthquakes`` over
    the window at the ``min_magnitude`` floor, reads the produced FGB, and returns
    the largest-Mw ``ResolvedEarthquake``. Raises a typed ``EarthquakeSourceError``
    on an ungeocodable region, a fetch failure, or an empty catalog (never a silent
    fabricated source)."""
    from trid3nt_server.data import TOOL_REGISTRY

    bbox = _region_bbox(region)
    try:
        layer = TOOL_REGISTRY["fetch_usgs_earthquakes"].fn(
            bbox=bbox,
            start_date=start_date,
            end_date=end_date,
            min_magnitude=float(min_magnitude),
        )
    except Exception as exc:  # noqa: BLE001 - a fetch failure => typed error
        raise EarthquakeSourceError(
            "EARTHQUAKE_CATALOG_FETCH_FAILED",
            f"USGS earthquake catalog query failed for region {region!r} "
            f"(Mw>={min_magnitude}, {start_date}..{end_date}): {exc}",
        ) from exc

    uri = getattr(layer, "uri", None) or (
        layer.get("uri") if isinstance(layer, dict) else None)
    if not uri:
        raise EarthquakeSourceError(
            "EARTHQUAKE_CATALOG_EMPTY",
            f"no earthquakes matched region {region!r} at Mw>={min_magnitude} "
            f"in {start_date}..{end_date}. Widen the window or lower the magnitude floor.",
        )
    try:
        feats = _read_fgb_features(str(uri))
    except Exception as exc:  # noqa: BLE001 - a read failure => typed error
        raise EarthquakeSourceError(
            "EARTHQUAKE_CATALOG_READ_FAILED",
            f"could not read the resolved earthquake catalog layer {uri}: {exc}",
        ) from exc

    event = select_largest_event(feats)
    if event is None:
        raise EarthquakeSourceError(
            "EARTHQUAKE_CATALOG_EMPTY",
            f"the earthquake catalog for region {region!r} carried no event with a "
            f"finite magnitude + epicenter.",
        )
    logger.info(
        "resolve_earthquake_source region=%r -> %s (bbox=%s)",
        region, event.provenance_label, bbox,
    )
    return event
