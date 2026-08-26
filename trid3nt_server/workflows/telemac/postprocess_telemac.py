"""TELEMAC-2D river-dye run-output postprocessing (river-dye reference scenario).

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
``continuous_dye_concentration``) as the map anchor + narration carrier; the time
animation rides the result SELAFIN, published as a ``layer_type="mesh"`` layer by
the emit-on-solve seam (the composer writes ``outputs.json`` with a ``kind="mesh"``
entry for ``r2d_river.slf``; ADR 0283). No per-frame COGs are written -- the mesh
already carries every frame.

Honesty floor (invariant 1): the dye scalars are computed with plain
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
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from trid3nt_contracts.telemac_contracts import (
    TELEMAC3D_STRATIFICATION_STYLE_PRESET,
    TELEMAC_AGITATION_STYLE_PRESET,
    TELEMAC_BED_EVOLUTION_STYLE_PRESET,
    TELEMAC_COASTAL_DEPTH_STYLE_PRESET,
    TELEMAC_DO_STYLE_PRESET,
    TELEMAC_DYE_STYLE_PRESET,
    TELEMAC_WAVE_STYLE_PRESET,
    TELEMAC_WSE_STYLE_PRESET,
    ArtemisAgitationLayerURI,
    Telemac3dLayerURI,
    TelemacCoastalLayerURI,
    TelemacDoLayerURI,
    TelemacDyeLayerURI,
    TelemacSedimentLayerURI,
    TelemacWaveLayerURI,
    TelemacWseLayerURI,
)
from trid3nt_contracts.execution import LegendKey

from trid3nt_server.workflows.shared import cog_io
from trid3nt_server.workflows.shared.cog_io import CogIoError
from trid3nt_server.workflows.shared.cog_io import RUNS_BUCKET_DEFAULT

__all__ = [
    "PostprocessTelemacError",
    "postprocess_telemac",
    "postprocess_telemac_deposition",
    "postprocess_telemac_wse",
    "postprocess_telemac_do",
    "postprocess_tomawac",
    "postprocess_artemis",
    "postprocess_telemac3d",
    "postprocess_coastal",
    "read_selafin",
    "TELEMAC_WAVE_STYLE_PRESET",
    "TELEMAC_AGITATION_STYLE_PRESET",
    "TELEMAC_DYE_STYLE_PRESET",
    "TELEMAC_BED_EVOLUTION_STYLE_PRESET",
    "TELEMAC_WSE_STYLE_PRESET",
    "TELEMAC_DO_STYLE_PRESET",
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
         "x": ndarray(npoin), "y": ndarray(npoin), "ikle": ndarray(nelem, ndp),
         "times": ndarray(nframes),
         "data": {varname: ndarray(nframes, npoin)}}

    The raster path uses only the variable NAMES + node coords + per-frame values
    (scattered-node interpolation), but the REAL element connectivity ``ikle``
    (0-based triangles) is returned so mesh-faithful renders can triangulate the
    channel along its true elements rather than an unconstrained Delaunay of the
    node cloud (which bridges river bends into a spurious fan). IPOBO is consumed
    for cursor alignment. Pure numpy; validated against a real solved ``r2d_river.slf``.
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
        # IKLE (nelem*ndp int32): the element connectivity, 1-based in SELAFIN.
        # Return it 0-based so a mesh-faithful render triangulates real elements.
        ikle = (np.frombuffer(_read_record(fh), dtype=">i4").astype("int64")
                .reshape(nelem, ndp) - 1)
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
        # The header's X-ORIGIN / Y-ORIGIN (IPARAM(3)/(4)), REPORTED and not
        # applied. ``x``/``y`` stay exactly as the file stores them because every
        # postprocessor adds the origin it recovers from the domain bbox, and
        # applying it here would double the offset on all of them. A reader that
        # wants absolute coordinates - the diagnostic sheet, or MDAL - adds these.
        "x_origin": int(iparam[2]),
        "y_origin": int(iparam[3]),
        "ikle": ikle,
        "times": np.asarray(times, dtype="float64"),
        "data": {v: (np.vstack(a) if a else np.empty((0, npoin))) for v, a in data.items()},
    }


def _pick_dye_var(varnames: list[str], *, prefer_sediment: bool = False) -> str | None:
    """The tracer variable name to rasterize, or None.

    Default (dye / decay runs): case-insensitive DYE, else a T-prefixed tracer
    (mirrors the worker entrypoint's tracer-sanity selection).

    ``prefer_sediment=True`` (GAIA sediment coupled run): the suspended sediment
    concentration rides as a SECOND telemac2d tracer, landing in
    ``r2d_river.slf`` as ``NCOH SEDIMENT1`` (g/l == kg/m3) alongside the
    required DYE companion. Pick that sediment tracer (a name carrying
    SEDIMENT / NCOH / COH), so the concentration COG is the SEDIMENT ribbon,
    not the conservative dye reference. Falls back to the dye pick when no
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
#: Static bed-elevation variable names (English + French). Read to reproduce the
#: worker's own ``bed > initial water line`` discrimination on the raster.
_BED_VAR_KEYS: tuple[str, ...] = ("BOTTOM", "FOND")


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


def _tri_from_ikle(ikle):
    """The element table as TRIANGLES (a quad element splits into two)."""
    import numpy as np

    ikle = np.asarray(ikle, dtype="int64")
    if ikle.ndim != 2 or ikle.shape[0] == 0:
        return np.empty((0, 3), dtype="int64")
    if ikle.shape[1] == 3:
        return ikle
    if ikle.shape[1] == 4:
        return np.vstack([ikle[:, [0, 1, 2]], ikle[:, [0, 2, 3]]])
    return ikle[:, :3]


def _rasterize_mesh_to_grid(lon, lat, ikle, vals, bbox, out_shape, wet_floor=0.0):
    """P1 (barycentric) interpolation of a nodal FEM field onto a regular grid.

    The TELEMAC solution IS piecewise-linear over its own elements, so evaluating
    each element's barycentric shape functions at the covered cell centres
    reproduces the solver's representation exactly - zero invented data, and the
    same thing QGIS's native mesh renderer draws. This REPLACES the nearest-node
    halo of :func:`_rasterize_nodes_to_grid` for open-water meshes, where nodes
    kilometres apart under a ~100 m halo published a lattice of isolated pixels
    instead of a field.

    A cell covered by no element stays NaN (the mesh footprint IS the clip - no
    distance threshold to tune). An element with ANY non-finite vertex value is
    SKIPPED: a masked node (dry, clamped land, never-wet) must not bleed a value
    across the element it touches. Sub-``wet_floor`` cells are NaN. Row 0 = NORTH.
    """
    import numpy as np

    nrows, ncols = int(out_shape[0]), int(out_shape[1])
    min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox)
    gdx = (max_lon - min_lon) / ncols
    gdy = (max_lat - min_lat) / nrows
    xc = min_lon + (np.arange(ncols) + 0.5) * gdx
    yc = max_lat - (np.arange(nrows) + 0.5) * gdy      # north -> south

    x = np.asarray(lon, dtype="float64")
    y = np.asarray(lat, dtype="float64")
    v = np.asarray(vals, dtype="float64")
    tri = _tri_from_ikle(ikle)
    if tri.size == 0 or tri.max() >= x.size:
        raise ValueError(
            f"element table does not index the {x.size} mesh nodes "
            f"(nelem={tri.shape[0]}, max index={tri.max() if tri.size else -1})")

    grid = np.full((nrows, ncols), np.nan, dtype="float64")
    x0, x1, x2 = x[tri[:, 0]], x[tri[:, 1]], x[tri[:, 2]]
    y0, y1, y2 = y[tri[:, 0]], y[tri[:, 1]], y[tri[:, 2]]
    v0, v1, v2 = v[tri[:, 0]], v[tri[:, 1]], v[tri[:, 2]]
    det = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)

    keep = np.isfinite(v0) & np.isfinite(v1) & np.isfinite(v2) & (np.abs(det) > 0.0)
    # cell-index window per element (half-cell offset: xc[j] = min_lon+(j+0.5)*gdx)
    txmin = np.minimum(np.minimum(x0, x1), x2)
    txmax = np.maximum(np.maximum(x0, x1), x2)
    tymin = np.minimum(np.minimum(y0, y1), y2)
    tymax = np.maximum(np.maximum(y0, y1), y2)
    j_lo = np.ceil((txmin - min_lon) / gdx - 0.5).astype("int64")
    j_hi = np.floor((txmax - min_lon) / gdx - 0.5).astype("int64")
    i_lo = np.ceil((max_lat - tymax) / gdy - 0.5).astype("int64")
    i_hi = np.floor((max_lat - tymin) / gdy - 0.5).astype("int64")
    np.clip(j_lo, 0, ncols - 1, out=j_lo)
    np.clip(j_hi, 0, ncols - 1, out=j_hi)
    np.clip(i_lo, 0, nrows - 1, out=i_lo)
    np.clip(i_hi, 0, nrows - 1, out=i_hi)

    eps = -1e-9
    for k in np.flatnonzero(keep):
        jl, jh, il, ih = int(j_lo[k]), int(j_hi[k]), int(i_lo[k]), int(i_hi[k])
        if jh < jl or ih < il:
            continue
        gx = xc[jl:jh + 1][None, :]
        gy = yc[il:ih + 1][:, None]
        d = det[k]
        l0 = ((y1[k] - y2[k]) * (gx - x2[k]) + (x2[k] - x1[k]) * (gy - y2[k])) / d
        l1 = ((y2[k] - y0[k]) * (gx - x2[k]) + (x0[k] - x2[k]) * (gy - y2[k])) / d
        l2 = 1.0 - l0 - l1
        inside = (l0 >= eps) & (l1 >= eps) & (l2 >= eps)
        if not inside.any():
            continue
        block = grid[il:ih + 1, jl:jh + 1]
        interp = l0 * v0[k] + l1 * v1[k] + l2 * v2[k]
        np.copyto(block, np.broadcast_to(interp, block.shape), where=inside)

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
    from the SELAFIN mesh sibling that ``open_case_in_qgis`` discovers next to
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
    erodible: bool = False,
) -> tuple[list[TelemacSedimentLayerURI], dict[str, Any]]:
    """Rasterize the GAIA final CUMUL BED EVOL field into ONE bed-evolution COG.

    ``erodible=False`` (v1 supply-limited): renders only the positive DEPOSITION
    tongue (nothing erodes) and errors if nothing deposited. ``erodible=True`` (v2
    morphodynamics): renders the SIGNED bed change - SCOUR (negative) and
    DEPOSITION (positive) - on the diverging ramp centered on 0, reports
    ``max_scour_mm`` beside ``max_deposition_mm``, and is valid as long as the bed
    moved either way.

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
    # v2 erodible-bed morphodynamics: the SCOUR limb is the point of the run, so
    # the deepest scour (most-negative bed change, reported as a positive mm depth)
    # is measured beside the deposition peak.
    scour_only_mm = np.where(node_final_mm < 0.0, -node_final_mm, 0.0)
    max_scour_mm = float(scour_only_mm.max()) if scour_only_mm.size else 0.0

    from pyproj import Transformer

    back = Transformer.from_crs(int(utm_epsg), 4326, always_xy=True)
    lon, lat = back.transform(x_utm, y_utm)
    lon = np.asarray(lon)
    lat = np.asarray(lat)

    if erodible:
        # SCOUR + DEPOSITION both matter: valid unless the bed did not move at all.
        if max_dep_mm <= 0.0 and max_scour_mm <= 0.0:
            raise PostprocessTelemacError(
                "TELEMAC_OUTPUT_EMPTY",
                message=f"no bed evolution anywhere in {slf.name} "
                f"(erodible-bed run neither scoured nor deposited measurably)",
                details={"max_deposition_mm": max_dep_mm,
                         "max_scour_mm": max_scour_mm},
            )
    elif max_dep_mm <= 0.0:
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
    try:
        if erodible:
            # rasterize the SIGNED bed change (scour negative / deposition
            # positive) with wet_floor -1e30 so NO node is value-masked - the
            # diverging ramp centered on 0 reads scour (blue) AND deposition (red).
            grid = _rasterize_nodes_to_grid(
                lon, lat, node_final_mm, bbox, shape, clip_dist_deg,
                wet_floor=-1e30)
        else:
            # v1 supply-limited: rasterize the DEPOSITION (positive mm) field;
            # erosion/zero -> NaN so the diverging ramp reads the tongue cleanly.
            # wet_floor tiny so a mm-scale tongue is not clipped.
            grid = _rasterize_nodes_to_grid(
                lon, lat, dep_only_mm, bbox, shape, clip_dist_deg,
                wet_floor=max(1e-4, 0.02 * max_dep_mm))
    except Exception as exc:  # noqa: BLE001
        raise PostprocessTelemacError(
            "TELEMAC_OUTPUT_READ_FAILED",
            message=f"bed-evolution rasterization failed: {exc}",
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
    # diverging legend centered on 0 (rdbu midpoint = 0 bed change), mm units. For
    # the erodible signed field the range is symmetric on the LARGER limb but capped
    # at the 99th percentile of |bed change| so a single inflow-boundary bedload
    # pile-up node does not wash the interior scour/deposition pattern off the ramp.
    if erodible:
        both = np.abs(node_final_mm)
        robust = float(np.percentile(both, 99)) if both.size else 0.0
        vext = round(max(min(max(max_dep_mm, max_scour_mm), max(robust, 1e-3)),
                         1e-3), 4)
    else:
        vext = round(max(max_dep_mm, 1e-3), 4)
    legend = LegendKey(
        kind="continuous", colormap="rdbu", vmin=-vext, vmax=vext, units="mm",
        label="Bed evolution (mm): scour < 0 < deposition"
        if erodible else "Bed evolution / deposition (mm)",
    )
    if erodible:
        honesty = (
            "Event-scale bed evolution (mm) under active bedload morphodynamics "
            "(Meyer-Peter-Mueller family), amplified by a MORPHOLOGICAL FACTOR - a "
            "planning-grade scour/deposition PATTERN, not a calibrated scour depth. "
            "Scour is negative bed change, deposition positive, on a diverging ramp "
            "centered at 0. A localized bedload pile-up at the sediment inflow "
            "boundary is a known GAIA artifact (capped off the diverging ramp). "
            "Grain size is a demo default / user override (no site bed-composition "
            "fetcher exists)."
        )
        layer_name = f"Bed evolution / scour ({reach_name})"
        layer_id = f"telemac-bed-evolution-{run_id}"
    else:
        honesty = (
            "Event-scale deposition (mm), not annual morphology: a supply-limited "
            "GAIA run (bed initial thickness 0) - only the injected sediment pulse "
            "can deposit, nothing erodes. Grain size is a demo default / user "
            "override (no site bed-composition fetcher exists), not a site measurement."
        )
        layer_name = f"Sediment deposition ({reach_name})"
        layer_id = f"telemac-sediment-deposition-{run_id}"
    layer = TelemacSedimentLayerURI(
        layer_id=layer_id,
        name=layer_name,
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
        max_scour_mm=round(max_scour_mm, 4) if erodible else None,
        grain_size_um=wsm.get("grain_size_um"),
        sediment_type=wsm.get("sediment_type"),
    )
    metrics: dict[str, Any] = {
        "evol_var": evol_var.strip(),
        "max_deposition_mm": round(max_dep_mm, 4),
        "max_scour_mm": round(max_scour_mm, 4) if erodible else None,
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


# --------------------------------------------------------------------------- #
# WAQTEL O2: the dissolved-oxygen SAG - steady-state DO COG + the sag curve.
# --------------------------------------------------------------------------- #
#: DISSOLVED O2 / ORGANIC LOAD variable names WAQTEL's O2 module writes (nametrac
#: strings, pinned by the 2026-08-07 in-image smoke).
_DO_VAR_KEYS: tuple[str, ...] = ("DISSOLVED O2", "O2 DISSOUS", "DISSOLVED OXYGEN")
_BOD_VAR_KEYS: tuple[str, ...] = ("ORGANIC LOAD", "CHARGE ORGANIQUE")


def _downstream_coordinate(x, y, centerline_utm=None):
    """Per-node DOWNSTREAM distance (m) + a label for how it was derived.

    With ``centerline_utm`` (an ordered [(x,y), ...] polyline) each node is
    projected to the nearest centerline segment and assigned that segment's
    cumulative arc length - the true along-reach distance. Without it, the nodes
    are projected onto their PRINCIPAL FLOW AXIS (PCA first component), which is
    exact for a straight channel (the S-P V&V) and a labelled approximation for a
    gently sinuous reach. Returns ``(s_m ndarray, label)``.
    """
    import numpy as np

    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    if centerline_utm is not None and len(centerline_utm) >= 2:
        cl = np.asarray(centerline_utm, dtype=float)
        seg = np.hypot(np.diff(cl[:, 0]), np.diff(cl[:, 1]))
        cum = np.concatenate([[0.0], np.cumsum(seg)])
        s = np.zeros(x.size)
        for i in range(x.size):
            ax = cl[:-1, 0]; ay = cl[:-1, 1]
            bx = cl[1:, 0]; by = cl[1:, 1]
            dx = bx - ax; dy = by - ay
            L2 = dx * dx + dy * dy
            t = np.clip(((x[i] - ax) * dx + (y[i] - ay) * dy) / np.maximum(L2, 1e-9), 0.0, 1.0)
            px = ax + t * dx; py = ay + t * dy
            d2 = (px - x[i]) ** 2 + (py - y[i]) ** 2
            k = int(np.argmin(d2))
            s[i] = cum[k] + t[k] * seg[k]
        return s - s.min(), "along-centerline arc length"
    # PCA principal axis
    cx = x - x.mean(); cy = y - y.mean()
    cov = np.cov(np.vstack([cx, cy]))
    w, v = np.linalg.eigh(cov)
    axis = v[:, int(np.argmax(w))]
    s = cx * axis[0] + cy * axis[1]
    # orient so the far end is positive (downstream = increasing s)
    if s.max() + s.min() < 0:
        s = -s
    return s - s.min(), "principal-flow-axis projection (straight-line proxy)"


def postprocess_telemac_do(
    slf_path: str | Path,
    *,
    run_id: str,
    utm_epsg: int,
    reach_name: str = "river",
    saturation_mgl: float = 9.0,
    upstream_do_mgl: float | None = None,
    bod_upstream_mgl: float | None = None,
    standard_mgl: float = 5.0,
    centerline_utm: list | None = None,
    n_sag_bins: int = 60,
    runs_bucket: str | None = None,
    target_ground_res_m: float = TELEMAC_TARGET_GROUND_RES_M,
    _output_dir: str | None = None,
) -> tuple[list[TelemacDoLayerURI], dict[str, Any]]:
    """Rasterize a WAQTEL O2 sag run into a steady-state DISSOLVED-O2 COG + curve.

    Reads ``slf_path`` (``r2d_river.slf``), takes the STEADY-STATE (last frame)
    DISSOLVED O2 field (the worst-case sag for a continuous discharge), reprojects
    the mesh nodes ``utm_epsg`` -> EPSG:4326, rasterizes the DO field onto an
    adaptive 4326 grid clipped to the channel, writes + uploads ONE COG
    (``telemac_do_field.tif``) and returns ``([TelemacDoLayerURI], metrics)``. It
    also bins DO + CBOD by downstream distance into the along-reach SAG CURVE the
    dock chart plots against the DO standard, and computes the sag minimum + its
    location (Invariant 1 - typed, never invented).

    ``_output_dir`` (TEST/offline hook): when set the COG is written locally and
    its path returned instead of uploading (mirrors ``postprocess_telemac_wse``).
    """
    try:
        import numpy as np
        from pyproj import Transformer  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        raise PostprocessTelemacError(
            "TELEMAC_DEPENDENCY_MISSING",
            message=f"numpy/pyproj unavailable for TELEMAC DO postprocess: {exc}",
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

    import numpy as np

    do_var = _pick_named_var(mesh["varnames"], _DO_VAR_KEYS, "\x00")
    if do_var is None or mesh["data"].get(do_var) is None or mesh["data"][do_var].size == 0:
        raise PostprocessTelemacError(
            "TELEMAC_OUTPUT_EMPTY",
            message=f"no DISSOLVED O2 tracer / no time steps in {slf.name} "
            f"(vars={mesh['varnames']}) - is this a WAQTEL O2 (do_sag) run?",
            details={"slf": str(slf), "varnames": mesh["varnames"]},
        )
    bod_var = _pick_named_var(mesh["varnames"], _BOD_VAR_KEYS, "\x00")

    do = np.asarray(mesh["data"][do_var])          # (nframes, npoin) mg/L
    times = np.asarray(mesh["times"])
    x_utm = np.asarray(mesh["x"]); y_utm = np.asarray(mesh["y"])
    do_field = do[-1]                               # steady-state (last frame)
    bod_field = (np.asarray(mesh["data"][bod_var])[-1]
                 if bod_var is not None and mesh["data"].get(bod_var) is not None
                 and mesh["data"][bod_var].size else None)

    # wet mask so dry nodes never paint a DO value
    depth_var = _pick_named_var(mesh["varnames"], _DEPTH_VAR_KEYS, "H")
    if depth_var is not None and mesh["data"].get(depth_var) is not None \
            and mesh["data"][depth_var].size:
        wet = np.asarray(mesh["data"][depth_var])[-1] > TELEMAC_WSE_WET_DEPTH_M
    else:
        wet = np.ones(x_utm.size, dtype=bool)
    if not wet.any():
        raise PostprocessTelemacError(
            "TELEMAC_OUTPUT_EMPTY",
            message=f"no wet node in {slf.name} (dry solve?)",
        )

    # --- the along-reach sag curve (DO + CBOD binned by downstream distance) --- #
    s_m, s_label = _downstream_coordinate(x_utm[wet], y_utm[wet], centerline_utm)
    do_w = do_field[wet]
    bod_w = bod_field[wet] if bod_field is not None else None
    nb = max(int(n_sag_bins), 8)
    edges = np.linspace(0.0, float(s_m.max()) if s_m.max() > 0 else 1.0, nb + 1)
    ctr = 0.5 * (edges[:-1] + edges[1:])
    idx = np.clip(np.digitize(s_m, edges) - 1, 0, nb - 1)
    curve_x, curve_do, curve_bod = [], [], []
    for b in range(nb):
        m = idx == b
        if not m.any():
            continue
        curve_x.append(float(ctr[b]))
        curve_do.append(float(np.mean(do_w[m])))
        curve_bod.append(float(np.mean(bod_w[m])) if bod_w is not None else 0.0)
    if len(curve_x) < 3:
        raise PostprocessTelemacError(
            "TELEMAC_OUTPUT_EMPTY",
            message=f"DO sag curve degenerate ({len(curve_x)} bins) in {slf.name}",
        )
    do_arr = np.asarray(curve_do)
    sag_i = int(do_arr.argmin())
    do_min = float(do_arr[sag_i])
    do_min_dist = float(curve_x[sag_i])
    do_up = float(upstream_do_mgl) if upstream_do_mgl is not None else float(curve_do[0])
    violates = bool(do_min < float(standard_mgl))

    # --- rasterize the steady-state DO field onto a 4326 grid ----------------- #
    from pyproj import Transformer
    back = Transformer.from_crs(int(utm_epsg), 4326, always_xy=True)
    lon, lat = back.transform(x_utm, y_utm)
    lon = np.asarray(lon); lat = np.asarray(lat)
    # mask dry nodes to NaN so the raster only covers the wetted channel
    do_masked = np.where(wet, do_field, np.nan)
    finite = np.isfinite(do_masked)
    lon_f = lon[finite]; lat_f = lat[finite]; do_f = do_masked[finite]

    pad = 0.0009
    bbox = (float(lon_f.min() - pad), float(lat_f.min() - pad),
            float(lon_f.max() + pad), float(lat_f.max() + pad))
    shape = _grid_shape(bbox, target_ground_res_m)
    clip_dist_deg = 1.5 * max((bbox[2] - bbox[0]) / shape[1], (bbox[3] - bbox[1]) / shape[0])
    try:
        # wet_floor very negative: DO (0..Cs mg/L) must NOT be value-masked; the
        # wet/dry decision was already made per node.
        grid = _rasterize_nodes_to_grid(
            lon_f, lat_f, do_f, bbox, shape, clip_dist_deg, wet_floor=-1e30)
    except Exception as exc:  # noqa: BLE001
        raise PostprocessTelemacError(
            "TELEMAC_OUTPUT_READ_FAILED", message=f"DO rasterization failed: {exc}")

    from rasterio.transform import from_bounds
    transform = from_bounds(bbox[0], bbox[1], bbox[2], bbox[3], shape[1], shape[0])
    try:
        cog = cog_io.write_cog_4326_from_grid(
            grid, src_crs="EPSG:4326", src_transform=transform, reproject=False,
            crs_roundtrip_guard=True, dst_suffix="_telemac_do_4326.tif")
    except CogIoError as exc:
        raise _reraise_cogio(exc) from exc

    try:
        if _output_dir is not None:
            import shutil
            uri = os.path.join(_output_dir, f"telemac_do_field_{run_id}.tif")
            shutil.copyfile(cog, uri)
        else:
            uri = cog_io.upload_cog(
                cog, run_id, runs_bucket, dest_filename="telemac_do_field.tif",
                content_type="image/tiff", gs_backend="fsspec",
                gs_fallback_to_file=False, runs_bucket_default=RUNS_BUCKET_DEFAULT,
                log_label="TELEMAC DO COG")
    except CogIoError as exc:
        raise _reraise_cogio(exc) from exc
    finally:
        cog_io.safe_unlink(cog)

    legend = LegendKey(
        kind="continuous", colormap="rdylbu",
        vmin=round(min(do_min, float(standard_mgl)), 3),
        vmax=round(max(float(saturation_mgl), float(np.nanmax(do_f))), 3),
        units="mg/L", label="Dissolved oxygen (mg/L)")
    honesty = (
        f"Steady-state DO field (last of {int(times.size)} frame(s)); downstream "
        f"distance by {s_label}. Streeter-Phelps O2 kinetics on an idealized "
        f"planar channel bed (screening/permit grade, not a calibrated study)."
        + (f" Sag min {do_min:.2f} mg/L VIOLATES the {standard_mgl:g} mg/L "
           "standard." if violates else
           f" Sag min {do_min:.2f} mg/L meets the {standard_mgl:g} mg/L standard.")
    )
    layer = TelemacDoLayerURI(
        layer_id=f"telemac-do-field-{run_id}",
        name=f"Dissolved oxygen sag ({reach_name})",
        layer_type="raster", uri=uri, style_preset=TELEMAC_DO_STYLE_PRESET,
        role="primary", units="mg/L", bbox=bbox, legend=legend,
        fallback_note=honesty,
        do_min_mgl=round(do_min, 4),
        do_min_distance_m=round(do_min_dist, 1),
        do_upstream_mgl=round(do_up, 4),
        do_saturation_mgl=round(float(saturation_mgl), 4),
        do_standard_mgl=round(float(standard_mgl), 4),
        do_violates_standard=violates,
        bod_upstream_mgl=(round(float(bod_upstream_mgl), 4)
                          if bod_upstream_mgl is not None else
                          (round(float(curve_bod[0]), 4) if curve_bod else None)),
        sag_curve_distance_m=[round(v, 1) for v in curve_x],
        sag_curve_do_mgl=[round(v, 4) for v in curve_do],
        sag_curve_bod_mgl=[round(v, 4) for v in curve_bod],
    )
    metrics: dict[str, Any] = {
        "do_var": do_var.strip(),
        "bod_var": bod_var.strip() if bod_var else None,
        "do_min_mgl": round(do_min, 4),
        "do_min_distance_m": round(do_min_dist, 1),
        "do_upstream_mgl": round(do_up, 4),
        "do_saturation_mgl": round(float(saturation_mgl), 4),
        "do_standard_mgl": round(float(standard_mgl), 4),
        "do_violates_standard": violates,
        "n_frames": int(times.size),
        "npoin": int(mesh["npoin"]),
        "nelem": int(mesh["nelem"]),
        "utm_epsg": int(utm_epsg),
        "bbox": list(bbox),
        "crs": "EPSG:4326",
        "downstream_coord": s_label,
        "sag_curve_distance_m": [round(v, 1) for v in curve_x],
        "sag_curve_do_mgl": [round(v, 4) for v in curve_do],
        "sag_curve_bod_mgl": [round(v, 4) for v in curve_bod],
        "honesty_label": honesty,
    }
    logger.info(
        "postprocess_telemac_do run_id=%s do_var=%s do_min=%.3g mg/L at %.0fm "
        "violates=%s n_frames=%d -> %s",
        run_id, do_var.strip(), do_min, do_min_dist, violates, int(times.size), uri)
    return [layer], metrics


# --------------------------------------------------------------------------- #
# TOMAWAC significant-wave-height (Hs) - the spectral-wave COG.
# --------------------------------------------------------------------------- #
#: Hs (m) below which a wet node is treated as "flat water" for the extent
#: metrics / detection floor. Tiny absolute floor separates a real wave field
#: from a genuinely empty solve.
_HS_WET_FLOOR: float = 1e-3


def _local_mesh_origin(domain_bbox: Any, utm_epsg: int, *,
                       required: bool = False,
                       context: str = "this postprocess") -> tuple[float, float]:
    """The UTM corner a LOCAL-coordinate mesh was built from. The ONE origin.

    Every open-water TELEMAC build lays its grid with node 0 at the AOI's SW
    corner, so the result SELAFIN carries local metres and the corner has to be
    added back before reprojection. Getting this wrong does not fail: it silently
    lands the field at the UTM zone's false origin, thousands of km from the
    domain. Three copies of the arithmetic is three places for that to happen, so
    there is one.

    ABSENCE and MALFORMATION are different facts. A build with no AOI (the
    geography-free idealized basin) has no corner to add and its coordinates are
    already what they are; ``required=True`` says this reader cannot place its
    mesh without one and refuses instead. A bbox that is PRESENT but not four
    numeric corners is a refusal either way - reading it as absent would put a
    real domain at the false origin, which is the bug this guards.
    """
    if domain_bbox is None:
        if required:
            raise PostprocessTelemacError(
                "TELEMAC_PARAMS_INVALID",
                message=f"{context} needs the 4326 domain bbox (min_lon, min_lat, "
                "max_lon, max_lat) to place the local-coordinate mesh; none was "
                f"supplied for utm_epsg={utm_epsg}.",
                details={"utm_epsg": utm_epsg, "domain_bbox": None},
            )
        return (0.0, 0.0)

    corners = tuple(domain_bbox)
    try:
        if len(corners) != 4:
            raise ValueError(f"{len(corners)} corners, expected 4")
        west, south, east, north = (float(v) for v in corners)
    except (TypeError, ValueError) as exc:
        raise PostprocessTelemacError(
            "TELEMAC_PARAMS_INVALID",
            message=f"{context} was handed a malformed domain bbox "
            f"{domain_bbox!r}: {exc}. It must be four numeric 4326 corners "
            "(min_lon, min_lat, max_lon, max_lat).",
            details={"utm_epsg": utm_epsg, "domain_bbox": repr(domain_bbox)},
        ) from exc

    from pyproj import Transformer

    fwd = Transformer.from_crs(4326, int(utm_epsg), always_xy=True)
    x0, y0 = fwd.transform(west, south)
    x1, y1 = fwd.transform(east, north)
    return (min(x0, x1), min(y0, y1))


def postprocess_tomawac(
    slf_path: str | Path,
    *,
    run_id: str,
    utm_epsg: int,
    reach_name: str = "wave_field",
    wave_mode: str = "fetch_growth",
    domain_bbox: Sequence[float] | None = None,
    runs_bucket: str | None = None,
    target_ground_res_m: float = 30.0,
) -> tuple[list[TelemacWaveLayerURI], dict[str, Any]]:
    """Rasterize a solved TOMAWAC result into ONE significant-wave-height COG.

    Reads ``slf_path`` (the TOMAWAC 2D result SELAFIN), picks the significant
    wave height variable (``WAVE HEIGHT HM0``, mnemonic ``HM0``), takes the FINAL
    frame (the steady sea state), reprojects the mesh nodes ``utm_epsg`` ->
    EPSG:4326, rasterizes Hs onto an adaptive 4326 grid clipped to the wet domain,
    writes + uploads ONE COG (``tomawac_hs.tif``), and returns
    ``([TelemacWaveLayerURI], metrics)``. The time evolution plays from the
    SELAFIN mesh sibling ``export_case_to_qgis`` discovers via
    ``TELEMAC_WAVE_STYLE_PRESET`` (no per-frame COGs).

    ``domain_bbox`` is the 4326 AOI the REAL-lake grid was built over, and it is
    what georeferences the result. The wave worker builds its grid in LOCAL
    coordinates (node 0 at the AOI's SW corner) and only offsets by the corner
    when it samples the bed, so the result SELAFIN carries local metres - exactly
    as the coastal build does. Without the bbox those metres reproject as ABSOLUTE
    UTM and the Hs COG lands at the zone's false origin, thousands of km from the
    lake, while the bed COG beside it sits correctly on the water. The IDEALIZED
    basin has no geographic footprint at all, so it passes no bbox and its layer
    stays where the geography-free grid puts it - which its own label already says.

    Honesty floor (invariant 1): every wave scalar is plain arithmetic over the
    Hs field -- no LLM. The COG carries a spectral-screening label so a demo run
    is never read as a calibrated hindcast.

    Raises ``PostprocessTelemacError`` on any read / rasterize / COG failure.
    """
    try:
        import numpy as np
        from pyproj import Transformer  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        raise PostprocessTelemacError(
            "TELEMAC_DEPENDENCY_MISSING",
            message=f"numpy/pyproj unavailable for TOMAWAC postprocess: {exc}",
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

    import numpy as np

    hs_var = None
    for v in mesh["varnames"]:
        u = v.strip().upper()
        if "HM0" in u or "WAVE HEIGHT" in u:
            hs_var = v
            break
    if hs_var is None or mesh["data"].get(hs_var) is None or mesh["data"][hs_var].size == 0:
        raise PostprocessTelemacError(
            "TELEMAC_OUTPUT_EMPTY",
            message=f"no WAVE HEIGHT HM0 field / no time steps in {slf.name} "
            f"(vars={mesh['varnames']})",
            details={"slf": str(slf), "varnames": mesh["varnames"]},
        )

    hs = np.asarray(mesh["data"][hs_var])          # (nframes, npoin)
    node_hs = hs[-1]                                # final frame = steady sea state
    x_utm = np.asarray(mesh["x"])
    y_utm = np.asarray(mesh["y"])
    finite = np.isfinite(node_hs)
    hs_max = float(np.nanmax(node_hs[finite])) if finite.any() else 0.0
    hs_mean = float(np.nanmean(node_hs[finite & (node_hs > _HS_WET_FLOOR)])) \
        if (finite & (node_hs > _HS_WET_FLOOR)).any() else 0.0
    if hs_max < _HS_WET_FLOOR:
        raise PostprocessTelemacError(
            "TELEMAC_OUTPUT_EMPTY",
            message=f"Hs never exceeded {_HS_WET_FLOOR} m anywhere in {slf.name} "
            f"(peak {hs_max:.4g}) -- a dry/zero-wave solve?",
            details={"hs_max_m": hs_max},
        )

    from pyproj import Transformer

    x_org, y_org = _local_mesh_origin(domain_bbox, utm_epsg)
    back = Transformer.from_crs(int(utm_epsg), 4326, always_xy=True)
    lon, lat = back.transform(x_utm + x_org, y_utm + y_org)
    lon = np.asarray(lon)
    lat = np.asarray(lat)

    pad = 0.0009
    bbox = (
        float(lon.min() - pad), float(lat.min() - pad),
        float(lon.max() + pad), float(lat.max() + pad),
    )
    shape = _grid_shape(bbox, target_ground_res_m)
    try:
        # barycentric over the wave mesh's own elements: a ~3 km TOMAWAC grid under
        # a nearest-node halo published isolated pixels, not an Hs field.
        # wet_floor tiny so a small-Hs run is not clipped; NaN nodes drop out.
        grid = _rasterize_mesh_to_grid(
            lon, lat, mesh["ikle"], node_hs, bbox, shape, wet_floor=_HS_WET_FLOOR)
    except Exception as exc:  # noqa: BLE001
        raise PostprocessTelemacError(
            "TELEMAC_OUTPUT_READ_FAILED",
            message=f"Hs rasterization failed: {exc}",
        ) from exc

    from rasterio.transform import from_bounds

    transform = from_bounds(bbox[0], bbox[1], bbox[2], bbox[3], shape[1], shape[0])
    try:
        cog = cog_io.write_cog_4326_from_grid(
            grid, src_crs="EPSG:4326", src_transform=transform,
            reproject=False, crs_roundtrip_guard=True,
            dst_suffix="_tomawac_hs_4326.tif",
        )
    except CogIoError as exc:
        raise _reraise_cogio(exc) from exc
    try:
        uri = cog_io.upload_cog(
            cog, run_id, runs_bucket,
            dest_filename="tomawac_hs.tif",
            content_type="image/tiff", gs_backend="fsspec",
            gs_fallback_to_file=False, runs_bucket_default=RUNS_BUCKET_DEFAULT,
            log_label="TOMAWAC Hs COG",
        )
    except CogIoError as exc:
        raise _reraise_cogio(exc) from exc
    finally:
        cog_io.safe_unlink(cog)

    vmax = round(max(hs_max, _HS_WET_FLOOR), 4)
    legend = LegendKey(
        kind="continuous", colormap="viridis", vmin=0.0, vmax=vmax, units="m",
        label="Significant wave height Hs (m)",
    )
    honesty = (
        "Spectral-wave screening (TOMAWAC WAM4 physics): significant wave height "
        "Hs over the domain. A planning-grade wave field driven by a prescribed "
        "steady wind / boundary swell, not a calibrated hindcast."
    )
    layer = TelemacWaveLayerURI(
        layer_id=f"tomawac-hs-{run_id}",
        name=f"Significant wave height ({reach_name})",
        layer_type="raster",
        uri=uri,
        style_preset=TELEMAC_WAVE_STYLE_PRESET,
        role="primary",
        units="m",
        bbox=bbox,
        legend=legend,
        fallback_note=honesty,
        hs_max_m=round(hs_max, 4),
        hs_mean_m=round(hs_mean, 4),
        wave_mode=wave_mode,
    )
    metrics: dict[str, Any] = {
        "hs_var": hs_var.strip(),
        "hs_max_m": round(hs_max, 4),
        "hs_mean_m": round(hs_mean, 4),
        "wave_mode": wave_mode,
        "npoin": int(mesh["npoin"]),
        "nelem": int(mesh["nelem"]),
        "utm_epsg": int(utm_epsg),
        "bbox": list(bbox),
        "crs": "EPSG:4326",
        "honesty_label": honesty,
    }
    logger.info(
        "postprocess_tomawac run_id=%s hs_var=%s hs_max=%.4g m mode=%s -> %s",
        run_id, hs_var.strip(), hs_max, wave_mode, uri,
    )
    return [layer], metrics


# --------------------------------------------------------------------------- #
# ARTEMIS harbour agitation (Kd = Hs/H0) - the phase-resolving COG.
# --------------------------------------------------------------------------- #
#: Kd (agitation coefficient) below which a wet node is treated as "flat water"
#: for the detection floor. Tiny absolute floor separates a real agitation field
#: from a genuinely empty solve.
_KD_WET_FLOOR: float = 1e-3


def postprocess_artemis(
    slf_path: str | Path,
    *,
    run_id: str,
    utm_epsg: int | None,
    incident_hs_m: float,
    request_bbox: Sequence[float] | None = None,
    reach_name: str = "harbor_agitation",
    wave_mode: str = "diffraction",
    runs_bucket: str | None = None,
    target_ground_res_m: float = 20.0,
) -> tuple[list[ArtemisAgitationLayerURI], dict[str, Any]]:
    """Rasterize a solved ARTEMIS agitation field into ONE Kd (Hs/H0) COG.

    Reads ``slf_path`` (the single-frame ``agit_field.slf`` the worker re-emits),
    picks the ``WAVE HEIGHT`` variable, normalizes it by the incident wave height
    ``incident_hs_m`` to the dimensionless agitation coefficient Kd = Hs/H0,
    reprojects the mesh nodes ``utm_epsg`` -> EPSG:4326 (real-bathy path) or keeps
    the local metres frame (idealized analytic path, ``utm_epsg`` None), rasterizes

    Georeferencing (real-bathy path): the worker meshes in a LOCAL UTM frame whose
    origin is the AOI SW corner -- it subtracts ``(x0m, y0m) = min-easting,
    min-northing`` from every node so the SELAFIN float32 coordinates keep sub-metre
    precision (a raw UTM easting ~4e5 loses ~0.03 m of precision in float32). Those
    LOCAL metres (x in [0, Lx], y in [0, Ly]) are what the result mesh carries, so
    this postprocess MUST add the same origin offset back before the UTM->4326
    inverse or the field georeferences to the UTM-zone origin (near lon -91, lat 0)
    instead of the real harbour. The offset is reconstructed sub-mm from
    ``request_bbox`` SW corner (the exact value the mesh builder subtracted:
    ``Transformer(4326->utm_epsg).transform(min_lon, min_lat)``).
    Kd onto an adaptive grid clipped to the wet domain, writes + uploads ONE COG
    (``artemis_agitation.tif``), and returns ``([ArtemisAgitationLayerURI], metrics)``.

    Honesty floor (invariant 1): every agitation scalar is plain arithmetic over
    the Hs field -- no LLM. The COG carries a phase-resolving-screening label.

    Raises ``PostprocessTelemacError`` on any read / rasterize / COG failure.
    """
    try:
        import numpy as np
        from pyproj import Transformer  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        raise PostprocessTelemacError(
            "TELEMAC_DEPENDENCY_MISSING",
            message=f"numpy/pyproj unavailable for ARTEMIS postprocess: {exc}",
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

    import numpy as np

    hs_var = None
    for v in mesh["varnames"]:
        u = v.strip().upper()
        if "WAVE HEIGHT" in u or u in ("HS", "HM0"):
            hs_var = v
            break
    if hs_var is None or mesh["data"].get(hs_var) is None or mesh["data"][hs_var].size == 0:
        raise PostprocessTelemacError(
            "TELEMAC_OUTPUT_EMPTY",
            message=f"no WAVE HEIGHT field in {slf.name} (vars={mesh['varnames']})",
            details={"slf": str(slf), "varnames": mesh["varnames"]},
        )

    hs = np.asarray(mesh["data"][hs_var])[-1]      # single-frame agitation field
    h0 = max(float(incident_hs_m), 1e-6)
    kd = hs / h0
    x_m = np.asarray(mesh["x"])
    y_m = np.asarray(mesh["y"])
    finite = np.isfinite(kd)
    kd_max = float(np.nanmax(kd[finite])) if finite.any() else 0.0
    if kd_max < _KD_WET_FLOOR:
        raise PostprocessTelemacError(
            "TELEMAC_OUTPUT_EMPTY",
            message=f"Kd never exceeded {_KD_WET_FLOOR} anywhere in {slf.name} "
            f"(peak {kd_max:.4g}) -- a dry/zero-agitation solve?",
            details={"kd_max": kd_max},
        )

    # real-bathy: reproject UTM -> 4326; idealized analytic: keep the local metres
    # frame (utm_epsg None) and stamp a placeholder projected CRS the way the WSE
    # local-frame path does, so the COG still renders on the map.
    if utm_epsg is not None:
        from pyproj import Transformer
        # Add back the LOCAL-frame origin the worker subtracted (AOI SW corner in
        # UTM) so the local mesh metres become TRUE UTM before the inverse to
        # 4326. Without the bbox the offset is unknown and the field would land at
        # the zone origin -- a georef bug, so it refuses rather than guessing.
        x0m, y0m = _local_mesh_origin(
            request_bbox, int(utm_epsg), required=True,
            context="postprocess_artemis")
        back = Transformer.from_crs(int(utm_epsg), 4326, always_xy=True)
        lon, lat = back.transform(x_m + x0m, y_m + y0m)
        lon = np.asarray(lon)
        lat = np.asarray(lat)
        dst_crs = "EPSG:4326"
        pad = 0.0009
    else:
        lon, lat = x_m, y_m         # local metres, rendered in a placeholder frame
        dst_crs = f"EPSG:{_LOCAL_FRAME_EPSG}"
        pad = max(2.0, float(target_ground_res_m))

    bbox = (float(lon.min() - pad), float(lat.min() - pad),
            float(lon.max() + pad), float(lat.max() + pad))
    if utm_epsg is not None:
        shape = _grid_shape(bbox, target_ground_res_m)
        clip_dist = 2.0 * max((bbox[2] - bbox[0]) / shape[1],
                              (bbox[3] - bbox[1]) / shape[0])
    else:
        import math
        w_m = bbox[2] - bbox[0]
        h_loc = bbox[3] - bbox[1]
        res_loc = max(float(target_ground_res_m), _nn_spacing_m(x_m, y_m) * 0.5)
        ncols = min(max(int(round(w_m / res_loc)), TELEMAC_MIN_PX_PER_SIDE), TELEMAC_MAX_PX_PER_SIDE)
        nrows = min(max(int(round(h_loc / res_loc)), TELEMAC_MIN_PX_PER_SIDE), TELEMAC_MAX_PX_PER_SIDE)
        shape = (nrows, ncols)
        clip_dist = 2.0 * _nn_spacing_m(x_m, y_m)

    try:
        grid = _rasterize_nodes_to_grid(
            lon, lat, kd, bbox, shape, clip_dist, wet_floor=_KD_WET_FLOOR)
    except Exception as exc:  # noqa: BLE001
        raise PostprocessTelemacError(
            "TELEMAC_OUTPUT_READ_FAILED",
            message=f"Kd rasterization failed: {exc}",
        ) from exc

    from rasterio.transform import from_bounds

    transform = from_bounds(bbox[0], bbox[1], bbox[2], bbox[3], shape[1], shape[0])
    try:
        cog = cog_io.write_cog_4326_from_grid(
            grid, src_crs=dst_crs, src_transform=transform,
            reproject=False, crs_roundtrip_guard=True,
            dst_suffix="_artemis_agitation.tif",
        )
    except CogIoError as exc:
        raise _reraise_cogio(exc) from exc
    try:
        uri = cog_io.upload_cog(
            cog, run_id, runs_bucket,
            dest_filename="artemis_agitation.tif",
            content_type="image/tiff", gs_backend="fsspec",
            gs_fallback_to_file=False, runs_bucket_default=RUNS_BUCKET_DEFAULT,
            log_label="ARTEMIS Kd COG",
        )
    except CogIoError as exc:
        raise _reraise_cogio(exc) from exc
    finally:
        cog_io.safe_unlink(cog)

    # legend vmax: a robust cap at the 99.5th percentile of the wet field so a
    # single spurious hotspot (a coastline reflection / focus caustic) does not
    # wash the readable 0..~2 agitation range off the ramp. The layer's kd_max
    # metric still carries the TRUE peak (invariant 1); this only styles the COG.
    kd_wet = kd[finite & (kd > _KD_WET_FLOOR)]
    kd_p995 = float(np.percentile(kd_wet, 99.5)) if kd_wet.size else kd_max
    vmax = round(max(min(kd_max, max(kd_p995, 1.0)), 1.0), 3)
    legend = LegendKey(
        kind="continuous", colormap="viridis", vmin=0.0, vmax=vmax, units="Kd",
        label="Agitation coefficient Kd = Hs / H0",
    )
    honesty = (
        "Phase-resolving harbour-agitation screening (ARTEMIS elliptic mild-slope "
        "/ Berkhoff): agitation coefficient Kd = Hs/H0 (how much the incident wave "
        "is amplified or sheltered). A planning-grade field driven by a prescribed "
        "monochromatic incident wave, not a calibrated hindcast."
    )
    layer = ArtemisAgitationLayerURI(
        layer_id=f"artemis-agitation-{run_id}",
        name=f"Wave agitation Kd ({reach_name})",
        layer_type="raster",
        uri=uri,
        style_preset=TELEMAC_AGITATION_STYLE_PRESET,
        role="primary",
        units="Kd",
        bbox=bbox,
        legend=legend,
        fallback_note=honesty,
        kd_max=round(kd_max, 3),
        hs_max_m=round(float(np.nanmax(hs[finite])), 4) if finite.any() else None,
        wave_mode=wave_mode,
    )
    metrics: dict[str, Any] = {
        "hs_var": hs_var.strip(),
        "kd_max": round(kd_max, 3),
        "wave_mode": wave_mode,
        "npoin": int(mesh["npoin"]),
        "nelem": int(mesh["nelem"]),
        "utm_epsg": utm_epsg,
        "bbox": list(bbox),
        "crs": dst_crs,
        "honesty_label": honesty,
    }
    logger.info(
        "postprocess_artemis run_id=%s hs_var=%s kd_max=%.3g mode=%s -> %s",
        run_id, hs_var.strip(), kd_max, wave_mode, uri,
    )
    return [layer], metrics


#: Placeholder projected EPSG the idealized analytic agitation COG is stamped with
#: (its coordinates are in local metres, no real georeferencing) so the raster
#: still renders on the map -- mirrors the WSE local-frame placeholder pattern.
_LOCAL_FRAME_EPSG: int = 3857


# --------------------------------------------------------------------------- #
# TELEMAC-3D stratified / 3D-hydrodynamics (surface + bottom layer COGs, 0241).
# --------------------------------------------------------------------------- #
def _rasterize_t3d_field(
    slf_path, *, run_id, utm_epsg, dest_filename, dst_suffix, log_label,
    runs_bucket, target_ground_res_m, domain_bbox=None,
):
    """Read a single-frame re-emitted 2D SELAFIN (surface OR bottom layer),
    rasterize its one field to a 4326 (or local-frame placeholder) COG, upload it,
    and return ``(uri, bbox, node_min, node_max, node_mean, valid_frac)``
    (``valid_frac`` = the fraction of output pixels carrying a value, the number
    that separates a FIELD from a dot lattice). NO value masking
    (temperature / velocity can be negative and valid) -- only NaN-clipped.

    ``domain_bbox`` is the 4326 AOI the real-lake grid was built over. The 3D build
    lays its mesh with node 0 at that AOI's SW corner, so the re-emitted layer
    SELAFINs carry LOCAL metres; without the corner they reproject as ABSOLUTE UTM
    and both COGs land at the zone's false origin, thousands of km from the lake.
    The idealized basin records no bbox and has no corner to add."""
    import numpy as np

    slf = Path(slf_path)
    try:
        mesh = read_selafin(slf)
    except Exception as exc:  # noqa: BLE001
        raise PostprocessTelemacError(
            "TELEMAC_OUTPUT_READ_FAILED",
            message=f"could not parse TELEMAC-3D layer SELAFIN {slf.name}: {exc}",
            details={"slf": str(slf)},
        ) from exc

    varnames = mesh["varnames"]
    fvar = None
    for v in varnames:                                  # the single re-emitted var
        if mesh["data"].get(v) is not None and mesh["data"][v].size:
            fvar = v
            break
    if fvar is None:
        raise PostprocessTelemacError(
            "TELEMAC_OUTPUT_EMPTY",
            message=f"no field in {slf.name} (vars={varnames})",
            details={"slf": str(slf), "varnames": varnames},
        )
    node_vals = np.asarray(mesh["data"][fvar])[-1]      # single frame
    x = np.asarray(mesh["x"])
    y = np.asarray(mesh["y"])
    finite = np.isfinite(node_vals)
    if not finite.any():
        raise PostprocessTelemacError(
            "TELEMAC_OUTPUT_EMPTY",
            message=f"TELEMAC-3D layer {slf.name} carried no finite values",
            details={"slf": str(slf)},
        )
    node_min = float(np.nanmin(node_vals[finite]))
    node_max = float(np.nanmax(node_vals[finite]))
    node_mean = float(np.nanmean(node_vals[finite]))

    if utm_epsg is not None:
        # real-bathy path: reproject the mesh nodes UTM -> 4326 here and write the
        # COG directly in 4326 (already-4326 direct path, guard on).
        from pyproj import Transformer
        x_org, y_org = _local_mesh_origin(domain_bbox, int(utm_epsg))
        back = Transformer.from_crs(int(utm_epsg), 4326, always_xy=True)
        lon, lat = back.transform(x + x_org, y + y_org)
        lon = np.asarray(lon)
        lat = np.asarray(lat)
        src_crs = "EPSG:4326"
        reproject = False
        guard = True
        pad = 0.0009
        bbox = (float(lon.min() - pad), float(lat.min() - pad),
                float(lon.max() + pad), float(lat.max() + pad))
        shape = _grid_shape(bbox, target_ground_res_m)
    else:
        # idealized path: the coords are LOCAL METRES with no real georeferencing.
        # Treat them as the placeholder projected frame (EPSG:3857) + WARP to 4326
        # so the COG carries valid lon/lat bounds (a small placeholder box) and
        # still renders on the map -- mirrors the WSE/ARTEMIS local-frame intent
        # but via the reproject path (a direct-write would tag local metres as
        # 4326 and trip the projected-coordinate guard).
        lon, lat = x, y                                 # local metres
        src_crs = f"EPSG:{_LOCAL_FRAME_EPSG}"
        reproject = True
        guard = False
        pad = max(2.0, float(target_ground_res_m))
        bbox = (float(lon.min() - pad), float(lat.min() - pad),
                float(lon.max() + pad), float(lat.max() + pad))
        nn = _nn_spacing_m(x, y)
        res_loc = max(float(target_ground_res_m), nn * 0.5)
        w_m = bbox[2] - bbox[0]
        h_m = bbox[3] - bbox[1]
        ncols = min(max(int(round(w_m / res_loc)), TELEMAC_MIN_PX_PER_SIDE), TELEMAC_MAX_PX_PER_SIDE)
        nrows = min(max(int(round(h_m / res_loc)), TELEMAC_MIN_PX_PER_SIDE), TELEMAC_MAX_PX_PER_SIDE)
        shape = (nrows, ncols)

    try:
        # barycentric over the RESULT triangulation: an open-water 3D grid spaces
        # its nodes ~1 km apart, so a nearest-node halo published ~2% valid pixels
        # (a dot lattice). The element fill is the solver's own P1 representation.
        grid = _rasterize_mesh_to_grid(
            lon, lat, mesh["ikle"], node_vals, bbox, shape, wet_floor=-1e30)
    except Exception as exc:  # noqa: BLE001
        raise PostprocessTelemacError(
            "TELEMAC_OUTPUT_READ_FAILED",
            message=f"TELEMAC-3D field rasterization failed: {exc}",
        ) from exc

    valid_frac = float(np.isfinite(grid).sum()) / float(max(grid.size, 1))

    from rasterio.transform import from_bounds

    transform = from_bounds(bbox[0], bbox[1], bbox[2], bbox[3], shape[1], shape[0])
    try:
        cog = cog_io.write_cog_4326_from_grid(
            grid, src_crs=src_crs, src_transform=transform,
            reproject=reproject, crs_roundtrip_guard=guard, dst_suffix=dst_suffix,
        )
    except CogIoError as exc:
        raise _reraise_cogio(exc) from exc
    try:
        uri = cog_io.upload_cog(
            cog, run_id, runs_bucket, dest_filename=dest_filename,
            content_type="image/tiff", gs_backend="fsspec",
            gs_fallback_to_file=False, runs_bucket_default=RUNS_BUCKET_DEFAULT,
            log_label=log_label,
        )
    except CogIoError as exc:
        raise _reraise_cogio(exc) from exc
    finally:
        cog_io.safe_unlink(cog)
    return uri, bbox, node_min, node_max, node_mean, valid_frac


def postprocess_telemac3d(
    surface_slf_path: str | Path,
    bottom_slf_path: str | Path,
    *,
    run_id: str,
    utm_epsg: int | None,
    worker_metrics: dict[str, Any] | None = None,
    reach_name: str = "stratified_flow",
    flow_mode: str = "stratification",
    runs_bucket: str | None = None,
    target_ground_res_m: float = 40.0,
) -> tuple[list[Telemac3dLayerURI], dict[str, Any]]:
    """Rasterize the TELEMAC-3D surface + bottom layers into two COGs.

    Reads the two single-frame re-emitted 2D SELAFINs (``t3d_surface.slf`` /
    ``t3d_bottom.slf`` the worker writes from the 3D result's top / bed sigma
    planes), rasterizes each to a COG (real-bathy reproject ``utm_epsg`` -> 4326,
    or the local-metres placeholder frame for the idealized path), and returns
    ``([surface_layer, bottom_layer], metrics)``. The discriminating scalar
    fields (stratification_dt / u_surface / u_bottom / front_speed_mps / ...)
    come from ``worker_metrics`` (computed off the full 3D column in the worker -
    the agent venv has no TELEMAC), folded onto BOTH layers so the agent narrates
    typed numbers (invariant 1). The full-column time evolution plays from the
    TELEMAC-3D result SELAFIN mesh sibling via ``TELEMAC3D_STRATIFICATION_STYLE_PRESET``.

    Honesty floor (invariant 1): every 3D scalar is plain arithmetic over the
    SELAFIN field -- no LLM. The COG carries an idealized/screening label.

    Raises ``PostprocessTelemacError`` on any read / rasterize / COG failure.
    """
    try:
        import numpy as np  # noqa: F401
        from pyproj import Transformer  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        raise PostprocessTelemacError(
            "TELEMAC_DEPENDENCY_MISSING",
            message=f"numpy/pyproj unavailable for TELEMAC-3D postprocess: {exc}",
        ) from exc

    wm = dict(worker_metrics or {})
    units = wm.get("variable_units") or ""
    var_label = wm.get("variable_label") or "Surface field"
    metric = float(wm.get("stratification_metric") or 0.0)

    s_uri, s_bbox, s_min, s_max, s_mean, s_frac = _rasterize_t3d_field(
        surface_slf_path, run_id=run_id, utm_epsg=utm_epsg,
        dest_filename="telemac3d_surface.tif", dst_suffix="_t3d_surface.tif",
        log_label="TELEMAC-3D surface COG", runs_bucket=runs_bucket,
        target_ground_res_m=target_ground_res_m, domain_bbox=wm.get("bbox"))
    b_uri, b_bbox, b_min, b_max, b_mean, b_frac = _rasterize_t3d_field(
        bottom_slf_path, run_id=run_id, utm_epsg=utm_epsg,
        dest_filename="telemac3d_bottom.tif", dst_suffix="_t3d_bottom.tif",
        log_label="TELEMAC-3D bottom COG", runs_bucket=runs_bucket,
        target_ground_res_m=target_ground_res_m, domain_bbox=wm.get("bbox"))

    # shared diverging/continuous legend over the combined surface+bottom range so
    # the two layers read on ONE ramp (the surface-vs-bottom contrast is the point).
    lo = round(min(s_min, b_min), 5)
    hi = round(max(s_max, b_max), 5)
    if hi <= lo:
        hi = lo + 1e-3
    # a signed field (velocity) reads on a diverging ramp centered on 0; a strictly
    # positive field (temperature / salinity) reads on a sequential ramp.
    signed = lo < 0.0 < hi
    colormap = "rdbu" if signed else "viridis"
    vext = round(max(abs(lo), abs(hi)), 5)
    legend_common = dict(units=units or None, label=f"{var_label} ({units})" if units else var_label)

    honesty = (
        "TELEMAC-3D 3D-hydrodynamics screening: the surface + bottom layers of a "
        f"{flow_mode} field (the vertical structure a 2D depth-averaging cannot "
        "resolve). A planning-grade idealized/prescribed-forcing field, not a "
        "calibrated site study."
    )
    if flow_mode == "stratification":
        # the deck has no THERMIC / no met forcing, so nothing can remove heat
        honesty += (
            " The deck carries NO surface heat exchange: heat is CONSERVED, so a "
            "falling surface temperature is the warm layer MIXING DOWNWARD, not "
            "the lake cooling."
        )
    vlabel = wm.get("vertical_resolution_label")
    if vlabel:
        honesty += f" Vertical fidelity: {vlabel}."
    if wm.get("n_clamped_nodes"):
        honesty += (
            f" {int(wm['n_clamped_nodes'])} grid nodes the DEM reports as land or "
            "sub-threshold shallows were clamped wet for solver stability and read "
            "NoData in these rasters."
        )

    def _mk(uri, bbox, role, is_surface, node_mean):
        if signed:
            legend = LegendKey(kind="continuous", colormap=colormap,
                               vmin=-vext, vmax=vext, **legend_common)
        else:
            legend = LegendKey(kind="continuous", colormap=colormap,
                               vmin=lo, vmax=hi, **legend_common)
        which = "Surface" if is_surface else "Bottom"
        return Telemac3dLayerURI(
            layer_id=f"telemac3d-{'surface' if is_surface else 'bottom'}-{run_id}",
            name=f"{which} {var_label.split(' ', 1)[-1] if ' ' in var_label else var_label} ({reach_name})",
            layer_type="raster",
            uri=uri,
            style_preset=TELEMAC3D_STRATIFICATION_STYLE_PRESET,
            role=role,
            units=units or None,
            bbox=bbox,
            legend=legend,
            fallback_note=honesty,
            stratification_metric=metric,
            flow_mode=flow_mode,
            variable_label=var_label,
            variable_units=units or None,
            stratification_dt=wm.get("stratification_dt"),
            u_surface=wm.get("u_surface"),
            u_bottom=wm.get("u_bottom"),
            depth_avg_u=wm.get("depth_avg_u"),
            front_speed_mps=wm.get("front_speed_mps"),
            benjamin_speed_mps=wm.get("benjamin_speed_mps"),
            surface_value_mean=wm.get("surface_value_mean"),
            bottom_value_mean=wm.get("bottom_value_mean"),
            nplan=wm.get("nplan"),
            non_hydrostatic=wm.get("non_hydrostatic"),
            wind_speed_mps=wm.get("wind_speed_mps"),
            mesh_size_m=wm.get("dx_m"),
            mesh_resolution_label=(
                f"{'real NOAA lake bathy' if utm_epsg is not None else 'idealized'} "
                f"grid {wm.get('dx_m', target_ground_res_m):g} m x {wm.get('nplan', '?')} planes"
                + (" (coarsened under node budget)" if wm.get("coarsened") else "")
                # vertical resolution is a DECLARED fact alongside horizontal dx_m
                + (f", near-surface layer {wm['vertical_dz_surface_m']:g} m"
                   if wm.get("vertical_dz_surface_m") is not None else "")),
        )

    surface_layer = _mk(s_uri, s_bbox, "primary", True, s_mean)
    bottom_layer = _mk(b_uri, b_bbox, "context", False, b_mean)

    metrics: dict[str, Any] = {
        "flow_mode": flow_mode,
        "stratification_metric": metric,
        "variable_label": var_label,
        "variable_units": units,
        "surface_value_range": [s_min, s_max],
        "bottom_value_range": [b_min, b_max],
        "utm_epsg": utm_epsg,
        "surface_bbox": list(s_bbox),
        "surface_valid_pixel_fraction": round(s_frac, 4),
        "bottom_valid_pixel_fraction": round(b_frac, 4),
        "vertical_dz_surface_m": wm.get("vertical_dz_surface_m"),
        "vertical_dz_uniform_m": wm.get("vertical_dz_uniform_m"),
        "mesh_transformation": wm.get("mesh_transformation"),
        "mesh_stretching_coefficients": wm.get("mesh_stretching_coefficients"),
        "thermocline_delta_m": wm.get("thermocline_delta_m"),
        "n_clamped_nodes": wm.get("n_clamped_nodes"),
        "column_heat_drift_frac": wm.get("column_heat_drift_frac"),
        "honesty_label": honesty,
    }
    logger.info(
        "postprocess_telemac3d run_id=%s mode=%s metric=%.4g surf=[%.3g,%.3g] "
        "bot=[%.3g,%.3g] valid_px=%.1f%%/%.1f%% -> %s , %s",
        run_id, flow_mode, metric, s_min, s_max, b_min, b_max,
        100.0 * s_frac, 100.0 * b_frac, s_uri, b_uri,
    )
    return [surface_layer, bottom_layer], metrics


# --------------------------------------------------------------------------- #
# Coastal tidal/surge: the PEAK-INUNDATION-DEPTH COG + flooded area.
# --------------------------------------------------------------------------- #
def _initially_dry_mask(mesh: Any, depth: Any, init_wl_m: Any) -> tuple[Any, str]:
    """The t=0 wet/dry mask: True where a node was DRY before the tide arrived.

    Two routes to the same discrimination, in preference order, because the answer
    layer has to mean the same thing as ``flooded_land_km2``:

    1. the worker's own rule - ``BOTTOM > init_wl`` - reproduced from the result
       SELAFIN's static bed and the DATUM-CORRECTED initial water line the worker
       cold-started from. This is the definition the scalar already uses.
    2. frame 0 of WATER DEPTH, when the result carries no bed or the run reported
       no initial stage. TELEMAC cold-starts ``H = max(0, init_wl - B)``, so a
       dry-at-t0 node is exactly one whose first frame is at the dry floor; it is
       the same discrimination read off the field instead of off the bed.

    Returned with the label of the route that ran, because "which land was already
    under water" is a statement the reader is entitled to check.
    """
    import numpy as np

    bed_var = _pick_named_var(mesh["varnames"], _BED_VAR_KEYS, "B")
    bed = mesh["data"].get(bed_var) if bed_var else None
    if bed is not None and getattr(bed, "size", 0) and init_wl_m is not None:
        bed0 = np.asarray(bed)[0]
        return (bed0 > float(init_wl_m),
                f"bed above the {float(init_wl_m):.4g} m initial water line "
                "(the worker's own flooded-land rule)")
    return (np.asarray(depth)[0] <= TELEMAC_WSE_WET_DEPTH_M,
            f"WATER DEPTH at t=0 at or below the {TELEMAC_WSE_WET_DEPTH_M} m dry "
            "floor (the result carried no bed / no initial stage)")


def postprocess_coastal(
    slf_path: str | Path,
    *,
    run_id: str,
    utm_epsg: int,
    domain_bbox: Sequence[float],
    reach_name: str = "coast",
    worker_metrics: dict[str, Any] | None = None,
    runs_bucket: str | None = None,
    target_ground_res_m: float = 30.0,
) -> tuple[list[TelemacCoastalLayerURI], dict[str, Any]]:
    """Rasterize a solved COASTAL result into an INUNDATION layer and its context.

    TWO products, because one raster was answering two questions at once. The
    PRIMARY is peak depth over land that was DRY at t=0 (``coastal_inundation.tif``)
    - the planning quantity, the same discrimination ``flooded_land_km2`` counts,
    so the picture and the scalar finally agree. Beside it, as ``role="context"``,
    the full peak WATER DEPTH field (``coastal_depth_max.tif``) including the
    permanently submerged bay, honestly named: it is where the water is, not where
    the tide went.

    The storm-tide analogue of :func:`postprocess_tomawac`: reads ``slf_path``
    (``res_coastal.slf``), takes the per-node MAX-over-time WATER DEPTH masked to
    wet nodes, reprojects the mesh ``utm_epsg`` -> EPSG:4326, rasterizes both
    fields onto one adaptive 4326 grid, uploads both COGs, and returns
    ``([inundation, water_depth], metrics)``. The rising-tide animation plays from
    the coastal result SELAFIN mesh sibling ``export_case_to_qgis`` discovers via
    ``TELEMAC_COASTAL_DEPTH_STYLE_PRESET``.

    The coastal worker writes LOCAL (origin-shifted) mesh coordinates into the
    result SELAFIN, so ``domain_bbox`` (the 4326 AOI the domain was built over) is
    REQUIRED to recover the UTM origin ``(min easting, min northing)`` added back
    before the ``utm_epsg`` -> 4326 reprojection -- exactly as the coastal build
    georeferences its bed. Without it the COG would land at the UTM false-origin.

    The flooded-LAND discriminant (newly-inundated area, km^2) is computed inside
    the worker (dry-at-t0 land that goes wet at peak stage) and folded in from
    ``worker_metrics`` -- the A/B storm-surge-vs-calm-tide signal. Honesty floor
    (invariant 1): every depth/area scalar is plain arithmetic over the field --
    no LLM.

    Raises ``PostprocessTelemacError`` on any read / rasterize / COG failure.
    """
    try:
        import numpy as np
        from pyproj import Transformer  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        raise PostprocessTelemacError(
            "TELEMAC_DEPENDENCY_MISSING",
            message=f"numpy/pyproj unavailable for coastal postprocess: {exc}",
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

    import numpy as np

    depth_var = _pick_named_var(mesh["varnames"], _DEPTH_VAR_KEYS, "H")
    if depth_var is None or mesh["data"].get(depth_var) is None \
            or mesh["data"][depth_var].size == 0:
        raise PostprocessTelemacError(
            "TELEMAC_OUTPUT_EMPTY",
            message=f"no WATER DEPTH variable / no time steps in {slf.name} "
            f"(vars={mesh['varnames']})",
            details={"slf": str(slf), "varnames": mesh["varnames"]},
        )

    depth = np.asarray(mesh["data"][depth_var])          # (nframes, npoin), metres
    times = np.asarray(mesh["times"])
    x_utm = np.asarray(mesh["x"])
    y_utm = np.asarray(mesh["y"])

    # per-node peak inundation depth over ONLY the wet frames; never-wet -> NaN.
    import warnings

    wet = depth > TELEMAC_WSE_WET_DEPTH_M
    masked = np.where(wet, depth, np.nan)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        node_peak = np.nanmax(masked, axis=0) if masked.shape[0] else np.full(
            x_utm.size, np.nan)
    finite = np.isfinite(node_peak)
    if not finite.any():
        raise PostprocessTelemacError(
            "TELEMAC_OUTPUT_EMPTY",
            message=f"no wet node in {slf.name}: WATER DEPTH never exceeded "
            f"{TELEMAC_WSE_WET_DEPTH_M} m anywhere (dry solve?)",
            details={"slf": str(slf), "wet_depth_m": TELEMAC_WSE_WET_DEPTH_M},
        )
    peak_depth = float(np.nanmax(node_peak[finite]))

    from pyproj import Transformer

    # the coastal SELAFIN carries LOCAL (0-origin) mesh coordinates; add back the
    # UTM origin (min easting/northing over the AOI corners, matching the build)
    # before reprojecting, else the COG lands at the UTM false-origin.
    x_org, y_org = _local_mesh_origin(
        domain_bbox, int(utm_epsg), required=True, context="postprocess_coastal")
    back = Transformer.from_crs(int(utm_epsg), 4326, always_xy=True)
    lon, lat = back.transform(x_utm + x_org, y_utm + y_org)
    lon = np.asarray(lon)
    lat = np.asarray(lat)

    pad = 0.0009
    bbox = (
        float(lon.min() - pad), float(lat.min() - pad),
        float(lon.max() + pad), float(lat.max() + pad),
    )
    wm = worker_metrics or {}
    # The ANSWER field: peak depth over land that was dry before the tide arrived.
    # Initially-wet nodes go NaN, so the interpolator drops every element they
    # touch and the permanent bay is nodata rather than a painted "inundation".
    dry0, dry0_basis = _initially_dry_mask(mesh, depth, wm.get("init_wl_m"))
    inundation = np.where(dry0, node_peak, np.nan)
    n_inundated = int(np.isfinite(inundation).sum())

    shape = _grid_shape(bbox, target_ground_res_m)

    def _grid_of(values: Any, label: str) -> Any:
        # barycentric over the coastal mesh's own elements (a ~250 m grid under a
        # nearest-node halo published a dot lattice, not an inundation field).
        # Values are passed UNFILTERED so the element table still indexes them:
        # a masked node is NaN, and the interpolator drops the elements it
        # touches - the dry rim is nodata, never an interpolated depth.
        try:
            return _rasterize_mesh_to_grid(
                lon, lat, mesh["ikle"], values, bbox, shape,
                wet_floor=TELEMAC_WSE_WET_DEPTH_M)
        except Exception as exc:  # noqa: BLE001
            raise PostprocessTelemacError(
                "TELEMAC_OUTPUT_READ_FAILED",
                message=f"coastal {label} rasterization failed: {exc}",
            ) from exc

    from rasterio.transform import from_bounds

    transform = from_bounds(bbox[0], bbox[1], bbox[2], bbox[3], shape[1], shape[0])

    def _cog_of(grid: Any, suffix: str, dest: str, label: str) -> str:
        try:
            cog = cog_io.write_cog_4326_from_grid(
                grid, src_crs="EPSG:4326", src_transform=transform,
                reproject=False, crs_roundtrip_guard=True, dst_suffix=suffix,
            )
        except CogIoError as exc:
            raise _reraise_cogio(exc) from exc
        try:
            return cog_io.upload_cog(
                cog, run_id, runs_bucket, dest_filename=dest,
                content_type="image/tiff", gs_backend="fsspec",
                gs_fallback_to_file=False, runs_bucket_default=RUNS_BUCKET_DEFAULT,
                log_label=label,
            )
        except CogIoError as exc:
            raise _reraise_cogio(exc) from exc
        finally:
            cog_io.safe_unlink(cog)

    inundation_uri = _cog_of(
        _grid_of(inundation, "inundation depth"), "_coastal_inundation_4326.tif",
        "coastal_inundation.tif", "TELEMAC coastal inundation COG")
    uri = _cog_of(
        _grid_of(node_peak, "water depth"), "_coastal_depth_4326.tif",
        "coastal_depth_max.tif", "TELEMAC coastal water-depth COG")

    flooded_land_km2 = float(wm.get("flooded_land_km2") or 0.0)
    wet_area_km2 = wm.get("wet_peak_km2")
    peak_wl_m = wm.get("peak_wl_max_m")
    sl_peak_m = wm.get("sl_max_m")
    series_type = wm.get("series_type")
    ocean_edge = wm.get("ocean_edge")

    inundation_peak = (float(np.nanmax(inundation[np.isfinite(inundation)]))
                       if n_inundated else 0.0)
    mesh_label = (
        f"real NOAA DEM_all topobathy grid {wm.get('dx_m', target_ground_res_m):g} m"
        + (" (coarsened under node budget)" if wm.get("coarsened") else ""))
    scalars: dict[str, Any] = dict(
        peak_depth_m=round(peak_depth, 4),
        flooded_land_km2=round(flooded_land_km2, 5),
        wet_area_km2=round(float(wet_area_km2), 5) if wet_area_km2 is not None else None,
        peak_wl_m=round(float(peak_wl_m), 4) if peak_wl_m is not None else None,
        sl_peak_m=round(float(sl_peak_m), 4) if sl_peak_m is not None else None,
        inundation_peak_depth_m=round(inundation_peak, 4),
        inundation_basis=dry0_basis,
        series_type=series_type,
        series_datum=wm.get("series_datum"),
        datum_offset_m=wm.get("datum_offset_m"),
        station_id=wm.get("station_id"),
        station_name=wm.get("station_name"),
        ocean_edge=ocean_edge,
        mesh_size_m=wm.get("dx_m"),
        mesh_resolution_label=mesh_label,
    )
    shared = (
        "Coastal tidal/surge (TELEMAC-2D SAINT-VENANT + TIDAL FLATS): an open-water "
        "domain driven at the seaward boundary by a NOAA CO-OPS / GTSM water-level "
        "series through the LIQUID BOUNDARIES FILE. Planning-grade screening (real "
        "topobathy + observed stage), not a calibrated hindcast; the tide datum is "
        "reconciled to the DEM by a labeled offset."
    )
    honesty = (
        "Peak water depth over land that was DRY at t=0 - the flooding the tide "
        f"CAUSED, on the same discrimination ({dry0_basis}) that "
        f"flooded_land_km2 counts. Permanently submerged water is nodata here; it "
        "is published beside this as the total water-depth context layer. " + shared
    )
    context_honesty = (
        "TOTAL peak water depth, INCLUDING the permanently submerged bay - this is "
        "where the water is, not where the tide went. The planning answer is the "
        "inundation layer beside it; read this one for the whole water column. "
        + shared
    )
    inundation_layer = TelemacCoastalLayerURI(
        layer_id=f"telemac-coastal-inundation-{run_id}",
        name=f"Peak inundation depth over initially-dry land ({reach_name})",
        layer_type="raster",
        uri=inundation_uri,
        style_preset=TELEMAC_COASTAL_DEPTH_STYLE_PRESET,
        role="primary",
        units="m",
        bbox=bbox,
        legend=LegendKey(
            kind="continuous", colormap="YlGnBu", vmin=0.0,
            vmax=round(max(inundation_peak, TELEMAC_WSE_WET_DEPTH_M), 4), units="m",
            label="Peak inundation depth over initially-dry land (m)"),
        fallback_note=honesty,
        **scalars,
    )
    water_depth_layer = TelemacCoastalLayerURI(
        layer_id=f"telemac-coastal-depth-{run_id}",
        name=f"Total water depth at peak ({reach_name})",
        layer_type="raster",
        uri=uri,
        style_preset=TELEMAC_COASTAL_DEPTH_STYLE_PRESET,
        role="context",
        units="m",
        bbox=bbox,
        legend=LegendKey(
            kind="continuous", colormap="YlGnBu", vmin=0.0,
            vmax=round(max(peak_depth, TELEMAC_WSE_WET_DEPTH_M), 4), units="m",
            label="Total water depth at peak (m)"),
        fallback_note=context_honesty,
        **scalars,
    )
    metrics: dict[str, Any] = {
        "depth_var": depth_var.strip(),
        "peak_depth_m": round(peak_depth, 4),
        "inundation_peak_depth_m": round(inundation_peak, 4),
        "inundation_basis": dry0_basis,
        "flooded_land_km2": round(flooded_land_km2, 5),
        "n_frames": int(times.size),
        "n_wet_nodes": int(finite.sum()),
        "n_inundated_nodes": n_inundated,
        "npoin": int(mesh["npoin"]),
        "nelem": int(mesh["nelem"]),
        "utm_epsg": int(utm_epsg),
        "bbox": list(bbox),
        "crs": "EPSG:4326",
        "honesty_label": honesty,
    }
    logger.info(
        "postprocess_coastal run_id=%s depth_var=%s peak_depth=%.4g m "
        "inundation_peak=%.4g m flooded_land=%.4g km^2 n_wet=%d/%d "
        "n_inundated=%d -> %s + %s",
        run_id, depth_var.strip(), peak_depth, inundation_peak, flooded_land_km2,
        int(finite.sum()), int(x_utm.size), n_inundated, inundation_uri, uri,
    )
    return [inundation_layer, water_depth_layer], metrics
