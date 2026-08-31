"""The reach front of every TELEMAC river plan: geocode, seed, river, mesh size.

Three declared steps, one declared Data producer, and the river resolution the
deck writer calls:

* ``Geocode.reach`` - place/AOI to a reach centre, and it REBINDS THE DOMAIN, so
  every spatial producer after it reads the reach AOI implicitly.
* ``fetch_reach_flowline`` - the ``Data("rivers")`` producer (reference data:
  fetched fresh for the domain, never supplied). It is an OSM waterway layer and
  its job is to place the mid-reach seed; it is not the river the run models,
  and the canvas says so.
* ``ReachSeed`` - the mid-reach point on the largest fetched flowline, which is
  where the seed ladder starts and where the carrier-discharge lookup queries.
* ``resolve_reach_river`` - the NLDI mainstem centerline, the NHDArea banks and
  the bed, fetched here and staged into the run directory. THE MESHED RIVER IS
  THE VISIBLE RIVER: the centerline this fetches is the layer the canvas shows,
  because it is the geometry the solve is built on.

The CFL timestep law lives here too: it is coupled to the edge the accepted mesh
was measured at, so a refined mesh tightens dt without anybody restating it.
"""

from __future__ import annotations

import asyncio
import logging
import re
import tempfile
from typing import Any

from trid3nt_server.workflows.lib import Step, journal_note, user_input
from trid3nt_server.workflows.shared.layer_fields import layer_field

from .errors import ReachBanksUnmapped, TelemacDyeScenarioError

logger = logging.getLogger("trid3nt_server.workflows.telemac.steps.reach")

__all__ = [
    "BANKS_DEST",
    "BED_DEST",
    "CENTERLINE_DEST",
    "DEFAULT_RIVER_AOI_HALF_DEG",
    "Geocode",
    "MESH_H_FLOOR_M",
    "MESH_NODE_CAP",
    "ReachSeed",
    "coerce_lonlat_point",
    "estimate_telemac_solve_seconds",
    "fetch_reach_flowline",
    "geocode_reach",
    "measure_bank_coverage",
    "named_watercourse",
    "reach_seed",
    "resolve_reach_river",
    "slug",
    "suggest_time_step_s",
]

_STEPS = "trid3nt_server.workflows.telemac.steps"

#: Half-width (deg) of the bbox fetched around the geocoded centroid to locate a
#: river reach + pick the seed. ~0.06 deg (~6 km) reliably catches the main stem
#: even when the geocoded city centroid sits a few km off the channel.
DEFAULT_RIVER_AOI_HALF_DEG: float = 0.06

# --------------------------------------------------------------------------- #
# Mesh granularity BOUNDS. The edge length ``h`` is an explicit sheet value; what
# a run is judged on is the edge the ACCEPTED mesh was measured at, so nothing
# here derives one from a channel width nobody surveyed.
# --------------------------------------------------------------------------- #
#: Absolute gmsh edge-length floor (below it quality + solve cost degrade).
MESH_H_FLOOR_M: float = 3.0
#: Node ceiling for a single local-docker TELEMAC reach. It bounds how long the
#: dispatcher WAITS - the worst case it sizes the wait against - and sizes no mesh:
#: the edge a run meshes at is the explicit sheet value the mesher was handed.
MESH_NODE_CAP: int = 60000
#: The timestep MUST be coupled to the edge length or the solve diverges (CFL).
#: dt = TIMESTEP_REF_S * min(1, h / MESH_TIMESTEP_REF_M), anchored at h=20 -> 1 s
#: so the law passes through both live-proven-stable points (20, 1.0) and
#: (10, 0.5) and lands at or below the stable dt at every tested size.
TIMESTEP_REF_S: float = 1.0
MESH_TIMESTEP_REF_M: float = 20.0
#: Floor on the coupled timestep (a runaway-fine mesh cannot drive dt to zero).
TIMESTEP_FLOOR_S: float = 0.2
#: Conservative throughput in node-steps/second, calibrated on two live runs
#: (rates 0.377M and 0.618M/s; the SLOWER is taken so estimates err HIGH).
_TELEMAC_NODE_STEPS_PER_S: float = 377_000.0
#: Fixed overhead outside the node-step model (container start + fetches).
_TELEMAC_SOLVE_OVERHEAD_S: float = 45.0


def suggest_time_step_s(mesh_size_m: float, *, mesh: Any = None) -> float:
    """CFL-safe TELEMAC timestep for the mesh a run will actually solve on.

    A BUILT mesh knows its own shortest edge, and that is what the stability
    criterion is about; ``mesh_size_m`` is the edge that was REQUESTED, which is
    all an estimate made before any mesh exists can honestly use. So a mesh
    artifact wins when one is supplied - refine a region at the gate and dt
    tightens with it, without anybody restating the number.
    """
    from trid3nt_server.workflows.mesh.artifact import measured_min_edge_m

    measured = measured_min_edge_m(mesh)
    h = max(float(mesh_size_m if measured is None else measured), MESH_H_FLOOR_M)
    dt = TIMESTEP_REF_S * min(1.0, h / MESH_TIMESTEP_REF_M)
    return round(max(dt, TIMESTEP_FLOOR_S), 3)


def estimate_telemac_solve_seconds(
    npoin: int, sim_duration_s: float, time_step_s: float
) -> float:
    """Conservative wall-clock estimate for a full TELEMAC solve. Errs high by design."""
    steps = max(float(sim_duration_s), 0.0) / max(float(time_step_s), 1e-6)
    est = max(int(npoin), 0) * steps / _TELEMAC_NODE_STEPS_PER_S
    return round(est + _TELEMAC_SOLVE_OVERHEAD_S, 1)


# --------------------------------------------------------------------------- #
# Geometry + registry helpers
# --------------------------------------------------------------------------- #
def slug(name: str) -> str:
    """A safe reach slug for the deck's ``name`` (ASCII, underscores)."""
    keep = [c.lower() if c.isalnum() else "_" for c in str(name)]
    out = "".join(keep).strip("_")
    while "__" in out:
        out = out.replace("__", "_")
    return (out or "river_dye")[:48]


def coerce_lonlat_point(value: Any, *,
                        label: str = "release point") -> tuple[float, float] | None:
    """``(lon, lat)`` from a wire value, in TELEMAC's own error family.

    The SHAPE rules are the user-input species' (one normalizer, whether the point
    was clicked or typed); what this adds is the engine's error TYPE, because
    callers whose contract is fail-open - the pre-dispatch gate builder, the mesh
    preview - catch ``TelemacDyeScenarioError`` at the call site.
    """
    try:
        return user_input.lonlat_point(value, label=label,
                                       code="TELEMAC_PARAMS_INVALID")
    except user_input.UserInputError as exc:
        raise TelemacDyeScenarioError("TELEMAC_PARAMS_INVALID", str(exc)) from None


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
        raise TelemacDyeScenarioError(
            "TELEMAC_DYE_SCENARIO_ERROR",
            f"required atomic tool {name!r} is not registered.",
        )
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

    A state-snap centroid is the middle of the state - as a river-reach seed that
    is 100+ km of drift, so it is never seeded from.
    """
    return isinstance(geo, dict) and (
        geo.get("source") == "state-bbox-fallback"
        or geo.get("fallback_reason") is not None
    )


def _locality_tail(location: str) -> str | None:
    """'Snake River near Twin Falls, Idaho' -> 'Twin Falls, Idaho'.

    Nominatim often has no feature for the compound query but pins the locality
    fine; the worker NLDI-snaps the locality seed to the nearest flowline anyway.
    """
    for sep in ("near", "at", "by", "outside", "in"):
        m = re.search(rf"\b{sep}\b(.+)$", location, flags=re.IGNORECASE)
        if m:
            tail = m.group(1).strip(" ,")
            if tail and tail.lower() != location.strip().lower():
                return tail
    return None


_WATERCOURSE_TYPES = ("river", "creek", "slough", "fork", "bayou")
_NAME_STOPWORDS = frozenset({"the", "a", "an", "on", "in", "into", "near", "at", "by"})


def named_watercourse(location: str) -> str | None:
    """The GNIS-style watercourse name in a location phrase, or None.

    The worker re-seeds onto the NAMED mainstem before the position snap, so a
    geocode near a confluence stops landing the mesh on the tributary.
    """
    m = re.search(
        rf"\b((?:[\w'.-]+\s+){{1,3}}(?:{'|'.join(_WATERCOURSE_TYPES)}))\b",
        str(location or ""), flags=re.IGNORECASE,
    )
    if not m:
        return None
    words = m.group(1).split()
    while words and words[0].lower() in _NAME_STOPWORDS:
        words = words[1:]
    if len(words) < 2:  # need at least '<Name> River'
        return None
    return " ".join(w.title() for w in words)


async def _geocode_seed_center(geocode_fn: Any, location: str,
                               geo: Any) -> tuple[float, float, str]:
    """(lon, lat, name) for the reach seed, REJECTING state-snaps.

    On a state-snap, retry once with the locality tail; if that also snaps (or no
    tail exists), refuse typed rather than simulating the wrong river.
    """
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
            raise TelemacDyeScenarioError(
                "TELEMAC_DYE_GEOCODE_AMBIGUOUS",
                f"geocode_location({location!r}) only matched a whole US state "
                "- too coarse to place a river reach (the centroid would be "
                "~100 km off). Give a more specific place (a city/town near "
                "the reach) or an explicit bbox AOI.",
            )
    glat = geo.get("latitude") if isinstance(geo, dict) else None
    glon = geo.get("longitude") if isinstance(geo, dict) else None
    if glat is None or glon is None:
        raise TelemacDyeScenarioError(
            "TELEMAC_DYE_GEOCODE_FAILED",
            f"geocode_location({location!r}) returned no centroid lat/lon.",
        )
    return float(glon), float(glat), str(geo.get("name") or location)


def river_seed_from_geometry(river_uri: str) -> tuple[float, float] | None:
    """Mid-reach ``(lon, lat)`` on the LONGEST flowline in the fetched FlatGeobuf.

    So the worker's NLDI snap lands on the main stem, not a stray ditch. Returns
    None on ANY failure - the caller then falls back to the geocoded centroid,
    which the worker snaps regardless.
    """
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
        logger.warning("telemac dye: river-seed extraction failed (non-fatal): %s", exc)
        return None


# --------------------------------------------------------------------------- #
# The declared runners
# --------------------------------------------------------------------------- #
async def geocode_reach(*, location: str | None,
                        bbox: tuple[float, float, float, float] | None) -> dict[str, Any]:
    """Resolve the reach AOI: a geocoded place, or the centre of an explicit bbox.

    The returned ``bbox`` is what REBINDS THE DOMAIN, so every spatial producer
    after this step fetches over the reach AOI without being handed one.
    """
    if bool(location and str(location).strip()):
        geocode_fn = registry_fn("geocode_location")
        geo = await call_registry_tool(geocode_fn, location)
        lon, lat, name = await _geocode_seed_center(geocode_fn, str(location), geo)
    elif bbox is not None:
        lon, lat = bbox_center(bbox)
        name = f"AOI ({lat:.4f}, {lon:.4f})"
    else:
        raise TelemacDyeScenarioError(
            "TELEMAC_DYE_SCENARIO_INPUT_INVALID",
            "the reach needs a place `location` (geocoded) or an explicit `bbox` AOI.",
        )
    return {
        "lon": lon, "lat": lat, "name": name, "slug": slug(name),
        "river_name": named_watercourse(location or name) or "",
        "bbox": bbox_around(lon, lat),
    }


async def fetch_reach_flowline(*, prefetched: str | None = None) -> str | None:
    """The reach flowline FlatGeobuf over the CURRENT DOMAIN. Reference data.

    ``prefetched`` reuses a flowline the caller already fetched for this reach -
    the same dataset, not a substituted one, so no cross-dataset gate applies.
    A non-object URI is a model-invented pseudo-call and is ignored.
    """
    if prefetched and str(prefetched).startswith(("s3://", "gs://")):
        return str(prefetched)
    if prefetched:
        logger.warning("telemac: river_geometry_uri %r is not an object URI - ignoring",
                       prefetched)
    from trid3nt_server.workflows.lib import current_domain

    domain = current_domain()
    if domain is None or domain.bbox is None:
        raise TelemacDyeScenarioError(
            "TELEMAC_DYE_SCENARIO_ERROR",
            "the reach flowline cannot be fetched: no domain is bound.",
        )
    # OSM waterways are MAP CONTEXT, and the name has to say so. The river this
    # run models is the NLDI mainstem the mesh is cut from, which the canvas now
    # shows; an "Input: river geometry" row beside it read as though the solve
    # were built on the OSM line, which it never was.
    layer = await call_registry_tool(
        registry_fn("fetch_river_geometry"), bbox=tuple(domain.bbox),
        purpose="OSM waterways (map context; the modeled river is the NLDI centerline)")
    return layer_field(layer, "uri")


# --------------------------------------------------------------------------- #
# The river the reach is MESHED on: the NLDI mainstem, fetched here.
#
# The seed ladder, the centerline, the banks and the bed were six network calls
# inside the solver container. They are server tier for the reason every fetch is
# - a fetch changes if the box moves - and moving them buys the run four things a
# container fetch could never have: the emit-on-fetch input layer (so the meshed
# river IS the visible river), the read-through cache, the provenance record, and
# the router's retry doctrine. That last one is not a nicety here: the seed rungs
# used to fail OPEN, so a slow query silently meshed a DIFFERENT reach and nothing
# recorded which had happened.
# --------------------------------------------------------------------------- #

#: Basenames the manifest stages the reach's three inputs under. The worker READS
#: these names; nothing in the image knows where the bytes came from.
CENTERLINE_DEST: str = "river_centerline.geojson"
BANKS_DEST: str = "river_banks.geojson"
BED_DEST: str = "bed_source.tif"

#: Copernicus GLO-30 is a 1-arcsecond grid, so this is its OWN lattice. Asking
#: for it is what makes the staged raster carry the source pixels rather than a
#: resample of them, and therefore what makes the bed the worker fits identical
#: to the one it used to fetch for itself.
_GLO30_PX_PER_DEG: float = 3600.0

#: Envelope half-widths (deg) the seed rungs search, and the record caps they ask
#: for. Both are part of the QUESTION - a different tolerance finds a different
#: nearest vertex, and a different cap truncates a different set - so they are
#: pinned here rather than left to a fetcher default.
_NAMED_SEARCH_DEG: float = 0.15
_NAMED_MAX_RECORDS: int = 200
_MAINSTEM_SEARCH_DEG: float = 0.05
_MAINSTEM_MAX_RECORDS: int = 500
#: A name-free re-seed is bounded so a genuine small-creek study is never yanked
#: onto a distant river.
_MAINSTEM_MAX_RESEED_KM: float = 6.0

#: Pad (deg) around the fetched centerline for the two rasters/polygons the mesh
#: is built from. The bank pad must cover FAR channels behind mid-river islands;
#: the bed pad must cover the whole corridor the mesher can lay, whose widest
#: legal half-width is the bank sampler's 800 m ceiling (~0.008 deg).
_BANKS_PAD_DEG: float = 0.03
_BED_PAD_DEG: float = 0.02


def _tool(name: str) -> Any:
    return registry_fn(name)


def _read_vector_features(uri: str) -> list[dict[str, Any]]:
    """The GeoJSON features behind a fetched vector layer.

    The worker image carries shapely but no geopandas, so a FlatGeobuf cannot be
    opened there. The bytes are read once HERE and staged as GeoJSON, which the
    worker parses with the standard library - the same shape the NLDI and ArcGIS
    responses arrived in when it fetched them itself.
    """
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


def _stage_geojson(features: list[dict[str, Any]], *, run_tag: str,
                   dest: str) -> tuple[str, str]:
    """Stage a FeatureCollection; return its ``s3://`` URI and the bytes' digest.

    The digest is what makes "the same run twice" a checkable claim rather than
    an impression: the staged bytes ARE the geometry the worker meshes, so two
    runs with the same digest meshed the same river.
    """
    import hashlib
    import json
    import os

    from trid3nt_server.workflows.solver.solver import _get_s3_client

    cache_bucket = (os.environ.get("TRID3NT_CACHE_BUCKET") or "").strip()
    if not cache_bucket:
        raise TelemacDyeScenarioError(
            "TELEMAC_DYE_STAGING_FAILED",
            "TRID3NT_CACHE_BUCKET must be set to stage the reach inputs.")
    body = json.dumps({"type": "FeatureCollection", "features": features},
                      sort_keys=True).encode("utf-8")
    key = f"telemac/{run_tag}/{dest}"
    _get_s3_client().put_object(Bucket=cache_bucket, Key=key, Body=body,
                                ContentType="application/geo+json")
    return f"s3://{cache_bucket}/{key}", hashlib.sha256(body).hexdigest()


async def measure_bank_coverage(*, banks: Any, centerline: Any) -> Any:
    """MEASURE how much of the reach the fetched water polygons map -> the banks.

    The gate between the banks fetch and the section cut, so an unmapped reach
    fails on its own cause instead of arriving at the cut as an empty section. A
    pass-through: it hands back the banks it measured, which is what puts it in
    the chain rather than beside it.

    NO threshold. Zero coverage is the terminal refusal - none of this reach is
    polygon-mapped and no rung below can invent a shape for it. Anything above
    zero PROCEEDS with the measured fraction journalled, because the pieces NHD
    maps only as flowlines are exactly the ones a reader would otherwise assume
    were modelled.
    """
    fraction = await asyncio.to_thread(_covered_fraction, banks, centerline)
    if fraction <= 0.0:
        raise ReachBanksUnmapped()
    journal_note(
        f"reach banks: {fraction:.1%} of the modelled centreline is covered by "
        "mapped water polygons; any stretch NHD maps only as a flowline carries "
        "no surveyed width and is not in the domain this run solved over.")
    return banks


def _covered_fraction(banks: Any, centerline: Any) -> float:
    """Fraction of the centreline's LENGTH that lies inside the water polygons.

    Measured in metres on the reach's own UTM zone: a fraction of a degree-space
    length would weight the two axes differently and read as coverage that
    depends on latitude.
    """
    from pyproj import Transformer
    from shapely.geometry import shape
    from shapely.ops import transform as _transform, unary_union

    from trid3nt_server.tools.processing._geometry_common import (
        source_uri, utm_epsg_for,
    )

    lines = [shape(f["geometry"]) for f in
             _read_vector_features(str(source_uri(centerline)))
             if (f.get("geometry") or {}).get("type") in
             ("LineString", "MultiLineString")]
    if not lines:
        raise TelemacDyeScenarioError(
            "TELEMAC_DYE_SCENARIO_ERROR",
            "the reach centreline carries no line geometry, so how much of it is "
            "polygon-mapped cannot be measured.")
    line = unary_union(lines)
    polys = [shape(f["geometry"]).buffer(0) for f in
             _read_vector_features(str(source_uri(banks)))
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


def _feature_vertices(feature: dict[str, Any]) -> list[list[float]]:
    geom = feature.get("geometry") or {}
    kind = geom.get("type")
    if kind == "LineString":
        return list(geom.get("coordinates") or [])
    if kind == "MultiLineString":
        return [v for line in (geom.get("coordinates") or []) for v in (line or [])]
    return []


def _nearest_vertex(features: list[dict[str, Any]], lon: float,
                    lat: float) -> tuple[float, float] | None:
    best, best_d2 = None, float("inf")
    for feat in features:
        for v in _feature_vertices(feat):
            d2 = (v[0] - lon) ** 2 + (v[1] - lat) ** 2
            if d2 < best_d2:
                best_d2, best = d2, (float(v[0]), float(v[1]))
    return best


async def _named_seed(name: str, lon: float, lat: float) -> tuple[float, float] | None:
    """Nearest vertex of the NAMED GNIS flowline to ``(lon, lat)``, or ``None``.

    ``None`` means the name matched nothing in the window - a real answer about
    the reach, and the caller keeps the raw position seed. A FETCH FAILURE is not
    that answer and no longer pretends to be one: it raises, because a run that
    silently meshes a different river than the one it was asked for cannot be
    compared against its own previous run.
    """
    features = await asyncio.to_thread(lambda: _read_vector_features(str(
        layer_field(_tool("fetch_nhdplus_hr_flowlines")(
            bbox=[round(lon - _NAMED_SEARCH_DEG, 6), round(lat - _NAMED_SEARCH_DEG, 6),
                  round(lon + _NAMED_SEARCH_DEG, 6), round(lat + _NAMED_SEARCH_DEG, 6)],
            gnis_name=str(name).strip(), max_records=_NAMED_MAX_RECORDS,
            # A seed PROBE, not an input: what this layer contributes to the run
            # is one vertex, and painting the whole named watercourse beside the
            # centerline that was cut from it says the run models both.
            visualize=False), "uri"))))
    return _nearest_vertex(features, lon, lat)


async def _mainstem_seed(lon: float, lat: float) -> tuple[float, float] | None:
    """Re-seed a NAME-FREE reach onto the dominant nearby mainstem, or ``None``.

    With no ``river_name`` to disambiguate, a bare position snap lands on whatever
    channel is geometrically nearest, and at a confluence that is often a short
    low-order tributary stub. This prefers the highest ``streamorde`` channel, tie
    broken by total upstream drainage then proximity - but ONLY when that mainstem
    STRICTLY outranks the nearest flowline and its nearest vertex is within the
    re-seed radius. ``None`` means no improvement was found, which is a decision;
    a fetch failure raises instead of impersonating one.
    """
    features = await asyncio.to_thread(lambda: _read_vector_features(str(
        layer_field(_tool("fetch_nhdplus_hr_flowlines")(
            bbox=[round(lon - _MAINSTEM_SEARCH_DEG, 6), round(lat - _MAINSTEM_SEARCH_DEG, 6),
                  round(lon + _MAINSTEM_SEARCH_DEG, 6), round(lat + _MAINSTEM_SEARCH_DEG, 6)],
            max_records=_MAINSTEM_MAX_RECORDS,
            visualize=False), "uri"))))
    cands: list[tuple[int, float, float, tuple[float, float]]] = []
    for feat in features:
        vertex = _nearest_vertex([feat], lon, lat)
        if vertex is None:
            continue
        props = feat.get("properties") or {}
        cands.append((int(props.get("streamorde") or 0),
                      float(props.get("totdasqkm") or 0.0),
                      ((vertex[0] - lon) ** 2 + (vertex[1] - lat) ** 2) ** 0.5,
                      vertex))
    if not cands:
        return None
    nearest = min(cands, key=lambda c: c[2])
    mainstem = max(cands, key=lambda c: (c[0], c[1], -c[2]))
    reseed_km = mainstem[2] * 111.0
    if mainstem[0] <= nearest[0] or reseed_km > _MAINSTEM_MAX_RESEED_KM:
        return None
    logger.info("telemac reach: mainstem re-seed - nearest order %d vs mainstem "
                "order %d (drainage %.0f km2) at %.2f km",
                nearest[0], mainstem[0], mainstem[1], reseed_km)
    return mainstem[3]


async def resolve_reach_seed_point(*, reach: dict[str, Any], seed: dict[str, Any],
                                   release: tuple[float, float] | None,
                                   ) -> dict[str, Any]:
    """The point the centerline is resolved from, and WHICH RUNG chose it.

    Three rungs, tried in a fixed order, and the choice is RECORDED rather than
    inferable only from a log line. That is the whole repair: the ladder used to
    fail open, so a slow NHDPlus query kept the raw seed, meshed a different
    reach, and left nothing in the run saying so - two identical invocations could
    produce two different rivers and the record could not tell them apart.
    """
    if release is not None:
        lon, lat, rung = float(release[0]), float(release[1]), "release-position"
    else:
        lon, lat, rung = float(seed["lon"]), float(seed["lat"]), "position"
    name = str(reach.get("river_name") or "").strip()
    if name:
        named = await _named_seed(name, lon, lat)
        if named is not None:
            logger.info("telemac reach: named-flowline re-seed %r (%.5f,%.5f) -> "
                        "(%.5f,%.5f)", name, lon, lat, named[0], named[1])
            return {"lon": named[0], "lat": named[1],
                    "rung": f"{rung}-named-flowline", "river_name": name}
        logger.info("telemac reach: %r matched no NHDPlus HR flowline within "
                    "%.2f deg - the raw seed stands", name, _NAMED_SEARCH_DEG)
        return {"lon": lon, "lat": lat, "rung": f"{rung}-named-flowline-absent",
                "river_name": name}
    main = await _mainstem_seed(lon, lat)
    if main is not None:
        return {"lon": main[0], "lat": main[1], "rung": f"{rung}-mainstem",
                "river_name": ""}
    return {"lon": lon, "lat": lat, "rung": f"{rung}-nearest-flowline",
            "river_name": ""}


def _lonlat_extent(features: list[dict[str, Any]]) -> tuple[float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []
    for feat in features:
        for v in _feature_vertices(feat):
            xs.append(float(v[0]))
            ys.append(float(v[1]))
    if not xs:
        raise TelemacDyeScenarioError(
            "TELEMAC_DYE_SCENARIO_ERROR",
            "the fetched reach centerline carries no vertices; there is no reach "
            "to mesh.")
    return min(xs), min(ys), max(xs), max(ys)


async def resolve_reach_river(*, reach: dict[str, Any], seed: dict[str, Any],
                              run_tag: str, reach_length_km: float,
                              release: tuple[float, float] | None = None,
                              nav_direction: str = "DM",
                              with_bed: bool = True) -> dict[str, Any]:
    """Fetch the river the reach is meshed on and stage it into the run directory.

    Returns the manifest ``inputs`` rows plus the provenance the run records: the
    seed rung, the navigated COMIDs and a digest of the centerline itself. The
    digest is the determinism test made checkable - two invocations that produce
    the same digest meshed the same river, and no reader has to take that on
    faith.

    ``with_bed`` is False for the mesh PREVIEW, which builds geometry and stops:
    a bed it never samples is a fetch nobody needs.
    """
    seed_point = await resolve_reach_seed_point(reach=reach, seed=seed,
                                                release=release)
    centerline_layer = await call_registry_tool(
        _tool("fetch_nhdplus_nldi_navigate"),
        seed_point=[round(seed_point["lon"], 6), round(seed_point["lat"], 6)],
        direction=str(nav_direction), distance_km=float(reach_length_km),
        purpose="the modeled river centerline")
    centerline_uri = layer_field(centerline_layer, "uri")
    features = await asyncio.to_thread(_read_vector_features, str(centerline_uri))
    if not features:
        raise TelemacDyeScenarioError(
            "TELEMAC_DYE_SCENARIO_ERROR",
            f"the NLDI navigate from ({seed_point['lon']:.5f},{seed_point['lat']:.5f}) "
            f"returned no flowlines for {reach_length_km:g} km {nav_direction}; "
            "there is no reach to mesh.")
    centerline_stage, centerline_sha = await asyncio.to_thread(
        _stage_geojson, features, run_tag=run_tag, dest=CENTERLINE_DEST)
    inputs = [{"gs_uri": centerline_stage, "dest": CENTERLINE_DEST}]

    min_lon, min_lat, max_lon, max_lat = _lonlat_extent(features)
    provenance: dict[str, Any] = {
        "seed_lon": round(seed_point["lon"], 6),
        "seed_lat": round(seed_point["lat"], 6),
        "seed_rung": seed_point["rung"],
        "centerline_uri": str(centerline_uri),
        "centerline_sha256": centerline_sha,
        "centerline_comids": sorted(
            int(c) for c in {(f.get("properties") or {}).get("nhdplus_comid")
                             for f in features} if c is not None),
        "centerline_extent": [round(min_lon, 6), round(min_lat, 6),
                              round(max_lon, 6), round(max_lat, 6)],
        "banks_uri": None,
        "bed_uri": None,
        "bed_source": None,
    }

    banks_layer = await call_registry_tool(
        _tool("fetch_nhd_area_water"),
        bbox=[round(min_lon - _BANKS_PAD_DEG, 6), round(min_lat - _BANKS_PAD_DEG, 6),
              round(max_lon + _BANKS_PAD_DEG, 6), round(max_lat + _BANKS_PAD_DEG, 6)],
        max_records=200,
        purpose="the river banks")
    bank_features = await asyncio.to_thread(
        _read_vector_features, str(layer_field(banks_layer, "uri")))
    # An EMPTY bank layer is staged as an empty collection rather than left out:
    # "no NHDArea polygon covers this reach" is the answer the unmapped-reach
    # refusal is built to raise, and a MISSING file would read as a staging failure.
    banks_stage, _sha = await asyncio.to_thread(
        _stage_geojson, bank_features, run_tag=run_tag, dest=BANKS_DEST)
    inputs.append({"gs_uri": banks_stage, "dest": BANKS_DEST})
    provenance["banks_uri"] = str(layer_field(banks_layer, "uri"))
    provenance["banks_features"] = len(bank_features)

    if with_bed:
        bed = await resolve_reach_bed(
            bbox=[round(min_lon - _BED_PAD_DEG, 6), round(min_lat - _BED_PAD_DEG, 6),
                  round(max_lon + _BED_PAD_DEG, 6), round(max_lat + _BED_PAD_DEG, 6)])
        inputs.append({"gs_uri": bed["uri"], "dest": BED_DEST})
        provenance["bed_uri"] = bed["uri"]
        provenance["bed_source"] = bed["source"]
        if bed.get("fallback_reason"):
            provenance["bed_fallback_reason"] = bed["fallback_reason"]
    return {"inputs": inputs, "provenance": provenance,
            "seed_lon": seed_point["lon"], "seed_lat": seed_point["lat"]}


async def resolve_reach_bed(*, bbox: list[float]) -> dict[str, Any]:
    """The terrain the reach bed is fitted from, with its ladder declared.

    Copernicus GLO-30 is the PRIMARY rung and 3DEP the fallback, which is the
    order the worker's own ladder ran in: the reach bed is a robust along-channel
    trend rather than a per-node elevation, so a globally uniform 30 m surface is
    the better-behaved input and the finer US lidar is what covers for it.

    The Copernicus rung asks for the source's OWN 1-arcsecond lattice, which is
    what makes the staged raster carry the GLO-30 pixels rather than a resample
    of them. A fall to 3DEP is a CROSS-DATASET substitution, so the reason rides
    on the returned record where every consumer reads it.
    """
    try:
        layer = await asyncio.to_thread(
            lambda: _tool("fetch_copernicus_dem")(
                bbox=list(bbox), px_per_deg=_GLO30_PX_PER_DEG,
                purpose="river bed elevation"))
        return {"uri": str(layer_field(layer, "uri")), "source": "cop-dem-glo-30"}
    except Exception as exc:  # noqa: BLE001 -- a LOUD cross-dataset fallback
        reason = f"{type(exc).__name__}: {exc}"
        code = getattr(exc, "error_code", None)
        if code:
            reason = f"{code} ({reason})"
        logger.warning("telemac reach bed: Copernicus GLO-30 unavailable for "
                       "bbox=%s (%s); falling back to USGS 3DEP", bbox, reason)
    layer = await asyncio.to_thread(
        lambda: _tool("fetch_dem")(
            bbox=list(bbox), source="3dep", resolution_m=30,
            purpose="river bed elevation"))
    return {"uri": str(layer_field(layer, "uri")), "source": "usgs-3dep",
            "fallback_reason": reason}


async def reach_seed(*, reach: dict[str, Any], rivers: str | None) -> dict[str, Any]:
    """The mid-reach seed the worker resolves the centerline from.

    The largest fetched flowline's midpoint when there is one, else the geocoded
    centroid - which the worker NLDI-snaps to the nearest flowline COMID anyway,
    so the degrade is honest rather than a dead end.
    """
    seed = None
    if rivers:
        seed = await asyncio.to_thread(river_seed_from_geometry, str(rivers))
    if seed is None:
        return {"lon": float(reach["lon"]), "lat": float(reach["lat"]),
                "source": "geocoded-centroid (NLDI will snap to the nearest flowline)"}
    return {"lon": seed[0], "lat": seed[1],
            "source": "mid-reach point on the largest fetched flowline"}


class Geocode:
    """Reach-AOI resolution steps."""

    @staticmethod
    def reach(location: Any, bbox: Any) -> Step:
        """Place/AOI -> the reach centre. Refines the domain for everything after it."""
        return Step(runner=f"{_STEPS}.reach.geocode_reach", stage="acquire",
                    kwargs={"location": location, "bbox": bbox}).overrides_domain()


def ReachSeed(*, reach: Any, rivers: Any) -> Step:  # noqa: N802 - a value constructor
    """The mid-reach seed on the fetched flowline."""
    return Step(runner=f"{_STEPS}.reach.reach_seed", stage="acquire",
                kwargs={"reach": reach, "rivers": rivers})
