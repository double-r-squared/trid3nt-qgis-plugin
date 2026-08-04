"""SCHISM barotropic-tidal run-output postprocessing (engine #12 landing, ADR 0118).

``postprocess_schism(out2d_path, out2d_uri, *, run_id, ...) -> (layers, metrics)``
reads a scribed SCHISM ``out2d_*.nc`` (UGRID: per-node surface ``elevation`` time
series + the mesh node coords + face connectivity), computes the PEAK (max over
time) water-surface elevation at each node, and produces:

  * ``layers[0]`` -- the max water-surface ELEVATION COG (``SchismElevationLayerURI``,
    role primary). For a GEOREFERENCED run (coastal_tin, lon/lat nodes) it is an
    EPSG:4326 grid CLIPPED to the mesh AOI (THE ADR 0116 contract: never a raw
    continental netCDF layer). For the IDEALIZED QuarterAnnulus verification mesh
    (planar non-geographic coords) it is a native-frame GeoTIFF with a LOUD
    non-geographic note -- the real QA deliverable is the analytical RMSE gate.
  * ``layers[1]`` -- the MESH preview (``layer_type="mesh"``, ADR 0118): the out2d
    UGRID netCDF itself, which the plugin opens via MDAL (``QgsMeshLayer``); the
    ONE format the live materializer STAGES (ADR 0116). ``crs_authid`` carries the
    explicit CRS MDAL needs.

Honesty floor (invariant 1 / FR-AS-7): every elevation scalar is plain arithmetic
from the netCDF -- no LLM. ``h5py``/``netCDF4``/``rasterio``/``pyproj`` are
lazy-imported so this module stays offline-suite-safe.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from trid3nt_contracts.execution import LayerURI, LegendKey
from trid3nt_contracts.schism_contracts import (
    SCHISM_ELEV_STYLE_PRESET,
    SCHISM_OUTPUT_EMPTY,
    SCHISM_SOLVE_FAILED,
    SchismElevationLayerURI,
)

from trid3nt_server.agent.workflows.shared import cog_io
from trid3nt_server.agent.workflows.shared.cog_io import CogIoError

__all__ = [
    "PostprocessSchismError",
    "postprocess_schism",
    "read_out2d_elevation",
    "read_station_series",
    "verify_against_analytical",
    "SCHISM_TARGET_GROUND_RES_M",
]

logger = logging.getLogger("trid3nt_server.agent.workflows.schism.postprocess_schism")

#: Target GROUND resolution (m/px) for the adaptive elevation COG + px caps
#: (mirrors the HEC-RAS/TELEMAC adaptive sizing).
SCHISM_TARGET_GROUND_RES_M: float = 60.0
_MIN_PX_PER_SIDE: int = 128
_MAX_PX_PER_SIDE: int = 2500

#: out2d UGRID variable-name candidates (SCHISM scribed-IO names first).
_NODE_X_CANDS = ("SCHISM_hgrid_node_x", "SCHISM_hgrid_node_lon", "x", "longitude")
_NODE_Y_CANDS = ("SCHISM_hgrid_node_y", "SCHISM_hgrid_node_lat", "y", "latitude")
_ELEV_CANDS = ("elevation", "elev")
_FACE_CANDS = ("SCHISM_hgrid_face_nodes", "element", "face_nodes")


class PostprocessSchismError(RuntimeError):
    """Raised on read / rasterize / COG-write / upload failures.

    ``error_code`` matches the A.6 open-set so the agent emitter renders a typed
    error frame (``SCHISM_OUTPUT_EMPTY`` -- no elevation / no finite nodes;
    ``SCHISM_SOLVE_FAILED`` -- read/rasterize/write/upload fault).
    """

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def _first_var(ds: Any, cands: tuple[str, ...]) -> str | None:
    for c in cands:
        if c in ds.variables:
            return c
    return None


def read_out2d_elevation(out2d_path: str | Path) -> dict[str, Any]:
    """Read node coords + peak/min surface elevation from a scribed out2d netCDF.

    Returns ``{node_x, node_y, elev_max, elev_min, n_nodes, is_geographic,
    n_times, bbox}``. ``elev_max``/``elev_min`` are (N,) max/min over time per node.
    Raises ``SCHISM_OUTPUT_EMPTY`` when elevation / node coords are absent or all
    non-finite.
    """
    import numpy as np
    from netCDF4 import Dataset  # lazy

    with Dataset(str(out2d_path), "r") as ds:
        xk = _first_var(ds, _NODE_X_CANDS)
        yk = _first_var(ds, _NODE_Y_CANDS)
        ek = _first_var(ds, _ELEV_CANDS)
        if not (xk and yk and ek):
            raise PostprocessSchismError(
                SCHISM_OUTPUT_EMPTY,
                f"out2d missing node coords/elevation (have x={xk} y={yk} elev={ek}); "
                f"vars={list(ds.variables)[:20]}",
            )
        node_x = np.asarray(ds.variables[xk][:], dtype=np.float64).ravel()
        node_y = np.asarray(ds.variables[yk][:], dtype=np.float64).ravel()
        elev = np.asarray(ds.variables[ek][:], dtype=np.float64)  # (time, node) or (node,)

    if elev.ndim == 1:
        elev = elev[None, :]
    n_times = int(elev.shape[0])
    # SCHISM dry/junk fill is a large sentinel; mask non-finite + |z|>1e6.
    elev = np.where(np.isfinite(elev) & (np.abs(elev) < 1.0e6), elev, np.nan)
    with np.errstate(all="ignore"):
        elev_max = np.nanmax(elev, axis=0)
        elev_min = np.nanmin(elev, axis=0)
    finite = np.isfinite(elev_max)
    if not finite.any():
        raise PostprocessSchismError(
            SCHISM_OUTPUT_EMPTY, "out2d elevation is entirely non-finite (empty solve)"
        )
    n = min(node_x.shape[0], node_y.shape[0], elev_max.shape[0])
    node_x, node_y = node_x[:n], node_y[:n]
    elev_max, elev_min, finite = elev_max[:n], elev_min[:n], finite[:n]

    is_geographic = bool(
        np.nanmax(np.abs(node_x)) <= 360.0 and np.nanmax(np.abs(node_y)) <= 90.0
    )
    fx, fy = node_x[finite], node_y[finite]
    bbox = [float(fx.min()), float(fy.min()), float(fx.max()), float(fy.max())]
    return {
        "node_x": node_x,
        "node_y": node_y,
        "elev_max": elev_max,
        "elev_min": elev_min,
        "finite": finite,
        "n_nodes": int(n),
        "is_geographic": is_geographic,
        "n_times": n_times,
        "bbox": bbox,
    }


def _adaptive_grid(bbox: list[float], is_geographic: bool) -> tuple[int, int]:
    """Pick ``(width_px, height_px)`` for the elevation COG."""
    import math

    min_x, min_y, max_x, max_y = bbox
    if is_geographic:
        lat_mid = 0.5 * (min_y + max_y)
        m_per_deg_lat = 111_320.0
        m_per_deg_lon = 111_320.0 * max(math.cos(math.radians(lat_mid)), 1e-6)
        width_m = (max_x - min_x) * m_per_deg_lon
        height_m = (max_y - min_y) * m_per_deg_lat
    else:  # planar metres (idealized)
        width_m = max_x - min_x
        height_m = max_y - min_y
    w = int(round(width_m / SCHISM_TARGET_GROUND_RES_M))
    h = int(round(height_m / SCHISM_TARGET_GROUND_RES_M))
    w = max(_MIN_PX_PER_SIDE, min(w, _MAX_PX_PER_SIDE))
    h = max(_MIN_PX_PER_SIDE, min(h, _MAX_PX_PER_SIDE))
    return w, h


def _rasterize_nodes(
    node_x: Any, node_y: Any, values: Any, finite: Any, bbox: list[float], is_geographic: bool
) -> tuple[Any, Any]:
    """Nearest-node rasterization of a per-node scalar onto a regular grid.

    Returns ``(grid, transform)``. Uses scipy's cKDTree nearest-node lookup and
    masks grid cells outside the mesh convex hull to NaN (honest no-data edge).
    """
    import numpy as np
    from rasterio.transform import from_bounds
    from scipy.spatial import cKDTree
    from scipy.spatial import Delaunay

    width_px, height_px = _adaptive_grid(bbox, is_geographic)
    min_x, min_y, max_x, max_y = bbox
    transform = from_bounds(min_x, min_y, max_x, max_y, width_px, height_px)

    fx, fy, fv = node_x[finite], node_y[finite], values[finite]
    pts = np.column_stack([fx, fy])
    tree = cKDTree(pts)
    # cell-center coords
    xs = min_x + (np.arange(width_px) + 0.5) * (max_x - min_x) / width_px
    ys = max_y - (np.arange(height_px) + 0.5) * (max_y - min_y) / height_px
    gx, gy = np.meshgrid(xs, ys)
    q = np.column_stack([gx.ravel(), gy.ravel()])
    _, idx = tree.query(q, k=1)
    grid = fv[idx].reshape(height_px, width_px).astype(np.float32)

    # Mask outside the mesh hull (Delaunay membership) so we do not paint water on
    # dry land outside the modeled domain.
    try:
        hull = Delaunay(pts)
        inside = hull.find_simplex(q) >= 0
        grid = np.where(inside.reshape(height_px, width_px), grid, np.nan).astype(np.float32)
    except Exception:  # noqa: BLE001 -- hull is a refinement, never fatal
        pass
    return grid, transform


def postprocess_schism(
    out2d_path: str | Path,
    out2d_uri: str,
    *,
    run_id: str,
    mesh_source: str,
    sim_days: float,
    constituents: list[str],
    n_nodes_grid: int | None = None,
    n_elements_grid: int | None = None,
    runs_bucket: str | None = None,
    fallback_note: str | None = None,
) -> tuple[list[LayerURI], dict[str, Any]]:
    """Rasterize a SCHISM out2d to a max-elevation COG + emit the UGRID mesh preview.

    Returns ``(layers, metrics)``: ``layers[0]`` the ``SchismElevationLayerURI``
    max-elevation COG (primary), ``layers[1]`` the ``layer_type="mesh"`` out2d
    UGRID preview (context). ``metrics`` carries the elevation stats.
    """
    out2d_path = Path(out2d_path)
    data = read_out2d_elevation(out2d_path)
    node_x, node_y = data["node_x"], data["node_y"]
    elev_max, elev_min, finite = data["elev_max"], data["elev_min"], data["finite"]
    is_geographic = data["is_geographic"]
    bbox = data["bbox"]

    import numpy as np

    grid, transform = _rasterize_nodes(node_x, node_y, elev_max, finite, bbox, is_geographic)

    # --- write + upload the max-elevation COG ---------------------------------
    if is_geographic:
        try:
            cog_path = cog_io.write_cog_4326_from_grid(
                grid, src_crs="EPSG:4326", src_transform=transform,
                reproject=False, crs_roundtrip_guard=False,
            )
        except CogIoError as exc:
            raise PostprocessSchismError(SCHISM_SOLVE_FAILED, f"elevation COG write failed: {exc}") from exc
        crs_authid = "EPSG:4326"
    else:
        cog_path = _write_native_cog(grid, transform)
        crs_authid = None

    try:
        cog_uri = cog_io.upload_cog(
            cog_path, run_id, runs_bucket,
            dest_filename="schism_elev_max.tif",
            log_label="SCHISM max-elevation COG",
        )
    except CogIoError as exc:
        raise PostprocessSchismError(SCHISM_SOLVE_FAILED, f"elevation COG upload failed: {exc}") from exc
    finally:
        cog_io.safe_unlink(cog_path)

    elev_max_m = float(np.nanmax(elev_max[finite]))
    elev_min_m = float(np.nanmin(elev_min[finite]))
    tidal_range_m = float(elev_max_m - elev_min_m)

    elev_layer = SchismElevationLayerURI(
        layer_id=f"schism-elev-max-{run_id}",
        name=f"Max water-surface elevation (SCHISM {mesh_source})",
        layer_type="raster",
        uri=cog_uri,
        style_preset=SCHISM_ELEV_STYLE_PRESET,
        role="primary",
        units="m",
        bbox=(tuple(bbox) if is_geographic else None),  # zoom-to only for geographic
        crs_authid=crs_authid,
        legend=LegendKey(
            kind="continuous", colormap="blues",
            vmin=round(min(0.0, elev_min_m), 3), vmax=round(elev_max_m, 3),
            units="m", label="Max water-surface elevation (m)",
        ),
        fallback_note=fallback_note,
        elev_max_m=round(elev_max_m, 4),
        elev_min_m=round(elev_min_m, 4),
        tidal_range_m=round(tidal_range_m, 4),
        n_nodes=int(n_nodes_grid or data["n_nodes"]),
        n_elements=(int(n_elements_grid) if n_elements_grid else None),
        sim_days=float(sim_days),
        mesh_source=mesh_source,
        constituents=list(constituents),
    )

    layers: list[LayerURI] = [elev_layer]

    # --- the UGRID mesh preview (layer_type="mesh", ADR 0116/0118) ------------
    mesh_layer = LayerURI(
        layer_id=f"schism-mesh-{run_id}",
        name=f"SCHISM mesh ({data['n_nodes']} nodes)",
        layer_type="mesh",
        uri=out2d_uri,  # the out2d UGRID netCDF the plugin opens via MDAL
        style_preset="mesh_grid",
        role="context",
        bbox=None,
        crs_authid=crs_authid,
    )
    layers.append(mesh_layer)

    metrics = {
        "elev_max_m": elev_max_m,
        "elev_min_m": elev_min_m,
        "tidal_range_m": tidal_range_m,
        "n_nodes": int(data["n_nodes"]),
        "n_times": int(data["n_times"]),
        "is_geographic": is_geographic,
        "bbox": bbox,
        "cog_uri": cog_uri,
        "mesh_uri": out2d_uri,
    }
    logger.info(
        "postprocess_schism run_id=%s mesh_source=%s nodes=%d elev_max=%.3f m "
        "tidal_range=%.3f m geographic=%s uri=%s",
        run_id, mesh_source, data["n_nodes"], elev_max_m, tidal_range_m,
        is_geographic, cog_uri,
    )
    return layers, metrics


def _write_native_cog(grid: Any, transform: Any) -> Path:
    """Write a native-frame (non-geographic) float32 GeoTIFF for the idealized mesh.

    Used only for the QuarterAnnulus verification mesh (planar coords). No CRS is
    assigned (crs_authid=None on the layer); the plugin renders it in a local
    frame. NaN is the no-data fill.
    """
    import tempfile

    import numpy as np
    import rasterio

    fd = tempfile.NamedTemporaryFile(suffix="_schism_native.tif", delete=False)
    fd.close()
    path = Path(fd.name)
    h, w = grid.shape
    with rasterio.open(
        str(path), "w", driver="GTiff", height=h, width=w, count=1,
        dtype="float32", transform=transform, nodata=float("nan"),
        tiled=True, blockxsize=256, blockysize=256, compress="deflate",
    ) as dst:
        dst.write(np.asarray(grid, dtype=np.float32), 1)
    return path


def read_station_series(staout_path: str | Path) -> list[dict[str, float]]:
    """Parse a SCHISM staout_1 (time[s], elevation[m]) into ``[{t_hr, elev_m}]``.

    Best-effort: returns ``[]`` on any read fault (the chart is a nice-to-have).
    """
    import numpy as np

    try:
        arr = np.loadtxt(str(staout_path))
        if arr.ndim == 1:
            arr = arr[None, :]
        return [
            {"t_hr": round(float(t) / 3600.0, 4), "elev_m": round(float(z), 5)}
            for t, z in arr[:, :2]
        ]
    except Exception as exc:  # noqa: BLE001
        logger.warning("schism station-series read failed (non-fatal): %s", exc)
        return []


def verify_against_analytical(
    staout_path: str | Path, analytical_path: str | Path, *, spinup_days: float = 3.0
) -> dict[str, float] | None:
    """Compare modeled station elevation to the bundled analytical M2 solution.

    Mirrors the spike's qa_gate.py exactly: staout_1 (time[s], elev[m]) vs
    ForPlot_ana_elev.dat over the spun-up window (t >= spinup_days). Returns
    ``{rmse_m, amp_err_m, amp_modeled_m, amp_analytical_m, correlation}`` or
    ``None`` on any read fault.
    """
    import numpy as np

    try:
        me = np.loadtxt(str(staout_path))
        t_d = me[:, 0] / 86400.0
        z_me = me[:, 1]
        ana = np.loadtxt(str(analytical_path), comments="%")
        n = min(len(z_me), len(ana))
        t_d, z_me, z_ana = t_d[:n], z_me[:n], ana[:n, 1]
        mask = t_d >= spinup_days
        if mask.sum() < 3:
            return None
        rmse = float(np.sqrt(np.mean((z_me[mask] - z_ana[mask]) ** 2)))
        amp_me = 0.5 * (z_me[mask].max() - z_me[mask].min())
        amp_an = 0.5 * (z_ana[mask].max() - z_ana[mask].min())
        corr = float(np.corrcoef(z_me[mask], z_ana[mask])[0, 1])
        return {
            "rmse_m": round(rmse, 5),
            "amp_err_m": round(abs(amp_me - amp_an), 5),
            "amp_modeled_m": round(float(amp_me), 5),
            "amp_analytical_m": round(float(amp_an), 5),
            "correlation": round(corr, 5),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("schism analytical verification failed (non-fatal): %s", exc)
        return None
