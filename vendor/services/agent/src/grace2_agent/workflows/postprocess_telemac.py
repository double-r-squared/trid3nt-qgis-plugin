"""TELEMAC-2D river-dye run-output postprocessing (river-dye North Star).

``postprocess_telemac(slf_path, *, run_id, utm_epsg, ...) -> (layers, metrics)``
reads a solved TELEMAC-2D result SELAFIN (``r2d_river.slf``), extracts the DYE
tracer field over its time steps, rasterizes the PEAK (per-node max over time)
concentration onto a regular EPSG:4326 grid clipped to the river channel, and
emits the SAME ``(layers, metrics)`` shape as ``postprocess_geoclaw`` /
``postprocess_openquake`` so the case/plugin render path consumes it unchanged.

THE DELIBERATE DIFFERENCE from GeoClaw/SWMM (which emit a peak COG + a per-frame
COG animation group): the TELEMAC result IS a native, time-stepped MDAL mesh --
QGIS's MDAL provider opens the ``.slf`` directly and animates its DYE dataset
group with ZERO new render code. So this postprocess emits ONLY the PEAK
concentration COG (``layers[0]``, role ``"primary"``, style preset
``continuous_dye_concentration``) as the map anchor + narration carrier; the
time animation is played from the SELAFIN mesh SIBLING that
``export_case_to_qgis`` discovers next to this COG in the runs bucket (its
``_MESH_SIBLING_BY_STYLE_PRESET`` maps this style preset to ``r2d_river.slf``).
No per-frame COGs are written -- the mesh already carries every frame.

Honesty floor (invariant 1 / FR-AS-7): the dye scalars are computed with plain
arithmetic from the SELAFIN tracer field -- no LLM anywhere. The COG carries an
"idealized bed plane + prescribed-dispersion" label so a demo release is never
read as a calibrated site study.

SELAFIN reading is HAND-ROLLED in pure numpy (mirroring ``postprocess_geoclaw``'s
hand-rolled ``fort.q`` reader): the agent venv has NO TELEMAC/pytel install, so
this module never imports ``data_manip`` -- it parses the big-endian Fortran
records itself, validated against a real solved ``r2d_river.slf``.
"""

from __future__ import annotations

import logging
import struct
from pathlib import Path
from typing import Any

from grace2_contracts.telemac_contracts import (
    TELEMAC_DYE_STYLE_PRESET,
    TelemacDyeLayerURI,
)
from grace2_contracts.execution import LegendKey

from . import cog_io
from .cog_io import CogIoError
from .postprocess_flood import RUNS_BUCKET_DEFAULT

__all__ = [
    "PostprocessTelemacError",
    "postprocess_telemac",
    "read_selafin",
    "TELEMAC_DYE_STYLE_PRESET",
    "TELEMAC_DYE_WET_MGL",
    "TELEMAC_TARGET_GROUND_RES_M",
]

logger = logging.getLogger("grace2_agent.workflows.postprocess_telemac")

#: Concentration (mg/L) below which a node is treated as "no dye" (dilution floor)
#: for the wet mask, plume-extent, and metric aggregation. 1 mg/L mirrors the
#: worker's tracer-sanity threshold so the agent and worker agree on "present".
TELEMAC_DYE_WET_MGL: float = 1.0

#: Target GROUND resolution (m/px) for the adaptive dye COG. A river channel is
#: narrow (tens of metres), so ~10 m/px keeps the plume a smooth ribbon rather
#: than chunky specks. Floor + cap mirror the GeoClaw adaptive sizing.
TELEMAC_TARGET_GROUND_RES_M: float = 10.0
TELEMAC_MIN_PX_PER_SIDE: int = 128
TELEMAC_MAX_PX_PER_SIDE: int = 2500
TELEMAC_MAX_TOTAL_CELLS: int = 5_000_000


class PostprocessTelemacError(RuntimeError):
    """Raised on read / rasterize / COG-write / upload failures.

    ``error_code`` matches the open-set A.6 surface so the agent emitter renders
    a typed error frame:

    - ``TELEMAC_OUTPUT_READ_FAILED`` -- could not parse the SELAFIN.
    - ``TELEMAC_OUTPUT_EMPTY`` -- no DYE variable / no time steps / no wet nodes.
    - ``TELEMAC_DEPENDENCY_MISSING`` -- numpy / scipy / rasterio not importable.
    - ``TELEMAC_COG_WRITE_FAILED`` -- rasterio could not write the COG.
    - ``TELEMAC_CRS_TAG_MISMATCH`` -- the COG CRS tag did not round-trip.
    - ``TELEMAC_COG_UPLOAD_FAILED`` -- the runs-bucket upload of the COG failed.
    """

    error_code: str = "POSTPROCESS_TELEMAC_FAILED"

    def __init__(
        self,
        error_code: str,
        *,
        message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message or error_code)
        self.error_code = error_code
        self.details: dict[str, Any] = dict(details or {})


# --------------------------------------------------------------------------- #
# Hand-rolled SELAFIN reader (pure numpy -- NO TELEMAC import).
# --------------------------------------------------------------------------- #
def _read_record(fh) -> bytes:
    """Read one Fortran sequential-unformatted record (big-endian 4-byte markers)."""
    head = fh.read(4)
    if len(head) < 4:
        raise EOFError("unexpected EOF reading record length")
    (n,) = struct.unpack(">i", head)
    payload = fh.read(n)
    if len(payload) < n:
        raise EOFError("unexpected EOF reading record payload")
    tail = fh.read(4)
    if len(tail) < 4:
        raise EOFError("unexpected EOF reading record trailer")
    (m,) = struct.unpack(">i", tail)
    if m != n:
        raise ValueError(f"record markers disagree ({n} != {m})")
    return payload


def read_selafin(path: str | Path) -> dict[str, Any]:
    """Parse a SELAFIN (SERAFIN) file into mesh + per-variable time series.

    Big-endian Fortran sequential-unformatted (opentelemac's SELAFIN/SERAFIN).
    Detects single (``SERAFIN``) vs double (``SERAFIND``) precision from the
    title trailer. Returns::

        {"title": str, "varnames": [str], "npoin": int, "nelem": int,
         "x": ndarray(npoin), "y": ndarray(npoin),
         "times": ndarray(nframes),
         "data": {varname: ndarray(nframes, npoin)}}

    Only the variable NAMES + node coords + per-frame values are needed here (we
    never touch IKLE for the raster path -- scattered-node interpolation is
    enough), but IKLE/IPOBO records are still consumed to keep the byte cursor
    aligned. Pure numpy; validated against a real solved ``r2d_river.slf``.
    """
    import numpy as np

    with open(path, "rb") as fh:
        title_rec = _read_record(fh)
        title = title_rec[:72].decode("latin-1", "replace").strip()
        precision_tag = title_rec[72:80].decode("latin-1", "replace")
        double = "SERAFIND" in precision_tag.upper() or "SELAFIND" in precision_tag.upper()
        fdtype = ">f8" if double else ">f4"
        fsize = 8 if double else 4

        nbv1, nbv2 = struct.unpack(">2i", _read_record(fh))
        varnames: list[str] = []
        for _ in range(nbv1):
            varnames.append(_read_record(fh)[:32].decode("latin-1", "replace").strip())
        for _ in range(nbv2):
            _read_record(fh)  # secondary (clandestine) vars -- skip

        iparam = struct.unpack(">10i", _read_record(fh))
        if iparam[9] == 1:  # IPARAM(10)==1 -> a date record follows
            _read_record(fh)

        nelem, npoin, ndp, _ = struct.unpack(">4i", _read_record(fh))
        _read_record(fh)  # IKLE (nelem*ndp int32) -- consumed, not used here
        _read_record(fh)  # IPOBO (npoin int32)    -- consumed, not used here
        x = np.frombuffer(_read_record(fh), dtype=fdtype).astype("float64")
        y = np.frombuffer(_read_record(fh), dtype=fdtype).astype("float64")
        if x.size != npoin or y.size != npoin:
            raise ValueError(f"coord record size mismatch (npoin={npoin}, x={x.size})")

        times: list[float] = []
        data: dict[str, list] = {v: [] for v in varnames}
        while True:
            try:
                trec = _read_record(fh)
            except EOFError:
                break
            t = np.frombuffer(trec, dtype=fdtype)
            if t.size < 1:
                break
            times.append(float(t[0]))
            for v in varnames:
                buf = _read_record(fh)
                arr = np.frombuffer(buf, dtype=fdtype).astype("float64")
                if arr.size != npoin:
                    raise ValueError(
                        f"variable {v!r} frame size {arr.size} != npoin {npoin}"
                    )
                data[v].append(arr)

    return {
        "title": title,
        "varnames": varnames,
        "npoin": int(npoin),
        "nelem": int(nelem),
        "x": x,
        "y": y,
        "times": np.asarray(times, dtype="float64"),
        "data": {v: (np.vstack(a) if a else np.empty((0, npoin))) for v, a in data.items()},
    }


def _pick_dye_var(varnames: list[str]) -> str | None:
    """The DYE tracer variable name (case-insensitive DYE, else a T-prefixed
    tracer), or None. Mirrors the worker entrypoint's tracer-sanity selection."""
    for v in varnames:
        if "DYE" in v.upper():
            return v
    for v in varnames:
        u = v.strip().upper()
        if u.startswith("T") and not u.startswith(("TEMP",)):
            return v
    return None


# --------------------------------------------------------------------------- #
# Rasterization: scatter mesh nodes -> regular 4326 grid, clipped to the channel.
# --------------------------------------------------------------------------- #
def _grid_shape(bbox, res_m: float) -> tuple[int, int]:
    import math

    min_lon, min_lat, max_lon, max_lat = bbox
    mean_lat = 0.5 * (min_lat + max_lat)
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * max(math.cos(math.radians(mean_lat)), 1e-6)
    h_m = (max_lat - min_lat) * m_per_deg_lat
    w_m = (max_lon - min_lon) * m_per_deg_lon
    res = max(res_m, 1e-6)
    nrows = min(max(int(round(h_m / res)), TELEMAC_MIN_PX_PER_SIDE), TELEMAC_MAX_PX_PER_SIDE)
    ncols = min(max(int(round(w_m / res)), TELEMAC_MIN_PX_PER_SIDE), TELEMAC_MAX_PX_PER_SIDE)
    if nrows * ncols > TELEMAC_MAX_TOTAL_CELLS:
        s = math.sqrt(TELEMAC_MAX_TOTAL_CELLS / float(nrows * ncols))
        nrows = max(TELEMAC_MIN_PX_PER_SIDE, int(nrows * s))
        ncols = max(TELEMAC_MIN_PX_PER_SIDE, int(ncols * s))
    return nrows, ncols


def _rasterize_nodes_to_grid(lon, lat, vals, bbox, out_shape, clip_dist_deg):
    """Linear-interpolate scattered node values onto a regular 4326 grid, then
    clip to the channel: a cell whose nearest node is farther than
    ``clip_dist_deg`` is set to NaN (griddata otherwise fills the whole convex
    hull, painting dye across meander cut-offs that carry no mesh). Sub-floor and
    uncovered cells are NaN. Row 0 = NORTH (COG orientation)."""
    import numpy as np
    from scipy.interpolate import griddata
    from scipy.spatial import cKDTree

    nrows, ncols = int(out_shape[0]), int(out_shape[1])
    min_lon, min_lat, max_lon, max_lat = bbox
    gdx = (max_lon - min_lon) / ncols
    gdy = (max_lat - min_lat) / nrows
    xc = min_lon + (np.arange(ncols) + 0.5) * gdx
    yc = max_lat - (np.arange(nrows) + 0.5) * gdy  # north->south
    gx, gy = np.meshgrid(xc, yc)

    pts = np.column_stack([lon, lat])
    grid = griddata(pts, vals, (gx, gy), method="linear")
    # Clip to the mesh footprint via nearest-node distance.
    tree = cKDTree(pts)
    dist, _ = tree.query(np.column_stack([gx.ravel(), gy.ravel()]), k=1)
    dist = dist.reshape(nrows, ncols)
    grid = np.asarray(grid, dtype="float64")
    grid[dist > clip_dist_deg] = np.nan
    grid[~np.isfinite(grid)] = np.nan
    grid[grid < TELEMAC_DYE_WET_MGL] = np.nan
    return grid


def _reraise_cogio(exc: CogIoError) -> "PostprocessTelemacError":
    codes = {
        "DEPENDENCY": "TELEMAC_DEPENDENCY_MISSING",
        "WRITE": "TELEMAC_COG_WRITE_FAILED",
        "REPROJECT": "TELEMAC_COG_WRITE_FAILED",
        "CRS_MISMATCH": "TELEMAC_CRS_TAG_MISMATCH",
        "UPLOAD": "TELEMAC_COG_UPLOAD_FAILED",
    }
    return PostprocessTelemacError(
        codes.get(exc.stage, "POSTPROCESS_TELEMAC_FAILED"),
        message=exc.message,
        details=dict(exc.details),
    )


# --------------------------------------------------------------------------- #
# Top-level postprocess.
# --------------------------------------------------------------------------- #
def postprocess_telemac(
    slf_path: str | Path,
    *,
    run_id: str,
    utm_epsg: int,
    reach_name: str = "river_dye",
    dye_units: str = "mg/L",
    runs_bucket: str | None = None,
    target_ground_res_m: float = TELEMAC_TARGET_GROUND_RES_M,
) -> tuple[list[TelemacDyeLayerURI], dict[str, Any]]:
    """Rasterize a solved TELEMAC-2D dye run into ONE peak-concentration COG.

    Reads ``slf_path`` (``r2d_river.slf``), extracts the DYE tracer, computes the
    per-node peak over time, reprojects the mesh nodes ``utm_epsg`` -> EPSG:4326,
    rasterizes the peak onto an adaptive 4326 grid clipped to the channel, writes
    + uploads ONE COG (``telemac_dye_peak.tif``) to the runs bucket, and returns
    ``([TelemacDyeLayerURI], metrics)``. The time animation is served separately
    from the SELAFIN mesh sibling that ``export_case_to_qgis`` discovers next to
    this COG (this postprocess writes NO per-frame COGs).

    Args:
        slf_path: the solved result SELAFIN (local path, already downloaded).
        run_id: the run id the COG is keyed under in the runs bucket (and whose
            ``r2d_river.slf`` sibling the export path discovers for animation).
        utm_epsg: the SELAFIN mesh CRS EPSG (the reach UTM zone; from
            ``telemac_metrics.json``'s ``utm_epsg``). SELAFIN carries no CRS.
        reach_name: echoed into the layer name.
        dye_units: concentration units label (default mg/L).
        runs_bucket: optional override for the runs bucket name.
        target_ground_res_m: target ground resolution (m/px) for the COG.

    Returns:
        ``(layers, metrics)`` -- ``layers[0]`` the peak ``TelemacDyeLayerURI``;
        ``metrics`` the peak/plume aggregates dict.

    Raises:
        PostprocessTelemacError: any read / rasterize / COG-write / upload failure.
    """
    try:
        import numpy as np
        from pyproj import Transformer  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        raise PostprocessTelemacError(
            "TELEMAC_DEPENDENCY_MISSING",
            message=f"numpy/pyproj unavailable for TELEMAC postprocess: {exc}",
        ) from exc

    slf = Path(slf_path)
    try:
        mesh = read_selafin(slf)
    except Exception as exc:  # noqa: BLE001
        raise PostprocessTelemacError(
            "TELEMAC_OUTPUT_READ_FAILED",
            message=f"could not parse SELAFIN {slf.name}: {exc}",
            details={"slf": str(slf)},
        ) from exc

    dye_var = _pick_dye_var(mesh["varnames"])
    if dye_var is None or mesh["data"].get(dye_var) is None or mesh["data"][dye_var].size == 0:
        raise PostprocessTelemacError(
            "TELEMAC_OUTPUT_EMPTY",
            message=f"no DYE tracer / no time steps in {slf.name} "
            f"(vars={mesh['varnames']})",
            details={"slf": str(slf), "varnames": mesh["varnames"]},
        )

    import numpy as np

    dye = np.asarray(mesh["data"][dye_var])  # (nframes, npoin)
    times = np.asarray(mesh["times"])
    x_utm = np.asarray(mesh["x"])
    y_utm = np.asarray(mesh["y"])

    # --- honest scalar metrics (pure arithmetic over the tracer field) -------- #
    per_frame_cmax = dye.max(axis=1) if dye.shape[0] else np.array([0.0])
    peak_i = int(np.argmax(per_frame_cmax))
    dye_cmax = float(per_frame_cmax.max())
    dye_peak_time_s = float(times[peak_i]) if times.size else None
    active_frames = int((per_frame_cmax > TELEMAC_DYE_WET_MGL).sum())
    node_peak = dye.max(axis=0)  # per-node peak over time (the published grid)

    # Plume travel: farthest downstream displacement of the wet-mass centroid.
    from pyproj import Transformer

    back = Transformer.from_crs(int(utm_epsg), 4326, always_xy=True)
    lon, lat = back.transform(x_utm, y_utm)
    lon = np.asarray(lon)
    lat = np.asarray(lat)

    plume_reach_m = None
    try:
        cxs = []
        cys = []
        for i in range(dye.shape[0]):
            c = dye[i]
            m = c > TELEMAC_DYE_WET_MGL
            if m.any() and c[m].sum() > 0:
                cxs.append(float((x_utm[m] * c[m]).sum() / c[m].sum()))
                cys.append(float((y_utm[m] * c[m]).sum() / c[m].sum()))
        if len(cxs) >= 2:
            c0 = np.array([cxs[0], cys[0]])
            disp = [float(np.hypot(cxs[k] - c0[0], cys[k] - c0[1])) for k in range(len(cxs))]
            plume_reach_m = round(max(disp), 1)
    except Exception:  # noqa: BLE001 -- travel metric is best-effort
        plume_reach_m = None

    if not np.isfinite(node_peak).any() or float(np.nanmax(node_peak)) < TELEMAC_DYE_WET_MGL:
        raise PostprocessTelemacError(
            "TELEMAC_OUTPUT_EMPTY",
            message=f"DYE never exceeded {TELEMAC_DYE_WET_MGL} {dye_units} "
            f"anywhere in {slf.name} (peak {dye_cmax:.4g})",
            details={"dye_cmax_mgl": dye_cmax},
        )

    # --- rasterize the per-node peak onto a 4326 grid clipped to the channel -- #
    pad = 0.0009  # ~100 m lon/lat pad so the ribbon is not clipped at the banks
    bbox = (
        float(lon.min() - pad),
        float(lat.min() - pad),
        float(lon.max() + pad),
        float(lat.max() + pad),
    )
    shape = _grid_shape(bbox, target_ground_res_m)
    # clip distance: ~1.5 output cells (keeps only near-channel cells).
    clip_dist_deg = 1.5 * max((bbox[2] - bbox[0]) / shape[1], (bbox[3] - bbox[1]) / shape[0])
    try:
        grid = _rasterize_nodes_to_grid(lon, lat, node_peak, bbox, shape, clip_dist_deg)
    except Exception as exc:  # noqa: BLE001
        raise PostprocessTelemacError(
            "TELEMAC_OUTPUT_READ_FAILED",
            message=f"dye rasterization failed: {exc}",
        ) from exc

    from rasterio.transform import from_bounds

    transform = from_bounds(bbox[0], bbox[1], bbox[2], bbox[3], shape[1], shape[0])
    try:
        cog = cog_io.write_cog_4326_from_grid(
            grid,
            src_crs="EPSG:4326",
            src_transform=transform,
            reproject=False,
            crs_roundtrip_guard=True,
            dst_suffix="_telemac_dye_4326.tif",
        )
    except CogIoError as exc:
        raise _reraise_cogio(exc) from exc

    try:
        uri = cog_io.upload_cog(
            cog,
            run_id,
            runs_bucket,
            dest_filename="telemac_dye_peak.tif",
            content_type="image/tiff",
            gs_backend="fsspec",
            gs_fallback_to_file=False,
            runs_bucket_default=RUNS_BUCKET_DEFAULT,
            log_label="TELEMAC dye COG",
        )
    except CogIoError as exc:
        raise _reraise_cogio(exc) from exc
    finally:
        cog_io.safe_unlink(cog)

    vmax = round(max(dye_cmax, TELEMAC_DYE_WET_MGL), 3)
    legend = LegendKey(
        kind="continuous",
        colormap="viridis",
        vmin=0.0,
        vmax=vmax,
        units=dye_units,
        label=f"Dye concentration ({dye_units})",
    )
    # Honesty floor: this is an idealized demo release (flat/planar idealized bed
    # + a prescribed dispersion coefficient), NOT a calibrated site study.
    honesty = (
        "Idealized demo: planar idealized channel bed + prescribed tracer "
        "dispersion; peak dye envelope over the run, not a calibrated study."
    )
    layer = TelemacDyeLayerURI(
        layer_id=f"telemac-dye-peak-{run_id}",
        name=f"Peak dye concentration ({reach_name})",
        layer_type="raster",
        uri=uri,
        style_preset=TELEMAC_DYE_STYLE_PRESET,
        role="primary",
        units=dye_units,
        bbox=bbox,
        legend=legend,
        fallback_note=honesty,
        dye_cmax_mgl=dye_cmax,
        dye_peak_time_s=dye_peak_time_s,
        plume_reach_m=plume_reach_m,
        active_frames=active_frames,
    )

    metrics: dict[str, Any] = {
        "dye_var": dye_var.strip(),
        "dye_cmax_mgl": dye_cmax,
        "dye_peak_time_s": dye_peak_time_s,
        "plume_reach_m": plume_reach_m,
        "active_frames": active_frames,
        "n_frames": int(times.size),
        "npoin": int(mesh["npoin"]),
        "nelem": int(mesh["nelem"]),
        "utm_epsg": int(utm_epsg),
        "bbox": list(bbox),
        "crs": "EPSG:4326",
        "honesty_label": honesty,
    }
    logger.info(
        "postprocess_telemac run_id=%s dye_var=%s cmax=%.4g mg/L peak_t=%ss "
        "plume_reach_m=%s active_frames=%d n_frames=%d -> %s",
        run_id,
        dye_var.strip(),
        dye_cmax,
        dye_peak_time_s,
        plume_reach_m,
        active_frames,
        int(times.size),
        uri,
    )
    return [layer], metrics
