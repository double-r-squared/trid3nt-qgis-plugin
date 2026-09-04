"""flood_extent_observation hooks (landcover + flood-extent wave).

The irreducible per-source steps the declarative categorical_tile_grid access
mode cannot carry:
- ``pre_resolve`` -- the LANCE NRT dir-walk that resolves ``date`` (or None ->
  latest) to the ``(year, doy)`` the tile URLs template on, run BEFORE
  read_through so the resolved day enters the cache key (a date=None request must
  not forever serve the first-cached day).
- ``envelope`` -- the POST-EMIT class-breakdown / flood-area / caveats / legend
  read back from the produced categorical COG (-> FloodExtentObservationResult).

Everything else -- the per-tile GET + first-valid uint8 mosaic + palette COG,
transport, retry, cache, payload gate, LayerURI -- is the shared router (the
categorical_tile_grid mode + array_to_cog_bytes).
"""

from __future__ import annotations

import datetime as _dt
import json
import math
from typing import Any

from trid3nt_contracts.execution import LegendClass, LegendKey
from trid3nt_contracts.source_spec import SourceSpec

from .. import hooks as _hooks
from ..errors import router_empty_error, router_input_error, router_upstream_error

__all__ = ["pre_resolve", "envelope", "MCDWD_CLASSES", "NODATA"]

#: LANCE NRT MODIS Global Flood Product, 3-day composite, collection 61.
_PRODUCT = "MCDWD_L3_F3_NRT"
_LANCE_API = (
    "https://nrt3.modaps.eosdis.nasa.gov/api/v2/content/details/allData/61/" + _PRODUCT
)

#: MCDWD native pixel encoding (RevE user guide). Value -> label.
MCDWD_CLASSES: dict[int, str] = {
    0: "No water",
    1: "Surface water (reference)",
    2: "Recurring flood",
    3: "Flood water",
}

#: Insufficient-data / cloud fill (nodata).
NODATA = 255

#: 10-degree geographic tiles, 4800 px (~0.00208333 deg cell) -- the flood-area
#: cell size (the envelope's km^2 conversion, twin ``_CELL_DEG``).
_CELL_DEG = 10.0 / 4800.0

_CAVEATS = [
    "Satellite flood mapping UNDER-detects flooding beneath vegetation canopy "
    "and in dense urban areas -- this SAR-and-optical detection limit applies "
    "to this MODIS/MCDWD product too; validate against ground truth (surveyed "
    "high-water marks) before treating the extent as complete.",
    "MODIS MCDWD is 250 m: narrow channels, small ponds, and sub-pixel "
    "flooding are missed, and cloud cover in the optical compositing window "
    "leaves data gaps (nodata=255).",
    "This is the NEAR-REAL-TIME 3-day product (a rolling recent window, "
    "provisional) -- NOT the QA'd/reprocessed archive; a specific historical "
    "event may be unavailable.",
]


# --------------------------------------------------------------------------- #
# pre_resolve: the LANCE dir-walk (date -> year/doy), over the shared transport.
# --------------------------------------------------------------------------- #


def _list_dir_names(spec: SourceSpec, url: str) -> list[str]:
    """The child directory names (digit-named) under a LANCE content-details URL."""
    from ..transport import TransportError, TransportNotFound, get_bytes, get_client

    ua = spec.auth.user_agent if spec.auth else "trid3nt_default"
    try:
        raw, _ct, _u = get_bytes(get_client(), url, headers={"User-Agent": ua})
    except TransportNotFound:
        return []
    except TransportError as exc:
        raise router_upstream_error(spec.error_code_prefix, f"LANCE listing failed {url}: {exc}")
    if not raw:
        return []
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise router_upstream_error(spec.error_code_prefix, f"LANCE listing is not valid JSON: {exc}")
    return [
        str(e.get("name"))
        for e in obj.get("content", [])
        if e.get("resourceType") == "Directory" and str(e.get("name") or "").isdigit()
    ]


def _latest_available(spec: SourceSpec) -> tuple[int, int]:
    """Newest available ``(year, day_of_year)`` in the NRT archive (twin parity)."""
    years = [int(n) for n in _list_dir_names(spec, _LANCE_API + "/")]
    if not years:
        raise router_empty_error(
            spec.error_code_prefix,
            "the LANCE MCDWD flood archive listing returned no years.",
            spec.empty_error_suffix,
        )
    year = max(years)
    days = [int(n) for n in _list_dir_names(spec, f"{_LANCE_API}/{year}/")]
    if not days:
        raise router_empty_error(
            spec.error_code_prefix,
            f"no days available under the {year} MCDWD flood archive.",
            spec.empty_error_suffix,
        )
    return year, max(days)


@_hooks.register_hook("flood_extent_observation.pre_resolve")
def pre_resolve(spec: SourceSpec, params: dict[str, Any]) -> dict[str, Any]:
    """Resolve ``date`` (ISO ``YYYY-MM-DD``) -> ``{year, doy}``; None -> latest.

    Merged into params by ``route()`` BEFORE read_through so the resolved day enters
    the cache key. A given date is a pure parse; None triggers the dir-walk.
    """
    date = params.get("date")
    if date is None or not str(date).strip():
        year, doy = _latest_available(spec)
        return {"year": year, "doy": doy}
    try:
        d = _dt.date.fromisoformat(str(date).strip())
    except ValueError as exc:
        raise router_input_error(
            spec.error_code_prefix,
            f"date={date!r} is not a valid ISO date (YYYY-MM-DD): {exc}",
            spec.input_error_suffix,
        )
    return {"year": d.year, "doy": d.timetuple().tm_yday}


# --------------------------------------------------------------------------- #
# envelope: class-breakdown / flood-area / legend / caveats from the produced COG.
# --------------------------------------------------------------------------- #


def _summarize_cog(data: bytes, bbox: tuple[float, float, float, float]) -> dict[str, Any]:
    """Class breakdown + flood area from the produced categorical COG (twin parity)."""
    import numpy as np
    import rasterio
    from rasterio.io import MemoryFile

    with MemoryFile(data) as mem, mem.open() as src:
        arr = src.read(1)
        lat_mid = (bbox[1] + bbox[3]) / 2.0
    vals, counts = np.unique(arr, return_counts=True)
    breakdown: dict[str, int] = {}
    for val, cnt in zip(vals.tolist(), counts.tolist()):
        if val == NODATA:
            continue
        breakdown[MCDWD_CLASSES.get(int(val), f"class_{int(val)}")] = int(cnt)
    flood_px = int(((arr == 2) | (arr == 3)).sum())
    cell_km_lat = _CELL_DEG * 111.32
    cell_km_lon = _CELL_DEG * 111.32 * math.cos(math.radians(lat_mid))
    flood_area = flood_px * cell_km_lat * cell_km_lon
    return {
        "class_breakdown": breakdown,
        "flood_pixel_count": flood_px,
        "flood_area_km2": round(flood_area, 4),
    }


@_hooks.register_hook("flood_extent_observation.envelope")
def envelope(spec: SourceSpec, params: dict[str, Any], layer: Any, data: bytes | None) -> dict[str, Any]:
    """The observation envelope (-> FloodExtentObservationResult)."""
    bbox = tuple(float(v) for v in params["bbox"])
    year = int(params["year"])
    doy = int(params["doy"])
    obs_date = _dt.date(year, 1, 1) + _dt.timedelta(days=doy - 1)
    summary = _summarize_cog(data, bbox) if data else {
        "class_breakdown": {}, "flood_pixel_count": 0, "flood_area_km2": None,
    }
    notes = [
        f"NASA LANCE {_PRODUCT} 3-day composite for {obs_date.isoformat()} "
        f"(doy {doy}); {summary['flood_pixel_count']} flood pixel(s) "
        f"(~{summary['flood_area_km2']} km^2) in the AOI.",
    ]
    return {
        "name": f"Observed flood extent (MODIS {obs_date.isoformat()})",
        "legend": LegendKey(
            kind="classed",
            classes=[
                LegendClass(value=1, color="#92c5de", label="Surface water (reference)"),
                LegendClass(value=2, color="#f4a582", label="Recurring flood"),
                LegendClass(value=3, color="#ca0020", label="Flood water"),
            ],
            label="Observed flood extent (MODIS MCDWD)",
        ),
        "product": _PRODUCT,
        "observation_date": obs_date.isoformat(),
        "class_breakdown": summary["class_breakdown"],
        "flood_pixel_count": summary["flood_pixel_count"],
        "flood_area_km2": summary["flood_area_km2"],
        "caveats": list(_CAVEATS),
        "notes": notes,
    }
