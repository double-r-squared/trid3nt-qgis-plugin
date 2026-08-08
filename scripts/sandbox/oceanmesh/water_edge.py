"""ADR 0194 -- high-res coastal water-edge builder (STANDALONE sandbox).

Builds a REAL coastal WATER polygon to replace the coarse GSHHG-intermediate
shoreline. The water polygon is handed to the custom-SDF coastal mesher
(_mesh_water_edge_incontainer.py), which meshes its interior with the exact
edge preserved -- aligned to the true shoreline, never cookie-cut by the AOI box.

Water-domain sources:
  * open coast / bay / estuary : OSM ``natural=coastline`` ways via Overpass
    (the repo's reliable OSM path; land-on-the-left / water-on-the-right OSM
    convention), polygonized against the domain box and classified into a water
    polygon. Optionally UNIONed with NHDPlus HR ``NHDArea`` (open-water areal
    features: SeaOcean, BayInlet, StreamRiver-area) + ``NHDWaterbody`` polygons
    that connect to it, so river arms the coastline generalizes over are added.
    NOAA CUSP is the production-grade coastal source but has NO public queryable
    ArcGIS vector layer (download-only via the Shoreline Data Explorer -- see
    docs/research/oceanmesh-front-proposal.md), so OSM coastline is the landed
    high-res source.
  * river valley : the watershed catchment (pysheds delineate_watershed) with
    NHDPlus HR / OSM flowlines (fetch_river_geometry) buffered into a river
    corridor clipped to the catchment.

Coastline -> water-polygon method (osmcoastline-style, self-contained):
  polygonize(domain-box boundary + coastline ways) splits the box into faces;
  each face is classified LAND vs WATER by the OSM coastline direction
  (water lies to the RIGHT of a way's travel direction). WATER = union of the
  water faces (islands survive automatically as holes).

The geometry hygiene (make_valid -> buffer(0) -> set_precision) mirrors the
original stage_shoreline so oceanmesh's shoreline classifier does not hit GEOS
side-location conflicts.
"""

from __future__ import annotations

import json
import logging
import math
import time
import urllib.parse
import urllib.request
from pathlib import Path

import geopandas as gpd
import shapely
from shapely.geometry import LineString, Point, box, shape
from shapely.ops import polygonize, unary_union
from shapely.validation import make_valid

log = logging.getLogger("water_edge")

# --------------------------------------------------------------------------- #
# Overpass etiquette (mirrors the repo's fetch_river_geometry / fetch_buildings
# path: 3 public mirrors tried in order, UA header, POST QL in the `data` field,
# small backoff on failure -- the data-source-fallback norm).
# --------------------------------------------------------------------------- #
_OVERPASS_MIRRORS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
_UA = (
    "trid3nt/0.1 (Hazard Modeling Agent; coastal-water-edge sandbox; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)


def _overpass_post(ql: str, timeout: float = 180.0) -> tuple[dict, str]:
    """POST an Overpass QL across the mirror chain; first success wins."""
    last: Exception | None = None
    for url in _OVERPASS_MIRRORS:
        for attempt in range(3):
            try:
                body = urllib.parse.urlencode({"data": ql}).encode("utf-8")
                req = urllib.request.Request(url, data=body, headers={"User-Agent": _UA})
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return json.loads(resp.read().decode("utf-8")), url
            except Exception as exc:  # noqa: BLE001 -- fall through the mirror chain
                last = exc
                log.warning("overpass mirror %s attempt %d failed: %s", url, attempt, exc)
                time.sleep(min(2.0 ** attempt, 8.0))
    raise RuntimeError(f"all Overpass mirrors failed: {last}")


def overpass_coastline_lines(bbox) -> tuple[list[LineString], dict]:
    """OSM ``natural=coastline`` ways in ``bbox`` as directed LineStrings.

    Directions carry the OSM invariant (land on the left, water on the right).
    """
    xmin, ymin, xmax, ymax = (float(v) for v in bbox)
    ql = (
        f"[out:json][timeout:180];"
        f'(way["natural"="coastline"]({ymin},{xmin},{ymax},{xmax}););'
        f"out geom;"
    )
    payload, mirror = _overpass_post(ql)
    lines: list[LineString] = []
    for el in payload.get("elements", []):
        if not isinstance(el, dict) or el.get("type") != "way":
            continue
        geom = el.get("geometry") or []
        coords = [(p["lon"], p["lat"]) for p in geom if "lon" in p and "lat" in p]
        if len(coords) >= 2:
            lines.append(LineString(coords))
    return lines, {"source": "osm_coastline", "mirror": mirror, "n_ways": len(lines)}


def _clean(geom):
    if geom is None or geom.is_empty:
        return None
    g = make_valid(geom) if not geom.is_valid else geom
    g = shapely.set_precision(g.buffer(0), 1e-8)
    return None if g.is_empty else g


def _offsets(a, b, eps):
    """Midpoint offset points to the right and left of a directed segment a->b."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy)
    if length == 0.0:
        return None
    mx, my = 0.5 * (a[0] + b[0]), 0.5 * (a[1] + b[1])
    rnx, rny = dy / length, -dx / length  # right normal (OSM water side)
    return (mx + eps * rnx, my + eps * rny), (mx - eps * rnx, my - eps * rny)


def coastline_water_polygon(bbox):
    """Water polygon for ``bbox`` from OSM coastline (osmcoastline-style faces).

    Returns ``(water_polygon_or_None, provenance)``. WATER is the union of
    polygonized faces whose interior lies on the RIGHT of the coastline ways
    (barrier islands / land survive as holes). If no coastline intersects the
    box, returns ``(None, ...)`` and the caller falls back to NHD water.
    """
    lines, prov = overpass_coastline_lines(bbox)
    xmin, ymin, xmax, ymax = (float(v) for v in bbox)
    dom = box(xmin, ymin, xmax, ymax)

    clipped: list[LineString] = []
    for ln in lines:
        c = ln.intersection(dom)
        if c.is_empty:
            continue
        if c.geom_type == "LineString":
            clipped.append(c)
        elif c.geom_type == "MultiLineString":
            clipped.extend([g for g in c.geoms if g.geom_type == "LineString"])
    prov["n_clipped"] = len(clipped)
    if not clipped:
        prov["classified"] = "none (no coastline in domain)"
        return None, prov

    # Directed segments retain the OSM orientation (union-noding would lose it).
    dsegs = []
    for ln in clipped:
        cs = list(ln.coords)
        dsegs.extend(zip(cs[:-1], cs[1:]))

    boundary = LineString(list(dom.exterior.coords))
    noded = unary_union([*clipped, boundary])
    faces = [p for p in polygonize(noded) if p.area > 0]
    prov["n_faces"] = len(faces)
    if not faces:
        prov["classified"] = "no faces polygonized"
        return None, prov

    # Vote each face WATER/LAND by locating each directed segment's right/left
    # offset point in the face index (STRtree): the OSM side the point lands on
    # is a vote for that face. O(segments * log faces).
    from collections import Counter

    from shapely import STRtree

    tree = STRtree(faces)
    span = min(xmax - xmin, ymax - ymin)
    eps = max(span * 1e-4, 5e-6)  # ~metres-scale offset, safely inside a face
    water_votes: Counter = Counter()
    land_votes: Counter = Counter()

    def _locate(pt) -> int:
        p = Point(pt)
        for idx in tree.query(p):
            if faces[int(idx)].contains(p):
                return int(idx)
        return -1

    for a, bpt in dsegs:
        off = _offsets(a, bpt, eps)
        if off is None:
            continue
        right_pt, left_pt = off
        ri = _locate(right_pt)
        if ri >= 0:
            water_votes[ri] += 1
        li = _locate(left_pt)
        if li >= 0:
            land_votes[li] += 1

    water_faces = []
    n_water = n_land = n_undecided = 0
    for i, face in enumerate(faces):
        w, la = water_votes.get(i, 0), land_votes.get(i, 0)
        if w > la:
            water_faces.append(face)
            n_water += 1
        elif la > w:
            n_land += 1
        else:
            n_undecided += 1
    prov.update(water_faces=n_water, land_faces=n_land, undecided_faces=n_undecided)

    if not water_faces:
        prov["classified"] = "no water faces (check coastline orientation)"
        return None, prov
    water = _clean(unary_union(water_faces))
    prov["classified"] = "ok"
    prov["water_km2"] = (
        round(float(gpd.GeoSeries([water], crs=4326).to_crs(3857).area.iloc[0] / 1e6), 3)
        if water is not None else 0.0
    )
    return water, prov


# --------------------------------------------------------------------------- #
# NHDPlus HR areal water (best-effort union): NHDArea (layer 8, open-water areal
# features -- SeaOcean / BayInlet / StreamRiver-area) + NHDWaterbody (layer 9)
# polygons that CONNECT to the coastline water, adding river arms the coastline
# generalizes over. Never fails the build.
# --------------------------------------------------------------------------- #
_NHD_QUERY = "https://hydro.nationalmap.gov/arcgis/rest/services/NHDPlus_HR/MapServer/{layer}/query"


def _arcgis_polys(layer: int, bbox, timeout: float = 120.0) -> list:
    xmin, ymin, xmax, ymax = (float(v) for v in bbox)
    params = {
        "where": "1=1",
        "geometry": f"{xmin},{ymin},{xmax},{ymax}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "outSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "ftype",
        "returnGeometry": "true",
        "f": "geojson",
    }
    url = _NHD_QUERY.format(layer=layer) + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    polys = []
    for feat in payload.get("features", []):
        try:
            g = _clean(shape(feat["geometry"]))
        except Exception:  # noqa: BLE001
            continue
        if g is not None and g.geom_type in ("Polygon", "MultiPolygon"):
            polys.append(g)
    return polys


def nhd_water_union(bbox, connect_to=None) -> tuple[object, dict]:
    """NHDArea + NHDWaterbody polygons intersecting ``bbox`` (best-effort).

    If ``connect_to`` (the coastline water) is given, NHDWaterbody polygons are
    kept only when they touch it (adds connected river arms, not isolated ponds);
    NHDArea open-water features are always kept.
    """
    prov = {"nhd_area": 0, "nhd_waterbody_kept": 0, "error": None}
    try:
        area = _arcgis_polys(8, bbox)
        wb = _arcgis_polys(9, bbox)
    except Exception as exc:  # noqa: BLE001 -- NHD is best-effort
        prov["error"] = str(exc)
        return None, prov
    prov["nhd_area"] = len(area)
    keep = list(area)
    if connect_to is not None and not connect_to.is_empty:
        for g in wb:
            if g.intersects(connect_to):
                keep.append(g)
                prov["nhd_waterbody_kept"] += 1
    else:
        keep.extend(wb)
        prov["nhd_waterbody_kept"] = len(wb)
    if not keep:
        return None, prov
    return _clean(unary_union(keep)), prov


def noaa_cusp_probe(timeout: float = 30.0) -> dict:
    """Record NOAA CUSP ArcGIS reachability (documentation/provenance only).

    The canonical NGS Continually Updated Shoreline Product is distributed as
    downloadable shapefiles via the Shoreline Data Explorer; it has no public
    queryable ArcGIS vector layer. This confirms the charttools CUSP MapServer
    is absent so the verdict is evidence-backed.
    """
    url = "https://gis.charttools.noaa.gov/arcgis/rest/services/MCS/CUSP/MapServer?f=json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        if "error" in payload:
            return {"queryable_vector_layer": False,
                    "note": "MCS/CUSP/MapServer returns an error (service absent)"}
        return {"queryable_vector_layer": True, "layers": payload.get("layers", [])}
    except Exception as exc:  # noqa: BLE001
        return {"queryable_vector_layer": False, "note": f"unreachable: {exc}"}


def build_coastal_water(bbox, use_nhd: bool = True) -> tuple[object, dict]:
    """Assemble the final coastal WATER polygon for ``bbox``.

    OSM coastline water UNION (best-effort) connected NHDArea/NHDWaterbody. The
    result is valid and self-intersection-free (make_valid + set_precision).
    """
    water, cprov = coastline_water_polygon(bbox)
    prov = {"coastline": cprov, "nhd": None}
    parts = [water] if water is not None else []
    if use_nhd:
        nhd, nprov = nhd_water_union(bbox, connect_to=water)
        prov["nhd"] = nprov
        if nhd is not None:
            parts.append(nhd)
    if not parts:
        raise RuntimeError(f"no water polygon built for bbox={bbox} (no coastline, no NHD)")
    # Clip to the domain box: NHDArea features (SeaOcean/StreamRiver-area) can
    # extend far outside the AOI, which would balloon the mesh domain.
    xmin, ymin, xmax, ymax = (float(v) for v in bbox)
    water = _clean(unary_union(parts).intersection(box(xmin, ymin, xmax, ymax)))
    if water is None:
        raise RuntimeError("assembled water polygon is empty after cleaning")
    prov["water_km2"] = round(
        float(gpd.GeoSeries([water], crs=4326).to_crs(3857).area.iloc[0] / 1e6), 3
    )
    prov["valid"] = bool(water.is_valid)
    prov["water_bounds"] = [round(v, 5) for v in water.bounds]
    return water, prov


def river_corridor_water(flowlines_gdf, catchment_geom, buffer_m: float = 70.0):
    """Buffer flowlines into a corridor and clip to the catchment -> the water
    polygon to mesh (the river corridor / valley network of the watershed)."""
    lat_c = catchment_geom.centroid.y
    deg = buffer_m / (111_320.0 * max(0.15, math.cos(math.radians(lat_c))))
    lines = [g for g in flowlines_gdf.geometry if g is not None and not g.is_empty]
    if not lines:
        raise ValueError("no flowlines to build a corridor from")
    corridor = unary_union([ln.buffer(deg) for ln in lines])
    catch = _clean(catchment_geom)
    water = corridor.intersection(catch)
    water = _clean(water)
    if water is None:
        raise ValueError("river corridor does not intersect the catchment")
    return water


# --------------------------------------------------------------------------- #
# Standalone offline-first check: build + dump the water polygon for an AOI so
# it can be eyeballed (area, validity, GeoJSON) BEFORE meshing.
# --------------------------------------------------------------------------- #
def _main(argv=None) -> int:
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    aois = {
        "delaware_bay": (-75.60, 38.72, -74.82, 39.55),
        "tampa_bay": (-82.90, 27.48, -82.38, 28.06),
    }
    ap = argparse.ArgumentParser()
    ap.add_argument("--aoi", choices=list(aois) + ["all"], default="all")
    ap.add_argument("--out", default=str(Path(__file__).parent / "_runs"))
    ap.add_argument("--no-nhd", action="store_true")
    args = ap.parse_args(argv)

    print("NOAA CUSP probe:", json.dumps(noaa_cusp_probe()))
    names = list(aois) if args.aoi == "all" else [args.aoi]
    for name in names:
        bbox = aois[name]
        water, prov = build_coastal_water(bbox, use_nhd=not args.no_nhd)
        outdir = Path(args.out) / name
        outdir.mkdir(parents=True, exist_ok=True)
        gpd.GeoSeries([water], crs=4326).to_file(outdir / "water_edge.gpkg", driver="GPKG")
        (outdir / "water_edge_prov.json").write_text(json.dumps(prov, indent=2))
        print(f"=== {name} ===")
        print(json.dumps(prov, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
