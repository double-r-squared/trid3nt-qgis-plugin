"""Ball Creek fork identification -- DEM flow-network exploration.

The EDI EML for the Ball Creek weir #9 record carries only the whole-basin
bounding box (CWTBASIN 21.85 km2), not the weir point. This driver locates the
weir's pour point EMPIRICALLY from the conditioned DEM: it finds the basin outlet
(max flow accumulation), walks the main stem upstream to the Ball Creek / Shope
Fork confluence, and delineates each fork so the fork whose area is ~half the
basin (the Ball Creek fork) can be identified and its pour point pinned.

Copernicus GLO-30 (natively geographic EPSG:4326) is staged for delineation --
the same DEM the mesh-acquisition delineator uses. Run in the agent venv with
.env.local sourced. ASCII only.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

# CWTBASIN bounding box (EML) + a margin so the closed catchment is never clipped.
BASIN_BBOX = (-83.4785, 35.0273, -83.4217, 35.0738)
MARGIN = 0.010
BBOX = (BASIN_BBOX[0] - MARGIN, BASIN_BBOX[1] - MARGIN,
        BASIN_BBOX[2] + MARGIN, BASIN_BBOX[3] + MARGIN)
RUNDIR = Path("/tmp/ballcreek_explore")

# ESRI/pysheds default dirmap: code -> (drow, dcol) movement of the flow.
DIRMOVE = {1: (0, 1), 2: (1, 1), 4: (1, 0), 8: (1, -1),
           16: (0, -1), 32: (-1, -1), 64: (-1, 0), 128: (-1, 1)}
# neighbor offset -> the fdir code the neighbor must carry to flow INTO center.
INFLOW_CODE = {(-dr, -dc): code for code, (dr, dc) in DIRMOVE.items()}


def _inflow_neighbors(fdir, acc, r, c):
    """Neighbors of (r,c) that flow into it, sorted by accumulation desc."""
    out = []
    H, W = fdir.shape
    for (nr, nc), code in INFLOW_CODE.items():
        rr, cc = r + nr, c + nc
        if 0 <= rr < H and 0 <= cc < W and int(fdir[rr, cc]) == code:
            out.append((float(acc[rr, cc]), rr, cc))
    out.sort(reverse=True)
    return out


def main() -> None:
    from trid3nt_server.data.processing._hydrology_common import (
        _condition_dem, _stage_dem, snap_and_delineate_index_space)
    import geopandas as gpd

    RUNDIR.mkdir(parents=True, exist_ok=True)
    dem_path = _stage_dem(BBOX, None, str(RUNDIR), [])
    grid, fdir_r, acc_r = _condition_dem(dem_path)
    fdir = np.asarray(fdir_r)   # plain-array views for the inflow walk
    acc = np.asarray(acc_r)
    affine = grid.affine
    print(f"DEM grid {acc.shape}  cellsize~{abs(affine.a)*111320:.1f} m")

    def area_km2(lon, lat):
        _m, poly, snap, cells = snap_and_delineate_index_space(
            grid, fdir_r, acc_r, lon, lat, snap_search_cells=10)
        if poly is None:
            return None, snap, 0, cells
        a = float(gpd.GeoSeries([poly], crs=4326).to_crs(6933).area.iloc[0] / 1e6)
        return a, snap, a, cells

    # 1. basin outlet = global max-accumulation cell.
    r_out, c_out = np.unravel_index(int(np.argmax(acc)), acc.shape)
    x_out, y_out = affine * (c_out + 0.5, r_out + 0.5)
    print(f"\n[outlet] max-acc cell=({r_out},{c_out}) acc={acc[r_out,c_out]:.0f} "
          f"lonlat=({x_out:.5f},{y_out:.5f})")
    a_basin, snap, _, cells = area_km2(x_out, y_out)
    print(f"[outlet] delineated area={a_basin:.2f} km2 cells={cells} "
          f"(documented CWTBASIN 21.85 km2)")

    # 2. walk the main stem upstream to the first major confluence.
    r, c = int(r_out), int(c_out)
    acc_out = float(acc[r, c])
    steps = 0
    confluence = None
    while steps < 5000:
        ins = _inflow_neighbors(fdir, acc, r, c)
        if not ins:
            break
        # a confluence: the top-2 inflows are both "major" (>= 25% basin acc).
        if len(ins) >= 2 and ins[1][0] >= 0.25 * acc_out:
            confluence = (r, c, ins)
            break
        r, c = ins[0][1], ins[0][2]  # follow the largest inflow up the main stem
        steps += 1
    if confluence is None:
        print("\n[confluence] none found with the 25% threshold; "
              "reporting the outlet's inflow structure.")
        confluence = (r_out, c_out, _inflow_neighbors(fdir, acc, r_out, c_out))

    cr, cc, ins = confluence
    xcf, ycf = affine * (cc + 0.5, cr + 0.5)
    print(f"\n[confluence] cell=({cr},{cc}) lonlat=({xcf:.5f},{ycf:.5f}) "
          f"after {steps} upstream steps; {len(ins)} inflows")
    for i, (a, rr, ccx) in enumerate(ins[:3]):
        xx, yy = affine * (ccx + 0.5, rr + 0.5)
        print(f"   inflow {i}: acc={a:.0f} lonlat=({xx:.5f},{yy:.5f})")

    # 3. delineate each of the top-2 forks DIRECTLY in index space (no lon/lat
    #    snap -- the two inflow cells are adjacent, so snapping merges them).
    from rasterio import features as rio_features
    from shapely.geometry import shape
    from shapely.ops import unary_union

    def delineate_index(rr, ccx):
        catch = grid.catchment(x=int(ccx), y=int(rr), fdir=fdir_r,
                               xytype="index", nodata_out=np.bool_(False))
        mask = np.asarray(catch, dtype=bool)
        geoms = [shape(g) for g, v in rio_features.shapes(
            mask.astype(np.uint8), mask=mask, transform=affine) if v == 1]
        poly = unary_union(geoms) if geoms else None
        area = (float(gpd.GeoSeries([poly], crs=4326).to_crs(6933).area.iloc[0] / 1e6)
                if poly is not None else 0.0)
        cent = poly.centroid if poly is not None else None
        return area, int(mask.sum()), poly, cent

    def walk_up(rr, ccx, n):
        for _ in range(n):
            nb = _inflow_neighbors(fdir, acc, rr, ccx)
            if not nb:
                break
            rr, ccx = nb[0][1], nb[0][2]
        return rr, ccx

    for i, (a, rr, ccx) in enumerate(ins[:2]):
        xx, yy = affine * (ccx + 0.5, rr + 0.5)
        af, cellsf, polyf, centf = delineate_index(rr, ccx)
        # walk 20 cells up this fork to see its channel-head direction.
        ur, uc = walk_up(rr, ccx, 20)
        ux, uy = affine * (uc + 0.5, ur + 0.5)
        lat_trend = "NORTH-heading" if uy > yy else "SOUTH-heading"
        print(f"\n[fork {i}] inflow lonlat=({xx:.5f},{yy:.5f}) acc={a:.0f}")
        print(f"   area={af:.2f} km2  cells={cellsf}  "
              f"centroid=({centf.x:.5f},{centf.y:.5f})")
        print(f"   20-cell-up channel point=({ux:.5f},{uy:.5f}) -> {lat_trend}")
        if polyf is not None:
            gpd.GeoDataFrame(geometry=[polyf], crs=4326).to_file(
                RUNDIR / f"fork_{i}.geojson", driver="GeoJSON")


if __name__ == "__main__":
    main()
