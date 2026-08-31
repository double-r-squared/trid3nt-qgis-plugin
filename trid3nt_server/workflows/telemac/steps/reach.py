"""The reach front of every TELEMAC river plan: geocode, seed, banks, mesh size.

Three declared steps and one declared Data producer:

* ``Geocode.reach`` - place/AOI to a reach centre, and it REBINDS THE DOMAIN, so
  every spatial producer after it reads the reach AOI implicitly.
* ``fetch_reach_flowline`` - the ``Data("rivers")`` producer (reference data:
  fetched fresh for the domain, never supplied). It is an OSM waterway layer and
  its job is to place the mid-reach seed; it is not the river the run models,
  and the canvas says so.
* ``ReachSeed`` - THE point the run's one centerline is navigated from, and where
  the carrier-discharge lookup queries. The template's ``DATA.centerline`` reads
  it, so the river the section cuts, the mesh holds and the canvas shows is the
  river every downstream reader means.
* ``measure_bank_coverage`` - how much of that centerline the fetched water
  polygons map, measured before the cut.
* ``measure_mesh_coverage`` - how much of it the ACCEPTED mesh holds, measured
  after the build. A distinct question with a distinct name: banks coverage is
  about what is mapped, mesh coverage about what is triangulated.

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

from .errors import (
    ReachBanksUnmapped,
    ReachMeshUncovered,
    TelemacDyeScenarioError,
)

logger = logging.getLogger("trid3nt_server.workflows.telemac.steps.reach")

__all__ = [
    "DEFAULT_RIVER_AOI_HALF_DEG",
    "Geocode",
    "MeshCoverage",
    "MESH_H_FLOOR_M",
    "MESH_NODE_CAP",
    "ReachSeed",
    "coerce_lonlat_point",
    "estimate_telemac_solve_seconds",
    "fetch_reach_flowline",
    "geocode_reach",
    "measure_bank_coverage",
    "measure_mesh_coverage",
    "reach_seed",
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
    fine, and the NLDI navigate snaps the locality seed to the nearest flowline
    anyway.
    """
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

    So the NLDI navigate starts on the main stem, not on a stray ditch. Returns
    None on ANY failure - the caller then falls back to the geocoded centroid,
    which the navigate snaps regardless.
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
    return {"lon": lon, "lat": lat, "name": name, "slug": slug(name),
            "bbox": bbox_around(lon, lat)}


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


async def measure_mesh_coverage(*, mesh: Any, centerline: Any) -> Any:
    """MEASURE how much of the reach the ACCEPTED mesh actually holds.

    Distinct from banks coverage: that one asks how much of the reach real water
    polygons MAP, this one asks how much of it the triangulation the solve runs on
    CONTAINS. A mesher handed a mapped polygon can still leave stretches out - a
    channel narrower than the requested edge does not resolve - and the answer a
    run publishes is about the stretch that was meshed, not the stretch that was
    asked for.

    A HEURISTIC, not a gate. Zero is terminal: none of the reach is in the domain,
    so the solve would answer about a different river. Anything above zero
    proceeds with the measured percent journalled, and what to do about it is the
    user's call - re-run finer, declare a sizing function, author the mesh. No
    automatic re-mesh: a resolution the run picked for itself is a decision the
    ask never made.
    """
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

    Summed over the cells the line touches rather than over their union: the
    triangulation tiles its domain without overlap, so the per-cell intersected
    lengths already add up to the length inside it, and no union of tens of
    thousands of triangles has to be built to learn that.
    """
    import numpy as np
    import shapely
    from shapely.geometry import LineString

    from trid3nt_server.workflows.mesh.shared.nodes import (
        read_accepted_mesh_nodes, read_centerline_utm,
    )

    utm_epsg = int(getattr(mesh.get("artifact"), "utm_epsg", 0) or 0)
    display_uri = str(mesh.get("display_uri") or "")
    if not display_uri or not utm_epsg:
        raise TelemacDyeScenarioError(
            "TELEMAC_DYE_SCENARIO_ERROR",
            "the accepted mesh carries no display face or no projected zone, so "
            "how much of the reach it holds cannot be measured.")
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

    A SUPPLIED point wins outright: naming where the substance enters the water is
    a statement about which stretch to model, and the navigate has to start there
    or the reach the user pinned is not the reach that gets meshed.

    Otherwise the largest fetched flowline's midpoint, else the geocoded centroid -
    which the NLDI navigate snaps to the nearest flowline COMID anyway, so the
    degrade is honest rather than a dead end.
    """
    point = coerce_lonlat_point(supplied, label="reach seed point")
    if point is not None:
        return {"lon": point[0], "lat": point[1],
                "source": "the supplied point the reach is seeded from"}
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


def ReachSeed(*, reach: Any, rivers: Any,  # noqa: N802 - a value constructor
              supplied: Any = None) -> Step:
    """The point the reach's one centerline is navigated from."""
    return Step(runner=f"{_STEPS}.reach.reach_seed", stage="acquire",
                kwargs={"reach": reach, "rivers": rivers, "supplied": supplied})


def MeshCoverage(*, mesh: Any, centerline: Any) -> Step:  # noqa: N802 - a value constructor
    """How much of the reach the accepted mesh holds, measured after the build."""
    return Step(runner=f"{_STEPS}.reach.measure_mesh_coverage", stage="mesh",
                kwargs={"mesh": mesh, "centerline": centerline})
