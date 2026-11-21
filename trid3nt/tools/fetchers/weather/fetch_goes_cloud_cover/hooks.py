"""goes_cloud hooks: the GOES ABI L2 Clear Sky Mask over one point, a cloud fraction per scan.

The delegate lists the anonymous NOAA bucket of the satellite that sees the point, reads the 2 km binary
cloud mask and its quality flags, and returns one point feature per requested hour."""

# The satellite is the one whose projection origin longitude is nearer the point; each bucket's first
# product hour was read off its own listing. The box is the 5 x 5 block of 2 km pixels (10 km at nadir)
# the point falls in, counted from the grid's own index zero. Only a pixel with quality flag 0 and a
# valid mask counts; the counts of every other kind stay on the record.

from __future__ import annotations

import math
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..._router.errors import router_empty_error, router_input_error, router_upstream_error
from ..._router.hooks import register_hook

__all__ = ["read", "validate"]

_PRODUCT = "ABI-L2-ACMC"
_BLOCK = 5
_STEP = timedelta(hours=1)
_WORKERS = 6

#: Satellite, bucket, the longitude its projection is centred on and the first hour its bucket holds.
_SATELLITES = (
    ("G19", "noaa-goes19", -75.0, datetime(2024, 10, 15, 18, tzinfo=timezone.utc)),
    ("G18", "noaa-goes18", -137.0, datetime(2022, 5, 11, 19, tzinfo=timezone.utc)),
)

#: The clear-sky-mask quality flag values the product guide states (PUG Vol 5, Table 5.2.6.1-3).
_GOOD, _BAD, _SPACE, _DEGRADED = 0, 1, 2, 6

_START = re.compile(r"_s(\d{4})(\d{3})(\d{2})(\d{2})(\d{2})")


def _moment(sc: str, value: Any, name: str) -> datetime:
    text = str(value or "").strip()
    try:
        found = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        raise router_input_error(sc, f"{name} is not an ISO 8601 instant") from None
    return found if found.tzinfo else found.replace(tzinfo=timezone.utc)


def _window(spec: SourceSpec, params: dict[str, Any]) -> tuple[float, float, datetime, datetime]:
    sc = spec.error_code_prefix
    point = params.get("seed_point")
    if not point or len(point) != 2:
        raise router_input_error(sc, "seed_point is the (lon, lat) the cloud cover is read at")
    if not params.get("start_time") or not params.get("end_time"):
        raise router_input_error(sc, "start_time and end_time state the window the cloud cover is read over")
    start = _moment(sc, params["start_time"], "start_time")
    end = _moment(sc, params["end_time"], "end_time")
    if end < start:
        raise router_input_error(sc, "start_time must not be after end_time")
    if start > datetime.now(timezone.utc):
        raise router_input_error(sc, "start_time is in the future; GOES holds observations only")
    return float(point[0]), float(point[1]), start, end


def _satellite(sc: str, lon: float, start: datetime) -> tuple[str, str]:
    name, bucket, _origin, first = min(_SATELLITES, key=lambda s: abs(lon - s[2]))
    if start < first:
        raise router_input_error(sc, f"{name} ({bucket}) holds {_PRODUCT} from {first:%Y-%m-%dT%H}Z; "
                                     "the window opens before it")
    return name, bucket


def _scan_angles(lat: float, lon: float, proj: Any) -> tuple[float, float] | None:
    """The fixed-grid scan angles (x, y) in radians of a point, or ``None`` where the satellite cannot see it."""
    a, b = float(proj.semi_major_axis), float(proj.semi_minor_axis)
    height = float(proj.perspective_point_height) + a
    lam0 = math.radians(float(proj.longitude_of_projection_origin))
    phi, lam = math.radians(lat), math.radians(lon)
    geocentric = math.atan((b * b) / (a * a) * math.tan(phi))
    radius = b / math.sqrt(1.0 - (a * a - b * b) / (a * a) * math.cos(geocentric) ** 2)
    sx = height - radius * math.cos(geocentric) * math.cos(lam - lam0)
    sy = -radius * math.cos(geocentric) * math.sin(lam - lam0)
    sz = radius * math.sin(geocentric)
    if height * (height - sx) < sy * sy + (a * a) / (b * b) * sz * sz:
        return None
    return math.asin(-sy / math.sqrt(sx * sx + sy * sy + sz * sz)), math.atan(sz / sx)


def _key_start(key: str) -> datetime | None:
    found = _START.search(key)
    if not found:
        return None
    year, doy, hour, minute, second = (int(g) for g in found.groups())
    return datetime(year, 1, 1, tzinfo=timezone.utc) + timedelta(days=doy - 1, hours=hour, minutes=minute,
                                                                 seconds=second)


def _keys(client: Any, bucket: str, hour: datetime) -> list[tuple[datetime, str]]:
    prefix = f"{_PRODUCT}/{hour.year}/{hour.timetuple().tm_yday:03d}/{hour.hour:02d}/"
    found: list[tuple[datetime, str]] = []
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for item in page.get("Contents", ()):
            stamp = _key_start(item["Key"])
            if stamp is not None:
                found.append((stamp, item["Key"]))
    return sorted(found)


def _hours(instant: datetime) -> tuple[datetime, datetime]:
    top = instant.replace(minute=0, second=0, microsecond=0)
    return top - _STEP, top


def _scan_at(listed: dict[datetime, list[tuple[datetime, str]]], instant: datetime) -> tuple[datetime, str] | None:
    """The scan that began last at or before ``instant``, from the instant's hour or the one before."""
    held = [pair for hour in _hours(instant) for pair in listed[hour] if pair[0] <= instant]
    return held[-1] if held else None


def _box(data: bytes, lon: float, lat: float, key: str, sc: str) -> dict[str, Any]:
    """One scan's 5 x 5 block around the point -> the record: counts by quality, and the cloud fraction of the used pixels."""
    import netCDF4

    try:
        ds = netCDF4.Dataset("scan.nc", memory=data)
    except OSError as exc:
        raise router_upstream_error(sc, f"{key} is not a readable netCDF file ({type(exc).__name__})") from exc
    try:
        ds.set_auto_maskandscale(False)
        angles = _scan_angles(lat, lon, ds["goes_imager_projection"])
        if angles is None:
            raise router_input_error(sc, "the point is not visible from the satellite")
        first_x, first_y = int(ds["x"][0]), int(ds["y"][0])
        width, height = len(ds["x"]), len(ds["y"])
        column = round((angles[0] - float(ds["x"].add_offset)) / float(ds["x"].scale_factor))
        row = round((angles[1] - float(ds["y"].add_offset)) / float(ds["y"].scale_factor))
        if not (0 <= column - first_x < width and 0 <= row - first_y < height):
            raise router_input_error(sc, "the point is outside the satellite's CONUS sector")
        x0, y0 = column // _BLOCK * _BLOCK, row // _BLOCK * _BLOCK
        cut = (slice(max(y0 - first_y, 0), min(y0 + _BLOCK - first_y, height)),
               slice(max(x0 - first_x, 0), min(x0 + _BLOCK - first_x, width)))
        mask, quality = ds["BCM"][cut], ds["DQF"][cut]
        good = quality == _GOOD
        used = good & ((mask == 0) | (mask == 1))
        counts = {"n_pixels": int(mask.size), "n_dqf_good": int(good.sum()),
                  "n_dqf_degraded": int((quality == _DEGRADED).sum()), "n_dqf_bad": int((quality == _BAD).sum()),
                  "n_dqf_space": int((quality == _SPACE).sum())}
        counts["n_dqf_other"] = counts["n_pixels"] - sum(counts[k] for k in counts if k != "n_pixels")
        counts["n_mask_fill"] = int((good & ~used).sum())
        counts["n_used"], counts["n_cloudy"] = int(used.sum()), int((used & (mask == 1)).sum())
        fraction = counts["n_cloudy"] / counts["n_used"] if counts["n_used"] else None
        return {"platform_ID": str(ds.platform_ID), "scene_id": str(ds.scene_id),
                "scan_start": str(ds.time_coverage_start), "scan_end": str(ds.time_coverage_end),
                "grid_x": column, "grid_y": row, "box_x0": x0, "box_y0": y0, **counts,
                "cloud_fraction": fraction, "cloud_octas": None if fraction is None else 8.0 * fraction}
    finally:
        ds.close()


@register_hook("goes_cloud.validate")
def validate(spec: SourceSpec, params: dict[str, Any]) -> None:
    """The point, the window and the satellite that sees the point, refused before any request."""
    lon, _lat, start, _end = _window(spec, params)
    _satellite(spec.error_code_prefix, lon, start)


@register_hook("goes_cloud.read")
def read(spec: SourceSpec, params: dict[str, Any], *, timeout_s: float) -> list[dict[str, Any]]:
    """One point feature per distinct scan at or before each whole hour of the window, every field of its box kept."""
    from ..._public_s3 import public_s3_client

    sc = spec.error_code_prefix
    lon, lat, start, end = _window(spec, params)
    platform, bucket = _satellite(sc, lon, start)
    client = public_s3_client("us-east-1")
    instants = [start + _STEP * n for n in range(int((end - start) / _STEP) + 1)]
    hours = sorted({hour for t in instants for hour in _hours(t)})
    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        listed = dict(zip(hours, pool.map(lambda hour: _keys(client, bucket, hour), hours)))
    scans = sorted({found for found in (_scan_at(listed, t) for t in instants) if found})
    if not scans:
        raise router_empty_error(sc, f"{platform} holds no {_PRODUCT} scan over this window", spec.empty_error_suffix)

    def body(scan: tuple[datetime, str]) -> bytes:
        return client.get_object(Bucket=bucket, Key=scan[1])["Body"].read()

    # The files download together; HDF5 is not thread-safe, so each is read here, one at a time.
    rows = []
    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        for scan, data in zip(scans, pool.map(body, scans)):
            rows.append({**_box(data, lon, lat, scan[1], sc), "site": platform, "bucket": bucket, "key": scan[1],
                         "lon": lon, "lat": lat})
    if not any(row["cloud_octas"] is not None for row in rows):
        raise router_empty_error(sc, "no scan of this window has a pixel of good quality in the point's box",
                                 spec.empty_error_suffix)
    return [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]}, "properties": row}
            for row in rows]
