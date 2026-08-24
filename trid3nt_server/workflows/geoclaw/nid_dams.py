"""Resolve a real USACE National Inventory of Dams (NID) dam for a GeoClaw
dam-break run - the fetcher-wiring that replaces the invented AOI-centroid
location + baked 10 m height with a site dam.

``fetch_usace_dams`` (registered, spec-driven) returns a point FlatGeobuf of NID
dams within a bbox, each feature carrying ``NAME`` / ``LATITUDE`` / ``LONGITUDE``
/ ``DAM_HEIGHT`` (FEET, the NID standard) / ``NID_STORAGE`` (acre-feet). This
module downloads that layer (seam-1: ``TOOL_REGISTRY['fetch_usace_dams'].fn``),
picks THE dam (by name when supplied, else nearest the AOI centroid), and returns
its location + height (converted to metres) + storage as a typed ``ResolvedDam``.

Missing-from-NID (no dam covers the AOI, or a named dam is not found) is signalled
by ``None`` so the caller raises a typed INPUT-shaped gate naming the manual
params - the location/height are NEVER silently invented.

Lazy imports (geopandas / boto3) keep the offline import graph clean: nothing
here loads until a live dam-break resolution runs.
"""

from __future__ import annotations

import logging
import math
import os
import tempfile
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("trid3nt_server.workflows.geoclaw.nid_dams")

__all__ = ["ResolvedDam", "resolve_nid_dam"]

#: NID DAM_HEIGHT is published in FEET; GeoClaw's released-column height is metres.
_FT_TO_M: float = 0.3048


@dataclass(frozen=True)
class ResolvedDam:
    """A single NID dam resolved for a dam-break run (metres for GeoClaw)."""

    name: str
    lon: float
    lat: float
    height_m: float
    storage_acreft: float | None
    river: str | None

    def note(self) -> str:
        """Human-readable provenance for the result envelope / narration."""
        parts = [
            f"Dam location + height sourced from the USACE National Inventory of "
            f"Dams (NID): '{self.name}'",
            f"height {self.height_m:.1f} m ({self.height_m / _FT_TO_M:.0f} ft NID DAM_HEIGHT)",
        ]
        if self.storage_acreft is not None:
            parts.append(f"NID max storage {self.storage_acreft:,.0f} acre-ft")
        if self.river:
            parts.append(f"on {self.river}")
        parts.append(f"at ({self.lon:.5f}, {self.lat:.5f})")
        return "; ".join(parts) + "."


def _download_fgb_to_local(uri: str) -> str:
    """Download an ``s3://`` FlatGeobuf to a temp file via the solver's boto3
    client (honours ``AWS_ENDPOINT_URL`` for MinIO); return the local path."""
    from trid3nt_server.workflows.solver.solver import (
        _get_s3_client,
        _split_object_uri,
    )

    _scheme, bucket, key = _split_object_uri(uri)
    suffix = os.path.splitext(key)[1] or ".fgb"
    fd, local = tempfile.mkstemp(prefix="nid-dams-", suffix=suffix)
    os.close(fd)
    s3 = _get_s3_client()
    resp = s3.get_object(Bucket=bucket, Key=key)
    with open(local, "wb") as fh:
        fh.write(resp["Body"].read())
    return local


def _haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _feat_lonlat(props: dict[str, Any], geom: Any) -> tuple[float, float] | None:
    """Prefer the NID LATITUDE/LONGITUDE attributes; fall back to the point
    geometry. Returns ``(lon, lat)`` or ``None`` when neither is usable."""
    lat = props.get("LATITUDE")
    lon = props.get("LONGITUDE")
    try:
        if lat is not None and lon is not None:
            return float(lon), float(lat)
    except (TypeError, ValueError):
        pass
    try:
        if geom is not None and getattr(geom, "geom_type", "") == "Point":
            return float(geom.x), float(geom.y)
    except Exception:  # noqa: BLE001
        pass
    return None


def resolve_nid_dam(
    bbox: tuple[float, float, float, float],
    *,
    dam_name: str | None = None,
) -> ResolvedDam | None:
    """Resolve the real NID dam for a dam-break AOI (seam-1 fetch + selection).

    Selection: when ``dam_name`` is supplied, filter to NID dams whose ``NAME``
    contains it (case-insensitive) and keep the nearest to the AOI centroid; else
    pick the single dam nearest the AOI centroid. A candidate is usable only when
    it has BOTH a valid location and a positive ``DAM_HEIGHT``.

    Returns ``None`` (the caller raises a typed INPUT gate) when the NID query
    yields no usable dam - a named dam is not in NID, or no dam covers the AOI.
    Never raises for a data miss; a fetch/read exception also degrades to ``None``
    (the gate names the manual params, never a silent invented dam).
    """
    from trid3nt_server.tools import TOOL_REGISTRY

    cx = 0.5 * (bbox[0] + bbox[2])
    cy = 0.5 * (bbox[1] + bbox[3])

    try:
        layer = TOOL_REGISTRY["fetch_usace_dams"].fn(bbox=tuple(bbox))
    except Exception as exc:  # noqa: BLE001 - a fetch failure => typed gate upstream
        logger.info("fetch_usace_dams failed for bbox %s (%s); no NID dam resolved", bbox, exc)
        return None

    uri = getattr(layer, "uri", None) or (
        layer.get("uri") if isinstance(layer, dict) else None
    )
    if not uri:
        logger.info("fetch_usace_dams returned no uri for bbox %s; no NID dam resolved", bbox)
        return None

    local: str | None = None
    try:
        import geopandas as gpd  # lazy: never imported on the offline path

        local = _download_fgb_to_local(str(uri))
        gdf = gpd.read_file(local, engine="pyogrio")
    except Exception as exc:  # noqa: BLE001 - read failure => typed gate upstream
        logger.info("could not read NID dam layer %s (%s); no NID dam resolved", uri, exc)
        return None
    finally:
        if local and os.path.exists(local):
            try:
                os.unlink(local)
            except OSError:
                pass

    want = (dam_name or "").strip().lower()
    best: ResolvedDam | None = None
    best_km = float("inf")
    for _idx, row in gdf.iterrows():
        props = {k: row[k] for k in gdf.columns if k != "geometry"}
        name = str(props.get("NAME") or "").strip()
        if want and want not in name.lower():
            continue
        loc = _feat_lonlat(props, row.get("geometry"))
        if loc is None:
            continue
        h_ft = props.get("DAM_HEIGHT")
        try:
            height_m = float(h_ft) * _FT_TO_M
        except (TypeError, ValueError):
            continue
        if not (height_m > 0.0):
            continue
        storage = props.get("NID_STORAGE")
        try:
            storage_acreft = float(storage) if storage is not None else None
        except (TypeError, ValueError):
            storage_acreft = None
        river = str(props.get("RIVER_OR_STREAM") or "").strip() or None
        d_km = _haversine_km(cx, cy, loc[0], loc[1])
        if d_km < best_km:
            best_km = d_km
            best = ResolvedDam(
                name=name or "(unnamed NID dam)",
                lon=loc[0],
                lat=loc[1],
                height_m=round(height_m, 2),
                storage_acreft=storage_acreft,
                river=river,
            )

    if best is None:
        logger.info(
            "no usable NID dam (name=%r) with a location + positive DAM_HEIGHT "
            "found for bbox %s",
            dam_name,
            bbox,
        )
        return None
    logger.info(
        "resolved NID dam '%s' for bbox %s: (%.5f, %.5f) height=%.1f m storage=%s "
        "(%.1f km from AOI centroid)",
        best.name, bbox, best.lon, best.lat, best.height_m, best.storage_acreft, best_km,
    )
    return best
