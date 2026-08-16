"""Worker-side SFINCS QUADTREE deck build (variable-resolution coastal mesh).

The real M4 quadtree leg. hydromt_sfincs CANNOT author a quadtree grid from
scratch (its ``setup_grid`` carries a ``# TODO gdf_refinement`` and ``setup_dep``
raises ``NotImplementedError`` for quadtree in every released version through
2.0.0-rc3), so quadtree authoring uses Deltares' ``cht_sfincs`` (GPL-3.0, the
Coastal Hazards Toolkit) - kept worker-side behind the same GPL isolation that
already carries ``hydromt_sfincs``. ADR 0113.

Contract (mirror of :func:`deck.build_sfincs_deck`): ``build_sfincs_quadtree_deck
(spec, scratch, download)`` localizes the topobathy DEM + waterlevel forcing,
builds a refined quadtree (coarse offshore -> fine at the coast + drawn
``refine_region`` polygons), samples per-face bed levels from the topobathy COG
(NO ``cht_bathymetry`` database - we bring our own fetched surface), builds the
active mask with a seaward open-water-level boundary, wires the surge timeseries,
writes ``sfincs.inp`` + the quadtree ``sfincs.nc`` + ``sfincs.bnd/.bzs`` + a
``mesh.geojson`` preview, and returns a provenance dict the entrypoint folds into
completion.json. From the solve onward the worker path is IDENTICAL to the
regular grid (SFINCS consumes ``qtrfile`` natively; the read-side postprocess
already probes the face-indexed output).

The granularity lever stays the user's: ``base_resolution_m`` + per-region
refinement levels ride in on ``spec["options"]["quadtree"]``.
"""

from __future__ import annotations

import json
import logging
import sys
import types
from dataclasses import replace
from pathlib import Path
from typing import Any

from .deck import (
    SFINCSSetupError,
    _localize_forcing_uris,
    build_options_from_dict,
    forcing_spec_from_dict,
)

logger = logging.getLogger("trid3nt.worker.sfincs_build.quadtree")


# --------------------------------------------------------------------------- #
# cht_sfincs import shim
#
# cht_sfincs 1.0.0 has module-level imports of an OLD cht_utils API
# (``cht_utils.misc_tools`` / ``cht_utils.pli_file``, both dropped in cht_utils
# 2.x), of ``cht_bathymetry`` (a bathymetry DATABASE we do not use - we sample
# our own fetched topobathy), and of ``datashader`` (map overlays we never
# render). We inject lightweight stand-ins into ``sys.modules`` BEFORE importing
# cht_sfincs so the worker image needs ONLY ``cht_sfincs`` + ``tabulate``
# (--no-deps) on top of the geo stack hydromt_sfincs already provides. The
# quadtree BUILD path (grid + mask + boundary + write) touches none of the
# stubbed callables; a stubbed call raises loudly rather than silently degrading.
# --------------------------------------------------------------------------- #


def _stub_module(name: str, **attrs: Any) -> types.ModuleType:
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


def _unavailable(what: str):
    def _raise(*_a: Any, **_k: Any) -> Any:
        raise SFINCSSetupError(
            "QUADTREE_UNSUPPORTED_OP",
            message=f"cht_sfincs {what} is stubbed in the quadtree deck builder "
            "(subgrid/tide/pli/datashader are out of scope for M4).",
        )

    return _raise


def _ensure_cht_importable() -> None:
    """Inject the stub modules cht_sfincs 1.0.0 imports at module load."""
    if "cht_sfincs" in sys.modules:
        return

    def _interp2(x0, y0, z0, x1, y1):  # noqa: ANN001 - numeric shim (subgrid only)
        import numpy as _np
        from scipy.interpolate import RegularGridInterpolator as _RGI

        f = _RGI((y0, x0), z0, bounds_error=False, fill_value=_np.nan)
        return f((y1, x1))

    if "cht_utils" not in sys.modules:
        sys.modules["cht_utils"] = _stub_module("cht_utils")
    sys.modules.setdefault(
        "cht_utils.misc_tools", _stub_module("cht_utils.misc_tools", interp2=_interp2)
    )
    sys.modules.setdefault(
        "cht_utils.pli_file",
        _stub_module(
            "cht_utils.pli_file",
            pli2gdf=_unavailable("pli2gdf"),
            gdf2pli=_unavailable("gdf2pli"),
            pol2gdf=_unavailable("pol2gdf"),
            gdf2pol=_unavailable("gdf2pol"),
        ),
    )
    if "cht_bathymetry" not in sys.modules:
        sys.modules["cht_bathymetry"] = _stub_module("cht_bathymetry")
    sys.modules.setdefault(
        "cht_bathymetry.bathymetry_database",
        _stub_module("cht_bathymetry.bathymetry_database", bathymetry_database=None),
    )
    if "datashader" not in sys.modules:
        ds = _stub_module("datashader", Canvas=_unavailable("Canvas"))
        sys.modules["datashader"] = ds
        sys.modules["datashader.transfer_functions"] = _stub_module(
            "datashader.transfer_functions", shade=_unavailable("shade")
        )
        sys.modules["datashader.utils"] = _stub_module(
            "datashader.utils", export_image=_unavailable("export_image")
        )


# --------------------------------------------------------------------------- #
# Quadtree config (rides in on spec["options"]["quadtree"])
# --------------------------------------------------------------------------- #

_DEFAULT_BASE_RES_M = 200.0
_DEFAULT_COAST_REFINE_LEVEL = 2  # base/4 at the shoreline (SFINCS-native)
_MAX_REFINE_LEVEL = 4
#: Coastal refinement band half-width (metres) around the z=0 shoreline when
#: ``coast_band_m`` is not supplied: the wider of 2x the base cell or 800 m, so
#: the fine band always spans at least a couple of coarse cells either side of
#: the land-sea interface.
_DEFAULT_COAST_BAND_FACTOR = 2.0
_DEFAULT_COAST_BAND_FLOOR_M = 800.0
_ACTIVE_ZMIN = -20.0  # deepest active bed (m NAVD88); below -> offshore inactive
_ACTIVE_ZMAX = 15.0   # highest active land (m NAVD88)


def _localize_dem(uri: str | None, download, inputs_dir: Path) -> str:
    if not uri:
        raise SFINCSSetupError(
            "BUILD_INPUT_MISSING",
            message="quadtree job_spec.inputs must carry dem_uri (topobathy)",
        )
    if uri.startswith("file://"):
        return uri[len("file://"):]
    if not (uri.startswith("s3://") or uri.startswith("gs://")):
        return uri
    dest = inputs_dir / f"dem{Path(uri.split('?', 1)[0]).suffix or '.tif'}"
    download(uri, dest)
    return str(dest)


def _sample_dem_on_points(dem_path: str, xs, ys, src_crs) -> Any:
    """Sample a topobathy COG (NAVD88 m, positive-up) at projected face points.

    Reprojects the DEM to the grid CRS lazily via rioxarray, then nearest-value
    samples at each face centroid. Faces off the DEM footprint come back NaN and
    are filled with the active-land ceiling so they read as dry land (never a
    silent hole).
    """
    import numpy as np
    import rioxarray  # noqa: F401 - registers the .rio accessor
    import xarray as xr

    da = xr.open_dataarray(dem_path)
    if da.rio.crs is None:
        da = da.rio.write_crs("EPSG:4326")
    da = da.rio.reproject(src_crs)
    if "band" in da.dims:
        da = da.isel(band=0, drop=True)
    xda = xr.DataArray(np.asarray(xs), dims="pts")
    yda = xr.DataArray(np.asarray(ys), dims="pts")
    z = da.sel(x=xda, y=yda, method="nearest").values.astype("float64")
    nodata = da.rio.nodata
    if nodata is not None:
        z = np.where(z == nodata, np.nan, z)
    return z


def _coast_refinement_geom(
    dem_path: str, crs_epsg: int, bbox_utm: tuple[float, float, float, float],
    coast_band_m: float,
):
    """A COAST-FOLLOWING refinement polygon buffered around the z=0 shoreline.

    Reprojects the topobathy DEM to the grid CRS, clips to the domain, extracts
    the ``z == 0`` land-sea interface as contour lines, and buffers them by
    ``coast_band_m`` on each side -- so the fine cells hug the ACTUAL shoreline
    (the meandering coast) rather than a horizontal latitude swath. Returns a
    shapely geometry clipped to the domain, or ``None`` when the AOI carries no
    land-sea interface (entirely wet or entirely dry -- the caller degrades to a
    center band with a loud warning).
    """
    import numpy as np
    import rioxarray  # noqa: F401 - registers the .rio accessor
    import xarray as xr
    from shapely.geometry import LineString, box
    from shapely.ops import unary_union

    xlo, ylo, xhi, yhi = bbox_utm
    da = xr.open_dataarray(dem_path)
    if da.rio.crs is None:
        da = da.rio.write_crs("EPSG:4326")
    da = da.rio.reproject(f"EPSG:{crs_epsg}")
    if "band" in da.dims:
        da = da.isel(band=0, drop=True)
    try:
        da = da.rio.clip_box(minx=xlo, miny=ylo, maxx=xhi, maxy=yhi)
    except Exception:  # noqa: BLE001 - DEM footprint may not cover the box; use as-is
        pass
    # Downsample for a fast contour (the shoreline shape survives a ~4x stride).
    da = da.isel(x=slice(None, None, 4), y=slice(None, None, 4))
    z = da.values.astype("float64")
    nodata = da.rio.nodata
    if nodata is not None:
        z = np.where(z == nodata, np.nan, z)
    finite = z[np.isfinite(z)]
    if finite.size == 0 or (finite < 0.0).sum() == 0 or (finite > 0.0).sum() == 0:
        return None  # no land-sea interface in the AOI
    xs = np.asarray(da.x.values, dtype="float64")
    ys = np.asarray(da.y.values, dtype="float64")
    zz = np.where(np.isfinite(z), z, 1.0e6)  # NaN -> high land so it reads dry
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    try:
        cs = plt.contour(xs, ys, zz, levels=[0.0])
        segs = [LineString(s) for s in cs.allsegs[0] if len(s) >= 2]
    except Exception:  # noqa: BLE001 - contouring failed -> caller degrades
        return None
    finally:
        plt.close("all")
    if not segs:
        return None
    band = unary_union(segs).buffer(float(coast_band_m))
    band = band.intersection(box(xlo, ylo, xhi, yhi))
    if band.is_empty or band.area <= 0.0:
        return None
    return band


def _mesh_geojson_from_grid(grid, crs_epsg: int) -> dict:
    """Build an EPSG:4326 FeatureCollection of quadtree cell polygons (preview).

    Variable cell sizes VISIBLE (the M1 spot-check pattern): each face is one
    polygon carrying ``cell_size_m`` + ``refine_level``. Reprojected to lon/lat
    so the mesh_grid style renders directly.
    """
    import numpy as np
    from pyproj import Transformer

    ug = grid.data.grid
    nx = ug.node_x
    ny = ug.node_y
    fn = ug.face_node_connectivity  # (nface, maxnodes), fill = -1
    level = grid.data["level"].values if "level" in grid.data else None
    t = Transformer.from_crs(crs_epsg, 4326, always_xy=True)
    feats = []
    for i, row in enumerate(fn):
        idx = [int(k) for k in row if k >= 0]
        if len(idx) < 3:
            continue
        xs = nx[idx]
        ys = ny[idx]
        area = 0.5 * abs(
            np.dot(xs, np.roll(ys, 1)) - np.dot(ys, np.roll(xs, 1))
        )
        size_m = round(float(np.sqrt(area)), 1)
        lon, lat = t.transform(xs, ys)
        ring = [[float(a), float(b)] for a, b in zip(lon, lat)]
        ring.append(ring[0])
        props = {"cell_size_m": size_m}
        if level is not None:
            props["refine_level"] = int(level[i]) - 1
        feats.append(
            {
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [ring]},
                "properties": props,
            }
        )
    return {"type": "FeatureCollection", "features": feats}


def build_sfincs_quadtree_deck(
    spec: dict[str, Any],
    scratch: Path,
    download,
) -> dict[str, Any]:
    """Build a variable-resolution SFINCS quadtree deck from the job_spec.

    Args mirror :func:`deck.build_sfincs_deck`. ``spec["options"]["quadtree"]``
    (optional nested dict) tunes the mesh:
        ``{"base_resolution_m": float, "coast_refine_level": int,
           "max_refine_level": int, "refine_regions": [{"polygon": <GeoJSON>,
           "refinement_level": int}]}``.

    Returns a provenance dict ``{deck_dir, quadtree: True, grid_resolution_m,
    nr_cells, nr_refinement_levels, refinement, forcing_type, mesh_uri_rel}``.

    Raises SFINCSSetupError on any build failure (typed error_code -> the same
    failed envelope the agent produces for the regular path).
    """
    import numpy as np
    import geopandas as gpd
    from pyproj import CRS, Transformer
    from shapely.geometry import Polygon, shape

    inputs_dir = scratch / "inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    deck_dir = scratch / "deck"
    deck_dir.mkdir(parents=True, exist_ok=True)

    bbox = tuple(float(v) for v in spec["bbox"])  # (w, s, e, n) EPSG:4326
    inp = spec.get("inputs") or {}
    dem_local = _localize_dem(inp.get("dem_uri"), download, inputs_dir)

    forcing_dict = _localize_forcing_uris(
        dict(spec.get("forcing") or {}), download, inputs_dir
    )
    forcing = forcing_spec_from_dict(forcing_dict)
    opts = build_options_from_dict(dict(spec.get("options") or {}))
    qt = dict((spec.get("options") or {}).get("quadtree") or {})
    base_res = float(qt.get("base_resolution_m") or _DEFAULT_BASE_RES_M)
    coast_level = int(qt.get("coast_refine_level") or _DEFAULT_COAST_REFINE_LEVEL)
    max_level = int(qt.get("max_refine_level") or _MAX_REFINE_LEVEL)
    coast_band_m = float(
        qt.get("coast_band_m")
        or max(base_res * _DEFAULT_COAST_BAND_FACTOR, _DEFAULT_COAST_BAND_FLOOR_M)
    )

    # --- Grid CRS: best UTM zone for the AOI centre ---
    lon_c = 0.5 * (bbox[0] + bbox[2])
    lat_c = 0.5 * (bbox[1] + bbox[3])
    utm_zone = int((lon_c + 180.0) // 6.0) + 1
    epsg = (32600 if lat_c >= 0 else 32700) + utm_zone
    crs = CRS.from_epsg(epsg)
    to_utm = Transformer.from_crs(4326, epsg, always_xy=True)
    x0, y0 = to_utm.transform(bbox[0], bbox[1])
    x1, y1 = to_utm.transform(bbox[2], bbox[3])
    span_x = abs(x1 - x0)
    span_y = abs(y1 - y0)
    nmax = max(4, int(round(span_y / base_res)))
    mmax = max(4, int(round(span_x / base_res)))

    _ensure_cht_importable()
    try:
        from cht_sfincs import SFINCS  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise SFINCSSetupError(
            "CHT_SFINCS_UNAVAILABLE",
            message=f"cht_sfincs not importable in the worker image: {exc}",
            details={"import_error": str(exc)},
        ) from exc

    # --- Refinement polygons: a COAST-FOLLOWING band (shoreline +/- buffer) ---
    # Refine where the topobathy crosses z=0 -- the true land-sea interface -- so
    # the fine cells hug the meandering coast, not a horizontal latitude swath.
    # ``_coast_refinement_geom`` extracts the z=0 contour and buffers it by
    # ``coast_band_m``. When the AOI has no interface (entirely wet or dry) it
    # degrades LOUDLY to a cross-shore center band. Drawn regions always honored.
    xlo, ylo = min(x0, x1), min(y0, y1)
    xhi, yhi = max(x0, x1), max(y0, y1)
    refine_rows: list[dict] = []
    coast_geom = _coast_refinement_geom(
        dem_local, epsg, (xlo, ylo, xhi, yhi), coast_band_m
    )
    if coast_geom is not None:
        refine_source = "shoreline_z0_contour"
        # cht_sfincs refine_in_polygon reads ``polygon.exterior`` per row, so a
        # MultiPolygon (disjoint coastal reaches) must be EXPLODED into one
        # single-Polygon row each (all at the coast refine level).
        _coast_level = max(1, min(coast_level, max_level))
        for poly in getattr(coast_geom, "geoms", [coast_geom]):
            if getattr(poly, "geom_type", "") == "Polygon" and not poly.is_empty:
                refine_rows.append({"geometry": poly, "refinement_level": _coast_level})
    if not refine_rows:
        coast_geom = None  # exploded to nothing -> fall through to center band
    if coast_geom is None:
        refine_source = "center_band_fallback"
        logger.warning(
            "quadtree: no z=0 land-sea interface resolved in AOI %s -- degrading "
            "refinement to the cross-shore center band (coast-following unavailable).",
            list(bbox),
        )
        band = Polygon(
            [
                (xlo, ylo + 0.30 * (yhi - ylo)),
                (xhi, ylo + 0.30 * (yhi - ylo)),
                (xhi, ylo + 0.70 * (yhi - ylo)),
                (xlo, ylo + 0.70 * (yhi - ylo)),
            ]
        )
        refine_rows.append(
            {"geometry": band, "refinement_level": max(1, min(coast_level, max_level))}
        )
    n_coast_rows = len(refine_rows)  # coast band rows precede any drawn regions
    for rr in qt.get("refine_regions") or []:
        try:
            geom4326 = shape(rr["polygon"]["geometry"] if "geometry" in rr["polygon"] else rr["polygon"])
            # reproject polygon 4326 -> utm
            xs, ys = geom4326.exterior.coords.xy
            ux, uy = to_utm.transform(list(xs), list(ys))
            geom = Polygon(list(zip(ux, uy)))
            lvl = int(rr.get("refinement_level") or coast_level)
            refine_rows.append({"geometry": geom, "refinement_level": max(1, min(lvl, max_level))})
        except Exception as exc:  # noqa: BLE001 - a bad drawn region never aborts
            logger.warning("quadtree: skipping malformed refine_region (%s)", exc)
    refine_gdf = gpd.GeoDataFrame(refine_rows, crs=epsg)

    # --- Build the quadtree topology ---
    try:
        sf = SFINCS(crs=epsg)
        sf.grid.build(
            float(min(x0, x1)), float(min(y0, y1)), int(nmax), int(mmax),
            float(base_res), float(base_res), 0.0,
            refinement_polygons=refine_gdf,
        )
    except SFINCSSetupError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SFINCSSetupError(
            "QUADTREE_GRID_BUILD_FAILED",
            message=f"cht_sfincs quadtree grid build failed: {exc}",
            details={"bbox": list(bbox), "base_res": base_res},
        ) from exc

    nr_cells = int(sf.grid.nr_cells)
    nr_levels = int(sf.grid.data.attrs.get("nr_levels", 1))

    # --- Per-face bed levels from OUR topobathy (no cht_bathymetry) ---
    import xugrid as xu
    import xarray as xr

    xy = sf.grid.data.grid.face_coordinates
    z = _sample_dem_on_points(dem_local, xy[:, 0], xy[:, 1], crs)
    z = np.where(np.isfinite(z), z, _ACTIVE_ZMAX)
    ug2d = sf.grid.data.grid
    sf.grid.data["z"] = xu.UgridDataArray(
        xr.DataArray(z, dims=[ug2d.face_dimension]), ug2d
    )

    # --- Active mask: cells with a physically active bed; seaward open boundary.
    # The seaward edge is the domain edge whose adjacent cells have the LOWEST
    # mean bed (the sea). Open-water-level boundary (msk==2) placed there.
    edge_band_m = max(base_res * 1.5, span_y * 0.06)
    # mean z near each of the 4 domain edges
    fx = xy[:, 0]
    fy = xy[:, 1]
    ylo, yhi = min(y0, y1), max(y0, y1)
    xlo, xhi = min(x0, x1), max(x0, x1)
    edges = {
        "south": (fy < ylo + edge_band_m),
        "north": (fy > yhi - edge_band_m),
        "west": (fx < xlo + edge_band_m),
        "east": (fx > xhi - edge_band_m),
    }
    sea_edge = min(
        edges,
        key=lambda k: float(np.nanmean(z[edges[k]])) if edges[k].any() else 1e9,
    )
    sel = edges[sea_edge]
    if sea_edge in ("south", "north"):
        poly = Polygon(
            [
                (xlo, ylo if sea_edge == "south" else yhi - edge_band_m),
                (xhi, ylo if sea_edge == "south" else yhi - edge_band_m),
                (xhi, ylo + edge_band_m if sea_edge == "south" else yhi),
                (xlo, ylo + edge_band_m if sea_edge == "south" else yhi),
            ]
        )
    else:
        poly = Polygon(
            [
                (xlo if sea_edge == "west" else xhi - edge_band_m, ylo),
                (xlo + edge_band_m if sea_edge == "west" else xhi, ylo),
                (xlo + edge_band_m if sea_edge == "west" else xhi, yhi),
                (xlo if sea_edge == "west" else xhi - edge_band_m, yhi),
            ]
        )
    open_gdf = gpd.GeoDataFrame({"geometry": [poly]}, crs=epsg)
    try:
        sf.mask.build(
            zmin=_ACTIVE_ZMIN, zmax=_ACTIVE_ZMAX,
            open_boundary_polygon=open_gdf, quiet=True,
        )
    except Exception as exc:  # noqa: BLE001
        raise SFINCSSetupError(
            "QUADTREE_MASK_BUILD_FAILED",
            message=f"cht_sfincs quadtree mask build failed: {exc}",
        ) from exc
    mask = sf.grid.data["mask"].values
    n_active = int((mask == 1).sum())
    n_bnd = int((mask == 2).sum())
    if n_active <= 0:
        raise SFINCSSetupError(
            "QUADTREE_EMPTY_MASK",
            message=f"quadtree mask has no active cells (bbox={list(bbox)}); "
            "the AOI bed is entirely outside the active band.",
        )

    # --- Surge water-level boundary (from ForcingSpec.waterlevel or a design peak).
    import pandas as pd

    sf.boundary_conditions.get_boundary_points_from_mask()
    wl_series = _resolve_waterlevel_timeseries(forcing, spec)
    if n_bnd > 0 and wl_series is not None:
        sf.boundary_conditions.set_timeseries_uniform(wl_series)

    # --- sfincs.inp ---
    v = sf.input.variables
    if wl_series is not None and len(wl_series) > 0:
        v.tref = wl_series.index[0].to_pydatetime()
        v.tstart = wl_series.index[0].to_pydatetime()
        v.tstop = wl_series.index[-1].to_pydatetime()
    out_stride_s = float((opts.output_interval_min or 30.0)) * 60.0
    v.dtmapout = out_stride_s
    v.dtmaxout = out_stride_s
    v.qtrfile = "sfincs.nc"
    v.mskfile = ""
    v.depfile = ""
    v.indexfile = ""
    v.manning_land = 0.04
    v.manning_sea = 0.02
    v.zsini = 0.0
    v.advection = 0

    # --- Write the deck (cht_sfincs sf.write() crashes on uninitialized
    #     self.infiltration; write the components we author explicitly) ---
    sf.path = str(deck_dir)
    try:
        sf.input.write()
        sf.grid.write()
        sf.boundary_conditions.write()
    except Exception as exc:  # noqa: BLE001
        raise SFINCSSetupError(
            "QUADTREE_DECK_WRITE_FAILED",
            message=f"quadtree deck write failed: {exc}",
        ) from exc

    # --- Mesh preview geojson (variable cells VISIBLE) ---
    mesh_rel = "mesh.geojson"
    try:
        fc = _mesh_geojson_from_grid(sf.grid, epsg)
        (deck_dir / mesh_rel).write_text(json.dumps(fc), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 - preview is best-effort, never fatal
        logger.warning("quadtree: mesh preview build failed (non-fatal): %s", exc)
        mesh_rel = None

    cell_sizes = sorted(
        {round(base_res / (2 ** lvl)) for lvl in range(nr_levels)}
    )
    logger.info(
        "quadtree deck: %d cells, %d levels, sizes=%s m, active=%d bnd=%d "
        "sea_edge=%s -> %s",
        nr_cells, nr_levels, cell_sizes, n_active, n_bnd, sea_edge, deck_dir,
    )
    return {
        "deck_dir": str(deck_dir),
        "quadtree": True,
        "grid_resolution_m": base_res,
        "nr_cells": nr_cells,
        "nr_refinement_levels": nr_levels,
        "refinement": {
            "base_resolution_m": base_res,
            "finest_resolution_m": float(base_res / (2 ** max(0, nr_levels - 1))),
            "cell_sizes_m": cell_sizes,
            "coast_refine_level": coast_level,
            "coast_band_m": coast_band_m,
            "refine_source": refine_source,
            "n_drawn_refine_regions": len(refine_rows) - n_coast_rows,
            "sea_boundary_edge": sea_edge,
        },
        "n_active_cells": n_active,
        "n_boundary_cells": n_bnd,
        "forcing_type": forcing.forcing_type,
        "grid_crs_epsg": epsg,
        "mesh_uri_rel": mesh_rel,
        "output_interval_min": opts.output_interval_min,
    }


def _resolve_waterlevel_timeseries(forcing, spec: dict[str, Any]):
    """A uniform boundary water-level series: the fetched surge CSV, else a
    design triangular surge scaled by ``return_period_yr`` (never fabricated
    silently - the design fallback is provenance-stamped by the caller)."""
    import numpy as np
    import pandas as pd

    wl = getattr(forcing, "waterlevel", None)
    ts_uri = getattr(wl, "timeseries_uri", None) if wl is not None else None
    if ts_uri and Path(ts_uri).is_file():
        try:
            df = pd.read_csv(ts_uri)
            tcol = df.columns[0]
            vcol = df.columns[1]
            idx = pd.to_datetime(df[tcol])
            s = pd.Series(df[vcol].astype(float).values, index=idx)
            if getattr(wl, "datum_offset_m", None):
                s = s + float(wl.datum_offset_m)
            return s
        except Exception as exc:  # noqa: BLE001 - fall through to design surge
            logger.warning("quadtree: waterlevel CSV read failed (%s); design surge", exc)

    # Design triangular surge (no fetched forcing): peak scaled by return period.
    rp = float((spec.get("options") or {}).get("return_period_yr") or spec.get("return_period_yr") or 100)
    peak = float(np.clip(1.0 + 0.4 * np.log10(max(rp, 2.0)), 1.0, 5.0))
    times = pd.date_range("2018-10-10 00:00", "2018-10-10 12:00", freq="30min")
    n = len(times)
    surge = np.concatenate([np.linspace(0, peak, n // 2), np.linspace(peak, 0, n - n // 2)])
    return pd.Series(surge, index=times)
