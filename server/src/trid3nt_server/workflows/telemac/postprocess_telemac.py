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
import os
import struct
from pathlib import Path
from typing import Any

from trid3nt_contracts.telemac_contracts import (
    TELEMAC_BED_EVOLUTION_STYLE_PRESET,
    TELEMAC_DYE_STYLE_PRESET,
    TELEMAC_WSE_STYLE_PRESET,
    TelemacDyeLayerURI,
    TelemacSedimentLayerURI,
    TelemacWseLayerURI,
)
from trid3nt_contracts.execution import LegendKey

from trid3nt_server.workflows.shared import cog_io
from trid3nt_server.workflows.shared.cog_io import CogIoError
from trid3nt_server.workflows.sfincs.postprocess_flood import RUNS_BUCKET_DEFAULT

__all__ = [
    "PostprocessTelemacError",
    "postprocess_telemac",
    "postprocess_telemac_deposition",
    "postprocess_telemac_wse",
    "read_selafin",
    "TELEMAC_DYE_STYLE_PRESET",
    "TELEMAC_BED_EVOLUTION_STYLE_PRESET",
    "TELEMAC_WSE_STYLE_PRESET",
    "TELEMAC_DYE_WET_MGL",
    "TELEMAC_TARGET_GROUND_RES_M",
    "TELEMAC_WSE_WET_DEPTH_M",
]

logger = logging.getLogger("trid3nt_server.workflows.telemac.postprocess_telemac")

#: Concentration (mg/L) below which a node is treated as "no dye". OPEN-23
#: (2026-07-16): a HARDCODED 1.0 mg/L false-flagged real-but-dilute plumes as
#: TELEMAC_OUTPUT_EMPTY (e.g. a heavily-diluted spill peaking at 0.18 mg/L over
#: a long reach). The detection floor is now RELATIVE to the run's own peak
#: (``max(_DYE_WET_FLOOR, _DYE_WET_FRAC * dye_cmax)``): any run with a real
#: plume passes at any concentration, while a genuinely empty run (peak ~0)
#: still fails via the tiny absolute floor. ``TELEMAC_DYE_WET_MGL`` is retained
#: as a legacy default only.
TELEMAC_DYE_WET_MGL: float = 1.0
#: Absolute floor (mg/L) that separates a real (any-concentration) plume from a
#: genuinely empty solve; below this, dye is treated as never injected.
_DYE_WET_FLOOR: float = 1e-3
#: Fraction of the run's peak concentration that defines the plume edge for the
#: wet mask / extent metrics (5% of source-strength = the visible ribbon).
_DYE_WET_FRAC: float = 0.05

#: Target GROUND resolution (m/px) for the adaptive dye COG. A river channel is
#: narrow (tens of metres), so ~10 m/px keeps the plume a smooth ribbon rather
#: than chunky specks. Floor + cap mirror the GeoClaw adaptive sizing.
TELEMAC_TARGET_GROUND_RES_M: float = 10.0
TELEMAC_MIN_PX_PER_SIDE: int = 128
TELEMAC_MAX_PX_PER_SIDE: int = 2500
TELEMAC_MAX_TOTAL_CELLS: int = 5_000_000

#: Water-depth floor (m) above which a node counts as WET for the max-WSE raster.
#: TELEMAC's FREE SURFACE equals the BED elevation at a dry node (depth 0), so an
#: unmasked max-over-time of FREE SURFACE would paint dry terrain as a water
#: surface. We take the peak FREE SURFACE only over frames where WATER DEPTH
#: exceeds this floor, so a never-wetted node reads NaN (no water), never its bed
#: elevation. 1 cm mirrors the flood engines' wet threshold.
TELEMAC_WSE_WET_DEPTH_M: float = 0.01


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


def _pick_dye_var(varnames: list[str], *, prefer_sediment: bool = False) -> str | None:
    """The tracer variable name to rasterize, or None.

    Default (dye / decay runs): case-insensitive DYE, else a T-prefixed tracer
    (mirrors the worker entrypoint's tracer-sanity selection).

    ``prefer_sediment=True`` (GAIA sediment coupled run): the suspended sediment
    concentration rides as a SECOND telemac2d tracer that the in-image smoke
    (2026-07-19) showed lands in ``r2d_river.slf`` as ``NCOH SEDIMENT1`` (g/l ==
    kg/m3) alongside the required DYE companion. Pick that sediment tracer (a name
    carrying SEDIMENT / NCOH / COH), so the concentration COG is the SEDIMENT
    ribbon, not the conservative dye reference. Falls back to the dye pick when no
    sediment-named var is present (an uncoupled rerun)."""
    if prefer_sediment:
        for v in varnames:
            u = v.strip().upper()
            if "SEDIMENT" in u or u.startswith(("NCOH", "COH", "CS")):
                return v
    for v in varnames:
        if "DYE" in v.upper():
            return v
    for v in varnames:
        u = v.strip().upper()
        if u.startswith("T") and not u.startswith(("TEMP",)):
            return v
    return None


#: TELEMAC-2D free-surface variable names (English + French decks). The Malpasset
#: reference deck emits ``FREE SURFACE    M``; a French deck emits ``SURFACE
#: LIBRE``/``COTE DE LA SURFACE LIBRE``. Never guessed -- verified by parsing the
#: bundled ``f2d_malpasset-small.slf`` header.
_WSE_VAR_KEYS: tuple[str, ...] = ("FREE SURFACE", "SURFACE LIBRE", "WATER SURFACE",
                                  "COTE DE LA SURFACE", "COTE DE L'EAU")
#: Water-depth variable names (English + French) used to build the wet mask.
_DEPTH_VAR_KEYS: tuple[str, ...] = ("WATER DEPTH", "HAUTEUR D'EAU", "HAUTEUR D EAU")


def _pick_named_var(varnames: list[str], keys: tuple[str, ...], letter: str) -> str | None:
    """First variable whose (upper, trimmed) name contains any of ``keys``.

    Falls back to an EXACT single-letter mnemonic match (``S`` free surface / ``H``
    water depth) for a terse deck. Returns ``None`` when nothing matches (the
    caller decides if that is fatal) -- never guesses a wrong field."""
    for v in varnames:
        u = v.strip().upper()
        for k in keys:
            if k in u:
                return v
    for v in varnames:
        if v.strip().upper() == letter:
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


def _rasterize_nodes_to_grid(lon, lat, vals, bbox, out_shape, clip_dist_deg, wet_floor=0.0):
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
    grid[grid < wet_floor] = np.nan
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
    substance: str = "dye",
    substance_class: str = "tracer",
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

    # GAIA sediment coupled run: pick the SUSPENDED SEDIMENT tracer (NCOH
    # SEDIMENT1, g/l == kg/m3) that GAIA appends beside the dye companion, so this
    # COG is the sediment concentration ribbon, not the conservative dye.
    _prefer_sed = str(substance_class or "tracer").lower() == "sediment"
    dye_var = _pick_dye_var(mesh["varnames"], prefer_sediment=_prefer_sed)
    if dye_var is None or mesh["data"].get(dye_var) is None or mesh["data"][dye_var].size == 0:
        raise PostprocessTelemacError(
            "TELEMAC_OUTPUT_EMPTY",
            message=f"no tracer / no time steps in {slf.name} "
            f"(vars={mesh['varnames']})",
            details={"slf": str(slf), "varnames": mesh["varnames"]},
        )

    import numpy as np

    dye = np.asarray(mesh["data"][dye_var])  # (nframes, npoin)
    # UNITS TRAP (pinned by the 2026-07-19 in-image smoke): the GAIA suspended
    # sediment tracer lands in r2d as 'NCOH SEDIMENT1' in g/l (== kg/m3), while
    # the dye tracer + our whole UI speak mg/L. Scale the sediment field g/l ->
    # mg/L (x1000) so the concentration COG + cmax are in mg/L like the dye - a
    # silent 1000x error otherwise passes every structural check.
    _du = dye_var.strip().upper()
    if _prefer_sed and ("SEDIMENT" in _du or _du.startswith(("NCOH", "COH", "CS"))):
        dye = dye * 1000.0
    times = np.asarray(mesh["times"])
    x_utm = np.asarray(mesh["x"])
    y_utm = np.asarray(mesh["y"])

    # --- honest scalar metrics (pure arithmetic over the tracer field) -------- #
    per_frame_cmax = dye.max(axis=1) if dye.shape[0] else np.array([0.0])
    peak_i = int(np.argmax(per_frame_cmax))
    dye_cmax = float(per_frame_cmax.max())
    dye_peak_time_s = float(times[peak_i]) if times.size else None
    # OPEN-23: detection floor relative to THIS run's peak (+ a tiny absolute
    # floor for genuinely-empty solves), so a dilute-but-real plume is not
    # false-flagged as OUTPUT_EMPTY.
    wet = max(_DYE_WET_FLOOR, _DYE_WET_FRAC * dye_cmax)
    active_frames = int((per_frame_cmax > wet).sum())
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
            m = c > wet
            if m.any() and c[m].sum() > 0:
                cxs.append(float((x_utm[m] * c[m]).sum() / c[m].sum()))
                cys.append(float((y_utm[m] * c[m]).sum() / c[m].sum()))
        if len(cxs) >= 2:
            c0 = np.array([cxs[0], cys[0]])
            disp = [float(np.hypot(cxs[k] - c0[0], cys[k] - c0[1])) for k in range(len(cxs))]
            plume_reach_m = round(max(disp), 1)
    except Exception:  # noqa: BLE001 -- travel metric is best-effort
        plume_reach_m = None

    if not np.isfinite(node_peak).any() or float(np.nanmax(node_peak)) < wet:
        raise PostprocessTelemacError(
            "TELEMAC_OUTPUT_EMPTY",
            message=f"DYE never exceeded the detection floor {wet:.4g} {dye_units} "
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
        grid = _rasterize_nodes_to_grid(lon, lat, node_peak, bbox, shape, clip_dist_deg, wet_floor=wet)
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

    vmax = round(max(dye_cmax, wet), 6)
    legend = LegendKey(
        kind="continuous",
        colormap="viridis",
        vmin=0.0,
        vmax=vmax,
        units=dye_units,
        label=f"{(substance or 'dye').title()} concentration ({dye_units})",
    )
    # Honesty floor: this is an idealized demo release (flat/planar idealized bed
    # + a prescribed dispersion coefficient), NOT a calibrated site study.
    honesty = (
        "Idealized demo: planar idealized channel bed + prescribed tracer "
        "dispersion; peak dye envelope over the run, not a calibrated study."
    )
    layer = TelemacDyeLayerURI(
        layer_id=f"telemac-dye-peak-{run_id}",
        name=f"Peak {(substance or 'dye')} concentration ({reach_name})",
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


# --------------------------------------------------------------------------- #
# GAIA sediment: the SECOND COG - final CUMUL BED EVOL (deposition, mm).
# --------------------------------------------------------------------------- #
def postprocess_telemac_deposition(
    gaia_slf_path: str | Path,
    *,
    run_id: str,
    utm_epsg: int,
    reach_name: str = "river_sediment",
    worker_sed_metrics: dict[str, Any] | None = None,
    runs_bucket: str | None = None,
    target_ground_res_m: float = TELEMAC_TARGET_GROUND_RES_M,
) -> tuple[list[TelemacSedimentLayerURI], dict[str, Any]]:
    """Rasterize the GAIA final CUMUL BED EVOL field into ONE deposition COG.

    Reads ``gaia_river.slf`` (the GAIA result), picks the CUMUL BED EVOL variable
    (mnemonic ``E``; the in-image smoke confirmed it is present in METRES), takes
    the FINAL frame (cumulative bed change -> final = total event deposition),
    reprojects the mesh nodes ``utm_epsg`` -> EPSG:4326, rasterizes the SIGNED bed
    change in MILLIMETRES onto an adaptive grid clipped to the channel, writes +
    uploads ONE COG (``telemac_sediment_deposition.tif``) on the diverging
    bed-evolution preset, and returns ``([TelemacSedimentLayerURI], metrics)``.

    The layer's deposited_mass_kg / deposit_fraction come from
    ``worker_sed_metrics`` (GAIA's OWN listing mass balance - the authoritative
    closure numbers, never reconstructed): deposited_mass_kg is the NET bed mass
    (CUMULATED BED EVOLUTIONS, clamped >= 0), the SAME net quantity the final-frame
    E-field map and deposit_fraction integrate - never the gross CUMULATED
    DEPOSITION, which can cancel against erosion and contradict the map.
    max_deposition_mm is measured independently off the E field here (the design's
    cross-check).

    Raises ``PostprocessTelemacError`` on any read / rasterize / COG failure.
    """
    try:
        import numpy as np
        from pyproj import Transformer  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        raise PostprocessTelemacError(
            "TELEMAC_DEPENDENCY_MISSING",
            message=f"numpy/pyproj unavailable for GAIA deposition: {exc}",
        ) from exc

    slf = Path(gaia_slf_path)
    try:
        mesh = read_selafin(slf)
    except Exception as exc:  # noqa: BLE001
        raise PostprocessTelemacError(
            "TELEMAC_OUTPUT_READ_FAILED",
            message=f"could not parse GAIA SELAFIN {slf.name}: {exc}",
            details={"slf": str(slf)},
        ) from exc

    import numpy as np

    # pick CUMUL BED EVOL (mnemonic E). Never pick BOTTOM (the static bed).
    evol_var = None
    for v in mesh["varnames"]:
        u = v.strip().upper()
        if "EVOL" in u or u.startswith("E"):
            evol_var = v
            break
    if evol_var is None or mesh["data"].get(evol_var) is None \
            or mesh["data"][evol_var].size == 0:
        raise PostprocessTelemacError(
            "TELEMAC_OUTPUT_EMPTY",
            message=f"no CUMUL BED EVOL in {slf.name} (vars={mesh['varnames']})",
            details={"slf": str(slf), "varnames": mesh["varnames"]},
        )

    evol = np.asarray(mesh["data"][evol_var])  # (nframes, npoin), metres
    x_utm = np.asarray(mesh["x"])
    y_utm = np.asarray(mesh["y"])
    node_final_mm = evol[-1] * 1000.0          # final cumulative bed change, mm
    dep_only_mm = np.where(node_final_mm > 0.0, node_final_mm, 0.0)
    max_dep_mm = float(dep_only_mm.max()) if dep_only_mm.size else 0.0

    from pyproj import Transformer

    back = Transformer.from_crs(int(utm_epsg), 4326, always_xy=True)
    lon, lat = back.transform(x_utm, y_utm)
    lon = np.asarray(lon)
    lat = np.asarray(lat)

    if max_dep_mm <= 0.0:
        raise PostprocessTelemacError(
            "TELEMAC_OUTPUT_EMPTY",
            message=f"no bed deposition anywhere in {slf.name} "
            f"(supply-limited run deposited nothing measurable)",
            details={"max_deposition_mm": max_dep_mm},
        )

    pad = 0.0009
    bbox = (
        float(lon.min() - pad), float(lat.min() - pad),
        float(lon.max() + pad), float(lat.max() + pad),
    )
    shape = _grid_shape(bbox, target_ground_res_m)
    clip_dist_deg = 1.5 * max(
        (bbox[2] - bbox[0]) / shape[1], (bbox[3] - bbox[1]) / shape[0])
    # rasterize the DEPOSITION (positive mm) field; erosion/zero -> NaN so the
    # diverging ramp reads the tongue cleanly (v1 is supply-limited: near all
    # signal is deposition). wet_floor tiny so a mm-scale tongue is not clipped.
    try:
        grid = _rasterize_nodes_to_grid(
            lon, lat, dep_only_mm, bbox, shape, clip_dist_deg,
            wet_floor=max(1e-4, 0.02 * max_dep_mm))
    except Exception as exc:  # noqa: BLE001
        raise PostprocessTelemacError(
            "TELEMAC_OUTPUT_READ_FAILED",
            message=f"deposition rasterization failed: {exc}",
        ) from exc

    from rasterio.transform import from_bounds

    transform = from_bounds(bbox[0], bbox[1], bbox[2], bbox[3], shape[1], shape[0])
    try:
        cog = cog_io.write_cog_4326_from_grid(
            grid, src_crs="EPSG:4326", src_transform=transform,
            reproject=False, crs_roundtrip_guard=True,
            dst_suffix="_telemac_deposition_4326.tif",
        )
    except CogIoError as exc:
        raise _reraise_cogio(exc) from exc
    try:
        uri = cog_io.upload_cog(
            cog, run_id, runs_bucket,
            dest_filename="telemac_sediment_deposition.tif",
            content_type="image/tiff", gs_backend="fsspec",
            gs_fallback_to_file=False, runs_bucket_default=RUNS_BUCKET_DEFAULT,
            log_label="TELEMAC GAIA deposition COG",
        )
    except CogIoError as exc:
        raise _reraise_cogio(exc) from exc
    finally:
        cog_io.safe_unlink(cog)

    wsm = dict(worker_sed_metrics or {})
    # deposited_mass_kg is the NET bed mass (CUMULATED BED EVOLUTIONS), NOT the
    # gross CUMULATED DEPOSITION: net is the quantity the final-frame E-field map
    # and deposit_fraction both integrate, so the headline number must agree with
    # them. In a supply-limited v1 run gross deposition can equal gross erosion
    # (re-suspension of just-settled sediment) leaving net ~= 0 -> the map is
    # (correctly) suppressed as empty, and the narrated mass must be ~0 too, not
    # the gross figure. A tiny numeric-negative net clamps to 0 (net erosion means
    # nothing net-deposited; also keeps the contract's ge=0.0 valid).
    _net = wsm.get("sediment_net_bed_mass_kg")
    deposited_mass_kg = max(float(_net), 0.0) if _net is not None else None
    deposit_fraction = wsm.get("sediment_deposit_fraction")
    # diverging legend centered on 0; range = the deposition peak (symmetric so
    # the rdbu midpoint is 0 bed change). mm units.
    vext = round(max(max_dep_mm, 1e-3), 4)
    legend = LegendKey(
        kind="continuous", colormap="rdbu", vmin=-vext, vmax=vext, units="mm",
        label="Bed evolution / deposition (mm)",
    )
    honesty = (
        "Event-scale deposition (mm), not annual morphology: a supply-limited "
        "GAIA run (bed initial thickness 0) - only the injected sediment pulse "
        "can deposit, nothing erodes. Grain size is a demo default / user "
        "override (no site bed-composition fetcher exists), not a site measurement."
    )
    layer = TelemacSedimentLayerURI(
        layer_id=f"telemac-sediment-deposition-{run_id}",
        name=f"Sediment deposition ({reach_name})",
        layer_type="raster",
        uri=uri,
        style_preset=TELEMAC_BED_EVOLUTION_STYLE_PRESET,
        role="primary",
        units="mm",
        bbox=bbox,
        legend=legend,
        fallback_note=honesty,
        deposited_mass_kg=deposited_mass_kg,
        deposit_fraction=deposit_fraction,
        max_deposition_mm=round(max_dep_mm, 4),
        grain_size_um=wsm.get("grain_size_um"),
        sediment_type=wsm.get("sediment_type"),
    )
    metrics: dict[str, Any] = {
        "evol_var": evol_var.strip(),
        "max_deposition_mm": round(max_dep_mm, 4),
        "deposited_mass_kg": deposited_mass_kg,
        "deposit_fraction": deposit_fraction,
        "npoin": int(mesh["npoin"]),
        "utm_epsg": int(utm_epsg),
        "bbox": list(bbox),
        "crs": "EPSG:4326",
        "honesty_label": honesty,
    }
    logger.info(
        "postprocess_telemac_deposition run_id=%s evol_var=%s max_dep_mm=%.4g "
        "deposited_kg=%s deposit_frac=%s -> %s",
        run_id, evol_var.strip(), max_dep_mm, deposited_mass_kg,
        deposit_fraction, uri,
    )
    return [layer], metrics


# --------------------------------------------------------------------------- #
# MAX FREE-SURFACE ELEVATION (WSE) - the dam-break / river validation COG.
# --------------------------------------------------------------------------- #
def _nn_spacing_m(x, y) -> float:
    """Median nearest-neighbour node spacing (mesh characteristic length).

    Used to size the raster clip distance for a COARSE validation mesh: the dye
    path clips at ~1.5 output cells (fine channel mesh), but a dam-break mesh has
    ~tens-of-metres node spacing, so a cell-based clip would punch holes BETWEEN
    nodes inside the domain. Clipping at ~2x the node spacing keeps the interior
    filled while still trimming cells outside the mesh footprint."""
    import numpy as np
    from scipy.spatial import cKDTree

    pts = np.column_stack([np.asarray(x, "float64"), np.asarray(y, "float64")])
    if pts.shape[0] < 2:
        return 1.0
    tree = cKDTree(pts)
    d, _ = tree.query(pts, k=2)  # col 0 = self (0), col 1 = nearest other node
    nn = d[:, 1]
    nn = nn[np.isfinite(nn) & (nn > 0)]
    return float(np.median(nn)) if nn.size else 1.0


def postprocess_telemac_wse(
    slf_path: str | Path,
    *,
    run_id: str,
    mesh_epsg: int,
    reach_name: str = "river",
    quantity: str = "wse",
    vertical_datum: str | None = None,
    mesh_frame_note: str | None = None,
    runs_bucket: str | None = None,
    target_ground_res_m: float = TELEMAC_TARGET_GROUND_RES_M,
    _output_dir: str | None = None,
) -> tuple[list[TelemacWseLayerURI], dict[str, Any]]:
    """Rasterize a solved TELEMAC-2D result into ONE peak FREE-SURFACE (WSE) COG.

    The validation-case analogue of :func:`postprocess_telemac` (which rasterizes
    the DYE tracer): reads ``slf_path`` (``r2d_river.slf`` / a reference result),
    picks the ``FREE SURFACE`` variable, and computes the per-node MAX-over-time
    water-surface elevation -- but ONLY over frames where that node's WATER DEPTH
    exceeded :data:`TELEMAC_WSE_WET_DEPTH_M` (TELEMAC's free surface equals the bed
    at a dry node, so an unmasked max would paint dry terrain as a water surface).
    A never-wetted node is NaN (no water), never its bed elevation.

    Unlike the dye path this writes the COG **in the MESH's OWN CRS**
    (``mesh_epsg``), with NO reprojection to EPSG:4326: obs high-water marks for a
    validation case live in the same mesh frame, so keeping both sides in one
    identical CRS makes the downstream ``extract_model_at_observations`` pairing an
    exact identity (zero reprojection distortion). The raster is stamped with a
    ``quantity=water_surface_elevation`` TAG so the pairing tool resolves the model
    quantity from the tag and pairs it like-for-like against a WSE observation (no
    DEM / depth conversion needed when both sides are WSE).

    Args:
        slf_path: the solved result SELAFIN (local path, already downloaded).
        run_id: run id the COG is keyed under in the runs bucket.
        mesh_epsg: the EPSG the SELAFIN mesh coordinates are in (the raster is
            written verbatim in this CRS -- NO reprojection). For a bundled
            local-frame validation mesh this is a PLACEHOLDER projected EPSG the
            coordinates are stamped with; ``mesh_frame_note`` records the caveat.
        reach_name: echoed into the layer name.
        quantity: ``"wse"`` (free surface, default) or ``"depth"`` (max water
            depth) -- selects the source variable + the stamped quantity tag.
        vertical_datum: OPTIONAL datum label carried on the layer (e.g. ``"NGF"``).
        mesh_frame_note: OPTIONAL local-frame caveat folded into ``fallback_note``.
        runs_bucket: optional override for the runs bucket name.
        target_ground_res_m: target ground resolution (m/px) for the COG.
        _output_dir: TEST/offline hook -- when set, the COG is written to this
            directory (``telemac_wse_max_<run_id>.tif``) and its LOCAL path is
            returned instead of uploading to the runs bucket (mirrors
            ``extract_model_at_observations``'s ``_output_dir``).

    Returns:
        ``([TelemacWseLayerURI], metrics)`` -- ``layers[0]`` the peak-WSE layer;
        ``metrics`` the WSE aggregates dict.

    Raises:
        PostprocessTelemacError: any read / rasterize / COG-write / upload failure.
    """
    try:
        import numpy as np
    except Exception as exc:  # noqa: BLE001
        raise PostprocessTelemacError(
            "TELEMAC_DEPENDENCY_MISSING",
            message=f"numpy unavailable for TELEMAC WSE postprocess: {exc}",
        ) from exc

    is_depth = str(quantity).strip().lower() in ("depth", "h", "water_depth", "hmax")
    quantity_tag = "water_depth" if is_depth else "water_surface_elevation"

    slf = Path(slf_path)
    try:
        mesh = read_selafin(slf)
    except Exception as exc:  # noqa: BLE001
        raise PostprocessTelemacError(
            "TELEMAC_OUTPUT_READ_FAILED",
            message=f"could not parse SELAFIN {slf.name}: {exc}",
            details={"slf": str(slf)},
        ) from exc

    if is_depth:
        surf_var = _pick_named_var(mesh["varnames"], _DEPTH_VAR_KEYS, "H")
    else:
        surf_var = _pick_named_var(mesh["varnames"], _WSE_VAR_KEYS, "S")
    if surf_var is None or mesh["data"].get(surf_var) is None or mesh["data"][surf_var].size == 0:
        raise PostprocessTelemacError(
            "TELEMAC_OUTPUT_EMPTY",
            message=f"no {'WATER DEPTH' if is_depth else 'FREE SURFACE'} variable / "
            f"no time steps in {slf.name} (vars={mesh['varnames']})",
            details={"slf": str(slf), "varnames": mesh["varnames"]},
        )

    surf = np.asarray(mesh["data"][surf_var])  # (nframes, npoin), metres
    times = np.asarray(mesh["times"])
    x = np.asarray(mesh["x"])  # metres, MESH CRS (no reprojection)
    y = np.asarray(mesh["y"])

    # Wet mask from WATER DEPTH (so dry terrain is never read as a water surface).
    depth_var = _pick_named_var(mesh["varnames"], _DEPTH_VAR_KEYS, "H")
    wet_note = ""
    if is_depth:
        # depth IS the field; wet where depth > floor.
        field = surf
        wet = field > TELEMAC_WSE_WET_DEPTH_M
    elif depth_var is not None and mesh["data"].get(depth_var) is not None \
            and mesh["data"][depth_var].size == surf.size:
        depth = np.asarray(mesh["data"][depth_var])
        field = surf
        wet = depth > TELEMAC_WSE_WET_DEPTH_M
        wet_note = (
            f"masked to WATER DEPTH > {TELEMAC_WSE_WET_DEPTH_M} m (dry nodes NaN)"
        )
    else:
        # No depth variable to mask with -- honest fallback: take the raw max
        # free surface and WARN (dry-terrain contamination possible).
        field = surf
        wet = np.ones_like(surf, dtype=bool)
        wet_note = (
            "NO water-depth variable found to build a wet mask; raw max free "
            "surface used (dry-terrain elevation may leak into the raster)"
        )

    # per-node peak over ONLY the wet frames; never-wet nodes -> NaN (the
    # all-NaN-slice RuntimeWarning for a never-wet node is expected, not an error).
    import warnings

    masked = np.where(wet, field, np.nan)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        node_peak = np.nanmax(masked, axis=0) if masked.shape[0] else np.full(x.size, np.nan)
    finite = np.isfinite(node_peak)
    if not finite.any():
        raise PostprocessTelemacError(
            "TELEMAC_OUTPUT_EMPTY",
            message=f"no wet node in {slf.name}: WATER DEPTH never exceeded "
            f"{TELEMAC_WSE_WET_DEPTH_M} m anywhere (dry solve?)",
            details={"slf": str(slf), "wet_depth_m": TELEMAC_WSE_WET_DEPTH_M},
        )

    # honest scalar metrics over the wet field.
    wet_field = np.where(wet, field, np.nan)
    with np.errstate(all="ignore"):
        per_frame_max = np.array(
            [np.nanmax(wet_field[i]) if np.isfinite(wet_field[i]).any() else np.nan
             for i in range(wet_field.shape[0])]
        ) if wet_field.shape[0] else np.array([np.nan])
    finite_frames = np.isfinite(per_frame_max)
    if finite_frames.any():
        peak_i = int(np.nanargmax(per_frame_max))
        wse_max = float(per_frame_max[peak_i])
        wse_peak_time_s = float(times[peak_i]) if times.size > peak_i else None
    else:
        wse_max = float(np.nanmax(node_peak))
        wse_peak_time_s = None
    wse_min = float(np.nanmin(node_peak))

    xw = x[finite]
    yw = y[finite]
    vw = node_peak[finite]

    # metric grid in the mesh frame (metres) -- NO degree conversion.
    nn_m = _nn_spacing_m(x, y)
    res_m = max(float(target_ground_res_m), nn_m * 0.5)
    pad = max(50.0, nn_m)
    bbox = (float(xw.min() - pad), float(yw.min() - pad),
            float(xw.max() + pad), float(yw.max() + pad))
    w_m = bbox[2] - bbox[0]
    h_m = bbox[3] - bbox[1]
    import math

    ncols = min(max(int(round(w_m / res_m)), TELEMAC_MIN_PX_PER_SIDE), TELEMAC_MAX_PX_PER_SIDE)
    nrows = min(max(int(round(h_m / res_m)), TELEMAC_MIN_PX_PER_SIDE), TELEMAC_MAX_PX_PER_SIDE)
    if nrows * ncols > TELEMAC_MAX_TOTAL_CELLS:
        s = math.sqrt(TELEMAC_MAX_TOTAL_CELLS / float(nrows * ncols))
        nrows = max(TELEMAC_MIN_PX_PER_SIDE, int(nrows * s))
        ncols = max(TELEMAC_MIN_PX_PER_SIDE, int(ncols * s))
    shape = (nrows, ncols)
    # clip at ~2x mesh node spacing so interior cells between nodes are kept.
    clip_dist = 2.0 * nn_m
    try:
        # wet_floor very negative: WSE values (down-valley ~14 m) must NOT be
        # value-masked; the wet/dry decision was already made per node above.
        grid = _rasterize_nodes_to_grid(
            xw, yw, vw, bbox, shape, clip_dist, wet_floor=-1e30
        )
    except Exception as exc:  # noqa: BLE001
        raise PostprocessTelemacError(
            "TELEMAC_OUTPUT_READ_FAILED",
            message=f"WSE rasterization failed: {exc}",
        ) from exc

    # --- write the COG in the MESH CRS (no reprojection), stamped quantity tag - #
    try:
        import rasterio
        from rasterio.transform import from_bounds
    except Exception as exc:  # noqa: BLE001
        raise PostprocessTelemacError(
            "TELEMAC_DEPENDENCY_MISSING",
            message=f"rasterio unavailable for WSE COG: {exc}",
        ) from exc

    transform = from_bounds(bbox[0], bbox[1], bbox[2], bbox[3], shape[1], shape[0])
    dst_crs = f"EPSG:{int(mesh_epsg)}"
    cog = Path(cog_io._named_tmp("_telemac_wse.tif"))
    try:
        profile = {
            "driver": "COG",
            "crs": dst_crs,
            "transform": transform,
            "width": shape[1],
            "height": shape[0],
            "count": 1,
            "dtype": "float32",
            "nodata": float("nan"),
            "compress": "LZW",
        }
        with rasterio.open(cog, "w", **profile) as dst:
            dst.write(np.asarray(grid, dtype="float32"), 1)
            tagset = {
                "quantity": quantity_tag,
                "measured_quantity": quantity_tag,
            }
            if vertical_datum:
                tagset["vertical_datum"] = str(vertical_datum)
            dst.update_tags(**tagset)
            dst.update_tags(1, **tagset)
    except Exception as exc:  # noqa: BLE001
        cog_io.safe_unlink(cog)
        raise PostprocessTelemacError(
            "TELEMAC_COG_WRITE_FAILED",
            message=f"WSE COG write failed: {exc}",
            details={"crs": dst_crs},
        ) from exc

    dest_filename = "telemac_wse_max.tif" if not is_depth else "telemac_depth_max.tif"
    try:
        if _output_dir is not None:
            import shutil

            local = os.path.join(_output_dir, f"{dest_filename[:-4]}_{run_id}.tif")
            shutil.copyfile(cog, local)
            uri = local
        else:
            uri = cog_io.upload_cog(
                cog,
                run_id,
                runs_bucket,
                dest_filename=dest_filename,
                content_type="image/tiff",
                gs_backend="fsspec",
                gs_fallback_to_file=False,
                runs_bucket_default=RUNS_BUCKET_DEFAULT,
                log_label="TELEMAC WSE COG",
            )
    except CogIoError as exc:
        raise _reraise_cogio(exc) from exc
    finally:
        cog_io.safe_unlink(cog)

    label_txt = "Max water depth" if is_depth else "Max water-surface elevation"
    legend = LegendKey(
        kind="continuous",
        colormap="viridis" if is_depth else "blues",
        vmin=round(min(wse_min, wse_max), 4),
        vmax=round(wse_max, 4),
        units="m",
        label=f"{label_txt} (m{f', {vertical_datum}' if vertical_datum else ''})",
    )
    honesty_bits = [
        f"Peak {'depth' if is_depth else 'free-surface elevation'} over the run "
        f"({int(times.size)} output frame(s))",
    ]
    if wet_note:
        honesty_bits.append(wet_note)
    if mesh_frame_note:
        honesty_bits.append(mesh_frame_note)
    if times.size <= 3 and not is_depth:
        honesty_bits.append(
            "COARSE cadence: a low-frame result can UNDER-estimate the transient "
            "crest (the wave peak may fall between output frames)"
        )
    honesty = "; ".join(honesty_bits) + "."

    layer = TelemacWseLayerURI(
        layer_id=f"telemac-wse-max-{run_id}",
        name=f"{label_txt} ({reach_name})",
        layer_type="raster",
        uri=uri,
        style_preset=TELEMAC_WSE_STYLE_PRESET,
        role="primary",
        units="m",
        bbox=None,  # mesh-CRS metres, not EPSG:4326 lon/lat -> no zoom-to bbox
        legend=legend,
        fallback_note=honesty,
        wse_max_m=round(wse_max, 4),
        wse_peak_time_s=wse_peak_time_s,
        n_frames=int(times.size),
        quantity=quantity_tag,
        vertical_datum=vertical_datum,
        mesh_epsg=int(mesh_epsg),
    )
    metrics: dict[str, Any] = {
        "surf_var": surf_var.strip(),
        "quantity": quantity_tag,
        "wse_max_m": round(wse_max, 4),
        "wse_min_m": round(wse_min, 4),
        "wse_peak_time_s": wse_peak_time_s,
        "n_frames": int(times.size),
        "n_wet_nodes": int(finite.sum()),
        "npoin": int(mesh["npoin"]),
        "nelem": int(mesh["nelem"]),
        "mesh_epsg": int(mesh_epsg),
        "vertical_datum": vertical_datum,
        "mesh_nn_spacing_m": round(nn_m, 3),
        "grid_shape": [int(shape[0]), int(shape[1])],
        "bbox_mesh_m": [round(v, 3) for v in bbox],
        "honesty_label": honesty,
    }
    logger.info(
        "postprocess_telemac_wse run_id=%s var=%s wse_max=%.4g m peak_t=%ss "
        "n_wet=%d/%d n_frames=%d mesh_epsg=%s -> %s",
        run_id, surf_var.strip(), wse_max, wse_peak_time_s, int(finite.sum()),
        int(x.size), int(times.size), mesh_epsg, uri,
    )
    return [layer], metrics
