"""ADR 0193 Part B -- high-res water-edge shoreline builder (STANDALONE sandbox).

Replaces the coarse GSHHG-intermediate land polygons with a REAL water-edge
domain. oceanmesh meshes the WATER (the complement of the land polygons inside
the region bbox), so given a ``water`` polygon (a bay, an estuary, or a river
corridor) this writes the LAND shapefile ``land = domain_box.difference(water)``
that makes oceanmesh fill exactly that water body -- aligned to the true edge,
never cookie-cut by the AOI box.

Water-domain sources (documented per case in the driver):
  * estuary/bay  : OSM ``natural=coastline`` polygonized to the bay, and/or
                   NHDPlus HR waterbody/estuary polygons (fetch_nhd_waterbodies).
                   NOAA CUSP is the production-grade coastal source (see the
                   proposal doc) -- the OSM coastline is the implemented high-res
                   upgrade here.
  * river valley : the watershed catchment (pysheds delineate_watershed) with
                   NHDPlus HR / OSM flowlines (fetch_river_geometry) buffered
                   into a river corridor clipped to the catchment.

The geometry hygiene (make_valid -> buffer(0) -> set_precision) mirrors the
original stage_shoreline so oceanmesh's shoreline classifier does not hit GEOS
side-location conflicts.
"""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import shapely
from shapely.geometry import box
from shapely.ops import unary_union
from shapely.validation import make_valid


def _clean(geom):
    if geom is None or geom.is_empty:
        return None
    g = make_valid(geom) if not geom.is_valid else geom
    g = shapely.set_precision(g.buffer(0), 1e-7)
    return None if g.is_empty else g


def write_land_from_water(
    water_geom, domain_bbox, out_shp: Path, margin_frac: float = 0.08
) -> Path:
    """land = (domain_box + margin) - water. Writes ``out_shp`` (EPSG:4326).

    ``water_geom`` is a shapely (Multi)Polygon in EPSG:4326 covering the water
    body to mesh. ``domain_bbox`` = (minx,miny,maxx,maxy) framing it.
    """
    xmin, ymin, xmax, ymax = (float(v) for v in domain_bbox)
    mx = (xmax - xmin) * margin_frac
    my = (ymax - ymin) * margin_frac
    domain = box(xmin - mx, ymin - my, xmax + mx, ymax + my)

    water = _clean(water_geom)
    if water is None:
        raise ValueError("empty water polygon -- nothing to mesh")
    land = _clean(domain.difference(water))
    if land is None:
        raise ValueError("land polygon empty -- water covers the whole domain")

    polys = list(land.geoms) if land.geom_type == "MultiPolygon" else [land]
    gdf = gpd.GeoDataFrame(geometry=[p for p in polys if p and not p.is_empty],
                           crs="EPSG:4326")
    gdf = gdf[gdf.geometry.is_valid & ~gdf.geometry.is_empty]
    out_shp.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_file(out_shp)
    return out_shp


def river_corridor_water(flowlines_gdf, catchment_geom, buffer_m: float = 70.0):
    """Buffer flowlines into a corridor and clip to the catchment -> the water
    polygon to mesh (the river corridor / valley network of the watershed)."""
    lat_c = catchment_geom.centroid.y
    import math

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
