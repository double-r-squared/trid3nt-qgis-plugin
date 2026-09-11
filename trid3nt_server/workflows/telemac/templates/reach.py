"""The reach rows every river template reads, and the steps that name them.

ONE shared DATA row module, by exception: a row two or more templates read is
stated here once rather than restated per template. Where the reach is
(geocode, seed, flowline), how much of it is mapped and meshed, what flows
through it (the carrier discharge, at the cycle the ask names) and what falls
on it (the signed net rain) - each a producer named on a template's own row."""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import os
import re
import tempfile
from typing import Any

from trid3nt_server.workflows.inputs.aoi import aoi_slug
from trid3nt_server.workflows.inputs.layer_fields import layer_field
from trid3nt_server.workflows.runtime import (
    RATE,
    Step,
    TemporalSpec,
    journal_note,
    transform_value,
)
from trid3nt_server.workflows.telemac.errors import (
    ReachMeshUncovered,
    ReachUnmapped,
    TelemacError,
    TelemacInputInvalid,
)

logger = logging.getLogger("trid3nt_server.workflows.telemac.templates.reach")

__all__ = [
    "CarrierDischarge",
    "DEFAULT_RIVER_AOI_HALF_DEG",
    "Geocode",
    "MeshCoverage",
    "ReachSeed",
    "coerce_event_time",
    "event_time",
    "fetch_reach_flowline",
    "geocode_reach",
    "measure_mesh_coverage",
    "measure_water_coverage",
    "reach_seed",
    "resolve_carrier_discharge",
    "resolve_rain_forcing",
]

_HERE = "trid3nt_server.workflows.telemac.templates.reach"

#: Half-width (deg) of the bbox fetched around the geocoded centroid to locate a
#: river reach + pick the seed. ~0.06 deg (~6 km) reliably catches the main stem
#: even when the geocoded city centroid sits a few km off the channel.
DEFAULT_RIVER_AOI_HALF_DEG: float = 0.06

def bbox_center(bbox: Any) -> tuple[float, float]:
    return (0.5 * (float(bbox[0]) + float(bbox[2])),
            0.5 * (float(bbox[1]) + float(bbox[3])))


def bbox_around(lon: float, lat: float,
                half_deg: float = DEFAULT_RIVER_AOI_HALF_DEG
                ) -> tuple[float, float, float, float]:
    return (lon - half_deg, lat - half_deg, lon + half_deg, lat + half_deg)


def registry_fn(name: str) -> Any:
    from trid3nt_server.tools import TOOL_REGISTRY

    entry = TOOL_REGISTRY.get(name)
    if entry is None:
        raise TelemacError(f"required atomic tool {name!r} is not registered.",
                           error_code="TELEMAC_TOOL_UNREGISTERED")
    return entry.fn


async def call_registry_tool(fn: Any, /, *args: Any, **kwargs: Any) -> Any:
    """Invoke a registry tool fn that may be sync or async - normalize both."""
    import inspect

    out = fn(*args, **kwargs)
    if inspect.isawaitable(out):
        out = await out
    return out


def _is_state_snap_geocode(geo: Any) -> bool:
    """True when the geocode fell back to a WHOLE-STATE bbox.

    A state centroid is 100+ km of drift as a reach seed, so it is never used."""
    return isinstance(geo, dict) and (
        geo.get("source") == "state-bbox-fallback"
        or geo.get("fallback_reason") is not None
    )


def _locality_tail(location: str) -> str | None:
    """The locality tail of a compound place name - what follows near/at/by/in.

    The geocoder often has no feature for the compound query but pins the tail."""
    for sep in ("near", "at", "by", "outside", "in"):
        m = re.search(rf"\b{sep}\b(.+)$", location, flags=re.IGNORECASE)
        if m:
            tail = m.group(1).strip(" ,")
            if tail and tail.lower() != location.strip().lower():
                return tail
    return None


async def _geocode_seed_center(geocode_fn: Any, location: str,
                               geo: Any) -> tuple[float, float, str]:
    """(lon, lat, name) for the reach seed, REJECTING state-snaps.

    One retry on the locality tail, then a typed refusal - never another river."""
    if _is_state_snap_geocode(geo):
        tail = _locality_tail(location)
        retry = None
        if tail:
            logger.info("telemac seed geocode: %r snapped to a whole state; retrying "
                        "with locality tail %r", location, tail)
            try:
                retry = await call_registry_tool(geocode_fn, tail)
            except Exception as exc:  # noqa: BLE001 - fall through to the typed error
                logger.warning("telemac seed geocode retry failed: %s", exc)
        if retry is not None and not _is_state_snap_geocode(retry):
            geo = retry
        else:
            raise TelemacError(
                f"geocode_location({location!r}) only matched a whole US state "
                "- too coarse to place a river reach (the centroid would be "
                "~100 km off). Give a more specific place (a city/town near "
                "the reach) or an explicit bbox AOI.",
                error_code="TELEMAC_GEOCODE_AMBIGUOUS")
    glat = geo.get("latitude") if isinstance(geo, dict) else None
    glon = geo.get("longitude") if isinstance(geo, dict) else None
    if glat is None or glon is None:
        raise TelemacError(
            f"geocode_location({location!r}) returned no centroid lat/lon.",
            error_code="TELEMAC_GEOCODE_FAILED")
    return float(glon), float(glat), str(geo.get("name") or location)


def river_seed_from_geometry(river_uri: str) -> tuple[float, float] | None:
    """Mid-reach ``(lon, lat)`` on the LONGEST flowline in the fetched FlatGeobuf.

    The longest line is the main stem; ``None`` on any failure, never a guess."""
    try:
        from trid3nt_server.workflows.solver.solver import (
            _get_s3_client,
            _split_object_uri,
        )

        if river_uri.startswith(("s3://", "gs://")):
            _scheme, bucket, key = _split_object_uri(river_uri)
            tmp = tempfile.NamedTemporaryFile(
                suffix=".fgb", delete=False, prefix="telemac_river_seed_")
            tmp.close()
            resp = _get_s3_client().get_object(Bucket=bucket, Key=key)
            with open(tmp.name, "wb") as fh:
                fh.write(resp["Body"].read())
            local_fgb = tmp.name
        else:
            local_fgb = river_uri  # a local path (test seam)

        import geopandas as gpd

        gdf = gpd.read_file(local_fgb)
        if gdf.empty:
            return None
        if gdf.crs is not None and str(gdf.crs).upper() not in ("EPSG:4326", "WGS84"):
            try:
                gdf = gdf.to_crs(4326)
            except Exception:  # noqa: BLE001
                pass
        lines = gdf[gdf.geometry.type.isin(["LineString", "MultiLineString"])]
        if lines.empty:
            return None
        longest = max(lines.geometry, key=lambda g: g.length)
        if longest.geom_type == "MultiLineString":
            longest = max(longest.geoms, key=lambda g: g.length)
        mid = longest.interpolate(0.5, normalized=True)
        return (float(mid.x), float(mid.y))
    except Exception as exc:  # noqa: BLE001 -- seed extraction is best-effort
        logger.warning("telemac: river-seed extraction failed (non-fatal): %s", exc)
        return None


async def geocode_reach(*, location: str | None,
                        bbox: tuple[float, float, float, float] | None) -> dict[str, Any]:
    """Resolve the reach AOI: a geocoded place, or the centre of an explicit bbox.

    The returned ``bbox`` REBINDS THE DOMAIN for every spatial producer after."""
    if bool(location and str(location).strip()):
        geocode_fn = registry_fn("geocode_location")
        geo = await call_registry_tool(geocode_fn, location)
        lon, lat, name = await _geocode_seed_center(geocode_fn, str(location), geo)
    elif bbox is not None:
        lon, lat = bbox_center(bbox)
        name = f"AOI ({lat:.4f}, {lon:.4f})"
    else:
        raise TelemacInputInvalid(
            "the reach needs a place `location` (geocoded) or an explicit `bbox` AOI.")
    return {"lon": lon, "lat": lat, "name": name,
            "slug": aoi_slug(name, default="reach"),
            "bbox": bbox_around(lon, lat)}


async def fetch_reach_flowline(*, prefetched: str | None = None) -> str | None:
    """The reach flowline FlatGeobuf over the CURRENT DOMAIN. Reference data.

    ``prefetched`` reuses the SAME dataset; a non-object URI is ignored."""
    if prefetched and str(prefetched).startswith(("s3://", "gs://")):
        return str(prefetched)
    if prefetched:
        logger.warning("telemac: river_geometry_uri %r is not an object URI - ignoring",
                       prefetched)
    from trid3nt_server.workflows.runtime import current_domain

    domain = current_domain()
    if domain is None or domain.bbox is None:
        raise TelemacError("the reach flowline cannot be fetched: no domain is bound.",
                           error_code="TELEMAC_DOMAIN_UNBOUND")
    # OSM waterways are MAP CONTEXT, and the name has to say so. The river this
    # run models is the NLDI mainstem the mesh is cut from, which the canvas now
    # shows; an "Input: river geometry" row beside it read as though the solve
    # were built on the OSM line, which it never was.
    layer = await call_registry_tool(
        registry_fn("fetch_river_geometry"), bbox=tuple(domain.bbox),
        purpose="OSM waterways (map context; the modeled river is the NLDI centerline)")
    return layer_field(layer, "uri")


def _read_vector_features(uri: str) -> list[dict[str, Any]]:
    """The GeoJSON features behind a fetched vector layer."""
    import json
    import os

    import geopandas as gpd

    from trid3nt_server.workflows.solver.solver import _get_s3_client, _split_object_uri

    if uri.startswith(("s3://", "gs://")):
        _scheme, bucket, key = _split_object_uri(uri)
        tmp = tempfile.NamedTemporaryFile(suffix=".fgb", delete=False,
                                          prefix="telemac_reach_")
        tmp.close()
        with open(tmp.name, "wb") as fh:
            fh.write(_get_s3_client().get_object(Bucket=bucket, Key=key)["Body"].read())
        path = tmp.name
    else:
        path = uri
    try:
        gdf = gpd.read_file(path)
    finally:
        if path != uri:
            try:
                os.unlink(path)
            except OSError:
                pass
    if gdf.empty:
        return []
    if gdf.crs is not None and str(gdf.crs).upper() not in ("EPSG:4326", "WGS84"):
        gdf = gdf.to_crs(4326)
    return list(json.loads(gdf.to_json())["features"])


async def measure_water_coverage(*, water: Any, centerline: Any) -> Any:
    """MEASURE how much of the reach the fetched water polygons map -> the water.

    A pass-through, and NO threshold: zero refuses, anything above it proceeds."""
    # The gate between the water fetch and the section cut, so an unmapped reach
    # fails on its own cause instead of arriving at the cut as an empty section.
    # Zero coverage is terminal - no rung below can invent a shape for a reach
    # nothing maps - while a measured fraction above zero is journalled, because
    # the stretches mapped only as flowlines are the ones a reader would otherwise
    # assume were modelled.
    fraction = await asyncio.to_thread(_covered_fraction, water, centerline)
    if fraction <= 0.0:
        raise ReachUnmapped()
    journal_note(
        f"reach water: {fraction:.1%} of the modelled centreline is covered by "
        "mapped water polygons; any stretch NHD maps only as a flowline carries "
        "no surveyed width and is not in the domain this run solved over.")
    return water


def _covered_fraction(water: Any, centerline: Any) -> float:
    """Fraction of the centreline's LENGTH that lies inside the water polygons.

    Measured in metres on the reach's own UTM zone, never in degree space."""
    from pyproj import Transformer
    from shapely.geometry import shape
    from shapely.ops import transform as _transform, unary_union

    from trid3nt_server.workflows.inputs.geometry import (
        source_uri, utm_epsg_for,
    )

    lines = [shape(f["geometry"]) for f in
             _read_vector_features(str(source_uri(centerline)))
             if (f.get("geometry") or {}).get("type") in
             ("LineString", "MultiLineString")]
    if not lines:
        raise TelemacError(
            "the reach centreline carries no line geometry, so how much of it is "
            "polygon-mapped cannot be measured.", error_code="REACH_UNMEASURABLE")
    line = unary_union(lines)
    polys = [shape(f["geometry"]).buffer(0) for f in
             _read_vector_features(str(source_uri(water)))
             if (f.get("geometry") or {}).get("type") in ("Polygon", "MultiPolygon")]
    if not polys:
        return 0.0
    epsg = utm_epsg_for(float(line.centroid.x), float(line.centroid.y))
    to_utm = Transformer.from_crs(4326, epsg, always_xy=True).transform
    line_m = _transform(to_utm, line)
    water_m = _transform(to_utm, unary_union(polys))
    if line_m.length <= 0.0:
        return 0.0
    return float(line_m.intersection(water_m).length / line_m.length)


async def measure_mesh_coverage(*, mesh: Any, centerline: Any) -> Any:
    """MEASURE how much of the reach the ACCEPTED mesh actually holds.

    A HEURISTIC, not a gate: zero is terminal, anything above it is journalled."""
    # Distinct from water coverage: that one asks how much of the reach real
    # polygons MAP, this one how much of it the triangulation the solve runs on
    # CONTAINS - a mesher handed a mapped polygon can still leave stretches out,
    # and what a run publishes is about the stretch that was meshed. What to do
    # about a partial cover is the user's call: re-run finer, declare a sizing
    # function, author the mesh. Nothing re-meshes automatically, because a
    # resolution the run picked for itself is a decision the ask never made.
    fraction = await asyncio.to_thread(_meshed_fraction, mesh, centerline)
    if fraction <= 0.0:
        raise ReachMeshUncovered()
    journal_note(
        f"mesh coverage: {fraction:.1%} of the reach centreline lies inside the "
        "accepted mesh; the run answers about that stretch. A finer "
        "mesh_resolution_m, a declared sizing function or a supplied mesh is how "
        "more of the reach gets resolved.")
    return mesh


def _meshed_fraction(mesh: Any, centerline: Any) -> float:
    """Fraction of the centreline's LENGTH that lies inside the mesh's cells.

    Summed over the cells the line touches, never over a union of them."""
    # The triangulation tiles its domain without overlap, so the per-cell
    # intersected lengths already add up to the length inside it and no union of
    # tens of thousands of triangles has to be built to learn that.
    import numpy as np
    import shapely
    from shapely.geometry import LineString

    from trid3nt_server.workflows.mesh.shared.nodes import (
        read_accepted_mesh_nodes, read_centerline_utm,
    )

    utm_epsg = int(getattr(mesh.get("artifact"), "utm_epsg", 0) or 0)
    display_uri = str(mesh.get("display_uri") or "")
    if not display_uri or not utm_epsg:
        raise TelemacError(
            "the accepted mesh carries no display face or no projected zone, so "
            "how much of the reach it holds cannot be measured.",
            error_code="REACH_UNMEASURABLE")
    points_utm, cells, _bed, _lonlat = read_accepted_mesh_nodes(
        display_uri, utm_epsg=utm_epsg)
    line = LineString(read_centerline_utm(centerline, utm_epsg))
    if line.length <= 0.0:
        return 0.0
    rings = np.asarray(points_utm, dtype=float)[np.asarray(cells, dtype=np.int64)]
    cell_polygons = shapely.polygons(np.concatenate([rings, rings[:, :1]], axis=1))
    touched = shapely.STRtree(cell_polygons).query(line, predicate="intersects")
    inside = sum(float(line.intersection(cell_polygons[i]).length) for i in touched)
    return min(1.0, inside / float(line.length))


async def reach_seed(*, reach: dict[str, Any], rivers: str | None,
                     supplied: Any = None) -> dict[str, Any]:
    """THE point the run's one centerline is navigated downstream from.

    A SUPPLIED point wins outright over the flowline midpoint and the centroid."""
    # Naming where the substance enters the water is a statement about which
    # stretch to model, and the navigate has to start there or the reach the user
    # pinned is not the reach that gets meshed. The geocoded centroid is the last
    # rung and is honest: the navigate snaps it to the nearest flowline anyway.
    if supplied is not None:
        return {"lon": float(supplied.lon), "lat": float(supplied.lat),
                "source": "the supplied point the reach is seeded from"}
    seed = None
    if rivers:
        seed = await asyncio.to_thread(river_seed_from_geometry, str(rivers))
    if seed is None:
        return {"lon": float(reach["lon"]), "lat": float(reach["lat"]),
                "source": "geocoded-centroid (NLDI will snap to the nearest flowline)"}
    return {"lon": seed[0], "lat": seed[1],
            "source": "mid-reach point on the largest fetched flowline"}


def Geocode(*, location: Any, bbox: Any) -> Step:  # noqa: N802 - a value constructor
    """Place/AOI -> the reach centre, named ``reach``. Refines the domain for
    everything after it."""
    return Step(runner=f"{_HERE}.geocode_reach", stage="acquire",
                kwargs={"location": location, "bbox": bbox}
                ).overrides_domain().named("reach")


def ReachSeed(*, reach: Any, rivers: Any,  # noqa: N802 - a value constructor
              supplied: Any = None) -> Step:
    """The point the reach's one centerline is navigated from, named ``seed``."""
    return Step(runner=f"{_HERE}.reach_seed", stage="acquire",
                kwargs={"reach": reach, "rivers": rivers, "supplied": supplied}
                ).named("seed")


def MeshCoverage(*, mesh: Any, centerline: Any) -> Step:  # noqa: N802 - a value constructor
    """How much of the reach the accepted mesh holds, measured after the build."""
    return Step(runner=f"{_HERE}.measure_mesh_coverage", stage="mesh",
                kwargs={"mesh": mesh, "centerline": centerline})


#: Half-width (deg) of the NWM query box centred on the reach seed. NWM is a
#: ~2.7M-reach point layer; a small box keeps it to a handful of reaches so the
#: nearest-to-seed pick lands on the carrier reach, not a distant tributary.
_DISCHARGE_QUERY_HALF_DEG: float = 0.03

#: Physically-sane band for the signed net rain-or-evaporation rate (a violent
#: storm ~500 mm/day; extreme PET ~20 mm/day), so a bad knob cannot destabilize
#: the solve.
_NET_RAIN_MIN_MM_DAY, _NET_RAIN_MAX_MM_DAY = -50.0, 2000.0

#: What the rain producer DELIVERS, which is what a declared ``.resample()`` /
#: ``.normalize()`` on the ``Data("rain")`` declaration is checked against. Both
#: rungs are daily-cadence rates: gridMET's aggregate is a daily field the router
#: time-reduces over the window, and a user rate is stated per day. TELEMAC's
#: single RAIN OR EVAPORATION keyword reads mm/day, so a declaration that asked
#: for anything else would be asking the run for a number it cannot carry.
_RAIN_NATIVE_INTERVAL, _RAIN_UNITS = "1D", "mm/day"


def _domain_bbox() -> tuple[float, float, float, float]:
    from trid3nt_server.workflows.runtime import current_domain

    domain = current_domain()
    if domain is None or domain.bbox is None:
        raise TelemacError("forcing cannot be resolved: no domain is bound.",
                           error_code="TELEMAC_DOMAIN_UNBOUND")
    return tuple(domain.bbox)  # type: ignore[return-value]


def _parse_gridmet_window(window: str) -> tuple[str, str]:
    """``"YYYY-MM-DD:YYYY-MM-DD"`` -> (start, end). A bad window is a loud refusal."""
    parts = [p.strip() for p in str(window or "").split(":") if p.strip()]
    if len(parts) != 2:
        raise TelemacInputInvalid(
            f"rainfall_gridmet_window must be 'YYYY-MM-DD:YYYY-MM-DD' (got {window!r}).")
    import datetime as _dt
    try:
        _dt.date.fromisoformat(parts[0])
        _dt.date.fromisoformat(parts[1])
    except ValueError as exc:
        raise TelemacInputInvalid(
            f"rainfall_gridmet_window has a non-ISO date: {exc}") from exc
    return parts[0], parts[1]


def _gridmet_domain_mean_pr(bbox: tuple[float, float, float, float],
                            start_date: str, end_date: str) -> float:
    """Domain-mean daily precipitation (mm/day) from the wired gridMET fetcher.

    Any failure REFUSES typed: a real storm never degrades to zero rain."""
    import numpy as np
    import rasterio
    from rasterio.io import MemoryFile

    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.workflows.solver.solver import _get_s3_client

    try:
        layer = TOOL_REGISTRY["fetch_gridmet"].fn(
            bbox=list(bbox), variable="pr", start_date=start_date, end_date=end_date)
    except Exception as exc:  # noqa: BLE001
        raise TelemacError(
            f"gridMET precip fetch failed for {start_date}..{end_date}: {exc}",
            error_code="TELEMAC_RAIN_SOURCE_FAILED") from exc
    uri = getattr(layer, "uri", None) or (
        layer.get("uri") if isinstance(layer, dict) else None)
    if not uri:
        raise TelemacError("gridMET fetch returned no COG uri.",
                           error_code="TELEMAC_RAIN_SOURCE_FAILED")
    try:
        if str(uri).startswith("s3://"):
            bucket, _, key = str(uri)[len("s3://"):].partition("/")
            data = _get_s3_client().get_object(Bucket=bucket, Key=key)["Body"].read()
            with MemoryFile(data) as mem, mem.open() as ds:
                arr = ds.read(1, masked=True).astype("float64")
        else:
            with rasterio.open(str(uri)) as ds:
                arr = ds.read(1, masked=True).astype("float64")
    except Exception as exc:  # noqa: BLE001
        raise TelemacError(f"gridMET COG read failed: {exc}",
                           error_code="TELEMAC_RAIN_SOURCE_FAILED") from exc
    vals = np.asarray(arr.compressed() if hasattr(arr, "compressed") else arr,
                      dtype="float64")
    vals = vals[np.isfinite(vals)]
    if vals.size == 0:
        raise TelemacError("gridMET precip COG had no finite pixels over the reach AOI.",
                           error_code="TELEMAC_RAIN_SOURCE_FAILED")
    return float(vals.mean())


async def resolve_rain_forcing(*, rainfall_mm_per_day: float | None,
                               evaporation_mm_per_day: float | None,
                               gridmet_window: str | None,
                               temporal: TemporalSpec | None = None) -> dict[str, Any]:
    """The SIGNED net rain-or-evaporation rate (mm/day) the sheet carries.

    A ``None`` rate is no forcing asked for, and the deck stays byte-identical."""
    # A dated gridMET window is the storm total for that window; without one, an
    # explicit user rate. Evaporation is then subtracted, because the engine has a
    # single signed RAIN OR EVAPORATION keyword. ``temporal`` is the declaration's
    # own resample/normalize, checked against the cadence and units this producer
    # delivers, and what it performs or declines is stamped onto the note.
    return await asyncio.to_thread(
        _rain_forcing, rainfall_mm_per_day, evaporation_mm_per_day,
        gridmet_window, temporal)


def _rain_forcing(rainfall_mm_per_day: float | None,
                  evaporation_mm_per_day: float | None,
                  gridmet_window: str | None,
                  temporal: TemporalSpec | None = None) -> dict[str, Any]:
    rung: str | None = None
    rain: float | None = None
    note_bits: list[str] = []
    if gridmet_window is not None and str(gridmet_window).strip():
        start_date, end_date = _parse_gridmet_window(gridmet_window)
        rain = _gridmet_domain_mean_pr(_domain_bbox(), start_date, end_date)
        rung = "gridmet_domain_mean"
        note_bits.append(
            f"gridMET pr domain-mean {rain:.1f} mm/day ({start_date}..{end_date})")
    elif rainfall_mm_per_day is not None:
        rain = float(rainfall_mm_per_day)
        rung = "user_rate"
        note_bits.append(f"rainfall {rain:.1f} mm/day (user)")

    evap: float | None = None
    if evaporation_mm_per_day is not None:
        evap = float(evaporation_mm_per_day)
        rung = rung or "user_rate"
        note_bits.append(f"evaporation {evap:.1f} mm/day")

    if rain is None and evap is None:
        return {"mm_per_day": None, "note": None, "rung": None,
                "temporal_note": None}
    net = float(min(max((rain or 0.0) - (evap or 0.0),
                        _NET_RAIN_MIN_MM_DAY), _NET_RAIN_MAX_MM_DAY))
    if temporal is not None and temporal.units is not None \
            and temporal.units.units != _RAIN_UNITS:
        raise TelemacInputInvalid(
            f"Data('rain').normalize(units={temporal.units.units!r}) cannot be "
            f"honored: TELEMAC's RAIN OR EVAPORATION keyword reads {_RAIN_UNITS}.")
    # The clamp band above is stated in mm/day, so the declared transform runs
    # after it; the units assertion just above is what keeps the two agreeing.
    moved = transform_value(net, temporal, quantity=RATE, units=_RAIN_UNITS,
                            native=_RAIN_NATIVE_INTERVAL)
    net = float(moved.values)
    note = "; ".join(note_bits) + f" -> net {net:+.1f} mm/day (distributed on-mesh)"
    if temporal is not None:
        note = f"{note} [{moved.note}]"
    logger.info("telemac rainfall forcing: %s", note)
    return {"mm_per_day": net, "note": note, "rung": rung,
            "temporal_note": moved.note}


def coerce_event_time(value: Any) -> str | None:
    """A UTC ISO-8601 timestamp from a wire date/datetime; ``None`` reads latest.

    A bare date is midnight UTC; a malformed value REFUSES, never falls back."""
    # Which discharge cycle governs dilution is a physically consequential choice,
    # so reading a different one than the one asked for is not available here. The
    # NWM PDS bucket retains only the last ~30 days; a request outside that window
    # still parses here and refuses later, typed, at the fetch itself.
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    iso = s[:-1] + "+00:00" if s.endswith("Z") else s
    try:
        dt = _dt.datetime.fromisoformat(iso)
    except ValueError:
        raise TelemacInputInvalid(
            f"event_time={value!r} is not a parseable ISO-8601 date or datetime "
            "(e.g. '2026-08-20' or '2026-08-20T06:00:00Z'). Omit it to read the "
            "most recent published NWM cycle.") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt.astimezone(_dt.timezone.utc).isoformat()


def event_time() -> Any:
    """A coercion that reads the wire's ``event_time`` into a pinned ISO cycle."""

    def _coerce(args: Any) -> dict[str, Any]:
        return {"event_time": coerce_event_time(args.get("event_time"))}

    _coerce.__name__ = "event_time"
    return _coerce


def _fmt_cycle(reference_time: str | None) -> str:
    """A short display form of a resolved cycle ISO string, for names/notes."""
    if not reference_time:
        return "unresolved cycle"
    try:
        dt = _dt.datetime.fromisoformat(str(reference_time).replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%dT%H:%MZ")
    except ValueError:
        return str(reference_time)


def _fmt_discharge(value: float) -> str:
    """A discharge for a NOTE, at a precision that cannot misstate it.

    The decimals follow the magnitude: whole numbers print 2.2 m3/s as "2"."""
    magnitude = abs(float(value))
    if magnitude >= 100.0:
        return f"{float(value):.0f}"
    if magnitude >= 1.0:
        return f"{float(value):.1f}"
    return f"{float(value):.3f}"


async def resolve_carrier_discharge(*, seed: dict[str, Any],
                                    explicit: float | None,
                                    event_time: str | None = None) -> dict[str, Any]:
    """The reach CARRIER discharge (m3/s) - real NWM streamflow, or a typed gate.

    A fetch or read miss REFUSES typed naming ``discharge_m3s``, never a constant."""
    # An explicit value short-circuits the fetch; otherwise the NHDPlus reach
    # nearest the seed in the National Water Model is the carrier, read at
    # ``event_time`` when set and at the most recent published cycle otherwise. The
    # returned note and ``reference_time`` pin the cycle actually served, so a
    # "latest" request is a real timestamp before it reaches provenance or metrics.
    seed_lon, seed_lat = float(seed["lon"]), float(seed["lat"])
    if explicit is not None:
        return {"m3s": float(explicit), "basis": "user", "real_source": None,
                "reference_time": None, "product": None,
                "note": f"carrier discharge {_fmt_discharge(explicit)} m3/s "
                        "(user-supplied)"}

    found = await asyncio.to_thread(_nwm_nearest_streamflow, seed_lon, seed_lat, event_time)
    if found is None:
        retention_txt = (
            f" for event_time={event_time!r} (the NWM PDS bucket retains only "
            "the last ~30 days; a request outside that window is not "
            "available - deeper history is a documented gap, not a source we "
            "carry)" if event_time else " for this reach"
        )
        raise TelemacError(
            "The NOAA National Water Model streamflow lookup found no carrier "
            f"discharge{retention_txt}, so the discharge that governs dilution "
            "is not fabricated. Retry with an explicit discharge_m3s (steady "
            "upstream carrier discharge, m3/s) for the reach, a different "
            "event_time within the retention window, or omit event_time for "
            "the latest cycle.", error_code="TELEMAC_DISCHARGE_INPUT_REQUIRED")
    await _surface_discharge_station_layer(found.get("layer"))
    reference_time = found.get("reference_time")
    product = found.get("product") or "analysis_assim"
    cycle_txt = f"{product} @ {_fmt_cycle(reference_time)}"
    return {
        "m3s": round(found["m3s"], 1), "basis": "fetched",
        "real_source": "fetch_noaa_nwm_streamflow (NOAA National Water Model)",
        "reference_time": reference_time, "product": product,
        "note": (f"carrier discharge {_fmt_discharge(found['m3s'])} m3/s (NOAA "
                 f"National Water Model, nearest reach to the seed, {cycle_txt})"),
    }


def CarrierDischarge(*, seed: Any, explicit: Any, event_time: Any = None) -> Step:  # noqa: N802
    """The reach's carrier discharge, named ``carrier_discharge``. A STEP, not
    Data: it reads the resolved seed, which is a step result rather than a
    declaration a producer could name."""
    return Step(runner=f"{_HERE}.resolve_carrier_discharge", stage="acquire",
                kwargs={"seed": seed, "explicit": explicit, "event_time": event_time}
                ).named("carrier_discharge")


async def _surface_discharge_station_layer(layer: Any) -> None:
    """Publish the NWM point layer as a context input, named for the cycle served.

    Best-effort: never raises, and a missing layer never voids the resolution."""
    # The fetch runs with ``visualize=False``, suppressing a generic auto-emission
    # that would only know the REQUESTED time rather than the resolved one, so this
    # is the only station layer that reaches the canvas: exactly one, and captioned
    # with the cycle actually served rather than the bare request word.
    if layer is None:
        return
    try:
        from trid3nt_server.emission.layer_uri_emit import publish_input_layer
        from trid3nt_server.emission.pipeline_emitter import current_emitter

        emitter = current_emitter()
        if emitter is None:
            return
        reference_time = getattr(layer, "reference_time", None)
        product = getattr(layer, "product", None) or "analysis_assim"
        station = layer.model_copy(update={
            "layer_id": f"input-nwm-streamflow-station-{layer.layer_id}",
            "name": f"Input: NWM discharge station ({product} @ "
                    f"{_fmt_cycle(reference_time)})",
            "role": "context", "bbox": None,
        })
        await publish_input_layer(emitter, station, role="context")
    except Exception as exc:  # noqa: BLE001 - input surfacing is NEVER fatal
        logger.warning("telemac: NWM station layer surfacing failed (non-fatal, "
                       "the discharge resolution is unaffected): %s", exc)


def _nwm_nearest_streamflow(seed_lon: float, seed_lat: float,
                            valid_time: str | None = None) -> dict[str, Any] | None:
    """The NWM reach nearest the seed: its discharge + the RESOLVED cycle served.

    ``None`` on any miss: offline, no coverage at the seed, or outside retention."""
    from trid3nt_server.tools import TOOL_REGISTRY

    box = (seed_lon - _DISCHARGE_QUERY_HALF_DEG, seed_lat - _DISCHARGE_QUERY_HALF_DEG,
           seed_lon + _DISCHARGE_QUERY_HALF_DEG, seed_lat + _DISCHARGE_QUERY_HALF_DEG)
    try:
        # visualize=False: this is a probe fetch for ONE scalar, not the
        # engine's own input - resolve_carrier_discharge surfaces its own
        # cycle-pinned station layer once the resolved reference_time is known.
        layer = TOOL_REGISTRY["fetch_noaa_nwm_streamflow"].fn(
            bbox=box, valid_time=valid_time, visualize=False)
    except Exception as exc:  # noqa: BLE001 - a fetch miss => the typed gate above
        logger.info("telemac: NWM streamflow fetch failed for seed %s valid_time=%s "
                    "(%s)", (seed_lon, seed_lat), valid_time, exc)
        return None
    uri = getattr(layer, "uri", None) or (
        layer.get("uri") if isinstance(layer, dict) else None)
    if not uri:
        return None

    local: str | None = None
    try:
        import geopandas as gpd  # lazy: never imported on the offline path

        from trid3nt_server.workflows.solver.solver import (
            _get_s3_client,
            _split_object_uri,
        )

        _scheme, bucket, key = _split_object_uri(str(uri))
        fd, local = tempfile.mkstemp(prefix="nwm-",
                                     suffix=os.path.splitext(key)[1] or ".fgb")
        os.close(fd)
        resp = _get_s3_client().get_object(Bucket=bucket, Key=key)
        with open(local, "wb") as fh:
            fh.write(resp["Body"].read())
        gdf = gpd.read_file(local, engine="pyogrio")
    except Exception as exc:  # noqa: BLE001 - a read miss => the typed gate above
        logger.info("telemac: could not read NWM streamflow layer %s (%s)", uri, exc)
        return None
    finally:
        if local and os.path.exists(local):
            try:
                os.unlink(local)
            except OSError:
                pass

    best_q: float | None = None
    best_d = float("inf")
    for _idx, row in gdf.iterrows():
        try:
            q = float(row["streamflow_cms"])
        except (KeyError, TypeError, ValueError):
            continue
        geom = row.get("geometry")
        try:
            d = (float(geom.x) - seed_lon) ** 2 + (float(geom.y) - seed_lat) ** 2
        except Exception:  # noqa: BLE001
            d = 0.0
        if d < best_d and q > 0.0:
            best_d, best_q = d, q
    if best_q is None:
        return None
    return {
        "m3s": best_q,
        "reference_time": getattr(layer, "reference_time", None),
        "product": getattr(layer, "product", None) or "analysis_assim",
        "layer": layer,
    }
