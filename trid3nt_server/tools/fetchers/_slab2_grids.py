"""The USGS Slab2 seam: the subduction-interface depth, strike and dip grids of every zone, read at a place.

The release is one tarball, downloaded WHOLE once through the cache and never vendored; each zone holds
``<zone>_slab2_{dep,str,dip}_<date>.grd`` (HDF5 netCDF, lon 0..360, depth negative km, masked outside the zone).
A read at a point is the grid's own value on a node and a bilinear blend between nodes; a stencil node the grid
masks refuses, so nothing is extrapolated and no point is moved.
"""

from __future__ import annotations

import io
import math
import re
import tarfile
from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np

from ._fetch_common import FetchError

__all__ = ["SlabAmbiguous", "SlabError", "SlabNoCoverage", "Zone", "archive_bytes", "blocks_from_record",
           "nearest_node", "read_archive", "record_zones", "sample", "window"]

_MEMBER = re.compile(r"(?:^|/)(?P<zone>[a-z]{3})_slab2_(?P<kind>dep|dip|str)_[0-9.]+\.grd$")

#: A node coordinate within this many grid cells of a stated one is that node.
_NODE_TOL = 1e-7


class SlabError(FetchError):
    """Base of the seam's refusals."""

    error_code = "SLAB_ERROR"
    retryable = False


class SlabNoCoverage(SlabError):
    """Slab2 states no interface at a place."""

    error_code = "SLAB_NO_COVERAGE"


class SlabAmbiguous(SlabError):
    """More than one zone states an interface at a place."""

    error_code = "SLAB_AMBIGUOUS"


@dataclass(frozen=True)
class Zone:
    """One zone's grids as arrays: ascending ``lons`` and ``lats``, depth positive down in km, NaN where masked."""

    code: str
    spacing: float
    lons: np.ndarray
    lats: np.ndarray
    depth: np.ndarray
    strike: np.ndarray
    dip: np.ndarray


def archive_bytes(spec: Any) -> bytes:
    """The release tarball through a constant-key read-through, so every call shares one download."""
    from ..cache import read_through
    from ._router.errors import router_upstream_error
    from ._router.router import synthesize_metadata
    from ._router.transport import TransportError, get_bytes, get_client

    def _fetch() -> bytes:
        try:
            body, _type, _final = get_bytes(get_client(), str(spec.endpoints["archive"].url),
                                            headers={"User-Agent": spec.auth.user_agent})
        except TransportError as exc:
            raise router_upstream_error(spec.error_code_prefix, f"{type(exc).__name__}: {exc}", exc.retryable) from exc
        return body

    result = read_through(metadata=synthesize_metadata(spec), params={"file": "slab2_archive"}, ext="tar.gz",
                          fetch_fn=_fetch)
    assert result.data is not None, "the archive cache entry holds bytes"
    return result.data


def read_archive(blob: bytes, only: str | None = None) -> dict[str, Zone]:
    """Every zone's (or ``only``'s) grids out of the tarball, keyed by zone code."""
    import netCDF4

    found: dict[str, dict[str, bytes]] = {}
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        for member in tar:
            match = _MEMBER.search(member.name) if member.isfile() else None
            if match is None or (only is not None and match["zone"] != only) or "/._" in member.name:
                continue
            found.setdefault(match["zone"], {})[match["kind"]] = tar.extractfile(member).read()
    zones: dict[str, Zone] = {}
    for code, files in sorted(found.items()):
        if set(files) != {"dep", "dip", "str"}:
            raise SlabError(f"the Slab2 release holds zone {code!r} without all of its depth, strike and dip grids.")
        grids = {}
        for kind, data in files.items():
            with netCDF4.Dataset("slab2", memory=data) as ds:
                grids[kind] = (np.asarray(ds["x"][:], dtype=float), np.asarray(ds["y"][:], dtype=float),
                               np.ma.filled(ds["z"][:].astype(float), np.nan))
        lons, lats, depth = grids["dep"]
        for kind in ("dip", "str"):
            if grids[kind][2].shape != depth.shape or not np.allclose(grids[kind][0], lons):
                raise SlabError(f"zone {code!r}: the {kind} grid is not on the depth grid.")
        zones[code] = Zone(code, round(float(lons[1] - lons[0]), 6), lons, lats, -depth, grids["str"][2], grids["dip"][2])
    return zones


def window(zones: Mapping[str, Zone], bbox: tuple[float, float, float, float]) -> list[Zone]:
    """The part of every zone that holds a depth inside ``bbox`` plus one node of margin, in the box's own longitude frame."""
    west, south, east, north = bbox
    out: list[Zone] = []
    for zone in zones.values():
        for shift in (0.0, -360.0, 360.0):
            lons = zone.lons + shift
            if east < lons[0] - zone.spacing or west > lons[-1] + zone.spacing or north < zone.lats[0] - zone.spacing \
                    or south > zone.lats[-1] + zone.spacing:
                continue
            i0 = max(int(math.floor((west - lons[0]) / zone.spacing + _NODE_TOL)) - 1, 0)
            i1 = min(int(math.ceil((east - lons[0]) / zone.spacing - _NODE_TOL)) + 1, len(lons) - 1)
            j0 = max(int(math.floor((south - zone.lats[0]) / zone.spacing + _NODE_TOL)) - 1, 0)
            j1 = min(int(math.ceil((north - zone.lats[0]) / zone.spacing - _NODE_TOL)) + 1, len(zone.lats) - 1)
            if i0 > i1 or j0 > j1:
                continue
            cut = (slice(j0, j1 + 1), slice(i0, i1 + 1))
            if np.isfinite(zone.depth[cut]).any():
                out.append(Zone(zone.code, zone.spacing, lons[i0:i1 + 1], zone.lats[j0:j1 + 1], zone.depth[cut],
                                zone.strike[cut], zone.dip[cut]))
    return out


def record_zones(zones: list[Zone]) -> list[dict[str, Any]]:
    """Zones as the JSON the record carries: coordinates rounded to 6 dp, a masked node null."""
    def rows(a: np.ndarray) -> list[list[float | None]]:
        return [[None if not math.isfinite(v) else float(v) for v in row] for row in a]

    return [{"zone": z.code, "spacing_deg": z.spacing, "lons": [round(float(v), 6) for v in z.lons],
             "lats": [round(float(v), 6) for v in z.lats], "depth_km": rows(z.depth), "strike": rows(z.strike),
             "dip": rows(z.dip)} for z in zones]


def blocks_from_record(record: Mapping[str, Any]) -> list[Zone]:
    """The zones a slab record carries, back as arrays."""
    zones = record.get("zones") if isinstance(record, Mapping) else None
    if not isinstance(zones, (list, tuple)) or not zones:
        raise SlabNoCoverage("the slab record holds no zone: it was read over a place Slab2 does not cover.")

    def grid(rows: Any) -> np.ndarray:
        return np.array([[np.nan if v is None else v for v in row] for row in rows], dtype=float)

    try:
        return [Zone(z["zone"], float(z["spacing_deg"]), np.array(z["lons"], dtype=float),
                     np.array(z["lats"], dtype=float), grid(z["depth_km"]), grid(z["strike"]), grid(z["dip"]))
                for z in zones]
    except (KeyError, TypeError, ValueError) as exc:
        raise SlabError(f"the slab record is not the one fetch_slab_geometry returns: {exc!r}") from exc


def _read(zone: Zone, lon: float, lat: float) -> tuple[float, float, float] | None:
    """``(depth_km, strike, dip)`` of one zone at a place, or None where the zone does not hold every node the read needs."""
    nx, ny = len(zone.lons), len(zone.lats)
    fx, fy = (lon - zone.lons[0]) / zone.spacing, (lat - zone.lats[0]) / zone.spacing
    if fx < -_NODE_TOL or fy < -_NODE_TOL or fx > nx - 1 + _NODE_TOL or fy > ny - 1 + _NODE_TOL:
        return None
    nodes: list[tuple[int, int, float]] = []
    axes = []
    for f, n in ((fx, nx), (fy, ny)):
        i = int(math.floor(f + _NODE_TOL))
        t = f - i
        if abs(t) < _NODE_TOL:
            t = 0.0
        i = min(i, n - 1)
        axes.append([(i, 1.0)] if t == 0.0 or i == n - 1 else [(i, 1.0 - t), (i + 1, t)])
    for j, wy in axes[1]:
        for i, wx in axes[0]:
            nodes.append((j, i, wx * wy))
    got = [(zone.depth[j, i], zone.strike[j, i], zone.dip[j, i], w) for j, i, w in nodes]
    if not all(math.isfinite(d) and math.isfinite(s) and math.isfinite(p) for d, s, p, _ in got):
        return None
    if len(got) == 1:
        return float(got[0][0]), float(got[0][1]), float(got[0][2])
    sin = sum(w * math.sin(math.radians(s)) for _, s, _, w in got)
    cos = sum(w * math.cos(math.radians(s)) for _, s, _, w in got)
    if math.hypot(sin, cos) < 1e-6:
        return None
    return (float(sum(w * d for d, _, _, w in got)), float(math.degrees(math.atan2(sin, cos)) % 360.0),
            float(sum(w * p for _, _, p, w in got)))


def sample(zones: list[Zone], lon: float, lat: float) -> dict[str, Any]:
    """The interface at a place -> ``{zone, depth_km, strike, dip}``; no zone holding it refuses with the nearest node, two refuse naming both."""
    held = [(z.code, hit) for z in zones
            if (hit := next((h for turn in (0.0, 360.0, -360.0) if (h := _read(z, lon + turn, lat)) is not None), None))
            is not None]
    if not held:
        raise SlabNoCoverage(f"Slab2 states no interface at ({lon:.6f}, {lat:.6f}); {_hint(zones, lon, lat)}")
    if len(held) > 1:
        raise SlabAmbiguous(
            f"({lon:.6f}, {lat:.6f}) lies in {len(held)} Slab2 zones at different depths "
            f"({', '.join(f'{c} {h[0]:.1f} km' for c, h in held)}): state the zone.")
    code, (depth, strike, dip) = held[0]
    return {"zone": code, "depth_km": depth, "strike": strike, "dip": dip}


def nearest_node(zones: list[Zone], lon: float, lat: float) -> dict[str, Any] | None:
    """The grid node nearest a place that states a depth, strike and dip, as ``{zone, longitude, latitude, distance_km}``."""
    best: dict[str, Any] | None = None
    for zone in zones:
        ok = np.isfinite(zone.depth) & np.isfinite(zone.strike) & np.isfinite(zone.dip)
        if not ok.any():
            continue
        jj, ii = np.nonzero(ok)
        nlon, nlat = zone.lons[ii], zone.lats[jj]
        nlon = nlon - 360.0 * np.round((nlon - lon) / 360.0)
        a = (np.sin(np.radians(nlat - lat) / 2) ** 2
             + np.cos(np.radians(lat)) * np.cos(np.radians(nlat)) * np.sin(np.radians(nlon - lon) / 2) ** 2)
        km = 2 * 6371.0088 * np.arcsin(np.sqrt(a))
        k = int(np.argmin(km))
        if best is None or km[k] < best["distance_km"]:
            best = {"zone": zone.code, "longitude": float(nlon[k]), "latitude": float(nlat[k]),
                    "distance_km": round(float(km[k]), 1)}
    return best


def _hint(zones: list[Zone], lon: float, lat: float) -> str:
    node = nearest_node(zones, lon, lat)
    if node is None:
        return "the slab read holds no node anywhere: read it over a box that holds the interface."
    return (f"nearest node that states one: ({node['longitude']:.2f}, {node['latitude']:.2f}) in zone "
            f"{node['zone']}, {node['distance_km']} km away.")
