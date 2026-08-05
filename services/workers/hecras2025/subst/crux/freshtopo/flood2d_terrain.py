#!/usr/bin/env python3
"""Fresh-AOI terrain preparation for the hecras_flood_2d authoring stage.

Turns a fetched EPSG:4326 DEM (metres, from ``fetch_dem`` seam-1) into the
US-Customary inputs the HEC-RAS 2025 authoring worker + the production 6.6
solver need: a terrain GeoTIFF in a LOCAL ftUS planar CRS with elevations in US
survey feet, a constant Manning-n GeoTIFF, and the mesh seeds
(``perimeter_ccw_open.f64`` + ``centers.f64``) on the same planar grid.

The planar CRS is a per-AOI custom Transverse Mercator centred on the AOI in US
survey feet (``+proj=tmerc ... +units=us-ft``) so the deck is unit-consistent
with the Muncie US-Customary solver ANYWHERE in the US -- no State-Plane zone
lookup, locally conformal over a few-km AOI. The same WKT rides through the deck
composer (projection override) so the depth COG geolocates correctly.

Pure rasterio/pyproj/numpy (already in the agent venv + the solver image). No
server import; callable as a library (the template composer) or a CLI (proofs).
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np

#: US survey foot per metre (exact) -- HEC US-Customary decks are ftUS.
_M_TO_USFT = 3937.0 / 1200.0


def local_usft_crs_wkt(clon: float, clat: float) -> str:
    """A local Transverse-Mercator CRS in US survey feet centred on (clon, clat).

    NAD83 datum, k=1, false-easting/northing 0. Locally conformal over a few-km
    AOI; units US survey feet so mesh coords + terrain elevations share one ftUS
    system with the Muncie solver.
    """
    from pyproj import CRS

    proj4 = (
        f"+proj=tmerc +lat_0={clat:.8f} +lon_0={clon:.8f} +k=1 "
        f"+x_0=0 +y_0=0 +datum=NAD83 +units=us-ft +no_defs"
    )
    return CRS.from_proj4(proj4).to_wkt()


@dataclass
class TerrainPrep:
    """Everything the authoring container + composer need for one AOI."""

    terrain_tif: str
    nvalue_tif: str
    perimeter_f64: str
    centers_f64: str
    crs_wkt: str
    n_seeds: int
    n_perim: int
    resolution_ft: float
    terrain_min_ft: float
    terrain_max_ft: float
    width_ft: float
    height_ft: float
    bbox4326: list


def prepare_terrain(
    dem_tif: str | Path,
    out_dir: str | Path,
    *,
    resolution_m: float = 60.0,
    manning_n: float = 0.06,
    margin_frac: float = 0.06,
) -> TerrainPrep:
    """Reproject ``dem_tif`` (4326, m) to a local ftUS grid + write mesh seeds.

    ``resolution_m`` is the target 2D cell size (metres, granularity-gated
    upstream); the seed grid is laid at that pitch converted to feet. A small
    ``margin_frac`` inset keeps the perimeter off the DEM edge (createterrain
    samples cleanly inside the coverage).
    """
    import rasterio
    from rasterio.warp import (
        calculate_default_transform, reproject, Resampling, transform_bounds,
    )

    dem_tif = Path(dem_tif)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    with rasterio.open(dem_tif) as src:
        b = src.bounds
        # AOI centre in lon/lat (the DEM may be in any projected CRS, e.g. the
        # 3DEP Albers EPSG:5070) -- the local ftUS CRS is centred there.
        ll = transform_bounds(src.crs, "EPSG:4326", *b)
        clon = 0.5 * (ll[0] + ll[2])
        clat = 0.5 * (ll[1] + ll[3])
        dst_wkt = local_usft_crs_wkt(clon, clat)
        res_ft = float(resolution_m) * _M_TO_USFT
        # Resample the DEM onto the ftUS grid at ~half the cell pitch so each 2D
        # cell integrates several posts (subgrid tables want sub-cell relief).
        dem_res_ft = max(res_ft / 3.0, 3.0 * _M_TO_USFT)
        transform, width, height = calculate_default_transform(
            src.crs, dst_wkt, src.width, src.height, *b, resolution=dem_res_ft
        )
        dst = np.full((height, width), np.nan, np.float32)
        reproject(
            source=rasterio.band(src, 1),
            destination=dst,
            src_transform=src.transform,
            src_crs=src.crs,
            dst_transform=transform,
            dst_crs=dst_wkt,
            resampling=Resampling.bilinear,
            dst_nodata=float("nan"),
        )

    # metres -> US survey feet (elevation).
    elev_ft = dst * _M_TO_USFT
    finite = np.isfinite(elev_ft)
    if not finite.any():
        raise ValueError("reprojected DEM has no finite elevation samples")
    tmin = float(np.nanmin(elev_ft))
    tmax = float(np.nanmax(elev_ft))
    # Reprojecting a projected DEM into the local ftUS grid leaves NaN in the
    # rotated-quad corners. The 2025 subgrid-table builder
    # (MeshPropertyTables.ComputeFrom / HydraulicProfile.Build) index-faults on a
    # cell that samples ANY NaN, so fill nodata with HIGH DRY GROUND (a few ft
    # above the max) -- those corner cells then read as dry high terrain (never
    # wetted) and the profile builder always sees a finite, monotone column.
    fill_ft = tmax + 50.0
    elev_ft = np.where(finite, elev_ft, np.float32(fill_ft)).astype(np.float32)

    terrain_tif = out_dir / "terrain.tif"
    nvalue_tif = out_dir / "nvalue.tif"
    prof = {
        "driver": "GTiff", "dtype": "float32", "count": 1,
        "width": width, "height": height, "crs": dst_wkt,
        "transform": transform, "nodata": None,
        "compress": "deflate",
    }
    import rasterio as _rio
    with _rio.open(terrain_tif, "w", **prof) as d:
        d.write(elev_ft.astype(np.float32), 1)
    nval = np.where(finite, np.float32(manning_n), np.float32(manning_n))
    with _rio.open(nvalue_tif, "w", **prof) as d:
        d.write(nval.astype(np.float32), 1)

    # Mesh extent (ftUS) from the FINITE-DATA envelope, not the full output
    # rectangle: reprojecting a projected DEM leaves nodata slivers in the
    # rotated-quad corners, and a perimeter over them would seat mesh cells on the
    # fill ground (polluting wse_max with the fill height). Bound the mesh to the
    # rows/cols that carry real terrain, then inset.
    rows = np.where(finite.any(axis=1))[0]
    cols = np.where(finite.any(axis=0))[0]
    r0, r1 = int(rows[0]), int(rows[-1]) + 1
    c0, c1 = int(cols[0]), int(cols[-1]) + 1
    gx0, gy0 = transform * (c0, r0)
    gx1, gy1 = transform * (c1, r1)
    minx, maxx = min(gx0, gx1), max(gx0, gx1)
    miny, maxy = min(gy0, gy1), max(gy0, gy1)
    dx = (maxx - minx) * margin_frac
    dy = (maxy - miny) * margin_frac
    px0, px1 = minx + dx, maxx - dx
    py0, py1 = miny + dy, maxy - dy

    # perimeter: rectangle, CCW, OPEN (drop the closing point) -- TryCreateMesh
    # requires CCW-open (ADR 0132 STEP 2). Sample edges so the mesh boundary is
    # smooth; keep it modest (a rectangle needs only corners, but a few points per
    # edge helps the tessellator).
    def _edge(a, b, n):
        return [(a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
                for t in np.linspace(0.0, 1.0, n, endpoint=False)]
    corners = [(px0, py0), (px1, py0), (px1, py1), (px0, py1)]  # CCW
    per_edge = 12
    perim = []
    for i in range(4):
        perim += _edge(corners[i], corners[(i + 1) % 4], per_edge)
    perim_arr = np.asarray(perim, np.float64)

    # cell-center seeds: regular grid at res_ft inside the perimeter
    nx = max(int((px1 - px0) / res_ft), 3)
    ny = max(int((py1 - py0) / res_ft), 3)
    xs = px0 + (np.arange(nx) + 0.5) * (px1 - px0) / nx
    ys = py0 + (np.arange(ny) + 0.5) * (py1 - py0) / ny
    gx, gy = np.meshgrid(xs, ys)
    centers = np.column_stack([gx.ravel(), gy.ravel()]).astype(np.float64)

    perimeter_f64 = out_dir / "perimeter_ccw_open.f64"
    centers_f64 = out_dir / "centers.f64"
    perim_arr.tofile(perimeter_f64)
    centers.tofile(centers_f64)

    prep = TerrainPrep(
        terrain_tif=str(terrain_tif), nvalue_tif=str(nvalue_tif),
        perimeter_f64=str(perimeter_f64), centers_f64=str(centers_f64),
        crs_wkt=dst_wkt, n_seeds=int(centers.shape[0]), n_perim=int(perim_arr.shape[0]),
        resolution_ft=res_ft, terrain_min_ft=tmin, terrain_max_ft=tmax,
        width_ft=float(maxx - minx), height_ft=float(maxy - miny),
        bbox4326=[ll[0], ll[1], ll[2], ll[3]],
    )
    (out_dir / "terrain_prep.json").write_text(json.dumps(asdict(prep), indent=2))
    return prep


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dem_tif")
    ap.add_argument("out_dir")
    ap.add_argument("--resolution-m", type=float, default=60.0)
    args = ap.parse_args()
    prep = prepare_terrain(args.dem_tif, args.out_dir, resolution_m=args.resolution_m)
    print(json.dumps(asdict(prep), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
