"""Build small Slab2-format NetCDF grids in the exact on-disk layout the real Slab2
``.grd`` files use -- used BOTH by the offline parse/tiling tests and, at Cascadia
resolution, as the geometry that drives the live scenario proof when the ScienceBase
distribution is unreachable (it is Cloudflare-walled from the CI datacenter; the
production ``fetch_slab2_grids`` path is exercised by the monkeypatched fetch test).

The encoded geometry is grounded in the REAL Cascadia subduction interface: a trench
that BOWS west with latitude (convex to the ocean), a slab dipping ~11 deg ENE, depth
increasing eastward from the trench, and a strike that rotates through north across the
margin. The curvature is genuine -- that is what makes the tiled deformation track the
trench rather than render as a straight bar."""

from __future__ import annotations

import numpy as np


def cascadia_trench_lon(lat: np.ndarray | float) -> np.ndarray | float:
    """Trench longitude vs latitude for the real Cascadia margin (convex west):
    -124.5 at 40N bowing to ~-129 at 50N."""
    dl = np.asarray(lat, dtype=float) - 40.0
    return -124.5 - 0.35 * dl - 0.01 * dl * dl


def build_cascadia_slab2(
    lon_min: float = -130.0, lon_max: float = -120.0,
    lat_min: float = 39.0, lat_max: float = 51.0,
    d_deg: float = 0.1, store_lon_0_360: bool = True,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Return (lon, lat, depth_km, strike_deg, dip_deg) Slab2-layout arrays.

    depth_km is NEGATIVE down (slab-top depth); NaN west of the trench and east of the
    ~60 km down-dip edge (the ragged real edges Slab2 pads with NaN). lon is stored
    0..360 like the real grids when ``store_lon_0_360``."""
    lon = np.arange(lon_min, lon_max + d_deg / 2, d_deg)
    lat = np.arange(lat_min, lat_max + d_deg / 2, d_deg)
    LON, LAT = np.meshgrid(lon, lat)  # [lat, lon]

    trench = cascadia_trench_lon(LAT)
    # horizontal distance east of the trench in km (approx, cos(lat) corrected)
    east_km = (LON - trench) * 111.0 * np.cos(np.radians(LAT))
    dip0 = 11.0
    depth_km = -np.tan(np.radians(dip0)) * east_km  # 0 at trench, deeper eastward
    # NaN outside the modeled slab: west of trench (east_km<0) and beyond ~60 km depth
    mask = (east_km < 0.0) | (-depth_km > 60.0)
    depth_km = np.where(mask, np.nan, depth_km)

    strike = np.where(mask, np.nan, (1.6 * (LAT - 45.0)) % 360.0)  # ~352..008 thru N
    dip = np.where(mask, np.nan, dip0 + 0.06 * (-depth_km))  # steepens down-dip

    if store_lon_0_360:
        lon = np.where(lon < 0.0, lon + 360.0, lon)
        order = np.argsort(lon)
        lon = lon[order]
        depth_km = depth_km[:, order]
        strike = strike[:, order]
        dip = dip[:, order]
    return lon, lat, depth_km, strike, dip


def write_grd(path: str, lon: np.ndarray, lat: np.ndarray, z: np.ndarray,
              z_name: str = "z") -> str:
    """Write one GMT-NetCDF-style ``.grd`` (COARDS x/y/z) to ``path``."""
    import xarray as xr

    ds = xr.Dataset(
        {z_name: (("y", "x"), z.astype("float32"))},
        coords={"x": lon.astype("float64"), "y": lat.astype("float64")},
    )
    ds["x"].attrs["units"] = "degrees_east"
    ds["y"].attrs["units"] = "degrees_north"
    ds.to_netcdf(path)
    ds.close()
    return path


def write_cascadia_fixture(dirpath: str, code: str = "cas", **kw) -> dict[str, str]:
    """Write the three ``<code>_slab2_{dep,str,dip}.grd`` fixture grids into
    ``dirpath`` and return their paths."""
    import os

    lon, lat, dep, strike, dip = build_cascadia_slab2(**kw)
    os.makedirs(dirpath, exist_ok=True)
    out = {}
    out["dep"] = write_grd(os.path.join(dirpath, f"{code}_slab2_dep.grd"), lon, lat, dep)
    out["str"] = write_grd(os.path.join(dirpath, f"{code}_slab2_str.grd"), lon, lat, strike)
    out["dip"] = write_grd(os.path.join(dirpath, f"{code}_slab2_dip.grd"), lon, lat, dip)
    return out
